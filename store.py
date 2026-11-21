"""Persistent state without a database: data/state.json committed by the workflow."""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path

import config

log = logging.getLogger(__name__)


class StoreError(RuntimeError):
    """State file unreadable or unwritable."""


def _parse_timestamp(value: object) -> datetime | None:
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


class BaseStore:
    """In-memory state shared by the memory and JSON implementations."""

    def __init__(self) -> None:
        self.state: dict[str, str] = {}
        self.sent: dict[str, str] = {}
        self.sent_hashes: set[str] = set()
        self._dirty = False
        self.news_messages: list[dict] = []
        self._extra: dict = {}

    def load(self) -> None:
        return None

    def flush(self) -> None:
        self._dirty = False

    def _save_news(self) -> None:
        return None

    def get_state(self, key: str, default: str = "") -> str:
        return self.state.get(key, default)

    def set_state(self, key: str, value: str) -> None:
        value = str(value)
        if self.state.get(key) == value:
            return
        self.state[key] = value
        self._dirty = True

    def record_news_message(
        self, chat_id: int | str, message_id: int, sent_at: datetime
    ) -> None:
        entry = {
            "chat_id": chat_id, "message_id": message_id, "sent_at": sent_at.isoformat()
        }
        self.news_messages.append(entry)
        try:
            self._save_news()
        except Exception:
            self.news_messages.remove(entry)
            raise

    def remove_news_message(self, entry: dict) -> None:
        self.news_messages.remove(entry)
        try:
            self._save_news()
        except Exception:
            self.news_messages.append(entry)
            raise

    def log_sent(
        self,
        url_hash: str,
        url: str,
        title: str,
        category: str,
        sent_at: str,
    ) -> bool:
        if not url_hash or url_hash in self.sent_hashes:
            return False
        self.sent_hashes.add(url_hash)
        self.sent[url_hash] = sent_at
        self._dirty = True
        return True


class MemoryStore(BaseStore):
    """Dry-run store: keeps everything in memory, never touches disk."""


class JsonStore(BaseStore):
    def __init__(self, path: Path) -> None:
        super().__init__()
        self.path = Path(path)
        self.news_path = self.path.with_name("news_messages.json")

    def load(self) -> None:
        if self.news_path.exists():
            try:
                messages = json.loads(self.news_path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise StoreError(
                    f"cannot read {self.news_path.name}: {exc.__class__.__name__}"
                ) from exc
            if not isinstance(messages, list):
                raise StoreError(f"{self.news_path.name} must contain a JSON array")
            self.news_messages = messages
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

        self._extra = {
            key: value for key, value in data.items()
            if key not in {"version", "updated_at", "state", "sent"}
        }

        state = data.get("state")
        if isinstance(state, dict):
            self.state = {str(key): str(value) for key, value in state.items()}

        sent = data.get("sent")
        if isinstance(sent, dict):
            cutoff = datetime.now(config.TEHRAN_TZ) - timedelta(
                days=config.SENT_LOG_RETENTION_DAYS
            )
            for url_hash, stamp in sent.items():
                parsed = _parse_timestamp(stamp)
                if parsed is not None and parsed < cutoff:
                    self._dirty = True
                    continue
                key = str(url_hash)
                self.sent[key] = str(stamp)
                self.sent_hashes.add(key)
        log.info(
            "state loaded keys=%d sent=%d", len(self.state), len(self.sent)
        )

    def _save_news(self) -> None:
        text = json.dumps(self.news_messages, ensure_ascii=False, separators=(",", ":"))
        try:
            self.news_path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.news_path.with_suffix(".json.tmp")
            temp.write_text(text, encoding="utf-8")
            os.replace(temp, self.news_path)
        except OSError as exc:
            raise StoreError(
                f"cannot write {self.news_path.name}: {exc.__class__.__name__}"
            ) from exc

    def flush(self) -> None:
        if not self._dirty:
            return
        payload = {
            **self._extra,
            "version": 1,
            "updated_at": datetime.now(config.TEHRAN_TZ).isoformat(timespec="seconds"),
            "state": dict(sorted(self.state.items())),
            "sent": dict(sorted(self.sent.items())),
        }
        text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
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
        log.info(
            "state saved path=%s sent=%d bytes=%d",
            self.path.name,
            len(self.sent),
            len(text),
        )
