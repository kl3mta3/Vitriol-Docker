"""Per-user daily conversion quotas + per-user rate limiting."""
from __future__ import annotations
import time
from collections import defaultdict, deque
from datetime import date, datetime
from threading import Lock
from typing import Optional

from sqlalchemy.orm import Session

from ..models import ConversionCounter, Role, ServerSettings, User


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
