"""CLM business logic — upload, vendor registry, AI processing, automation."""

from __future__ import annotations

import hashlib
import logging
import uuid
from datetime import date, datetime, timedelta
from typing import Any, Optional

from fastapi import HTTPException, UploadFile, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.endpoints.sources import (
    ALLOWED_UPLOAD_EXTENSIONS,
    _file_extension,
    _max_upload_bytes,
    _max_upload_mb_label,
    _queue_or_run_ingestion,
)
from app.core.config import settings
from app.services.document_text_extraction import extract_text_from_upload
from app.db.models import (
    ClmContract,
    ClmContractStatus,
    ClmObligation,
    ClmSubLocation,
    ClmVendor,
    ComplianceGap,
    Document,
    Organization,
    User,
    WorkspaceTask,
    WorkspaceTaskStatus,
)
from app.services.clm_extraction import extract_clm_metadata
from app.services.compliance_ai_integration import process_intelligence_compliance_update
from app.services.compliance_gap_helpers import GapSourceContext, apply_gap_source_context
from app.services.document_storage import content_type_for_extension, save_document_file

logger = logging.getLogger(__name__)

RENEWAL_LEAD_DAYS = 90


def _contract_status_for_dates(expiration: Optional[date]) -> str:
    if not expiration:
        return ClmContractStatus.ACTIVE.value
    today = date.today()
    if expiration < today:
        return ClmContractStatus.EXPIRED.value
    if expiration <= today + timedelta(days=90):
        return ClmContractStatus.EXPIRING.value
    return ClmContractStatus.ACTIVE.value


async def _next_contract_number(db: AsyncSession, org_id: uuid.UUID) -> str:
    count = int(
        (
            await db.execute(
                select(func.count()).select_from(ClmContract).where(
                    ClmContract.organization_id == org_id
                )
            )
        ).scalar()
        or 0
    )
    year = datetime.utcnow().year
    return f"CNT-{year}-{count + 1:05d}"


async def find_or_create_vendor(
    db: AsyncSession,
    org_id: uuid.UUID,
    name: str,
    *,
    sub_location_id: Optional[uuid.UUID] = None,
    source: str = "auto",
) -> ClmVendor:
    clean = name.strip()
    if not clean:
        clean = "Unknown Vendor"
    existing = await db.execute(
        select(ClmVendor).where(
            ClmVendor.organization_id == org_id,
            ClmVendor.is_active.is_(True),
            func.lower(ClmVendor.name) == clean.lower(),
        )
    )
    vendor = existing.scalar_one_or_none()
    if vendor:
        return vendor
    vendor = ClmVendor(
        organization_id=org_id,
        sub_location_id=sub_location_id,
        name=clean[:255],
        source=source,
        is_active=True,
    )
    db.add(vendor)
    await db.flush()
    return vendor


async def upload_contract_document(
    db: AsyncSession,
    org: Organization,
    user: User,
    file: UploadFile,
    *,
    sub_location_id: Optional[uuid.UUID] = None,
    vendor_id: Optional[uuid.UUID] = None,
) -> ClmContract:
    filename = file.filename or "contract.pdf"
    ext = _file_extension(filename)
    if f".{ext}" not in ALLOWED_UPLOAD_EXTENSIONS and ext:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"File type not allowed. Supported: {', '.join(sorted(ALLOWED_UPLOAD_EXTENSIONS))}",
        )

    raw = await file.read()
    if len(raw) > _max_upload_bytes():
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"File exceeds {_max_upload_mb_label()} MB limit",
        )
    if not raw:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Empty file")

    if sub_location_id:
        loc = await db.get(ClmSubLocation, sub_location_id)
        if not loc or loc.organization_id != org.id or not loc.is_active:
            raise HTTPException(status_code=400, detail="Sub-location not found")

    if vendor_id:
        vendor = await db.get(ClmVendor, vendor_id)
        if not vendor or vendor.organization_id != org.id or not vendor.is_active:
            raise HTTPException(status_code=400, detail="Vendor not found")

    try:
        extraction = extract_text_from_upload(filename, raw)
        extracted = extraction.text
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Could not read file: {exc}") from exc

    uploader = f"{user.first_name} {user.last_name}".strip() or user.email
    doc_id = uuid.uuid4()
    storage_path = save_document_file(
        org.id,
        doc_id,
        filename,
        raw,
        content_type=file.content_type or content_type_for_extension(ext),
    )

    document = Document(
        id=doc_id,
        organization_id=org.id,
        title=filename,
        content=extracted or None,
        content_hash=hashlib.sha256(raw).hexdigest(),
        storage_path=storage_path,
        document_metadata={
            "category": "Contracts",
            "file_extension": ext,
            "content_type": file.content_type or content_type_for_extension(ext),
            "size_bytes": len(raw),
            "uploaded_by": uploader,
            "uploaded_by_id": str(user.id),
            "ingestion_status": "processing",
            "clm_source": True,
        },
    )
    db.add(document)
    await db.flush()

    contract = ClmContract(
        organization_id=org.id,
        document_id=document.id,
        vendor_id=vendor_id,
        sub_location_id=sub_location_id,
        owner_id=user.id,
        created_by_id=user.id,
        title=filename.rsplit(".", 1)[0][:500],
        contract_number=await _next_contract_number(db, org.id),
        status=ClmContractStatus.PROCESSING.value,
    )
    db.add(contract)
    await db.commit()
    await db.refresh(contract)

    await _queue_or_run_ingestion(db, document)
    await db.commit()

    try:
        await process_contract_after_upload(db, contract.id, org, user)
    except Exception as exc:
        logger.warning("clm_process_after_upload_failed: %s", exc)

    await db.refresh(contract)
    return contract


