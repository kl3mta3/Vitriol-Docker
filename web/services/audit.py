"""Audit log helper."""
from __future__ import annotations
import json
from typing import Any, Optional

from sqlalchemy.orm import Session

from ..models import AuditLog


def log(
    db: Session,
    actor_id: Optional[int],
    action: str,
    target_user_id: Optional[int] = None,
    metadata: Optional[dict[str, Any]] = None,
) -> None:
    row = AuditLog(
        actor_user_id=actor_id,
        action=action,
        target_user_id=target_user_id,
        metadata_json=json.dumps(metadata) if metadata else None,
    )
    db.add(row)
    db.commit()
