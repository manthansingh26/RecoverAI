"""Development workflow processing endpoint.

This endpoint is for local development and hackathon demos ONLY.
It manually triggers the recovery workflow processor to find and
process eligible RecoveryCase records.

Does NOT execute real Razorpay actions.
Does NOT run in production.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.deps import require_origin, require_permission
from app.core.config import settings
from app.core.roles import Permission
from app.db.session import get_db
from app.models.operator import Operator
from app.services.recovery_workflow import (
    WorkflowSummary,
    discover_and_execute_due_cases,
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
    execution_attempted: int
    execution_succeeded: int
    execution_failed: int
    execution_blocked: int
    results: list[dict]
    message: str


def _summary_to_response(summary: WorkflowSummary) -> WorkflowResponse:
    """Convert a WorkflowSummary to a WorkflowResponse."""
    return WorkflowResponse(
        success=True,
        received_processed=summary.received_processed,
        received_skipped=summary.received_skipped,
        due_cases_found=summary.due_cases_found,
        execution_attempted=summary.execution_attempted,
        execution_succeeded=summary.execution_succeeded,
        execution_failed=summary.execution_failed,
        execution_blocked=summary.execution_blocked,
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
            f"discovered {summary.due_cases_found} due cases, "
            f"executed {summary.execution_attempted} ("
            f"{summary.execution_succeeded} succeeded, "
            f"{summary.execution_failed} failed, "
            f"{summary.execution_blocked} blocked)"
        ),
    )


@router.post(
    "/api/dev/process-recovery-workflow",
    dependencies=[Depends(require_origin)],
)
async def process_recovery_workflow(
    db: Session = Depends(get_db),
    operator: Operator = Depends(require_permission(Permission.RUN_WORKFLOW)),
) -> WorkflowResponse:
    """Manually trigger recovery workflow processing for development/testing.

    This endpoint:
    - Only works in development or test environments.
    - Processes all eligible RECEIVED cases through the Decision Engine.
    - Discovers and executes due PENDING_EXECUTION cases.
    - In SIMULATION mode (default), no real financial actions occur.
    - Requires RUN_WORKFLOW permission. Actor recorded on executions.

    Args:
        db: Database session dependency.
        operator: Authenticated operator.

    Returns:
        WorkflowResponse with processing and execution results.

    Raises:
        HTTPException 404: Workflow endpoint not available outside dev/test.
    """
    if not _is_workflow_enabled():
        raise HTTPException(
            status_code=404,
            detail="Workflow endpoint not available in this environment",
        )

    # Process RECEIVED cases
    received_summary = process_received_cases(db)

    # Discover and execute due PENDING_EXECUTION cases (audited to this operator)
    exec_summary = discover_and_execute_due_cases(
        db, actor=operator.email
    )

    # Combine results
    combined = WorkflowSummary(
        received_processed=received_summary.received_processed,
        received_skipped=received_summary.received_skipped,
        due_cases_found=exec_summary.due_cases_found,
        execution_attempted=exec_summary.execution_attempted,
        execution_succeeded=exec_summary.execution_succeeded,
        execution_failed=exec_summary.execution_failed,
        execution_blocked=exec_summary.execution_blocked,
        results=received_summary.results + exec_summary.results,
    )

    logger.info(
        "Manual workflow trigger: processed=%d skipped=%d due=%d executed=%d",
        combined.received_processed,
        combined.received_skipped,
        combined.due_cases_found,
        combined.execution_attempted,
    )

    return _summary_to_response(combined)