async def process_contract_after_upload(
    db: AsyncSession,
    contract_id: uuid.UUID,
    org: Organization,
    user: User,
) -> None:
    result = await db.execute(
        select(ClmContract)
        .where(ClmContract.id == contract_id)
        .options(
            selectinload(ClmContract.document),
            selectinload(ClmContract.vendor),
        )
    )
    contract = result.scalar_one_or_none()
    if not contract:
        return

    doc = contract.document
    text = doc.content or ""
    if not text and doc.storage_path:
        text = doc.content or ""

    meta = await extract_clm_metadata(text, doc.title)

    if not contract.vendor_id and meta.get("vendor_name"):
        vendor = await find_or_create_vendor(
            db,
            contract.organization_id,
            meta["vendor_name"],
            sub_location_id=contract.sub_location_id,
            source="auto",
        )
        contract.vendor_id = vendor.id

    contract.effective_date = meta.get("effective_date")
    contract.expiration_date = meta.get("expiration_date")
    contract.renewal_clause = meta.get("renewal_clause")
    contract.risk_score = meta.get("risk_score")
    contract.ai_extraction = {
        "vendor_name": meta.get("vendor_name"),
        "extracted_at": datetime.utcnow().isoformat(),
        "obligations_count": len(meta.get("obligations") or []),
    }
    contract.status = _contract_status_for_dates(contract.expiration_date)

    if contract.vendor_id and contract.title == (doc.title.rsplit(".", 1)[0] if doc.title else ""):
        vendor_row = await db.get(ClmVendor, contract.vendor_id)
        if vendor_row:
            contract.title = f"{vendor_row.name} Agreement"[:500]

    await _create_obligations_and_gaps(db, contract, meta.get("obligations") or [], org)
    await _schedule_renewal_task(db, contract, user)
    await db.flush()

    if text:
        try:
            await process_intelligence_compliance_update(
                db,
                org,
                user_message=f"CLM contract uploaded: {contract.title}",
                assistant_message=text[:8000],
                source_type="document_analysis",
                has_documents=True,
                use_mock=settings.mock_chat_stream,
                contract_id=contract.id,
                contract_name=contract.title,
            )
        except Exception as exc:
            logger.warning("clm_compliance_sync_skip: %s", exc)

    await db.commit()


async def _create_obligations_and_gaps(
    db: AsyncSession,
    contract: ClmContract,
    obligations: list[dict],
    org: Organization,
) -> None:
    for item in obligations:
        title = item.get("title") or "Contract obligation"
        gap = ComplianceGap(
            organization_id=contract.organization_id,
            framework_slug="hipaa",
            title=title[:500],
            description=item.get("description"),
            severity=item.get("severity") or "medium",
            status="open",
            category="Contracts",
        )
        apply_gap_source_context(
            gap,
            GapSourceContext(
                source_type="clm",
                source_label=contract.title[:500],
                contract_id=contract.id,
                contract_name=contract.title[:500],
                project_name="Contracts",
            ),
        )
        db.add(gap)
        await db.flush()

        obligation = ClmObligation(
            organization_id=contract.organization_id,
            contract_id=contract.id,
            compliance_gap_id=gap.id,
            title=title[:500],
            description=item.get("description"),
            obligation_type=item.get("obligation_type") or "other",
            due_date=item.get("due_date"),
            severity=item.get("severity") or "medium",
            status="open",
        )
        db.add(obligation)

        due = item.get("due_date")
        if due and isinstance(due, date):
            task = WorkspaceTask(
                organization_id=contract.organization_id,
                creator_id=contract.owner_id or contract.created_by_id,
                assignee_id=contract.owner_id,
                title=f"Obligation follow-up: {title[:200]}",
                description=item.get("description") or "Contractual obligation from CLM upload.",
                status=WorkspaceTaskStatus.PENDING.value,
                priority="high" if item.get("severity") in ("critical", "high") else "medium",
                category="Contracts",
                due_date=due.isoformat(),
                document_id=contract.document_id,
            )
            db.add(task)
            await db.flush()
            obligation.task_id = task.id
            gap.task_id = task.id
            gap.task_name = task.title[:500]


