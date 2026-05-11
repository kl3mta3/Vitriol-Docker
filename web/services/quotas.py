"""Per-user daily conversion quotas + per-user rate limiting."""
from __future__ import annotations
import time
from collections import defaultdict, deque
from datetime import date, datetime
from threading import Lock
from typing import Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from ..models import ConversionCounter, Job, JobStatus, Role, ServerSettings, User


def daily_limit_for(user: User, settings: ServerSettings) -> Optional[int]:
    if user.daily_conversion_limit is not None:
        return user.daily_conversion_limit
    if user.role == Role.admin:
        return settings.default_admin_daily_limit
    if user.role == Role.user:
        return settings.default_user_daily_limit
    if user.role == Role.super_admin:
        return None  # unlimited
    return 0


def max_file_size_for(user: User, settings: ServerSettings) -> Optional[int]:
    """Three-tier resolution for max upload size, most specific wins:

    1. ``user.max_file_size_bytes`` (per-user override set by admin
       with the ``set_user_file_size_cap`` capability)
    2. ``user.custom_role.max_file_size_bytes`` (per-role override)
    3. ``settings.max_file_size_bytes`` (server-wide default)

    Super admin is always unlimited (returns ``None``) — the cap
    exists to protect non-privileged users from accidentally uploading
    a 50 GB file, not to gate the operator's own diagnostic work.

    Returns ``None`` to mean "no limit"; otherwise an integer byte count
    that the caller compares against the incoming upload size.
    """
    if user.role == Role.super_admin:
        return None
    if user.max_file_size_bytes is not None:
        return user.max_file_size_bytes
    if user.custom_role is not None and user.custom_role.max_file_size_bytes is not None:
        return user.custom_role.max_file_size_bytes
    return settings.max_file_size_bytes if settings else None


def max_output_size_for(user: User, settings: ServerSettings) -> Optional[int]:
    """Three-tier resolution for max OUTPUT size (post-conversion file).

    Mirrors :func:`max_file_size_for` exactly but for the engine's
    output file. Distinct cap because Stone-mode cross-format
    conversions can balloon a small input into a much larger output
    (PNG→WAV ~3-5x, video containers 2-10x), so capping input alone
    leaves disk + bandwidth exposed.

    Super admin: unlimited (returns ``None``).
    Server / role default 0: unlimited (returns ``None``).
    """
    if user.role == Role.super_admin:
        return None
    if user.max_output_size_bytes is not None:
        return user.max_output_size_bytes if user.max_output_size_bytes > 0 else None
    if user.custom_role is not None and user.custom_role.max_output_size_bytes is not None:
        v = user.custom_role.max_output_size_bytes
        return v if v > 0 else None
    if settings is not None:
        v = int(getattr(settings, "max_output_size_bytes", 0) or 0)
        return v if v > 0 else None
    return None


def max_storage_for(user: User, settings: ServerSettings) -> Optional[int]:
    """Three-tier resolution for total-storage quota per user (sum of
    bytes_out across `done` jobs whose files still exist).

    Super admin: unlimited.
    Server / role default 0: unlimited.
    """
    if user.role == Role.super_admin:
        return None
    if user.max_storage_bytes is not None:
        return user.max_storage_bytes if user.max_storage_bytes > 0 else None
    if user.custom_role is not None and user.custom_role.max_storage_bytes is not None:
        v = user.custom_role.max_storage_bytes
        return v if v > 0 else None
    if settings is not None:
        v = int(getattr(settings, "max_storage_bytes", 0) or 0)
        return v if v > 0 else None
    return None


def current_storage_used(db: Session, user_id: int) -> int:
    """Sum bytes_out for the user's `done` jobs.

    Approximation — we count bytes_out even when the file on disk has
    been retention-cleaned. That's intentional: keeping the SUM cheap
    matters more than perfect accuracy on a rare race window. The
    retention sweep + delete-on-download flows leave the row but null
    out the file; a more precise version would join against a "file
    still exists" check, which is too expensive on every upload.

    For a more accurate value, run the regular cleanup sweep first
    (it implicitly reconciles by deleting orphan rows). 99% of the
    time the approximation is within a small percent of disk truth.
    """
    total = (
        db.query(func.coalesce(func.sum(Job.bytes_out), 0))
        .filter(Job.user_id == user_id)
        .filter(Job.status == JobStatus.done)
        .scalar()
    )
    return int(total or 0)


def rate_limit_for(user: User, settings: ServerSettings) -> int:
    if user.rate_limit_per_minute is not None:
        return user.rate_limit_per_minute
    if user.role == Role.admin:
        return settings.default_admin_rate_limit
    if user.role == Role.super_admin:
        return max(settings.default_admin_rate_limit, 240)
    return settings.default_user_rate_limit


def check_and_increment_daily(db: Session, user: User) -> tuple[bool, int, Optional[int]]:
    """Returns (allowed, current_count, limit). Increments only if allowed."""
    settings: Optional[ServerSettings] = db.query(ServerSettings).get(1)
    if settings is None:
        return True, 0, None
    limit = daily_limit_for(user, settings)
    today = date.today()
    counter = (
        db.query(ConversionCounter)
        .filter(ConversionCounter.user_id == user.id, ConversionCounter.date == today)
        .one_or_none()
    )
    current = counter.count if counter else 0
    if limit is not None and current >= limit:
        return False, current, limit
    if counter is None:
        counter = ConversionCounter(user_id=user.id, date=today, count=1)
        db.add(counter)
    else:
        counter.count = current + 1
    db.commit()
    return True, current + 1, limit


# --- In-memory per-user rate limiter (sliding 60s window) -------------

_rl_buckets: dict[int, deque] = defaultdict(deque)
_rl_lock = Lock()


def check_rate_limit(user: User, db: Session) -> bool:
    settings: Optional[ServerSettings] = db.query(ServerSettings).get(1)
    if settings is None:
        return True
    limit = rate_limit_for(user, settings)
    if limit <= 0:
        return False
    now = time.monotonic()
    cutoff = now - 60.0
    with _rl_lock:
        dq = _rl_buckets[user.id]
        while dq and dq[0] < cutoff:
            dq.popleft()
        if len(dq) >= limit:
            return False
        dq.append(now)
    return True
