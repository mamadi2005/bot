"""Hybrid architecture: webhook bot on Render + scheduled news runs.

One news run is: fetch -> filter -> triage -> summarize (Persian) -> deliver ->
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
import unicodedata
from datetime import datetime, timedelta

import config
from analyzer import AIBudget, Analyzer, AnalyzerError, fallback_summary
from fetcher import Item, enrich_full_text, fetch_all
from filter import finalize_candidates, prefilter
from storage import ChannelStore, NewsStore, UserStore, iso, open_database, parse_iso, stores, utc_now
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
# Persian enforcement
# --------------------------------------------------------------------------- #
_PERSIAN = re.compile(r"[\u0600-\u06FF]")
_LATIN = re.compile(r"[A-Za-z]")


def _script_counts(text: str) -> tuple[int, int]:
    plain = re.sub(r"<[^>]*>", " ", text or "")
    plain = re.sub(r"https?://\S+", " ", plain)
    return len(_PERSIAN.findall(plain)), len(_LATIN.findall(plain))


def _script_shares(text: str) -> tuple[float, float]:
    persian, latin = _script_counts(text)
    total = persian + latin
    return (0.0, 0.0) if total == 0 else (persian / total, latin / total)


def persian_share(text: str) -> float:
    return _script_shares(text)[0]


def looks_persian(text: str) -> bool:
    persian, _ = _script_counts(text)
    return persian >= 30 and persian_share(text) >= 0.45


def looks_english(text: str) -> bool:
    _, latin = _script_counts(text)
    return latin >= 30 and _script_shares(text)[1] >= 0.45


def normalize_persian(text: str) -> str:
    """Unify Arabic/Persian variants so output reads consistently Persian."""
    return unicodedata.normalize("NFC", text or "").replace("\u064A", "\u06CC").replace("\u0643", "\u06A9")


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
        "⚠️ <b>خطای اجرای ربات</b>\n"
        f"<code>{html.escape(detail)}</code>\n"
        "<i>لاگ اجرای ربات را بررسی کنید.</i>",
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
def _store_summaries(item: Item, texts: dict[str, str]) -> None:
    item.summaries = dict(texts)
    item.summary_html = texts.get(config.DEFAULT_LANG) or next(iter(texts.values()), "")


def _fallback(item: Item, lang: str) -> str:
    return sanitize_html(fallback_summary(item, lang))


def _analyze(
    candidates: list[Item],
    no_ai: bool,
    budget: AIBudget,
    stats: dict,
    langs: list[str],
    dry_run: bool = False,
) -> tuple[list[Item], bool]:
    """Triage + summarize in every language the recipients use."""
    langs = [
        lang for lang in (langs or [config.DEFAULT_LANG]) if lang in config.SUPPORTED_LANGS
    ]
    if not langs:
        langs = [config.DEFAULT_LANG]
    if not candidates:
        return [], False

    if no_ai:
        selected = candidates[:3]
        for item in selected:
            _store_summaries(item, {lang: _fallback(item, lang) for lang in langs})
        log.info("no-ai mode selected=%d", len(selected))
        return selected, False

    try:
        analyzer = Analyzer(config.GEMINI_API_KEYS, budget)
    except AnalyzerError as exc:
        log.warning("analyzer unavailable: %s", exc)
        selected = candidates[:3]
        for item in selected:
            _store_summaries(item, {lang: _fallback(item, lang) for lang in langs})
        return selected, False

    log.info("analyzer keys=%d langs=%s", analyzer.key_count, ",".join(langs))
    selected = analyzer.triage(candidates)
    if selected is None:
        fallback_items = candidates[:3]
        for item in fallback_items:
            _store_summaries(item, {lang: _fallback(item, lang) for lang in langs})
        log.warning("triage unavailable; delivering top=%d with plain summaries", len(fallback_items))
        return fallback_items, budget.daily_remaining() <= 0

    approved: list[Item] = []
    quota_exhausted = False
    rejected = 0
    for item in selected[: config.MAX_ITEMS_PER_RUN]:
        texts: dict[str, str] = {}
        skipped = False
        for index, lang in enumerate(langs):
            result = analyzer.summarize(item, lang)
            if result.status == "quota":
                quota_exhausted = True
                texts[lang] = _fallback(item, lang)
                continue
            if result.status == "skip":
                if index == 0 and not texts:
                    skipped = True
                    break
                continue
            text = sanitize_html(result.text or "")
            valid_language = looks_persian(text) if lang == "fa" else looks_english(text)
            if not valid_language:
                rejected += 1
                share = persian_share(text) if lang == "fa" else _script_shares(text)[1]
                log.warning(
                    "wrong-language summary rejected lang=%s source=%s share=%.2f",
                    lang,
                    item.source_label,
                    share,
                )
                text = _fallback(item, lang)
            elif lang == "fa":
                text = normalize_persian(text)
            texts[lang] = text or _fallback(item, lang)
        if skipped or not texts:
            continue
        _store_summaries(item, texts)
        approved.append(item)

    stats["ai_calls"] = budget.used_this_run
    log.info(
        "analysis approved=%d ai_calls=%d rejected_wrong_language=%d daily_remaining=%d",
        len(approved),
        budget.used_this_run,
        rejected,
        budget.daily_remaining(),
    )
    return approved, quota_exhausted


# --------------------------------------------------------------------------- #
# Delivery
# --------------------------------------------------------------------------- #
def _summary_langs() -> list[str]:
    """All supported languages, so mid-run preference changes still match."""
    langs = list(config.SUPPORTED_LANGS)
    if config.DEFAULT_LANG in langs:
        langs.remove(config.DEFAULT_LANG)
        langs.insert(0, config.DEFAULT_LANG)
    return langs or [config.DEFAULT_LANG]


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
            int(value)
        except (TypeError, ValueError):
            continue
        unique.append(value)
    if not unique and config.ADMIN_CHAT_ID:
        unique = [str(config.ADMIN_CHAT_ID)]
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
        if not item.summaries and not item.summary_html:
            continue
        delivered = 0
        for chat_id in recipients:
            if chat_id in blocked:
                continue
            if not gated.get(chat_id, True):
                continue
            lang = users.lang(chat_id)
            text = item.summaries.get(lang) or _fallback(item, lang)
            if not text:
                continue
            notice = config.news_expiry_warning(lang)
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
    stats = {"updates": 0, "messages": 0, "ai_calls": 0, "recipients": 0}
    result: dict = {
        "ok": True,
        "dry_run": dry_run,
        "fetched": 0,
        "approved": 0,
        "messages": 0,
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
        sender = None if dry_run else TelegramSender(config.TELEGRAM_BOT_TOKEN)

        if sender is not None:
            expired = cleanup_expired(store, sender)
            result["deleted"] = expired["deleted"]
        if cleanup_only:
            result["elapsed"] = round(time.monotonic() - started, 1)
            return result

        now = utc_now()
        today = now.astimezone(config.TEHRAN_TZ).date().isoformat()

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

        recipients = _recipients(users)
        langs = _summary_langs()
        approved, quota_exhausted = _analyze(
            candidates, no_ai, budget, stats, langs, dry_run=dry_run
        )
        result["approved"] = len(approved)

        if dry_run:
            _print_preview(approved)
        else:
            deliver_news(approved, store, sender, users, channels, now, stats, quota_exhausted)
            if config.ADMIN_CHAT_ID and stats["recipients"]:
                admin_lang = users.lang(config.ADMIN_CHAT_ID)
                if approved:
                    summary = (
                        f"✅ {len(approved)} new item(s) sent to "
                        f"{stats['recipients']} user(s)."
                        if admin_lang == "en"
                        else f"✅ خبر جدید ارسال شد: {len(approved)} مورد به "
                        f"{stats['recipients']} کاربر."
                    )
                else:
                    summary = (
                        "⚠️ The run finished but nothing was sent."
                        if admin_lang == "en"
                        else "⚠️ اجرای خبر تمام شد اما خبری ارسال نشد."
                    )
                _notify_admin(sender, summary)

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