async def _schedule_renewal_task(
    db: AsyncSession,
    contract: ClmContract,
    user: User,
) -> None:
    if not contract.expiration_date:
        return
    renewal_due = contract.expiration_date - timedelta(days=RENEWAL_LEAD_DAYS)
    if contract.renewal_task_id:
        return

    task = WorkspaceTask(
        organization_id=contract.organization_id,
        creator_id=contract.created_by_id,
        assignee_id=contract.owner_id or contract.created_by_id,
        title=f"Contract renewal: {contract.title[:200]}",
        description=(
            f"Renewal review for {contract.contract_number}. "
            f"Expires {contract.expiration_date.isoformat()}."
        ),
        status=WorkspaceTaskStatus.PENDING.value,
        priority="high",
        category="Contracts",
        due_date=renewal_due.isoformat(),
        document_id=contract.document_id,
    )
    db.add(task)
    await db.flush()
    contract.renewal_task_id = task.id


def serialize_contract(contract: ClmContract, *, vendor_name: Optional[str] = None) -> dict[str, Any]:
    vendor = contract.vendor
    sub_loc = contract.sub_location
    return {
        "id": str(contract.id),
        "document_id": str(contract.document_id),
        "vendor_id": str(contract.vendor_id) if contract.vendor_id else None,
        "vendor_name": vendor_name or (vendor.name if vendor else None),
        "sub_location_id": str(contract.sub_location_id) if contract.sub_location_id else None,
        "sub_location_name": sub_loc.name if sub_loc else None,
        "title": contract.title,
        "contract_number": contract.contract_number,
        "status": contract.status,
        "effective_date": contract.effective_date.isoformat() if contract.effective_date else None,
        "expiration_date": contract.expiration_date.isoformat() if contract.expiration_date else None,
        "renewal_clause": contract.renewal_clause,
        "risk_score": contract.risk_score,
        "owner_id": str(contract.owner_id) if contract.owner_id else None,
        "created_at": contract.created_at.isoformat(),
        "updated_at": contract.updated_at.isoformat(),
    }


async def get_clm_overview(db: AsyncSession, org_id: uuid.UUID) -> dict[str, Any]:
    contracts_total = int(
        (await db.execute(select(func.count()).select_from(ClmContract).where(ClmContract.organization_id == org_id))).scalar()
        or 0
    )
    vendors_active = int(
        (
            await db.execute(
                select(func.count()).select_from(ClmVendor).where(
                    ClmVendor.organization_id == org_id,
                    ClmVendor.is_active.is_(True),
                )
            )
        ).scalar()
        or 0
    )
    expiring = int(
        (
            await db.execute(
                select(func.count()).select_from(ClmContract).where(
                    ClmContract.organization_id == org_id,
                    ClmContract.status == ClmContractStatus.EXPIRING.value,
                )
            )
        ).scalar()
        or 0
    )
    obligations_open = int(
        (
            await db.execute(
                select(func.count()).select_from(ClmObligation).where(
                    ClmObligation.organization_id == org_id,
                    ClmObligation.status == "open",
                )
            )
        ).scalar()
        or 0
    )
    sub_locations = int(
        (
            await db.execute(
                select(func.count()).select_from(ClmSubLocation).where(
                    ClmSubLocation.organization_id == org_id,
                    ClmSubLocation.is_active.is_(True),
                )
            )
        ).scalar()
        or 0
    )
    return {
        "contracts_total": contracts_total,
        "vendors_active": vendors_active,
        "expiring_soon": expiring,
        "obligations_open": obligations_open,
        "sub_locations": sub_locations,
    }
