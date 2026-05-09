"""Suspension duration parsing."""
from __future__ import annotations
from datetime import datetime, timedelta

DURATIONS = {
    "24h": timedelta(hours=24),
    "3d": timedelta(days=3),
    "7d": timedelta(days=7),
    "30d": timedelta(days=30),
}


def expiry_for(duration: str) -> datetime:
    delta = DURATIONS.get(duration)
    if delta is None:
        raise ValueError(f"Unknown duration {duration!r}; expected one of {list(DURATIONS)}")
    return datetime.utcnow() + delta
