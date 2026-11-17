"""Orchestration: fetch -> filter -> triage -> summarize -> deliver."""

from __future__ import annotations

import argparse
import html
import json
import logging
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import config
from analyzer import AIBudget, Analyzer, AnalyzerError, fallback_summary
from fetcher import Item, enrich_full_text, fetch_all
from filter import finalize_candidates, prefilter
from pdf_report import build_daily_pdf, pdf_caption
from store import JsonStore, MemoryStore, StoreError
from telegram_sender import (
    TelegramSender,
    handle_updates,
    sanitize_html,
)

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
        text = re.sub(r"bot\d{6,}:[A-Za-z0-9_-]{20,}", "bot***", text)
        return text


def _secrets() -> list[str]:
    return [
        config.TELEGRAM_BOT_TOKEN,
        config.GEMINI_API_KEY,
        config.GITHUB_TOKEN,
    ]


def _redact(text: str) -> str:
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
        RedactFormatter(
            "%(asctime)s | %(levelname)-7s | %(name)-15s | %(message)s",
            _secrets(),
        )
    )
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)
    for noisy in ("urllib3", "trafilatura", "charset_normalizer"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Security intelligence bot")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="fetch, score and summarize without sending or writing state",
    )
    parser.add_argument(
        "--no-ai", action="store_true", help="skip Gemini and use plain summaries"
    )
    parser.add_argument(
        "--limit", type=int, default=0, help="cap the number of candidates"
    )
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


def _safe_admin_alert(
    sender: TelegramSender | None, exc: Exception, dry_run: bool
) -> None:
    if dry_run:
        return
    detail = _redact(f"{exc.__class__.__name__}: {exc}")[:600]
    _notify_admin(
        sender,
        "⚠️ <b>خطای اجرای ربات</b>\n"
        f"<code>{html.escape(detail)}</code>\n"
        "<i>لاگ کامل را در GitHub Actions بررسی کنید.</i>",
    )


def _onboarding(
    sender: TelegramSender, store, stats: dict
) -> None:
    offset = _int_state(store, "last_update_id", 0)
    updates = sender.get_updates(offset)
    stats["updates"] = len(updates)
    if not updates:
        return
    last_update_id = handle_updates(updates, sender)
    if last_update_id:
        store.set_state("last_update_id", str(last_update_id + 1))
    log.info("onboarding updates=%d next_offset=%d", len(updates), last_update_id + 1)


def _analyze(
    candidates: list[Item],
    args: argparse.Namespace,
    budget: AIBudget,
    stats: dict,
) -> tuple[list[Item], bool]:
    if not candidates:
        return [], False

    if args.no_ai:
        selected = candidates[:3]
        for item in selected:
            item.summary_html = fallback_summary(item)
        log.info("no-ai mode selected=%d", len(selected))
        return selected, False

    try:
        analyzer = Analyzer(config.GEMINI_API_KEY, budget)
    except AnalyzerError as exc:
        log.warning("analyzer unavailable: %s", exc)
        selected = candidates[:3]
        for item in selected:
            item.summary_html = fallback_summary(item)
        return selected, False

    selected = analyzer.triage(candidates)
    if selected is None:
        fallback = candidates[:3]
        for item in fallback:
            item.summary_html = fallback_summary(item)
        log.warning(
            "triage unavailable; delivering top=%d with plain summaries",
            len(fallback),
        )
        return fallback, budget.daily_remaining() <= 0

    approved: list[Item] = []
    quota_exhausted = False
    for item in selected[: config.MAX_ITEMS_PER_RUN]:
        result = analyzer.summarize(item)
        if result.status == "quota":
            quota_exhausted = True
            item.summary_html = result.text or fallback_summary(item)
            approved.append(item)
            continue
        if result.status == "skip":
            continue
        item.summary_html = result.text or fallback_summary(item)
        approved.append(item)

    stats["ai_calls"] = budget.used_this_run
    log.info(
        "analysis approved=%d ai_calls=%d daily_remaining=%d",
        len(approved),
        budget.used_this_run,
        budget.daily_remaining(),
    )
    return approved, quota_exhausted


