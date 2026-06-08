"""Admin: live transmute pipeline visibility + controls.

Surface for the Active-transmutes admin tab. Two responsibilities:

  * GET /admin/transmutes/active — snapshot of running / queued / held jobs
    across all users, enriched with the bits the UI needs to render a row
    (username, src/dst ext, stone, queued-at). Cheap enough to poll.
  * POST /admin/transmutes/{job_id}/(skip|pause|resume|stop) — push-button
    controls per row. skip/pause/resume only act on queued/held; stop
    works on both queued and running (running goes through the
    cancellation-token path the user-facing cancel uses).
  * GET /admin/transmutes/stats?window=... — completed-transmute rollup
    for the header card. Counts done jobs in [now - window, now], split
    by stone vs non-stone. Sourced from the Job table directly — no
    separate event log needed since Job already carries user_id +
    created_at + stone.
"""
from __future__ import annotations
from datetime import datetime, timedelta
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..auth.permissions import has_capability, CAN_VIEW_ACTIVE_TRANSMUTES
from ..deps import get_current_user, get_db
from ..models import Job, JobStatus, User
from ..services import audit
from ..services import conversion as conv_svc

router = APIRouter(prefix="/admin/transmutes", tags=["admin-transmutes"])


def _require_view(user: User = Depends(get_current_user)) -> User:
    if not has_capability(user, CAN_VIEW_ACTIVE_TRANSMUTES):
        raise HTTPException(status_code=403, detail="Missing capability: view_active_transmutes")
    return user


class ActiveItem(BaseModel):
    job_id: int
    user_id: int
    username: str
    state: Literal["running", "queued", "held"]
    position: Optional[int] = None
    src_ext: str
    dst_ext: str
    stone: bool
    progress: int
    created_at: datetime


@router.get("/active", response_model=list[ActiveItem])
def list_active_transmutes(
    _actor: User = Depends(_require_view),
    db: Session = Depends(get_db),
):
    snap = conv_svc.list_active()
    if not snap:
        return []
    job_ids = [s["job_id"] for s in snap]
    rows = db.query(Job).filter(Job.id.in_(job_ids)).all()
    by_id = {r.id: r for r in rows}
    user_ids = {r.user_id for r in rows}
    users = {u.id: u for u in db.query(User).filter(User.id.in_(user_ids)).all()}
    out: list[ActiveItem] = []
    for s in snap:
        job = by_id.get(s["job_id"])
        if job is None:
            # Row vanished between snapshot and DB read (job finished mid-poll).
            continue
        u = users.get(job.user_id)
        out.append(ActiveItem(
            job_id=job.id,
            user_id=job.user_id,
            username=u.username if u else f"user#{job.user_id}",
            state=s["state"],
            position=s.get("position"),
            src_ext=job.src_ext,
            dst_ext=job.dst_ext,
            stone=bool(job.stone),
            progress=int(job.progress or 0),
            created_at=job.created_at,
        ))
    # Stable display order: running first, then queued (by position), then held.
    _order = {"running": 0, "queued": 1, "held": 2}
    out.sort(key=lambda x: (_order[x.state], x.position if x.position is not None else 0, x.job_id))
    return out


class MessageResponse(BaseModel):
    ok: bool
    detail: str


def _fail_queued_job(db: Session, job_id: int, reason: str) -> None:
    """Flip a queued/held job row to `cancelled` and publish the WS event.
    Used by stop() when the job hasn't started yet — there's no running
    worker that would do it for us."""
    job = db.query(Job).get(job_id)
    if job is None or job.status not in (JobStatus.queued,):
        # If it's already running, the cancellation token publishes; if it's
        # already terminal there's nothing to do.
        return
    job.status = JobStatus.cancelled
    job.error = reason
    job.finished_at = datetime.utcnow()
    db.commit()
    conv_svc._publish(job_id, {"type": "cancelled", "error": reason})


@router.post("/{job_id}/skip", response_model=MessageResponse)
def skip_transmute(
    job_id: int,
    actor: User = Depends(_require_view),
    db: Session = Depends(get_db),
):
    if not conv_svc.skip(job_id):
        raise HTTPException(status_code=409, detail="Job is not queued (already running, held, or finished).")
    audit.log(db, actor.id, "transmute_skip", target_user_id=None, metadata={"job_id": job_id})
    return MessageResponse(ok=True, detail="Moved to end of queue.")


@router.post("/{job_id}/pause", response_model=MessageResponse)
def pause_transmute(
    job_id: int,
    actor: User = Depends(_require_view),
    db: Session = Depends(get_db),
):
    if not conv_svc.pause(job_id):
        raise HTTPException(status_code=409, detail="Job is not queued (running jobs can't be paused).")
    audit.log(db, actor.id, "transmute_pause", target_user_id=None, metadata={"job_id": job_id})
    return MessageResponse(ok=True, detail="Held.")


@router.post("/{job_id}/resume", response_model=MessageResponse)
def resume_transmute(
    job_id: int,
    actor: User = Depends(_require_view),
    db: Session = Depends(get_db),
):
    if not conv_svc.resume(job_id):
        raise HTTPException(status_code=409, detail="Job is not held.")
    audit.log(db, actor.id, "transmute_resume", target_user_id=None, metadata={"job_id": job_id})
    return MessageResponse(ok=True, detail="Resumed.")


@router.post("/{job_id}/stop", response_model=MessageResponse)
def stop_transmute(
    job_id: int,
    actor: User = Depends(_require_view),
    db: Session = Depends(get_db),
):
    # cancel() handles both paths: running (token) and queued/held (drop).
    # For the queued/held path it doesn't touch the DB — do that here so
    # the user's WebSocket sees a terminal event and the row reflects it.
    job = db.query(Job).get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found.")
    was_pre_run = job.status == JobStatus.queued
    if not conv_svc.cancel(job_id):
        raise HTTPException(status_code=409, detail="Job is not active.")
    if was_pre_run:
        _fail_queued_job(db, job_id, "Stopped by administrator.")
    audit.log(db, actor.id, "transmute_stop", target_user_id=job.user_id, metadata={"job_id": job_id})
    return MessageResponse(ok=True, detail="Stopped.")


# ---------------------------------------------------------------- stats

_WINDOWS: dict[str, Optional[timedelta]] = {
    "today": timedelta(days=1),
    "weekly": timedelta(days=7),
    "monthly": timedelta(days=30),
    "3mo": timedelta(days=90),
    "6mo": timedelta(days=180),
    "yearly": timedelta(days=365),
    "all": None,
}


class StatsOut(BaseModel):
    window: str
    since: Optional[datetime]
    total: int
    stone: int
    non_stone: int


@router.get("/stats", response_model=StatsOut)
def transmute_stats(
    window: str = "today",
    _actor: User = Depends(_require_view),
    db: Session = Depends(get_db),
):
    if window not in _WINDOWS:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown window. Choose from: {', '.join(_WINDOWS)}",
        )
    delta = _WINDOWS[window]
    since = (datetime.utcnow() - delta) if delta is not None else None
    q = db.query(Job).filter(Job.status == JobStatus.done)
    if since is not None:
        q = q.filter(Job.created_at >= since)
    rows = q.all()
    total = len(rows)
    stone = sum(1 for r in rows if r.stone)
    return StatsOut(
        window=window,
        since=since,
        total=total,
        stone=stone,
        non_stone=total - stone,
    )
