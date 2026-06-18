"""Build workflow context for Intelligence chat (EPIC-003)."""

from __future__ import annotations

from typing import Optional
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.models import Document, User, WorkspaceTask, WorkspaceWorkflow

DOC_EXCERPT_CHARS = 4000


def _user_display(user: Optional[User]) -> Optional[str]:
    if not user:
        return None
    name = f"{user.first_name or ''} {user.last_name or ''}".strip()
    return name or user.email


def _doc_excerpt(doc: Document) -> str:
    if doc.content:
        text = doc.content.strip()
        if len(text) > DOC_EXCERPT_CHARS:
            return text[:DOC_EXCERPT_CHARS] + "\n…(truncated)"
        return text
    return "(No extracted text available)"


async def build_workflow_context(
    workflow: WorkspaceWorkflow,
    org_id: UUID,
    db: AsyncSession,
    *,
    focus_task_id: Optional[UUID] = None,
) -> dict:
    """Assemble case metadata, linked tasks, and document excerpts."""
    tasks_result = await db.execute(
        select(WorkspaceTask)
        .where(
            WorkspaceTask.organization_id == org_id,
            WorkspaceTask.workflow_id == workflow.id,
        )
        .options(
            selectinload(WorkspaceTask.assignee),
            selectinload(WorkspaceTask.reviewer),
            selectinload(WorkspaceTask.approver),
        )
        .order_by(WorkspaceTask.created_at.asc())
    )
    tasks = tasks_result.scalars().all()

    docs_result = await db.execute(
        select(Document)
        .where(
            Document.organization_id == org_id,
            Document.workflow_id == workflow.id,
            Document.is_active == True,
        )
        .order_by(Document.created_at.asc())
    )
    documents = docs_result.scalars().all()

    focus_task = None
    if focus_task_id:
        for t in tasks:
            if t.id == focus_task_id:
                focus_task = t
                break

    task_summaries = []
    for task in tasks:
        task_summaries.append(
            {
                "id": str(task.id),
                "title": task.title,
                "description": (task.description or "")[:2000],
                "status": task.status,
                "priority": task.priority,
                "category": task.category,
                "assignee": _user_display(task.assignee),
                "document_ids": list(task.document_ids or []),
                "has_resolution": bool(task.resolution_result),
                "is_focus": focus_task_id is not None and task.id == focus_task_id,
            }
        )

    doc_excerpts = []
    for doc in documents:
        meta = doc.document_metadata or {}
        doc_excerpts.append(
            {
                "id": str(doc.id),
                "title": doc.title,
                "type": (meta.get("file_extension") or "file").upper(),
                "excerpt": _doc_excerpt(doc),
            }
        )

    return {
        "workflow_id": str(workflow.id),
        "reference_code": workflow.reference_code,
        "case_number": workflow.external_ref,
        "title": workflow.title,
        "description": workflow.description or "",
        "status": workflow.status,
        "priority": workflow.priority,
        "category": workflow.category,
        "source": workflow.source,
        "due_date": workflow.due_date,
        "task_count": len(task_summaries),
        "document_count": len(doc_excerpts),
        "tasks": task_summaries,
        "documents": doc_excerpts,
        "focus_task": (
            {
                "id": str(focus_task.id),
                "title": focus_task.title,
                "description": focus_task.description or "",
                "status": focus_task.status,
                "document_ids": [str(d) for d in (focus_task.document_ids or [])],
            }
            if focus_task
            else None
        ),
    }


def format_workflow_context_for_prompt(ctx: dict, *, task_execution: bool = False) -> str:
    """Render context dict as system-injectable text for the AI."""
    lines = [
        "=== WORKFLOW CASE CONTEXT ===",
        f"Reference: {ctx.get('reference_code')}",
        f"Case #: {ctx.get('case_number') or 'N/A'}",
        f"Title: {ctx.get('title')}",
        f"Status: {ctx.get('status')} | Priority: {ctx.get('priority')}",
        f"Description: {ctx.get('description') or '(none)'}",
        "",
        "--- Linked Tasks ---",
    ]
    for t in ctx.get("tasks") or []:
        marker = " [FOCUS TASK]" if t.get("is_focus") else ""
        lines.append(
            f"• {t['title']}{marker} (status: {t['status']}, id: {t['id']})"
        )
        if t.get("description"):
            lines.append(f"  Description: {t['description'][:500]}")
    lines.append("")
    lines.append("--- Linked Documents ---")
    for d in ctx.get("documents") or []:
        lines.append(f"• {d['title']} ({d['type']}, id: {d['id']})")
        lines.append(f"  Excerpt:\n{d['excerpt']}")
    lines.append("=== END WORKFLOW CONTEXT ===")

    if task_execution and ctx.get("focus_task"):
        ft = ctx["focus_task"]
        lines.extend(
            [
                "",
                "=== TASK EXECUTION MODE ===",
                f"You are resolving task: {ft['title']}",
                f"Task ID: {ft['id']}",
                f"Task description: {ft.get('description') or '(none)'}",
                "",
                TASK_EXECUTION_JSON_INSTRUCTION,
            ]
        )
    return "\n".join(lines)


TASK_EXECUTION_JSON_INSTRUCTION = """When resolving this task, end your response with a machine-readable JSON block wrapped in ```json ... ``` using this schema:
{
  "task_id": "<uuid>",
  "summary": "2-3 sentence executive summary",
  "findings": [{"id": "F1", "severity": "high|medium|low", "title": "...", "detail": "..."}],
  "recommendations": ["..."],
  "risk_level": "high|medium|low",
  "confidence": 0.0,
  "deliverable": {
    "include_document": false,
    "document_type": "resolution_memo",
    "title": "",
    "reason": "",
    "body_markdown": ""
  }
}
Set deliverable.include_document to true ONLY when a formal written deliverable (memo, report, letter) is required. Otherwise keep it false.
Always include the JSON block when in task execution mode."""