def _deliver(
    approved: list[Item],
    store,
    sender: TelegramSender | None,
    now: datetime,
    dry_run: bool,
    recipients: list[int | str],
    stats: dict,
) -> None:
    if not approved:
        return
    if dry_run:
        print("\n=== DRY RUN PREVIEW ===", flush=True)
        print(f"approved={len(approved)}", flush=True)
        for item in approved:
            print("-" * 72, flush=True)
            print(
                f"score={item.score:.2f} category={item.category} "
                f"source={item.source_label}",
                flush=True,
            )
            print(item.url, flush=True)
            print(sanitize_html(item.summary_html), flush=True)
        return

    assert sender is not None
    if not recipients:
        log.warning(
            "no recipients: set TELEGRAM_CHAT_IDS (or ADMIN_CHAT_ID) to your "
            "chat id from /start"
        )

    for item in approved:
        delivered = False
        for chat_id in recipients:
            if sender.send_message(chat_id, item.summary_html):
                delivered = True
                stats["messages"] += 1
            time.sleep(config.BROADCAST_DELAY)
        if delivered:
            store.log_sent(
                item.hash,
                item.url,
                item.title,
                item.category,
                now.isoformat(timespec="seconds"),
            )
    log.info(
        "delivery items=%d recipients=%d messages=%d",
        len(approved),
        len(recipients),
        stats["messages"],
    )


def _buffer_pdf_items(store, approved: list[Item], today: str) -> None:
    if not config.ENABLE_PDF or not approved:
        return
    raw = (
        store.get_state("pdf_buffer", "")
        if store.get_state("pdf_buffer_date", "") == today
        else ""
    )
    try:
        buffer = json.loads(raw) if raw else []
    except (TypeError, ValueError):
        buffer = []
    if not isinstance(buffer, list):
        buffer = []
    existing = {
        entry.get("url") for entry in buffer if isinstance(entry, dict)
    }
    for item in approved:
        if item.url in existing:
            continue
        buffer.append(
            {
                "title": item.title,
                "url": item.url,
                "category": item.category,
                "source": item.source_label,
                "html": item.summary_html,
            }
        )
        existing.add(item.url)
    blob = json.dumps(buffer, ensure_ascii=False)
    while len(buffer) > 1 and len(blob) > config.PDF_BUFFER_MAX_CHARS:
        buffer.pop(0)
        blob = json.dumps(buffer, ensure_ascii=False)
    store.set_state("pdf_buffer_date", today)
    store.set_state("pdf_buffer", blob)


def _maybe_send_pdf(
    store,
    sender: TelegramSender | None,
    now: datetime,
    today: str,
    dry_run: bool,
    recipients: list[int | str],
    stats: dict,
) -> None:
    if dry_run or not config.ENABLE_PDF or sender is None:
        return
    if store.get_state("last_pdf_date") == today:
        return
    if now.hour < config.PDF_SEND_HOUR:
        return
    if store.get_state("pdf_buffer_date") != today:
        return
    try:
        buffer = json.loads(store.get_state("pdf_buffer", "") or "[]")
    except (TypeError, ValueError):
        return
    if not isinstance(buffer, list) or not buffer:
        return

    output = Path(config.ROOT_DIR) / "build" / f"security-digest-{today}.pdf"
    path = build_daily_pdf(buffer, output, day=today)
    if path is None:
        return
    content = path.read_bytes()
    targets = list(recipients)
    if not targets:
        log.warning("pdf built but no recipients configured")
        return

    caption = pdf_caption(today, len(buffer))
    sent = False
    for chat_id in targets:
        if sender.send_document(chat_id, path.name, content, caption):
            sent = True
            stats["pdf_documents"] += 1
        time.sleep(config.BROADCAST_DELAY)
    if sent:
        store.set_state("last_pdf_date", today)
        store.set_state("pdf_buffer", "[]")
        log.info("pdf sent targets=%d items=%d", len(targets), len(buffer))


