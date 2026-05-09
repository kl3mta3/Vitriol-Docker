#!/usr/bin/env bash
# Vitriol container entrypoint.
#
#   1. Run Alembic migrations (idempotent).
#   2. Hand off to the CMD (uvicorn).
#
# The lifespan in web.main also creates tables from metadata if Alembic
# hasn't been run, so this script staying on the happy path doesn't gate
# startup on the DB schema.

set -euo pipefail

cd /app
mkdir -p "${VITRIOL_DATA_DIR:-/data}/uploads" \
         "${VITRIOL_DATA_DIR:-/data}/outputs" \
         "${VITRIOL_DATA_DIR:-/data}/certs" \
         "${VITRIOL_DATA_DIR:-/data}/logs"

if command -v alembic >/dev/null 2>&1; then
  echo "[entrypoint] running alembic upgrade head"
  alembic upgrade head || echo "[entrypoint] alembic failed; main.lifespan will fall back to create_all"
fi

exec "$@"
