"""
Public platform blog (read-only).

Serves only published posts from the platform-admin blog store.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
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


def _published_conditions(
    category: Optional[str],
    q: Optional[str],
) -> list:
    conditions = [PlatformBlogPost.status == PlatformBlogStatus.PUBLISHED]

    if category:
        try:
            cat = PlatformBlogCategory(category)
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid category")
        conditions.append(PlatformBlogPost.category == cat)

    if q and q.strip():
        needle = f"%{q.strip().lower()}%"
        conditions.append(
            (PlatformBlogPost.title.ilike(needle))
            | (PlatformBlogPost.excerpt.ilike(needle))
        )

    return conditions


async def _category_counts(db: AsyncSession, q: Optional[str]) -> dict[str, int]:
    conditions = _published_conditions(None, q)
    counts_stmt = (
        select(PlatformBlogPost.category, func.count())
        .where(*conditions)
        .group_by(PlatformBlogPost.category)
    )
    result = await db.execute(counts_stmt)
    counts = {cat.value: 0 for cat in PlatformBlogCategory}
    for cat, count in result.all():
        counts[cat.value] = int(count)
    return counts


@public_router.get("")
async def list_published_posts(
    category: Optional[str] = None,
    q: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: Optional[int] = Query(None, ge=1, le=100),
    include_category_counts: bool = Query(False),
    db: AsyncSession = Depends(get_db),
):
    conditions = _published_conditions(category, q)

    total = int(
        (
            await db.execute(
                select(func.count()).select_from(PlatformBlogPost).where(*conditions)
            )
        ).scalar()
        or 0
    )

    if page_size is not None:
        total_pages = max(1, (total + page_size - 1) // page_size) if total > 0 else 1
        safe_page = min(page, total_pages) if total > 0 else 1
        offset = (safe_page - 1) * page_size
        response_page = safe_page
        response_page_size = page_size
    else:
        total_pages = 1
        safe_page = 1
        offset = 0
        response_page = 1
        response_page_size = total

    data_stmt = (
        select(PlatformBlogPost)
        .where(*conditions)
        .options(selectinload(PlatformBlogPost.author))
        .order_by(
            PlatformBlogPost.published_at.desc().nullslast(),
            PlatformBlogPost.created_at.desc(),
        )
    )

    if page_size is not None:
        data_stmt = data_stmt.offset(offset).limit(page_size)

    result = await db.execute(data_stmt)
    posts = result.scalars().all()

    response: dict = {
        "posts": [_post_payload(p) for p in posts],
        "total": total,
        "page": response_page,
        "page_size": response_page_size,
        "total_pages": total_pages,
    }

    if include_category_counts:
        response["category_counts"] = await _category_counts(db, q)

    return response


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
    if post.category != PlatformBlogCategory.BLOG:
        raise HTTPException(status_code=404, detail="Post not found")
    return {"post": _post_payload(post)}
