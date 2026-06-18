"""Persist uploaded document binaries (local disk, AWS S3, or Google Cloud Storage)."""

from __future__ import annotations

import logging
import re
import uuid
from pathlib import Path

from app.core.config import settings

logger = logging.getLogger(__name__)

MIME_BY_EXTENSION = {
    "pdf": "application/pdf",
    "txt": "text/plain; charset=utf-8",
    "md": "text/markdown; charset=utf-8",
    "csv": "text/csv; charset=utf-8",
    "html": "text/html; charset=utf-8",
    "htm": "text/html; charset=utf-8",
}


def _upload_root() -> Path:
    root = Path(settings.document_upload_dir)
    root.mkdir(parents=True, exist_ok=True)
    return root.resolve()


def _safe_filename(filename: str) -> str:
    name = Path(filename).name
    name = re.sub(r"[^\w.\- ]", "_", name).strip() or "upload"
    return name[:200]


def content_type_for_extension(ext: str) -> str:
    return MIME_BY_EXTENSION.get(ext.lower(), "application/octet-stream")


def _storage_backend() -> str:
    return (settings.document_storage_type or "local").strip().lower()


def is_remote_storage_path(storage_path: str) -> bool:
    """Cloud objects are stored in DB with a leading slash (e.g. /org/doc/file.pdf)."""
    return storage_path.startswith("/")


def is_s3_storage_path(storage_path: str) -> bool:
    """Backward-compatible alias for remote/cloud storage paths."""
    return is_remote_storage_path(storage_path)


def _object_key(storage_path: str) -> str:
    return storage_path.lstrip("/")


def _relative_local_path(
    organization_id: uuid.UUID,
    document_id: uuid.UUID,
    filename: str,
) -> str:
    safe_name = _safe_filename(filename)
    return str(Path(str(organization_id)) / str(document_id) / safe_name)


def _db_storage_path_for_object(object_key: str) -> str:
    return f"/{object_key}"


def _ensure_s3_config() -> None:
    missing = []
    if not settings.s3_bucket_name:
        missing.append("S3_BUCKET_NAME")
    if not settings.aws_access_key_id:
        missing.append("AWS_ACCESS_KEY_ID")
    if not settings.aws_secret_access_key:
        missing.append("AWS_SECRET_ACCESS_KEY")
    if missing:
        raise ValueError(f"Missing required S3 configuration: {', '.join(missing)}")


def _resolve_gcs_credentials_path() -> Path:
    raw = (settings.gcs_credentials_path or "").strip()
    if not raw:
        raise ValueError("GCS_CREDENTIALS_PATH is not set")
    path = Path(raw)
    if path.is_file():
        return path.resolve()
    repo_root = Path(__file__).resolve().parents[2]
    alt = (repo_root / raw).resolve()
    if alt.is_file():
        return alt
    raise ValueError(f"GCS credentials file not found: {raw}")


def _ensure_gcs_config() -> None:
    missing = []
    if not settings.gcs_bucket_name:
        missing.append("GCS_BUCKET_NAME")
    if not (settings.gcs_credentials_path or "").strip():
        missing.append("GCS_CREDENTIALS_PATH")
    if missing:
        raise ValueError(f"Missing required GCS configuration: {', '.join(missing)}")
    _resolve_gcs_credentials_path()


def _s3_client():
    import boto3

    _ensure_s3_config()
    return boto3.client(
        "s3",
        region_name=settings.s3_region,
        aws_access_key_id=settings.aws_access_key_id,
        aws_secret_access_key=settings.aws_secret_access_key,
    )


def _gcs_bucket():
    from google.cloud import storage
    from google.oauth2 import service_account

    _ensure_gcs_config()
    cred_path = _resolve_gcs_credentials_path()
    credentials = service_account.Credentials.from_service_account_file(str(cred_path))
    client = storage.Client(
        credentials=credentials,
        project=settings.gcs_project_id or credentials.project_id,
    )
    return client.bucket(settings.gcs_bucket_name)


def _s3_configured() -> bool:
    return bool(
        settings.s3_bucket_name
        and settings.aws_access_key_id
        and settings.aws_secret_access_key
    )


def build_document_file_url(storage_path: str | None) -> str | None:
    """Public file URL for cloud-stored documents."""
    if not storage_path or not is_remote_storage_path(storage_path):
        return None

    key = _object_key(storage_path)
    backend = _storage_backend()

    if backend == "gcs":
        base = (settings.gcs_public_base_url or "").strip()
        if base:
            return f"{base.rstrip('/')}/{key}"
        if settings.gcs_bucket_name:
            return f"https://storage.googleapis.com/{settings.gcs_bucket_name}/{key}"
        return None

    endpoint = (settings.s3_endpoint_url or "").strip()
    if endpoint:
        return f"{endpoint.rstrip('/')}{storage_path}"
    return None


