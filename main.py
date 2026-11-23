"""Hybrid architecture: webhook bot on Render + scheduled news runs.

One news run is: fetch -> filter -> triage -> summarize (English) -> deliver ->
expire. The same function powers the CLI (``python main.py``) and the
``/trigger_news`` endpoint that GitHub Actions calls on a schedule.
"""

from __future__ import annotations

import argparse
import html
import logging
import re
import sys
import threading
import time
from datetime import datetime, timedelta

import config
from analyzer import AIBudget, Analyzer, AnalyzerError, fallback_summary
from fetcher import Item, enrich_full_text, fetch_all
from filter import finalize_candidates, prefilter
from storage import ChannelStore, NewsStore, UserStore, iso, open_database, stores, utc_now
from telegram_sender import TelegramSender, handle_updates, sanitize_html

log = logging.getLogger("main")


# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #
class RedactFormatter(logging.Formatter):
    def __init__(self, fmt: str, secrets: list[str]) -> None:
        super().__init__(fmt)
        self.secrets = [value for value in secrets if value and len(value) >= 8]

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        for secret in self.secrets:
            text = text.replace(secret, "***")
        return re.sub(r"bot\d{6,}:[A-Za-z0-9_-]{20,}", "bot***", text)


def _secrets() -> list[str]:
    return [
        config.TELEGRAM_BOT_TOKEN,
        *config.GEMINI_API_KEYS,
        config.GITHUB_TOKEN,
        config.TRIGGER_TOKEN,
    ]


def redact(text: str) -> str:
    for secret in _secrets():
        if secret and len(secret) >= 8:
            text = text.replace(secret, "***")
    return re.sub(r"bot\d{6,}:[A-Za-z0-9_-]{20,}", "bot***", text)


