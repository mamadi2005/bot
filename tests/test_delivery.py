"""Delivery, expiry and pipeline behaviour on the hybrid (SQLite) architecture."""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import config
import main
import storage
from fetcher import Item
from telegram_sender import TelegramSender


class Response:
    def __init__(self, status, data):
        self.status_code = status
        self.data = data

    def json(self):
        return self.data


class FakeTelegram(TelegramSender):
    """Replays scripted responses; records every call."""

    def __init__(self):
        super().__init__("test-token")
        self.requests = []
        self.responses = []
        self.counter = 0

    def _pace(self, chat_id):
        pass

    def _post(self, method, payload, timeout=30):
        self.requests.append((method, payload.copy()))
        if method == "getChatMember":
            return Response(200, {"ok": True, "result": {"status": "member"}})
        if self.responses:
            return self.responses.pop(0)
        if method == "sendMessage":
            self.counter += 1
            return Response(
                200,
                {"ok": True, "result": {"message_id": self.counter, "chat": {"id": payload["chat_id"]}}},
            )
        return Response(200, {"ok": True, "result": True})

    def sends(self):
        return [payload for method, payload in self.requests if method == "sendMessage"]

    def deletes(self):
        return [payload for method, payload in self.requests if method == "deleteMessage"]


def make_item(**overrides) -> Item:
    data = dict(
        title="تیتر خبر امنیتی",
        url="https://example.test/news",
        source_type="rss",
        source_label="source",
        category="security",
        hash="hash-1",
    )
    data.update(overrides)
    return Item(**data)


class HybridDeliveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.db_path = Path(self.tempdir.name) / "bot.db"
        self.db = storage.open_database(str(self.db_path))
        self.users, self.channels, self.news = storage.stores(self.db)
        self.sender = FakeTelegram()
        self.now = storage.utc_now()

    def tearDown(self) -> None:
        try:
            self.db.connect().close()
        except Exception:
            pass

    def _item_with_fa_summary(self, text: str | None = None) -> Item:
        item = make_item()
        item.summaries = {
            "fa": text
            or "<b>این یک خلاصه فارسی درباره آسیب‌پذیری XSS در پنل مدیریت است که بسیار مهم است.</b>"
        }
        item.summary_html = item.summaries["fa"]
        return item

    # -- delivery ------------------------------------------------------ #
    def test_delivers_only_to_subscribed_users(self):
        for chat_id in (1, 2, 3):
            self.users.ensure(chat_id, "U", "u")
        self.users.set_subscribed(2, False)
        with patch.object(config, "BROADCAST_DELAY", 0):
            main.deliver_news(
                [self._item_with_fa_summary()],
                self.news,
                self.sender,
                self.users,
                self.channels,
                self.now,
                {"messages": 0, "recipients": 0},
            )
        targets = sorted(payload["chat_id"] for payload in self.sender.sends())
        self.assertEqual(targets, [1, 3])

    def test_each_message_carries_the_24h_notice_and_html_mode(self):
        self.users.ensure(1, "U", "u")
        with patch.object(config, "BROADCAST_DELAY", 0):
            main.deliver_news(
                [self._item_with_fa_summary()],
                self.news,
                self.sender,
                self.users,
                self.channels,
                self.now,
                {"messages": 0, "recipients": 0},
            )
        payload = self.sender.sends()[0]
        self.assertEqual(payload["parse_mode"], "HTML")
        self.assertIn("۲۴ ساعت", payload["text"])

    def test_delivery_records_expiry_rows(self):
        self.users.ensure(1, "U", "u")
        self.users.ensure(2, "U", "u")
        with patch.object(config, "BROADCAST_DELAY", 0):
            main.deliver_news(
                [self._item_with_fa_summary()],
                self.news,
                self.sender,
                self.users,
                self.channels,
                self.now,
                {"messages": 0, "recipients": 0},
            )
        rows = self.db.query("SELECT chat_id, message_id FROM news_messages ORDER BY chat_id")
        self.assertEqual(len(rows), 2)
        self.assertEqual({row["chat_id"] for row in rows}, {"1", "2"})

    def test_delivery_marks_the_item_as_sent(self):
        self.users.ensure(1, "U", "u")
        with patch.object(config, "BROADCAST_DELAY", 0):
            main.deliver_news(
                [self._item_with_fa_summary()],
                self.news,
                self.sender,
                self.users,
                self.channels,
                self.now,
                {"messages": 0, "recipients": 0},
            )
        self.assertIn("hash-1", self.news.sent_hashes)

    def test_blocked_user_is_unsubscribed_and_skipped(self):
        self.users.ensure(1, "U", "u")
        self.users.ensure(2, "U", "u")
        self.sender.responses = [
            Response(403, {"ok": False, "description": "Forbidden: bot was blocked by the user"}),
            Response(200, {"ok": True, "result": {"message_id": 9, "chat": {"id": 2}}}),
        ]
        with patch.object(config, "BROADCAST_DELAY", 0):
            main.deliver_news(
                [self._item_with_fa_summary()],
                self.news,
                self.sender,
                self.users,
                self.channels,
                self.now,
                {"messages": 0, "recipients": 0},
            )
        self.assertFalse(self.users.is_subscribed(1))
        self.assertTrue(self.users.is_subscribed(2))

    def test_membership_gate_skips_non_members(self):
        self.users.ensure(1, "U", "u")
        self.db.execute("INSERT INTO channels (username) VALUES ('@required')")
        self.channels.load()

        class GateSender(FakeTelegram):
            def get_chat_member(self, chat_id, channel):
                return {"status": "left"}

        gate = GateSender()
        with patch.object(config, "BROADCAST_DELAY", 0):
            main.deliver_news(
                [self._item_with_fa_summary()],
                self.news,
                gate,
                self.users,
                self.channels,
                self.now,
                {"messages": 0, "recipients": 0},
            )
        self.assertEqual(gate.sends(), [])

    def test_each_user_gets_the_summary_in_their_own_language(self):
        self.users.ensure(1, "U", "u", language="fa")
        self.users.ensure(2, "U", "u", language="en")
        item = make_item()
        item.summaries = {
            "fa": "<b>خلاصه فارسی درباره آسیب‌پذیری XSS در پنل مدیریت است.</b>",
            "en": "<b>English summary about an XSS vulnerability in the admin panel.</b>",
        }
        item.summary_html = item.summaries["fa"]
        with patch.object(config, "BROADCAST_DELAY", 0):
            main.deliver_news(
                [item],
                self.news,
                self.sender,
                self.users,
                self.channels,
                self.now,
                {"messages": 0, "recipients": 0},
            )
        by_chat = {payload["chat_id"]: payload["text"] for payload in self.sender.sends()}
        self.assertIn("خلاصه فارسی", by_chat[1])
        self.assertIn("۲۴ ساعت", by_chat[1])
        self.assertIn("English summary", by_chat[2])
        self.assertIn("24 hours", by_chat[2])

    def test_all_supported_summary_languages_put_the_default_first(self):
        self.assertEqual(main._summary_langs(), ["fa", "en"])

    def test_language_change_during_analysis_uses_the_new_language_summary(self):
        self.users.ensure(1, "U", "u", language="fa")
        langs = main._summary_langs()
        item = make_item()
        item.summaries = {
            "fa": "<b>خلاصه فارسی خبر امنیتی است.</b>",
            "en": "<b>English security news summary.</b>",
        }
        item.summary_html = item.summaries["fa"]
        self.users.set_language(1, "en")

        with patch.object(config, "BROADCAST_DELAY", 0):
            main.deliver_news(
                [item],
                self.news,
                self.sender,
                self.users,
                self.channels,
                self.now,
                {"messages": 0, "recipients": 0},
            )

        self.assertEqual(langs, ["fa", "en"])
        self.assertIn("English security news summary", self.sender.sends()[0]["text"])

    def test_analysis_rejects_summaries_in_the_wrong_language(self):
        item = make_item(content="Technical details about the vulnerability. " * 4)

        class StubAnalyzer:
            key_count = 1

            def triage(self, candidates):
                return candidates

            def summarize(self, item, lang):
                return SimpleNamespace(
                    status="ok",
                    text="این یک خلاصه فارسی درباره آسیب‌پذیری و روش بررسی آن است.",
                )

        with patch.object(main, "Analyzer", return_value=StubAnalyzer()):
            approved, quota_exhausted = main._analyze(
                [item],
                no_ai=False,
                budget=main.AIBudget(per_run=4, daily=4, used_today=0),
                stats={},
                langs=["fa", "en"],
            )

        self.assertEqual(approved, [item])
        self.assertFalse(quota_exhausted)
        self.assertIn("خلاصه فارسی", item.summaries["fa"])
        self.assertNotIn("خلاصه فارسی", item.summaries["en"])
        self.assertIn("Automatic summary", item.summaries["en"])

    def test_no_recipients_logs_and_sends_nothing(self):
        with patch.object(config, "BROADCAST_DELAY", 0):
            main.deliver_news(
                [self._item_with_fa_summary()],
                self.news,
                self.sender,
                self.users,
                self.channels,
                self.now,
                {"messages": 0, "recipients": 0},
            )
        self.assertEqual(self.sender.sends(), [])

    # -- expiry -------------------------------------------------------- #
    def test_cleanup_deletes_only_expired_messages(self):
        self.news.record_news_message(1, 10, storage.utc_now() - timedelta(hours=25))
        self.news.record_news_message(2, 20, storage.utc_now())
        stats = main.cleanup_expired(self.news, self.sender)
        self.assertEqual(stats["deleted"], 1)
        self.assertEqual(self.sender.deletes()[0]["message_id"], 10)
        remaining = self.db.query("SELECT message_id FROM news_messages")
        self.assertEqual([row["message_id"] for row in remaining], [20])

    def test_cleanup_keeps_rows_telegram_refuses_to_delete(self):
        self.news.record_news_message(1, 10, storage.utc_now() - timedelta(hours=25))
        self.sender.responses = [Response(400, {"ok": False, "description": "Bad Request"})]
        stats = main.cleanup_expired(self.news, self.sender)
        self.assertEqual(stats["failed"], 1)
        self.assertEqual(stats["deleted"], 0)
        self.assertEqual(len(self.db.query("SELECT 1 FROM news_messages")), 1)

    def test_already_deleted_messages_are_removed_from_tracking(self):
        self.news.record_news_message(1, 10, storage.utc_now() - timedelta(hours=25))
        self.sender.responses = [
            Response(400, {"ok": False, "description": "Bad Request: message to delete not found"})
        ]
        stats = main.cleanup_expired(self.news, self.sender)
        self.assertEqual(stats["deleted"], 1)
        self.assertEqual(len(self.db.query("SELECT 1 FROM news_messages")), 0)

    def test_settings_messages_are_never_queued_for_deletion(self):
        from telegram_sender import _handle_message

        self._post_chat = None
        sent = []

        class PlainSender:
            def send_message(inner, chat_id, text, reply_markup=None, on_sent=None, warning=""):
                sent.append({"text": text, "on_sent": on_sent})
                return True

            def answer_callback_query(inner, *args, **kwargs):
                return True

        self.channels.channels = []
        message = {
            "chat": {"id": 1, "type": "private"},
            "from": {"id": 1, "first_name": "U", "language_code": "fa"},
            "text": "/start",
        }
        _handle_message(message, PlainSender(), self.users, self.channels)
        self.assertTrue(sent)
        self.assertTrue(all(entry["on_sent"] is None for entry in sent))
        self.assertEqual(self.db.query("SELECT COUNT(*) AS n FROM news_messages")[0]["n"], 0)

    def test_cleanup_batches_are_limited(self):
        for index in range(7):
            self.news.record_news_message(1, 100 + index, storage.utc_now() - timedelta(hours=25))
        stats = main.cleanup_expired(self.news, self.sender, limit=3)
        self.assertEqual(stats["deleted"], 3)

    # -- persian enforcement ------------------------------------------- #
    def test_persian_detection_rejects_english_summaries(self):
        english = "This is a purely English summary about an XSS vulnerability in the admin panel."
        persian = "این یک خلاصهٔ فارسی دربارهٔ آسیب‌پذیری XSS در پنل مدیریت است و بسیار مهم است."
        self.assertFalse(main.looks_persian(english))
        self.assertTrue(main.looks_persian(persian))
        self.assertFalse(main.looks_persian("<code>XSS</code> RCE SQLi"))
        self.assertTrue(main.looks_english(english))
        self.assertFalse(main.looks_english(persian))
        self.assertFalse(main.looks_english("<code>XSS</code> RCE SQLi"))

    def test_non_persian_summary_falls_back_when_delivered(self):
        english = "<b>This is a purely English summary about an XSS vulnerability in the panel.</b>"
        item = make_item()
        item.summaries = {"fa": english}
        item.summary_html = english
        self.users.ensure(1, "U", "u")
        with patch.object(config, "BROADCAST_DELAY", 0):
            main.deliver_news(
                [item], self.news, self.sender, self.users, self.channels, self.now,
                {"messages": 0, "recipients": 0},
            )
        self.assertTrue(self.sender.sends())

    def test_normalize_persian_folds_arabic_variants(self):
        self.assertEqual(main.normalize_persian("يك"), "یک")
        self.assertEqual(main.normalize_persian("كتاب"), "کتاب")

    def test_persian_share_ignores_urls_and_tags(self):
        text = '<b>خبر بسیار مهمی است که باید خوانده شود</b> <a href="https://x.test/en-only-page">لینک</a>'
        self.assertGreaterEqual(main.persian_share(text), 0.5)


if __name__ == "__main__":
    unittest.main()
