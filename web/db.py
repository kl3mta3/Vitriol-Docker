"""SQLAlchemy engine + session factory."""
from __future__ import annotations
from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker, Session

from .config import get_settings


_settings = get_settings()
_url = _settings.database_url_resolved()

# SQLite needs check_same_thread=False because we use the thread-pool
# executor for conversions; engine reads happen on worker threads.
_connect_args = {"check_same_thread": False} if _url.startswith("sqlite") else {}

engine = create_engine(_url, connect_args=_connect_args, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
Base = declarative_base()


def get_db():
    db: Session = SessionLocal()
    try:
        yield db
    finally:
        db.close()
