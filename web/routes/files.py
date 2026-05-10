"""Files API — list / delete converted outputs, gated on the new
view_*_files / download_others_files / delete_others_files capabilities.

The Files tab in the UI talks exclusively to this namespace. We re-use
the existing /jobs/{id}/result + /jobs/download-zip endpoints for the
actual file streaming — those already handle access checks for the
'own files' case correctly.
"""
from __future__ import annotations
import os
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from ..auth.permissions import (
    has_capability,
    CAN_VIEW_OWN_FILES, CAN_VIEW_OTHERS_FILES,
    CAN_DOWNLOAD_OTHERS_FILES, CAN_DELETE_OTHERS_FILES,
)
from ..deps import get_current_user, get_db
from ..models import Job, JobStatus, User
from ..services import audit

router = APIRouter(prefix="/files", tags=["files"])


class FileEntry(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int                      # job_id
    user_id: int
    owner_username: str
    src_filename: str
    src_ext: str
    dst_ext: str
    dst_filename: str
    bytes_out: Optional[int]
    finished_at: Optional[datetime]
    created_at: datetime
    is_own: bool                 # rendered as "you" in the UI


def _gate(actor: User) -> None:
    if not has_capability(actor, CAN_VIEW_OWN_FILES):
        raise HTTPException(status_code=403, detail="Files tab is not enabled for your role.")


@router.get("", response_model=List[FileEntry])
def list_files(
    actor: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    limit: int = 500,
):
    """Return done jobs whose output file is still on disk.

    Caller sees own files always; others' files only if the
    `view_others_files` capability is granted.
    """
    _gate(actor)
    sees_others = has_capability(actor, CAN_VIEW_OTHERS_FILES)

    q = db.query(Job, User).join(User, Job.user_id == User.id).filter(Job.status == JobStatus.done)
    if not sees_others:
        q = q.filter(Job.user_id == actor.id)
    q = q.order_by(Job.finished_at.desc().nullslast(), Job.id.desc()).limit(min(max(1, limit), 2000))

    out: list[FileEntry] = []
    for job, owner in q.all():
        # Skip rows whose file was already cleaned up — UI is "what's
        # currently retrievable", not full job history.
        try:
            if not Path(job.dst_path).exists():
                continue
            if job.bytes_out is None:
                # Backfill on read for older rows.
                job.bytes_out = Path(job.dst_path).stat().st_size
        except OSError:
            continue
        out.append(FileEntry(
            id=job.id,
            user_id=job.user_id,
            owner_username=owner.username,
            src_filename=job.src_filename,
            src_ext=job.src_ext,
            dst_ext=job.dst_ext,
            dst_filename=job.dst_filename,
            bytes_out=job.bytes_out,
            finished_at=job.finished_at,
            created_at=job.created_at,
            is_own=(job.user_id == actor.id),
        ))
    return out


@router.delete("/{job_id}")
def delete_file(
    job_id: int,
    actor: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _gate(actor)
    job: Optional[Job] = db.query(Job).get(job_id)
    if job is None or job.status != JobStatus.done:
        raise HTTPException(status_code=404, detail="File not found")
    is_own = job.user_id == actor.id
    if not is_own and not has_capability(actor, CAN_DELETE_OTHERS_FILES):
        # 404 (not 403) so an admin without delete-others can't probe
        # for the existence of someone else's files.
        raise HTTPException(status_code=404, detail="File not found")

    try:
        p = Path(job.dst_path)
        if p.exists():
            p.unlink()
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"Could not delete file: {e}")

    audit.log(db, actor.id, "file_delete", target_user_id=job.user_id, metadata={"job_id": job_id})
    return {"message": "File deleted."}


def can_access_file(actor: User, job: Job, *, action: str) -> bool:
    """Helper used by the existing /jobs/{id}/result + /jobs/download-zip
    routes to authorise file streaming. action is 'download' or 'delete'."""
    if job.user_id == actor.id:
        return True
    if action == "download":
        return has_capability(actor, CAN_DOWNLOAD_OTHERS_FILES)
    if action == "delete":
        return has_capability(actor, CAN_DELETE_OTHERS_FILES)
    return False
