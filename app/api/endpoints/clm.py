"""CLM API — Enterprise contract repository."""

from __future__ import annotations

import io
import uuid
import zipfile
from datetime import date
from typing import List, Literal, Optional

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    UploadFile,
)
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from starlette.datastructures import Headers

from app.api.endpoints.sources import (
    ALLOWED_UPLOAD_EXTENSIONS,
    _file_extension,
    _max_upload_bytes,
)
from app.core.infrastructure import celery_is_ready
from app.db.database import get_db
from app.db.models import (
    ClmContract,
    ClmObligation,
    ClmSubLocation,
    ClmUserLocationAccess,
    ClmVendor,
    Seat,
)
from app.services.clm_access import (
    ACCESS_LEVELS,
    ClmAccessContext,
    get_clm_access_context,
    require_clm_admin,
    require_location_access,
)
from app.services.clm_service import (
    find_or_create_vendor,
    get_clm_overview,
    reprocess_contract_metadata,
    serialize_contract,
    upload_contract_document,
)

router = APIRouter()

MAX_BATCH_FILES = 50
MAX_ARCHIVE_BYTES = 100 * 1024 * 1024


class SubLocationCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    code: Optional[str] = Field(None, max_length=64)
    parent_id: Optional[str] = None


class VendorCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    contact_email: Optional[str] = None
    contact_phone: Optional[str] = None
    sub_location_id: Optional[str] = None


class ContractUpdate(BaseModel):
    title: Optional[str] = Field(None, min_length=1, max_length=500)
    status: Optional[Literal["active", "expiring", "expired", "archived"]] = None
    vendor_id: Optional[str] = None
    sub_location_id: Optional[str] = None
    effective_date: Optional[date] = None
    expiration_date: Optional[date] = None
    renewal_clause: Optional[str] = Field(None, max_length=4000)


