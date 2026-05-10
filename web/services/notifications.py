"""Outbound notification fan-out.

``notify_all(db, message)`` walks every enabled row in
``notification_channels`` and dispatches the message via the matching
per-kind handler. Each handler is independent — a failure in one
channel doesn't block the rest. Per-channel failures are stamped onto
the row's ``last_test_*`` columns so the UI can surface them.

Adding a new kind:
  1. Extend ``models.NotificationKind``.
  2. Add a ``_send_<kind>`` async function below.
  3. Wire it into ``_HANDLERS``.
  4. Document its config schema in the model docstring.
  5. Add a UI catalog template in ``admin_server.js NOTIFICATION_TEMPLATES``.

All handlers respect a 15s budget — the same hard cap as the SMTP test
endpoint — so a broken target can't hang the whole signup-notification
fan-out for a minute.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shlex
from datetime import datetime
from typing import Any, Awaitable, Callable, Optional

import httpx
from sqlalchemy.orm import Session

from ..auth.crypto import decrypt
from ..models import NotificationChannel, NotificationKind

logger = logging.getLogger("vitriol.notifications")

_HTTP_TIMEOUT = 15.0  # seconds — same budget as the SMTP test endpoint


# --------------------------------------------------------------------- shared

def _config(ch: NotificationChannel) -> dict:
    """Decode the per-kind config JSON. Tolerates corrupt rows by
    returning an empty dict so a single bad channel doesn't break the
    fan-out for everyone else."""
    try:
        return json.loads(ch.config_json or "{}")
    except (json.JSONDecodeError, TypeError):
        logger.warning("Channel %s (%s) has malformed config_json", ch.id, ch.kind.value)
        return {}


def _secret(ch: NotificationChannel) -> Optional[str]:
    """Decrypt the channel's secret column. Returns None if the column
    is empty or decryption fails (key mismatch after a rotation, etc.)."""
    if not ch.secret_enc:
        return None
    return decrypt(ch.secret_enc)


# ---------------------------------------------------------------- per-kind

async def _send_discord(ch: NotificationChannel, message: str) -> None:
    url = _secret(ch)
    if not url:
        raise RuntimeError("Discord webhook URL not set")
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
        r = await client.post(url, json={"content": message})
        r.raise_for_status()


async def _send_slack(ch: NotificationChannel, message: str) -> None:
    url = _secret(ch)
    if not url:
        raise RuntimeError("Slack webhook URL not set")
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
        r = await client.post(url, json={"text": message})
        r.raise_for_status()


async def _send_ntfy(ch: NotificationChannel, message: str) -> None:
    cfg = _config(ch)
    server = (cfg.get("server_url") or "").rstrip("/")
    topic = (cfg.get("topic") or "").strip().lstrip("/")
    if not server or not topic:
        raise RuntimeError("ntfy server_url and topic are required")
    url = f"{server}/{topic}"
    headers = {"Content-Type": "text/plain; charset=utf-8"}

    auth_kind = (cfg.get("auth_kind") or "none").lower()
    secret = _secret(ch)
    auth = None
    if auth_kind == "bearer" and secret:
        headers["Authorization"] = f"Bearer {secret}"
    elif auth_kind == "basic" and secret and ":" in secret:
        # Stored as "user:pass"; httpx accepts a (user, pass) tuple.
        user, _, pw = secret.partition(":")
        auth = (user, pw)

    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
        r = await client.post(url, content=message.encode("utf-8"), headers=headers, auth=auth)
        r.raise_for_status()


async def _send_gotify(ch: NotificationChannel, message: str) -> None:
    cfg = _config(ch)
    server = (cfg.get("server_url") or "").rstrip("/")
    token = _secret(ch)
    if not server or not token:
        raise RuntimeError("Gotify server_url and app token are required")
    url = f"{server}/message?token={token}"
    payload = {"title": "Vitriol", "message": message, "priority": 5}
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
        r = await client.post(url, json=payload)
        r.raise_for_status()


async def _send_telegram(ch: NotificationChannel, message: str) -> None:
    cfg = _config(ch)
    chat_id = (cfg.get("chat_id") or "").strip()
    token = _secret(ch)
    if not chat_id or not token:
        raise RuntimeError("Telegram bot token and chat_id are required")
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
        r = await client.post(url, json={"chat_id": chat_id, "text": message})
        r.raise_for_status()


async def _send_generic_webhook(ch: NotificationChannel, message: str) -> None:
    cfg = _config(ch)
    url = (cfg.get("url") or "").strip()
    if not url:
        raise RuntimeError("Generic webhook URL is required")
    method = (cfg.get("method") or "POST").upper()
    headers: dict[str, str] = {}
    raw_headers = cfg.get("headers_json") or ""
    if raw_headers:
        try:
            parsed = json.loads(raw_headers)
            if isinstance(parsed, dict):
                headers = {str(k): str(v) for k, v in parsed.items()}
        except json.JSONDecodeError:
            logger.warning("Generic webhook %s has malformed headers_json", ch.id)

    bearer = _secret(ch)
    if bearer:
        headers.setdefault("Authorization", f"Bearer {bearer}")

    body_template = cfg.get("body_template") or "{message}"
    body = body_template.replace("{message}", message)
    headers.setdefault("Content-Type", "application/json" if body.lstrip().startswith(("{", "[")) else "text/plain")

    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
        r = await client.request(method, url, content=body.encode("utf-8"), headers=headers)
        r.raise_for_status()


async def _send_script(ch: NotificationChannel, message: str) -> None:
    cfg = _config(ch)
    body = cfg.get("script") or ""
    if not body.strip():
        raise RuntimeError("Script body is empty")
    # Run via bash with the message in the env. Hard 15s budget.
    proc = await asyncio.create_subprocess_exec(
        "bash", "-c", body,
        env={**os.environ, "VITRIOL_MESSAGE": message},
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=_HTTP_TIMEOUT)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        raise RuntimeError("Script timed out after 15s")
    if proc.returncode != 0:
        tail = (stderr.decode("utf-8", errors="replace") or stdout.decode("utf-8", errors="replace"))[-200:]
        raise RuntimeError(f"Script exited {proc.returncode}: {tail}")


async def _send_bluesky(ch: NotificationChannel, message: str) -> None:
    """Post to Bluesky via the AT Protocol XRPC API.

    Two-step: createSession (handle + app password → access JWT), then
    createRecord (post the message). Uses the operator's PDS, which
    defaults to bsky.social but can be overridden for self-hosted PDSes.
    """
    cfg = _config(ch)
    handle = (cfg.get("handle") or "").strip()
    server = (cfg.get("server") or "https://bsky.social").rstrip("/")
    app_password = _secret(ch)
    if not handle or not app_password:
        raise RuntimeError("Bluesky handle and app password are required")
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
        sess = await client.post(
            f"{server}/xrpc/com.atproto.server.createSession",
            json={"identifier": handle, "password": app_password},
        )
        sess.raise_for_status()
        session = sess.json()
        access_jwt = session["accessJwt"]
        did = session["did"]
        # Bluesky posts cap at 300 graphemes; truncate defensively.
        post_text = message[:280]
        record = {
            "repo": did,
            "collection": "app.bsky.feed.post",
            "record": {
                "$type": "app.bsky.feed.post",
                "text": post_text,
                "createdAt": datetime.utcnow().isoformat(timespec="seconds") + "Z",
            },
        }
        r = await client.post(
            f"{server}/xrpc/com.atproto.repo.createRecord",
            json=record,
            headers={"Authorization": f"Bearer {access_jwt}"},
        )
        r.raise_for_status()


# Map kinds → handlers. New kinds added here.
_HANDLERS: dict[NotificationKind, Callable[[NotificationChannel, str], Awaitable[None]]] = {
    NotificationKind.discord: _send_discord,
    NotificationKind.slack: _send_slack,
    NotificationKind.ntfy: _send_ntfy,
    NotificationKind.gotify: _send_gotify,
    NotificationKind.telegram: _send_telegram,
    NotificationKind.generic_webhook: _send_generic_webhook,
    NotificationKind.script: _send_script,
    NotificationKind.bluesky: _send_bluesky,
}


# ---------------------------------------------------------------- public API

async def send_one(db: Session, ch: NotificationChannel, message: str) -> tuple[bool, Optional[str]]:
    """Send to one channel. Returns (ok, error_message). Stamps the
    last_test_* columns on the row so the admin UI can show the result.

    Used by both the per-row Test button and the fan-out below.
    """
    handler = _HANDLERS.get(ch.kind)
    if handler is None:
        err = f"Unknown notification kind: {ch.kind!r}"
        ch.last_test_at = datetime.utcnow()
        ch.last_test_ok = False
        ch.last_test_error = err
        db.commit()
        return False, err
    try:
        await handler(ch, message)
    except Exception as e:
        err = f"{type(e).__name__}: {e}"[:500]
        logger.exception("Notification %s (%s) failed", ch.id, ch.kind.value)
        ch.last_test_at = datetime.utcnow()
        ch.last_test_ok = False
        ch.last_test_error = err
        db.commit()
        return False, err
    ch.last_test_at = datetime.utcnow()
    ch.last_test_ok = True
    ch.last_test_error = None
    db.commit()
    return True, None


async def notify_all(db: Session, message: str) -> int:
    """Fan out a message to every enabled channel. Returns the count of
    channels that succeeded. Used by signup, approval, and any other
    flow that previously called the singleton ``discord.notify``.
    """
    channels = (
        db.query(NotificationChannel)
        .filter(NotificationChannel.enabled.is_(True))
        .all()
    )
    if not channels:
        return 0
    # Run in parallel — the per-channel handlers are independent and
    # there's no benefit to serializing them. Each has its own 15s cap.
    results = await asyncio.gather(
        *[send_one(db, ch, message) for ch in channels],
        return_exceptions=True,
    )
    return sum(1 for r in results if isinstance(r, tuple) and r[0])
