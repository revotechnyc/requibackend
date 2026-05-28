"""
Platform-admin blog management (marketing blog / guides / resources).
Separate from organization-scoped /blog endpoints.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.platform_admin_roles import PlatformAdminRole
from app.core.platform_admin_security import get_current_platform_admin
from app.db.database import get_db
from app.db.models import (
    PlatformAdmin,
    PlatformBlogCategory,
    PlatformBlogPost,
    PlatformBlogStatus,
)

router = APIRouter()


def _is_super_admin(admin: PlatformAdmin) -> bool:
    return admin.role == PlatformAdminRole.SUPER_ADMIN.value


def _can_write(admin: PlatformAdmin) -> bool:
    return admin.role in (
        PlatformAdminRole.SUPER_ADMIN.value,
        PlatformAdminRole.BLOG_ADMIN.value,
        PlatformAdminRole.BLOG_EDITOR.value,
        PlatformAdminRole.BLOG_WRITER.value,
    )


def _can_edit_any(admin: PlatformAdmin) -> bool:
    return admin.role in (
        PlatformAdminRole.SUPER_ADMIN.value,
        PlatformAdminRole.BLOG_ADMIN.value,
        PlatformAdminRole.BLOG_EDITOR.value,
    )


def _can_publish(admin: PlatformAdmin) -> bool:
    return admin.role in (
        PlatformAdminRole.SUPER_ADMIN.value,
        PlatformAdminRole.BLOG_ADMIN.value,
        PlatformAdminRole.BLOG_EDITOR.value,
    )


def _ensure_allowed_category(raw: str) -> PlatformBlogCategory:
    try:
        return PlatformBlogCategory(raw)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid category")


def _ensure_allowed_status(raw: str) -> PlatformBlogStatus:
    try:
        return PlatformBlogStatus(raw)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid status")


def _slugify(title: str) -> str:
    import re

    slug = re.sub(r"[^\w\s-]", "", title.lower())
    slug = re.sub(r"[\s-]+", "-", slug).strip("-")
    return slug[:120] or "post"


async def _ensure_unique_slug(db: AsyncSession, base_slug: str) -> str:
    slug = base_slug
    i = 0
    while True:
        existing = await db.execute(select(PlatformBlogPost).where(PlatformBlogPost.slug == slug))
        if not existing.scalar_one_or_none():
            return slug
        i += 1
        slug = f"{base_slug}-{i}"[:120]


def _post_payload(post: PlatformBlogPost) -> dict:
    author = post.author
    return {
        "id": str(post.id),
        "title": post.title,
        "slug": post.slug,
        "excerpt": post.excerpt,
        "content": post.content,
        "cover_image_url": post.cover_image_url,
        "category": post.category.value,
        "status": post.status.value,
        "tags": post.tags or [],
        "meta_title": post.meta_title,
        "meta_description": post.meta_description,
        "published_at": post.published_at.isoformat() if post.published_at else None,
        "scheduled_for": post.scheduled_for.isoformat() if post.scheduled_for else None,
        "read_time_minutes": post.read_time_minutes,
        "view_count": post.view_count,
        "created_at": post.created_at.isoformat(),
        "updated_at": post.updated_at.isoformat(),
        "author": {
            "id": str(author.id),
            "email": author.email,
            "name": f"{author.first_name} {author.last_name}".strip() or author.email,
            "role": author.role,
        }
        if author
        else None,
        "last_edited_by_id": str(post.last_edited_by_id) if post.last_edited_by_id else None,
        "published_by_id": str(post.published_by_id) if post.published_by_id else None,
    }


class PlatformBlogCreate(BaseModel):
    title: str
    excerpt: str
    content: str
    category: str = PlatformBlogCategory.BLOG.value
    tags: list[str] = []
    cover_image_url: Optional[str] = None
    meta_title: Optional[str] = None
    meta_description: Optional[str] = None

    @field_validator("title")
    @classmethod
    def _title(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("Title is required")
        return v.strip()

    @field_validator("excerpt")
    @classmethod
    def _excerpt(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("Excerpt is required")
        return v.strip()


class PlatformBlogUpdate(BaseModel):
    title: Optional[str] = None
    excerpt: Optional[str] = None
    content: Optional[str] = None
    category: Optional[str] = None
    tags: Optional[list[str]] = None
    cover_image_url: Optional[str] = None
    meta_title: Optional[str] = None
    meta_description: Optional[str] = None


class PlatformBlogPublish(BaseModel):
    scheduled_for: Optional[datetime] = None


@router.get("")
async def list_posts(
    category: Optional[str] = None,
    status: Optional[str] = None,
    q: Optional[str] = None,
    admin: PlatformAdmin = Depends(get_current_platform_admin),
    db: AsyncSession = Depends(get_db),
):
    if not _can_write(admin):
        raise HTTPException(status_code=403, detail="Blog access required")

    query = select(PlatformBlogPost).options(selectinload(PlatformBlogPost.author))

    if category:
        cat = _ensure_allowed_category(category)
        query = query.where(PlatformBlogPost.category == cat)

    if status:
        st = _ensure_allowed_status(status)
        query = query.where(PlatformBlogPost.status == st)

    if q:
        needle = f"%{q.strip().lower()}%"
        query = query.where(
            (PlatformBlogPost.title.ilike(needle))
            | (PlatformBlogPost.excerpt.ilike(needle))
        )

    # Writers only see their own drafts + all published/scheduled/archived
    if admin.role == PlatformAdminRole.BLOG_WRITER.value:
        query = query.where(
            (PlatformBlogPost.status != PlatformBlogStatus.DRAFT)
            | (PlatformBlogPost.author_id == admin.id)
        )

    query = query.order_by(PlatformBlogPost.created_at.desc())
    result = await db.execute(query)
    posts = result.scalars().all()
    return {
        "posts": [_post_payload(p) for p in posts],
        "capabilities": {
            "can_publish": _can_publish(admin),
            "can_edit_any": _can_edit_any(admin),
        },
    }


@router.get("/{post_id}")
async def get_post(
    post_id: UUID,
    admin: PlatformAdmin = Depends(get_current_platform_admin),
    db: AsyncSession = Depends(get_db),
):
    if not _can_write(admin):
        raise HTTPException(status_code=403, detail="Blog access required")

    result = await db.execute(
        select(PlatformBlogPost)
        .where(PlatformBlogPost.id == post_id)
        .options(selectinload(PlatformBlogPost.author))
    )
    post = result.scalar_one_or_none()
    if not post:
        raise HTTPException(status_code=404, detail="Post not found")

    if admin.role == PlatformAdminRole.BLOG_WRITER.value and post.status == PlatformBlogStatus.DRAFT and post.author_id != admin.id:
        raise HTTPException(status_code=403, detail="Not allowed")

    return {"post": _post_payload(post)}


@router.post("", status_code=201)
async def create_post(
    body: PlatformBlogCreate,
    admin: PlatformAdmin = Depends(get_current_platform_admin),
    db: AsyncSession = Depends(get_db),
):
    if not _can_write(admin):
        raise HTTPException(status_code=403, detail="Blog access required")

    cat = _ensure_allowed_category(body.category)
    base_slug = _slugify(body.title)
    slug = await _ensure_unique_slug(db, base_slug)

    post = PlatformBlogPost(
        author_id=admin.id,
        title=body.title,
        slug=slug,
        excerpt=body.excerpt,
        content=body.content,
        cover_image_url=body.cover_image_url,
        category=cat,
        status=PlatformBlogStatus.DRAFT,
        tags=body.tags or [],
        meta_title=body.meta_title,
        meta_description=body.meta_description,
        last_edited_by_id=admin.id,
    )
    db.add(post)
    await db.commit()
    await db.refresh(post)

    reload = await db.execute(
        select(PlatformBlogPost)
        .where(PlatformBlogPost.id == post.id)
        .options(selectinload(PlatformBlogPost.author))
    )
    post = reload.scalar_one()
    return {"post": _post_payload(post)}


@router.patch("/{post_id}")
async def update_post(
    post_id: UUID,
    body: PlatformBlogUpdate,
    admin: PlatformAdmin = Depends(get_current_platform_admin),
    db: AsyncSession = Depends(get_db),
):
    if not _can_write(admin):
        raise HTTPException(status_code=403, detail="Blog access required")

    result = await db.execute(select(PlatformBlogPost).where(PlatformBlogPost.id == post_id))
    post = result.scalar_one_or_none()
    if not post:
        raise HTTPException(status_code=404, detail="Post not found")

    # Writers can only edit their own drafts
    if admin.role == PlatformAdminRole.BLOG_WRITER.value:
        if post.author_id != admin.id or post.status != PlatformBlogStatus.DRAFT:
            raise HTTPException(status_code=403, detail="Writers can only edit their own drafts")

    # Editors/admin/super can edit anything
    if not _can_edit_any(admin) and post.author_id != admin.id:
        raise HTTPException(status_code=403, detail="Not allowed")

    if body.title is not None:
        post.title = body.title.strip()
    if body.excerpt is not None:
        post.excerpt = body.excerpt.strip()
    if body.content is not None:
        post.content = body.content
    if body.category is not None:
        post.category = _ensure_allowed_category(body.category)
    if body.tags is not None:
        post.tags = body.tags
    if body.cover_image_url is not None:
        post.cover_image_url = body.cover_image_url
    if body.meta_title is not None:
        post.meta_title = body.meta_title
    if body.meta_description is not None:
        post.meta_description = body.meta_description

    post.last_edited_by_id = admin.id
    post.updated_at = datetime.utcnow()

    await db.commit()
    return {"success": True}


@router.post("/{post_id}/publish")
async def publish_post(
    post_id: UUID,
    body: PlatformBlogPublish,
    admin: PlatformAdmin = Depends(get_current_platform_admin),
    db: AsyncSession = Depends(get_db),
):
    if not _can_publish(admin):
        raise HTTPException(status_code=403, detail="Publish permission required")

    result = await db.execute(select(PlatformBlogPost).where(PlatformBlogPost.id == post_id))
    post = result.scalar_one_or_none()
    if not post:
        raise HTTPException(status_code=404, detail="Post not found")

    now = datetime.utcnow()
    post.published_by_id = admin.id
    post.last_edited_by_id = admin.id

    if body.scheduled_for and body.scheduled_for > now:
        post.status = PlatformBlogStatus.SCHEDULED
        post.scheduled_for = body.scheduled_for
        post.published_at = None
    else:
        post.status = PlatformBlogStatus.PUBLISHED
        post.published_at = now
        post.scheduled_for = None

    await db.commit()
    return {"success": True}


@router.post("/{post_id}/unpublish")
async def unpublish_post(
    post_id: UUID,
    admin: PlatformAdmin = Depends(get_current_platform_admin),
    db: AsyncSession = Depends(get_db),
):
    if not _can_publish(admin):
        raise HTTPException(status_code=403, detail="Publish permission required")

    result = await db.execute(select(PlatformBlogPost).where(PlatformBlogPost.id == post_id))
    post = result.scalar_one_or_none()
    if not post:
        raise HTTPException(status_code=404, detail="Post not found")

    post.status = PlatformBlogStatus.DRAFT
    post.published_at = None
    post.scheduled_for = None
    post.last_edited_by_id = admin.id
    await db.commit()
    return {"success": True}


@router.delete("/{post_id}")
async def delete_post(
    post_id: UUID,
    admin: PlatformAdmin = Depends(get_current_platform_admin),
    db: AsyncSession = Depends(get_db),
):
    # Only super admin or blog admin can delete
    if admin.role not in (PlatformAdminRole.SUPER_ADMIN.value, PlatformAdminRole.BLOG_ADMIN.value):
        raise HTTPException(status_code=403, detail="Delete permission required")

    result = await db.execute(select(PlatformBlogPost).where(PlatformBlogPost.id == post_id))
    post = result.scalar_one_or_none()
    if not post:
        raise HTTPException(status_code=404, detail="Post not found")

    await db.delete(post)
    await db.commit()
    return {"success": True}

