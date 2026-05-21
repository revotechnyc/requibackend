"""
Public chat share links and import (continue) flow.
"""

import uuid
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.endpoints.auth import get_current_active_user
from app.db.database import get_db
from app.db.models import User
from app.services.chat_share_service import (
    build_share_url,
    create_or_get_share,
    get_public_share,
    import_shared_conversation,
)

public_router = APIRouter()
router = APIRouter()


class ShareMessageSnapshot(BaseModel):
    role: str
    content: str
    created_at: Optional[str] = None


class PublicShareResponse(BaseModel):
    share_token: str
    title: Optional[str]
    messages: List[ShareMessageSnapshot]
    created_at: str
    share_url: str


class CreateShareResponse(BaseModel):
    share_token: str
    share_url: str


class ContinueShareResponse(BaseModel):
    conversation_id: str
    title: Optional[str]
    is_shared_import: bool


@public_router.get("/public/chat-share/{share_token}", response_model=PublicShareResponse)
async def get_public_chat_share(
    share_token: str,
    db: AsyncSession = Depends(get_db),
):
    """Read-only shared conversation (no auth)."""
    share = await get_public_share(db, share_token)
    if not share:
        raise HTTPException(status_code=404, detail="Share link not found or expired")

    messages = [
        ShareMessageSnapshot(
            role=m.get("role", "user"),
            content=m.get("content", ""),
            created_at=m.get("created_at"),
        )
        for m in (share.snapshot_messages or [])
    ]

    return PublicShareResponse(
        share_token=share.share_token,
        title=share.title,
        messages=messages,
        created_at=share.created_at.isoformat(),
        share_url=build_share_url(share.share_token),
    )


@router.post("/chat-share/{share_token}/continue", response_model=ContinueShareResponse)
async def continue_from_share(
    share_token: str,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Import shared snapshot into the current user's conversations."""
    try:
        conversation = await import_shared_conversation(
            db,
            share_token=share_token,
            user_id=current_user.id,
        )
    except ValueError as e:
        msg = str(e)
        if "not found" in msg.lower():
            raise HTTPException(status_code=404, detail=msg)
        raise HTTPException(status_code=400, detail=msg)

    return ContinueShareResponse(
        conversation_id=str(conversation.id),
        title=conversation.title,
        is_shared_import=conversation.is_shared_import,
    )
