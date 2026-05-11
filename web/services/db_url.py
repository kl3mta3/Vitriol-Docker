"""Build SQLAlchemy connection URLs from DbProvider rows.

The admin UI lets operators describe a target database via template +
fields (host / port / user / db_name / ...) and we centralize the
construction of the resulting ``dialect+driver://...`` URL here so the
test, create-DB, initialize-schema, and "make active" flows all agree
on dialect quirks (psycopg vs psycopg2, port defaults, SSL params).

Nothing here connects to a database — building a URL is pure string
work. The routes layer is what actually opens connections via
``create_engine``.
"""
from __future__ import annotations

from typing import Optional
from urllib.parse import quote_plus


# Dialect+driver strings. Picking the right driver per kind keeps the
# operator from having to install N drivers — only the ones they
# actually use. The driver names match the wheels we'd add to
# requirements.txt if/when the operator goes live on a new backend.
_DIALECT: dict[str, str] = {
    "sqlite":   "sqlite",
    "postgres": "postgresql+psycopg2",
    "mysql":    "mysql+pymysql",
    "mariadb":  "mysql+pymysql",
    "mssql":    "mssql+pyodbc",
}

_DEFAULT_PORT: dict[str, int] = {
    "postgres": 5432,
    "mysql":    3306,
    "mariadb":  3306,
    "mssql":    1433,
}


def known_kinds() -> tuple[str, ...]:
    return tuple(_DIALECT.keys())


def build_url(kind: str, config: dict, secret: Optional[str]) -> str:
    """Return a SQLAlchemy connection URL for the given kind + config.

    ``config`` is the freeform per-kind dict from DbProvider.config_json.
    ``secret`` is the cleartext password (already decrypted by the caller).
    """
    kind = (kind or "").lower()
    if kind not in _DIALECT:
        raise ValueError(f"Unknown DB kind: {kind!r}")

    if kind == "sqlite":
        # The operator can either name a file under /data, or pass an
        # absolute path. SQLAlchemy wants three slashes for relative,
        # four for absolute: sqlite:///rel.db, sqlite:////abs/path.db.
        path = (config.get("db_path") or "vitriol-target.db").strip()
        if path.startswith("/"):
            return f"sqlite://{path}"
        return f"sqlite:///{path}"

    host = (config.get("host") or "").strip()
    if not host:
        raise ValueError(f"{kind}: host is required")
    port = int(config.get("port") or _DEFAULT_PORT.get(kind, 0)) or _DEFAULT_PORT[kind]
    user = (config.get("user") or "").strip()
    db_name = (config.get("db_name") or "").strip()

    auth = ""
    if user:
        if secret:
            auth = f"{quote_plus(user)}:{quote_plus(secret)}@"
        else:
            auth = f"{quote_plus(user)}@"

    url = f"{_DIALECT[kind]}://{auth}{host}:{port}/{db_name}"

    # Optional query string (sslmode for Postgres, charset for MySQL, etc.).
    extras: list[str] = []
    sslmode = (config.get("sslmode") or "").strip()
    if sslmode and kind == "postgres":
        extras.append(f"sslmode={quote_plus(sslmode)}")
    extra_args = config.get("extra_args")
    if isinstance(extra_args, dict):
        for k, v in extra_args.items():
            if k and v is not None:
                extras.append(f"{quote_plus(str(k))}={quote_plus(str(v))}")
    if extras:
        url = url + "?" + "&".join(extras)
    return url


def server_url_for_create_db(kind: str, config: dict, secret: Optional[str]) -> str:
    """Build a URL pointing at the *server-level* database (e.g.
    ``postgres``, ``mysql``) so we can issue ``CREATE DATABASE`` from
    outside the target. SQLite doesn't need this — the file is the DB."""
    kind = (kind or "").lower()
    if kind == "sqlite":
        return build_url(kind, config, secret)
    server_db = {
        "postgres": "postgres",
        "mysql":    "mysql",
        "mariadb":  "mysql",
        "mssql":    "master",
    }.get(kind)
    if server_db is None:
        raise ValueError(f"create-db not supported for kind={kind!r}")
    return build_url(kind, {**config, "db_name": server_db}, secret)
