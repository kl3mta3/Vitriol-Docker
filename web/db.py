"""SQLAlchemy engine + session factory."""
from __future__ import annotations
import logging
from pathlib import Path

from sqlalchemy import create_engine, event, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import declarative_base, sessionmaker, Session

from .config import get_settings

_log = logging.getLogger(__name__)


_settings = get_settings()


def _resolve_boot_url(cfg) -> str:
    """Pick the database URL at boot.

    Resolution order, highest priority first:
      1. ``/data/active_db.url`` — UI-written file. If present and the
         target is reachable, we use it. If unreachable, we log the
         failure and fall back to step 2 so the operator stays able
         to reach the app and clear the file.
      2. ``DATABASE_URL`` env var (via Pydantic settings).
      3. Built-in default: ``sqlite:///{data_dir}/vitriol.db``.

    Always-default URL (steps 2/3 only) is returned if step 1 isn't
    usable; step 1 never silently overwrites your live DB.
    """
    env_url = cfg.database_url_resolved()
    active_file = cfg.data_dir / "active_db.url"
    if not active_file.exists():
        return env_url
    try:
        candidate = active_file.read_text(encoding="utf-8").strip()
    except OSError as e:
        _log.warning("active_db.url unreadable (%s); using DATABASE_URL/SQLite default", e)
        return env_url
    if not candidate:
        return env_url
    # Probe the candidate URL before committing the rest of boot to it.
    # We dispose the test engine immediately — the real engine is
    # created below with the right connect_args.
    try:
        _probe = create_engine(candidate, future=True)
        with _probe.connect() as conn:
            conn.execute(text("SELECT 1"))
        _probe.dispose()
    except (SQLAlchemyError, OSError, Exception) as e:
        # Fallback path — DO NOT raise. Locking the operator out of the
        # admin UI because a saved Postgres is down is the worst
        # possible failure mode. Log loudly, drop back to whatever the
        # env var / default points at (usually SQLite, which we know
        # works because the operator was just using it).
        _log.error(
            "active_db.url target unreachable (%s: %s); falling back to %s. "
            "Delete /data/active_db.url to suppress this warning.",
            type(e).__name__, e, env_url,
        )
        return env_url
    _log.info("Using active_db.url override: %s", _redact_url(candidate))
    return candidate


def _redact_url(url: str) -> str:
    """Strip password from a SQLAlchemy URL for logging. Best-effort —
    if the URL doesn't match the expected user:pass@host shape we
    return the original (which is fine for SQLite paths)."""
    try:
        from urllib.parse import urlparse, urlunparse
        u = urlparse(url)
        if u.password:
            netloc = u.netloc.replace(":" + u.password + "@", ":***@")
            return urlunparse(u._replace(netloc=netloc))
    except Exception:
        pass
    return url


_url = _resolve_boot_url(_settings)

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
