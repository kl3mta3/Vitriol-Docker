#!/usr/bin/env bash
# Vitriol container entrypoint.
#
# Phase 1 (as root): fix /data ownership so writes don't silently fail
# after the volume is mounted, then re-exec self as the vitriol user.
# Phase 2 (as vitriol): create per-feature subdirs, run migrations, exec
# the CMD (uvicorn).
#
# Why two phases: the Dockerfile's chown -R only affects the image
# filesystem; the moment Docker mounts a named volume at /data the
# pre-existing ownership is hidden by the mount, and the mount itself
# is typically root-owned. If the app runs as a non-root user from the
# very first instruction, every write to /data either fails outright
# or (worse) succeeds the first time on a forgiving FS layer and then
# starts failing on subsequent boots — which is the exact failure mode
# that was silently rotating the encryption key and breaking SMTP /
# OAuth / OIDC across container restarts.

set -euo pipefail

DATA_DIR="${VITRIOL_DATA_DIR:-/data}"
APP_USER="vitriol"
APP_UID=1000
APP_GID=1000

if [ "$(id -u)" = "0" ]; then
  # Phase 1: running as root. Take ownership of the data volume so the
  # vitriol user can read AND write everything inside, then drop privs.
  echo "[entrypoint] Phase 1 (root): preparing ${DATA_DIR}"

  mkdir -p "${DATA_DIR}/uploads" \
           "${DATA_DIR}/outputs" \
           "${DATA_DIR}/certs" \
           "${DATA_DIR}/logs"

  # `|| true` because some bind-mount setups (host-side bind mounts,
  # especially on macOS) reject chown but still serve writable files.
  chown -R "${APP_UID}:${APP_GID}" "${DATA_DIR}" 2>/dev/null || \
    echo "[entrypoint] note: chown ${DATA_DIR} failed; trusting host-side perms"

  # Re-exec ourselves as the vitriol user. gosu is preferred over `su`
  # because it preserves signals (SIGTERM from the orchestrator reaches
  # uvicorn cleanly).
  exec gosu "${APP_USER}" "$0" "$@"
fi

# Phase 2: running as vitriol (UID 1000). The volume is now writable.
echo "[entrypoint] Phase 2 (vitriol): starting app"
cd /app

if command -v alembic >/dev/null 2>&1; then
  echo "[entrypoint] running alembic upgrade head"
  alembic upgrade head || \
    echo "[entrypoint] alembic failed; main.lifespan will fall back to create_all"
fi

exec "$@"
