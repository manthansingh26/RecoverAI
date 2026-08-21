"""Tests for the Recovery Execution Engine (Milestone 5).

Covers:
1. WAIT_AND_RETRY simulation success
2. WAIT_AND_RETRY simulation failure schedules another retry
3. WAIT_AND_RETRY stops after max retries
4. CREATE_PAYMENT_LINK simulation success
5. HUMAN_REVIEW is blocked
6. STOP_RECOVERY is never financially executed
7. High-value cases requiring approval are blocked
8. Non-due cases are not executed
9. Non-PENDING_EXECUTION cases are not executed
10. Duplicate workflow trigger does not duplicate execution
11. Execution creates appropriate ExecutionLog records
12. Simulation mode creates no real external API calls
13. Missing real-mode credentials fail closed
14. Development endpoint reports execution summary
15. Production environment still returns 404
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.core.config import settings
from app.main import app
from app.models.enums import (
    ExecutionMode,
    ExecutionStatus,
    FailureCategory,
    RecoveryStatus,
    RecoveryStrategy,
)
from app.models.execution_log import ExecutionLog
from app.models.payment_event import PaymentEvent
from app.models.recovery_case import RecoveryCase
from app.services.recovery_executor import (
    ExecutionResult,
    SimulationBehavior,
    _resolve_execution_mode,
    execute_due_cases,
    execute_single_case,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _create_test_payment_event(
    db,
    *,
    error_reason: str = "bank_technical_error",
    amount_paise: int = 100_000,
) -> PaymentEvent:
    """Create a test PaymentEvent in the database."""
    pe = PaymentEvent(
        event_type="payment.failed",
        external_event_id=f"evt_exec_{uuid.uuid4().hex[:12]}",
        external_payment_id=f"pay_exec_{uuid.uuid4().hex[:12]}",
        external_order_id=f"order_exec_{uuid.uuid4().hex[:12]}",
        amount_paise=amount_paise,
        currency="INR",
        error_code="payment_failed",
        error_reason=error_reason,
        error_description="Test payment failure",
        raw_payload={"test": True},
        payload_hash=uuid.uuid4().hex,
    )
    db.add(pe)
    db.flush()
    return pe


def _create_pending_case(
    db,
    payment_event: PaymentEvent,
    *,
    strategy: str = RecoveryStrategy.WAIT_AND_RETRY.value,
    retry_count: int = 0,
    next_run_at: datetime | None = None,
    requires_human_approval: bool = False,
    approved_by_human: bool | None = None,
) -> RecoveryCase:
    """Create a RecoveryCase in PENDING_EXECUTION state."""
    if next_run_at is None:
        next_run_at = datetime.now(timezone.utc) - timedelta(minutes=5)

    rc = RecoveryCase(
        payment_event_id=payment_event.id,
        status=RecoveryStatus.PENDING_EXECUTION.value,
        failure_category=FailureCategory.TRANSIENT.value,
        recovery_probability=0.8,
        priority_score=800.0,
        recommended_strategy=strategy,
        expected_value_paise=80000,
        decision_audit_trail={
            "ingestion": {"source": "test"},
            "classification": {"category": "TRANSIENT"},
            "recommendation": {"strategy": strategy},
            "policy": {"approved": True},
        },
        next_run_at=next_run_at,
        retry_count=retry_count,
        requires_human_approval=requires_human_approval,
        approved_by_human=approved_by_human,
    )
    db.add(rc)
    db.flush()
    return rc


# ---------------------------------------------------------------------------
# 1. WAIT_AND_RETRY simulation success
# ---------------------------------------------------------------------------

class TestWaitAndRetrySuccess:
    """WAIT_AND_RETRY simulation success path."""

    def test_successful_retry_schedules_next(self, db_session) -> None:
        pe = _create_test_payment_event(db_session)
        rc = _create_pending_case(db_session, pe, strategy=RecoveryStrategy.WAIT_AND_RETRY.value)
        db_session.commit()

        sim = SimulationBehavior()
        sim.set_outcome(RecoveryStrategy.WAIT_AND_RETRY.value, ExecutionStatus.SUCCESS.value)

        result = execute_single_case(db_session, str(rc.id), sim_behavior=sim)

        assert result is not None
        assert result.status == ExecutionStatus.SUCCESS.value
        assert result.execution_mode == "SIMULATION"

        db_session.refresh(rc)
        assert rc.status == RecoveryStatus.PENDING_EXECUTION.value
        assert rc.retry_count == 1
        assert rc.next_run_at is not None
        assert rc.next_run_at > datetime.now(timezone.utc)

    def test_execution_log_created(self, db_session) -> None:
        pe = _create_test_payment_event(db_session)
        rc = _create_pending_case(db_session, pe, strategy=RecoveryStrategy.WAIT_AND_RETRY.value)
        db_session.commit()

        sim = SimulationBehavior()
        execute_single_case(db_session, str(rc.id), sim_behavior=sim)

        logs = db_session.execute(
            select(ExecutionLog).where(ExecutionLog.recovery_case_id == rc.id)
        ).scalars().all()
        assert len(logs) == 1
        assert logs[0].status == ExecutionStatus.SUCCESS.value
        assert logs[0].execution_mode == ExecutionMode.SIMULATION.value


# ---------------------------------------------------------------------------
# 2. WAIT_AND_RETRY simulation failure schedules another retry
# ---------------------------------------------------------------------------

class TestWaitAndRetryFailure:
    """WAIT_AND_RETRY simulation failure path."""

    def test_failed_retry_increments_count(self, db_session) -> None:
        pe = _create_test_payment_event(db_session)
        rc = _create_pending_case(
            db_session, pe,
            strategy=RecoveryStrategy.WAIT_AND_RETRY.value,
            retry_count=0,
        )
        db_session.commit()

        sim = SimulationBehavior()
        sim.set_outcome(RecoveryStrategy.WAIT_AND_RETRY.value, ExecutionStatus.FAILED.value)

        result = execute_single_case(db_session, str(rc.id), sim_behavior=sim)

        assert result is not None
        assert result.status == ExecutionStatus.FAILED.value

        db_session.refresh(rc)
        assert rc.retry_count == 1  # Incremented even on failure
        assert rc.next_run_at is not None  # Scheduled for another retry
        assert rc.status == RecoveryStatus.PENDING_EXECUTION.value


# ---------------------------------------------------------------------------
# 3. WAIT_AND_RETRY stops after max retries
# ---------------------------------------------------------------------------

class TestWaitAndRetryMaxRetries:
    """WAIT_AND_RETRY stops after max retries."""

    def test_max_retries_resolves_failed(self, db_session) -> None:
        pe = _create_test_payment_event(db_session)
        rc = _create_pending_case(
            db_session, pe,
            strategy=RecoveryStrategy.WAIT_AND_RETRY.value,
            retry_count=settings.RECOVERY_MAX_RETRIES,
        )
        db_session.commit()

        sim = SimulationBehavior()
        sim.set_outcome(RecoveryStrategy.WAIT_AND_RETRY.value, ExecutionStatus.FAILED.value)

        result = execute_single_case(db_session, str(rc.id), sim_behavior=sim)

        assert result is not None
        assert result.status == ExecutionStatus.FAILED.value

        db_session.refresh(rc)
        assert rc.status == RecoveryStatus.RESOLVED_FAILED.value
        assert rc.next_run_at is None


# ---------------------------------------------------------------------------
# 4. CREATE_PAYMENT_LINK simulation success
# ---------------------------------------------------------------------------

class TestCreatePaymentLinkSuccess:
    """CREATE_PAYMENT_LINK simulation success path."""

    def test_link_created_success(self, db_session) -> None:
        pe = _create_test_payment_event(db_session, error_reason="authentication_failed")
        rc = _create_pending_case(
            db_session, pe,
            strategy=RecoveryStrategy.CREATE_PAYMENT_LINK.value,
        )
        db_session.commit()

        sim = SimulationBehavior()
        sim.set_outcome(RecoveryStrategy.CREATE_PAYMENT_LINK.value, ExecutionStatus.SUCCESS.value)

        result = execute_single_case(db_session, str(rc.id), sim_behavior=sim)

        assert result is not None
        assert result.status == ExecutionStatus.SUCCESS.value

        db_session.refresh(rc)
        assert rc.status == RecoveryStatus.PENDING_EXECUTION.value
        # Link created but not yet paid — stays pending
        assert rc.next_run_at is not None

    def test_response_distinguishes_link_from_payment(self, db_session) -> None:
        pe = _create_test_payment_event(db_session, error_reason="authentication_failed")
        rc = _create_pending_case(
            db_session, pe,
            strategy=RecoveryStrategy.CREATE_PAYMENT_LINK.value,
        )
        db_session.commit()

        sim = SimulationBehavior()
        execute_single_case(db_session, str(rc.id), sim_behavior=sim)

        logs = db_session.execute(
            select(ExecutionLog).where(ExecutionLog.recovery_case_id == rc.id)
        ).scalars().all()
        assert len(logs) == 1
        resp = logs[0].response_data
        assert resp.get("link_created") is True
        assert resp.get("payment_recovered") is False  # Key distinction


# ---------------------------------------------------------------------------
# 5. HUMAN_REVIEW is blocked
# ---------------------------------------------------------------------------

class TestHumanReviewBlocked:
    """HUMAN_REVIEW strategy always blocked."""

    def test_human_review_always_blocked(self, db_session) -> None:
        pe = _create_test_payment_event(db_session, error_reason="some_unknown_reason")
        rc = _create_pending_case(
            db_session, pe,
            strategy=RecoveryStrategy.HUMAN_REVIEW.value,
        )
        db_session.commit()

        result = execute_single_case(db_session, str(rc.id))

        assert result is not None
        assert result.status == ExecutionStatus.BLOCKED.value

        db_session.refresh(rc)
        # Status should not change
        assert rc.status == RecoveryStatus.PENDING_EXECUTION.value

    def test_blocked_execution_creates_log(self, db_session) -> None:
        pe = _create_test_payment_event(db_session, error_reason="some_unknown_reason")
        rc = _create_pending_case(
            db_session, pe,
            strategy=RecoveryStrategy.HUMAN_REVIEW.value,
        )
        db_session.commit()

        execute_single_case(db_session, str(rc.id))

        logs = db_session.execute(
            select(ExecutionLog).where(ExecutionLog.recovery_case_id == rc.id)
        ).scalars().all()
        assert len(logs) == 1
        assert logs[0].status == ExecutionStatus.BLOCKED.value


# ---------------------------------------------------------------------------
# 6. STOP_RECOVERY is never financially executed
# ---------------------------------------------------------------------------

class TestStopRecovery:
    """STOP_RECOVERY strategy never executes."""

    def test_stop_recovery_blocked(self, db_session) -> None:
        pe = _create_test_payment_event(db_session, error_reason="debit_instrument_blocked")
        rc = _create_pending_case(
            db_session, pe,
            strategy=RecoveryStrategy.STOP_RECOVERY.value,
        )
        db_session.commit()

        result = execute_single_case(db_session, str(rc.id))

        assert result is not None
        assert result.status == ExecutionStatus.BLOCKED.value

    def test_stop_recovery_creates_log(self, db_session) -> None:
        pe = _create_test_payment_event(db_session, error_reason="debit_instrument_blocked")
        rc = _create_pending_case(
            db_session, pe,
            strategy=RecoveryStrategy.STOP_RECOVERY.value,
        )
        db_session.commit()

        execute_single_case(db_session, str(rc.id))

        logs = db_session.execute(
            select(ExecutionLog).where(ExecutionLog.recovery_case_id == rc.id)
        ).scalars().all()
        assert len(logs) == 1
        assert logs[0].status == ExecutionStatus.BLOCKED.value


# ---------------------------------------------------------------------------
# 7. High-value cases requiring approval are blocked
# ---------------------------------------------------------------------------

class TestHighValueApproval:
    """High-value cases requiring human approval are blocked."""

    def test_requires_approval_not_approved_blocked(self, db_session) -> None:
        pe = _create_test_payment_event(
            db_session,
            error_reason="authentication_failed",
            amount_paise=6_000_000,
        )
        rc = _create_pending_case(
            db_session, pe,
            strategy=RecoveryStrategy.CREATE_PAYMENT_LINK.value,
            requires_human_approval=True,
            approved_by_human=None,
        )
        db_session.commit()

        result = execute_single_case(db_session, str(rc.id))

        assert result is not None
        assert result.status == ExecutionStatus.BLOCKED.value
        assert "human approval" in result.message.lower()

    def test_approved_case_can_proceed(self, db_session) -> None:
        pe = _create_test_payment_event(
            db_session,
            error_reason="authentication_failed",
            amount_paise=6_000_000,
        )
        rc = _create_pending_case(
            db_session, pe,
            strategy=RecoveryStrategy.CREATE_PAYMENT_LINK.value,
            requires_human_approval=True,
            approved_by_human=True,
        )
        db_session.commit()

        sim = SimulationBehavior()
        sim.set_outcome(RecoveryStrategy.CREATE_PAYMENT_LINK.value, ExecutionStatus.SUCCESS.value)

        result = execute_single_case(db_session, str(rc.id), sim_behavior=sim)

        assert result is not None
        assert result.status == ExecutionStatus.SUCCESS.value


# ---------------------------------------------------------------------------
# 8. Non-due cases are not executed
# ---------------------------------------------------------------------------

class TestNonDueCases:
    """Non-due cases (future next_run_at) are not executed."""

    def test_future_next_run_not_executed(self, db_session) -> None:
        pe = _create_test_payment_event(db_session)
        future_time = datetime.now(timezone.utc) + timedelta(hours=1)
        rc = _create_pending_case(
            db_session, pe,
            strategy=RecoveryStrategy.WAIT_AND_RETRY.value,
            next_run_at=future_time,
        )
        db_session.commit()

        result = execute_single_case(db_session, str(rc.id))

        assert result is not None
        assert result.status == ExecutionStatus.BLOCKED.value
        assert "future" in result.message.lower()


# ---------------------------------------------------------------------------
# 9. Non-PENDING_EXECUTION cases are not executed
# ---------------------------------------------------------------------------

class TestNonPendingCases:
    """Non-PENDING_EXECUTION cases are not executed."""

    def test_received_case_not_executed(self, db_session) -> None:
        pe = _create_test_payment_event(db_session)
        rc = RecoveryCase(
            payment_event_id=pe.id,
            status=RecoveryStatus.RECEIVED.value,
            failure_category=FailureCategory.UNKNOWN.value,
            recommended_strategy=RecoveryStrategy.WAIT_AND_RETRY.value,
            retry_count=0,
            requires_human_approval=False,
            next_run_at=datetime.now(timezone.utc) - timedelta(minutes=5),
        )
        db_session.add(rc)
        db_session.commit()

        result = execute_single_case(db_session, str(rc.id))

        assert result is not None
        assert result.status == ExecutionStatus.BLOCKED.value
        assert "RECEIVED" in result.message


# ---------------------------------------------------------------------------
# 10. Duplicate workflow trigger does not duplicate execution
# ---------------------------------------------------------------------------

class TestIdempotency:
    """Duplicate execution attempts are safely handled."""

    def test_duplicate_execution_skipped(self, db_session) -> None:
        pe = _create_test_payment_event(db_session)
        rc = _create_pending_case(
            db_session, pe,
            strategy=RecoveryStrategy.WAIT_AND_RETRY.value,
        )
        db_session.commit()

        sim = SimulationBehavior()

        # First execution
        result1 = execute_single_case(db_session, str(rc.id), sim_behavior=sim)
        assert result1 is not None
        assert result1.status == ExecutionStatus.SUCCESS.value

        # Re-query to get fresh state
        db_session.refresh(rc)

        # Second execution — after first execution, retry_count=1 so same idempotency key
        result2 = execute_single_case(db_session, str(rc.id), sim_behavior=sim)
        assert result2 is not None
        # Should be blocked as duplicate (idempotency key collision)
        assert result2.status == ExecutionStatus.BLOCKED.value

    def test_only_one_execution_log_created(self, db_session) -> None:
        pe = _create_test_payment_event(db_session)
        rc = _create_pending_case(
            db_session, pe,
            strategy=RecoveryStrategy.WAIT_AND_RETRY.value,
        )
        db_session.commit()

        sim = SimulationBehavior()
        execute_single_case(db_session, str(rc.id), sim_behavior=sim)
        execute_single_case(db_session, str(rc.id), sim_behavior=sim)

        logs = db_session.execute(
            select(ExecutionLog).where(ExecutionLog.recovery_case_id == rc.id)
        ).scalars().all()
        assert len(logs) == 1


# ---------------------------------------------------------------------------
# 11. Execution creates appropriate ExecutionLog records
# ---------------------------------------------------------------------------

class TestExecutionLogging:
    """Execution creates proper ExecutionLog records."""

    def test_log_has_all_required_fields(self, db_session) -> None:
        pe = _create_test_payment_event(db_session)
        rc = _create_pending_case(
            db_session, pe,
            strategy=RecoveryStrategy.WAIT_AND_RETRY.value,
        )
        db_session.commit()

        sim = SimulationBehavior()
        execute_single_case(db_session, str(rc.id), sim_behavior=sim)

        log = db_session.execute(
            select(ExecutionLog).where(ExecutionLog.recovery_case_id == rc.id)
        ).scalar_one()

        assert log.recovery_case_id == rc.id
        assert log.execution_mode == ExecutionMode.SIMULATION.value
        assert log.action == RecoveryStrategy.WAIT_AND_RETRY.value
        assert log.status == ExecutionStatus.SUCCESS.value
        assert log.request_data is not None
        assert log.response_data is not None
        assert log.executed_at is not None
        assert log.idempotency_key is not None


# ---------------------------------------------------------------------------
# 12. Simulation mode creates no real external API calls
# ---------------------------------------------------------------------------

class TestSimulationSafety:
    """Simulation mode creates no real external API calls."""

    def test_simulation_result_has_simulated_flag(self, db_session) -> None:
        pe = _create_test_payment_event(db_session)
        rc = _create_pending_case(
            db_session, pe,
            strategy=RecoveryStrategy.WAIT_AND_RETRY.value,
        )
        db_session.commit()

        sim = SimulationBehavior()
        result = execute_single_case(db_session, str(rc.id), sim_behavior=sim)

        assert result is not None
        assert result.execution_mode == ExecutionMode.SIMULATION.value
        assert result.response_data is not None
        assert result.response_data.get("simulated") is True


# ---------------------------------------------------------------------------
# 13. Missing real-mode credentials fail closed
# ---------------------------------------------------------------------------

class TestRealModeFailClosed:
    """Missing real-mode credentials fall back to SIMULATION."""

    def test_razorpay_mode_without_credentials_falls_back(self, db_session) -> None:
        original_mode = settings.EXECUTION_MODE
        original_key_id = settings.RAZORPAY_KEY_ID
        original_key_secret = settings.RAZORPAY_KEY_SECRET

        settings.EXECUTION_MODE = "RAZORPAY"
        settings.RAZORPAY_KEY_ID = ""
        settings.RAZORPAY_KEY_SECRET = ""

        try:
            mode = _resolve_execution_mode()
            assert mode == ExecutionMode.SIMULATION
        finally:
            settings.EXECUTION_MODE = original_mode
            settings.RAZORPAY_KEY_ID = original_key_id
            settings.RAZORPAY_KEY_SECRET = original_key_secret

    def test_invalid_mode_falls_back_to_simulation(self, db_session) -> None:
        original_mode = settings.EXECUTION_MODE

        settings.EXECUTION_MODE = "INVALID_MODE"

        try:
            mode = _resolve_execution_mode()
            assert mode == ExecutionMode.SIMULATION
        finally:
            settings.EXECUTION_MODE = original_mode


# ---------------------------------------------------------------------------
# 14. execute_due_cases batch execution
# ---------------------------------------------------------------------------

class TestExecuteDueCases:
    """Test batch execution of due cases."""

    def test_executes_multiple_due_cases(self, db_session) -> None:
        pe1 = _create_test_payment_event(db_session)
        rc1 = _create_pending_case(
            db_session, pe1, strategy=RecoveryStrategy.WAIT_AND_RETRY.value
        )
        pe2 = _create_test_payment_event(db_session)
        rc2 = _create_pending_case(
            db_session, pe2, strategy=RecoveryStrategy.WAIT_AND_RETRY.value
        )
        db_session.commit()

        sim = SimulationBehavior()
        summary = execute_due_cases(db_session, sim_behavior=sim)

        assert summary.attempted == 2
        assert summary.succeeded == 2

    def test_skips_future_cases(self, db_session) -> None:
        pe = _create_test_payment_event(db_session)
        future_time = datetime.now(timezone.utc) + timedelta(hours=1)
        rc = _create_pending_case(
            db_session, pe,
            strategy=RecoveryStrategy.WAIT_AND_RETRY.value,
            next_run_at=future_time,
        )
        db_session.commit()

        summary = execute_due_cases(db_session)
        assert summary.attempted == 0


# ---------------------------------------------------------------------------
# 15. Development endpoint reports execution summary
# ---------------------------------------------------------------------------

class TestWorkflowEndpointExecution:
    """Test that the workflow endpoint reports execution summary."""

    @pytest.mark.asyncio
    async def test_endpoint_reports_execution_fields(self, db_session) -> None:
        original_env = settings.APP_ENV
        settings.APP_ENV = "development"

        try:
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://testserver") as client:
                response = await client.post("/api/dev/process-recovery-workflow")

            assert response.status_code == 200
            data = response.json()
            assert data["success"] is True
            assert "execution_attempted" in data
            assert "execution_succeeded" in data
            assert "execution_failed" in data
            assert "execution_blocked" in data
        finally:
            settings.APP_ENV = original_env

    @pytest.mark.asyncio
    async def test_endpoint_executes_due_cases(self, db_session) -> None:
        original_env = settings.APP_ENV
        settings.APP_ENV = "development"

        try:
            # Create a due case
            pe = _create_test_payment_event(db_session)
            rc = _create_pending_case(
                db_session, pe,
                strategy=RecoveryStrategy.WAIT_AND_RETRY.value,
            )
            db_session.commit()
            rc_id = str(rc.id)

            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://testserver") as client:
                response = await client.post("/api/dev/process-recovery-workflow")

            assert response.status_code == 200
            data = response.json()
            assert data["execution_attempted"] >= 1
            assert data["execution_succeeded"] >= 1

            # Verify case was executed
            from sqlalchemy import select
            processed_rc = db_session.execute(
                select(RecoveryCase).where(RecoveryCase.id == rc.id)
            ).scalar_one()
            assert processed_rc.retry_count == 1
        finally:
            settings.APP_ENV = original_env

    @pytest.mark.asyncio
    async def test_endpoint_returns_404_in_production(self, db_session) -> None:
        original_env = settings.APP_ENV
        settings.APP_ENV = "production"

        try:
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://testserver") as client:
                response = await client.post("/api/dev/process-recovery-workflow")

            assert response.status_code == 404
        finally:
            settings.APP_ENV = original_env


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

class TestEdgeCases:
    """Edge cases for the execution engine."""

    def test_invalid_uuid_returns_none(self, db_session) -> None:
        result = execute_single_case(db_session, "not-a-uuid")
        assert result is None

    def test_nonexistent_case_returns_none(self, db_session) -> None:
        fake_id = str(uuid.uuid4())
        result = execute_single_case(db_session, fake_id)
        assert result is None

    def test_no_strategy_blocked(self, db_session) -> None:
        pe = _create_test_payment_event(db_session)
        rc = _create_pending_case(
            db_session, pe,
            strategy=RecoveryStrategy.WAIT_AND_RETRY.value,
        )
        rc.recommended_strategy = None
        db_session.commit()

        result = execute_single_case(db_session, str(rc.id))

        assert result is not None
        assert result.status == ExecutionStatus.BLOCKED.value
        assert "No recommended_strategy" in result.message

    def test_next_run_at_none_blocked(self, db_session) -> None:
        pe = _create_test_payment_event(db_session)
        rc = _create_pending_case(
            db_session, pe,
            strategy=RecoveryStrategy.WAIT_AND_RETRY.value,
        )
        rc.next_run_at = None
        db_session.commit()

        result = execute_single_case(db_session, str(rc.id))

        assert result is not None
        assert result.status == ExecutionStatus.BLOCKED.value
