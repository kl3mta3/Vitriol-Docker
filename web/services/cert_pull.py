"""SSL cert pull — webhook (HTTP) or script (shell) modes.

Two paths the super admin can wire up:

1. **Webhook**: a configurable HTTP request to a cert-API endpoint. Supports
   GET or POST, optional static-header auth (e.g. `X-Api-Key`), and either
   HMAC-SHA256 body signing OR header auth (mutually exclusive in practice).
   Response is parsed as JSON; the cert + key are pulled out using
   configurable field names (default `fullchain` / `privkey`; can be set to
   `certPem` / `certKey` for SphereSSL).

2. **Script**: a shell script the operator pastes in. Stored in the DB,
   written to a temp file and executed with `bash`. Receives `$VITRIOL_CERT_DIR`
   pointing at `/data/certs/` and is expected to write `fullchain.pem` +
   `privkey.pem` into that directory. Most flexible — handles any cert API
   shape since the operator owns the parsing logic.

Both paths land at `_install(...)` which writes the files atomically (tmp +
rename), so a failed pull mid-write doesn't leave a half-baked cert that
the proxy might pick up.
"""
from __future__ import annotations
import asyncio
import hashlib
import hmac
import json
import logging
import os
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Optional

import httpx
from sqlalchemy.orm import Session

from ..auth.crypto import decrypt
from ..config import get_settings
from ..models import ServerSettings

_log = logging.getLogger(__name__)
_cfg = get_settings()


class CertPullError(Exception):
    """Bubbles the *user-facing* reason a pull failed up to the route /
    scheduler so the UI can show it. Keep the message short — it ends up
    in `ssl_cert_pull_last_status` (VARCHAR(512))."""


async def run(db: Session) -> str:
    """Dispatch to the configured mode. Returns a short status message
    suitable for storing on the row + showing in the UI."""
    s: Optional[ServerSettings] = db.query(ServerSettings).get(1)
    if s is None:
        raise CertPullError("Server settings row missing.")

    mode = (s.ssl_cert_pull_mode or "webhook").lower()
    try:
        if mode == "script":
            msg = await _run_script(s)
        elif mode == "webhook":
            msg = await _run_webhook(s)
        else:
            raise CertPullError(f"Unknown cert-pull mode {mode!r}.")
    except CertPullError as e:
        s.ssl_cert_pull_last_run_at = datetime.utcnow()
        s.ssl_cert_pull_last_status = f"failed: {e}"[:512]
        db.commit()
        raise
    except Exception as e:
        s.ssl_cert_pull_last_run_at = datetime.utcnow()
        s.ssl_cert_pull_last_status = f"failed: {type(e).__name__}: {e}"[:512]
        db.commit()
        raise CertPullError(str(e)) from e

    s.ssl_cert_pull_last_run_at = datetime.utcnow()
    s.ssl_cert_pull_last_status = msg[:512]
    db.commit()
    return msg


# ----------------------------------------------------------------- webhook

