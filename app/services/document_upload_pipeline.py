"""
Post-upload background work: compliance gap extraction after document ingestion.
"""

from __future__ import annotations

import logging
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.db.models import Document, Organization
from app.services.compliance_ai_integration import process_intelligence_compliance_update

logger = logging.getLogger(__name__)


async def run_document_compliance_analysis(
    db: AsyncSession,
    document_id: UUID,
    *,
    filename: str | None = None,
) -> None:
    """Run compliance gap extraction for an uploaded document (after chunks exist)."""
    document = await db.get(Document, document_id)
    if not document or not (document.content or "").strip():
        return

    if not filename:
        filename = document.title

    org_result = await db.execute(
        select(Organization)
        .where(Organization.id == document.organization_id)
        .options(selectinload(Organization.subscription))
    )
    org = org_result.scalar_one_or_none()
    if not org:
        return

    compliance_body = (document.content or "").strip()
    if not compliance_body:
        return

    try:
        await process_intelligence_compliance_update(
            db,
            org,
            user_message=(
                f"Review this uploaded compliance document ({filename}) for gaps "
                f"matching the organization's active compliance frameworks only."
            ),
            assistant_message=compliance_body,
            source_type="document_analysis",
            has_documents=True,
            use_mock=settings.mock_chat_stream,
            document_filename=filename,
        )
    except Exception as exc:
        logger.warning("document_compliance_analysis_skip doc=%s: %s", document_id, exc)
        try:
            await db.rollback()
        except Exception:
            pass
