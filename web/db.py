"""SQLAlchemy engine + session factory."""
from __future__ import annotations
from sqlalchemy import create_engine, event
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


# Enable WAL on SQLite. WAL allows concurrent readers + a single writer
# without blocking each other (the default DELETE journal serializes
# everything), which materially helps when the cleanup sweep or a long
# conversion is holding a write transaction while the UI is rendering.
# `synchronous=NORMAL` is the canonical companion: durable across app
# crashes but not power loss, which matches how every other piece of
# state in /data behaves (uploads, outputs, certs).
#
# Set per-connection so it kicks in even if the journal mode somehow
# reverted (some PaaS snapshot/restore flows can do this). No-op on
# Postgres / MySQL / etc.
if _url.startswith("sqlite"):
    @event.listens_for(engine, "connect")
    def _set_sqlite_pragmas(dbapi_conn, _connection_record):
        cur = dbapi_conn.cursor()
        try:
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA synchronous=NORMAL")
        finally:
            cur.close()


def get_db():
    db: Session = SessionLocal()
    try:
        yield db
    finally:
        db.close()
