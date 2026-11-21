"""Render webhook server: Telegram updates + the scheduled news trigger.

Routes
------
POST /webhook/<secret>   Telegram update intake (secret path from env)
GET  /health             liveness probe for Render
POST /trigger_news       news run, called by GitHub Actions with a bearer token

Run locally with ``python server.py`` and expose it with a tunnel, or deploy to
Render with ``render.yaml``. Telegram requires HTTPS, so Render is the normal
target.
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import threading
import time
from datetime import datetime

from flask import Flask, jsonify, request

import config
import main
from storage import open_database, stores
from telegram_sender import TelegramSender

log = logging.getLogger("server")

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 1 * 1024 * 1024

# Serialize DB access inside one process; the pipeline additionally guards runs.
_DB_LOCK = threading.Lock()
_db = None
_state: dict = {"started": time.time(), "last_news_run": None, "updates": 0, "errors": 0}


def db():
    global _db
    if _db is None:
        _db = open_database()
    return _db


@app.get("/health")
def health():
    ok = True
    detail = ""
    try:
        db().queryone("SELECT 1 AS ok")
    except Exception as exc:  # noqa: BLE001
        ok = False
        detail = exc.__class__.__name__
    return (
        jsonify(
            {
                "ok": ok,
                "detail": detail,
                "uptime_seconds": int(time.time() - _state["started"]),
                "updates": _state["updates"],
                "errors": _state["errors"],
                "last_news_run": _state["last_news_run"],
                "webhook_configured": bool(config.WEBHOOK_SECRET),
                "trigger_configured": bool(config.TRIGGER_TOKEN),
            }
        ),
        (200 if ok else 503),
    )


def _update_allowed_secret(provided: str) -> bool:
    expected = config.WEBHOOK_SECRET
    if not expected:
        return True  # allow /webhook without a secret path when unset
    return hmac.compare_digest(str(provided or ""), str(expected))


@app.post("/webhook")
@app.post("/webhook/<secret>")
def webhook(secret: str = ""):
    if not _update_allowed_secret(secret):
        return jsonify({"ok": False, "error": "forbidden"}), 403

    update = request.get_json(silent=True)
    if not isinstance(update, dict) or not update.get("update_id"):
        return jsonify({"ok": False, "error": "invalid update"}), 400

    try:
        sender = TelegramSender(config.TELEGRAM_BOT_TOKEN)
        with _DB_LOCK:
            users, channels, _ = stores(db())
            main.process_update(update, sender, users, channels)
        _state["updates"] += 1
    except Exception:  # noqa: BLE001
        _state["errors"] += 1
        log.exception("failed to process update id=%s", update.get("update_id"))
        # Always 200: Telegram must not retry a poisoned update forever.
    return jsonify({"ok": True})


def _trigger_authorized() -> bool:
    expected = config.TRIGGER_TOKEN
    if not expected:
        return False  # never open by default
    header = request.headers.get("Authorization", "")
    token = header[7:].strip() if header.lower().startswith("bearer ") else ""
    if not token:
        token = request.headers.get("X-Trigger-Token", "").strip()
    if not token:
        token = (request.get_json(silent=True) or {}).get("token", "")
    return hmac.compare_digest(str(token), str(expected))


def _run_news_job(*, dry_run: bool, cleanup_only: bool, limit: int) -> None:
    _state["news_running"] = True
    _state["last_result"] = None
    try:
        result = main.run_news(dry_run=dry_run, cleanup_only=cleanup_only, limit=limit)
    except Exception as exc:  # noqa: BLE001
        _state["errors"] += 1
        log.exception("triggered news run failed")
        result = {"ok": False, "error": main.redact(str(exc))[:300]}
    finally:
        _state["news_running"] = False
        _state["last_news_run"] = datetime.utcnow().isoformat(timespec="seconds") + "Z"
        _state["last_result"] = result


@app.post("/trigger_news")
def trigger_news():
    """Kick off a news run. Async by default so the HTTP call returns fast.

    Body (all optional): {"dry_run": bool, "cleanup_only": bool, "limit": int,
    "wait": bool, "token": "..."} — prefer the Authorization: Bearer header.
    """
    if not _trigger_authorized():
        return jsonify({"ok": False, "error": "unauthorized"}), 401

    body = request.get_json(silent=True) or {}
    dry_run = bool(body.get("dry_run")) or request.args.get("dry_run") == "1"
    cleanup_only = bool(body.get("cleanup_only"))
    try:
        limit = max(0, int(body.get("limit") or 0))
    except (TypeError, ValueError):
        limit = 0
    wait = bool(body.get("wait")) or request.args.get("wait") == "1"

    if _state.get("news_running"):
        return jsonify({"ok": False, "error": "a news run is already in progress", "running": True}), 409

    if wait:
        _run_news_job(dry_run=dry_run, cleanup_only=cleanup_only, limit=limit)
        result = _state.get("last_result") or {"ok": False, "error": "no result"}
        return jsonify(result), (200 if result.get("ok") else 500)

    _state["last_news_run"] = datetime.utcnow().isoformat(timespec="seconds") + "Z"
    threading.Thread(
        target=_run_news_job,
        kwargs={"dry_run": dry_run, "cleanup_only": cleanup_only, "limit": limit},
        daemon=True,
        name="news-run",
    ).start()
    return jsonify({"ok": True, "accepted": True, "dry_run": dry_run, "running": True}), 202


@app.get("/news_status")
def news_status():
    if not _trigger_authorized():
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    return jsonify(
        {
            "ok": True,
            "running": bool(_state.get("news_running")),
            "last_news_run": _state.get("last_news_run"),
            "last_result": _state.get("last_result"),
        }
    )


@app.post("/set_webhook")
def set_webhook():
    """Helper: point Telegram at this deployment. Requires TRIGGER_TOKEN."""
    if not _trigger_authorized():
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    base = (request.get_json(silent=True) or {}).get("base_url") or config.PUBLIC_BASE_URL
    if not base:
        return jsonify({"ok": False, "error": "base_url missing"}), 400
    url = f"{base.rstrip('/')}/webhook" + (f"/{config.WEBHOOK_SECRET}" if config.WEBHOOK_SECRET else "")
    sender = TelegramSender(config.TELEGRAM_BOT_TOKEN)
    ok = sender.set_webhook(url, secret_token=config.WEBHOOK_SECRET or None)
    return jsonify({"ok": ok, "webhook_url": url})


@app.errorhandler(404)
def not_found(_error):
    return jsonify({"ok": False, "error": "not found"}), 404


@app.errorhandler(500)
def server_error(_error):
    _state["errors"] += 1
    return jsonify({"ok": False, "error": "internal error"}), 500


if __name__ == "__main__":
    main.setup_logging(os.getenv("DEBUG", "").lower() in ("1", "true", "yes"))
    port = int(os.getenv("PORT", "8080"))
    log.info("starting webhook server on port=%d", port)
    app.run(host="0.0.0.0", port=port, threaded=True)
