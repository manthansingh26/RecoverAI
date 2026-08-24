"""Recovery workflow processor — finds and processes eligible RecoveryCase records.

Responsibilities:
- Discover RECEIVED cases and run the Decision Engine on them
- Discover due PENDING_EXECUTION cases (next_run_at <= now)
- Execute eligible due cases via the Recovery Executor
- Ensure idempotency and concurrency safety

This module is the "scheduler" entry point that connects ingestion
to the decision engine and execution engine without background threads.
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.enums import RecoveryStatus
from app.models.recovery_case import RecoveryCase
from app.services.decision_engine import run_decision_engine
from app.services.recovery_executor import (
    ExecutionSummary,
    SimulationBehavior,
    execute_due_cases,
)

logger = logging.getLogger(__name__)


@dataclass
class WorkflowResult:
    """Result of a single case processing attempt."""

    recovery_case_id: str
    previous_status: str
    new_status: str
    processed: bool
    message: str


@dataclass
class WorkflowSummary:
    """Aggregate result of a workflow processing run."""

    received_processed: int
    received_skipped: int
    due_cases_found: int
    execution_attempted: int = 0
    execution_succeeded: int = 0
    execution_failed: int = 0
    execution_blocked: int = 0
    results: list[WorkflowResult] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.results is None:
            self.results = []


def get_received_cases(db: Session) -> list[RecoveryCase]:
    """Find all RecoveryCase records in RECEIVED status.

    Uses SELECT ... FOR UPDATE to lock rows and prevent concurrent
    processing of the same case by parallel workflow invocations.

    Args:
        db: Active database session.

    Returns:
        List of RecoveryCase records locked for update.
    """
    stmt = (
        select(RecoveryCase)
        .where(RecoveryCase.status == RecoveryStatus.RECEIVED.value)
        .with_for_update(skip_locked=True)
    )
    return list(db.execute(stmt).scalars().all())


def get_due_recovery_cases(db: Session) -> list[RecoveryCase]:
    """Find PENDING_EXECUTION cases whose next_run_at <= current UTC time.

    These cases are ready for execution (Milestone 5 will act on them).
    In Milestone 4, this is used for discovery only — no execution occurs.

    Uses SELECT ... FOR UPDATE SKIP LOCKED for safe concurrent access.

    Args:
        db: Active database session.

    Returns:
        List of due RecoveryCase records locked for update.
    """
    now = datetime.now(timezone.utc)
    stmt = (
        select(RecoveryCase)
        .where(
            RecoveryCase.status == RecoveryStatus.PENDING_EXECUTION.value,
            RecoveryCase.next_run_at <= now,
        )
        .with_for_update(skip_locked=True)
    )
    return list(db.execute(stmt).scalars().all())


def process_received_case(
    db: Session,
    recovery_case_id: str,
) -> WorkflowResult:
    """Process a single RECEIVED case through the Decision Engine.

    Re-checks the case status before processing to prevent races.
    If the case is no longer in RECEIVED status, it is safely skipped.

    Args:
        db: Active database session.
        recovery_case_id: UUID string of the RecoveryCase.

    Returns:
        WorkflowResult describing what happened.
    """
    try:
        rc_uuid = uuid.UUID(recovery_case_id)
    except ValueError:
        return WorkflowResult(
            recovery_case_id=recovery_case_id,
            previous_status="UNKNOWN",
            new_status="UNKNOWN",
            processed=False,
            message="Invalid recovery_case_id format",
        )

    # Lock the row for update to prevent concurrent processing
    rc = db.execute(
        select(RecoveryCase)
        .where(RecoveryCase.id == rc_uuid)
        .with_for_update(skip_locked=True)
    ).scalar_one_or_none()

    if rc is None:
        return WorkflowResult(
            recovery_case_id=recovery_case_id,
            previous_status="NOT_FOUND",
            new_status="NOT_FOUND",
            processed=False,
            message="RecoveryCase not found",
        )

    previous_status = rc.status

    # Idempotency: skip if no longer RECEIVED
    if rc.status != RecoveryStatus.RECEIVED.value:
        return WorkflowResult(
            recovery_case_id=recovery_case_id,
            previous_status=previous_status,
            new_status=rc.status,
            processed=False,
            message=f"Case is in {rc.status} state, not RECEIVED — skipping",
        )

    # Run the Decision Engine (which commits its own changes)
    result_rc = run_decision_engine(db, recovery_case_id)

    if result_rc is None:
        return WorkflowResult(
            recovery_case_id=recovery_case_id,
            previous_status=previous_status,
            new_status=previous_status,
            processed=False,
            message="Decision engine returned None",
        )

    return WorkflowResult(
        recovery_case_id=recovery_case_id,
        previous_status=previous_status,
        new_status=result_rc.status,
        processed=True,
        message=f"Processed: {previous_status} -> {result_rc.status}",
    )


def process_received_cases(db: Session) -> WorkflowSummary:
    """Process all eligible RECEIVED cases through the Decision Engine.

    This is the main workflow entry point. It:
    1. Finds all RECEIVED cases (with row locking)
    2. Processes each through the Decision Engine
    3. Returns a summary of what was done

    Args:
        db: Active database session.

    Returns:
        WorkflowSummary with counts and per-case results.
    """
    cases = get_received_cases(db)
    results: list[WorkflowResult] = []

    for rc in cases:
        result = process_received_case(db, str(rc.id))
        results.append(result)

    processed = sum(1 for r in results if r.processed)
    skipped = len(results) - processed

    logger.info(
        "Workflow processed %d RECEIVED cases (%d processed, %d skipped)",
        len(results),
        processed,
        skipped,
    )

    return WorkflowSummary(
        received_processed=processed,
        received_skipped=skipped,
        due_cases_found=0,  # Not querying due cases in this pass
        results=results,
    )


def discover_and_execute_due_cases(
    db: Session,
    *,
    sim_behavior: SimulationBehavior | None = None,
    actor: str | None = None,
) -> WorkflowSummary:
    """Discover due PENDING_EXECUTION cases and execute them.

    In Milestone 5, due cases are passed to the execution engine.
    Execution mode is controlled by config (default: SIMULATION).

    Args:
        db: Active database session.
        sim_behavior: Optional simulation behavior override for testing.
        actor: Optional actor identifier for audit attribution on executions
               (defaults to "system:scheduler").

    Returns:
        WorkflowSummary with due case and execution information.
    """
    # First, discover due cases for reporting
    due_cases = get_due_recovery_cases(db)

    # Execute eligible due cases
    exec_summary = execute_due_cases(db, sim_behavior=sim_behavior, actor=actor)

    # Build results from execution
    results: list[WorkflowResult] = []
    for er in exec_summary.results:
        results.append(WorkflowResult(
            recovery_case_id=er.recovery_case_id,
            previous_status=er.previous_case_status,
            new_status=er.new_case_status,
            processed=er.status != "BLOCKED",
            message=er.message,
        ))

    logger.info(
        "Workflow due cases: discovered=%d attempted=%d succeeded=%d failed=%d blocked=%d",
        len(due_cases),
        exec_summary.attempted,
        exec_summary.succeeded,
        exec_summary.failed,
        exec_summary.blocked,
    )

    return WorkflowSummary(
        received_processed=0,
        received_skipped=0,
        due_cases_found=len(due_cases),
        execution_attempted=exec_summary.attempted,
        execution_succeeded=exec_summary.succeeded,
        execution_failed=exec_summary.failed,
        execution_blocked=exec_summary.blocked,
        results=results,
    )
