"""
Blog management endpoints
Handles blog post CRUD with SEO team permissions
"""

from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.endpoints.auth import get_current_active_user
from app.core.permissions import PermissionChecker, FeatureGate
from app.db.database import get_db
from app.db.models import (
    BlogPost,
    BlogPostStatus,
    Organization,
    PlanType,
    Seat,
    User,
    UserRole,
)

router = APIRouter()


# Pydantic models
class BlogPostCreate(BaseModel):
    title: str
    excerpt: str
    content: str
    tags: List[str] = []
    status: str = "draft"
    meta_title: Optional[str] = None
    meta_description: Optional[str] = None


class BlogPostUpdate(BaseModel):
    title: Optional[str] = None
    excerpt: Optional[str] = None
    content: Optional[str] = None
    tags: Optional[List[str]] = None
    status: Optional[str] = None
    meta_title: Optional[str] = None
    meta_description: Optional[str] = None


class BlogPostOut(BaseModel):
    id: str
    title: str
    slug: str
    excerpt: str
    content: str
    author: str
    author_role: str
    published_at: Optional[str] = None
    status: str
    tags: List[str]
    read_time: str
    view_count: int
    created_at: str


# ==========================
# HELPERS
# ==========================

async def get_user_org_and_seat(
    user: User,
    db: AsyncSession,
) -> tuple[Organization, Seat]:
    """Get user's primary active organization and seat"""
    result = await db.execute(
        select(Seat)
        .where(Seat.user_id == user.id, Seat.is_active == True)
        .options(selectinload(Seat.organization).selectinload(Organization.subscription))
    )
    seat = result.scalar_one_or_none()
    if not seat:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No active organization membership"
        )
    return seat.organization, seat


def slugify(title: str) -> str:
    """Convert title to URL slug"""
    import re
    slug = re.sub(r'[^\w\s-]', '', title.lower())
    slug = re.sub(r'[\s-]+', '-', slug)
    return slug[:100]


def serialize_post(post: BlogPost) -> dict:
    """Serialize blog post for API response"""
    return {
        "id": str(post.id),
        "title": post.title,
        "slug": post.slug,
        "excerpt": post.excerpt,
        "content": post.content,
        "author": f"{post.author.first_name} {post.author.last_name}".strip() or post.author.email,
        "author_role": post.author.role.value if hasattr(post.author, 'role') else "unknown",
        "published_at": post.published_at.isoformat() if post.published_at else None,
        "status": post.status.value,
        "tags": post.tags or [],
        "read_time": f"{post.read_time_minutes} min read",
        "view_count": post.view_count,
        "created_at": post.created_at.isoformat(),
    }


# ==========================
# PERMISSION CHECK
# ==========================

async def check_blog_permission(
    user: User,
    db: AsyncSession,
    permission: str = "publish_blog",
) -> Organization:
    """Check if user has blog permission"""
    org, seat = await get_user_org_and_seat(user, db)
    
    # Check plan allows blog
    if not FeatureGate.has_feature(
        org.subscription.plan_type if org.subscription else PlanType.STANDARD,
        "blog"
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Blog feature not available on your plan"
        )
    
    # Check role permission
    if not PermissionChecker.has_permission(seat.role, permission):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Permission '{permission}' required"
        )
    
    return org


# ==========================
# ENDPOINTS
# ==========================

