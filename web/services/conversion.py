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
_subscribers: dict[int, set[tuple[asyncio.Queue, asyncio.AbstractEventLoop]]] = {}
_subscribers_lock = threading.Lock()


# ------------------------------------------------------------- pub/sub
#
# Cross-thread plumbing: subscribers are websocket handlers running on
# the event-loop thread; publishers are conversion jobs running in
# ThreadPoolExecutor workers. asyncio.Queue is NOT thread-safe — calling
# put_nowait() from a worker thread silently drops events (sometimes!)
# and that's exactly the symptom of "100% then no done event" we hit in
# production. Fix: capture the loop at subscribe time and route every
# publish through loop.call_soon_threadsafe, which IS thread-safe.

# Each subscription stores (queue, owning_loop) so the publisher knows
# which loop to schedule the put on.
_Subscriber = tuple  # (asyncio.Queue, asyncio.AbstractEventLoop)


def _put_safe(q: "asyncio.Queue", event: dict) -> None:
    """Runs on the event-loop thread (scheduled by call_soon_threadsafe)."""
    try:
        q.put_nowait(event)
    except asyncio.QueueFull:
        pass


def _publish(job_id: int, event: dict) -> None:
    with _subscribers_lock:
        subs = list(_subscribers.get(job_id, ()))
    for q, loop in subs:
        if loop.is_closed():
            continue
        try:
            loop.call_soon_threadsafe(_put_safe, q, event)
        except RuntimeError:
            # Loop was closed between our check and the schedule call.
            # Treat as a dead subscriber — the WS will drop on its end.
            pass


def subscribe(job_id: int) -> asyncio.Queue:
    """Called by the websocket handler on the event-loop thread."""
    q: asyncio.Queue = asyncio.Queue(maxsize=128)
    loop = asyncio.get_running_loop()
    with _subscribers_lock:
        _subscribers.setdefault(job_id, set()).add((q, loop))
    return q


