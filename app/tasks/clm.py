"""Background CLM contract metadata and obligation processing."""

import asyncio
import logging
from uuid import UUID

from celery import shared_task

from app.db.database import get_db_context
from app.db.models import ClmContract, Organization, User
from app.services.clm_service import process_contract_after_upload


logger = logging.getLogger(__name__)


async def process_clm_contract_background(
    contract_id: str,
    organization_id: str,
    user_id: str,
) -> dict:
    async with get_db_context() as db:
        contract = await db.get(ClmContract, UUID(contract_id))
        organization = await db.get(Organization, UUID(organization_id))
        user = await db.get(User, UUID(user_id))
        if not contract or not organization or not user:
            return {"status": "not_found", "contract_id": contract_id}
        if contract.organization_id != organization.id:
            return {"status": "invalid_scope", "contract_id": contract_id}
        if (
            isinstance(contract.ai_extraction, dict)
            and contract.ai_extraction.get("extracted_at")
            and contract.status != "processing"
        ):
            return {"status": "already_processed", "contract_id": contract_id}
        await process_contract_after_upload(db, contract.id, organization, user)
        return {"status": "processed", "contract_id": contract_id}


@shared_task(bind=True, max_retries=2)
def process_clm_contract_task(
    self,
    contract_id: str,
    organization_id: str,
    user_id: str,
):
    try:
        return asyncio.run(
            process_clm_contract_background(
                contract_id, organization_id, user_id
            )
        )
    except Exception as exc:
        logger.exception("CLM processing failed for %s", contract_id)
        raise self.retry(exc=exc, countdown=30) from exc