def _save_to_s3(object_key: str, raw: bytes, media_type: str) -> None:
    client = _s3_client()
    try:
        client.put_object(
            Bucket=settings.s3_bucket_name,
            Key=object_key,
            Body=raw,
            ContentType=media_type,
        )
    except Exception as exc:
        logger.exception("s3_upload_failed key=%s", object_key)
        raise RuntimeError(f"S3 upload failed: {exc}") from exc


def _save_to_gcs(object_key: str, raw: bytes, media_type: str) -> None:
    bucket = _gcs_bucket()
    blob = bucket.blob(object_key)
    try:
        blob.upload_from_string(raw, content_type=media_type)
    except Exception as exc:
        logger.exception("gcs_upload_failed key=%s", object_key)
        raise RuntimeError(f"GCS upload failed: {exc}") from exc


def _read_from_s3(object_key: str) -> bytes:
    client = _s3_client()
    try:
        response = client.get_object(Bucket=settings.s3_bucket_name, Key=object_key)
        return response["Body"].read()
    except Exception as exc:
        logger.warning("s3_read_failed key=%s error=%s", object_key, exc)
        raise FileNotFoundError(object_key) from exc


def _read_from_gcs(object_key: str) -> bytes:
    bucket = _gcs_bucket()
    blob = bucket.blob(object_key)
    try:
        if not blob.exists():
            raise FileNotFoundError(object_key)
        return blob.download_as_bytes()
    except FileNotFoundError:
        raise
    except Exception as exc:
        logger.warning("gcs_read_failed key=%s error=%s", object_key, exc)
        raise FileNotFoundError(object_key) from exc


def _delete_from_s3(object_key: str) -> None:
    try:
        client = _s3_client()
        client.delete_object(Bucket=settings.s3_bucket_name, Key=object_key)
    except Exception as exc:
        logger.warning("s3_delete_failed key=%s error=%s", object_key, exc)


def _delete_from_gcs(object_key: str) -> None:
    try:
        bucket = _gcs_bucket()
        blob = bucket.blob(object_key)
        if blob.exists():
            blob.delete()
    except Exception as exc:
        logger.warning("gcs_delete_failed key=%s error=%s", object_key, exc)


def save_document_file(
    organization_id: uuid.UUID,
    document_id: uuid.UUID,
    filename: str,
    raw: bytes,
    content_type: str | None = None,
) -> str:
    """Persist upload bytes; return storage path for the database."""
    media_type = content_type or "application/octet-stream"
    object_key = _relative_local_path(organization_id, document_id, filename)
    backend = _storage_backend()

    if backend == "s3":
        _save_to_s3(object_key, raw, media_type)
        return _db_storage_path_for_object(object_key)

    if backend == "gcs":
        _save_to_gcs(object_key, raw, media_type)
        return _db_storage_path_for_object(object_key)

    relative = object_key
    absolute = _upload_root() / relative
    absolute.parent.mkdir(parents=True, exist_ok=True)
    absolute.write_bytes(raw)
    return relative


def resolve_storage_path(storage_path: str) -> Path:
    """Resolve a local on-disk path (legacy uploads only)."""
    if is_remote_storage_path(storage_path):
        raise ValueError("Storage path refers to cloud storage, not local disk")
    absolute = (_upload_root() / storage_path).resolve()
    root = _upload_root()
    if not str(absolute).startswith(str(root)):
        raise ValueError("Invalid storage path")
    return absolute


def read_document_file(storage_path: str) -> bytes:
    if is_remote_storage_path(storage_path):
        key = _object_key(storage_path)
        backend = _storage_backend()

        if backend == "gcs":
            try:
                return _read_from_gcs(key)
            except FileNotFoundError:
                if _s3_configured():
                    logger.info("gcs_read_miss_trying_s3 key=%s", key)
                    return _read_from_s3(key)
                raise FileNotFoundError(storage_path) from None

        if backend == "s3":
            return _read_from_s3(key)

        raise ValueError(f"Unsupported cloud storage backend: {backend}")

    path = resolve_storage_path(storage_path)
    if not path.is_file():
        raise FileNotFoundError(storage_path)
    return path.read_bytes()


def remove_document_file(storage_path: str | None) -> None:
    if not storage_path:
        return

    if is_remote_storage_path(storage_path):
        key = _object_key(storage_path)
        backend = _storage_backend()
        if backend == "gcs":
            _delete_from_gcs(key)
        elif backend == "s3":
            _delete_from_s3(key)
        return

    try:
        path = resolve_storage_path(storage_path)
    except ValueError:
        return
    if path.is_file():
        path.unlink(missing_ok=True)
    parent = path.parent
    if parent.is_dir() and not any(parent.iterdir()):
        parent.rmdir()
        org_parent = parent.parent
        if org_parent.is_dir() and not any(org_parent.iterdir()):
            org_parent.rmdir()