def unsubscribe(job_id: int, q: asyncio.Queue) -> None:
    with _subscribers_lock:
        s = _subscribers.get(job_id)
        if s is None:
            return
        # Find the (q, loop) tuple matching this queue and discard it.
        target = next((sub for sub in s if sub[0] is q), None)
        if target is not None:
            s.discard(target)
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
    # The engine only understands local Paths. For S3-backed jobs we
    # download the source into a tempfile, give the engine a tempfile
    # destination, then upload the result back to the backend. For
    # local-backed jobs both URIs already point at files on disk and
    # the download/upload are no-ops (we still go through the abstraction
    # so cleanup paths handle both backends uniformly).
    import tempfile
    from ..services.storage import LocalBackend, backend_for_uri, current_backend
    work_dir = Path(tempfile.mkdtemp(prefix="vitriol-job-"))
    src_local: Optional[Path] = None
    dst_local: Optional[Path] = None
    try:
        job: Optional[Job] = db.query(Job).get(job_id)
        if job is None:
            return
        job.status = JobStatus.running
        job.started_at = datetime.utcnow()
        job.progress = 0
        db.commit()
        _publish(job_id, {"type": "started", "progress": 0})

        src_backend = backend_for_uri(job.src_path)
        # Fast path: if source is local, skip the download — the engine
        # can read it in place.
        if isinstance(src_backend, LocalBackend):
            src_local = src_backend._uri_to_path(job.src_path)
        else:
            src_local = work_dir / f"src{job.src_ext}"
            src_backend.download_to_path(job.src_path, src_local)

        # Same fast path for the destination — local backend writes
        # straight to the final location; non-local backends use a
        # tempfile and we upload after.
        active_backend = current_backend(db)
        if isinstance(active_backend, LocalBackend) and job.dst_path.startswith("file://"):
            dst_local = LocalBackend._uri_to_path(job.dst_path)
            dst_local.parent.mkdir(parents=True, exist_ok=True)
        else:
            dst_local = work_dir / f"dst{job.dst_ext}"

        warnings: list[str] = []
        password = b""
        if job.has_password:
            pw_path = src_local.with_suffix(src_local.suffix + ".pw")
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
                src=src_local,
                dst=dst_local,
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
            # Enforce the per-user max-output-size cap before publishing
            # the result. Engine ran on a tempfile, so refusing here is
            # cheap — we just unlink and fail the job. Without this,
            # a Stone-mode PNG → WAV expansion (~5x) lets a small input
            # bypass max_file_size_bytes by producing a much larger
            # output. Three-tier resolution: user → role → server.
            try:
                final_out_size = dst_local.stat().st_size
            except OSError:
                final_out_size = 0
            from ..services import quotas as _quotas
            from ..models import ServerSettings as _SS
            _s = db.query(_SS).get(1)
            owner = db.query(Job).get(job_id).user if False else None  # noqa — readability stub
            from ..models import User as _U
            owner_row = db.query(_U).get(job.user_id)
            out_cap = _quotas.max_output_size_for(owner_row, _s) if owner_row else None
            if out_cap is not None and final_out_size > out_cap:
                # Delete the over-cap output and fail the job. The user
                # sees a clear message + the file never reaches their
                # storage quota or download flow.
                try:
                    dst_local.unlink(missing_ok=True)
                except OSError:
                    pass
                raise RuntimeError(
                    f"Output exceeds max-output-size cap "
                    f"({final_out_size:,} > {out_cap:,} bytes). "
                    f"Try a smaller input, a less-expanding target format, "
                    f"or ask an admin to raise your output-size cap."
                )

            # If the destination was a tempfile (non-local backend), push
            # it to the active backend now and update the Job row with
            # the resulting URI. The placeholder URI written at upload
            # time gets replaced with the canonical URI from the upload.
            if not (isinstance(active_backend, LocalBackend) and job.dst_path.startswith("file://")):
                # Recover the original key from the placeholder URI so
                # the uploaded object lands at exactly the spot the
                # placeholder predicted (keeps cleanup/audit consistent).
                key = _key_from_placeholder(job.dst_path)
                final_uri = active_backend.upload_from_path(dst_local, key)
                job.dst_path = final_uri
            job.bytes_out = final_out_size
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
        # Cleanup password file if any. For local sources the .pw
        # sidecar lives next to src_local; for non-local sources it
        # would have been on the original upload (also local) — best
        # effort either way.
        try:
            if src_local is not None:
                pw_path = src_local.with_suffix(src_local.suffix + ".pw")
                if pw_path.exists():
                    pw_path.unlink()
        except Exception:
            pass
        # Always nuke the tempdir we made; harmless when src/dst were
        # local files outside the tempdir.
        try:
            import shutil as _sh
            _sh.rmtree(work_dir, ignore_errors=True)
        except Exception:
            pass
        db.close()


def _key_from_placeholder(uri: str) -> str:
    """Recover the backend-relative key from a placeholder URI. The
    placeholder was built by ``_placeholder_uri_for`` at upload time;
    we strip the scheme + bucket/prefix to get back the original key
    ("outputs/<user>_<token>.<ext>").
    """
    if uri.startswith("file://"):
        # file:///data/outputs/12_abc.png  → "outputs/12_abc.png"
        from urllib.parse import urlparse, unquote
        p = unquote(urlparse(uri).path)
        # Drop leading /data/ if present so the key matches the
        # backend's namespace expectations.
        cfg_data = str(get_settings().data_dir).rstrip("/")
        if p.startswith(cfg_data + "/"):
            return p[len(cfg_data) + 1:]
        # Fall back to last two path components.
        parts = p.strip("/").split("/")
        return "/".join(parts[-2:]) if len(parts) >= 2 else parts[-1]
    if uri.startswith("s3://"):
        # s3://bucket/<key>  → "<key>"
        from urllib.parse import urlparse, unquote
        return unquote(urlparse(uri).path.lstrip("/"))
    return uri


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
