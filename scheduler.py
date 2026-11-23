"""Slot-aware news runs invoked by an external trigger.

This module contains no polling loop. A GitHub Actions request wakes Render and
calls ``run_due``, which decides whether the current slot is unserved.

Slots are 00:00, 08:00, 12:00, 16:00 and 20:00 Tehran time. ``main.run_news``
has its own lock, so concurrent callers cannot double-send.
"""

from __future__ import annotations

import logging
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
    """Run a news job when called by an external trigger and a slot is due."""
    owned = db is None
    db = db or open_database()
    try:
        store = NewsStore(db)
        if config.NEWS_TEST_MODE:
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


def status() -> dict:
    db = open_database()
    store = NewsStore(db)
    if config.NEWS_TEST_MODE:
        previous = parse_iso(store.get_state(TEST_RUN_KEY, ""))
        next_test = (
            previous + timedelta(seconds=config.NEWS_TEST_INTERVAL_SECONDS)
            if previous is not None
            else utc_now()
        )
        last_run = previous
        next_run = next_test
    else:
        last_run = _last_served(store)
        next_run = next_run_at()
    try:
        db.connect().close()
    except Exception:
        log.exception("failed to close scheduler status database connection")
    return {
        "mode": "test" if config.NEWS_TEST_MODE else "scheduled",
        "last_run": iso(last_run) if last_run else None,
        "next_run": next_run.astimezone(config.TEHRAN_TZ).isoformat(timespec="minutes"),
    }
