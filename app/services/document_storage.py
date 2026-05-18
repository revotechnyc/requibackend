"""Persist uploaded document binaries for preview and download."""

from __future__ import annotations

import re
import uuid
from pathlib import Path

from app.core.config import settings

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


def save_document_file(
    organization_id: uuid.UUID,
    document_id: uuid.UUID,
    filename: str,
    raw: bytes,
) -> str:
    """Write upload bytes to disk; return relative storage path."""
    safe_name = _safe_filename(filename)
    relative = Path(str(organization_id)) / str(document_id) / safe_name
    absolute = _upload_root() / relative
    absolute.parent.mkdir(parents=True, exist_ok=True)
    absolute.write_bytes(raw)
    return str(relative)


def resolve_storage_path(storage_path: str) -> Path:
    absolute = (_upload_root() / storage_path).resolve()
    root = _upload_root()
    if not str(absolute).startswith(str(root)):
        raise ValueError("Invalid storage path")
    return absolute


def read_document_file(storage_path: str) -> bytes:
    path = resolve_storage_path(storage_path)
    if not path.is_file():
        raise FileNotFoundError(storage_path)
    return path.read_bytes()


def remove_document_file(storage_path: str | None) -> None:
    if not storage_path:
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
