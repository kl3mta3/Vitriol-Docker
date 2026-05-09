"""Conversion API: upload + submit + list jobs + cancel + download."""
from __future__ import annotations
import json
import secrets
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..auth.permissions import (
    has_capability, CAN_RUN_CONVERSION, CAN_USE_STONE, CAN_USE_SELF_COMPILE,
)
from ..config import get_settings
from ..deps import get_current_user, get_db, require_active_non_pending
from ..models import Job, JobStatus, ServerSettings, User
from ..schemas import FormatsResponse, JobOut, MessageResponse
from ..services import audit, conversion as conv_svc, quotas

router = APIRouter(prefix="", tags=["convert"])
_cfg = get_settings()


@router.get("/formats", response_model=FormatsResponse)
def formats(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    data = conv_svc.supported_formats()
    s: Optional[ServerSettings] = db.query(ServerSettings).get(1)
    if s:
        try:
            disabled_in = set(json.loads(s.disabled_input_formats_json or "[]"))
            disabled_out = set(json.loads(s.disabled_output_formats_json or "[]"))
        except json.JSONDecodeError:
            disabled_in, disabled_out = set(), set()
        data["inputs"] = [e for e in data["inputs"] if e not in disabled_in]
        data["outputs"] = [e for e in data["outputs"] if e not in disabled_out]
        for k, v in list(data["targets_for"].items()):
            data["targets_for"][k] = [e for e in v if e not in disabled_out]
    return FormatsResponse(**data)


@router.post("/convert", response_model=JobOut)
async def submit_conversion(
    file: UploadFile = File(...),
    dst_ext: str = Form(...),
    stone: bool = Form(False),
    self_compile_target: Optional[str] = Form(None),  # 'py' or 'exe'
    verify_round_trip: bool = Form(False),
    password: Optional[str] = Form(None),
    user: User = Depends(require_active_non_pending),
    db: Session = Depends(get_db),
):
    if not has_capability(user, CAN_RUN_CONVERSION):
        raise HTTPException(status_code=403, detail="Conversions are not permitted for your role.")
    if stone and not has_capability(user, CAN_USE_STONE):
        raise HTTPException(status_code=403, detail="Stone mode not granted.")
    if self_compile_target and not has_capability(user, CAN_USE_SELF_COMPILE):
        raise HTTPException(status_code=403, detail="Self-compile not granted.")

    if not quotas.check_rate_limit(user, db):
        raise HTTPException(status_code=429, detail="Rate limit exceeded.")
    allowed, count, limit = quotas.check_and_increment_daily(db, user)
    if not allowed:
        raise HTTPException(status_code=429, detail=f"Daily limit reached ({count}/{limit}).")

    s: Optional[ServerSettings] = db.query(ServerSettings).get(1)
    max_size = s.max_file_size_bytes if s else 1024 * 1024 * 1024

    src_ext = "." + (file.filename.rsplit(".", 1)[-1] if file.filename and "." in file.filename else "bin").lower()
    dst_ext_norm = dst_ext if dst_ext.startswith(".") else "." + dst_ext

    # Honor disabled-format toggles.
    if s:
        try:
            if src_ext in set(json.loads(s.disabled_input_formats_json or "[]")):
                raise HTTPException(status_code=400, detail=f"Input format {src_ext} is disabled.")
            if dst_ext_norm in set(json.loads(s.disabled_output_formats_json or "[]")):
                raise HTTPException(status_code=400, detail=f"Output format {dst_ext_norm} is disabled.")
        except json.JSONDecodeError:
            pass

    # Stage upload to disk (streamed; no in-memory hold).
    job_token = secrets.token_hex(8)
    src_path = _cfg.upload_dir / f"{user.id}_{job_token}{src_ext}"
    bytes_in = 0
    with src_path.open("wb") as out:
        while True:
            chunk = await file.read(_cfg.upload_chunk_size)
            if not chunk:
                break
            bytes_in += len(chunk)
            if bytes_in > max_size:
                out.close()
                src_path.unlink(missing_ok=True)
                raise HTTPException(status_code=413, detail=f"File exceeds max size ({max_size} bytes).")
            out.write(chunk)

    safe_stem = (file.filename or "output").rsplit(".", 1)[0]
    safe_stem = "".join(c for c in safe_stem if c.isalnum() or c in ("-", "_"))[:96] or "output"
    if self_compile_target == "exe":
        dst_ext_actual = ".exe"
    elif self_compile_target == "py":
        dst_ext_actual = ".py"
    else:
        dst_ext_actual = dst_ext_norm
    dst_filename = f"{safe_stem}{dst_ext_actual}"
    dst_path = _cfg.output_dir / f"{user.id}_{job_token}{dst_ext_actual}"

    has_password = bool(password)
    if has_password:
        pw_path = src_path.with_suffix(src_path.suffix + ".pw")
        pw_path.write_bytes(password.encode("utf-8"))

    job = Job(
        user_id=user.id,
        src_filename=file.filename or f"upload{src_ext}",
        src_ext=src_ext,
        dst_ext=dst_ext_actual,
        dst_filename=dst_filename,
        stone=stone or bool(self_compile_target),
        self_compile_target=self_compile_target,
        verify_round_trip=verify_round_trip,
        status=JobStatus.queued,
        bytes_in=bytes_in,
        src_path=str(src_path),
        dst_path=str(dst_path),
        has_password=has_password,
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    conv_svc.submit(job.id)
    audit.log(db, user.id, "convert_submit", target_user_id=user.id, metadata={
        "job_id": job.id, "src_ext": src_ext, "dst_ext": dst_ext_actual, "stone": job.stone,
    })
    return job


@router.get("/jobs", response_model=List[JobOut])
def list_jobs(user: User = Depends(get_current_user), db: Session = Depends(get_db), limit: int = 50):
    return (
        db.query(Job)
        .filter(Job.user_id == user.id)
        .order_by(Job.id.desc())
        .limit(min(limit, 200))
        .all()
    )


@router.get("/jobs/{job_id}", response_model=JobOut)
def get_job(job_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    job: Optional[Job] = db.query(Job).get(job_id)
    if job is None or (job.user_id != user.id and user.role.value not in ("admin", "super_admin")):
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@router.delete("/jobs/{job_id}", response_model=MessageResponse)
def cancel_job(job_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    job: Optional[Job] = db.query(Job).get(job_id)
    if job is None or (job.user_id != user.id and user.role.value not in ("admin", "super_admin")):
        raise HTTPException(status_code=404, detail="Job not found")
    if job.status not in (JobStatus.queued, JobStatus.running):
        return MessageResponse(message=f"Job already {job.status.value}.")
    if conv_svc.cancel(job_id):
        audit.log(db, user.id, "job_cancel", target_user_id=job.user_id, metadata={"job_id": job_id})
        return MessageResponse(message="Cancellation requested.")
    return MessageResponse(message="Job is queued; cancellation requested.")


class _ZipRequest(BaseModel):
    ids: List[int]


@router.post("/jobs/download-zip")
def download_zip(req: _ZipRequest, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Bundle the result files of multiple done jobs into a single zip.

    Jobs the caller can't see (someone else's, or not yet done) are
    silently skipped — the response only includes what's actually
    available so a stale UI selection doesn't 403 the whole request.
    """
    if not req.ids:
        raise HTTPException(status_code=400, detail="No job ids provided")
    jobs = db.query(Job).filter(Job.id.in_(req.ids)).all()
    is_admin = user.role.value in ("admin", "super_admin")
    available: list[Job] = []
    for j in jobs:
        if j.user_id != user.id and not is_admin:
            continue
        if j.status != JobStatus.done:
            continue
        if not Path(j.dst_path).exists():
            continue
        available.append(j)
    if not available:
        raise HTTPException(status_code=404, detail="None of the selected jobs are available for download.")

    import io
    import os
    import zipfile
    buf = io.BytesIO()
    used: set[str] = set()
    with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        for j in available:
            # Disambiguate name collisions so two "out.png" files don't
            # silently overwrite each other in the zip.
            name = j.dst_filename or Path(j.dst_path).name
            if name in used:
                stem, ext = os.path.splitext(name)
                i = 1
                while f"{stem} ({i}){ext}" in used:
                    i += 1
                name = f"{stem} ({i}){ext}"
            used.add(name)
            zf.write(j.dst_path, arcname=name)
    buf.seek(0)
    audit.log(db, user.id, "convert_download_zip", metadata={"count": len(available)})
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="vitriol-conversions.zip"'},
    )


@router.get("/jobs/{job_id}/result")
def download_result(job_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    job: Optional[Job] = db.query(Job).get(job_id)
    if job is None or (job.user_id != user.id and user.role.value not in ("admin", "super_admin")):
        raise HTTPException(status_code=404, detail="Job not found")
    if job.status != JobStatus.done:
        raise HTTPException(status_code=409, detail=f"Job is {job.status.value}")
    p = Path(job.dst_path)
    if not p.exists():
        raise HTTPException(status_code=410, detail="Output file no longer available")
    return FileResponse(p, filename=job.dst_filename)
