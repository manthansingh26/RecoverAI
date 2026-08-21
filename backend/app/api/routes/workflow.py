"""Development workflow processing endpoint.

This endpoint is for local development and hackathon demos ONLY.
It manually triggers the recovery workflow processor to find and
process eligible RecoveryCase records.

Does NOT execute real Razorpay actions.
Does NOT run in production.
"""

import logging

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.session import get_db
from app.services.recovery_workflow import (
    WorkflowSummary,
    discover_due_cases,
    process_received_cases,
)

logger = logging.getLogger(__name__)

router = APIRouter()


def _is_workflow_enabled() -> bool:
    """Check if workflow endpoints are enabled for current environment."""
    return settings.APP_ENV in ("development", "test")


class WorkflowResponse(BaseModel):
    """Response for the workflow processing endpoint."""

    success: bool
    received_processed: int
    received_skipped: int
    due_cases_found: int
    results: list[dict]
    message: str


def _summary_to_response(summary: WorkflowSummary) -> WorkflowResponse:
    """Convert a WorkflowSummary to a WorkflowResponse."""
    return WorkflowResponse(
        success=True,
        received_processed=summary.received_processed,
        received_skipped=summary.received_skipped,
        due_cases_found=summary.due_cases_found,
        results=[
            {
                "recovery_case_id": r.recovery_case_id,
                "previous_status": r.previous_status,
                "new_status": r.new_status,
                "processed": r.processed,
                "message": r.message,
            }
            for r in summary.results
        ],
        message=(
            f"Processed {summary.received_processed} RECEIVED cases, "
            f"skipped {summary.received_skipped}, "
            f"discovered {summary.due_cases_found} due cases"
        ),
    )


@router.post("/api/dev/process-recovery-workflow")
async def process_recovery_workflow(
    db: Session = Depends(get_db),
) -> WorkflowResponse:
    """Manually trigger recovery workflow processing for development/testing.

    This endpoint:
    - Only works in development or test environments.
    - Processes all eligible RECEIVED cases through the Decision Engine.
    - Discovers due PENDING_EXECUTION cases (discovery only, no execution).
    - Does NOT perform any real financial actions.

    Args:
        db: Database session dependency.

    Returns:
        WorkflowResponse with processing results.

    Raises:
        HTTPException 404: Workflow endpoint not available outside dev/test.
    """
    if not _is_workflow_enabled():
        from fastapi import HTTPException

        raise HTTPException(
            status_code=404,
            detail="Workflow endpoint not available in this environment",
        )

    # Process RECEIVED cases
    received_summary = process_received_cases(db)

    # Discover due PENDING_EXECUTION cases
    due_summary = discover_due_cases(db)

    # Combine results
    combined = WorkflowSummary(
        received_processed=received_summary.received_processed,
        received_skipped=received_summary.received_skipped,
        due_cases_found=due_summary.due_cases_found,
        results=received_summary.results + due_summary.results,
    )

    logger.info(
        "Manual workflow trigger: processed=%d skipped=%d due=%d",
        combined.received_processed,
        combined.received_skipped,
        combined.due_cases_found,
    )

    return _summary_to_response(combined)
