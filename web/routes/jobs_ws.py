"""WebSocket endpoint for live job progress."""
from __future__ import annotations
import asyncio
from typing import Optional

from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect, status
from sqlalchemy.orm import Session

from ..auth.jwt import decode_access_token
from ..auth.api_keys import find_active, looks_like_api_key
from ..deps import get_db
from ..models import Job, User
from ..services import conversion as conv_svc

router = APIRouter()


def _ws_user(db: Session, token: Optional[str]) -> Optional[User]:
    if not token:
        return None
    if looks_like_api_key(token):
        row = find_active(db, token)
        return db.query(User).get(row.user_id) if row else None
    payload = decode_access_token(token)
    if not payload:
        return None
    try:
        return db.query(User).get(int(payload["sub"]))
    except Exception:
        return None


@router.websocket("/ws/jobs/{job_id}")
async def ws_job(websocket: WebSocket, job_id: int, db: Session = Depends(get_db)):
    # Token comes via query string OR cookie (browser case).
    token = websocket.query_params.get("token") or websocket.cookies.get("vitriol_access")
    user = _ws_user(db, token)
    if user is None:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    job: Optional[Job] = db.query(Job).get(job_id)
    if job is None or (job.user_id != user.id and user.role.value not in ("admin", "super_admin")):
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    await websocket.accept()
    # Subscribe BEFORE reading the snapshot. The job row from line 42 was
    # loaded when the route handler created it (status=queued); by the time
    # the WS opens, the dispatcher may have already run the job to completion
    # and published every event. Subscribing first guarantees we catch any
    # events that fire from this point on; db.refresh() then gives us the
    # latest committed status (running/done/failed) instead of the stale
    # "queued" cached on the row. Without this ordering, a fast job races
    # past the WS and the UI sticks on "queued" forever.
    q = conv_svc.subscribe(job_id)
    try:
        db.refresh(job)
        await websocket.send_json({
            "type": "snapshot",
            "status": job.status.value,
            "progress": job.progress,
        })
        # If the job already reached a terminal state before we subscribed,
        # forward anything buffered (the done/failed/cancelled event carries
        # warnings/error the UI wants) and close. Without this, the client
        # would sit on a dead socket waiting for an event that already fired.
        if job.status.value in ("done", "failed", "cancelled"):
            while not q.empty():
                await websocket.send_json(q.get_nowait())
            return
        while True:
            try:
                event = await asyncio.wait_for(q.get(), timeout=30)
            except asyncio.TimeoutError:
                await websocket.send_json({"type": "ping"})
                continue
            await websocket.send_json(event)
            if event.get("type") in ("done", "failed", "cancelled"):
                break
    except WebSocketDisconnect:
        pass
    finally:
        conv_svc.unsubscribe(job_id, q)
        try:
            await websocket.close()
        except Exception:
            pass
