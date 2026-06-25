"""CLM API — Enterprise contract repository."""

from __future__ import annotations

import uuid
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.endpoints.auth import get_current_active_user
from app.core.permissions import require_feature_dependency
from app.db.database import get_db
from app.db.models import (
    ClmContract,
    ClmSubLocation,
    ClmVendor,
    Organization,
    User,
)
from app.services.clm_service import (
    find_or_create_vendor,
    get_clm_overview,
    serialize_contract,
    upload_contract_document,
)

router = APIRouter()

CLM_FEATURE = "clm"
require_clm = require_feature_dependency(CLM_FEATURE)


class SubLocationCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    code: Optional[str] = Field(None, max_length=64)
    parent_id: Optional[str] = None


class VendorCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    contact_email: Optional[str] = None
    contact_phone: Optional[str] = None
    sub_location_id: Optional[str] = None


@router.get("/overview")
async def clm_overview(
    organization: Organization = Depends(require_clm),
    db: AsyncSession = Depends(get_db),
):
    return await get_clm_overview(db, organization.id)


@router.get("/sub-locations")
async def list_sub_locations(
    organization: Organization = Depends(require_clm),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(ClmSubLocation)
        .where(
            ClmSubLocation.organization_id == organization.id,
            ClmSubLocation.is_active.is_(True),
        )
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
    organization: Organization = Depends(require_clm),
    db: AsyncSession = Depends(get_db),
):
    parent_id = None
    if data.parent_id:
        try:
            parent_id = uuid.UUID(data.parent_id)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid parent_id")
        parent = await db.get(ClmSubLocation, parent_id)
        if not parent or parent.organization_id != organization.id:
            raise HTTPException(status_code=400, detail="Parent location not found")

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
    organization: Organization = Depends(require_clm),
    db: AsyncSession = Depends(get_db),
    q: Optional[str] = None,
):
    stmt = select(ClmVendor).where(
        ClmVendor.organization_id == organization.id,
        ClmVendor.is_active.is_(True),
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
    organization: Organization = Depends(require_clm),
    db: AsyncSession = Depends(get_db),
):
    sub_location_id = None
    if data.sub_location_id:
        try:
            sub_location_id = uuid.UUID(data.sub_location_id)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid sub_location_id")

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
    organization: Organization = Depends(require_clm),
    db: AsyncSession = Depends(get_db),
    status_filter: Optional[str] = Query(None, alias="status"),
    sub_location_id: Optional[str] = None,
    q: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    stmt = (
        select(ClmContract)
        .where(ClmContract.organization_id == organization.id)
        .options(selectinload(ClmContract.vendor), selectinload(ClmContract.sub_location))
        .order_by(ClmContract.created_at.desc())
    )
    if status_filter:
        stmt = stmt.where(ClmContract.status == status_filter)
    if sub_location_id:
        try:
            sid = uuid.UUID(sub_location_id)
            stmt = stmt.where(ClmContract.sub_location_id == sid)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid sub_location_id")
    if q and q.strip():
        needle = f"%{q.strip().lower()}%"
        stmt = stmt.where(ClmContract.title.ilike(needle))

    count_stmt = select(ClmContract).where(ClmContract.organization_id == organization.id)
    if status_filter:
        count_stmt = count_stmt.where(ClmContract.status == status_filter)
    total = len((await db.execute(count_stmt)).scalars().all())

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
    organization: Organization = Depends(require_clm),
    db: AsyncSession = Depends(get_db),
    q: Optional[str] = None,
):
    """Active contracts for task submission attachment picker."""
    stmt = (
        select(ClmContract)
        .where(
            ClmContract.organization_id == organization.id,
            ClmContract.status.in_(("active", "expiring", "processing")),
        )
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
    organization: Organization = Depends(require_clm),
    db: AsyncSession = Depends(get_db),
):
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
    payload = serialize_contract(contract)
    payload["ai_extraction"] = contract.ai_extraction
    payload["document_title"] = contract.document.title if contract.document else None
    return {"contract": payload}


@router.post("/contracts/upload")
async def upload_contract(
    file: UploadFile = File(...),
    sub_location_id: Optional[str] = Form(None),
    vendor_id: Optional[str] = Form(None),
    organization: Organization = Depends(require_clm),
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    sid = None
    vid = None
    if sub_location_id:
        try:
            sid = uuid.UUID(sub_location_id)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid sub_location_id")
    if vendor_id:
        try:
            vid = uuid.UUID(vendor_id)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid vendor_id")

    contract = await upload_contract_document(
        db,
        organization,
        current_user,
        file,
        sub_location_id=sid,
        vendor_id=vid,
    )
    await db.refresh(contract, attribute_names=["vendor", "sub_location"])
    return {"contract": serialize_contract(contract)}
