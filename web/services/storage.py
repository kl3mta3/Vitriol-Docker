"""Storage backends for uploaded sources + converted outputs.

Two implementations:

* :class:`LocalBackend` — writes to ``/data/uploads`` and ``/data/outputs``
  (the on-disk tree the app has always used). URIs look like
  ``file:///data/outputs/12_abc.png``.
* :class:`S3Backend` — writes to an operator-configured S3-compatible
  endpoint (AWS, MinIO, Backblaze B2, Cloudflare R2, …). URIs look like
  ``s3://bucket/outputs/12_abc.png``.

The factory :func:`current_backend` reads ``ServerSettings.storage_backend``
and returns the active backend for *new* writes. The resolver
:func:`backend_for_uri` dispatches by URI scheme so existing
``file://`` rows keep working after a flip to S3 (and vice versa) — no
mass rewrite needed when the operator switches backends.

The conversion engine ([app/core/router.py](app/core/router.py)) only
understands :class:`pathlib.Path` inputs, so the service layer is
responsible for downloading sources to a tempfile, running the engine
against tempfiles, and uploading the result back to the active backend.
That round-trip is the *only* place the engine touches the backend; nothing
inside the engine needs to know about S3.

This module is import-side-effect-free — instantiating an :class:`S3Backend`
performs the boto3 import lazily so local-only operators don't pay the
~30 MB import cost.
"""
from __future__ import annotations

import logging
import shutil
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO, Iterator, Optional, Protocol
from urllib.parse import quote, unquote, urlparse

from fastapi.responses import FileResponse, Response, StreamingResponse

from ..config import get_settings

_log = logging.getLogger(__name__)


# --------------------------------------------------------------- protocol


class StorageBackend(Protocol):
    """Common shape every backend implements. Methods take *URIs* (not
    bare paths) so a single call site can route writes through one
    backend and reads through another based on the URI scheme."""

    scheme: str   # 'file' or 's3' — used by the resolver

    def save_stream(self, fileobj: BinaryIO, key: str) -> str:
        """Persist ``fileobj`` at ``key``. Returns the URI it can be
        retrieved by. ``key`` is a relative path inside the backend's
        namespace ("uploads/12_abc.pdf", "outputs/3_xyz.png")."""

    def upload_from_path(self, src: Path, key: str) -> str:
        """Persist a local file at ``key``. Returns the URI."""

    def download_to_path(self, uri: str, dst: Path) -> None:
        """Materialize the object at ``uri`` onto local disk at
        ``dst``. Used by the conversion service to feed the engine."""

    def open_read(self, uri: str) -> BinaryIO:
        """Open a streaming reader for ``uri``. Caller is responsible
        for closing the handle. Used by the zip-download path that
        needs to mix multiple URIs into one stream."""

    def exists(self, uri: str) -> bool: ...

    def size(self, uri: str) -> Optional[int]:
        """Object size in bytes, or None if unknown / unsupported."""

    def delete(self, uri: str) -> bool:
        """Delete the object. Returns True if a delete happened, False
        if it was already gone. Errors are logged + swallowed —
        cleanup paths can't afford to crash on a single missing key."""

    def stream_response(self, uri: str, filename: str, *,
                        media_type: Optional[str] = None) -> Response:
        """Build a FastAPI response that streams the object to the
        client. Caller adds Content-Disposition via ``filename``."""


# --------------------------------------------------------- local backend