class LocationAccessAssignment(BaseModel):
    sub_location_id: str
    access_level: str = "viewer"

    @field_validator("access_level")
    @classmethod
    def valid_access_level(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in ACCESS_LEVELS:
            raise ValueError(
                "access_level must be facility_owner, facility_manager, "
                "compliance_officer, or viewer"
            )
        return normalized


class LocationAccessUpdate(BaseModel):
    assignments: list[LocationAccessAssignment] = Field(default_factory=list)


def _serialize_obligation(item: ClmObligation) -> dict:
    return {
        "id": str(item.id),
        "contract_id": str(item.contract_id),
        "compliance_gap_id": str(item.compliance_gap_id)
        if item.compliance_gap_id
        else None,
        "task_id": str(item.task_id) if item.task_id else None,
        "title": item.title,
        "description": item.description,
        "obligation_type": item.obligation_type,
        "due_date": item.due_date.isoformat() if item.due_date else None,
        "severity": item.severity,
        "status": item.status,
        "created_at": item.created_at.isoformat(),
    }


async def _queue_or_process_contract(
    contract: ClmContract,
    context: ClmAccessContext,
    background_tasks: BackgroundTasks,
    *,
    celery_ready: Optional[bool] = None,
) -> None:
    ready = celery_is_ready() if celery_ready is None else celery_ready
    if ready:
        try:
            from app.tasks.clm import process_clm_contract_task

            process_clm_contract_task.delay(
                str(contract.id),
                str(context.organization.id),
                str(context.user.id),
            )
            return
        except Exception:
            pass
    from app.tasks.clm import process_clm_contract_background

    background_tasks.add_task(
        process_clm_contract_background,
        str(contract.id),
        str(context.organization.id),
        str(context.user.id),
    )


async def _parse_location_and_vendor(
    db: AsyncSession,
    context: ClmAccessContext,
    sub_location_id: Optional[str],
    vendor_id: Optional[str],
) -> tuple[Optional[uuid.UUID], Optional[uuid.UUID]]:
    sid = None
    vid = None
    if sub_location_id:
        try:
            sid = uuid.UUID(sub_location_id)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid sub_location_id")
    require_location_access(context, sid, write=True)
    if vendor_id:
        try:
            vid = uuid.UUID(vendor_id)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid vendor_id")
        vendor = await db.get(ClmVendor, vid)
        if (
            not vendor
            or vendor.organization_id != context.organization.id
            or not vendor.is_active
        ):
            raise HTTPException(status_code=400, detail="Vendor not found")
        if vendor.sub_location_id:
            require_location_access(context, vendor.sub_location_id, write=True)
    return sid, vid


def _safe_archive_members(raw: bytes) -> list[tuple[str, bytes]]:
    if len(raw) > _max_upload_bytes():
        raise HTTPException(status_code=413, detail="ZIP file exceeds upload size limit")
    try:
        archive = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile as exc:
        raise HTTPException(status_code=400, detail="Invalid ZIP archive") from exc

    members: list[tuple[str, bytes]] = []
    total_uncompressed = 0
    with archive:
        candidates = [
            item
            for item in archive.infolist()
            if not item.is_dir()
            and not item.filename.startswith("__MACOSX/")
            and not item.filename.split("/")[-1].startswith(".")
        ]
        if len(candidates) > MAX_BATCH_FILES:
            raise HTTPException(
                status_code=400,
                detail=f"ZIP contains more than {MAX_BATCH_FILES} files",
            )
        for item in candidates:
            path = item.filename.replace("\\", "/")
            parts = [part for part in path.split("/") if part]
            if (
                not parts
                or path.startswith("/")
                or any(part == ".." for part in parts)
                or item.flag_bits & 0x1
            ):
                raise HTTPException(
                    status_code=400,
                    detail=f"Unsafe ZIP entry: {item.filename}",
                )
            ext = _file_extension(parts[-1])
            if f".{ext}" not in ALLOWED_UPLOAD_EXTENSIONS:
                continue
            if item.file_size > _max_upload_bytes():
                raise HTTPException(
                    status_code=413,
                    detail=f"{parts[-1]} exceeds per-file upload limit",
                )
            total_uncompressed += item.file_size
            if total_uncompressed > MAX_ARCHIVE_BYTES:
                raise HTTPException(
                    status_code=413,
                    detail="ZIP uncompressed contents exceed 100 MB",
                )
            members.append((parts[-1], archive.read(item)))
    if not members:
        raise HTTPException(
            status_code=400,
            detail="ZIP contains no supported contract files",
        )
    return members


@router.get("/overview")
async def clm_overview(
    context: ClmAccessContext = Depends(get_clm_access_context),
    db: AsyncSession = Depends(get_db),
):
    accessible = (
        None
        if context.location_permissions is None
        else set(context.location_permissions)
    )
    return await get_clm_overview(db, context.organization.id, accessible)


@router.get("/sub-locations")
async def list_sub_locations(
    context: ClmAccessContext = Depends(get_clm_access_context),
    db: AsyncSession = Depends(get_db),
):
    organization = context.organization
    predicates = [
        ClmSubLocation.organization_id == organization.id,
        ClmSubLocation.is_active.is_(True),
    ]
    if context.location_permissions is not None:
        predicates.append(ClmSubLocation.id.in_(set(context.location_permissions)))
    result = await db.execute(
        select(ClmSubLocation)
        .where(*predicates)
        .order_by(ClmSubLocation.name.asc())
    )
    rows = result.scalars().all()
    return {
        "sub_locations": [
            {
                "id": str(r.id),
                "name": r.name,
                "code": r.code,
                "parent_id": str(r.parent_id) if r.parent_id else None,
            }
            for r in rows
        ]
    }


@router.post("/sub-locations")
async def create_sub_location(
    data: SubLocationCreate,
    context: ClmAccessContext = Depends(get_clm_access_context),
    db: AsyncSession = Depends(get_db),
):
    organization = context.organization
    parent_id = None
    if data.parent_id:
        try:
            parent_id = uuid.UUID(data.parent_id)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid parent_id")
        parent = await db.get(ClmSubLocation, parent_id)
        if not parent or parent.organization_id != organization.id:
            raise HTTPException(status_code=400, detail="Parent location not found")
        require_location_access(context, parent_id, write=True)
    elif not context.can_manage_access:
        raise HTTPException(
            status_code=403,
            detail="Only a CLM administrator can create a top-level location",
        )

    row = ClmSubLocation(
        organization_id=organization.id,
        parent_id=parent_id,
        name=data.name.strip(),
        code=data.code,
        is_active=True,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return {
        "sub_location": {
            "id": str(row.id),
            "name": row.name,
            "code": row.code,
            "parent_id": str(row.parent_id) if row.parent_id else None,
        }
    }


@router.get("/vendors")
async def list_vendors(
    context: ClmAccessContext = Depends(get_clm_access_context),
    db: AsyncSession = Depends(get_db),
    q: Optional[str] = None,
):
    organization = context.organization
    stmt = select(ClmVendor).where(
        ClmVendor.organization_id == organization.id,
        ClmVendor.is_active.is_(True),
    )
    if context.location_permissions is not None:
        stmt = stmt.where(
            ClmVendor.sub_location_id.in_(set(context.location_permissions))
        )
    if q and q.strip():
        needle = f"%{q.strip().lower()}%"
        stmt = stmt.where(ClmVendor.name.ilike(needle))
    stmt = stmt.order_by(ClmVendor.name.asc())
    result = await db.execute(stmt)
    vendors = result.scalars().all()
    return {
        "vendors": [
            {
                "id": str(v.id),
                "name": v.name,
                "contact_email": v.contact_email,
                "contact_phone": v.contact_phone,
                "sub_location_id": str(v.sub_location_id) if v.sub_location_id else None,
                "source": v.source,
            }
            for v in vendors
        ],
        "total": len(vendors),
    }


@router.post("/vendors")
async def create_vendor(
    data: VendorCreate,
    context: ClmAccessContext = Depends(get_clm_access_context),
    db: AsyncSession = Depends(get_db),
):
    organization = context.organization
    sub_location_id = None
    if data.sub_location_id:
        try:
            sub_location_id = uuid.UUID(data.sub_location_id)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid sub_location_id")
        location = await db.get(ClmSubLocation, sub_location_id)
        if (
            not location
            or location.organization_id != organization.id
            or not location.is_active
        ):
            raise HTTPException(status_code=400, detail="Sub-location not found")
    require_location_access(context, sub_location_id, write=True)

    vendor = await find_or_create_vendor(
        db,
        organization.id,
        data.name,
        sub_location_id=sub_location_id,
        source="manual",
    )
    if data.contact_email:
        vendor.contact_email = data.contact_email
    if data.contact_phone:
        vendor.contact_phone = data.contact_phone
    await db.commit()
    await db.refresh(vendor)
    return {
        "vendor": {
            "id": str(vendor.id),
            "name": vendor.name,
            "contact_email": vendor.contact_email,
            "contact_phone": vendor.contact_phone,
            "sub_location_id": str(vendor.sub_location_id) if vendor.sub_location_id else None,
            "source": vendor.source,
        }
    }


@router.get("/contracts")
async def list_contracts(
    context: ClmAccessContext = Depends(get_clm_access_context),
    db: AsyncSession = Depends(get_db),
    status_filter: Optional[str] = Query(None, alias="status"),
    sub_location_id: Optional[str] = None,
    q: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    organization = context.organization
    predicates = [ClmContract.organization_id == organization.id]
    if context.location_permissions is not None:
        predicates.append(
            ClmContract.sub_location_id.in_(set(context.location_permissions))
        )
    if status_filter:
        predicates.append(ClmContract.status == status_filter)
    if sub_location_id:
        try:
            sid = uuid.UUID(sub_location_id)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid sub_location_id")
        require_location_access(context, sid)
        predicates.append(ClmContract.sub_location_id == sid)
    if q and q.strip():
        predicates.append(ClmContract.title.ilike(f"%{q.strip()}%"))

    stmt = (
        select(ClmContract)
        .where(*predicates)
        .options(selectinload(ClmContract.vendor), selectinload(ClmContract.sub_location))
        .order_by(ClmContract.created_at.desc())
    )
    total = int(
        (
            await db.execute(
                select(func.count()).select_from(ClmContract).where(*predicates)
            )
        ).scalar_one()
        or 0
    )

    offset = (page - 1) * page_size
    result = await db.execute(stmt.offset(offset).limit(page_size))
    contracts = result.scalars().all()
    return {
        "contracts": [serialize_contract(c) for c in contracts],
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": max(1, (total + page_size - 1) // page_size) if total else 1,
    }


@router.get("/contracts/picker")
async def contracts_picker(
    context: ClmAccessContext = Depends(get_clm_access_context),
    db: AsyncSession = Depends(get_db),
    q: Optional[str] = None,
):
    """Active contracts for task submission attachment picker."""
    organization = context.organization
    predicates = [
        ClmContract.organization_id == organization.id,
        ClmContract.status.in_(("active", "expiring", "processing")),
    ]
    if context.location_permissions is not None:
        predicates.append(
            ClmContract.sub_location_id.in_(set(context.location_permissions))
        )
    stmt = (
        select(ClmContract)
        .where(*predicates)
        .options(selectinload(ClmContract.vendor))
        .order_by(ClmContract.title.asc())
        .limit(50)
    )
    if q and q.strip():
        needle = f"%{q.strip().lower()}%"
        stmt = stmt.where(ClmContract.title.ilike(needle))
    result = await db.execute(stmt)
    contracts = result.scalars().all()
    return {
        "contracts": [
            {
                "id": str(c.id),
                "document_id": str(c.document_id),
                "title": c.title,
                "contract_number": c.contract_number,
                "vendor_name": c.vendor.name if c.vendor else None,
                "status": c.status,
            }
            for c in contracts
        ]
    }


@router.get("/contracts/{contract_id}")
async def get_contract(
    contract_id: str,
    context: ClmAccessContext = Depends(get_clm_access_context),
    db: AsyncSession = Depends(get_db),
):
    organization = context.organization
    try:
        cid = uuid.UUID(contract_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid contract id")
    result = await db.execute(
        select(ClmContract)
        .where(ClmContract.id == cid, ClmContract.organization_id == organization.id)
        .options(
            selectinload(ClmContract.vendor),
            selectinload(ClmContract.sub_location),
            selectinload(ClmContract.document),
        )
    )
    contract = result.scalar_one_or_none()
    if not contract:
        raise HTTPException(status_code=404, detail="Contract not found")
    require_location_access(context, contract.sub_location_id)
    payload = serialize_contract(contract)
    payload["ai_extraction"] = contract.ai_extraction
    payload["document_title"] = contract.document.title if contract.document else None
    obligations = (
        (
            await db.execute(
                select(ClmObligation)
                .where(
                    ClmObligation.organization_id == organization.id,
                    ClmObligation.contract_id == contract.id,
                )
                .order_by(ClmObligation.due_date.asc().nullslast())
            )
        )
        .scalars()
        .all()
    )
    payload["obligations"] = [_serialize_obligation(item) for item in obligations]
    return {"contract": payload}


@router.post("/contracts/upload")
async def upload_contract(
    file: UploadFile = File(...),
    sub_location_id: Optional[str] = Form(None),
    vendor_id: Optional[str] = Form(None),
    context: ClmAccessContext = Depends(get_clm_access_context),
    db: AsyncSession = Depends(get_db),
):
    sid, vid = await _parse_location_and_vendor(
        db, context, sub_location_id, vendor_id
    )
    contract = await upload_contract_document(
        db,
        context.organization,
        context.user,
        file,
        sub_location_id=sid,
        vendor_id=vid,
    )
    await db.refresh(contract, attribute_names=["vendor", "sub_location"])
    return {"contract": serialize_contract(contract)}


@router.post("/contracts/bulk-upload")
async def bulk_upload_contracts(
    background_tasks: BackgroundTasks,
    files: List[UploadFile] = File(...),
    sub_location_id: Optional[str] = Form(None),
    vendor_id: Optional[str] = Form(None),
    context: ClmAccessContext = Depends(get_clm_access_context),
    db: AsyncSession = Depends(get_db),
):
    """Upload multiple agreements and/or ZIP archives with per-file results."""
    if not files or len(files) > MAX_BATCH_FILES:
        raise HTTPException(
            status_code=400,
            detail=f"Select between 1 and {MAX_BATCH_FILES} files",
        )
    sid, vid = await _parse_location_and_vendor(
        db, context, sub_location_id, vendor_id
    )

    expanded: list[tuple[str, bytes, str]] = []
    rejected: list[dict] = []
    for incoming in files:
        filename = incoming.filename or "contract"
        raw = await incoming.read()
        if filename.lower().endswith(".zip"):
            try:
                expanded.extend(
                    (name, content, "application/octet-stream")
                    for name, content in _safe_archive_members(raw)
                )
            except HTTPException as exc:
                rejected.append({"filename": filename, "error": str(exc.detail)})
        else:
            expanded.append(
                (filename, raw, incoming.content_type or "application/octet-stream")
            )

    if len(expanded) > MAX_BATCH_FILES:
        raise HTTPException(
            status_code=400,
            detail=f"Batch expands to more than {MAX_BATCH_FILES} contract files",
        )

    accepted: list[dict] = []
    celery_ready = celery_is_ready()
    for filename, raw, content_type in expanded:
        upload = UploadFile(
            file=io.BytesIO(raw),
            filename=filename,
            headers=Headers({"content-type": content_type}),
        )
        try:
            contract = await upload_contract_document(
                db,
                context.organization,
                context.user,
                upload,
                sub_location_id=sid,
                vendor_id=vid,
                process_immediately=False,
            )
            await db.refresh(contract, attribute_names=["vendor", "sub_location"])
            accepted.append(serialize_contract(contract))
            await _queue_or_process_contract(
                contract,
                context,
                background_tasks,
                celery_ready=celery_ready,
            )
        except HTTPException as exc:
            await db.rollback()
            rejected.append({"filename": filename, "error": str(exc.detail)})
        except Exception:
            await db.rollback()
            rejected.append(
                {"filename": filename, "error": "Contract could not be processed"}
            )

    return {
        "accepted": accepted,
        "rejected": rejected,
        "total": len(accepted) + len(rejected),
        "accepted_count": len(accepted),
        "rejected_count": len(rejected),
    }


@router.patch("/contracts/{contract_id}")
async def update_contract(
    contract_id: str,
    data: ContractUpdate,
    context: ClmAccessContext = Depends(get_clm_access_context),
    db: AsyncSession = Depends(get_db),
):
    try:
        cid = uuid.UUID(contract_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid contract id")
    result = await db.execute(
        select(ClmContract)
        .where(
            ClmContract.id == cid,
            ClmContract.organization_id == context.organization.id,
        )
        .options(selectinload(ClmContract.vendor), selectinload(ClmContract.sub_location))
    )
    contract = result.scalar_one_or_none()
    if not contract:
        raise HTTPException(status_code=404, detail="Contract not found")
    require_location_access(context, contract.sub_location_id, write=True)

    updates = data.model_dump(exclude_unset=True)
    if "sub_location_id" in updates:
        value = updates.pop("sub_location_id")
        new_location_id = None
        if value:
            try:
                new_location_id = uuid.UUID(value)
            except ValueError:
                raise HTTPException(status_code=400, detail="Invalid sub_location_id")
            location = await db.get(ClmSubLocation, new_location_id)
            if (
                not location
                or location.organization_id != context.organization.id
                or not location.is_active
            ):
                raise HTTPException(status_code=400, detail="Sub-location not found")
        require_location_access(context, new_location_id, write=True)
        contract.sub_location_id = new_location_id
    if "vendor_id" in updates:
        value = updates.pop("vendor_id")
        new_vendor_id = None
        if value:
            try:
                new_vendor_id = uuid.UUID(value)
            except ValueError:
                raise HTTPException(status_code=400, detail="Invalid vendor_id")
            vendor = await db.get(ClmVendor, new_vendor_id)
            if not vendor or vendor.organization_id != context.organization.id:
                raise HTTPException(status_code=400, detail="Vendor not found")
        contract.vendor_id = new_vendor_id
    for key, value in updates.items():
        setattr(contract, key, value)
    await db.commit()
    await db.refresh(contract, attribute_names=["vendor", "sub_location"])
    return {"contract": serialize_contract(contract)}


@router.post("/contracts/{contract_id}/reprocess")
async def reprocess_contract(
    contract_id: str,
    context: ClmAccessContext = Depends(get_clm_access_context),
    db: AsyncSession = Depends(get_db),
):
    """Re-run vendor/date/obligation extraction for a contract."""
    try:
        cid = uuid.UUID(contract_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid contract id")
    result = await db.execute(
        select(ClmContract).where(
            ClmContract.id == cid,
            ClmContract.organization_id == context.organization.id,
        )
    )
    contract = result.scalar_one_or_none()
    if not contract:
        raise HTTPException(status_code=404, detail="Contract not found")
    require_location_access(context, contract.sub_location_id, write=True)

    updated = await reprocess_contract_metadata(
        db, contract.id, context.organization, context.user
    )
    return {"contract": serialize_contract(updated)}


@router.get("/obligations")
async def list_obligations(
    contract_id: Optional[str] = None,
    status_filter: Optional[str] = Query(None, alias="status"),
    context: ClmAccessContext = Depends(get_clm_access_context),
    db: AsyncSession = Depends(get_db),
):
    contract_predicates = [
        ClmContract.organization_id == context.organization.id
    ]
    if context.location_permissions is not None:
        contract_predicates.append(
            ClmContract.sub_location_id.in_(set(context.location_permissions))
        )
    if contract_id:
        try:
            cid = uuid.UUID(contract_id)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid contract id")
        contract_predicates.append(ClmContract.id == cid)
    visible_contract_ids = select(ClmContract.id).where(*contract_predicates)
    predicates = [
        ClmObligation.organization_id == context.organization.id,
        ClmObligation.contract_id.in_(visible_contract_ids),
    ]
    if status_filter:
        predicates.append(ClmObligation.status == status_filter)
    obligations = (
        (
            await db.execute(
                select(ClmObligation)
                .where(*predicates)
                .order_by(
                    ClmObligation.due_date.asc().nullslast(),
                    ClmObligation.created_at.desc(),
                )
            )
        )
        .scalars()
        .all()
    )
    return {
        "obligations": [_serialize_obligation(item) for item in obligations],
        "total": len(obligations),
    }


@router.get("/location-access")
async def list_location_access(
    context: ClmAccessContext = Depends(get_clm_access_context),
    db: AsyncSession = Depends(get_db),
):
    assignment_stmt = (
        select(ClmUserLocationAccess)
        .where(ClmUserLocationAccess.organization_id == context.organization.id)
        .order_by(ClmUserLocationAccess.created_at.asc())
    )
    if not context.can_manage_access:
        assignment_stmt = assignment_stmt.where(
            ClmUserLocationAccess.user_id == context.user.id
        )
    assignments = (await db.execute(assignment_stmt)).scalars().all()

    members = []
    if context.can_manage_access:
        seats = (
            (
                await db.execute(
                    select(Seat)
                    .where(
                        Seat.organization_id == context.organization.id,
                        Seat.is_active.is_(True),
                    )
                    .options(selectinload(Seat.user))
                    .order_by(Seat.created_at.asc())
                )
            )
            .scalars()
            .all()
        )
        members = [
            {
                "user_id": str(seat.user_id),
                "name": f"{seat.user.first_name} {seat.user.last_name}".strip()
                or seat.user.email,
                "email": seat.user.email,
                "role": seat.role.value,
            }
            for seat in seats
        ]
    else:
        members = [
            {
                "user_id": str(context.user.id),
                "name": f"{context.user.first_name} {context.user.last_name}".strip()
                or context.user.email,
                "email": context.user.email,
                "role": context.seat.role.value,
            }
        ]

    total_assignments = int(
        (
            await db.execute(
                select(func.count())
                .select_from(ClmUserLocationAccess)
                .where(
                    ClmUserLocationAccess.organization_id == context.organization.id
                )
            )
        ).scalar_one()
        or 0
    )
    return {
        "members": members,
        "assignments": [
            {
                "id": str(item.id),
                "user_id": str(item.user_id),
                "sub_location_id": str(item.sub_location_id),
                "access_level": item.access_level,
            }
            for item in assignments
        ],
        "acl_enforced": total_assignments > 0,
        "can_manage": context.can_manage_access,
    }


@router.put("/location-access/{user_id}")
async def replace_location_access(
    user_id: str,
    data: LocationAccessUpdate,
    context: ClmAccessContext = Depends(get_clm_access_context),
    db: AsyncSession = Depends(get_db),
):
    require_clm_admin(context)
    try:
        target_user_id = uuid.UUID(user_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid user id")
    target_seat = (
        await db.execute(
            select(Seat).where(
                Seat.organization_id == context.organization.id,
                Seat.user_id == target_user_id,
                Seat.is_active.is_(True),
            )
        )
    ).scalar_one_or_none()
    if not target_seat:
        raise HTTPException(status_code=404, detail="Workspace member not found")

    normalized: dict[uuid.UUID, str] = {}
    for assignment in data.assignments:
        try:
            location_id = uuid.UUID(assignment.sub_location_id)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid sub_location_id")
        location = await db.get(ClmSubLocation, location_id)
        if (
            not location
            or location.organization_id != context.organization.id
            or not location.is_active
        ):
            raise HTTPException(status_code=400, detail="Sub-location not found")
        normalized[location_id] = assignment.access_level

    await db.execute(
        delete(ClmUserLocationAccess).where(
            ClmUserLocationAccess.organization_id == context.organization.id,
            ClmUserLocationAccess.user_id == target_user_id,
        )
    )
    for location_id, access_level in normalized.items():
        db.add(
            ClmUserLocationAccess(
                organization_id=context.organization.id,
                user_id=target_user_id,
                sub_location_id=location_id,
                access_level=access_level,
                granted_by_id=context.user.id,
            )
        )
    await db.commit()
    return {
        "user_id": str(target_user_id),
        "assignments": [
            {
                "sub_location_id": str(location_id),
                "access_level": access_level,
            }
            for location_id, access_level in normalized.items()
        ],
    }
