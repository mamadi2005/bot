"""Telegram Bot API: onboarding, HTML sanitizing, splitting, retries."""

from __future__ import annotations

import html
import logging
import re
import threading
import time
from datetime import datetime
from html.parser import HTMLParser
from urllib.parse import urlsplit

import requests

import config

log = logging.getLogger(__name__)

ALLOWED_TAGS = {"b", "i", "u", "s", "a", "code", "pre", "blockquote", "span", "tg-spoiler"}
TAG_ALIASES = {"strong": "b", "em": "i", "del": "s", "strike": "s", "ins": "u"}
BLOCK_TAGS = {
    "p",
    "div",
    "section",
    "article",
    "header",
    "footer",
    "figure",
    "figcaption",
    "main",
    "aside",
    "address",
}
LIST_TAGS = {"ul", "ol", "table", "tr"}
HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
SKIP_CONTENT_TAGS = {
    "script",
    "style",
    "noscript",
    "iframe",
    "object",
    "embed",
    "template",
    "svg",
    "canvas",
}


# --------------------------------------------------------------------------- #
# HTML sanitizer (last line of defence before the Bot API)
# --------------------------------------------------------------------------- #
def _safe_href(value: str) -> str:
    href = (value or "").strip()
    if not href:
        return ""
    try:
        scheme = urlsplit(href).scheme.lower()
    except ValueError:
        return ""
    if scheme in {"http", "https", "mailto", "tg"}:
        return href
    return ""


