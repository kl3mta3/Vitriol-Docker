"""Thin async wrapper around the engine's `convert_file`.

Keeps the engine code (`app/core/router.py` etc.) untouched. Adds:
  - thread-pool execution
  - per-job progress pushed to a websocket pub/sub
  - DB job row updates
  - cancellation tokens addressable by job_id
"""
from __future__ import annotations
import asyncio
import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Optional

from sqlalchemy.orm import Session

from app.core.router import convert_file, UnsupportedConversionError
from app.utils.cancellation import CancellationToken, CancelledError

from ..config import get_settings
from ..db import SessionLocal
from ..models import Job, JobStatus

_settings = get_settings()
_executor = ThreadPoolExecutor(max_workers=_settings.max_concurrent_conversions, thread_name_prefix="vit-convert")
_tokens: dict[int, CancellationToken] = {}
_subscribers: dict[int, set[asyncio.Queue]] = {}
_subscribers_lock = threading.Lock()


# ------------------------------------------------------------- pub/sub

def _publish(job_id: int, event: dict) -> None:
    with _subscribers_lock:
        queues = list(_subscribers.get(job_id, ()))
    for q in queues:
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            pass


def subscribe(job_id: int) -> asyncio.Queue:
    q: asyncio.Queue = asyncio.Queue(maxsize=128)
    with _subscribers_lock:
        _subscribers.setdefault(job_id, set()).add(q)
    return q


def unsubscribe(job_id: int, q: asyncio.Queue) -> None:
    with _subscribers_lock:
        s = _subscribers.get(job_id)
        if s is not None:
            s.discard(q)
            if not s:
                _subscribers.pop(job_id, None)


# ----------------------------------------------------------- run a job

def submit(job_id: int) -> None:
    """Schedule a job for execution. Job row must already be in `queued` state."""
    _executor.submit(_run, job_id)


def cancel(job_id: int) -> bool:
    tok = _tokens.get(job_id)
    if tok is None:
        return False
    tok.cancel()
    return True


def _run(job_id: int) -> None:
    db: Session = SessionLocal()
    token = CancellationToken()
    _tokens[job_id] = token
    try:
        job: Optional[Job] = db.query(Job).get(job_id)
        if job is None:
            return
        job.status = JobStatus.running
        job.started_at = datetime.utcnow()
        job.progress = 0
        db.commit()
        _publish(job_id, {"type": "started", "progress": 0})

        warnings: list[str] = []
        password = b""
        if job.has_password:
            pw_path = Path(job.src_path).with_suffix(Path(job.src_path).suffix + ".pw")
            try:
                password = pw_path.read_bytes()
            except OSError:
                password = b""

        def _progress(p: float) -> None:
            pct = max(0, min(100, int(p * 100)))
            try:
                # Reuse the open session — small writes, low contention.
                job.progress = pct
                db.commit()
            except Exception:
                db.rollback()
            _publish(job_id, {"type": "progress", "progress": pct})

        try:
            convert_file(
                src=Path(job.src_path),
                dst=Path(job.dst_path),
                src_ext=job.src_ext,
                dst_ext=job.dst_ext,
                cancel=token,
                progress=_progress,
                warnings=warnings,
                masquerade=bool(job.stone),
                compiler=bool(job.self_compile_target == "py"),
                password=password,
                preserve_animations=False,
            )
            # .exe self-compile path: route through masquerade with .exe target.
            # The engine handles .exe natively via masquerade when stone=True
            # and dst_ext='.exe'; nothing extra needed here.
            try:
                size = Path(job.dst_path).stat().st_size
                job.bytes_out = size
            except OSError:
                pass
            job.status = JobStatus.done
            job.progress = 100
            job.warnings_json = json.dumps(warnings) if warnings else None
            job.finished_at = datetime.utcnow()
            db.commit()
            _publish(job_id, {"type": "done", "warnings": warnings})
        except CancelledError:
            job.status = JobStatus.cancelled
            job.finished_at = datetime.utcnow()
            db.commit()
            _publish(job_id, {"type": "cancelled"})
        except UnsupportedConversionError as e:
            job.status = JobStatus.failed
            job.error = str(e)
            job.finished_at = datetime.utcnow()
            db.commit()
            _publish(job_id, {"type": "failed", "error": str(e)})
        except Exception as e:
            job.status = JobStatus.failed
            job.error = f"{type(e).__name__}: {e}"
            job.finished_at = datetime.utcnow()
            db.commit()
            _publish(job_id, {"type": "failed", "error": job.error})
    finally:
        _tokens.pop(job_id, None)
        # Cleanup password file if any.
        try:
            j = db.query(Job).get(job_id)
            if j is not None and j.has_password:
                pw_path = Path(j.src_path).with_suffix(Path(j.src_path).suffix + ".pw")
                if pw_path.exists():
                    pw_path.unlink()
        except Exception:
            pass
        db.close()


# ----------------------------------------------------------- formats

def supported_formats() -> dict:
    """Snapshot of the engine's registered formats. Imported lazily so the
    handler discovery side-effects only run when first needed."""
    from app import format_handlers as fh
    fh.load_all()
    inputs = sorted(set(fh.READERS) | set(fh.MEDIA_HANDLERS) | set(fh.STONE_ONLY_SOURCES))
    outputs = sorted(set(fh.WRITERS) | set(fh.MEDIA_WRITE_OK))
    targets_for: dict[str, list[str]] = {}
    for ext in inputs:
        try:
            targets_for[ext] = sorted(fh.valid_targets_for(ext, masquerade=False))
            targets_for[ext + "+stone"] = sorted(fh.valid_targets_for(ext, masquerade=True))
        except Exception:
            targets_for[ext] = []
    return {
        "inputs": inputs,
        "outputs": outputs,
        "targets_for": targets_for,
        "media_categories": dict(fh.MEDIA_CATEGORY_OF),
    }
