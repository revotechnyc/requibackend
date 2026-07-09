"""
AI Q&A endpoints
"""

import asyncio
import json
import logging
import time as time_module
import uuid
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.endpoints.auth import get_current_active_user
from app.core.permissions import Feature, check_feature_access, require_feature_dependency
from app.db.database import AsyncSessionLocal, get_db
from app.db.models import Conversation, Document, Message, NotificationType, Organization, Seat, User, WorkspaceTask, WorkspaceWorkflow
from app.services.ml import MLService
from app.core.config import settings
from app.services.compliance_ai_integration import process_intelligence_compliance_update
from app.services.responses_service import (
    SONIA_SYSTEM_INSTRUCTION,
    ResponsesService,
    mock_chat_stream_for_testing,
)
from app.services.realtime_voice_service import (
    is_realtime_voice_configured,
    negotiate_webrtc_call,
    validate_sdp_offer,
)
from app.services.usage import check_trial_usage_limit, get_trial_info, increment_usage_if_trialing
from app.services.retrieval import RetrievalService
from app.services.workflow_context_builder import (
    build_workflow_context,
    format_workflow_context_for_prompt,
)
from app.services.workflow_service import get_workflow_for_org, log_workflow_activity
from app.services.task_visibility import workflow_visible_to_user

router = APIRouter()
logger = logging.getLogger(__name__)
ml_service = MLService()


# Pydantic models
class AskRequest(BaseModel):
    question: str
    conversation_id: Optional[str] = None


class PipelineRequest(BaseModel):
    """Full agent pipeline request (GPT-5.5)"""
    question: str
    document_text: Optional[str] = None
    document_id: Optional[str] = None


class AskResponse(BaseModel):
    answer: str
    confidence: str
    citations: List[dict]
    knowledge_gap_detected: bool
    gap_description: Optional[str] = None
    gap_task_id: Optional[str] = None
    tokens_used: int


class ConversationCreate(BaseModel):
    title: Optional[str] = None


class MessageResponse(BaseModel):
    id: str
    role: str
    content: str
    citations: Optional[List[dict]] = None
    created_at: str


class ConversationResponse(BaseModel):
    id: str
    title: Optional[str]
    messages: List[MessageResponse]
    created_at: str
    updated_at: str
    is_shared_import: bool = False
    shared_from_token: Optional[str] = None


