"""Storage tests: users, channels, dedupe, expiry queue, rate limiting."""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

import storage


class StorageTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.mkdtemp(prefix="bot-storage-")
        self.path = Path(self.dir) / "bot.db"
        self.db = storage.open_database(str(self.path))
        self.users, self.channels, self.news = storage.stores(self.db)

    def tearDown(self) -> None:
        try:
            self.db.connect().close()
        except Exception:
            pass
        for suffix in ("", "-wal", "-shm"):
            target = Path(str(self.path) + suffix)
            if target.exists():
                try:
                    os.remove(target)
                except OSError:
                    pass
        try:
            os.rmdir(self.dir)
        except OSError:
            pass

    # -- schema -------------------------------------------------------- #
    def test_unwritable_dir_falls_back_to_temp(self):
        target = storage.Database._resolve_path(os.path.join("Z:\\missing-drive", "bot.db"))
        self.assertTrue(Path(tempfile.gettempdir()).resolve() in target.resolve().parents)
        self.assertEqual(target.name, "bot.db")

    def test_schema_is_created_idempotently(self):
        tables = {
            row["name"]
            for row in self.db.query("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        for expected in ("users", "channels", "news_messages", "sent_items", "settings"):
            self.assertIn(expected, tables)
        again = storage.open_database(str(self.path))
        self.assertEqual(storage.stores(again)[0].count(), 0)

    # -- users --------------------------------------------------------- #
    def test_ensure_registers_then_updates_last_seen(self):
        user, created = self.users.ensure(1, "Ali", "@ali")
        self.assertTrue(created)
        self.assertEqual(user["chat_id"], "1")
        self.assertEqual(user["first_name"], "Ali")
        self.assertEqual(user["username"], "ali")
        self.assertTrue(self.users.is_subscribed(1))

        again, created_again = self.users.ensure(1, "Ali Reza", "ali2")
        self.assertFalse(created_again)
        self.assertEqual(again["first_name"], "Ali Reza")
        self.assertEqual(again["username"], "ali2")
        self.assertEqual(self.users.count(), 1)

    def test_subscription_toggle_filters_broadcast_list(self):
        for chat_id in (1, 2, 3):
            self.users.ensure(chat_id, "U", "u")
        self.users.set_subscribed(2, False)
        self.assertEqual(self.users.chat_ids(), [1, 3])
        self.assertEqual(self.users.chat_ids(subscribed_only=False), [1, 2, 3])
        self.assertFalse(self.users.is_subscribed(2))

    def test_language_helpers_fall_back_to_persian(self):
        self.users.ensure(9, "U", "u")
        self.assertEqual(self.users.lang(9), "fa")
        self.assertTrue(self.users.set_lang(9, "en"))
        self.assertEqual(self.users.lang(9), "en")
        self.assertFalse(self.users.set_lang(9, "de"))
        self.assertEqual(self.users.lang(9), "en")

    def test_language_is_stored_on_registration(self):
        user, created = self.users.ensure(10, "U", "u", language="en")
        self.assertTrue(created)
        self.assertEqual(user["language"], "en")

        other, _ = self.users.ensure(11, "U", "u", language="de")
        self.assertEqual(other["language"], "fa")

        self.assertTrue(self.users.set_language(11, "en"))
        self.assertEqual(self.users.lang(11), "en")
        self.assertFalse(self.users.set_language(11, "klingon"))

    def test_stats_counts_active_and_subscribed(self):
        self.users.ensure(1, "A", "a")
        self.users.ensure(2, "B", "b")
        self.users.set_subscribed(2, False)
        old = storage.iso(storage.utc_now() - timedelta(days=90))
        self.db.execute("UPDATE users SET last_seen = ? WHERE chat_id = '1'", (old,))
        stats = self.users.stats()
        self.assertEqual(stats["total"], 2)
        self.assertEqual(stats["subscribed"], 1)
        self.assertEqual(stats["active"], 1)

    def test_unknown_user_operations_are_safe(self):
        self.assertIsNone(self.users.get(404))
        self.assertFalse(self.users.set_lang(404, "fa"))
        self.assertFalse(self.users.set_subscribed(404, True))
        self.assertFalse(self.users.rate_limited(404, storage.utc_now()))

    # -- rate limiting ------------------------------------------------- #
    def test_fourth_message_in_a_window_mutes_the_user(self):
        self.users.ensure(5, "U", "u")
        now = storage.utc_now()
        self.assertEqual([self.users.rate_limited(5, now) for _ in range(4)], [False, False, False, True])
        row = self.users.get(5)
        self.assertIsNotNone(row["muted_until"])
        # Still muted immediately afterwards.
        self.assertTrue(self.users.rate_limited(5, now + timedelta(seconds=1)))
        # Released after the mute window, with a fresh counter.
        later = now + timedelta(seconds=400)
        self.assertFalse(self.users.rate_limited(5, later))

    def test_window_resets_after_the_configured_interval(self):
        self.users.ensure(6, "U", "u")
        now = storage.utc_now()
        for _ in range(3):
            self.users.rate_limited(6, now)
        self.assertFalse(self.users.rate_limited(6, now + timedelta(seconds=120)))

    # -- channels ------------------------------------------------------ #
    def test_channels_add_remove_and_case_folding(self):
        added, channel = self.channels.add("@MyChannel")
        self.assertTrue(added)
        self.assertEqual(channel, "@mychannel")
        self.assertEqual(self.channels.channels, ["@mychannel"])

        self.assertFalse(self.channels.add("mychannel")[0])
        self.assertTrue(self.channels.remove("MYCHANNEL")[0])
        self.assertEqual(self.channels.channels, [])
        self.assertFalse(self.channels.remove("mychannel")[0])

    def test_channel_duplicates_and_invalid_values(self):
        self.channels.add("https://evil.test/x")
        self.assertEqual(self.channels.channels, ["@https://evil.test/x"])
        self.assertFalse(self.channels.add("")[0])

    # -- expiry queue -------------------------------------------------- #
    def test_news_messages_are_due_only_after_the_ttl(self):
        self.news.record_news_message(1, 10, storage.utc_now())
        self.assertEqual(self.news.due_news_messages(), [])
        later = storage.utc_now() + timedelta(hours=25)
        due = self.news.due_news_messages(later)
        self.assertEqual(len(due), 1)
        self.assertEqual(due[0]["chat_id"], 1)
        self.assertEqual(due[0]["message_id"], 10)

    def test_news_message_recording_is_idempotent(self):
        self.news.record_news_message(1, 10, storage.utc_now())
        self.news.record_news_message(1, 10, storage.utc_now())
        rows = self.db.query("SELECT COUNT(*) AS n FROM news_messages")
        self.assertEqual(rows[0]["n"], 1)

    def test_remove_news_message_deletes_the_row(self):
        self.news.record_news_message(1, 10, storage.utc_now())
        entry = self.news.news_messages[-1]
        self.news.remove_news_message(entry)
        later = storage.utc_now() + timedelta(hours=25)
        self.assertEqual(self.news.due_news_messages(later), [])

    def test_invalid_chat_ids_are_ignored_when_loading(self):
        self.db.execute(
            "INSERT INTO news_messages (chat_id, message_id, sent_at) VALUES (?, ?, ?)",
            ("not-a-number", 1, storage.iso(storage.utc_now() - timedelta(hours=48))),
        )
        reloaded = storage.NewsStore(self.db)
        reloaded.load()
        self.assertEqual(reloaded.news_messages, [])

    # -- dedupe -------------------------------------------------------- #
    def test_dedupe_rejects_repeated_hashes(self):
        self.assertEqual(self.news.sent_hashes, set())
        self.assertTrue(self.news.log_sent("h1", "https://x.test", "t", "c", storage.iso()))
        self.assertIn("h1", self.news.sent_hashes)
        self.assertFalse(self.news.log_sent("h1", "https://x.test", "t", "c", storage.iso()))
        self.assertEqual(self.db.query("SELECT COUNT(*) AS n FROM sent_items")[0]["n"], 1)

    def test_dedupe_ignores_expired_entries(self):
        old = storage.iso(storage.utc_now() - timedelta(days=90))
        self.db.execute(
            "INSERT INTO sent_items (url_hash, url, title, sent_at) VALUES (?, ?, ?, ?)",
            ("old", "u", "t", old),
        )
        self.assertNotIn("old", self.news.sent_hashes)

    def test_empty_hash_is_never_recorded(self):
        self.assertFalse(self.news.log_sent("", "u", "t", "c", storage.iso()))

    # -- settings ------------------------------------------------------ #
    def test_settings_roundtrip_and_upsert(self):
        self.assertEqual(self.news.get_state("missing", "fallback"), "fallback")
        self.news.set_state("ai_calls_date", "2026-01-01")
        self.news.set_state("ai_calls_date", "2026-01-02")
        self.assertEqual(self.news.get_state("ai_calls_date"), "2026-01-02")


if __name__ == "__main__":
    unittest.main()
