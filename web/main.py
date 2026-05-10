"""FastAPI application factory + lifespan."""
from __future__ import annotations
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import inspect
from starlette.middleware.sessions import SessionMiddleware

from . import __version__
from .config import get_settings
from .db import Base, SessionLocal, engine
from .models import ServerSettings  # noqa: F401 — ensure metadata is populated
from .routes import auth as auth_routes
from .routes import convert as convert_routes
from .routes import jobs_ws
from .routes import me as me_routes
from .routes import files as files_routes
from .routes import oidc_providers as oidc_routes
from .routes import roles as roles_routes
from .routes import server as server_routes
from .routes import setup as setup_routes
from .routes import ui as ui_routes
from .routes import users as users_routes
from .services.bootstrap import apply_recovery_config, ensure_schema, ensure_server_settings, ensure_super_admin, migrate_legacy_oidc, super_admin_exists

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
_log = logging.getLogger("vitriol")


@asynccontextmanager
async def lifespan(app: FastAPI):
    cfg = get_settings()
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    # If Alembic hasn't run (e.g. dev mode), create tables on the fly.
    insp = inspect(engine)
    if not insp.has_table("users"):
        _log.warning("Tables missing — creating from SQLAlchemy metadata. "
                     "Run alembic upgrade head in production.")
    # create_all is idempotent — it only creates tables that don't exist
    # yet. Running it on every boot picks up newly-added tables (e.g.
    # custom_roles) without forcing the operator through alembic.
    Base.metadata.create_all(bind=engine)
    # Apply any column additions that postdate the create_all baseline.
    ensure_schema(engine)
    db = SessionLocal()
    try:
        ensure_server_settings(db)
        apply_recovery_config(db)
        ensure_super_admin(db)
        migrate_legacy_oidc(db)
    finally:
        db.close()
    _log.info("Vitriol web v%s started; data dir=%s", __version__, cfg.data_dir)

    # Background cert-pull scheduler. Wakes hourly, runs the pull if
    # auto_days is set and the last run is older than that. Stays a
    # no-op until the operator turns auto-renewal on.
    import asyncio
    from datetime import datetime, timedelta
    from .services.cert_pull import run as cert_pull_run, CertPullError

    async def _cert_pull_loop():
        while True:
            try:
                await asyncio.sleep(3600)
                db_ = SessionLocal()
                try:
                    s = db_.query(ServerSettings).get(1)
                    if s is None:
                        continue
                    days = int(s.ssl_cert_pull_auto_days or 0)
                    if days <= 0:
                        continue
                    last = s.ssl_cert_pull_last_run_at
                    due = last is None or last < (datetime.utcnow() - timedelta(days=days))
                    if not due:
                        continue
                    _log.info("Auto cert pull: %d-day interval; last run %s", days, last)
                    try:
                        msg = await cert_pull_run(db_)
                        _log.info("Auto cert pull ok: %s", msg)
                    except CertPullError as e:
                        _log.warning("Auto cert pull failed: %s", e)
                finally:
                    db_.close()
            except asyncio.CancelledError:
                raise
            except Exception:
                _log.exception("Cert-pull scheduler hiccup; continuing")

    cert_task = asyncio.create_task(_cert_pull_loop())

    # Output retention cleanup: every 30 minutes, prune by max_age and
    # max_files per the role policy in server settings. Cheap — a couple
    # of joined queries plus filesystem unlinks.
    from .services.cleanup import run_once as cleanup_once

    async def _cleanup_loop():
        while True:
            try:
                await asyncio.sleep(1800)  # 30 min
                db_ = SessionLocal()
                try:
                    # Run sync function on a thread so we don't block the
                    # event loop on filesystem unlinks for a big sweep.
                    await asyncio.to_thread(cleanup_once, db_)
                finally:
                    db_.close()
            except asyncio.CancelledError:
                raise
            except Exception:
                _log.exception("Output cleanup hiccup; continuing")

    cleanup_task = asyncio.create_task(_cleanup_loop())

    yield
    for t in (cert_task, cleanup_task):
        t.cancel()
        try:
            await t
        except (asyncio.CancelledError, Exception):
            pass
    _log.info("Vitriol web shutting down")


def create_app() -> FastAPI:
    cfg = get_settings()
    app = FastAPI(title="Vitriol", version=__version__, lifespan=lifespan)

    app.add_middleware(SessionMiddleware, secret_key=cfg.secret_key)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Paths that must remain reachable while setup is pending — anything
    # else gets redirected to /setup until the super admin row exists.
    _setup_allowlist_prefixes = (
        "/setup",
        "/api/v1/auth/setup",
        "/api/v1/health",
        "/static/",
        "/docs",      # FastAPI Swagger UI
        "/redoc",
        "/openapi.json",
        "/favicon.ico",
    )

    @app.middleware("http")
    async def _setup_gate(request: Request, call_next):
        path = request.url.path
        if any(path == p or path.startswith(p) for p in _setup_allowlist_prefixes):
            return await call_next(request)
        # Cheap DB peek — one row count.
        try:
            db = SessionLocal()
            needs_setup = not super_admin_exists(db)
            db.close()
        except Exception:
            needs_setup = False
        if needs_setup:
            from fastapi.responses import RedirectResponse, JSONResponse
            if path.startswith("/api/"):
                return JSONResponse(status_code=503, content={"detail": "Setup pending — open /setup in a browser."})
            return RedirectResponse(url="/setup")
        return await call_next(request)

    @app.middleware("http")
    async def _domain_lock(request: Request, call_next):
        # Read settings off the DB once per request — cheap on SQLite.
        try:
            db = SessionLocal()
            s = db.query(ServerSettings).get(1)
            allowed = s.allowed_origin if s else None
            db.close()
        except Exception:
            allowed = None
        if allowed:
            host = (request.headers.get("host") or "").split(":")[0]
            if host and host != allowed:
                return PlainTextResponse("Host not allowed", status_code=403)
        return await call_next(request)

    @app.exception_handler(404)
    async def _not_found(request: Request, exc):
        if request.url.path.startswith("/api/"):
            return JSONResponse(status_code=404, content={"detail": "Not found"})
        return PlainTextResponse("Not Found", status_code=404)

    # Static + UI
    app.mount("/static", StaticFiles(directory="web/static"), name="static")
    app.include_router(setup_routes.router)         # /setup HTML
    app.include_router(ui_routes.router)

    # API
    app.include_router(setup_routes.api_router, prefix="/api/v1")  # /api/v1/auth/setup
    app.include_router(auth_routes.router, prefix="/api/v1")
    app.include_router(me_routes.router, prefix="/api/v1")
    app.include_router(users_routes.router, prefix="/api/v1")
    app.include_router(roles_routes.router, prefix="/api/v1")
    app.include_router(server_routes.router, prefix="/api/v1")
    app.include_router(oidc_routes.router, prefix="/api/v1")
    app.include_router(convert_routes.router, prefix="/api/v1")
    app.include_router(files_routes.router, prefix="/api/v1")

    # WebSocket
    app.include_router(jobs_ws.router)

    @app.get("/api/v1/health")
    def _health():
        return {"status": "ok", "version": __version__}

    return app


app = create_app()
