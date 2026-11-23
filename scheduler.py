"""In-process, slot-aware news scheduler.

Delivery must not depend on a single external trigger: Render's free tier sleeps
after inactivity, so a GitHub cron tick can land on a cold instance. This module
decides whether a *scheduled slot* is still unserved and only then runs, which
makes a late wake-up recover the missed slot exactly once instead of sending on
every tick.

Slots are 00:00, 08:00, 12:00, 16:00 and 20:00 Tehran time. ``main.run_news``
has its own lock, so concurrent callers cannot double-send.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta

import config
import main
from storage import NewsStore, iso, open_database, parse_iso, utc_now

log = logging.getLogger("scheduler")

RUN_HOURS = (0, 8, 12, 16, 20)
TICK_SECONDS = 60
SETTING_KEY = "last_slot_served"
ATTEMPT_KEY = "slot_attempts"
TEST_RUN_KEY = "last_test_run_at"
MAX_SLOT_ATTEMPTS = 3

_lock = threading.Lock()
_test_run_lock = threading.Lock()
_thread: threading.Thread | None = None
_state: dict = {"started_at": None, "last_check": None, "last_slot": None, "last_result": None}


def _slot_at_or_before(now: datetime) -> datetime | None:
    current = now.astimezone(config.TEHRAN_TZ)
    for day_offset in (0, 1):
        base = current - timedelta(days=day_offset)
        for hour in sorted(RUN_HOURS, reverse=True):
            candidate = base.replace(hour=hour, minute=0, second=0, microsecond=0)
            if candidate <= current:
                return candidate
    return None


def next_run_at(now: datetime | None = None) -> datetime:
    current = now or datetime.now(config.TEHRAN_TZ)
    current = current.astimezone(config.TEHRAN_TZ)
    for day_offset in (0, 1):
        base = current + timedelta(days=day_offset)
        for hour in RUN_HOURS:
            candidate = base.replace(hour=hour, minute=0, second=0, microsecond=0)
            if candidate > current:
                return candidate
    return current + timedelta(hours=8)


def _last_served(store: NewsStore) -> datetime | None:
    return parse_iso(store.get_state(SETTING_KEY, ""))


def _mark_served(store: NewsStore, slot: datetime) -> None:
    store.set_state(SETTING_KEY, iso(slot))


def _attempts(store: NewsStore, slot: datetime) -> int:
    raw = store.get_state(ATTEMPT_KEY, "")
    try:
        stamp, count = raw.rsplit(":", 1)
        if parse_iso(stamp) == slot:
            return int(count)
    except (ValueError, AttributeError):
        pass
    return 0


def _bump_attempts(store: NewsStore, slot: datetime) -> int:
    count = _attempts(store, slot) + 1
    store.set_state(ATTEMPT_KEY, f"{iso(slot)}:{count}")
    return count


def test_run_due(store: NewsStore, now: datetime | None = None) -> bool:
    current = now or utc_now()
    previous = parse_iso(store.get_state(TEST_RUN_KEY, ""))
    return previous is None or (
        current - previous
    ).total_seconds() >= config.NEWS_TEST_INTERVAL_SECONDS


def due_slot(store: NewsStore, now: datetime | None = None) -> datetime | None:
    """Return the oldest unserved slot, or ``None`` when everything is done."""
    current = now or utc_now()
    slot = _slot_at_or_before(current)
    if slot is None:
        return None
    served = _last_served(store)
    if served is not None and served >= slot:
        return None
    return slot


def run_due(force: bool = False, db=None, **kwargs) -> dict:
    """Run the news job only when a slot is due (or ``force``)."""
    owned = db is None
    db = db or open_database()
    test_lock_acquired = False
    try:
        store = NewsStore(db)
        if config.NEWS_TEST_MODE:
            if not _test_run_lock.acquire(blocking=False):
                return {"ok": True, "skipped": True, "reason": "test run in progress"}
            test_lock_acquired = True
            now = utc_now()
            if not force and not test_run_due(store, now):
                return {"ok": True, "skipped": True, "reason": "test interval not due"}
            store.set_state(TEST_RUN_KEY, iso(now))
            kwargs["limit"] = 1
            return main.run_news(**kwargs)

        slot = _slot_at_or_before(utc_now())
        if not force:
            if slot is None or due_slot(store, utc_now()) is None:
                return {"ok": True, "skipped": True, "reason": "no slot due"}
        result = main.run_news(**kwargs)
        if slot is not None:
            if result.get("ok"):
                _mark_served(store, slot)
            elif _bump_attempts(store, slot) >= MAX_SLOT_ATTEMPTS:
                # Give up on this slot so a broken run cannot retry forever.
                log.warning("slot %s failed %d times; giving up", iso(slot), MAX_SLOT_ATTEMPTS)
                _mark_served(store, slot)
        return result
    finally:
        if test_lock_acquired:
            _test_run_lock.release()
        if owned:
            try:
                db.connect().close()
            except Exception:
                pass


def is_due(now: datetime | None = None) -> bool:
    db = open_database()
    try:
        store = NewsStore(db)
        if config.NEWS_TEST_MODE:
            return test_run_due(store, now)
        return due_slot(store, now) is not None
    finally:
        try:
            db.connect().close()
        except Exception:
            pass


def _loop() -> None:
    while True:
        try:
            with _lock:
                _state["last_check"] = datetime.now(config.TEHRAN_TZ).isoformat(timespec="seconds")
            if is_due():
                result = run_due()
                with _lock:
                    _state["last_slot"] = datetime.now(config.TEHRAN_TZ).isoformat(timespec="seconds")
                    _state["last_result"] = {
                        "ok": result.get("ok"),
                        "approved": result.get("approved"),
                        "messages": result.get("messages"),
                        "error": result.get("error", ""),
                        "skipped": result.get("skipped", False),
                    }
                log.info(
                    "scheduled run ok=%s approved=%s messages=%s",
                    result.get("ok"),
                    result.get("approved"),
                    result.get("messages"),
                )
        except Exception:  # noqa: BLE001
            log.exception("scheduled news check failed")
        poll_seconds = (
            min(10, config.NEWS_TEST_INTERVAL_SECONDS)
            if config.NEWS_TEST_MODE
            else TICK_SECONDS
        )
        threading.Event().wait(poll_seconds)


def start() -> bool:
    """Start the background thread once; returns False when already running."""
    global _thread
    with _lock:
        if _thread is not None and _thread.is_alive():
            return False
        _state["started_at"] = datetime.now(config.TEHRAN_TZ).isoformat(timespec="seconds")
        _thread = threading.Thread(target=_loop, daemon=True, name="news-scheduler")
        _thread.start()
    if config.NEWS_TEST_MODE:
        log.info(
            "scheduler started test_mode=true lookback_days=%d interval_seconds=%d",
            config.NEWS_TEST_LOOKBACK_DAYS,
            config.NEWS_TEST_INTERVAL_SECONDS,
        )
    else:
        log.info("scheduler started slots=%s", RUN_HOURS)
    return True


def status() -> dict:
    with _lock:
        payload = dict(_state)
    if config.NEWS_TEST_MODE:
        db = open_database()
        try:
            previous = parse_iso(NewsStore(db).get_state(TEST_RUN_KEY, ""))
        finally:
            try:
                db.connect().close()
            except Exception:
                pass
        next_test = (
            previous + timedelta(seconds=config.NEWS_TEST_INTERVAL_SECONDS)
            if previous is not None
            else utc_now()
        )
        payload["next_run"] = next_test.astimezone(config.TEHRAN_TZ).isoformat(
            timespec="minutes"
        )
    else:
        payload["next_run"] = next_run_at().isoformat(timespec="minutes")
    return payload
