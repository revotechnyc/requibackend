"""Persist uploaded document binaries (local disk or AWS S3)."""

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


def _use_s3() -> bool:
    return (settings.document_storage_type or "local").strip().lower() == "s3"


def is_s3_storage_path(storage_path: str) -> bool:
    """S3 objects are stored in DB with a leading slash (e.g. /org/doc/file.pdf)."""
    return storage_path.startswith("/")


def _s3_object_key(storage_path: str) -> str:
    return storage_path.lstrip("/")


def _relative_local_path(
    organization_id: uuid.UUID,
    document_id: uuid.UUID,
    filename: str,
) -> str:
    safe_name = _safe_filename(filename)
    return str(Path(str(organization_id)) / str(document_id) / safe_name)


def _db_storage_path_for_s3(object_key: str) -> str:
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
        raise ValueError(
            f"Missing required S3 configuration: {', '.join(missing)}"
        )


def _s3_client():
    import boto3

    _ensure_s3_config()
    return boto3.client(
        "s3",
        region_name=settings.s3_region,
        aws_access_key_id=settings.aws_access_key_id,
        aws_secret_access_key=settings.aws_secret_access_key,
    )


def build_document_file_url(storage_path: str | None) -> str | None:
    """Public file URL for S3-stored documents (S3_ENDPOINT_URL + /key path)."""
    if not storage_path or not is_s3_storage_path(storage_path):
        return None
    endpoint = (settings.s3_endpoint_url or "").strip()
    if not endpoint:
        return None
    return f"{endpoint.rstrip('/')}{storage_path}"


def save_document_file(
    organization_id: uuid.UUID,
    document_id: uuid.UUID,
    filename: str,
    raw: bytes,
    content_type: str | None = None,
) -> str:
    """Persist upload bytes; return storage path for the database."""
    media_type = content_type or "application/octet-stream"

    if _use_s3():
        object_key = _relative_local_path(organization_id, document_id, filename)
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
        return _db_storage_path_for_s3(object_key)

    relative = _relative_local_path(organization_id, document_id, filename)
    absolute = _upload_root() / relative
    absolute.parent.mkdir(parents=True, exist_ok=True)
    absolute.write_bytes(raw)
    return relative


def resolve_storage_path(storage_path: str) -> Path:
    """Resolve a local on-disk path (legacy uploads only)."""
    if is_s3_storage_path(storage_path):
        raise ValueError("Storage path refers to S3, not local disk")
    absolute = (_upload_root() / storage_path).resolve()
    root = _upload_root()
    if not str(absolute).startswith(str(root)):
        raise ValueError("Invalid storage path")
    return absolute


def read_document_file(storage_path: str) -> bytes:
    if is_s3_storage_path(storage_path):
        client = _s3_client()
        key = _s3_object_key(storage_path)
        try:
            response = client.get_object(
                Bucket=settings.s3_bucket_name,
                Key=key,
            )
            return response["Body"].read()
        except Exception as exc:
            logger.warning("s3_read_failed key=%s error=%s", key, exc)
            raise FileNotFoundError(storage_path) from exc

    path = resolve_storage_path(storage_path)
    if not path.is_file():
        raise FileNotFoundError(storage_path)
    return path.read_bytes()


def remove_document_file(storage_path: str | None) -> None:
    if not storage_path:
        return

    if is_s3_storage_path(storage_path):
        try:
            client = _s3_client()
            client.delete_object(
                Bucket=settings.s3_bucket_name,
                Key=_s3_object_key(storage_path),
            )
        except Exception as exc:
            logger.warning("s3_delete_failed path=%s error=%s", storage_path, exc)
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