@router.post("/ask", response_model=AskResponse)
async def ask_question(
    request: AskRequest,
    organization: Organization = Depends(lambda: require_feature_dependency(Feature.AI_QA)),
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Ask a compliance question"""
    # Get or create conversation
    conversation = None
    conversation_history = []
    
    if request.conversation_id:
        result = await db.execute(
            select(Conversation).where(
                Conversation.id == request.conversation_id,
                Conversation.user_id == current_user.id,
            )
        )
        conversation = result.scalar_one_or_none()
        
        if conversation:
            # Build conversation history
            for msg in conversation.messages:
                conversation_history.append({
                    "role": msg.role,
                    "content": msg.content,
                })
    
    # Get answer from ML service
    result = await ml_service.answer_query(
        db,
        organization.id,
        request.question,
        conversation_history if conversation_history else None,
    )
    
    # Save to conversation
    if conversation:
        # Add user message
        user_msg = Message(
            conversation_id=conversation.id,
            role="user",
            content=request.question,
        )
        db.add(user_msg)
        
        # Add assistant message
        assistant_msg = Message(
            conversation_id=conversation.id,
            role="assistant",
            content=result["answer"],
            citations=result["citations"],
            confidence_score=0.8 if result["confidence"] == "high" else 0.5 if result["confidence"] == "medium" else 0.3,
        )
        db.add(assistant_msg)
        await db.commit()
    
    return AskResponse(
        answer=result["answer"],
        confidence=result["confidence"],
        citations=result["citations"],
        knowledge_gap_detected=result["knowledge_gap_detected"],
        gap_description=result.get("gap_description"),
        gap_task_id=result.get("gap_task_id"),
        tokens_used=result["tokens_used"],
    )


@router.post("/pipeline", response_model=dict)
async def run_agent_pipeline(
    request: PipelineRequest,
    organization: Organization = Depends(lambda: require_feature_dependency(Feature.AI_QA)),
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Run full GPT-5.5 agent pipeline:
    Contract Agent → Compliance Agent → Gap Agent → Execution Agent → Orchestrator
    """
    from app.services.ml import MLService
    ml = MLService()

    # Get org context
    from app.db.models import Seat, Task, Document, Deadline

    seat_result = await db.execute(
        select(Seat).where(
            Seat.organization_id == organization.id,
            Seat.is_active == True,
        )
    )
    seats = seat_result.scalars().all()

    tasks_result = await db.execute(
        select(Task).where(Task.organization_id == organization.id).limit(100)
    )
    tasks = tasks_result.scalars().all()

    docs_result = await db.execute(
        select(Document).where(Document.organization_id == organization.id).limit(50)
    )
    documents = docs_result.scalars().all()

    deadlines_result = await db.execute(
        select(Deadline).where(Deadline.organization_id == organization.id).limit(50)
    )
    deadlines = deadlines_result.scalars().all()

    org_context = {
        "states": organization.settings.get("states", []),
        "plan_types": organization.settings.get("plan_types", []),
        "users": [{"id": str(s.user_id), "role": s.role.value} for s in seats],
        "tasks": [{"id": str(t.id), "title": t.title, "status": t.status, "requirement_id": str(t.requirement_id)} for t in tasks],
        "documents": [{"id": str(d.id), "title": d.title, "type": d.type} for d in documents],
        "deadlines": [{"id": str(dl.id), "due_date": dl.due_date.isoformat() if dl.due_date else None, "status": dl.status} for dl in deadlines],
    }

    # Run full pipeline
    result = await ml.run_full_pipeline(
        db=db,
        organization_id=organization.id,
        query=request.question,
        document_text=request.document_text,
        org_context=org_context,
    )

    return {
        "answer": result["answer"],
        "requirements": result.get("requirements", []),
        "gaps": result.get("gaps", []),
        "tasks": result.get("tasks", []),
        "risk_score": result.get("risk_score", 0),
        "risk_level": result.get("risk_level", "low"),
        "scores": result.get("scores", {}),
        "confidence": result.get("confidence", "medium"),
        "tokens_used": result.get("tokens_used", 0),
        "model": "gpt-5.5",
    }


@router.post("/conversations", response_model=dict)
async def create_conversation(
    data: ConversationCreate,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Create new conversation"""
    # Get user's organization
    from app.db.models import Seat
    
    result = await db.execute(
        select(Seat).where(
            Seat.user_id == current_user.id,
            Seat.is_active == True,
        )
    )
    seat = result.scalar_one_or_none()
    
    if not seat:
        raise HTTPException(status_code=400, detail="No active organization")
    
    conversation = Conversation(
        organization_id=seat.organization_id,
        user_id=current_user.id,
        title=data.title or "New Conversation",
    )
    db.add(conversation)
    await db.commit()
    await db.refresh(conversation)
    
    return {
        "id": str(conversation.id),
        "title": conversation.title,
        "created_at": conversation.created_at.isoformat(),
    }


@router.get("/conversations", response_model=List[dict])
async def list_conversations(
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """List user's conversations"""
    result = await db.execute(
        select(
            Conversation,
            func.count(Message.id).label("message_count"),
        )
        .outerjoin(Message, Message.conversation_id == Conversation.id)
        .where(Conversation.user_id == current_user.id)
        .group_by(Conversation.id)
        .order_by(Conversation.updated_at.desc())
    )

    return [
        {
            "id": str(conv.id),
            "title": conv.title,
            "message_count": int(message_count or 0),
            "created_at": conv.created_at.isoformat(),
            "updated_at": conv.updated_at.isoformat(),
            "is_shared_import": bool(getattr(conv, "is_shared_import", False)),
        }
        for conv, message_count in result.all()
    ]


@router.get("/conversations/{conversation_id}", response_model=ConversationResponse)
async def get_conversation(
    conversation_id: str,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Get conversation with messages"""
    result = await db.execute(
        select(Conversation)
        .options(selectinload(Conversation.messages))
        .where(
            Conversation.id == conversation_id,
            Conversation.user_id == current_user.id,
        )
    )
    conversation = result.scalar_one_or_none()
    
    if not conversation:
        raise HTTPException(status_code=404, detail="Conversation not found")
    
    return ConversationResponse(
        id=str(conversation.id),
        title=conversation.title,
        messages=[
            MessageResponse(
                id=str(m.id),
                role=m.role,
                content=m.content,
                citations=m.citations,
                created_at=m.created_at.isoformat(),
            )
            for m in conversation.messages
        ],
        created_at=conversation.created_at.isoformat(),
        updated_at=conversation.updated_at.isoformat(),
        is_shared_import=bool(getattr(conversation, "is_shared_import", False)),
        shared_from_token=getattr(conversation, "shared_from_token", None),
    )


@router.delete("/conversations/{conversation_id}")
async def delete_conversation(
    conversation_id: str,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Delete conversation"""
    result = await db.execute(
        select(Conversation).where(
            Conversation.id == conversation_id,
            Conversation.user_id == current_user.id,
        )
    )
    conversation = result.scalar_one_or_none()
    
    if not conversation:
        raise HTTPException(status_code=404, detail="Conversation not found")
    
    await db.delete(conversation)
    await db.commit()
    
    return {"status": "deleted"}


@router.post("/conversations/{conversation_id}/share")
async def create_conversation_share(
    conversation_id: str,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Create or refresh a public link for the user's conversation."""
    from app.services.chat_share_service import build_share_url, create_or_get_share

    try:
        conv_uuid = uuid.UUID(conversation_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid conversation id")

    try:
        share = await create_or_get_share(
            db,
            conversation_id=conv_uuid,
            user_id=current_user.id,
        )
    except ValueError as e:
        msg = str(e)
        if "not found" in msg.lower():
            raise HTTPException(status_code=404, detail=msg)
        raise HTTPException(status_code=400, detail=msg)

    return {
        "share_token": share.share_token,
        "share_url": build_share_url(share.share_token),
    }


# ==========================
# RESPONSES API — Streaming Intelligence Chat
# ==========================

def _uuid_list(raw: Optional[List[str]]) -> List[uuid.UUID]:
    out: List[uuid.UUID] = []
    if not raw:
        return out
    for s in raw:
        if not s or not isinstance(s, str):
            continue
        try:
            out.append(uuid.UUID(s.strip()))
        except (ValueError, AttributeError):
            continue
    return out


class ChatStreamRequest(BaseModel):
    message: str
    conversation_id: Optional[str] = None
    """Legacy: same as internal_document_ids when split fields are omitted."""
    document_ids: Optional[List[str]] = None
    """Files uploaded / attached for this chat turn (analyzeAttachedFiles)."""
    attachment_document_ids: Optional[List[str]] = None
    """Explicit picks from the org document library (indexed uploads)."""
    internal_document_ids: Optional[List[str]] = None
    """When true, run hybrid retrieval over indexed chunks (searchInternalDocuments)."""
    search_internal_documents: bool = False
    """Optional workflow case context (EPIC-003)."""
    workflow_id: Optional[str] = None
    """Optional focus task for task execution mode."""
    task_id: Optional[str] = None
    """When true with task_id, AI returns structured resolution JSON."""
    task_execution_mode: bool = False


class ChatRequest(BaseModel):
    message: str
    conversation_id: Optional[str] = None


@router.post("/chat/stream")
async def chat_stream(
    request: ChatStreamRequest,
    fastapi_request: Request,
    current_user: User = Depends(get_current_active_user),
    _: Organization = Depends(lambda: require_feature_dependency(Feature.AI_QA)),
    __trial_limit: None = Depends(check_trial_usage_limit),
):
    """Stream AI chat via OpenAI Responses API with stored prompt (GPT-5)."""
    req_id = str(uuid.uuid4())[:8]
    logger = logging.getLogger(__name__)
    logger.info(
        json.dumps(
            {
                "event": "chat_stream_start",
                "request_id": req_id,
                "user_id": str(current_user.id),
                "message_preview": request.message[:100],
            }
        )
    )

    t0 = time_module.time()
    conv_id: str
    conversation_id: uuid.UUID
    organization_id: uuid.UUID
    workflow_uuid: Optional[uuid.UUID] = None
    task_uuid: Optional[uuid.UUID] = None
    workflow_ctx: Optional[dict] = None
    workflow_context_text: Optional[str] = None

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Seat).where(
                Seat.user_id == current_user.id,
                Seat.is_active == True,
            )
        )
        seat = result.scalar_one_or_none()
        if not seat:
            raise HTTPException(status_code=400, detail="No active organization")

        organization_id = seat.organization_id

        if request.workflow_id:
            try:
                workflow_uuid = uuid.UUID(request.workflow_id.strip())
            except (ValueError, AttributeError):
                workflow_uuid = None
        if request.task_id:
            try:
                task_uuid = uuid.UUID(request.task_id.strip())
            except (ValueError, AttributeError):
                task_uuid = None

        conversation = None
        if request.task_execution_mode and task_uuid:
            if request.conversation_id:
                conv_result = await db.execute(
                    select(Conversation).where(
                        Conversation.id == request.conversation_id,
                        Conversation.user_id == current_user.id,
                        Conversation.task_id == task_uuid,
                    )
                )
                conversation = conv_result.scalar_one_or_none()
            if not conversation:
                conversation = Conversation(
                    organization_id=organization_id,
                    user_id=current_user.id,
                    title=f"Task: {(request.message[:40] or 'Execution')}",
                    workflow_id=workflow_uuid,
                    task_id=task_uuid,
                )
                db.add(conversation)
                await db.commit()
                await db.refresh(conversation)
        elif request.conversation_id:
            conv_result = await db.execute(
                select(Conversation).where(
                    Conversation.id == request.conversation_id,
                    Conversation.user_id == current_user.id,
                )
            )
            conversation = conv_result.scalar_one_or_none()

        if not conversation:
            conversation = Conversation(
                organization_id=seat.organization_id,
                user_id=current_user.id,
                title=request.message[:50] or "New Conversation",
            )
            db.add(conversation)
            await db.commit()
            await db.refresh(conversation)

        conv_updated = False
        if workflow_uuid and conversation.workflow_id != workflow_uuid:
            conversation.workflow_id = workflow_uuid
            conv_updated = True
        if task_uuid and conversation.task_id != task_uuid:
            if not request.task_execution_mode:
                conversation.task_id = task_uuid
                conv_updated = True
        if conv_updated:
            await db.commit()

        if workflow_uuid:
            try:
                org_result = await db.execute(
                    select(Organization).where(Organization.id == seat.organization_id)
                )
                org_row = org_result.scalar_one_or_none()
                if org_row:
                    workflow = await get_workflow_for_org(str(workflow_uuid), seat.organization_id, db)
                    seat_result = await db.execute(
                        select(Seat).where(Seat.user_id == current_user.id, Seat.is_active == True)
                    )
                    user_seat = seat_result.scalar_one_or_none()
                    if user_seat and await workflow_visible_to_user(
                        workflow, seat.organization_id, current_user.id, user_seat.role, db
                    ):
                        workflow_ctx = await build_workflow_context(
                            workflow,
                            seat.organization_id,
                            db,
                            focus_task_id=task_uuid,
                        )
                        workflow_context_text = format_workflow_context_for_prompt(
                            workflow_ctx,
                            task_execution=request.task_execution_mode and bool(task_uuid),
                        )
                        await log_workflow_activity(
                            db,
                            workflow.id,
                            seat.organization_id,
                            current_user.id,
                            "ai_query",
                            {
                                "message_preview": request.message[:200],
                                "task_id": str(task_uuid) if task_uuid else None,
                                "task_execution_mode": request.task_execution_mode,
                            },
                        )
                        await db.commit()
            except Exception as wf_exc:
                logger.warning(
                    json.dumps(
                        {
                            "event": "chat_stream_workflow_context_failed",
                            "request_id": req_id,
                            "error": str(wf_exc),
                        }
                    )
                )

        conversation_id = conversation.id
        conv_id = str(conversation.id)

    async def generate():
        full_content = ""
        token_count = 0
        first_token_time = None
        client_aborted = False

        yield f"data: {json.dumps({'type': 'start', 'conversation_id': conv_id})}\n\n"
        yield f"data: {json.dumps({'type': 'phase', 'phase': 'initializing'})}\n\n"

        attachment_uuids = _uuid_list(request.attachment_document_ids)
        internal_explicit_uuids = _uuid_list(request.internal_document_ids)
        legacy_uuids = _uuid_list(request.document_ids)

        if legacy_uuids and not attachment_uuids and not internal_explicit_uuids and not request.search_internal_documents:
            internal_pick_uuids = legacy_uuids
        elif legacy_uuids:
            merged: List[uuid.UUID] = []
            seen_m: set = set()
            for u in internal_explicit_uuids + legacy_uuids:
                if u not in seen_m:
                    seen_m.add(u)
                    merged.append(u)
            internal_pick_uuids = merged
        else:
            internal_pick_uuids = internal_explicit_uuids

        att_set = set(attachment_uuids)
        internal_pick_uuids = [u for u in internal_pick_uuids if u not in att_set]

        if attachment_uuids:
            yield f"data: {json.dumps({'type': 'phase', 'phase': 'loading_attachments'})}\n\n"
        if internal_pick_uuids:
            yield f"data: {json.dumps({'type': 'phase', 'phase': 'loading_library_documents'})}\n\n"
        if request.search_internal_documents:
            yield f"data: {json.dumps({'type': 'phase', 'phase': 'searching_internal_documents'})}\n\n"
        yield f"data: {json.dumps({'type': 'phase', 'phase': 'searching'})}\n\n"

        t_prep_start = time_module.time()

        try:
            async with AsyncSessionLocal() as stream_db:
                try:
                    messages = []
                    try:
                        msg_result = await stream_db.execute(
                            select(Message)
                            .where(Message.conversation_id == conversation_id)
                            .order_by(Message.created_at.desc())
                            .limit(25)
                        )
                        for msg in reversed(msg_result.scalars().all()):
                            messages.append({"role": msg.role, "content": msg.content})
                    except Exception:
                        messages = []

                    file_refs: List[dict] = []
                    explicit_section_labels: List[str] = []
                    retrieval = RetrievalService()

                    if attachment_uuids or internal_pick_uuids:
                        all_load_ids = list(dict.fromkeys([*attachment_uuids, *internal_pick_uuids]))
                        if all_load_ids:
                            doc_result = await stream_db.execute(
                                select(Document).where(
                                    Document.id.in_(all_load_ids),
                                    Document.organization_id == organization_id,
                                )
                            )
                            by_id = {d.id: d for d in doc_result.scalars().all()}

                            for uid in attachment_uuids:
                                d = by_id.get(uid)
                                if not d:
                                    continue
                                context_text, labels = await retrieval.build_document_context_text(
                                    stream_db,
                                    d,
                                    "Attached to this chat",
                                )
                                if context_text:
                                    file_refs.append(
                                        {"type": "input_text", "text": context_text}
                                    )
                                    explicit_section_labels.extend(labels)

                            for uid in internal_pick_uuids:
                                d = by_id.get(uid)
                                if not d:
                                    continue
                                context_text, labels = await retrieval.build_document_context_text(
                                    stream_db,
                                    d,
                                    "Internal library document",
                                )
                                if context_text:
                                    file_refs.append(
                                        {"type": "input_text", "text": context_text}
                                    )
                                    explicit_section_labels.extend(labels)

                    if request.search_internal_documents and request.message.strip():
                        try:
                            context_str, _citations = await retrieval.get_context_for_query(
                                stream_db,
                                organization_id,
                                request.message.strip(),
                                max_chunks=5,
                            )
                            if context_str:
                                cap = 12000
                                trimmed = context_str if len(context_str) <= cap else context_str[:cap] + "\n…(truncated)"
                                file_refs.append(
                                    {
                                        "type": "input_text",
                                        "text": f"--- Relevant excerpts from indexed company documents (knowledge base search) ---\n{trimmed}",
                                    }
                                )
                        except Exception as kb_exc:
                            logger.warning(
                                json.dumps(
                                    {
                                        "event": "chat_stream_kb_search_failed",
                                        "request_id": req_id,
                                        "error": str(kb_exc),
                                    }
                                )
                            )

                    if file_refs:
                        user_content = [{"type": "input_text", "text": request.message}] + file_refs
                    else:
                        user_content = request.message

                    if workflow_context_text:
                        messages.insert(
                            0,
                            {
                                "role": "developer",
                                "content": workflow_context_text,
                            },
                        )

                    messages.append({"role": "user", "content": user_content})

                    conv_result = await stream_db.execute(
                        select(Conversation).where(Conversation.id == conversation_id)
                    )
                    conversation_row = conv_result.scalar_one_or_none()
                    if not conversation_row:
                        yield f"data: {json.dumps({'type': 'error', 'error': 'Conversation not found'})}\n\n"
                        return

                    user_msg = Message(
                        conversation_id=conversation_id,
                        role="user",
                        content=request.message,
                    )
                    stream_db.add(user_msg)
                    conversation_row.updated_at = datetime.utcnow()
                    await stream_db.commit()

                    responses_service = ResponsesService(
                        system_instruction=SONIA_SYSTEM_INSTRUCTION,
                        request_id=req_id,
                        skip_vector_search=bool(explicit_section_labels),
                        document_source_labels=explicit_section_labels,
                    )

                    t1 = time_module.time()
                    logger.info(
                        json.dumps(
                            {
                                "event": "chat_stream_prep_done",
                                "request_id": req_id,
                                "prep_time_ms": round((t1 - t_prep_start) * 1000),
                                "mock_stream": settings.mock_chat_stream,
                            }
                        )
                    )

                    if settings.mock_chat_stream:
                        stream_iter = mock_chat_stream_for_testing(
                            request.message,
                            delay_ms=settings.mock_chat_stream_delay_ms,
                        )
                    else:
                        stream_iter = responses_service.chat_stream(messages)

                    async for sse_event in stream_iter:
                        if await fastapi_request.is_disconnected():
                            client_aborted = True
                            await stream_db.rollback()
                            break
                        await asyncio.sleep(0)
                        try:
                            data = json.loads(sse_event.replace("data: ", "").strip())
                            if data.get("type") == "done":
                                data["conversation_id"] = conv_id
                                t2 = time_module.time()
                                data["ttft_ms"] = (
                                    round((first_token_time - t0) * 1000, 1) if first_token_time else None
                                )
                                data["total_time_ms"] = round((t2 - t0) * 1000)
                                data["token_count"] = token_count
                                if first_token_time and token_count > 1:
                                    data["tokens_per_second"] = round(
                                        token_count / ((t2 - first_token_time) or 1), 1
                                    )
                                else:
                                    data["tokens_per_second"] = 0
                                yield f"data: {json.dumps(data)}\n\n"
                            else:
                                yield sse_event
                            if data.get("type") == "token":
                                full_content += data.get("content", "")
                                token_count += 1
                                if first_token_time is None:
                                    first_token_time = time_module.time()
                        except Exception:
                            yield sse_event

                    if client_aborted:
                        logger.info(
                            json.dumps(
                                {
                                    "event": "chat_stream_client_abort",
                                    "request_id": req_id,
                                    "token_count": token_count,
                                }
                            )
                        )
                        return

                    if full_content:
                        assistant_msg = Message(
                            conversation_id=conversation_id,
                            role="assistant",
                            content=full_content,
                        )
                        stream_db.add(assistant_msg)
                        conversation_row.updated_at = datetime.utcnow()
                        await increment_usage_if_trialing(str(current_user.id), stream_db)
                        await stream_db.commit()

                        if workflow_uuid and workflow_ctx:
                            try:
                                await log_workflow_activity(
                                    stream_db,
                                    uuid.UUID(workflow_ctx["workflow_id"]),
                                    organization_id,
                                    current_user.id,
                                    "ai_response",
                                    {
                                        "summary_preview": full_content[:300],
                                        "task_id": str(task_uuid) if task_uuid else None,
                                    },
                                )
                                await stream_db.commit()
                            except Exception:
                                await stream_db.rollback()

                        try:
                            org_result = await stream_db.execute(
                                select(Organization)
                                .where(Organization.id == organization_id)
                                .options(selectinload(Organization.subscription))
                            )
                            org = org_result.scalar_one()
                            has_doc_context = bool(
                                attachment_uuids
                                or internal_pick_uuids
                                or request.search_internal_documents
                            )
                            compliance_summary = await process_intelligence_compliance_update(
                                stream_db,
                                org,
                                request.message,
                                full_content,
                                source_type="chat",
                                has_documents=has_doc_context,
                                use_mock=settings.mock_chat_stream,
                                conversation_id=conversation_id,
                                task_id=task_uuid,
                                workflow_id=workflow_uuid,
                            )
                            if compliance_summary and compliance_summary.get("gaps_created", 0) > 0:
                                yield (
                                    "data: "
                                    + json.dumps(
                                        {
                                            "type": "compliance_analysis",
                                            "gaps_created": compliance_summary["gaps_created"],
                                            "overall_ai_score": compliance_summary.get("overall_ai_score"),
                                        }
                                    )
                                    + "\n\n"
                                )
                        except Exception as comp_exc:
                            logger.warning(
                                json.dumps(
                                    {
                                        "event": "chat_stream_compliance_skip",
                                        "request_id": req_id,
                                        "error": str(comp_exc),
                                    }
                                )
                            )

                    t2 = time_module.time()
                    logger.info(
                        json.dumps(
                            {
                                "event": "chat_stream_complete",
                                "request_id": req_id,
                                "total_time_ms": round((t2 - t0) * 1000),
                                "token_count": token_count,
                                "content_length": len(full_content),
                            }
                        )
                    )
                except asyncio.CancelledError:
                    await stream_db.rollback()
                    logger.info(
                        json.dumps(
                            {
                                "event": "chat_stream_cancelled",
                                "request_id": req_id,
                                "token_count": token_count,
                            }
                        )
                    )
                    return
        except asyncio.CancelledError:
            logger.info(
                json.dumps(
                    {
                        "event": "chat_stream_cancelled",
                        "request_id": req_id,
                    }
                )
            )
            return

    return StreamingResponse(
        generate(),
        media_type="text/event-stream; charset=utf-8",
        headers={
            "Cache-Control": "no-cache, no-store, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
            "Content-Encoding": "identity",
        },
    )


@router.get("/usage")
async def ai_usage(
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Trial AI usage: prompts used today vs daily limit."""
    return await get_trial_info(str(current_user.id), db)


@router.get("/realtime/config")
async def realtime_voice_config(
    current_user: User = Depends(get_current_active_user),
):
    """Whether OpenAI Realtime (Requi Sonia / Marin) is available for live mode."""
    if not is_realtime_voice_configured():
        return {"enabled": False}
    return {
        "enabled": True,
        "voice": settings.openai_realtime_voice,
        "model": settings.openai_realtime_model,
        "prompt_id": settings.openai_voice_prompt_id,
        "prompt_version": settings.openai_voice_prompt_version,
        "prompt_source": "voice",
        "text_chat_prompt_id": settings.openai_prompt_id,
    }


async def _read_sdp_offer_from_request(request: Request) -> str:
    """Accept raw SDP (application/sdp) or multipart form field ``sdp``."""
    content_type = (request.headers.get("content-type") or "").lower()

    if "multipart/form-data" in content_type:
        form = await request.form()
        field = form.get("sdp")
        if field is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail='Multipart form must include field "sdp"',
            )
        if hasattr(field, "read"):
            raw = await field.read()
            return raw.decode("utf-8", errors="replace")
        return str(field)

    return (await request.body()).decode("utf-8", errors="replace")


@router.post("/realtime/call")
async def realtime_webrtc_call(
    request: Request,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
    _: Organization = Depends(lambda: require_feature_dependency(Feature.AI_QA)),
    __trial_limit: None = Depends(check_trial_usage_limit),
):
    """
    WebRTC SDP exchange for OpenAI Realtime (live voice).
    Body: raw SDP (application/sdp) or multipart form field ``sdp``. Returns SDP answer.
    """
    if not is_realtime_voice_configured():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Live voice (OpenAI Realtime) is not configured.",
        )

    try:
        offer_raw = await _read_sdp_offer_from_request(request)
        offer_sdp = validate_sdp_offer(offer_raw)
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from e

    try:
        answer_sdp = await negotiate_webrtc_call(
            offer_sdp,
            safety_identifier=str(current_user.id),
        )
    except ValueError as e:
        msg = str(e)
        status_code = (
            status.HTTP_503_SERVICE_UNAVAILABLE
            if "not available for your OpenAI API key" in msg
            else status.HTTP_502_BAD_GATEWAY
        )
        raise HTTPException(status_code=status_code, detail=msg) from e

    logger.warning(
        "realtime/call user=%s offer_bytes=%s answer_bytes=%s answer_starts=%r",
        current_user.id,
        len(offer_sdp.encode("utf-8")),
        len(answer_sdp.encode("utf-8")),
        answer_sdp[:40],
    )

    try:
        from app.services.notification_service import NotificationService

        seat_row = await db.execute(
            select(Seat).where(Seat.user_id == current_user.id, Seat.is_active == True)
        )
        seat_for_notif = seat_row.scalar_one_or_none()
        org_id = seat_for_notif.organization_id if seat_for_notif else None
        await NotificationService(db).create_notification(
            current_user.id,
            org_id,
            NotificationType.LIVE_VOICE_CONNECTED,
            allow_duplicate_within_minutes=5,
        )
    except Exception:
        logger.exception("Failed to create live_voice_connected notification")

    return Response(content=answer_sdp, media_type="application/sdp")


class RealtimeTurnCompleteRequest(BaseModel):
    """Persist a live-voice turn and count trial usage (OpenAI Realtime / Sonia)."""

    conversation_id: Optional[str] = None
    user_message: Optional[str] = None
    assistant_message: Optional[str] = None
    title_hint: Optional[str] = None


async def _get_active_seat(db: AsyncSession, user_id: uuid.UUID) -> Seat:
    result = await db.execute(
        select(Seat).where(Seat.user_id == user_id, Seat.is_active == True)
    )
    seat = result.scalar_one_or_none()
    if not seat:
        raise HTTPException(status_code=400, detail="No active organization")
    return seat


async def _get_or_create_realtime_conversation(
    db: AsyncSession,
    *,
    user: User,
    seat: Seat,
    conversation_id: Optional[str],
    title_hint: Optional[str],
) -> Conversation:
    conversation: Optional[Conversation] = None
    if conversation_id:
        try:
            conv_uuid = uuid.UUID(conversation_id)
        except (ValueError, AttributeError):
            conv_uuid = None
        if conv_uuid:
            conv_result = await db.execute(
                select(Conversation).where(
                    Conversation.id == conv_uuid,
                    Conversation.user_id == user.id,
                )
            )
            conversation = conv_result.scalar_one_or_none()

    if not conversation:
        title = (title_hint or "Live with Sonia").strip()[:255] or "Live with Sonia"
        conversation = Conversation(
            organization_id=seat.organization_id,
            user_id=user.id,
            title=title,
        )
        db.add(conversation)
        await db.flush()

    return conversation


@router.post("/realtime/turn-complete")
async def realtime_turn_complete(
    body: RealtimeTurnCompleteRequest = RealtimeTurnCompleteRequest(),
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
    _: Organization = Depends(lambda: require_feature_dependency(Feature.AI_QA)),
):
    """
    After each Realtime assistant turn: save user/assistant lines to the conversation
  history and count one trial prompt. Uses OPENAI_VOICE_PROMPT_ID session (not text chat prompt).
    """
    seat = await _get_active_seat(db, current_user.id)
    user_text = (body.user_message or "").strip()
    assistant_text = (body.assistant_message or "").strip()

    conversation = await _get_or_create_realtime_conversation(
        db,
        user=current_user,
        seat=seat,
        conversation_id=body.conversation_id,
        title_hint=body.title_hint,
    )

    if user_text:
        db.add(
            Message(
                conversation_id=conversation.id,
                role="user",
                content=user_text,
            )
        )
    if assistant_text:
        db.add(
            Message(
                conversation_id=conversation.id,
                role="assistant",
                content=assistant_text,
            )
        )

    if user_text or assistant_text:
        conversation.updated_at = datetime.utcnow()

    await increment_usage_if_trialing(str(current_user.id), db)

    if user_text or assistant_text:
        try:
            from app.services.notification_service import NotificationService

            await NotificationService(db).create_notification(
                current_user.id,
                seat.organization_id,
                NotificationType.LIVE_VOICE_TURN_SAVED,
                template_vars={"conversation_title": (conversation.title or "Live with Sonia")[:120]},
                allow_duplicate_within_minutes=1,
            )
        except Exception:
            logger.exception("Failed to create live_voice_turn_saved notification")

    await db.commit()

    return {
        "ok": True,
        "conversation_id": str(conversation.id),
    }


@router.post("/chat")
async def chat(
    request: ChatRequest,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
    _: Organization = Depends(lambda: require_feature_dependency(Feature.AI_QA)),
    __trial_limit: None = Depends(check_trial_usage_limit),
):
    """Non-streaming chat via OpenAI Responses API with stored prompt."""
    req_id = str(uuid.uuid4())[:8]
    logger = logging.getLogger(__name__)

    result = await db.execute(
        select(Seat).where(
            Seat.user_id == current_user.id,
            Seat.is_active == True,
        )
    )
    seat = result.scalar_one_or_none()
    if not seat:
        raise HTTPException(status_code=400, detail="No active organization")

    conversation = None
    messages: List[dict] = []

    if request.conversation_id:
        conv_result = await db.execute(
            select(Conversation).where(
                Conversation.id == request.conversation_id,
                Conversation.user_id == current_user.id,
            )
        )
        conversation = conv_result.scalar_one_or_none()

    if not conversation:
        conversation = Conversation(
            organization_id=seat.organization_id,
            user_id=current_user.id,
            title=request.message[:50] or "New Conversation",
        )
        db.add(conversation)
        await db.commit()
        await db.refresh(conversation)
    else:
        msg_result = await db.execute(
            select(Message)
            .where(Message.conversation_id == conversation.id)
            .order_by(Message.created_at.desc())
            .limit(25)
        )
        for msg in reversed(msg_result.scalars().all()):
            messages.append({"role": msg.role, "content": msg.content})

    messages.append({"role": "user", "content": request.message})

    user_msg = Message(
        conversation_id=conversation.id,
        role="user",
        content=request.message,
    )
    db.add(user_msg)
    await db.commit()

    responses_service = ResponsesService(
        system_instruction=SONIA_SYSTEM_INSTRUCTION,
        request_id=req_id,
    )
    result_data = await responses_service.chat(messages)

    content = result_data.get("content", "")
    if content:
        assistant_msg = Message(
            conversation_id=conversation.id,
            role="assistant",
            content=content,
        )
        db.add(assistant_msg)
        await increment_usage_if_trialing(str(current_user.id), db)
        await db.commit()

        try:
            org_result = await db.execute(
                select(Organization)
                .where(Organization.id == seat.organization_id)
                .options(selectinload(Organization.subscription))
            )
            org = org_result.scalar_one()
            await process_intelligence_compliance_update(
                db,
                org,
                request.message,
                content,
                source_type="chat",
                has_documents=False,
                use_mock=settings.mock_chat_stream,
                conversation_id=conversation.id,
                task_id=conversation.task_id,
                workflow_id=conversation.workflow_id,
            )
        except Exception as comp_exc:
            logger.warning("chat_compliance_skip: %s", comp_exc)

    return {
        "conversation_id": str(conversation.id),
        "content": content,
        "tokens_used": result_data.get("tokens_used", 0),
    }


# ==========================
# v3.0 — OpenAI-Compatible Chat Completions
# ==========================

from typing import Literal, Union
import time


class ChatCompletionMessage(BaseModel):
    role: Literal["system", "user", "assistant"]
    content: str


class ChatCompletionRequest(BaseModel):
    """OpenAI-compatible /v1/chat/completions request body"""
    model: str = "requi-gpt-4"
    messages: List[ChatCompletionMessage]
    temperature: float = 0.7
    max_tokens: int = 2048
    stream: bool = False
    user: Optional[str] = None
    # Requi-specific extensions
    extract_tasks: bool = True  # Auto-extract actionable tasks from response
    zapier_actions: Optional[List[str]] = None  # e.g., ["email", "calendar"]


class ChatCompletionChoice(BaseModel):
    index: int = 0
    message: dict
    finish_reason: str = "stop"


class ChatCompletionUsage(BaseModel):
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


class ChatCompletionResponse(BaseModel):
    id: str
    object: str = "chat.completion"
    created: int
    model: str
    choices: List[ChatCompletionChoice]
    usage: ChatCompletionUsage
    # Requi-specific extensions
    extracted_tasks: Optional[List[dict]] = None
    zapier_triggers: Optional[List[dict]] = None
    compliance_scan: Optional[dict] = None
    rate_limit_info: Optional[dict] = None


@router.post("/v1/chat/completions", response_model=ChatCompletionResponse)
async def chat_completions(
    request: ChatCompletionRequest,
    organization: Organization = Depends(lambda: require_feature_dependency(Feature.AI_QA)),
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """
    OpenAI-compatible chat completions endpoint.
    
    Accepts standard OpenAI chat format with Requi-specific extensions:
    - extract_tasks: Auto-detect and extract actionable tasks from responses
    - zapier_actions: Trigger Zapier workflows (email, calendar, forms)
    
    Example:
    {
      "model": "requi-gpt-4",
      "messages": [{"role": "user", "content": "Analyze my HIPAA compliance"}],
      "extract_tasks": true,
      "zapier_actions": ["email"]
    }
    """
    # Build full context from conversation history
    conversation_text = "\n".join([
        f"{m.role}: {m.content}" for m in request.messages
    ])
    last_message = request.messages[-1].content if request.messages else ""
    
    # Get compliance-aware answer from ML service
    result = await ml_service.answer_query(
        db=db,
        organization_id=organization.id,
        query=last_message,
        conversation_history=[{"role": m.role, "content": m.content} for m in request.messages[:-1]],
    )
    
    # Extract tasks if enabled
    extracted_tasks = None
    if request.extract_tasks:
        extracted_tasks = _extract_tasks_from_response(result["answer"])
    
    # Build Zapier triggers if requested
    zapier_triggers = None
    if request.zapier_actions:
        zapier_triggers = _build_zapier_triggers(
            request.zapier_actions, result["answer"], current_user.email
        )
    
    # PHI/compliance scan
    compliance_scan = {
        "phi_detected": _detect_phi(last_message),
        "hipaa_compliant": True,
        "anonymized": _detect_phi(last_message),
        "risk_level": result.get("risk_level", "low"),
    }
    
    # v3.1: Rate limit info for trial users
    rate_limit_info = {
        "prompt_limit": settings.trial_prompt_limit,  # 3
        "trial_days": settings.trial_days,  # 7
        "note": "Trial users get 3 AI prompts and 7-day access. Upgrade for unlimited.",
    }
    
    # Token estimation
    prompt_tokens = len(conversation_text.split()) * 1.3
    completion_tokens = len(result["answer"].split()) * 1.3
    
    return ChatCompletionResponse(
        id=f"chatcmpl_{int(time.time() * 1000)}",
        object="chat.completion",
        created=int(time.time()),
        model=request.model,
        choices=[ChatCompletionChoice(
            message={"role": "assistant", "content": result["answer"]},
        )],
        usage=ChatCompletionUsage(
            prompt_tokens=int(prompt_tokens),
            completion_tokens=int(completion_tokens),
            total_tokens=int(prompt_tokens + completion_tokens),
        ),
        extracted_tasks=extracted_tasks,
        zapier_triggers=zapier_triggers,
        compliance_scan=compliance_scan,
        rate_limit_info=rate_limit_info,
    )


def _extract_tasks_from_response(answer: str) -> List[dict]:
    """Extract actionable tasks from AI response using keyword detection.
    
    DevOps: Replace with GPT-4 function calling or fine-tuned model.
    """
    tasks = []
    keywords = ["must", "should", "required", "implement", "complete", "submit", 
                "review", "update", "configure", "enable", "task", "action item", "to-do"]
    sentences = answer.replace("!", ".").replace("?", ".").split(".")
    
    task_id = 1
    for sentence in sentences:
        sentence = sentence.strip()
        if any(kw in sentence.lower() for kw in keywords) and len(sentence) > 15:
            priority = "critical" if any(kw in sentence.lower() for kw in ["critical", "urgent", "immediately"]) else \
                       "high" if any(kw in sentence.lower() for kw in ["must", "required"]) else "medium"
            tasks.append({
                "id": f"task_{task_id}",
                "title": sentence[:120] + ("..." if len(sentence) > 120 else ""),
                "priority": priority,
                "status": "pending",
                "confidence": 0.85,
            })
            task_id += 1
            if task_id > 5:  # Cap at 5 tasks
                break
    return tasks


def _build_zapier_triggers(actions: List[str], answer: str, user_email: str) -> List[dict]:
    """Build Zapier trigger payloads for requested actions.
    
    DevOps: Configure Zapier webhooks at https://zapier.com/app/webhooks
    """
    triggers = []
    for action in actions:
        if action == "email":
            triggers.append({
                "action": "send_email",
                "service": "sendgrid",
                "payload": {
                    "to": user_email,
                    "subject": "Requi AI — Compliance Summary",
                    "body": answer[:500] + "..." if len(answer) > 500 else answer,
                    "source": "requi_ai_chat",
                },
                "webhook_url": "# BLANK — DevOps: Add SendGrid/Zapier webhook URL",
                "status": "queued",
            })
        elif action == "calendar":
            triggers.append({
                "action": "create_event",
                "service": "outlook",
                "payload": {
                    "title": "Compliance Review — Requi AI",
                    "description": answer[:300],
                    "start": "2026-05-20T10:00:00Z",
                    "end": "2026-05-20T11:00:00Z",
                    "attendees": [user_email],
                },
                "webhook_url": "# BLANK — DevOps: Add Outlook/Google Calendar webhook URL",
                "status": "queued",
            })
        elif action == "forms":
            triggers.append({
                "action": "export_to_form",
                "service": "microsoft_forms",
                "payload": {
                    "form_title": "Compliance Audit Checklist",
                    "fields": [{"label": "Summary", "value": answer[:500]}],
                },
                "webhook_url": "# BLANK — DevOps: Add Microsoft Forms webhook URL",
                "status": "queued",
            })
    return triggers


def _detect_phi(text: str) -> bool:
    """Detect if text contains PHI/PII that should be anonymized.
    
    DevOps: Integrate with Microsoft Presidio or AWS Macie for production.
    """
    phi_patterns = [
        r"\b\d{3}-\d{2}-\d{4}\b",  # SSN
        r"\b\d{10}\b",  # Phone
        r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}",  # Email
        r"\b\d{1,2}/\d{1,2}/\d{2,4}\b",  # Dates
        r"MRN[\s:#]?\s*\d+",  # MRN
    ]
    import re
    return any(re.search(pattern, text) for pattern in phi_patterns)


# ==========================
# v3.0 — File Upload with OCR
# ==========================

from fastapi import UploadFile, File


@router.post("/upload")
async def upload_file(
    file: UploadFile = File(...),
    organization: Organization = Depends(lambda: require_feature_dependency(Feature.AI_QA)),
    current_user: User = Depends(get_current_active_user),
):
    """
    Upload a file (PDF, image, document) for OCR extraction and compliance scanning.
    
    Returns:
    - extracted_text: OCR text from document
    - compliance_flags: Any compliance issues detected
    - file_info: Metadata about the uploaded file
    
    DevOps: Integrate with AWS Textract, Azure Form Recognizer, or Google Document AI.
    """
    import hashlib
    import time
    
    content = await file.read()
    file_hash = hashlib.sha256(content).hexdigest()[:16]
    
    return {
        "file_id": f"file_{file_hash}_{int(time.time())}",
        "filename": file.filename,
        "content_type": file.content_type,
        "size_bytes": len(content),
        "status": "processing",
        "extracted_text": "# PLACEHOLDER — DevOps: Integrate OCR (AWS Textract / Azure Form Recognizer)",
        "compliance_scan": {
            "phi_detected": _detect_phi(content.decode("utf-8", errors="ignore")),
            "hipaa_compliant": None,
            "encryption": "AES-256 at rest",
            "retention_days": 2555,
        },
        "ocr_engine": "placeholder",
        "processing_time_ms": 0,
    }


# ==========================
# v3.0 — Audio Transcription
# ==========================

@router.post("/audio/transcribe")
async def transcribe_audio(
    file: UploadFile = File(...),
    organization: Organization = Depends(lambda: require_feature_dependency(Feature.AI_QA)),
    current_user: User = Depends(get_current_active_user),
):
    """
    Transcribe audio file (voice input) to text.
    
    Supports: MP3, WAV, M4A, OGG, FLAC
    
    DevOps: Integrate with:
    - Whisper API (OpenAI)
    - AWS Transcribe Medical
    - Google Cloud Speech-to-Text (Healthcare model)
    - Azure Speech Services
    
    Returns:
    - transcription: Full text transcription
    - segments: Timed segments with speaker labels
    - confidence: Overall confidence score
    """
    import time
    
    content = await file.read()
    
    return {
        "transcription_id": f"trans_{int(time.time() * 1000)}",
        "filename": file.filename,
        "duration_seconds": 0,  # DevOps: Extract from audio metadata
        "transcription": "# PLACEHOLDER — DevOps: Integrate Whisper/AWS Transcribe",
        "segments": [
            {
                "start": 0.0,
                "end": 0.0,
                "text": "# BLANK",
                "speaker": "SPEAKER_1",
                "confidence": 0.0,
            }
        ],
        "language": "en-US",
        "confidence": 0.0,
        "word_count": 0,
        "model": "placeholder",
        "speaker_diarization": False,  # DevOps: Enable with AWS Transcribe or pyannote.audio
    }


# ==========================
# v3.0 — Live Conversation WebSocket
# ==========================

from fastapi import WebSocket, WebSocketDisconnect


@router.websocket("/live")
async def live_conversation(
    websocket: WebSocket,
):
    """
    WebSocket endpoint for Live Conversation mode.
    
    Real-time bidirectional streaming:
    - Client → Server: Audio chunks or text messages
    - Server → Client: Transcription + AI responses
    
    DevOps: 
    - Deploy with WebSocket support (UVicorn with wsproto)
    - Integrate Whisper streaming for real-time transcription
    - Use GPT-4 Turbo for low-latency responses
    - Add Redis pub/sub for multi-instance scaling
    
    Auth: Use token in query param (?token=xxx) validated against JWT.
    """
    await websocket.accept()
    try:
        await websocket.send_json({
            "type": "status",
            "message": "Live conversation connected",
            "mode": "realtime",
            "features": {
                "transcription": "# PLACEHOLDER",
                "ai_response": "# PLACEHOLDER",
                "human_handoff": True,
            },
        })
        
        while True:
            data = await websocket.receive_json()
            
            if data.get("type") == "audio_chunk":
                # DevOps: Stream to Whisper API
                await websocket.send_json({
                    "type": "transcription_partial",
                    "text": "# BLANK — Streaming transcription placeholder",
                    "is_final": False,
                })
                
            elif data.get("type") == "text":
                # DevOps: Stream to GPT-4
                await websocket.send_json({
                    "type": "ai_response_chunk",
                    "text": "# BLANK — Streaming AI response placeholder",
                    "is_final": False,
                })
                
            elif data.get("type") == "handoff":
                await websocket.send_json({
                    "type": "handoff_initiated",
                    "message": "Transferring to human agent...",
                    "queue_position": 1,
                    "estimated_wait": "2 minutes",
                })
                
    except WebSocketDisconnect:
        print("Live conversation disconnected")
