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

USER vitriol
# Default listening port. 3825 picked as a clean unused IANA range port to
# minimize collision with other self-hosted services (8000, 8080, 3000 etc.
# get reused everywhere). Override at the orchestrator layer if needed.
EXPOSE 3825
VOLUME ["/data"]

ENTRYPOINT ["/entrypoint.sh"]
CMD ["uvicorn", "web.main:app", "--host", "0.0.0.0", "--port", "3825", "--proxy-headers"]
