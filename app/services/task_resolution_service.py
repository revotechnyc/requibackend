"""Parse and persist AI task resolution results."""

from __future__ import annotations

import hashlib
import json
import re
import uuid as uuid_lib
from datetime import datetime
from typing import Optional, Tuple
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Document, User, WorkspaceTask, WorkflowFinding
from app.services.document_storage import content_type_for_extension, save_document_file
from app.services.workflow_service import log_workflow_activity


def parse_resolution_json(content: str) -> Optional[dict]:
    """Extract structured task resolution JSON from AI response."""
    if not content:
        return None
    # Prefer fenced ```json block
    fence = re.search(r"```json\s*([\s\S]*?)\s*```", content, re.IGNORECASE)
    if fence:
        try:
            return json.loads(fence.group(1).strip())
        except json.JSONDecodeError:
            pass
    # Fallback: last JSON object in text
    for match in re.finditer(r"\{[\s\S]*\}", content):
        try:
            obj = json.loads(match.group(0))
            if isinstance(obj, dict) and "summary" in obj:
                return obj
        except json.JSONDecodeError:
            continue
    return None


def _format_file_size(num_bytes: int) -> str:
    if num_bytes < 1024:
        return f"{num_bytes} B"
    if num_bytes < 1024 * 1024:
        return f"{round(num_bytes / 1024, 1)} KB"
    return f"{round(num_bytes / (1024 * 1024), 1)} MB"


async def create_resolution_text_document(
    db: AsyncSession,
    org_id: UUID,
    user: User,
    *,
    workflow_id: Optional[UUID],
    title: str,
    body_markdown: str,
) -> Document:
    """Create a text document from AI deliverable markdown."""
    safe_title = (title or "Resolution Memo").strip()[:200]
    filename = f"{safe_title}.md"
    if not filename.endswith(".md"):
        filename += ".md"
    raw = body_markdown.encode("utf-8")
    doc_id = uuid_lib.uuid4()
    uploader = f"{user.first_name} {user.last_name}".strip() or user.email
    storage_path = save_document_file(
        org_id,
        doc_id,
        filename,
        raw,
        content_type="text/markdown; charset=utf-8",
    )
    document = Document(
        id=doc_id,
        organization_id=org_id,
        workflow_id=workflow_id,
        title=safe_title,
        content=body_markdown,
        content_hash=hashlib.sha256(raw).hexdigest(),
        storage_path=storage_path,
        document_metadata={
            "category": "Resolution",
            "file_extension": "md",
            "content_type": content_type_for_extension("md"),
            "size_bytes": len(raw),
            "size_display": _format_file_size(len(raw)),
            "uploaded_by": uploader,
            "uploaded_by_id": str(user.id),
            "source": "ai_task_resolution",
            "ingestion_status": "complete",
        },
    )
    db.add(document)
    await db.flush()
    return document


async def save_task_resolution(
    db: AsyncSession,
    task: WorkspaceTask,
    org_id: UUID,
    user: User,
    resolution: dict,
    *,
    conversation_id: Optional[UUID] = None,
    raw_content: Optional[str] = None,
) -> Tuple[WorkspaceTask, Optional[WorkflowFinding]]:
    """Persist resolution JSON on task; optionally create deliverable document + workflow finding."""
    resolution_doc = None
    deliverable = resolution.get("deliverable") or {}
    if deliverable.get("include_document") and deliverable.get("body_markdown"):
        resolution_doc = await create_resolution_text_document(
            db,
            org_id,
            user,
            workflow_id=task.workflow_id,
            title=deliverable.get("title") or f"Resolution — {task.title}",
            body_markdown=str(deliverable["body_markdown"]),
        )
        task.resolution_document_id = resolution_doc.id
        doc_ids = list(task.document_ids or [])
        doc_id_str = str(resolution_doc.id)
        if doc_id_str not in doc_ids:
            doc_ids.append(doc_id_str)
            task.document_ids = doc_ids

    finding = None
    finding_id: Optional[uuid_lib.UUID] = None
    if task.workflow_id:
        finding_id = uuid_lib.uuid4()
        finding = WorkflowFinding(
            id=finding_id,
            workflow_id=task.workflow_id,
            organization_id=org_id,
            task_id=task.id,
            created_by_id=user.id,
            summary=str(resolution.get("summary") or ""),
            findings=resolution.get("findings") or [],
            risk_level=resolution.get("risk_level"),
            recommendations=resolution.get("recommendations") or [],
            evidence_refs=resolution.get("evidence_refs") or [],
            raw_payload={"resolution": resolution, "raw_content_preview": (raw_content or "")[:500]},
            created_at=datetime.utcnow(),
        )
        db.add(finding)
        await log_workflow_activity(
            db,
            task.workflow_id,
            org_id,
            user.id,
            "task_resolution_saved",
            {
                "task_id": str(task.id),
                "task_title": task.title,
                "risk_level": resolution.get("risk_level"),
                "has_document": bool(resolution_doc),
            },
        )

    saved_at = datetime.utcnow()
    history_entry = {
        "id": str(uuid_lib.uuid4()),
        "resolution": resolution,
        "summary": str(resolution.get("summary") or ""),
        "risk_level": resolution.get("risk_level"),
        "conversation_id": str(conversation_id) if conversation_id else None,
        "finding_id": str(finding_id) if finding_id else None,
        "resolution_document_id": str(resolution_doc.id) if resolution_doc else None,
        "resolution_document_title": resolution_doc.title if resolution_doc else None,
        "saved_by": f"{user.first_name or ''} {user.last_name or ''}".strip() or user.email,
        "created_at": saved_at.isoformat(),
    }
    resolution_history = list(task.resolution_history or [])
    resolution_history.append(history_entry)
    task.resolution_history = resolution_history
    task.resolution_result = resolution
    task.updated_at = saved_at
    if conversation_id:
        task.execution_conversation_id = conversation_id

    history = list(task.history or [])
    history.append(
        {
            "action": "resolution_saved",
            "user_id": str(user.id),
            "user_name": f"{user.first_name or ''} {user.last_name or ''}".strip() or user.email,
            "timestamp": saved_at.isoformat(),
            "has_document": bool(resolution_doc),
            "resolution_entry_id": history_entry["id"],
        }
    )
    task.history = history

    await db.flush()
    return task, finding