@router.get("/", response_model=dict)
async def list_posts(
    status: Optional[str] = None,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """List blog posts for the organization"""
    org, seat = await get_user_org_and_seat(current_user, db)
    
    # Check plan allows blog
    if not FeatureGate.has_feature(
        org.subscription.plan_type if org.subscription else PlanType.STANDARD,
        "blog"
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Blog feature not available on your plan"
        )
    
    query = select(BlogPost).where(BlogPost.organization_id == org.id)
    
    # Non-SEO/Admin users only see published posts
    if not PermissionChecker.has_permission(seat.role, "publish_blog"):
        query = query.where(BlogPost.status == BlogPostStatus.PUBLISHED)
    elif status:
        try:
            query = query.where(BlogPost.status == BlogPostStatus(status))
        except ValueError:
            pass
    
    query = query.order_by(BlogPost.created_at.desc())
    
    result = await db.execute(query.options(selectinload(BlogPost.author)))
    posts = result.scalars().all()
    
    return {
        "posts": [serialize_post(p) for p in posts],
        "can_manage": PermissionChecker.has_permission(seat.role, "publish_blog"),
    }


@router.get("/{post_id}", response_model=dict)
async def get_post(
    post_id: str,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Get a single blog post"""
    org, seat = await get_user_org_and_seat(current_user, db)
    
    # Check plan
    if not FeatureGate.has_feature(
        org.subscription.plan_type if org.subscription else PlanType.STANDARD,
        "blog"
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Blog feature not available on your plan"
        )
    
    result = await db.execute(
        select(BlogPost)
        .where(BlogPost.id == post_id, BlogPost.organization_id == org.id)
        .options(selectinload(BlogPost.author))
    )
    post = result.scalar_one_or_none()
    
    if not post:
        raise HTTPException(status_code=404, detail="Post not found")
    
    # Non-managers can only see published posts
    if post.status != BlogPostStatus.PUBLISHED:
        if not PermissionChecker.has_permission(seat.role, "publish_blog"):
            raise HTTPException(status_code=403, detail="Post not available")
    
    # Increment view count
    post.view_count += 1
    await db.commit()
    
    return serialize_post(post)


@router.post("/", response_model=dict)
async def create_post(
    data: BlogPostCreate,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Create a new blog post (SEO/Admin only)"""
    org = await check_blog_permission(current_user, db, "publish_blog")
    
    # Validate status
    try:
        post_status = BlogPostStatus(data.status.lower())
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid status. Use 'draft' or 'published'")
    
    # Calculate read time (rough estimate: 200 words per minute)
    word_count = len(data.content.split())
    read_time = max(1, word_count // 200)
    
    # Generate slug
    base_slug = slugify(data.title)
    slug = base_slug
    
    # Ensure slug uniqueness
    counter = 1
    while True:
        result = await db.execute(
            select(BlogPost).where(BlogPost.slug == slug)
        )
        if not result.scalar_one_or_none():
            break
        slug = f"{base_slug}-{counter}"
        counter += 1
    
    post = BlogPost(
        organization_id=org.id,
        author_id=current_user.id,
        title=data.title,
        slug=slug,
        excerpt=data.excerpt,
        content=data.content,
        meta_title=data.meta_title or data.title,
        meta_description=data.meta_description or data.excerpt[:160],
        tags=data.tags,
        status=post_status,
        read_time_minutes=read_time,
        published_at=datetime.utcnow() if post_status == BlogPostStatus.PUBLISHED else None,
    )
    
    db.add(post)
    await db.commit()
    await db.refresh(post)
    
    # Load author for serialization
    result = await db.execute(
        select(BlogPost).where(BlogPost.id == post.id).options(selectinload(BlogPost.author))
    )
    post = result.scalar_one()
    
    return serialize_post(post)


@router.patch("/{post_id}", response_model=dict)
async def update_post(
    post_id: str,
    data: BlogPostUpdate,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Update a blog post (SEO/Admin only)"""
    org = await check_blog_permission(current_user, db, "manage_blog")
    
    result = await db.execute(
        select(BlogPost).where(BlogPost.id == post_id, BlogPost.organization_id == org.id)
    )
    post = result.scalar_one_or_none()
    
    if not post:
        raise HTTPException(status_code=404, detail="Post not found")
    
    # Update fields
    if data.title is not None:
        post.title = data.title
    if data.excerpt is not None:
        post.excerpt = data.excerpt
    if data.content is not None:
        post.content = data.content
        # Recalculate read time
        word_count = len(data.content.split())
        post.read_time_minutes = max(1, word_count // 200)
    if data.tags is not None:
        post.tags = data.tags
    if data.meta_title is not None:
        post.meta_title = data.meta_title
    if data.meta_description is not None:
        post.meta_description = data.meta_description
    if data.status is not None:
        try:
            new_status = BlogPostStatus(data.status.lower())
            post.status = new_status
            if new_status == BlogPostStatus.PUBLISHED and not post.published_at:
                post.published_at = datetime.utcnow()
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid status")
    
    await db.commit()
    await db.refresh(post)
    
    # Load author
    result = await db.execute(
        select(BlogPost).where(BlogPost.id == post.id).options(selectinload(BlogPost.author))
    )
    post = result.scalar_one()
    
    return serialize_post(post)


@router.delete("/{post_id}", response_model=dict)
async def delete_post(
    post_id: str,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Delete a blog post (SEO/Admin only)"""
    org = await check_blog_permission(current_user, db, "manage_blog")
    
    result = await db.execute(
        select(BlogPost).where(BlogPost.id == post_id, BlogPost.organization_id == org.id)
    )
    post = result.scalar_one_or_none()
    
    if not post:
        raise HTTPException(status_code=404, detail="Post not found")
    
    await db.delete(post)
    await db.commit()
    
    return {"message": "Post deleted"}
