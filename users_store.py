"""data/users.json: language preference, profile snapshot and rate-limit state."""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path

import config
from store import StoreError

log = logging.getLogger(__name__)

VALID_LANGS = set(config.SUPPORTED_LANGS)


def _parse_time(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=config.TEHRAN_TZ)
    return parsed


def _key(chat_id: int | str) -> str | None:
    try:
        return str(int(chat_id))
    except (TypeError, ValueError):
        return None


class UserRegistry:
    """Persistent user directory; read-only in dry runs."""

    def __init__(self, path: Path, read_only: bool = False) -> None:
        self.path = Path(path)
        self.read_only = read_only
        self.users: dict[str, dict] = {}
        self._limits: dict[str, dict] = {}
        self._dirty = False

    # -- lifecycle ----------------------------------------------------- #
    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise StoreError(
                f"cannot read {self.path.name}: {exc.__class__.__name__}"
            ) from exc
        if not isinstance(data, dict):
            raise StoreError(f"{self.path.name} must contain a JSON object")
        for raw_key, entry in data.items():
            key = _key(raw_key)
            if key is None or not isinstance(entry, dict):
                continue
            self.users[key] = {
                "first_name": str(entry.get("first_name") or "")[:64],
                "username": str(entry.get("username") or "").lstrip("@"),
                "join_date": str(entry.get("join_date") or ""),
                "last_seen": str(entry.get("last_seen") or ""),
                "lang": str(entry.get("lang") or ""),
            }
            for field in ("msg_count", "msg_window_start", "muted_until"):
                if field in entry:
                    self.users[key][field] = entry[field]
        log.info("users loaded count=%d", len(self.users))

    def flush(self) -> None:
        if self.read_only or not self._dirty:
            return
        payload = {
            key: self.users[key]
            for key in sorted(
                self.users, key=lambda value: int(value) if value.isdigit() else value
            )
        }
        text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
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
        log.info("users saved count=%d bytes=%d", len(self.users), len(text))

    # -- users --------------------------------------------------------- #
    def get(self, chat_id: int | str) -> dict | None:
        key = _key(chat_id)
        return self.users.get(key) if key else None

    def chat_ids(self) -> list[int]:
        return [int(key) for key in self.users if key.isdigit()]

    def ensure(
        self,
        chat_id: int | str,
        first_name: str = "",
        username: str = "",
        now: datetime | None = None,
    ) -> tuple[dict | None, bool]:
        key = _key(chat_id)
        if key is None:
            return None, False
        stamp = (now or datetime.now(config.TEHRAN_TZ)).isoformat(timespec="seconds")
        clean_username = (username or "").lstrip("@")
        user = self.users.get(key)
        if user is None:
            user = {
                "first_name": (first_name or "")[:64],
                "username": clean_username,
                "join_date": stamp,
                "last_seen": stamp,
                "lang": "",
            }
            pending = self._limits.pop(key, None)
            if pending:
                user.update(pending)
            self.users[key] = user
            self._dirty = True
            log.info("user registered chat_id=%s", key)
            return user, True
        changed = False
        if user.get("last_seen") != stamp:
            user["last_seen"] = stamp
            changed = True
        if first_name and user.get("first_name") != first_name[:64]:
            user["first_name"] = first_name[:64]
            changed = True
        if clean_username and user.get("username") != clean_username:
            user["username"] = clean_username
            changed = True
        if changed:
            self._dirty = True
        return user, False

    def lang_of(self, chat_id: int | str) -> str:
        user = self.get(chat_id)
        return str(user.get("lang") or "") if user else ""

    def lang(self, chat_id: int | str, default: str | None = None) -> str:
        value = self.lang_of(chat_id)
        if value in VALID_LANGS:
            return value
        return default or config.DEFAULT_LANG

    def set_lang(self, chat_id: int | str, lang: str) -> bool:
        if lang not in VALID_LANGS:
            return False
        user = self.get(chat_id)
        if user is None:
            return False
        if user.get("lang") == lang:
            return True
        user["lang"] = lang
        self._dirty = True
        return True

    # -- stats --------------------------------------------------------- #
    def stats(self, now: datetime | None = None) -> dict[str, int]:
        current = now or datetime.now(config.TEHRAN_TZ)
        cutoff = current - timedelta(days=config.STATS_ACTIVE_DAYS)
        active = 0
        for user in self.users.values():
            seen = _parse_time(user.get("last_seen"))
            if seen is not None and seen >= cutoff:
                active += 1
        return {
            "total": len(self.users),
            "active": active,
            "langs": sum(
                1 for user in self.users.values() if user.get("lang") in VALID_LANGS
            ),
        }

    # -- rate limiting ------------------------------------------------- #
    def rate_limited(self, chat_id: int | str, now: datetime) -> bool:
        """Count this message; return True when the user must be ignored."""
        key = _key(chat_id)
        if key is None:
            return False
        target = self.users.get(key)
        persist = target is not None
        if target is None:
            target = self._limits.setdefault(key, {})

        muted = _parse_time(target.get("muted_until"))
        if muted is not None:
            if muted > now:
                return True
            target.pop("muted_until", None)
            target.pop("msg_count", None)
            target.pop("msg_window_start", None)
            if persist:
                self._dirty = True

        window = _parse_time(target.get("msg_window_start"))
        count = 0
        try:
            count = int(target.get("msg_count") or 0)
        except (TypeError, ValueError):
            count = 0
        if window is None or (now - window).total_seconds() > config.USER_RATE_WINDOW_SECONDS:
            count = 0
            window = now
        count += 1
        limit_reached = count > config.USER_RATE_MAX_MESSAGES
        if limit_reached:
            target["muted_until"] = (
                now + timedelta(seconds=config.USER_MUTE_SECONDS)
            ).isoformat(timespec="seconds")
            target["msg_count"] = 0
            target.pop("msg_window_start", None)
        else:
            target["msg_count"] = count
            target["msg_window_start"] = window.isoformat(timespec="seconds")
        if persist:
            self._dirty = True
        if limit_reached:
            log.info(
                "user rate limited chat_id=%s until=%s",
                key,
                target.get("muted_until"),
            )
        return limit_reached