async def _run_webhook(s: ServerSettings) -> str:
    if not s.ssl_cert_pull_webhook_url:
        raise CertPullError(
            "Webhook URL is empty (mode=webhook). Either fill in the URL field "
            "and click Save settings, or switch the Mode dropdown to 'Script'."
        )
    method = (s.ssl_cert_pull_webhook_method or "POST").upper()
    headers: dict[str, str] = {}
    body: Optional[bytes] = None

    # Static-header auth (SphereSSL-style) takes precedence when set.
    header_name = (s.ssl_cert_pull_webhook_header_name or "").strip()
    header_value = decrypt(s.ssl_cert_pull_webhook_header_value_enc) if s.ssl_cert_pull_webhook_header_value_enc else None
    if header_name and header_value:
        headers[header_name] = header_value

    # POST gets a JSON body and (if no static header set) the HMAC sig.
    if method == "POST":
        body = json.dumps({"action": "pull-certs"}).encode("utf-8")
        headers["Content-Type"] = "application/json"
        if not header_name:
            secret = decrypt(s.ssl_cert_pull_webhook_secret_enc) or ""
            if secret:
                sig = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
                headers["X-Vitriol-Signature"] = f"sha256={sig}"

    try:
        # follow_redirects: many cert APIs (SphereSSL, etc.) put auth checks
        # behind 302s before serving the cert payload. Default-off in httpx
        # so we have to opt in explicitly.
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            r = await client.request(method, s.ssl_cert_pull_webhook_url, content=body, headers=headers)
            r.raise_for_status()
            data = r.json()
    except httpx.HTTPStatusError as e:
        raise CertPullError(f"Webhook returned {e.response.status_code}.")
    except json.JSONDecodeError:
        raise CertPullError("Webhook response is not JSON.")
    except Exception as e:
        raise CertPullError(f"Webhook call failed: {e}")

    cert_field = s.ssl_cert_pull_response_cert_field or "fullchain"
    key_field = s.ssl_cert_pull_response_key_field or "privkey"
    fullchain = data.get(cert_field)
    privkey = data.get(key_field)
    if not fullchain or not privkey:
        raise CertPullError(
            f"Webhook response missing {cert_field!r} or {key_field!r}. "
            f"Got keys: {sorted(data.keys())[:8]}"
        )
    _install(fullchain, privkey)
    return f"webhook ok — wrote fullchain.pem + privkey.pem ({len(fullchain)} / {len(privkey)} bytes)"


# ------------------------------------------------------------------ script

async def _run_script(s: ServerSettings) -> str:
    if not s.ssl_cert_pull_script or not s.ssl_cert_pull_script.strip():
        raise CertPullError(
            "Script body is empty (mode=script). Paste your script in the "
            "Script body field and click Save settings."
        )
    cert_dir = _cfg.cert_dir
    cert_dir.mkdir(parents=True, exist_ok=True)

    # Write the script to a temp file with execute permission. Drop it under
    # /data so the path is stable across container restarts and we can show
    # absolute paths in error messages.
    scripts_dir = _cfg.data_dir / "scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)
    script_path = scripts_dir / "cert_pull.sh"
    script_path.write_text(s.ssl_cert_pull_script, encoding="utf-8")
    try:
        os.chmod(script_path, 0o700)
    except OSError:
        pass

    env = {
        **os.environ,
        "VITRIOL_CERT_DIR": str(cert_dir),
        "VITRIOL_DATA_DIR": str(_cfg.data_dir),
        "PATH": os.environ.get("PATH", "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"),
    }

    # Execute under bash (slim image has it). 5-minute hard timeout —
    # plenty for a curl + jq, short enough that a hung renewal can't pin
    # the scheduler.
    proc = await asyncio.create_subprocess_exec(
        "bash", str(script_path),
        cwd=str(_cfg.data_dir),
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=300)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        raise CertPullError("Script timed out (>5 min).")

    if proc.returncode != 0:
        tail = (stderr or b"").decode("utf-8", errors="replace").strip().splitlines()
        last = tail[-1] if tail else "(no stderr)"
        raise CertPullError(f"Script exited {proc.returncode}: {last}")

    # We don't enforce a specific output filename — the operator's script
    # writes whatever it wants under $VITRIOL_CERT_DIR. The most common
    # convention is fullchain.pem + privkey.pem; surface what's there now.
    listed = sorted(p.name for p in cert_dir.iterdir() if p.is_file())
    summary = ", ".join(listed[:8]) if listed else "(no files)"
    return f"script ok (exit 0). $VITRIOL_CERT_DIR contents: {summary}"


# ------------------------------------------------------------------ shared

def _install(fullchain_pem: str, privkey_pem: str) -> None:
    cert_dir = _cfg.cert_dir
    cert_dir.mkdir(parents=True, exist_ok=True)
    # Atomic-ish: write to .tmp first, then rename. A reverse proxy that's
    # hot-reloading certs won't catch a half-written file.
    for name, content in [("fullchain.pem", fullchain_pem), ("privkey.pem", privkey_pem)]:
        target = cert_dir / name
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_text(content, encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, target)
