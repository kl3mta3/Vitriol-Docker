"""Row-by-row data migration from the *currently active* DB into a
target described by a :class:`DbProvider` row.

Design notes:

* Source = whatever engine the app is running on right now (``web.db.engine``).
  We **only read** from it — the migration never modifies the live DB,
  so if the operator interrupts (or the target dies mid-way) the source
  is untouched and the app keeps working.
* Target = a fresh :class:`Engine` built from the provider's resolved
  URL. We disposable it on completion.
* Refusal policy: if any table on the target already has rows, the
  migration refuses before copying anything. Cleaner UX than mixing
  partial data; the operator's path to "re-migrate" is to drop +
  re-init the target.
* FK ordering: we walk ``Base.metadata.sorted_tables`` which gives
  parents before children, so foreign keys hold during inserts.
  Postgres has ``session_replication_role = replica`` temporarily set
  so partial-batch failures don't cascade across the FK graph; SQLite
  doesn't enforce FKs by default so it's a no-op.
* Progress is reported via the module-level :data:`_state` dict that
  the routes layer reads through :func:`current_status`. Only one
  migration may run at a time (enforced by :func:`start` checking
  ``_state["state"]``).
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime
from typing import Optional

from sqlalchemy import create_engine, select, func
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

from ..db import Base, engine as _source_engine
from ..models import DbProvider

_log = logging.getLogger(__name__)


# Single-flight state. Only one migration runs at a time per process —
# realistic for a single-container deployment, and rejecting overlapping
# migrations is far less work than orchestrating concurrent copies.
_state: dict = {
    "state": "idle",       # idle | running | done | failed
    "provider_id": None,
    "started_at": None,
    "finished_at": None,
    "current_table": None,
    "tables_done": 0,
    "tables_total": 0,
    "rows_copied": 0,
    "rows_total": 0,
    "per_table": {},       # name -> {"copied": int, "total": int}
    "error": None,
}
_state_lock = threading.Lock()


def current_status() -> dict:
    with _state_lock:
        s = dict(_state)
        s["percent"] = _compute_percent(s)
        s["per_table"] = dict(_state["per_table"])
        return s


def _compute_percent(s: dict) -> float:
    if s["rows_total"] <= 0:
        return 100.0 if s["state"] == "done" else 0.0
    return round(100.0 * s["rows_copied"] / s["rows_total"], 2)


def _set(**fields) -> None:
    with _state_lock:
        _state.update(fields)


def _reset() -> None:
    with _state_lock:
        _state.update({
            "state": "running",
            "started_at": datetime.utcnow(),
            "finished_at": None,
            "current_table": None,
            "tables_done": 0,
            "tables_total": 0,
            "rows_copied": 0,
            "rows_total": 0,
            "per_table": {},
            "error": None,
        })


def is_running() -> bool:
    with _state_lock:
        return _state["state"] == "running"


def start(provider_id: int, target_url: str) -> None:
    """Spawn the migration in a background thread. Caller is the
    `/migrate` route — it returns immediately and the UI polls
    ``/migrate/status`` for progress."""
    if is_running():
        raise RuntimeError("A migration is already running; wait for it to finish.")
    _reset()
    _set(provider_id=provider_id)
    t = threading.Thread(target=_run, args=(provider_id, target_url), daemon=True, name="db-migrate")
    t.start()


def _run(provider_id: int, target_url: str) -> None:
    target_engine: Optional[Engine] = None
    try:
        target_engine = create_engine(target_url, future=True)
        with target_engine.connect() as probe:
            probe.execute(select(1)) if hasattr(probe, "execute") else None  # smoke test

        # Empty-target precheck. We count rows on each table in the
        # target before copying; if any have rows, refuse so we don't
        # produce a mixed-data result the operator can't reason about.
        non_empty: list[str] = []
        with target_engine.connect() as conn:
            for tbl in Base.metadata.sorted_tables:
                try:
                    n = conn.execute(select(func.count()).select_from(tbl)).scalar() or 0
                    if n > 0:
                        non_empty.append(f"{tbl.name} ({n})")
                except SQLAlchemyError:
                    # Table not present on target = nothing to refuse
                    # (we'll skip it during copy with the same check).
                    pass
        if non_empty:
            raise RuntimeError(
                "Target has existing rows in: " + ", ".join(non_empty) +
                ". Clear it (or drop + re-initialize the database) and try again."
            )

        # Phase 1: total-row estimate so the UI can show a meaningful %.
        per_table: dict[str, dict] = {}
        total_rows = 0
        with _source_engine.connect() as conn:
            for tbl in Base.metadata.sorted_tables:
                try:
                    n = conn.execute(select(func.count()).select_from(tbl)).scalar() or 0
                except SQLAlchemyError:
                    n = 0
                per_table[tbl.name] = {"copied": 0, "total": int(n)}
                total_rows += int(n)
        _set(
            tables_total=len(Base.metadata.sorted_tables),
            rows_total=total_rows,
            per_table=per_table,
        )

        # Phase 2: copy. Postgres gets FK-constraint suppression for
        # the whole pass; SQLite doesn't need it (FKs off by default).
        dialect = target_engine.dialect.name
        with target_engine.begin() as tx:
            if dialect == "postgresql":
                tx.exec_driver_sql("SET session_replication_role = replica")
            for idx, tbl in enumerate(Base.metadata.sorted_tables):
                _set(current_table=tbl.name)
                _copy_table(tbl, tx)
                with _state_lock:
                    _state["tables_done"] = idx + 1
            if dialect == "postgresql":
                tx.exec_driver_sql("SET session_replication_role = DEFAULT")

        _set(state="done", finished_at=datetime.utcnow(), current_table=None)
        _stamp_provider(provider_id, ok=True, status="ok")
    except Exception as e:
        msg = f"{type(e).__name__}: {e}"
        _log.exception("Migration failed for provider %s", provider_id)
        _set(state="failed", finished_at=datetime.utcnow(), error=msg)
        _stamp_provider(provider_id, ok=False, status=msg[:250])
    finally:
        if target_engine is not None:
            target_engine.dispose()


def _copy_table(tbl, tx) -> None:
    """Stream rows from source to target in batches. Chunked so a
    huge table doesn't blow up the worker's memory."""
    batch_size = 1000
    name = tbl.name
    with _source_engine.connect() as src:
        # Some tables may not exist on either side — skip silently.
        try:
            rs = src.execution_options(stream_results=True).execute(select(tbl))
        except SQLAlchemyError as e:
            _log.warning("migrate: skipping %s: %s", name, e)
            return
        buf: list[dict] = []
        copied = 0
        for row in rs:
            buf.append(row._mapping)   # SQLAlchemy 2.x row → mapping
            if len(buf) >= batch_size:
                tx.execute(tbl.insert(), [dict(r) for r in buf])
                copied += len(buf)
                buf.clear()
                _bump(name, copied)
        if buf:
            tx.execute(tbl.insert(), [dict(r) for r in buf])
            copied += len(buf)
            _bump(name, copied)


def _bump(table_name: str, copied: int) -> None:
    with _state_lock:
        entry = _state["per_table"].get(table_name)
        if entry is None:
            entry = {"copied": 0, "total": 0}
            _state["per_table"][table_name] = entry
        delta = copied - entry["copied"]
        if delta < 0:
            delta = 0
        entry["copied"] = copied
        _state["rows_copied"] += delta


def _stamp_provider(provider_id: int, *, ok: bool, status: str) -> None:
    """Write the migration outcome back onto the provider row so the
    badge in the admin UI survives a page reload."""
    from ..db import SessionLocal
    db = SessionLocal()
    try:
        p = db.query(DbProvider).get(provider_id)
        if p is None:
            return
        p.last_migrate_at = datetime.utcnow()
        p.last_migrate_status = ("ok" if ok else f"failed: {status}")[:250]
        db.commit()
    except Exception:
        _log.exception("Failed to stamp provider %s post-migration", provider_id)
    finally:
        db.close()
