"""
Extract plain text from uploaded files (PDF, DOCX, text formats) with OCR fallback for scans.
"""

from __future__ import annotations

import io
import logging
import zipfile
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Optional
from xml.etree import ElementTree as ET

from fastapi import HTTPException, status

from app.core.config import settings

logger = logging.getLogger(__name__)

_RELS_NS = {"r": "http://schemas.openxmlformats.org/package/2006/relationships"}
_CT_NS = {"ct": "http://schemas.openxmlformats.org/package/2006/content-types"}


@dataclass
class ExtractionResult:
    text: str
    page_count: Optional[int] = None
    method: str = "unknown"


def _resolve_docx_part_target(rels_path: str, target: str) -> Optional[str]:
    """Map a relationship Target to a ZIP member path, or None if external/skip."""
    target = (target or "").strip()
    if not target or target.startswith("#"):
        return None
    if target.startswith(("http://", "https://", "mailto:")):
        return None
    if target.startswith("/"):
        return target.lstrip("/")

    # word/_rels/document.xml.rels → base directory is word/
    base = PurePosixPath(rels_path).parent.parent
    resolved = PurePosixPath(base) / target
    parts: list[str] = []
    for part in resolved.as_posix().split("/"):
        if part == "..":
            if parts:
                parts.pop()
        elif part and part != ".":
            parts.append(part)
    return "/".join(parts)


def _strip_missing_docx_relationships(
    rels_xml: bytes, rels_path: str, zip_names: set[str]
) -> tuple[bytes, list[str]]:
    """Drop Relationship entries whose Target part is absent from the package."""
    try:
        root = ET.fromstring(rels_xml)
    except ET.ParseError:
        return rels_xml, []

    removed: list[str] = []
    for rel in list(root.findall("r:Relationship", _RELS_NS)):
        if (rel.get("TargetMode") or "").lower() == "external":
            continue
        target = rel.get("Target") or ""
        member = _resolve_docx_part_target(rels_path, target)
        if member is None:
            continue
        if member not in zip_names and member.replace("\\", "/") not in zip_names:
            removed.append(member)
            root.remove(rel)

    if not removed:
        return rels_xml, []
    return ET.tostring(root, encoding="utf-8", xml_declaration=True), removed


def _strip_missing_content_types(
    content_types_xml: bytes, zip_names: set[str]
) -> tuple[bytes, list[str]]:
    """Drop Override entries that point at missing package parts."""
    try:
        root = ET.fromstring(content_types_xml)
    except ET.ParseError:
        return content_types_xml, []

    removed: list[str] = []
    for override in list(root.findall("ct:Override", _CT_NS)):
        part_name = (override.get("PartName") or "").lstrip("/")
        if part_name and part_name not in zip_names:
            removed.append(part_name)
            root.remove(override)

    if not removed:
        return content_types_xml, []
    return ET.tostring(root, encoding="utf-8", xml_declaration=True), removed


def _sanitize_docx_package(raw: bytes) -> tuple[bytes, list[str]]:
    """
    Repair incomplete DOCX packages that declare optional parts (e.g. endnotes)
    without including those XML files. Word/LibreOffice tolerate this; python-docx does not.
    Valid packages are returned unchanged.
    """
    try:
        src = zipfile.ZipFile(io.BytesIO(raw), "r")
    except zipfile.BadZipFile as exc:
        raise ValueError(f"Invalid DOCX archive: {exc}") from exc

    with src:
        names = {n.replace("\\", "/") for n in src.namelist()}
        removed_parts: list[str] = []
        rewritten: dict[str, bytes] = {}

        for name in names:
            if name.endswith(".rels"):
                data, removed = _strip_missing_docx_relationships(
                    src.read(name), name, names
                )
                if removed:
                    rewritten[name] = data
                    removed_parts.extend(removed)
            elif name == "[Content_Types].xml":
                data, removed = _strip_missing_content_types(src.read(name), names)
                if removed:
                    rewritten[name] = data
                    removed_parts.extend(removed)

        if not rewritten:
            return raw, []

        out = io.BytesIO()
        with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as dst:
            for info in src.infolist():
                member = info.filename.replace("\\", "/")
                payload = rewritten.get(member, src.read(info.filename))
                dst.writestr(info.filename, payload)

    unique_removed = sorted(set(removed_parts))
    logger.info(
        "DOCX package sanitized; ignored missing parts: %s",
        ", ".join(unique_removed),
    )
    return out.getvalue(), unique_removed