# --------------------------------------------------------------------------- #
# Main pipeline
# --------------------------------------------------------------------------- #
def run(args: argparse.Namespace) -> int:
    started = time.monotonic()
    now = datetime.now(config.TEHRAN_TZ)
    today = now.date().isoformat()
    dry_run = bool(args.dry_run or config.DRY_RUN)

    stats: dict = {
        "updates": 0,
        "messages": 0,
        "pdf_documents": 0,
        "ai_calls": 0,
    }
    store = None
    sender: TelegramSender | None = None
    budget: AIBudget | None = None
    used_today = 0
    had_error = False

    log.info(
        "run_start mode=%s today=%s hour=%d model=%s",
        "dry-run" if dry_run else "live",
        today,
        now.hour,
        config.GEMINI_MODEL,
    )
    recipients = config.recipient_chat_ids()
    log.info("recipients configured=%d", len(recipients))

    try:
        store = MemoryStore() if dry_run else JsonStore(config.STATE_FILE)
        store.load()

        if not dry_run:
            sender = TelegramSender(config.TELEGRAM_BOT_TOKEN)
            _onboarding(sender, store, stats)

        raw_items = fetch_all()
        filter_result = prefilter(raw_items, store.sent_hashes, now)
        candidates = filter_result.items
        enrich_full_text(candidates)
        candidates = finalize_candidates(candidates)
        if args.limit > 0:
            candidates = candidates[: args.limit]
        log.info(
            "candidates fetched=%d stage1=%d final=%d",
            len(raw_items),
            filter_result.counters.get("kept", 0),
            len(candidates),
        )

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
        approved, quota_exhausted = _analyze(candidates, args, budget, stats)

        _deliver(approved, store, sender, now, dry_run, recipients, stats)
        _buffer_pdf_items(store, approved, today)
        _maybe_send_pdf(store, sender, now, today, dry_run, recipients, stats)

        if quota_exhausted:
            log.warning("gemini daily quota exhausted for this run")
            _notify_admin(sender, config.QUOTA_NOTICE)

        counters = filter_result.counters
        log.info(
            "run_summary fetched=%d duplicates=%d stale=%d kept=%d approved=%d "
            "ai_calls=%d messages=%d updates=%d elapsed=%.1fs",
            len(raw_items),
            counters.get("duplicate", 0),
            counters.get("stale", 0),
            counters.get("kept", 0),
            len(approved),
            budget.used_this_run,
            stats["messages"],
            stats["updates"],
            time.monotonic() - started,
        )
    except StoreError as exc:
        had_error = True
        log.error("state error: %s", exc)
        _safe_admin_alert(sender, exc, dry_run)
    except Exception as exc:
        had_error = True
        log.exception("unhandled run failure")
        _safe_admin_alert(sender, exc, dry_run)
    finally:
        if store is not None:
            if budget is not None:
                try:
                    store.set_state("ai_calls_date", today)
                    store.set_state(
                        "ai_calls_today",
                        str(used_today + budget.used_this_run),
                    )
                except Exception:
                    log.exception("failed to update ai counters")
            try:
                store.flush()
            except Exception as exc:
                had_error = True
                log.exception("final flush failed")
                if dry_run is False:
                    _safe_admin_alert(sender, exc, dry_run)

    log.info("run_finished errors=%s elapsed=%.1fs", had_error, time.monotonic() - started)
    return 1 if had_error else 0


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    setup_logging(args.debug)
    try:
        return run(args)
    except KeyboardInterrupt:
        log.warning("interrupted")
        return 130


if __name__ == "__main__":
    sys.exit(main())
