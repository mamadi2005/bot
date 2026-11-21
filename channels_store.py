"""data/channels.json: channels users must join before using the bot."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from store import StoreError

log = logging.getLogger(__name__)

MEMBER_STATUSES = {"creator", "administrator", "member"}


def normalize_channel(value: str) -> str:
    text = (value or "").strip().split()[0] if (value or "").strip() else ""
    if not text:
        return ""
    if not text.startswith("@"):
        text = "@" + text.lstrip("/")
    text = text.rstrip("/")
    # Telegram usernames are case-insensitive; store one canonical form.
    return text.lower() if len(text) > 1 else ""


def join_url(channel: str) -> str:
    return f"https://t.me/{channel.lstrip('@')}"


class ChannelRegistry:
    """Persistent force-join list; read-only in dry runs."""

    def __init__(self, path: Path, read_only: bool = False) -> None:
        self.path = Path(path)
        self.read_only = read_only
        self.channels: list[str] = []
        self._dirty = False

    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise StoreError(
                f"cannot read {self.path.name}: {exc.__class__.__name__}"
            ) from exc
        if isinstance(data, dict):
            data = data.get("channels")
        if not isinstance(data, list):
            raise StoreError(f"{self.path.name} must contain a JSON array")
        self.channels = []
        for entry in data:
            channel = normalize_channel(str(entry))
            if channel and channel not in self.channels:
                self.channels.append(channel)
        log.info("channels loaded count=%d", len(self.channels))

    def flush(self) -> None:
        if self.read_only or not self._dirty:
            return
        text = json.dumps(self.channels, ensure_ascii=False, indent=2)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.path.with_suffix(".json.tmp")
            temp.write_text(text, encoding="utf-8")
            os.replace(temp, self.path)
        except OSError as exc:
            raise StoreError(
                f"cannot write {self.path.name}: {exc.__class__.__name__}"
            ) from exc
        self._dirty = False
        log.info("channels saved count=%d", len(self.channels))

    def add(self, value: str) -> tuple[bool, str]:
        channel = normalize_channel(value)
        if not channel:
            return False, ""
        if channel.lower() in {existing.lower() for existing in self.channels}:
            return False, channel
        self.channels.append(channel)
        self._dirty = True
        return True, channel

    def remove(self, value: str) -> tuple[bool, str]:
        channel = normalize_channel(value)
        if not channel:
            return False, ""
        for existing in list(self.channels):
            if existing.lower() == channel.lower():
                self.channels.remove(existing)
                self._dirty = True
                return True, existing
        return False, channel

    def missing_for(self, sender, chat_id: int | str) -> list[str]:
        """Channels the user has not joined yet.

        A failed getChatMember call (usually because the bot is not an admin)
        is treated as satisfied so a misconfigured channel cannot lock everyone
        out; the failure is logged for the operator.
        """
        missing: list[str] = []
        for channel in self.channels:
            info = sender.get_chat_member(chat_id, channel)
            if info is None:
                log.warning(
                    "join check skipped channel=%s (bot must be an admin there)",
                    channel,
                )
                continue
            status = str(info.get("status") or "").lower()
            if status in MEMBER_STATUSES:
                continue
            if status == "restricted" and info.get("is_member"):
                continue
            missing.append(channel)
        return missing