def setup_logging(debug: bool = False) -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    level = logging.DEBUG if (debug or config.DEBUG) else logging.INFO
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        RedactFormatter("%(asctime)s | %(levelname)-7s | %(name)-15s | %(message)s", _secrets())
    )
    root = logging.getLogger()
    if not any(getattr(existing, "_secintel", False) for existing in root.handlers):
        handler._secintel = True  # type: ignore[attr-defined]
        root.handlers.clear()
        root.addHandler(handler)
    root.setLevel(level)
    for noisy in ("urllib3", "trafilatura", "charset_normalizer"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


# --------------------------------------------------------------------------- #
# English-language enforcement
# --------------------------------------------------------------------------- #
_LATIN = re.compile(r"[A-Za-z]")
_ENGLISH_COMMON_WORDS = {
    "a", "about", "after", "also", "and", "are", "as", "at", "be", "been",
    "by", "can", "for", "from", "has", "have", "in", "into", "is", "it",
    "its", "may", "not", "of", "on", "or", "that", "the", "their", "this",
    "to", "was", "were", "will", "with",
}


def _english_counts(text: str) -> tuple[int, int]:
    plain = re.sub(r"<[^>]*>", " ", text or "")
    plain = re.sub(r"https?://\S+", " ", plain)
    letters = len(_LATIN.findall(plain))
    total = letters + len(re.findall(r"[^\W\d_]", plain, re.UNICODE)) - letters
    return letters, total


def looks_english(text: str) -> bool:
    plain = re.sub(r"<[^>]*>", " ", text or "")
    plain = re.sub(r"https?://\S+", " ", plain)
    latin, letters = _english_counts(plain)
    words = re.findall(r"[A-Za-z]+", plain.lower())
    common_words = sum(word in _ENGLISH_COMMON_WORDS for word in words)
    return (
        latin >= 30
        and letters > 0
        and latin / letters >= 0.7
        and common_words >= 2
    )


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Security intelligence bot (news run)")
    parser.add_argument("--dry-run", action="store_true",
                        help="fetch, score and summarize without sending or writing state")
    parser.add_argument("--no-ai", action="store_true", help="skip Gemini and use plain summaries")
    parser.add_argument("--limit", type=int, default=0, help="cap the number of candidates")
    parser.add_argument("--cleanup-only", action="store_true",
                        help="only delete news older than 24h")
    parser.add_argument("--debug", action="store_true", help="verbose logging")
    return parser.parse_args(argv)


def _int_state(store, key: str, default: int = 0) -> int:
    try:
        return int((store.get_state(key, str(default)) or "").strip())
    except (TypeError, ValueError):
        return default


def _notify_admin(sender: TelegramSender | None, text: str) -> None:
    if sender is None or not config.ADMIN_CHAT_ID:
        return
    try:
        sender.send_message(config.ADMIN_CHAT_ID, text)
    except Exception:
        log.warning("admin notification failed")


def _safe_admin_alert(sender: TelegramSender | None, exc: Exception, dry_run: bool) -> None:
    if dry_run:
        return
    detail = redact(f"{exc.__class__.__name__}: {exc}")[:600]
    _notify_admin(
        sender,
        "⚠️ <b>News bot error</b>\n"
        f"<code>{html.escape(detail)}</code>\n"
        "<i>Check the service logs for details.</i>",
    )


# --------------------------------------------------------------------------- #
# Webhook updates (called by server.py)
# --------------------------------------------------------------------------- #
def process_update(update: dict, sender: TelegramSender, users: UserStore, channels: ChannelStore) -> None:
    """Handle a single Telegram update delivered to the webhook."""
    handle_updates([update], sender, users, channels)


def process_updates(
    updates: list[dict], sender: TelegramSender, users: UserStore, channels: ChannelStore
) -> None:
    if updates:
        handle_updates(updates, sender, users, channels)


# --------------------------------------------------------------------------- #
# Analysis
# --------------------------------------------------------------------------- #
def _store_summary(item: Item, text: str) -> None:
    item.summaries = {"en": text}
    item.summary_html = text


def _fallback(item: Item) -> str:
    return sanitize_html(fallback_summary(item))


def _analyze(
    candidates: list[Item],
    no_ai: bool,
    budget: AIBudget,
    stats: dict,
) -> tuple[list[Item], bool]:
    """Triage and summarize each approved item once in English."""
    if not candidates:
        return [], False

    if no_ai:
        selected = candidates[:3]
        for item in selected:
            _store_summary(item, _fallback(item))
        log.info("no-ai mode selected=%d", len(selected))
        return selected, False

    try:
        analyzer = Analyzer(config.GEMINI_API_KEYS, budget)
    except AnalyzerError as exc:
        log.warning("analyzer unavailable: %s", exc)
        selected = candidates[:3]
        for item in selected:
            _store_summary(item, _fallback(item))
        return selected, False

    log.info("analyzer keys=%d language=en", analyzer.key_count)
    selected = analyzer.triage(candidates)
    if selected is None:
        fallback_items = candidates[:3]
        for item in fallback_items:
            _store_summary(item, _fallback(item))
        log.warning("triage unavailable; delivering top=%d with plain summaries", len(fallback_items))
        return fallback_items, budget.daily_remaining() <= 0
    if not selected:
        selected = candidates[:1]
        log.warning(
            "triage approved no candidates; using best prefiltered item count=%d",
            len(selected),
        )

    approved: list[Item] = []
    quota_exhausted = False
    rejected = 0
    for item in selected[: config.MAX_ITEMS_PER_RUN]:
        result = analyzer.summarize(item)
        if result.status == "skip":
            continue
        if result.status == "quota":
            quota_exhausted = True
        text = sanitize_html(result.text or "")
        if not looks_english(text):
            rejected += 1
            log.warning(
                "non-English summary rejected source=%s",
                item.source_label,
            )
            text = _fallback(item)
        _store_summary(item, text or _fallback(item))
        approved.append(item)

    stats["ai_calls"] = budget.used_this_run
    log.info(
        "analysis approved=%d ai_calls=%d rejected_non_english=%d daily_remaining=%d",
        len(approved),
        budget.used_this_run,
        rejected,
        budget.daily_remaining(),
    )
    return approved, quota_exhausted


# --------------------------------------------------------------------------- #
# Delivery
# --------------------------------------------------------------------------- #
def _recipients(users: UserStore, days: int = 0) -> list[int]:
    """Configured chat IDs (never gated) followed by every subscribed user."""
    ordered: list[str] = [str(value) for value in config.recipient_chat_ids()]
    if days:
        cutoff = iso(utc_now() - timedelta(days=days))
        rows = users.db.query(
            "SELECT chat_id FROM users WHERE is_subscribed = 1 AND last_seen >= ? ORDER BY chat_id",
            (cutoff,),
        )
        subscribed = [str(row["chat_id"]) for row in rows]
    else:
        subscribed = [str(chat_id) for chat_id in users.chat_ids(subscribed_only=True)]

    unique: list[str] = []
    for value in ordered + subscribed:
        if value in unique:
            continue
        try:
            chat_id = int(value)
        except (TypeError, ValueError):
            continue
        if users.get(chat_id) is not None and not users.is_subscribed(chat_id):
            continue
        unique.append(value)
    if not unique and config.ADMIN_CHAT_ID:
        admin_id = str(config.ADMIN_CHAT_ID)
        if users.get(admin_id) is None or users.is_subscribed(admin_id):
            unique = [admin_id]
    return [int(value) for value in unique]


def deliver_news(
    approved: list[Item],
    store: NewsStore,
    sender: TelegramSender,
    users: UserStore,
    channels: ChannelStore,
    now: datetime,
    stats: dict,
    quota_notice: bool = False,
) -> None:
    if not approved:
        return

    recipients = _recipients(users)
    stats["recipients"] = len(recipients)
    delivered_users = stats.setdefault("delivered_users", set())
    if not recipients:
        log.warning("no recipients: set TELEGRAM_CHAT_IDS or have users send /start")
        return

    # Membership is checked once per run, not once per article.
    gated: dict[int, bool] = {}
    for chat_id in recipients:
        if not channels.is_enabled() or str(chat_id) in {str(value) for value in config.recipient_chat_ids()}:
            gated[chat_id] = True
            continue
        try:
            gated[chat_id] = not channels.missing_for(sender, chat_id)
        except Exception:
            gated[chat_id] = True  # never drop delivery because the gate errored

    blocked: set[int] = set()

    for item in approved:
        delivered = 0
        for chat_id in recipients:
            if chat_id in blocked:
                continue
            if users.get(chat_id) is not None and not users.is_subscribed(chat_id):
                continue
            if not gated.get(chat_id, True):
                continue
            text = item.summaries.get("en") or _fallback(item)
            if not text:
                continue
            notice = config.news_expiry_warning()
            try:
                sent = sender.send_message(
                    chat_id,
                    text,
                    on_sent=store.record_news_message,
                    warning=notice,
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("delivery failed chat=%s err=%s", chat_id, exc.__class__.__name__)
                continue
            if sent:
                delivered += 1
                stats["messages"] += 1
                delivered_users.add(chat_id)
            else:
                blocked.add(chat_id)
                users.set_subscribed(chat_id, False)
            time.sleep(config.BROADCAST_DELAY)
        if delivered:
            store.log_sent(item.hash, item.url, item.title, item.category, iso(now))

    if quota_notice:
        _notify_admin(sender, config.QUOTA_NOTICE)
    log.info(
        "delivery items=%d recipients=%d messages=%d blocked=%d",
        len(approved),
        len(recipients),
        stats["messages"],
        len(blocked),
    )


# --------------------------------------------------------------------------- #
# Expiry
# --------------------------------------------------------------------------- #
def cleanup_expired(store: NewsStore, sender: TelegramSender, limit: int = 500) -> dict:
    """Delete news messages older than NEWS_LIFETIME_SECONDS."""
    now = utc_now()
    deleted = failed = 0
    for entry in store.due_news_messages(now)[:limit]:
        if sender.delete_message(entry["chat_id"], entry["message_id"]):
            store.remove_news_message(entry)
            deleted += 1
        else:
            failed += 1
    if deleted or failed:
        log.info("cleanup deleted=%d failed=%d", deleted, failed)
    return {"deleted": deleted, "failed": failed}


# --------------------------------------------------------------------------- #
# One news run
# --------------------------------------------------------------------------- #
_RUN_LOCK = threading.Lock()


def run_news(
    *,
    dry_run: bool = False,
    no_ai: bool = False,
    limit: int = 0,
    cleanup_only: bool = False,
    database_url: str = "",
) -> dict:
    """Fetch, summarize and deliver one batch of news. Safe to call from threads."""
    started = time.monotonic()
    stats = {
        "updates": 0,
        "messages": 0,
        "ai_calls": 0,
        "recipients": 0,
        "delivered_users": set(),
    }
    result: dict = {
        "ok": True,
        "dry_run": dry_run,
        "fetched": 0,
        "approved": 0,
        "messages": 0,
        "subscribed_users": 0,
        "users_sent": 0,
        "would_send_to": 0,
        "deleted": 0,
        "elapsed": 0.0,
        "error": "",
    }

    if not _RUN_LOCK.acquire(blocking=False):
        log.warning("news run skipped: another run is in progress")
        result.update(ok=False, error="run already in progress")
        return result

    db = None
    try:
        db = open_database(database_url)
        users, channels, store = stores(db)
        subscribed_users = len(users.chat_ids(subscribed_only=True))
        result["subscribed_users"] = subscribed_users
        log.info("Found %d subscribed users in database", subscribed_users)
        sender = None if dry_run else TelegramSender(config.TELEGRAM_BOT_TOKEN)

        if sender is not None:
            expired = cleanup_expired(store, sender)
            result["deleted"] = expired["deleted"]
        if cleanup_only:
            result["elapsed"] = round(time.monotonic() - started, 1)
            return result

        now = utc_now()
        today = now.astimezone(config.TEHRAN_TZ).date().isoformat()

        log.info("Starting news fetch...")
        raw_items = fetch_all()
        filter_result = prefilter(raw_items, store.sent_hashes, now)
        candidates = filter_result.items
        enrich_full_text(candidates)
        candidates = finalize_candidates(candidates)
        if limit > 0:
            candidates = candidates[:limit]
        result["fetched"] = len(raw_items)

        used_today = (
            _int_state(store, "ai_calls_today", 0)
            if store.get_state("ai_calls_date", "") == today
            else 0
        )
        budget = AIBudget(
            per_run=config.MAX_AI_CALLS_PER_RUN,
            daily=config.DAILY_AI_BUDGET,
            used_today=used_today,
        )

        if candidates:
            log.info("Summarizing news for language: en")
        approved, quota_exhausted = _analyze(candidates, no_ai, budget, stats)
        result["approved"] = len(approved)

        if dry_run:
            result["would_send_to"] = subscribed_users
            log.info(
                "Dry Run Successful: Found %d news items, would send to %d subscribed users",
                len(approved),
                subscribed_users,
            )
            log.info("Dry run mode: skipping Telegram send")
            _print_preview(approved)
        else:
            deliver_news(approved, store, sender, users, channels, now, stats, quota_exhausted)
            result["users_sent"] = len(stats["delivered_users"])
            if stats["delivered_users"]:
                log.info("Successfully sent to %d users", result["users_sent"])
            else:
                log.info("No news messages sent")
            if config.ADMIN_CHAT_ID and stats["recipients"]:
                if approved:
                    summary = (
                        f"✅ {len(approved)} new item(s) sent to "
                        f"{stats['recipients']} user(s)."
                    )
                else:
                    summary = "⚠️ The run finished but nothing was sent."
                _notify_admin(sender, summary)

        if not dry_run:
            store.set_state("ai_calls_date", today)
            store.set_state("ai_calls_today", str(used_today + budget.used_this_run))
            store.set_state("last_run_at", iso())
            store.set_state("last_run_approved", str(len(approved)))
        result["messages"] = stats["messages"]
    except Exception as exc:  # noqa: BLE001
        result.update(ok=False, error=redact(f"{exc.__class__.__name__}: {exc}")[:400])
        log.exception("news run failed")
        if not dry_run:
            try:
                _safe_admin_alert(TelegramSender(config.TELEGRAM_BOT_TOKEN), exc, dry_run)
            except Exception:
                pass
    finally:
        result["elapsed"] = round(time.monotonic() - started, 1)
        log.info(
            "news_run ok=%s fetched=%d approved=%d messages=%d deleted=%d elapsed=%.1fs",
            result["ok"],
            result["fetched"],
            result["approved"],
            result["messages"],
            result["deleted"],
            result["elapsed"],
        )
        _RUN_LOCK.release()
    return result


def _print_preview(approved: list[Item]) -> None:
    print("\n=== DRY RUN PREVIEW ===", flush=True)
    print(f"approved={len(approved)}", flush=True)
    for item in approved:
        print("-" * 72, flush=True)
        print(f"score={item.score:.2f} category={item.category} source={item.source_label}", flush=True)
        print(item.url, flush=True)
        print(sanitize_html(item.summary_html), flush=True)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    setup_logging(args.debug)
    try:
        result = run_news(
            dry_run=args.dry_run or config.DRY_RUN,
            no_ai=args.no_ai,
            limit=args.limit,
            cleanup_only=args.cleanup_only,
        )
    except KeyboardInterrupt:
        log.warning("interrupted")
        return 130
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
