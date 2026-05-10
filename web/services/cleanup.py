"""Background cleanup of converted output files.

Runs periodically from the lifespan task. Reads the per-role retention
policy out of `ServerSettings.output_retention_json` and prunes files
that exceed either the time bound (max_age) or the count bound
(max_files) for the owner's role.

The "delete on download" path is handled separately in
`web/routes/convert.py::download_result`; this service is only the
*passive* time/count-based cleanup.
"""
from __future__ import annotations
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from sqlalchemy.orm import Session

from ..models import Job, JobStatus, Role, ServerSettings, User

_log = logging.getLogger(__name__)


_DEFAULT = {
    "super_admin": {"max_files": 0, "max_age": 0, "age_unit": "days", "delete_on_download": False},
    "admin":       {"max_files": 0, "max_age": 30, "age_unit": "days", "delete_on_download": False},
    "user":        {"max_files": 20, "max_age": 24, "age_unit": "hours", "delete_on_download": False},
}

_UNIT_TO_TIMEDELTA = {
    "minutes": lambda n: timedelta(minutes=n),
    "hours":   lambda n: timedelta(hours=n),
    "days":    lambda n: timedelta(days=n),
}


def _load_policy(s: Optional[ServerSettings]) -> dict:
    if s is None or not s.output_retention_json:
        return dict(_DEFAULT)
    try:
        parsed = json.loads(s.output_retention_json)
    except (json.JSONDecodeError, TypeError):
        return dict(_DEFAULT)
    out = {k: dict(v) for k, v in _DEFAULT.items()}
    for role, cfg in parsed.items():
        if role in out and isinstance(cfg, dict):
            out[role].update(cfg)
    return out


def _role_key(user: User) -> str:
    role = user.role.value if hasattr(user.role, "value") else str(user.role)
    return role if role in _DEFAULT else "user"


def _delete_file(job: Job) -> bool:
    try:
        p = Path(job.dst_path)
        if p.exists():
            p.unlink()
            return True
    except OSError as e:
        _log.warning("cleanup: failed to delete %s: %s", job.dst_path, e)
    return False


def run_once(db: Session) -> dict:
    """One sweep: age-based first, then count-based per user. Returns a
    small summary dict for logging / metrics."""
    s = db.query(ServerSettings).get(1)
    policy = _load_policy(s)
    now = datetime.utcnow()
    age_deleted = 0
    count_deleted = 0

    # 1. Age-based pass — every done job whose owner role has max_age>0.
    done_jobs = (
        db.query(Job, User)
        .join(User, Job.user_id == User.id)
        .filter(Job.status == JobStatus.done)
        .all()
    )
    for job, owner in done_jobs:
        role = _role_key(owner)
        cfg = policy.get(role, _DEFAULT[role])
        max_age = int(cfg.get("max_age") or 0)
        if max_age <= 0:
            continue
        unit = cfg.get("age_unit") or "days"
        delta_fn = _UNIT_TO_TIMEDELTA.get(unit, _UNIT_TO_TIMEDELTA["days"])
        anchor = job.finished_at or job.created_at
        if anchor is None:
            continue
        if (now - anchor) <= delta_fn(max_age):
            continue
        if _delete_file(job):
            age_deleted += 1

    # 2. Count-based pass — for each user, keep the N newest done jobs
    #    whose file is still on disk; delete the rest's files. Skip when
    #    max_files == 0 (= unlimited).
    by_user: dict[int, tuple[User, list[Job]]] = {}
    for job, owner in done_jobs:
        slot = by_user.setdefault(owner.id, (owner, []))[1]
        slot.append(job)

    for uid, (owner, jobs) in by_user.items():
        cfg = policy.get(_role_key(owner), _DEFAULT[_role_key(owner)])
        max_files = int(cfg.get("max_files") or 0)
        if max_files <= 0:
            continue
        # Filter to jobs whose file is still present, sorted newest first.
        live = [j for j in jobs if _file_exists(j)]
        live.sort(key=lambda j: (j.finished_at or j.created_at or datetime.min), reverse=True)
        for old in live[max_files:]:
            if _delete_file(old):
                count_deleted += 1

    if age_deleted or count_deleted:
        _log.info("cleanup: aged=%d count-pruned=%d", age_deleted, count_deleted)

    return {"aged": age_deleted, "count_pruned": count_deleted}


def _file_exists(job: Job) -> bool:
    try:
        return Path(job.dst_path).exists()
    except OSError:
        return False