class LocalBackend:
    """Backend that reads/writes the on-disk ``/data`` volume.

    URIs are formatted as ``file:///absolute/path``. Path is URL-quoted
    so a stray space or unicode character in a key doesn't break parsing
    on the way back through :func:`backend_for_uri`.
    """

    scheme = "file"

    def __init__(self, data_dir: Path) -> None:
        self._data_dir = Path(data_dir)

    # --- internal helpers ------------------------------------------------

    def _key_to_path(self, key: str) -> Path:
        # Two top-level namespaces — uploads/, outputs/. Anything else
        # also works; we just join under data_dir.
        return self._data_dir / key

    @staticmethod
    def _uri_to_path(uri: str) -> Path:
        parsed = urlparse(uri)
        if parsed.scheme != "file":
            raise ValueError(f"LocalBackend got non-file URI: {uri!r}")
        # urlparse on file:///a/b puts /a/b in `path`, but on Windows
        # the leading slash needs trimming. Keep it simple — strip a
        # single leading slash only if path[2] == ':' (drive letter).
        p = unquote(parsed.path)
        if len(p) > 2 and p[0] == "/" and p[2] == ":":
            p = p[1:]
        return Path(p)

    @staticmethod
    def _path_to_uri(p: Path) -> str:
        # POSIX-flavored URIs even on Windows so the value is portable
        # across hosts. quote() leaves '/' alone.
        s = p.resolve().as_posix()
        if not s.startswith("/"):
            s = "/" + s   # Windows drive-relative → /C:/...
        return "file://" + quote(s, safe="/:")

    # --- protocol --------------------------------------------------------

    def save_stream(self, fileobj: BinaryIO, key: str) -> str:
        dst = self._key_to_path(key)
        dst.parent.mkdir(parents=True, exist_ok=True)
        with dst.open("wb") as out:
            shutil.copyfileobj(fileobj, out)
        return self._path_to_uri(dst)

    def upload_from_path(self, src: Path, key: str) -> str:
        dst = self._key_to_path(key)
        dst.parent.mkdir(parents=True, exist_ok=True)
        # rename() is atomic + free when src and dst are on the same
        # filesystem (almost always true inside the container). Fall
        # back to copy on cross-fs error.
        try:
            src.replace(dst)
        except OSError:
            shutil.copyfile(src, dst)
        return self._path_to_uri(dst)

    def download_to_path(self, uri: str, dst: Path) -> None:
        src = self._uri_to_path(uri)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)

    def open_read(self, uri: str) -> BinaryIO:
        return self._uri_to_path(uri).open("rb")

    def exists(self, uri: str) -> bool:
        try:
            return self._uri_to_path(uri).exists()
        except OSError:
            return False

    def size(self, uri: str) -> Optional[int]:
        try:
            return self._uri_to_path(uri).stat().st_size
        except OSError:
            return None

    def delete(self, uri: str) -> bool:
        try:
            p = self._uri_to_path(uri)
            if p.exists():
                p.unlink()
                return True
        except OSError as e:
            _log.warning("storage(local): failed to delete %s: %s", uri, e)
        return False

    def stream_response(self, uri: str, filename: str, *,
                        media_type: Optional[str] = None) -> Response:
        # FileResponse handles range requests + ETag for free, which is
        # exactly what the legacy /jobs/{id}/result returned. Keep it
        # for the file backend so semantics don't drift.
        return FileResponse(
            self._uri_to_path(uri),
            filename=filename,
            media_type=media_type or "application/octet-stream",
        )


# -------------------------------------------------------------- s3 backend