class _Sanitizer(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.stack: list[str] = []
        self.skip_depth = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        name = (tag or "").lower()
        if name in SKIP_CONTENT_TAGS:
            self.skip_depth += 1
            return
        if self.skip_depth:
            return
        attributes = {key.lower(): (value or "") for key, value in attrs}

        if name in ("br", "hr"):
            self.parts.append("\n")
            return
        if name == "li":
            self.parts.append("\n• ")
            return
        if name in HEADING_TAGS:
            self.parts.append("\n\n")
            return
        if name in BLOCK_TAGS or name in LIST_TAGS:
            self.parts.append("\n")
            return
        if name in ("td", "th"):
            self.parts.append(" ")
            return
        if name == "img":
            alt = attributes.get("alt", "").strip()
            if alt:
                self.parts.append(html.escape(alt, quote=False))
            return

        name = TAG_ALIASES.get(name, name)
        if name not in ALLOWED_TAGS:
            return

        if name == "a":
            href = _safe_href(attributes.get("href", ""))
            if not href:
                return
            raw = f'<a href="{html.escape(href, quote=True)}">'
        elif name == "span":
            classes = attributes.get("class", "")
            raw = (
                '<span class="tg-spoiler">'
                if "spoiler" in classes
                else "<span>"
            )
        else:
            raw = f"<{name}>"

        self.stack.append(name)
        self.parts.append(raw)

    def handle_startendtag(self, tag: str, attrs) -> None:
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        name = (tag or "").lower()
        if name in SKIP_CONTENT_TAGS:
            self.skip_depth = max(0, self.skip_depth - 1)
            return
        if self.skip_depth:
            return
        if name in HEADING_TAGS:
            self.parts.append("\n")
            return
        if name in BLOCK_TAGS or name in LIST_TAGS:
            self.parts.append("\n")
            return
        name = TAG_ALIASES.get(name, name)
        if name not in ALLOWED_TAGS or name not in self.stack:
            return
        while self.stack:
            current = self.stack.pop()
            self.parts.append(f"</{current}>")
            if current == name:
                break

    def handle_data(self, data: str) -> None:
        if data and not self.skip_depth:
            self.parts.append(html.escape(data, quote=False))


def sanitize_html(raw: str) -> str:
    if not raw:
        return ""
    parser = _Sanitizer()
    try:
        parser.feed(raw)
        parser.close()
    except Exception:
        text = re.sub(r"<[^>]{0,800}>", "", raw)
        return re.sub(r"\n{3,}", "\n\n", html.unescape(text)).strip()

    while parser.stack:
        parser.parts.append(f"</{parser.stack.pop()}>")
    text = "".join(parser.parts)
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\u00a0", " ")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def html_to_plain(text: str) -> str:
    stripped = re.sub(r"<[^>]{0,800}>", "", text or "")
    return html.unescape(stripped)


# --------------------------------------------------------------------------- #
# Tag-aware splitting (never cuts inside a tag, closes/reopens tags)
# --------------------------------------------------------------------------- #
class _ChunkTokenizer(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tokens: list[tuple[str, str | None, str]] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        raw = self.get_starttag_text() or f"<{tag}>"
        self.tokens.append(("open", tag, raw))

    def handle_startendtag(self, tag: str, attrs) -> None:
        raw = self.get_starttag_text() or f"<{tag}/>"
        self.tokens.append(("self", tag, raw))

    def handle_endtag(self, tag: str) -> None:
        self.tokens.append(("close", tag, f"</{tag}>"))

    def handle_data(self, data: str) -> None:
        if data:
            self.tokens.append(("text", None, html.escape(data, quote=False)))


def _safe_cut(window: str, cut: int) -> int:
    if cut <= 1:
        return 1
    paragraph = window.rfind("\n\n", 0, cut)
    if paragraph >= cut // 2:
        cut = paragraph + 2
    else:
        newline = window.rfind("\n", 0, cut)
        if newline >= cut // 2:
            cut = newline + 1
        else:
            space = window.rfind(" ", 0, cut)
            if space >= cut // 2:
                cut = space + 1
    amp = window.rfind("&", 0, cut)
    if amp != -1 and ";" not in window[amp:cut] and cut - amp <= 12:
        cut = amp
    return max(1, cut)


def split_html(text: str, limit: int = config.MAX_MESSAGE_CHARS) -> list[str]:
    if not text:
        return []
    if len(text) <= limit:
        return [text]

    tokenizer = _ChunkTokenizer()
    try:
        tokenizer.feed(text)
        tokenizer.close()
    except Exception:
        return _split_plain(html_to_plain(text), limit)

    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    stack: list[tuple[str, str]] = []
    has_text = False

    def closing() -> str:
        return "".join(f"</{tag}>" for tag, _ in reversed(stack))

    def flush() -> None:
        nonlocal current, current_len, has_text
        body = "".join(current).strip()
        if body and has_text:
            chunks.append(body + closing())
        current = [raw for _, raw in stack]
        current_len = sum(len(raw) for raw in current)
        has_text = False

    for kind, tag, raw in tokenizer.tokens:
        if kind == "open":
            stack.append((tag or "", raw))
            current.append(raw)
            current_len += len(raw)
        elif kind == "close":
            for index in range(len(stack) - 1, -1, -1):
                if stack[index][0] == tag:
                    del stack[index:]
                    break
            current.append(raw)
            current_len += len(raw)
        elif kind == "self":
            current.append(raw)
            current_len += len(raw)
        else:
            remaining = raw
            while remaining:
                reserve = sum(len(tag) + 3 for tag, _ in stack)
                room = limit - current_len - reserve
                if room <= 80:
                    if has_text:
                        flush()
                        continue
                    room = 120
                if len(remaining) <= room:
                    current.append(remaining)
                    current_len += len(remaining)
                    has_text = True
                    break
                window = remaining[:room]
                cut = _safe_cut(window, room)
                current.append(remaining[:cut])
                current_len += cut
                has_text = True
                remaining = remaining[cut:]
                flush()

    body = "".join(current).strip()
    if body and has_text:
        chunks.append(body + closing())
    return [chunk for chunk in chunks if chunk.strip()]


def _split_plain(text: str, limit: int) -> list[str]:
    chunks: list[str] = []
    current: list[str] = []
    length = 0
    for line in text.split("\n"):
        if current and length + len(line) + 1 > limit:
            chunks.append("\n".join(current))
            current, length = [], 0
        while len(line) > limit:
            chunks.append(line[:limit])
            line = line[limit:]
        current.append(line)
        length += len(line) + 1
    if current:
        chunks.append("\n".join(current))
    return [chunk for chunk in chunks if chunk.strip()]


# --------------------------------------------------------------------------- #
# Sender
# --------------------------------------------------------------------------- #
class TelegramSender:
    def __init__(self, token: str) -> None:
        if not token:
            raise ValueError("TELEGRAM_BOT_TOKEN is missing")
        self._token = token
        self._base = f"https://api.telegram.org/bot{token}"
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": config.USER_AGENT})
        self._last_send: dict[str, float] = {}
        self._lock = threading.Lock()

    # -- low level ---------------------------------------------------- #
    def _post(self, method: str, payload: dict, timeout: int = 30):
        try:
            return self._session.post(
                f"{self._base}/{method}", json=payload, timeout=timeout
            )
        except requests.RequestException as exc:
            log.warning(
                "telegram network error method=%s err=%s",
                method,
                exc.__class__.__name__,
            )
            return None

    def _call(self, method: str, payload: dict) -> dict | None:
        for attempt in range(2):
            response = self._post(method, payload)
            if response is None:
                if attempt == 0:
                    time.sleep(1.5)
                    continue
                return None
            try:
                data = response.json()
            except ValueError:
                return None
            if response.status_code == 200 and isinstance(data, dict) and data.get("ok"):
                return data
            if response.status_code == 429:
                retry_after = 3
                if isinstance(data, dict):
                    retry_after = int(
                        ((data.get("parameters") or {}).get("retry_after")) or 3
                    )
                time.sleep(max(retry_after, 1))
                continue
            if response.status_code == 401:
                log.error("telegram rejected the bot token (401)")
                return None
            if response.status_code == 409:
                log.error(
                    "telegram 409: a webhook is set; call deleteWebhook once "
                    "(https://api.telegram.org/bot<TOKEN>/deleteWebhook)"
                )
                return None
            log.warning(
                "telegram call failed method=%s status=%s",
                method,
                response.status_code,
            )
            return None
        return None

    def _pace(self, chat_id: int | str) -> None:
        key = str(chat_id)
        with self._lock:
            last = self._last_send.get(key, 0.0)
        wait = config.MESSAGE_DELAY_SAME_CHAT - (time.monotonic() - last)
        if wait > 0:
            time.sleep(wait)
        with self._lock:
            self._last_send[key] = time.monotonic()

    # -- public API --------------------------------------------------- #
    def get_updates(self, offset: int = 0) -> list[dict]:
        payload = {
            "offset": offset,
            "timeout": 0,
            "limit": 100,
            "allowed_updates": ["message", "callback_query"],
        }
        data = self._call("getUpdates", payload)
        if not data:
            return []
        result = data.get("result")
        return result if isinstance(result, list) else []

    def set_webhook(self, url: str, secret_token: str | None = None) -> bool:
        payload = {
            "url": url,
            "allowed_updates": ["message", "callback_query"],
            "drop_pending_updates": True,
        }
        if secret_token:
            payload["secret_token"] = secret_token
        return self._call("setWebhook", payload) is not None

    def delete_webhook(self) -> bool:
        return self._call("deleteWebhook", {"drop_pending_updates": True}) is not None

    def get_webhook_info(self) -> dict | None:
        data = self._call("getWebhookInfo", {})
        result = (data or {}).get("result")
        return result if isinstance(result, dict) else None

    def get_chat_member(self, chat_id: int | str, channel: str) -> dict | None:
        data = self._call(
            "getChatMember", {"chat_id": channel, "user_id": chat_id}
        )
        if not data:
            return None
        result = data.get("result")
        return result if isinstance(result, dict) else None

    def answer_callback_query(self, callback_query_id: str, text: str = "") -> bool:
        payload = {"callback_query_id": callback_query_id}
        if text:
            payload["text"] = text[:200]
        return self._call("answerCallbackQuery", payload) is not None

    def send_message(
        self,
        chat_id: int | str,
        text: str,
        reply_markup: dict | None = None,
        on_sent=None,
        warning: str = "",
    ) -> bool:
        cleaned = sanitize_html(text)
        if not cleaned:
            return False
        notice = sanitize_html(warning)
        if notice:
            reserve = len(notice) + 2
            if reserve >= config.MAX_MESSAGE_CHARS - 120:
                raise ValueError("news warning is too long")
            chunks = [
                f"{chunk}\n\n{notice}"
                for chunk in split_html(cleaned, config.MAX_MESSAGE_CHARS - reserve)
            ]
        else:
            chunks = split_html(cleaned)
        ok = True
        markup = reply_markup
        for chunk in chunks:
            self._pace(chat_id)
            status = (
                self._send_text(chat_id, chunk, "HTML", markup)
                if on_sent is None
                else self._send_text(chat_id, chunk, "HTML", markup, on_sent)
            )
            markup = None
            if status == "parse_error":
                log.warning("HTML parse failed chat=%s, retrying as plain text", chat_id)
                if notice:
                    body, _ = chunk.rsplit("\n\n", 1)
                    plain_notice = html_to_plain(notice)
                    parts = [
                        f"{part}\n\n{plain_notice}"
                        for part in _split_plain(html_to_plain(body), 3900 - len(plain_notice) - 2)
                    ]
                else:
                    parts = _split_plain(html_to_plain(chunk), 3900)
                for part in parts:
                    self._pace(chat_id)
                    sent = (
                        self._send_plain(chat_id, part, markup)
                        if on_sent is None
                        else self._send_plain(chat_id, part, markup, on_sent)
                    )
                    if not sent:
                        ok = False
                    markup = None
            elif status != "ok":
                ok = False
        return ok

    def _send_plain(
        self,
        chat_id: int | str,
        text: str,
        reply_markup: dict | None = None,
        on_sent=None,
    ) -> bool:
        if on_sent is None:
            return self._send_text(chat_id, text, None, reply_markup) == "ok"
        return self._send_text(chat_id, text, None, reply_markup, on_sent) == "ok"

    def _send_text(
        self,
        chat_id: int | str,
        text: str,
        parse_mode: str | None,
        reply_markup: dict | None = None,
        on_sent=None,
    ) -> str:
        payload = {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": True,
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode
        if reply_markup:
            payload["reply_markup"] = reply_markup
        for attempt in range(3):
            response = self._post("sendMessage", payload)
            if response is None:
                time.sleep(1.5 * (attempt + 1))
                continue
            try:
                data = response.json()
            except ValueError:
                data = {}
            description = str(data.get("description") or "")
            if response.status_code == 200 and data.get("ok"):
                if on_sent is not None:
                    self._record_news_send(data, chat_id, on_sent)
                return "ok"
            if response.status_code == 429:
                retry_after = int(
                    ((data.get("parameters") or {}).get("retry_after")) or 3
                )
                log.warning(
                    "telegram 429 chat=%s retry_after=%ss",
                    chat_id,
                    retry_after,
                )
                time.sleep(max(retry_after, 1))
                continue
            if response.status_code >= 500:
                time.sleep(2 ** attempt)
                continue
            if response.status_code == 400 and "parse entities" in description.lower():
                return "parse_error"
            if response.status_code == 403:
                log.warning("telegram 403 chat=%s desc=%s", chat_id, description[:120])
                return "blocked"
            log.warning(
                "telegram send failed chat=%s status=%s desc=%s",
                chat_id,
                response.status_code,
                description[:160],
            )
            return "failed"
        return "failed"

    def _record_news_send(self, data: dict, chat_id: int | str, on_sent) -> None:
        result = data.get("result") or {}
        message_id = result.get("message_id") if isinstance(result, dict) else None
        if not isinstance(message_id, int) or isinstance(message_id, bool) or message_id <= 0:
            raise RuntimeError("Telegram returned a successful news send without a message ID")
        actual_chat = result.get("chat") or {}
        target = actual_chat.get("id", chat_id) if isinstance(actual_chat, dict) else chat_id
        try:
            on_sent(target, message_id, datetime.now(config.TEHRAN_TZ))
        except Exception as exc:
            try:
                deleted = self.delete_message(target, message_id)
            except Exception:
                deleted = False
                log.exception("immediate news deletion failed")
            log.error("news message persistence failed; immediate delete=%s", deleted)
            raise RuntimeError("news message persistence failed; immediate deletion attempted") from exc

    def delete_message(self, chat_id: int | str, message_id: int) -> bool:
        """Return True only when deleted or Telegram confirms it is already absent."""
        for attempt in range(3):
            response = self._post("deleteMessage", {"chat_id": chat_id, "message_id": message_id})
            if response is None:
                time.sleep(1.5 * (attempt + 1))
                continue
            try:
                data = response.json()
            except ValueError:
                data = {}
            if not isinstance(data, dict):
                data = {}
            if response.status_code == 200 and data.get("ok"):
                return True
            description = str(data.get("description") or "").lower()
            if response.status_code == 400 and "message to delete not found" in description:
                return True
            if response.status_code == 429:
                try:
                    delay = int((data.get("parameters") or {}).get("retry_after") or 3)
                except (TypeError, ValueError):
                    delay = 3
                time.sleep(max(delay, 1))
                continue
            if response.status_code >= 500:
                time.sleep(2 ** attempt)
                continue
            log.warning("telegram delete failed status=%s", response.status_code)
            return False
        return False

    def send_document(
        self,
        chat_id: int | str,
        filename: str,
        content: bytes,
        caption: str = "",
        on_sent=None,
    ) -> bool:
        data = {"chat_id": str(chat_id)}
        cleaned_caption = sanitize_html(caption) if caption else ""
        if cleaned_caption:
            if len(cleaned_caption) > 1000:
                parts = split_html(cleaned_caption, 1000)
                cleaned_caption = parts[0] if parts else ""
            data["caption"] = cleaned_caption
            data["parse_mode"] = "HTML"
        files = {"document": (filename, content, "application/pdf")}
        for attempt in range(3):
            self._pace(chat_id)
            try:
                response = self._session.post(
                    f"{self._base}/sendDocument",
                    data=data,
                    files=files,
                    timeout=90,
                )
            except requests.RequestException as exc:
                log.warning(
                    "telegram document network error err=%s", exc.__class__.__name__
                )
                time.sleep(1.5 * (attempt + 1))
                continue
            try:
                body = response.json()
            except ValueError:
                body = {}
            if response.status_code == 200 and body.get("ok"):
                if on_sent is not None:
                    self._record_news_send(body, chat_id, on_sent)
                return True
            if response.status_code == 429:
                retry_after = int(
                    ((body.get("parameters") or {}).get("retry_after")) or 3
                )
                time.sleep(max(retry_after, 1))
                continue
            if response.status_code >= 500:
                time.sleep(2 ** attempt)
                continue
            log.warning(
                "telegram document failed chat=%s status=%s desc=%s",
                chat_id,
                response.status_code,
                str(body.get("description") or "")[:160],
            )
            return False
        return False


# --------------------------------------------------------------------------- #
# Onboarding / commands (Persian + English)
# --------------------------------------------------------------------------- #
def _is_admin(chat_id: int | str) -> bool:
    if not config.ADMIN_CHAT_ID:
        return False
    return str(chat_id) == str(config.ADMIN_CHAT_ID)


def _subscribed(user: dict | None) -> bool:
    return bool(user and int(user.get("is_subscribed") or 0) == 1)


def _settings_keyboard(lang: str, subscribed: bool) -> dict:
    """Persistent reply keyboard with /start, /lang and the subscription toggles."""
    return config.main_keyboard(lang)


def _language_keyboard() -> dict:
    return {
        "inline_keyboard": [
            [
                {"text": "فارسی 🇮🇷", "callback_data": "set_lang:fa"},
                {"text": "English 🇬🇧", "callback_data": "set_lang:en"},
            ]
        ]
    }


CONFIRM_CALLBACKS = {
    "check_membership": "",
    "check_membership_aiceur_zero": "@aiceur_zero",
}


def _join_keyboard(lang: str, entries: list[dict]) -> dict:
    rows = [
        [{"text": f"📢 {entry.get('name') or entry.get('username')}", "url": entry["link"]}]
        for entry in entries
    ]
    callback = "check_membership"
    if len(entries) == 1 and entries[0].get("username") == "@aiceur_zero":
        callback = "check_membership_aiceur_zero"
    rows.append([{"text": config.confirm_join_text(lang), "callback_data": callback}])
    return {"inline_keyboard": rows}


def _send_join_request(
    sender: TelegramSender, chat_id: int | str, lang: str, entries: list[dict]
) -> None:
    sender.send_message(
        chat_id,
        config.join_required_text(lang, entries),
        reply_markup=_join_keyboard(lang, entries),
    )


def _show_main_keyboard(sender: TelegramSender, chat_id: int | str, lang: str) -> None:
    """Re-attach the persistent /start keyboard without sending a message."""
    sender.send_message(
        chat_id, config.hint_text(lang), reply_markup=config.main_keyboard(lang)
    )

def _missing_channels(
    sender: TelegramSender, channels, chat_id: int | str, only: str = ""
) -> list[dict]:
    """Pending channels; a gating error must never lock a user out."""
    if channels is None or not channels.is_enabled():
        return []
    try:
        missing = channels.missing_entries(sender, chat_id)
    except Exception:
        log.warning("membership check failed chat=%s", chat_id)
        return []
    if only:
        return [entry for entry in missing if entry["username"] == only]
    return missing


def _handle_message(message: dict, sender: TelegramSender, users, channels) -> None:
    chat = message.get("chat") or {}
    if chat.get("type") != "private":
        return
    chat_id = chat.get("id")
    if chat_id is None:
        return
    text = (message.get("text") or "").strip()
    if not text:
        return
    now = datetime.now(config.TEHRAN_TZ)
    admin = _is_admin(chat_id)
    if not admin and users.rate_limited(chat_id, now):
        return

    from_user = message.get("from") or {}
    first_name = str(from_user.get("first_name") or "")
    username = str(from_user.get("username") or "")
    detected = config.detect_language(from_user.get("language_code"))

    user = users.get(chat_id)
    lang = users.lang(chat_id) if user is not None else detected

    if not text.startswith("/"):
        # Plain text: nudge registered users to the settings screen.
        if user is not None:
            sender.send_message(chat_id, config.hint_text(lang), reply_markup=config.main_keyboard(lang))
        return

    head, _, arg = text.partition(" ")
    command = head.split("@", 1)[0].lower()
    arg = arg.strip()
    if arg and command not in ("/addchannel", "/removechannel"):
        return

    if command == "/start":
        if user is None:
            if not admin:
                missing = _missing_channels(sender, channels, chat_id)
                if missing:
                    _send_join_request(sender, chat_id, lang, missing)
                    return
            users.ensure(chat_id, first_name, username, now, language=detected)
            new_lang = users.lang(chat_id)
            sender.send_message(
                chat_id,
                config.welcome_text(new_lang, chat_id),
                reply_markup=config.main_keyboard(new_lang),
            )
            return
        users.ensure(chat_id, first_name, username, now)
        if not admin:
            missing = _missing_channels(sender, channels, chat_id)
            if missing:
                _send_join_request(sender, chat_id, lang, missing)
                return
        sent_user = users.get(chat_id) or user
        subscribed = _subscribed(sent_user)
        sender.send_message(
            chat_id,
            config.settings_text(lang, subscribed),
            reply_markup=_settings_keyboard(lang, False),
        )
        return

    if command in ("/lang", "/subscribe", "/unsubscribe"):
        if user is None:
            if not admin:
                missing = _missing_channels(sender, channels, chat_id)
                if missing:
                    _send_join_request(sender, chat_id, lang, missing)
                    return
            users.ensure(chat_id, first_name, username, now, language=detected)
            user = users.get(chat_id)
            lang = users.lang(chat_id) if user is not None else detected

    if command == "/lang":
        sender.send_message(
            chat_id, config.language_prompt_text(lang), reply_markup=_language_keyboard()
        )
        return

    if command in ("/subscribe", "/unsubscribe"):
        if not admin:
            missing = _missing_channels(sender, channels, chat_id)
            if missing:
                _send_join_request(sender, chat_id, lang, missing)
                return
        subscribed = command == "/subscribe"
        users.set_subscribed(chat_id, subscribed)
        sender.send_message(
            chat_id,
            config.subscription_changed_text(lang, subscribed),
            reply_markup=_settings_keyboard(lang, False),
        )
        return

    if command == "/help":
        sender.send_message(
            chat_id, config.help_text(lang), reply_markup=config.main_keyboard(lang)
        )
        return

    if command not in ("/stats", "/addchannel", "/removechannel") or not admin:
        return

    if command == "/stats":
        result = users.stats(now)
        sender.send_message(
            chat_id,
            config.stats_text(lang, result["total"], result["subscribed"], result["active"]),
        )
        return

    if not arg:
        sender.send_message(chat_id, config.channel_usage_text(lang))
        return
    if command == "/addchannel":
        added, channel = channels.add(arg)
        if not channel:
            sender.send_message(chat_id, config.channel_usage_text(lang))
        elif added:
            sender.send_message(chat_id, config.channel_added_text(lang, channel))
        else:
            sender.send_message(chat_id, config.channel_exists_text(lang, channel))
        return

    removed, channel = channels.remove(arg)
    if not channel:
        sender.send_message(chat_id, config.channel_usage_text(lang))
    elif removed:
        sender.send_message(chat_id, config.channel_removed_text(lang, channel))
    else:
        sender.send_message(chat_id, config.channel_missing_text(lang, channel))


def _handle_callback(callback: dict, sender: TelegramSender, users, channels) -> None:
    data = str(callback.get("data") or "")
    callback_id = str(callback.get("id") or "")
    message = callback.get("message") or {}
    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    if chat_id is None:
        return

    from_user = callback.get("from") or {}
    first_name = str(from_user.get("first_name") or "")
    username = str(from_user.get("username") or "")
    detected = config.detect_language(from_user.get("language_code"))
    now = datetime.now(config.TEHRAN_TZ)
    user = users.get(chat_id)
    lang = users.lang(chat_id) if user is not None else detected

    if data in CONFIRM_CALLBACKS:
        only = CONFIRM_CALLBACKS[data]
        missing = _missing_channels(sender, channels, chat_id, only=only)
        if missing:
            sender.answer_callback_query(callback_id, config.join_again_text(lang))
            _send_join_request(sender, chat_id, lang, missing)
            return
        created = False
        if user is None:
            users.ensure(chat_id, first_name, username, now, language=detected)
            created = True
        user = users.get(chat_id)
        lang = users.lang(chat_id) if user is not None else detected
        sender.answer_callback_query(callback_id, config.join_confirmed_text(lang))
        if created:
            sender.send_message(
                chat_id,
                config.welcome_text(lang, chat_id),
                reply_markup=_settings_keyboard(lang, False),
            )
            return
        sender.send_message(
            chat_id,
            config.settings_text(lang, _subscribed(user)),
            reply_markup=_settings_keyboard(lang, False),
        )
        return

    if user is None:
        missing = _missing_channels(sender, channels, chat_id)
        if missing:
            sender.answer_callback_query(callback_id, config.join_required_hint_text(lang))
            _send_join_request(sender, chat_id, lang, missing)
            return
        users.ensure(chat_id, first_name, username, now, language=detected)
        user = users.get(chat_id)
        lang = users.lang(chat_id) if user is not None else detected

    if data == "lang_menu":
        sender.answer_callback_query(callback_id)
        sender.send_message(
            chat_id, config.language_prompt_text(lang), reply_markup=_language_keyboard()
        )
        return

    if data.startswith("set_lang:"):
        code = data.split(":", 1)[1].strip().lower()
        if not users.set_language(chat_id, code):
            sender.answer_callback_query(callback_id)
            return
        lang = users.lang(chat_id)
        sender.answer_callback_query(callback_id, config.language_saved_text(lang))
        sender.send_message(
            chat_id,
            config.settings_text(lang, _subscribed(user)),
            reply_markup=_settings_keyboard(lang, False),
        )
        return

    if data == "help":
        sender.answer_callback_query(callback_id, "✅")
        sender.send_message(chat_id, config.help_text(lang))
        return

    if data == "toggle_subscription":
        subscribed = not _subscribed(user)
        users.set_subscribed(chat_id, subscribed)
        sender.answer_callback_query(
            callback_id, config.subscription_changed_text(lang, subscribed)
        )
        sender.send_message(
            chat_id,
            config.settings_text(lang, subscribed),
            reply_markup=_settings_keyboard(lang, False),
        )
        return

    sender.answer_callback_query(callback_id)


def handle_updates(
    updates: list[dict], sender: TelegramSender, users, channels
) -> int:
    last_update_id = 0
    for update in updates:
        try:
            update_id = int(update.get("update_id") or 0)
            last_update_id = max(last_update_id, update_id)
            if update.get("callback_query"):
                _handle_callback(update["callback_query"], sender, users, channels)
                continue
            message = update.get("message")
            if message:
                _handle_message(message, sender, users, channels)
        except Exception:
            log.exception("failed to process update")
    for registry in (users, channels):
        try:
            registry.flush()
        except Exception:
            log.exception("failed to save registry")
    return last_update_id

