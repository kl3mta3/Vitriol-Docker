FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    QT_QPA_PLATFORM=offscreen \
    VITRIOL_DATA_DIR=/data \
    UC_USER_DATA_DIR=/data \
    UC_DOCS_DIR=/data/outputs

# System deps. ffmpeg + assimp give the engine its native binaries; the
# Qt offscreen platform plugin needs a couple of X libs even when no
# display is used (PySide6 still imports them).
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        assimp-utils \
        libassimp5 \
        pandoc \
        jq \
        gosu \
        libxkbcommon0 \
        libdbus-1-3 \
        libegl1 \
        libgl1 \
        libfreetype6 \
        libfontconfig1 \
        ca-certificates \
        curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/
COPY web/ ./web/
COPY resources/ ./resources/
COPY alembic.ini ./
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh && \
    mkdir -p /data /data/uploads /data/outputs /data/certs /data/logs && \
    useradd -r -u 1000 -m -d /home/vitriol vitriol && \
    chown -R vitriol:vitriol /app /data

# Default listening port. 3825 picked as a clean unused IANA range port to
# minimize collision with other self-hosted services (8000, 8080, 3000 etc.
# get reused everywhere). Override at the orchestrator layer if needed.
EXPOSE 3825
VOLUME ["/data"]

# NOTE: container starts as root. The entrypoint chowns /data to the
# vitriol user *after* the volume is mounted (Docker mounts volumes after
# image filesystem materialisation, so any earlier chown -R is a no-op
# the moment a named volume comes into play), then drops to vitriol via
# `gosu`. This is the standard pattern for handling persistent volumes
# without forcing the operator to chown the host-side bind mount, and it
# fixes the silent write failures that previously corrupted /data/.secret_key
# across restarts on Coolify and other PaaS hosts.
ENTRYPOINT ["/entrypoint.sh"]
CMD ["uvicorn", "web.main:app", "--host", "0.0.0.0", "--port", "3825", "--proxy-headers"]
