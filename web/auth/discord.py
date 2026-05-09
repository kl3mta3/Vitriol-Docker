"""Fire-and-forget Discord webhook notifications."""
from __future__ import annotations
import httpx
from typing import Optional

from sqlalchemy.orm import Session

from ..models import ServerSettings


async def notify(db: Session, content: str) -> bool:
    s: Optional[ServerSettings] = db.query(ServerSettings).get(1)
    if s is None or not s.discord_webhook_url:
        return False
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(s.discord_webhook_url, json={"content": content})
            return r.status_code in (200, 204)
    except Exception:
        return False