def _extract_pdf_pypdf(raw: bytes) -> tuple[str, int]:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(raw))
    page_count = len(reader.pages)
    parts = []
    for page in reader.pages:
        parts.append(page.extract_text() or "")
    return "\n".join(parts).strip(), page_count


def _extract_pdf_pymupdf(raw: bytes) -> tuple[str, int]:
    import fitz

    doc = fitz.open(stream=raw, filetype="pdf")
    page_count = doc.page_count
    parts = []
    for page in doc:
        parts.append(page.get_text("text") or "")
    doc.close()
    return "\n".join(parts).strip(), page_count


def _extract_pdf_ocr(raw: bytes, max_pages: int) -> tuple[str, int]:
    import fitz
    import pytesseract
    from PIL import Image

    doc = fitz.open(stream=raw, filetype="pdf")
    page_count = doc.page_count
    limit = min(page_count, max_pages)
    parts: list[str] = []
    for i in range(limit):
        page = doc[i]
        pix = page.get_pixmap(matrix=fitz.Matrix(2, 2))
        img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        parts.append(pytesseract.image_to_string(img) or "")
    doc.close()
    text = "\n".join(parts).strip()
    if page_count > limit:
        text += f"\n\n[OCR limited to first {limit} of {page_count} pages]"
    return text, page_count


def _ocr_available() -> bool:
    if not settings.document_ocr_enabled:
        return False
    try:
        import pytesseract

        pytesseract.get_tesseract_version()
        return True
    except Exception:
        return False


def _extract_pdf(raw: bytes) -> ExtractionResult:
    text, page_count = _extract_pdf_pypdf(raw)
    method = "pypdf"

    min_chars = max(32, page_count * 40)
    if len(text) < min_chars:
        try:
            alt, page_count = _extract_pdf_pymupdf(raw)
            if len(alt) > len(text):
                text, method = alt, "pymupdf"
        except Exception as exc:
            logger.debug("pymupdf extraction skipped: %s", exc)

    if len(text) < min_chars and _ocr_available():
        try:
            ocr_text, page_count = _extract_pdf_ocr(raw, settings.document_ocr_max_pages)
            if len(ocr_text) > len(text):
                text, method = ocr_text, "ocr"
        except Exception as exc:
            logger.warning("PDF OCR failed: %s", exc)

    return ExtractionResult(text=text, page_count=page_count, method=method)


def _extract_docx(raw: bytes) -> ExtractionResult:
    from docx import Document as DocxDocument

    try:
        sanitized, missing = _sanitize_docx_package(raw)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Could not read file: {exc}",
        ) from exc

    doc = DocxDocument(io.BytesIO(sanitized))
    parts = [p.text for p in doc.paragraphs if p.text and p.text.strip()]
    for table in doc.tables:
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells if c.text and c.text.strip()]
            if cells:
                parts.append(" | ".join(cells))
    method = "docx_sanitized" if missing else "docx"
    return ExtractionResult(text="\n".join(parts).strip(), page_count=None, method=method)


def extract_text_from_upload(filename: str, raw: bytes) -> ExtractionResult:
    """Return extracted text and metadata for an uploaded file."""
    if not raw:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Empty file")

    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""

    if ext == "pdf":
        return _extract_pdf(raw)

    if ext in ("txt", "md", "csv", "html", "htm"):
        text = raw.decode("utf-8", errors="ignore").strip()
        return ExtractionResult(text=text, page_count=None, method=ext)

    if ext == "docx":
        return _extract_docx(raw)

    if ext == "doc":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Legacy .doc files are not supported. Save as .docx or PDF and re-upload.",
        )

    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail=f"Unsupported file type: .{ext or 'unknown'}",
    )
