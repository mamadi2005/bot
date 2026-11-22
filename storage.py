"""SQLite/Postgres storage for users, channels, sent news and expiry tracking.

SQL text is written with ``?`` placeholders and rewritten to ``%s`` for
Postgres, so both backends share the same statements. Timestamps are stored as
UTC ISO-8601 strings, which sort lexicographically.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import config

log = logging.getLogger(__name__)

SCHEMA = (
    """CREATE TABLE IF NOT EXISTS users (
        chat_id TEXT PRIMARY KEY,
        first_name TEXT NOT NULL DEFAULT '',
        username TEXT NOT NULL DEFAULT '',
        language TEXT NOT NULL DEFAULT 'fa',
        is_subscribed INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL,
        last_seen TEXT NOT NULL,
        muted_until TEXT,
        msg_count INTEGER NOT NULL DEFAULT 0,
        msg_window_start TEXT
    )""",
    "CREATE TABLE IF NOT EXISTS channels (username TEXT PRIMARY KEY)",
    """CREATE TABLE IF NOT EXISTS news_messages (
        chat_id TEXT NOT NULL,
        message_id INTEGER NOT NULL,
        sent_at TEXT NOT NULL,
        PRIMARY KEY (chat_id, message_id)
    )""",
    """CREATE TABLE IF NOT EXISTS sent_items (
        url_hash TEXT PRIMARY KEY,
        url TEXT NOT NULL DEFAULT '',
        title TEXT NOT NULL DEFAULT '',
        sent_at TEXT NOT NULL
    )""",
    "CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(moment: datetime | None = None) -> str:
    return (moment or utc_now()).astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_iso(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _key(chat_id: int | str) -> str | None:
    try:
        return str(int(chat_id))
    except (TypeError, ValueError):
        return None


class Database:
    """Tiny portable connection wrapper (SQLite by default, Postgres optional)."""

    def __init__(self, dsn: str = "") -> None:
        dsn = dsn or os.getenv("DATABASE_URL", "") or ""
        if dsn.startswith("postgres"):
            self.backend = "postgres"
            self._dsn = dsn
        else:
            self.backend = "sqlite"
            path = dsn[len("sqlite:///"):] if dsn.startswith("sqlite:///") else dsn
            self.path = Path(path or os.getenv("DB_PATH", "data/bot.db"))
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = None

    # -- connection ---------------------------------------------------- #
    def connect(self):
        with self._lock:
            if self._conn is not None:
                return self._conn
            if self.backend == "sqlite":
                conn = sqlite3.connect(str(self.path), check_same_thread=False, timeout=30)
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA busy_timeout=30000")
                conn.execute("PRAGMA synchronous=NORMAL")
            else:
                from psycopg import connect  # optional dependency
                from psycopg.rows import dict_row

                conn = connect(self._dsn, autocommit=True, row_factory=dict_row)
            self._conn = conn
            return conn

    def _sql(self, sql: str) -> str:
        return sql.replace("?", "%s") if self.backend == "postgres" else sql

    def init(self) -> None:
        conn = self.connect()
        with self._lock:
            for statement in SCHEMA:
                if self.backend == "sqlite":
                    conn.execute(statement)
                else:
                    with conn.cursor() as cur:
                        cur.execute(statement)
            if self.backend == "sqlite":
                conn.commit()
        self._migrate()

    def _columns(self, table: str) -> set[str]:
        conn = self.connect()
        with self._lock:
            if self.backend == "sqlite":
                rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
                return {str(row["name"]) for row in rows}
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema = current_schema() AND table_name = %s",
                    (table,),
                )
                return {str(row["column_name"]) for row in cur.fetchall()}

    def _migrate(self) -> None:
        """Upgrade databases created before the multi-language schema."""
        columns = self._columns("users")
        if "language" in columns:
            return
        self.execute("ALTER TABLE users ADD COLUMN language TEXT NOT NULL DEFAULT 'fa'")
        if "lang" in columns:
            self.execute(
                "UPDATE users SET language = lang WHERE lang IN ('fa', 'en')"
            )
            log.info("migrated users.lang -> users.language")

    # -- queries ------------------------------------------------------- #
    def execute(self, sql: str, params: tuple | list = ()) -> int:
        conn = self.connect()
        with self._lock:
            if self.backend == "sqlite":
                cur = conn.execute(self._sql(sql), tuple(params))
                conn.commit()
                return cur.rowcount
            with conn.cursor() as cur:
                cur.execute(self._sql(sql), tuple(params))
                return cur.rowcount

    def executemany(self, sql: str, rows: list[tuple]) -> None:
        if not rows:
            return
        conn = self.connect()
        with self._lock:
            if self.backend == "sqlite":
                conn.executemany(self._sql(sql), rows)
                conn.commit()
                return
            with conn.cursor() as cur:
                cur.executemany(self._sql(sql), rows)

    def query(self, sql: str, params: tuple | list = ()) -> list[dict]:
        conn = self.connect()
        with self._lock:
            if self.backend == "sqlite":
                rows = conn.execute(self._sql(sql), tuple(params)).fetchall()
                return [dict(row) for row in rows]
            with conn.cursor() as cur:
                cur.execute(self._sql(sql), tuple(params))
                return [dict(row) for row in cur.fetchall()]

    def queryone(self, sql: str, params: tuple | list = ()) -> dict | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    @contextmanager
    def transaction(self):
        conn = self.connect()
        with self._lock:
            if self.backend == "sqlite":
                try:
                    yield conn
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise
            else:
                yield conn


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #
class SettingsStore:
    def __init__(self, db: Database) -> None:
        self.db = db

    def get(self, key: str, default: str = "") -> str:
        row = self.db.queryone("SELECT value FROM settings WHERE key = ?", (key,))
        return str(row["value"]) if row else default

    def set(self, key: str, value: str) -> None:
        self.db.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, str(value)),
        )


# --------------------------------------------------------------------------- #
# Users
# --------------------------------------------------------------------------- #
class UserStore:
    """Drop-in replacement for ``UserRegistry`` backed by the database."""

    def __init__(self, db: Database, read_only: bool = False) -> None:
        self.db = db
        self.read_only = read_only

    def load(self) -> None:
        return None

    def flush(self) -> None:
        return None

    # -- reads --------------------------------------------------------- #
    def get(self, chat_id: int | str) -> dict | None:
        key = _key(chat_id)
        if key is None:
            return None
        row = self.db.queryone("SELECT * FROM users WHERE chat_id = ?", (key,))
        return dict(row) if row else None

    def count(self) -> int:
        row = self.db.queryone("SELECT COUNT(*) AS total FROM users")
        return int(row["total"]) if row else 0

    @property
    def users(self) -> dict:
        rows = self.db.query("SELECT chat_id FROM users")
        return {str(row["chat_id"]): {} for row in rows}

    def chat_ids(self, subscribed_only: bool = True) -> list[int]:
        sql = "SELECT chat_id FROM users"
        if subscribed_only:
            sql += " WHERE is_subscribed = 1"
        ids = []
        for row in self.db.query(sql + " ORDER BY chat_id"):
            try:
                ids.append(int(row["chat_id"]))
            except (TypeError, ValueError):
                continue
        return ids

    def lang_of(self, chat_id: int | str) -> str:
        user = self.get(chat_id)
        if not user:
            return ""
        return str(user.get("language") or user.get("lang") or "")

    def lang(self, chat_id: int | str, default: str | None = None) -> str:
        value = self.lang_of(chat_id)
        if value in config.SUPPORTED_LANGS:
            return value
        return default or config.DEFAULT_LANG

    def is_subscribed(self, chat_id: int | str) -> bool:
        user = self.get(chat_id)
        return bool(user and int(user.get("is_subscribed") or 0) == 1)

    # -- writes -------------------------------------------------------- #
    def ensure(
        self,
        chat_id: int | str,
        first_name: str = "",
        username: str = "",
        now: datetime | None = None,
        language: str = "",
    ) -> tuple[dict | None, bool]:
        key = _key(chat_id)
        if key is None or self.read_only:
            return None, False
        stamp = iso(now)
        clean_name = (first_name or "").strip()[:64]
        clean_username = (username or "").lstrip("@")[:32]
        initial_lang = language if language in config.SUPPORTED_LANGS else config.DEFAULT_LANG
        existing = self.get(key)
        if existing is None:
            self.db.execute(
                "INSERT INTO users (chat_id, first_name, username, language, is_subscribed, "
                "created_at, last_seen) VALUES (?, ?, ?, ?, 1, ?, ?) "
                "ON CONFLICT(chat_id) DO NOTHING",
                (key, clean_name, clean_username, initial_lang, stamp, stamp),
            )
            return self.get(key), True
        self.db.execute(
            "UPDATE users SET last_seen = ?, first_name = CASE WHEN ? != '' THEN ? ELSE first_name END, "
            "username = CASE WHEN ? != '' THEN ? ELSE username END WHERE chat_id = ?",
            (stamp, clean_name, clean_name, clean_username, clean_username, key),
        )
        return self.get(key), False

    def set_language(self, chat_id: int | str, language: str) -> bool:
        key = _key(chat_id)
        if key is None or language not in config.SUPPORTED_LANGS or self.read_only:
            return False
        return (
            self.db.execute(
                "UPDATE users SET language = ? WHERE chat_id = ?", (language, key)
            )
            > 0
        )

    def set_lang(self, chat_id: int | str, lang: str) -> bool:
        """Backwards-compatible alias for :meth:`set_language`."""
        return self.set_language(chat_id, lang)

    def set_subscribed(self, chat_id: int | str, subscribed: bool) -> bool:
        key = _key(chat_id)
        if key is None or self.read_only:
            return False
        return self.db.execute(
            "UPDATE users SET is_subscribed = ? WHERE chat_id = ?",
            (1 if subscribed else 0, key),
        ) > 0

    def mute(self, chat_id: int | str, now: datetime) -> None:
        key = _key(chat_id)
        if key is None or self.read_only:
            return
        until = iso(now + timedelta(seconds=config.USER_MUTE_SECONDS))
        self.db.execute(
            "UPDATE users SET muted_until = ?, msg_count = 0, msg_window_start = NULL WHERE chat_id = ?",
            (until, key),
        )

    def stats(self, now: datetime | None = None) -> dict[str, int]:
        current = now or utc_now()
        cutoff = iso(current - timedelta(days=config.STATS_ACTIVE_DAYS))
        total = self.count()
        active_row = self.db.queryone(
            "SELECT COUNT(*) AS active FROM users WHERE last_seen >= ?", (cutoff,)
        )
        subs_row = self.db.queryone(
            "SELECT COUNT(*) AS subscribed FROM users WHERE is_subscribed = 1"
        )
        return {
            "total": total,
            "active": int(active_row["active"]) if active_row else 0,
            "subscribed": int(subs_row["subscribed"]) if subs_row else 0,
        }

    # -- rate limiting ------------------------------------------------- #
    def rate_limited(self, chat_id: int | str, now: datetime) -> bool:
        """Count this message; return True when it must be ignored."""
        key = _key(chat_id)
        if key is None or self.read_only:
            return False
        user = self.get(key)
        if user is None:
            return False
        muted = parse_iso(user.get("muted_until"))
        if muted is not None:
            if muted > now:
                return True
            self.db.execute(
                "UPDATE users SET muted_until = NULL, msg_count = 0, msg_window_start = NULL "
                "WHERE chat_id = ?",
                (key,),
            )
            user = self.get(key) or user
        window = parse_iso(user.get("msg_window_start"))
        try:
            count = int(user.get("msg_count") or 0)
        except (TypeError, ValueError):
            count = 0
        if window is None or (now - window).total_seconds() > config.USER_RATE_WINDOW_SECONDS:
            count = 0
            window = now
        count += 1
        if count > config.USER_RATE_MAX_MESSAGES:
            self.mute(key, now)
            return True
        self.db.execute(
            "UPDATE users SET msg_count = ?, msg_window_start = ? WHERE chat_id = ?",
            (count, iso(window), key),
        )
        return False


# --------------------------------------------------------------------------- #
# Channels
# --------------------------------------------------------------------------- #
class ChannelStore:
    """Drop-in replacement for ``ChannelRegistry`` backed by the database."""

    def __init__(self, db: Database, read_only: bool = False) -> None:
        self.db = db
        self.read_only = read_only
        self.channels: list[str] = []

    def load(self) -> None:
        self.channels = [
            str(row["username"])
            for row in self.db.query("SELECT username FROM channels ORDER BY username")
        ]

    def flush(self) -> None:
        return None

    def add(self, value: str) -> tuple[bool, str]:
        from channels_store import normalize_channel

        channel = normalize_channel(value)
        if not channel:
            return False, ""
        if self.read_only:
            return False, channel
        changed = self.db.execute(
            "INSERT INTO channels (username) VALUES (?) ON CONFLICT(username) DO NOTHING",
            (channel,),
        )
        if changed:
            self.load()
            return True, channel
        return False, channel

    def remove(self, value: str) -> tuple[bool, str]:
        from channels_store import normalize_channel

        channel = normalize_channel(value)
        if not channel or self.read_only:
            return False, channel or ""
        # Stored rows may predate normalisation; match case-insensitively.
        changed = self.db.execute(
            "DELETE FROM channels WHERE lower(username) = ?", (channel,)
        )
        if changed:
            self.load()
            return True, channel
        return False, channel

    def configured(self) -> list[dict]:
        """Gating list = ``channels_config.REQUIRED_CHANNELS`` + DB channels.

        Each entry is ``{"username": "@name", "name": "@Name", "link": url}``.
        The config file is the recommended way to add channels because it also
        carries the invite link; rows added with ``/addchannel`` are merged in
        with a derived ``https://t.me/...`` link.
        """
        from channels_config import REQUIRED_CHANNELS
        from channels_store import join_url, normalize_channel

        entries: list[dict] = []
        seen: set[str] = set()
        for raw in REQUIRED_CHANNELS or []:
            if isinstance(raw, dict):
                username = raw.get("username") or raw.get("channel") or ""
                link = str(raw.get("link") or "").strip()
            else:
                username, link = str(raw), ""
            channel = normalize_channel(str(username))
            if not channel or channel in seen:
                continue
            seen.add(channel)
            name = str(raw.get("name") or username).strip() or channel
            entries.append(
                {"username": channel, "name": name, "link": link or join_url(channel)}
            )
        for channel in self.channels:
            if channel in seen:
                continue
            seen.add(channel)
            entries.append(
                {"username": channel, "name": channel, "link": join_url(channel)}
            )
        return entries

    def is_enabled(self) -> bool:
        return bool(self.configured())

    def missing_entries(self, sender, chat_id: int | str) -> list[dict]:
        """Configured channels the user has not joined yet."""
        from channels_store import MEMBER_STATUSES

        missing: list[dict] = []
        for entry in self.configured():
            member = sender.get_chat_member(chat_id, entry["username"])
            if member is None:
                log.warning(
                    "join check skipped channel=%s (bot must be an admin there)",
                    entry["username"],
                )
                continue
            status = str(member.get("status") or "").lower()
            if status in MEMBER_STATUSES:
                continue
            if status == "restricted" and member.get("is_member") is True:
                continue
            missing.append(entry)
        return missing

    def missing_for(self, sender, chat_id: int | str) -> list[str]:
        return [entry["username"] for entry in self.missing_entries(sender, chat_id)]


# --------------------------------------------------------------------------- #
# News state (dedupe + expiry + counters)
# --------------------------------------------------------------------------- #
class NewsStore:
    """Drop-in replacement for the JSON ``BaseStore`` pipeline state."""

    def __init__(self, db: Database) -> None:
        self.db = db
        self.settings = SettingsStore(db)
        self.news_messages: list[dict] = []
        self._sent_cache: set[str] | None = None

    # -- lifecycle ----------------------------------------------------- #
    def load(self) -> None:
        self.news_messages = [
            {
                "chat_id": int(row["chat_id"]),
                "message_id": int(row["message_id"]),
                "sent_at": row["sent_at"],
            }
            for row in self.db.query("SELECT chat_id, message_id, sent_at FROM news_messages")
            if str(row["chat_id"]).lstrip("-").isdigit()
        ]

    def flush(self) -> None:
        return None

    # -- settings ------------------------------------------------------ #
    def get_state(self, key: str, default: str = "") -> str:
        return self.settings.get(key, default)

    def set_state(self, key: str, value: str) -> None:
        self.settings.set(key, value)

    # -- dedupe -------------------------------------------------------- #
    @property
    def sent_hashes(self) -> set[str]:
        if self._sent_cache is None:
            cutoff = iso(utc_now() - timedelta(days=config.SENT_LOG_RETENTION_DAYS))
            self._sent_cache = {
                str(row["url_hash"])
                for row in self.db.query(
                    "SELECT url_hash FROM sent_items WHERE sent_at >= ?", (cutoff,)
                )
            }
        return self._sent_cache

    def log_sent(self, url_hash: str, url: str, title: str, category: str, sent_at: str) -> bool:
        if not url_hash or url_hash in self.sent_hashes:
            return False
        changed = self.db.execute(
            "INSERT INTO sent_items (url_hash, url, title, sent_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(url_hash) DO NOTHING",
            (url_hash, url or "", (title or "")[:200], sent_at or iso()),
        )
        if changed:
            self.sent_hashes.add(url_hash)
            return True
        return False

    # -- message expiry ------------------------------------------------ #
    def record_news_message(self, chat_id: int | str, message_id: int, sent_at: datetime) -> None:
        entry = {
            "chat_id": int(chat_id),
            "message_id": int(message_id),
            "sent_at": iso(sent_at),
        }
        self.db.execute(
            "INSERT INTO news_messages (chat_id, message_id, sent_at) VALUES (?, ?, ?) "
            "ON CONFLICT(chat_id, message_id) DO UPDATE SET sent_at = excluded.sent_at",
            (str(entry["chat_id"]), entry["message_id"], entry["sent_at"]),
        )
        self.news_messages.append(entry)

    def remove_news_message(self, entry: dict) -> None:
        try:
            self.news_messages.remove(entry)
        except ValueError:
            pass
        self.db.execute(
            "DELETE FROM news_messages WHERE chat_id = ? AND message_id = ?",
            (str(entry.get("chat_id")), int(entry.get("message_id") or 0)),
        )

    def due_news_messages(self, now: datetime | None = None) -> list[dict]:
        current = now or utc_now()
        cutoff = iso(current - timedelta(seconds=config.NEWS_LIFETIME_SECONDS))
        rows = self.db.query(
            "SELECT chat_id, message_id, sent_at FROM news_messages WHERE sent_at <= ? "
            "ORDER BY sent_at LIMIT 500",
            (cutoff,),
        )
        return [
            {
                "chat_id": int(row["chat_id"]),
                "message_id": int(row["message_id"]),
                "sent_at": row["sent_at"],
            }
            for row in rows
            if str(row["chat_id"]).lstrip("-").isdigit()
        ]


def open_database(dsn: str = "") -> Database:
    db = Database(dsn)
    db.init()
    return db


def stores(db: Database) -> tuple[UserStore, ChannelStore, NewsStore]:
    users = UserStore(db)
    channels = ChannelStore(db)
    news = NewsStore(db)
    users.load()
    channels.load()
    news.load()
    return users, channels, news
