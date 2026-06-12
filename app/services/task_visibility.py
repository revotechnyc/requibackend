"""Task and workflow visibility — assignment-based access for team members."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import UserRole, WorkspaceTask, WorkspaceWorkflow
from app.services.enterprise_roles import is_workspace_admin


def user_has_full_task_access(user_role: UserRole) -> bool:
    """Admin and enterprise_admin see all tasks and workflows."""
    return is_workspace_admin(user_role)


def task_assignee_is_user(task: WorkspaceTask, user_id: UUID) -> bool:
    """User is the assignee on this task."""
    return task.assignee_id is not None and task.assignee_id == user_id


def task_assigned_to_user(task: WorkspaceTask, user_id: UUID) -> bool:
    """User is assignee, reviewer, or approver — used for Tasks tab visibility."""
    return (
        task_assignee_is_user(task, user_id)
        or (task.reviewer_id is not None and task.reviewer_id == user_id)
        or (task.approver_id is not None and task.approver_id == user_id)
    )


def task_visible_to_user(task: WorkspaceTask, user_id: UUID, user_role: UserRole) -> bool:
    if user_has_full_task_access(user_role):
        return True
    return task_assigned_to_user(task, user_id)


async def workflow_ids_visible_to_user(
    org_id: UUID,
    user_id: UUID,
    user_role: UserRole,
    db: AsyncSession,
    workflow_ids: list[UUID],
) -> set[UUID]:
    """Workflow IDs visible in Workflow tab — assignee on a linked task only."""
    if not workflow_ids:
        return set()
    if user_has_full_task_access(user_role):
        return set(workflow_ids)

    task_result = await db.execute(
        select(WorkspaceTask.workflow_id)
        .where(
            WorkspaceTask.organization_id == org_id,
            WorkspaceTask.workflow_id.in_(workflow_ids),
            WorkspaceTask.workflow_id.isnot(None),
            WorkspaceTask.assignee_id == user_id,
        )
        .distinct()
    )
    return {wid for wid in task_result.scalars().all() if wid is not None}


async def workflow_visible_to_user(
    workflow: WorkspaceWorkflow,
    org_id: UUID,
    user_id: UUID,
    user_role: UserRole,
    db: AsyncSession,
) -> bool:
    visible = await workflow_ids_visible_to_user(
        org_id, user_id, user_role, db, [workflow.id]
    )
    return workflow.id in visible