class S3Backend:
    """Backend that reads/writes an S3-compatible endpoint.

    The boto3 import is deferred to ``__init__`` so installations that
    only ever use ``LocalBackend`` never trigger the (substantial)
    boto3/botocore import cost. If boto3 isn't installed at all, the
    constructor raises a clear error instead of an opaque ImportError
    elsewhere.

    URIs are ``s3://<bucket>/<key>`` and do **not** embed the endpoint
    URL. The endpoint lives in ``ServerSettings`` (one per deployment),
    so a URI is meaningful only relative to the active endpoint. That
    matches every other piece of operator-tied state — re-pointing the
    endpoint moves the whole set of URIs as a group.
    """

    scheme = "s3"

    def __init__(
        self,
        *,
        endpoint_url: Optional[str],
        bucket: str,
        region: Optional[str],
        access_key: str,
        secret_key: str,
        path_prefix: Optional[str] = None,
        force_path_style: bool = False,
    ) -> None:
        try:
            import boto3
            from botocore.client import Config
        except ImportError as e:   # pragma: no cover — covered by deploy
            raise RuntimeError(
                "S3 storage backend requires boto3 — add it to requirements.txt "
                "or set storage_backend back to 'local'."
            ) from e
        self._bucket = bucket
        self._prefix = (path_prefix or "").strip("/")
        # `path` addressing style is needed for MinIO + some self-hosted
        # gateways; AWS itself prefers `virtual`, the boto3 default.
        cfg = Config(
            s3={"addressing_style": "path" if force_path_style else "virtual"},
            signature_version="s3v4",
        )
        self._client = boto3.client(
            "s3",
            endpoint_url=endpoint_url or None,
            region_name=region or None,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            config=cfg,
        )

    # --- internal helpers ------------------------------------------------

    def _prefixed(self, key: str) -> str:
        key = key.lstrip("/")
        return f"{self._prefix}/{key}" if self._prefix else key

    @staticmethod
    def _uri_to_bucket_key(uri: str) -> tuple[str, str]:
        parsed = urlparse(uri)
        if parsed.scheme != "s3":
            raise ValueError(f"S3Backend got non-s3 URI: {uri!r}")
        bucket = parsed.netloc
        key = parsed.path.lstrip("/")
        return bucket, unquote(key)

    def _make_uri(self, key: str) -> str:
        return f"s3://{self._bucket}/{quote(key, safe='/')}"

    # --- protocol --------------------------------------------------------

    def save_stream(self, fileobj: BinaryIO, key: str) -> str:
        full_key = self._prefixed(key)
        self._client.upload_fileobj(fileobj, self._bucket, full_key)
        return self._make_uri(full_key)

    def upload_from_path(self, src: Path, key: str) -> str:
        full_key = self._prefixed(key)
        self._client.upload_file(str(src), self._bucket, full_key)
        return self._make_uri(full_key)

    def download_to_path(self, uri: str, dst: Path) -> None:
        bucket, key = self._uri_to_bucket_key(uri)
        dst.parent.mkdir(parents=True, exist_ok=True)
        self._client.download_file(bucket, key, str(dst))

    def open_read(self, uri: str) -> BinaryIO:
        bucket, key = self._uri_to_bucket_key(uri)
        obj = self._client.get_object(Bucket=bucket, Key=key)
        # botocore's StreamingBody is a file-like wrapper.
        return obj["Body"]

    def exists(self, uri: str) -> bool:
        bucket, key = self._uri_to_bucket_key(uri)
        try:
            self._client.head_object(Bucket=bucket, Key=key)
            return True
        except Exception:
            return False

    def size(self, uri: str) -> Optional[int]:
        bucket, key = self._uri_to_bucket_key(uri)
        try:
            obj = self._client.head_object(Bucket=bucket, Key=key)
            return int(obj.get("ContentLength")) if "ContentLength" in obj else None
        except Exception:
            return None

    def delete(self, uri: str) -> bool:
        bucket, key = self._uri_to_bucket_key(uri)
        try:
            self._client.delete_object(Bucket=bucket, Key=key)
            return True
        except Exception as e:
            _log.warning("storage(s3): failed to delete %s: %s", uri, e)
            return False

    def stream_response(self, uri: str, filename: str, *,
                        media_type: Optional[str] = None) -> Response:
        body = self.open_read(uri)

        def _iter() -> Iterator[bytes]:
            try:
                while True:
                    chunk = body.read(64 * 1024)
                    if not chunk:
                        break
                    yield chunk
            finally:
                body.close()

        return StreamingResponse(
            _iter(),
            media_type=media_type or "application/octet-stream",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    def head_bucket(self) -> None:
        """Used by the admin Test button. Raises on any failure so the
        route handler can stamp ``s3_last_test_ok=False`` with a useful
        error string."""
        self._client.head_bucket(Bucket=self._bucket)


# -------------------------------------------------------- factory + cache


# Cached active backend. Cleared by `invalidate_cache()` whenever a
# settings PATCH touches a storage_* field, so the next call re-reads
# from the DB. Boot order: lifespan task seeds this by calling
# current_backend() once.
_active: Optional[StorageBackend] = None


def invalidate_cache() -> None:
    """Drop the cached backend so the next ``current_backend()`` call
    rebuilds it from current ``ServerSettings``."""
    global _active
    _active = None


def _build_from_settings(s) -> StorageBackend:
    """Construct the active backend from a ServerSettings row. ``s``
    is the SQLAlchemy ORM instance (or any duck-typed equivalent)."""
    backend_kind = (getattr(s, "storage_backend", None) or "local").lower()
    if backend_kind == "s3":
        from ..auth.crypto import decrypt
        secret = decrypt(s.s3_secret_key_enc)
        if not s.s3_bucket or not s.s3_access_key or not secret:
            # Misconfigured S3 — fall back to local rather than crash.
            # Operator will see the test-failed pill in the UI.
            _log.warning("storage: s3 backend selected but config incomplete; "
                         "falling back to local")
        else:
            return S3Backend(
                endpoint_url=s.s3_endpoint_url,
                bucket=s.s3_bucket,
                region=s.s3_region,
                access_key=s.s3_access_key,
                secret_key=secret,
                path_prefix=s.s3_path_prefix,
                force_path_style=bool(s.s3_force_path_style),
            )
    return LocalBackend(_settings_data_dir())


def _settings_data_dir() -> Path:
    return get_settings().data_dir


def current_backend(db=None) -> StorageBackend:
    """Active backend for *new* writes. Cached after first call.

    ``db`` is optional — if a session is already available the caller
    passes it; otherwise we open one. Cheap either way (a single read
    of the singleton row).
    """
    global _active
    if _active is not None:
        return _active
    from ..models import ServerSettings
    if db is None:
        from ..db import SessionLocal
        db = SessionLocal()
        try:
            s = db.query(ServerSettings).get(1)
        finally:
            db.close()
    else:
        s = db.query(ServerSettings).get(1)
    if s is None:
        # Pre-bootstrap call. Default to local — caller will be the
        # first-boot path and storage_backend column doesn't exist yet
        # on a brand-new fresh-DB.
        _active = LocalBackend(_settings_data_dir())
    else:
        _active = _build_from_settings(s)
    return _active


def backend_for_uri(uri: str) -> StorageBackend:
    """Return the right backend to *read* a given URI.

    Existing ``file://`` URIs always go through the local backend, even
    after an operator switches the active backend to S3. That's how
    rows written before the flip remain downloadable.
    """
    if not uri:
        # Defensive — caller should never pass empty, but a `file://`
        # path fallback keeps things from crashing in legacy data paths.
        return LocalBackend(_settings_data_dir())
    if uri.startswith("file://"):
        return LocalBackend(_settings_data_dir())
    if uri.startswith("s3://"):
        # Reads of s3:// URIs assume the *current* S3 config still
        # points at the bucket that wrote them. Operators who repoint
        # the endpoint at a different bucket will lose access — same
        # contract as any other cloud-backed app.
        return current_backend()
    # Legacy bare path — treat as local.
    return LocalBackend(_settings_data_dir())


def normalize_legacy_path(path: str) -> str:
    """Convert a bare absolute path (from a row written before the
    URI migration) to a ``file://`` URI. Idempotent — already-prefixed
    URIs pass through unchanged. Used by the one-shot boot-time
    backfill on existing Job rows."""
    if not path:
        return path
    if "://" in path:
        return path
    p = Path(path)
    s = p.as_posix()
    if not s.startswith("/"):
        s = "/" + s
    return "file://" + quote(s, safe="/:")
