"""Scheduler slot maths and the "run at most once per slot" guarantee."""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import config
import scheduler
import storage


def tehran(hour: int, minute: int = 0, day: int = 23) -> datetime:
    return datetime(2026, 11, day, hour, minute, tzinfo=config.TEHRAN_TZ)


class SlotMathTests(unittest.TestCase):
    def test_slots_are_the_five_configured_hours(self):
        self.assertEqual(scheduler.RUN_HOURS, (0, 8, 12, 16, 20))

    def test_next_run_rolls_to_the_next_slot(self):
        self.assertEqual(scheduler.next_run_at(tehran(7, 30)), tehran(8))
        self.assertEqual(scheduler.next_run_at(tehran(18, 5)), tehran(20))
        self.assertEqual(scheduler.next_run_at(tehran(20, 1)), tehran(0, day=24))

    def test_latest_slot_looks_backwards_across_midnight(self):
        self.assertEqual(scheduler._slot_at_or_before(tehran(0, 5)), tehran(0))
        self.assertEqual(scheduler._slot_at_or_before(tehran(9, 59)), tehran(8))
        self.assertEqual(scheduler._slot_at_or_before(tehran(0, 5, day=24)), tehran(0, day=24))


class DueTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.mkdtemp(prefix="sched-")
        self.db = storage.open_database(str(Path(self.dir) / "s.db"))
        self.news = storage.NewsStore(self.db)

    def tearDown(self) -> None:
        try:
            self.db.connect().close()
        except Exception:
            pass

    def test_first_run_is_due(self):
        self.assertIsNotNone(scheduler.due_slot(self.news, storage.utc_now()))

    def test_served_slot_is_not_due_again(self):
        now = storage.utc_now()
        slot = scheduler._slot_at_or_before(now)
        scheduler._mark_served(self.news, slot)
        self.assertIsNone(scheduler.due_slot(self.news, now))

    def test_next_slot_becomes_due_after_the_hour_passes(self):
        now = storage.utc_now()
        scheduler._mark_served(self.news, scheduler._slot_at_or_before(now))
        later = scheduler.next_run_at(now) + timedelta(minutes=1)
        self.assertIsNotNone(scheduler.due_slot(self.news, later))

    def test_failed_slot_is_retried_then_abandoned(self):
        now = storage.utc_now()
        slot = scheduler._slot_at_or_before(now)
        with patch.object(scheduler, "main") as fake_main:
            fake_main.run_news.return_value = {"ok": False, "error": "boom"}
            for _ in range(scheduler.MAX_SLOT_ATTEMPTS):
                self.assertIsNotNone(scheduler.due_slot(self.news, now))
                scheduler.run_due(db=self.db)
            self.assertIsNone(scheduler.due_slot(self.news, now))

    def test_successful_run_marks_the_slot_and_skips_the_next_call(self):
        with patch.object(scheduler, "main") as fake_main:
            fake_main.run_news.return_value = {"ok": True, "approved": 1, "messages": 1}
            first = scheduler.run_due(db=self.db)
            second = scheduler.run_due(db=self.db)
        self.assertFalse(first.get("skipped"))
        self.assertTrue(second.get("skipped"))

    def test_status_reports_the_next_slot(self):
        with patch.object(scheduler, "open_database", return_value=self.db):
            payload = scheduler.status()
        self.assertIn("next_run", payload)


def _hours(count: int, base: datetime):
    return timedelta(hours=count)


if __name__ == "__main__":
    unittest.main()
