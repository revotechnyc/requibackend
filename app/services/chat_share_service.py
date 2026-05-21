"""
Public conversation sharing: snapshot links and import for other users.
"""

import secrets
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.models import Conversation, ConversationShare, Message, Seat

LIVE_APP_URL = "https://requi.io"


def build_share_url(share_token: str) -> str:
    return f"{LIVE_APP_URL.rstrip('/')}/#share/{share_token}"


def _message_to_snapshot(msg: Message) -> Dict[str, Any]:
    return {
        "role": msg.role,
        "content": msg.content,
        "created_at": msg.created_at.isoformat() if msg.created_at else None,
    }


async def create_or_get_share(
    db: AsyncSession,
    *,
    conversation_id: uuid.UUID,
    user_id: uuid.UUID,
) -> ConversationShare:
    """Create a public share snapshot for the owner's conversation."""
    result = await db.execute(
        select(Conversation)
        .options(selectinload(Conversation.messages))
        .where(
            Conversation.id == conversation_id,
            Conversation.user_id == user_id,
        )
    )
    conversation = result.scalar_one_or_none()
    if not conversation:
        raise ValueError("Conversation not found")

    if not conversation.messages:
        raise ValueError("Conversation has no messages to share")

    existing = await db.execute(
        select(ConversationShare).where(
            ConversationShare.conversation_id == conversation.id,
            ConversationShare.is_active == True,
        )
    )
    share = existing.scalar_one_or_none()
    if share:
        share.snapshot_messages = [_message_to_snapshot(m) for m in conversation.messages]
        share.title = conversation.title
        await db.commit()
        await db.refresh(share)
        return share

    token = secrets.token_urlsafe(16).replace("-", "").replace("_", "")[:22]
    share = ConversationShare(
        share_token=token,
        conversation_id=conversation.id,
        created_by_user_id=user_id,
        organization_id=conversation.organization_id,
        title=conversation.title,
        snapshot_messages=[_message_to_snapshot(m) for m in conversation.messages],
        is_active=True,
    )
    db.add(share)
    await db.commit()
    await db.refresh(share)
    return share


async def get_public_share(db: AsyncSession, share_token: str) -> Optional[ConversationShare]:
    result = await db.execute(
        select(ConversationShare).where(
            ConversationShare.share_token == share_token,
            ConversationShare.is_active == True,
        )
    )
    return result.scalar_one_or_none()


async def import_shared_conversation(
    db: AsyncSession,
    *,
    share_token: str,
    user_id: uuid.UUID,
) -> Conversation:
    """Copy shared snapshot into a new conversation for the current user."""
    share = await get_public_share(db, share_token)
    if not share:
        raise ValueError("Share link not found or expired")

    seat_result = await db.execute(
        select(Seat).where(Seat.user_id == user_id, Seat.is_active == True)
    )
    seat = seat_result.scalar_one_or_none()
    if not seat:
        raise ValueError("No active organization")

    title = share.title or "Shared conversation"
    if not title.lower().startswith("shared"):
        title = f"Shared: {title}"

    conversation = Conversation(
        organization_id=seat.organization_id,
        user_id=user_id,
        title=title[:255],
        is_shared_import=True,
        shared_from_token=share_token,
    )
    db.add(conversation)
    await db.flush()

    for item in share.snapshot_messages or []:
        role = item.get("role") or "user"
        content = item.get("content") or ""
        if role not in ("user", "assistant", "system"):
            role = "user"
        db.add(
            Message(
                conversation_id=conversation.id,
                role=role,
                content=content,
            )
        )

    await db.commit()
    await db.refresh(conversation)
    return conversation
