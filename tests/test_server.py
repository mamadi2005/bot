"""Webhook server tests: auth, onboarding flow and the news trigger."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import channels_config

TOKEN = "123456:TEST-TOKEN-FOR-TESTS"
WEBHOOK_SECRET = "hook-secret"
TRIGGER_TOKEN = "trigger-secret"


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tempdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        os.environ["DB_PATH"] = str(Path(cls._tempdir.name) / "server.db")
        os.environ["TELEGRAM_BOT_TOKEN"] = TOKEN
        os.environ["WEBHOOK_SECRET"] = WEBHOOK_SECRET
        os.environ["TRIGGER_TOKEN"] = TRIGGER_TOKEN
        os.environ["ADMIN_CHAT_ID"] = ""
        os.environ["TELEGRAM_CHAT_IDS"] = ""
        os.environ["GEMINI_API_KEY"] = ""

        import importlib

        import config
        import server

        importlib.reload(config)
        importlib.reload(server)
        cls.server = server
        cls.client = server.app.test_client()

    @classmethod
    def tearDownClass(cls) -> None:
        try:
            cls.server.db().connect().close()
        except Exception:
            pass
        import shutil

        shutil.rmtree(cls._tempdir.name, ignore_errors=True)
        cls._tempdir.cleanup()

    def setUp(self) -> None:
        # Each test starts from a clean user/channel set.
        self.server._state["news_running"] = False
        db = self.server.db()
        db.execute("DELETE FROM users")
        db.execute("DELETE FROM channels")
        db.execute("DELETE FROM news_messages")
        db.execute("DELETE FROM sent_items")
        db.execute("DELETE FROM settings")
        self.sent: list[dict] = []
        self.deleted: list[tuple] = []

        class FakeSender:
            def __init__(inner, token=None):
                pass

            def get_chat_member(inner, chat_id, channel):
                return {"status": "member"}

            def send_message(inner, chat_id, text, reply_markup=None, on_sent=None, warning=""):
                self.sent.append({"chat_id": chat_id, "text": text, "markup": reply_markup})
                return True

            def answer_callback_query(inner, callback_id, text=""):
                return True

            def delete_message(inner, chat_id, message_id):
                self.deleted.append((chat_id, message_id))
                return True

        self._patches = [
            patch.object(self.server, "TelegramSender", FakeSender),
            patch.object(self.server.main, "TelegramSender", FakeSender),
        ]
        for patcher in self._patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    # -- helpers ------------------------------------------------------- #
    def _message(
        self, text: str, chat_id: int = 900, update_id: int = 1, language_code: str | None = None
    ) -> dict:
        sender = {"id": chat_id, "first_name": "Ali", "username": "ali"}
        if language_code is not None:
            sender["language_code"] = language_code
        return {
            "update_id": update_id,
            "message": {
                "message_id": update_id,
                "date": 0,
                "chat": {"id": chat_id, "type": "private"},
                "from": sender,
                "text": text,
            },
        }

    def _callback(
        self, data: str, chat_id: int = 900, update_id: int = 2, language_code: str | None = None
    ) -> dict:
        sender = {"id": chat_id, "first_name": "Ali"}
        if language_code is not None:
            sender["language_code"] = language_code
        return {
            "update_id": update_id,
            "callback_query": {
                "id": f"cb-{update_id}",
                "from": sender,
                "data": data,
                "message": {"message_id": 7, "chat": {"id": chat_id, "type": "private"}},
            },
        }

    def _post_update(self, payload: dict):
        return self.client.post(f"/webhook/{WEBHOOK_SECRET}", json=payload)

    # -- health -------------------------------------------------------- #
    def test_health_reports_database_and_config(self):
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertTrue(body["ok"])
        self.assertTrue(body["webhook_configured"])
        self.assertTrue(body["trigger_configured"])

    # -- webhook auth -------------------------------------------------- #
    def test_webhook_rejects_missing_or_wrong_secret(self):
        self.assertEqual(self.client.post("/webhook", json=self._message("/start")).status_code, 403)
        self.assertEqual(
            self.client.post("/webhook/wrong", json=self._message("/start")).status_code, 403
        )
        self.assertEqual(self.sent, [])

    def test_webhook_rejects_malformed_payloads(self):
        self.assertEqual(self._post_update({}).status_code, 400)
        self.assertEqual(self._post_update({"update_id": 0}).status_code, 400)
        self.assertEqual(
            self.client.post(f"/webhook/{WEBHOOK_SECRET}", data="not json").status_code, 400
        )

    # -- onboarding ---------------------------------------------------- #
    def test_new_user_start_gets_the_english_welcome_and_is_subscribed(self):
        self.assertEqual(self._post_update(self._message("/start")).status_code, 200)
        self.assertEqual(len(self.sent), 1)
        text = self.sent[0]["text"]
        self.assertIn("Welcome to the news bot.", text)
        self.assertIn("registered successfully", text)
        self.assertIn("News subscription: active", text)
        self.assertIn("English", text)
        self.assertEqual(
            self.sent[0]["markup"]["keyboard"][0][0]["text"], "/start"
        )
        rows = self.server.db().query("SELECT chat_id, is_subscribed FROM users")
        self.assertEqual(rows, [{"chat_id": "900", "is_subscribed": 1}])
        self.assertEqual(self.server.db().query("SELECT language FROM users")[0]["language"], "en")

    def test_returning_user_start_shows_settings_with_start_keyboard(self):
        self._post_update(self._message("/start", update_id=1))
        self._post_update(self._message("/start", update_id=2))
        text = self.sent[-1]["text"]
        self.assertIn("Your settings", text)
        self.assertIn("active ✅", text)
        keyboard = self.sent[-1]["markup"]
        self.assertTrue(keyboard["is_persistent"])
        self.assertEqual(keyboard["keyboard"][0][0]["text"], "/start")
        self.assertEqual(keyboard["keyboard"][0][1]["text"], "/help")

    def test_unsubscribe_and_subscribe_commands(self):
        self._post_update(self._message("/start", update_id=1))
        self._post_update(self._message("/unsubscribe", update_id=2))
        self.assertIn("off", self.sent[-1]["text"])
        self.assertEqual(
            self.server.db().query("SELECT is_subscribed FROM users")[0]["is_subscribed"], 0
        )
        self._post_update(self._message("/subscribe", update_id=3))
        self.assertIn("active", self.sent[-1]["text"])
        self.assertEqual(
            self.server.db().query("SELECT is_subscribed FROM users")[0]["is_subscribed"], 1
        )

    def test_start_reactivates_a_returning_user(self):
        self._post_update(self._message("/start", update_id=1))
        self.server.db().execute("UPDATE users SET is_subscribed = 0 WHERE chat_id = '900'")
        self._post_update(self._message("/start", update_id=2))
        self.assertEqual(
            self.server.db().query("SELECT is_subscribed FROM users")[0]["is_subscribed"], 1
        )
        self.assertIn("active ✅", self.sent[-1]["text"])

    def test_subscribe_without_registration_auto_registers(self):
        self._post_update(self._message("/subscribe"))
        self.assertIn("active", self.sent[-1]["text"])
        self.assertEqual(self.server.db().query("SELECT COUNT(*) AS n FROM users")[0]["n"], 1)

    def test_button_without_registration_auto_registers(self):
        self._post_update(self._callback("help", update_id=1))
        self.assertIn("Bot help", self.sent[-1]["text"])
        self.assertEqual(self.server.db().query("SELECT COUNT(*) AS n FROM users")[0]["n"], 1)

    def test_plain_text_nudges_registered_users_only(self):
        self._post_update(self._message("hello", chat_id=901, update_id=1))
        self.assertEqual(self.sent, [])
        self._post_update(self._message("/start", chat_id=901, update_id=2))
        self._post_update(self._message("hello", chat_id=901, update_id=3))
        self.assertIn("/start", self.sent[-1]["text"])

    def test_help_command_and_callback(self):
        self._post_update(self._message("/start", update_id=1))
        self._post_update(self._message("/help", update_id=2))
        self.assertIn("Bot help", self.sent[-1]["text"])
        self._post_update(self._callback("help", update_id=3))
        self.assertIn("Bot help", self.sent[-1]["text"])

    def test_toggle_callback_flips_subscription(self):
        self._post_update(self._message("/start", update_id=1))
        self._post_update(self._callback("toggle_subscription", update_id=2))
        self.assertEqual(
            self.server.db().query("SELECT is_subscribed FROM users")[0]["is_subscribed"], 0
        )
        self._post_update(self._callback("toggle_subscription", update_id=3))
        self.assertEqual(
            self.server.db().query("SELECT is_subscribed FROM users")[0]["is_subscribed"], 1
        )

    def test_start_always_uses_english(self):
        self._post_update(self._message("/start", language_code="en-US"))
        self.assertIn("Welcome to the news bot", self.sent[-1]["text"])
        row = self.server.db().query("SELECT language FROM users")[0]
        self.assertEqual(row["language"], "en")

    def test_non_english_telegram_locale_is_ignored(self):
        self._post_update(self._message("/start", language_code="fa-IR"))
        self.assertIn("Welcome to the news bot", self.sent[-1]["text"])
        row = self.server.db().query("SELECT language FROM users")[0]
        self.assertEqual(row["language"], "en")

    def test_channel_gating_join_message_and_confirm_callback(self):
        state = {"member": False}
        sent = self.sent

        class GateSender:
            def __init__(inner, token=None):
                pass

            def get_chat_member(inner, chat_id, channel):
                return {"status": "member" if state["member"] else "left"}

            def send_message(inner, chat_id, text, reply_markup=None, on_sent=None, warning=""):
                sent.append({"chat_id": chat_id, "text": text, "markup": reply_markup})
                return True

            def answer_callback_query(inner, callback_id, text=""):
                return True

            def delete_message(inner, chat_id, message_id):
                return True

        required = [{"username": "@mynews", "link": "https://t.me/mynews"}]
        with patch.object(self.server, "TelegramSender", GateSender), patch.object(
            self.server.main, "TelegramSender", GateSender
        ), patch.object(channels_config, "REQUIRED_CHANNELS", required):
            self._post_update(self._message("/start", update_id=1))
            self.assertEqual(len(sent), 1)
            self.assertIn("zerobot", sent[0]["text"])
            self.assertIn("https://t.me/mynews", sent[0]["text"])
            keyboard = sent[0]["markup"]["inline_keyboard"]
            self.assertEqual(keyboard[0][0]["url"], "https://t.me/mynews")
            self.assertEqual(keyboard[-1][0]["callback_data"], "check_membership")
            self.assertEqual(keyboard[-1][0]["text"], "✅ I've Joined")
            self.assertEqual(self.server.db().query("SELECT COUNT(*) AS n FROM users")[0]["n"], 0)

            # Clicking confirm while still outside the channel repeats the prompt.
            self._post_update(self._callback("check_membership", update_id=2))
            self.assertEqual(len(sent), 2)
            self.assertIn("zerobot", sent[1]["text"])

            state["member"] = True
            self._post_update(self._callback("check_membership", update_id=3))
            self.assertEqual(len(sent), 3)
            self.assertIn("Welcome to the news bot", sent[2]["text"])
            self.assertEqual(self.server.db().query("SELECT COUNT(*) AS n FROM users")[0]["n"], 1)

    def test_aiceur_zero_is_the_required_channel(self):
        usernames = [entry["username"] for entry in channels_config.REQUIRED_CHANNELS]
        self.assertIn("@aiceur_zero", usernames)

    def test_aiceur_zero_gating_uses_dedicated_callback_and_english_copy(self):
        state = {"member": False}
        sent = self.sent

        class GateSender:
            def __init__(inner, token=None):
                pass

            def get_chat_member(inner, chat_id, channel):
                return {"status": "member" if state["member"] else "left"}

            def send_message(inner, chat_id, text, reply_markup=None, on_sent=None, warning=""):
                sent.append({"chat_id": chat_id, "text": text, "markup": reply_markup})
                return True

            def answer_callback_query(inner, callback_id, text=""):
                return True

            def delete_message(inner, chat_id, message_id):
                return True

        with patch.object(self.server, "TelegramSender", GateSender), patch.object(
            self.server.main, "TelegramSender", GateSender
        ):
            self._post_update(self._message("/start", update_id=1, language_code="en"))
            self.assertEqual(len(sent), 1)
            self.assertIn("To use zerobot", sent[0]["text"])
            self.assertIn("https://t.me/aiceur_zero", sent[0]["text"])
            keyboard = sent[0]["markup"]["inline_keyboard"]
            self.assertEqual(keyboard[0][0]["url"], "https://t.me/aiceur_zero")
            self.assertEqual(keyboard[-1][0]["callback_data"], "check_membership_aiceur_zero")
            self.assertEqual(keyboard[-1][0]["text"], "✅ I've Joined")
            self.assertEqual(self.server.db().query("SELECT COUNT(*) AS n FROM users")[0]["n"], 0)

            self._post_update(
                self._callback("check_membership_aiceur_zero", update_id=2, language_code="en")
            )
            self.assertEqual(len(sent), 2)
            self.assertIn("To use zerobot", sent[1]["text"])

            state["member"] = True
            self._post_update(
                self._callback("check_membership_aiceur_zero", update_id=3, language_code="en")
            )
            self.assertIn("Welcome to the news bot", sent[-1]["text"])
            self.assertEqual(self.server.db().query("SELECT COUNT(*) AS n FROM users")[0]["n"], 1)

    def test_unknown_callback_is_answered_without_a_message(self):
        self._post_update(self._message("/start", update_id=1))
        before = len(self.sent)
        self._post_update(self._callback("something_else", update_id=2))
        self.assertEqual(len(self.sent), before)

    def test_non_private_chats_are_ignored(self):
        payload = self._message("/start")
        payload["message"]["chat"]["type"] = "group"
        self._post_update(payload)
        self.assertEqual(self.sent, [])

    def test_admin_channel_and_stats_commands(self):
        with patch.dict(os.environ, {"ADMIN_CHAT_ID": "42"}):
            import importlib

            import config

            importlib.reload(config)
            importlib.reload(self.server)
            self.client = self.server.app.test_client()

            class FakeSender:
                def __init__(inner, token=None):
                    pass

                def get_chat_member(inner, chat_id, channel):
                    return {"status": "member"}

                def send_message(inner, chat_id, text, reply_markup=None, on_sent=None, warning=""):
                    self.sent.append({"chat_id": chat_id, "text": text, "markup": reply_markup})
                    return True

                def answer_callback_query(inner, callback_id, text=""):
                    return True

                def delete_message(inner, chat_id, message_id):
                    return True

            # The reload re-imported TelegramSender; patch it again.
            self.server.TelegramSender = FakeSender
            self.server.main.TelegramSender = FakeSender

            self._post_update(self._message("/addchannel @ExampleCh", chat_id=7, update_id=1))
            self.assertEqual(self.server.db().query("SELECT COUNT(*) AS n FROM channels")[0]["n"], 0)

            self._post_update(self._message("/addchannel @ExampleCh", chat_id=42, update_id=2))
            self.assertEqual(
                self.server.db().query("SELECT username FROM channels")[0]["username"], "@examplech"
            )

            self._post_update(self._message("/stats", chat_id=42, update_id=3))
            self.assertIn("Bot statistics", self.sent[-1]["text"])

            self._post_update(self._message("/removechannel examplech", chat_id=42, update_id=4))
            self.assertEqual(self.server.db().query("SELECT COUNT(*) AS n FROM channels")[0]["n"], 0)

    # -- trigger ------------------------------------------------------- #
    def test_trigger_requires_a_token(self):
        self.assertEqual(self.client.post("/trigger_news", json={}).status_code, 401)
        self.assertEqual(
            self.client.post(
                "/trigger_news", json={}, headers={"Authorization": "Bearer wrong"}
            ).status_code,
            401,
        )

    def test_trigger_accepts_bearer_and_x_token(self):
        with patch.object(
            self.server.main, "run_news", return_value={"ok": True, "messages": 0}
        ) as run, patch.object(
            self.server.scheduler, "run_due", return_value={"ok": True, "messages": 0}
        ), patch.object(self.server.threading, "Thread") as thread:
            response = self.client.post(
                "/trigger_news?wait=1",
                json={"cleanup_only": True},
                headers={"Authorization": f"Bearer {TRIGGER_TOKEN}"},
            )
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.get_json()["ok"])
            self.assertTrue(run.called)

            response = self.client.post(
                "/trigger_news", json={}, headers={"X-Trigger-Token": TRIGGER_TOKEN}
            )
            self.assertEqual(response.status_code, 202)
            self.assertTrue(response.get_json()["accepted"])
            thread.assert_called_once()

    def test_trigger_accepts_true_query_parameter_for_dry_run(self):
        with patch.object(
            self.server.main,
            "run_news",
            return_value={"ok": True, "dry_run": True, "approved": 1},
        ) as run, patch.object(self.server.scheduler, "run_due") as run_due:
            response = self.client.post(
                "/trigger_news?dry_run=true&wait=true",
                headers={"Authorization": f"Bearer {TRIGGER_TOKEN}"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["dry_run"])
        run.assert_called_once_with(dry_run=True, cleanup_only=False, limit=0)
        run_due.assert_not_called()

    def test_string_false_does_not_enable_dry_run(self):
        with patch.object(
            self.server.scheduler,
            "run_due",
            return_value={"ok": True, "skipped": True},
        ) as run_due, patch.object(self.server.main, "run_news") as run:
            response = self.client.post(
                "/trigger_news?wait=true",
                json={"dry_run": "false"},
                headers={"Authorization": f"Bearer {TRIGGER_TOKEN}"},
            )
        self.assertEqual(response.status_code, 200)
        run.assert_not_called()
        run_due.assert_called_once_with(limit=0)

    def test_trigger_reports_failures_with_500(self):
        with patch.object(
            self.server.main, "run_news", return_value={"ok": False, "error": "boom"}
        ), patch.object(
            self.server.scheduler, "run_due", return_value={"ok": False, "error": "boom"}
        ):
            response = self.client.post(
                "/trigger_news?wait=1", headers={"Authorization": f"Bearer {TRIGGER_TOKEN}"}
            )
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.get_json()["error"], "boom")

    def test_news_status_requires_auth_and_reports(self):
        self.assertEqual(self.client.get("/news_status").status_code, 401)
        with patch.object(
            self.server.scheduler, "run_due", return_value={"ok": True}
        ):
            self.client.post(
                "/trigger_news?wait=1", headers={"Authorization": f"Bearer {TRIGGER_TOKEN}"}
            )
        status = self.client.get(
            "/news_status", headers={"Authorization": f"Bearer {TRIGGER_TOKEN}"}
        ).get_json()
        self.assertTrue(status["ok"])
        self.assertFalse(status["running"])

    def test_unknown_route_returns_json_404(self):
        response = self.client.get("/nope")
        self.assertEqual(response.status_code, 404)
        self.assertFalse(response.get_json()["ok"])


if __name__ == "__main__":
    unittest.main()
