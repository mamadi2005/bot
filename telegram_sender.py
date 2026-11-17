"""Telegram Bot API: onboarding, HTML sanitizing, splitting, retries."""

from __future__ import annotations

import html
import logging
import re
import threading
import time
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
                time.sleep(min(max(retry_after, 1), 60))
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
            "allowed_updates": ["message"],
        }
        data = self._call("getUpdates", payload)
        if not data:
            return []
        result = data.get("result")
        return result if isinstance(result, list) else []

    def send_message(self, chat_id: int | str, text: str) -> bool:
        cleaned = sanitize_html(text)
        if not cleaned:
            return False
        ok = True
        for chunk in split_html(cleaned):
            self._pace(chat_id)
            status = self._send_text(chat_id, chunk, "HTML")
            if status == "parse_error":
                log.warning("HTML parse failed chat=%s, retrying as plain text", chat_id)
                for part in _split_plain(html_to_plain(chunk), 3900):
                    self._pace(chat_id)
                    if not self._send_plain(chat_id, part):
                        ok = False
            elif status != "ok":
                ok = False
        return ok

    def _send_plain(self, chat_id: int | str, text: str) -> bool:
        return self._send_text(chat_id, text, None) == "ok"

    def _send_text(self, chat_id: int | str, text: str, parse_mode: str | None) -> str:
        payload = {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": True,
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode
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
                time.sleep(min(max(retry_after, 1), 60))
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

    def send_document(
        self,
        chat_id: int | str,
        filename: str,
        content: bytes,
        caption: str = "",
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
                return True
            if response.status_code == 429:
                retry_after = int(
                    ((body.get("parameters") or {}).get("retry_after")) or 3
                )
                time.sleep(min(max(retry_after, 1), 60))
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
# Onboarding / commands
# --------------------------------------------------------------------------- #
def handle_updates(updates: list[dict], sender: TelegramSender) -> int:
    last_update_id = 0
    for update in updates:
        try:
            update_id = int(update.get("update_id") or 0)
            last_update_id = max(last_update_id, update_id)
            message = update.get("message") or {}
            chat = message.get("chat") or {}
            if chat.get("type") != "private":
                continue
            text = (message.get("text") or "").strip()
            if not text.startswith("/"):
                continue
            command = text.split()[0].split("@", 1)[0].lower()
            chat_id = chat.get("id")
            if chat_id is None:
                continue

            if command == "/start":
                sender.send_message(chat_id, config.start_text(chat_id))
            elif command == "/help":
                sender.send_message(chat_id, config.help_text())
        except Exception:
            log.exception("failed to process update")
    return last_update_id
