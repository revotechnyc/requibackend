"""
Public platform blog (read-only).

Serves only published posts from the platform-admin blog store.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.database import get_db
from app.db.models import PlatformBlogCategory, PlatformBlogPost, PlatformBlogStatus

public_router = APIRouter()


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
        "read_time_minutes": post.read_time_minutes,
        "view_count": post.view_count,
        "created_at": post.created_at.isoformat(),
        "author": {
            "name": f"{author.first_name} {author.last_name}".strip() or author.email,
        }
        if author
        else None,
    }


@public_router.get("")
async def list_published_posts(
    category: Optional[str] = None,
    q: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
):
    query = (
        select(PlatformBlogPost)
        .where(PlatformBlogPost.status == PlatformBlogStatus.PUBLISHED)
        .options(selectinload(PlatformBlogPost.author))
        .order_by(PlatformBlogPost.published_at.desc().nullslast(), PlatformBlogPost.created_at.desc())
    )

    if category:
        try:
            cat = PlatformBlogCategory(category)
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid category")
        query = query.where(PlatformBlogPost.category == cat)

    if q:
        needle = f"%{q.strip().lower()}%"
        query = query.where(
            (PlatformBlogPost.title.ilike(needle))
            | (PlatformBlogPost.excerpt.ilike(needle))
        )

    result = await db.execute(query)
    posts = result.scalars().all()
    return {"posts": [_post_payload(p) for p in posts]}


@public_router.get("/{slug}")
async def get_published_post_by_slug(
    slug: str,
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(PlatformBlogPost)
        .where(
            PlatformBlogPost.slug == slug,
            PlatformBlogPost.status == PlatformBlogStatus.PUBLISHED,
        )
        .options(selectinload(PlatformBlogPost.author))
    )
    post = result.scalar_one_or_none()
    if not post:
        raise HTTPException(status_code=404, detail="Post not found")
    return {"post": _post_payload(post)}

