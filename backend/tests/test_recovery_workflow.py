"""Tests for the Recovery Workflow Processor (Milestone 4).

Covers:
- RECEIVED case gets processed through Decision Engine
- Transient failure becomes PENDING_EXECUTION with future next_run_at
- Authentication failure gets appropriate decision
- Unknown failure becomes REQUIRES_HUMAN
- Hard failure becomes RESOLVED_FAILED
- Non-RECEIVED cases are not incorrectly reprocessed
- Due PENDING_EXECUTION cases can be discovered
- Future PENDING_EXECUTION cases are not returned as due
- Dev endpoint works in development/test
- Dev endpoint returns 404 outside development/test
- Workflow does not execute any Razorpay API action
- Idempotency: processing same case twice is safe
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.config import settings
from app.main import app
from app.models.enums import (
    FailureCategory,
    RecoveryStatus,
    RecoveryStrategy,
)
from app.models.payment_event import PaymentEvent
from app.models.recovery_case import RecoveryCase
from app.services.recovery_workflow import (
    WorkflowResult,
    WorkflowSummary,
    discover_due_cases,
    get_due_recovery_cases,
    get_received_cases,
    process_received_case,
    process_received_cases,
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
        external_event_id=f"evt_wf_{uuid.uuid4().hex[:12]}",
        external_payment_id=f"pay_wf_{uuid.uuid4().hex[:12]}",
        external_order_id=f"order_wf_{uuid.uuid4().hex[:12]}",
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


def _create_test_recovery_case(
    db,
    payment_event: PaymentEvent,
    *,
    status: str = RecoveryStatus.RECEIVED.value,
    retry_count: int = 0,
    next_run_at: datetime | None = None,
) -> RecoveryCase:
    """Create a test RecoveryCase with configurable state."""
    rc = RecoveryCase(
        payment_event_id=payment_event.id,
        status=status,
        failure_category=FailureCategory.UNKNOWN.value,
        recovery_probability=None,
        priority_score=None,
        recommended_strategy=None,
        expected_value_paise=None,
        decision_audit_trail={
            "ingestion": {
                "source": "test",
                "event_id": payment_event.external_event_id,
                "signature_verified": False,
            }
        },
        retry_count=retry_count,
        requires_human_approval=False,
        approved_by_human=None,
        next_run_at=next_run_at,
    )
    db.add(rc)
    db.flush()
    return rc


# ---------------------------------------------------------------------------
# Unit Tests: get_received_cases
# ---------------------------------------------------------------------------

class TestGetReceivedCases:
    """Test discovery of RECEIVED cases."""

    def test_finds_received_cases(self, db_session) -> None:
        pe = _create_test_payment_event(db_session)
        rc = _create_test_recovery_case(db_session, pe)
        db_session.commit()

        cases = get_received_cases(db_session)
        ids = [str(c.id) for c in cases]
        assert str(rc.id) in ids

    def test_ignores_non_received_cases(self, db_session) -> None:
        pe = _create_test_payment_event(db_session)
        rc = _create_test_recovery_case(
            db_session, pe, status=RecoveryStatus.PENDING_EXECUTION.value
        )
        db_session.commit()

        cases = get_received_cases(db_session)
        ids = [str(c.id) for c in cases]
        assert str(rc.id) not in ids

    def test_empty_when_no_cases(self, db_session) -> None:
        cases = get_received_cases(db_session)
        assert cases == []


# ---------------------------------------------------------------------------
# Unit Tests: get_due_recovery_cases
# ---------------------------------------------------------------------------

class TestGetDueRecoveryCases:
    """Test discovery of due PENDING_EXECUTION cases."""

    def test_finds_due_cases(self, db_session) -> None:
        pe = _create_test_payment_event(db_session)
        past_time = datetime.now(timezone.utc) - timedelta(hours=1)
        rc = _create_test_recovery_case(
            db_session,
            pe,
            status=RecoveryStatus.PENDING_EXECUTION.value,
            next_run_at=past_time,
        )
        db_session.commit()

        cases = get_due_recovery_cases(db_session)
        ids = [str(c.id) for c in cases]
        assert str(rc.id) in ids

    def test_ignores_future_cases(self, db_session) -> None:
        pe = _create_test_payment_event(db_session)
        future_time = datetime.now(timezone.utc) + timedelta(hours=1)
        rc = _create_test_recovery_case(
            db_session,
            pe,
            status=RecoveryStatus.PENDING_EXECUTION.value,
            next_run_at=future_time,
        )
        db_session.commit()

        cases = get_due_recovery_cases(db_session)
        ids = [str(c.id) for c in cases]
        assert str(rc.id) not in ids

    def test_ignores_non_pending_cases(self, db_session) -> None:
        pe = _create_test_payment_event(db_session)
        past_time = datetime.now(timezone.utc) - timedelta(hours=1)
        rc = _create_test_recovery_case(
            db_session,
            pe,
            status=RecoveryStatus.RECEIVED.value,
            next_run_at=past_time,
        )
        db_session.commit()

        cases = get_due_recovery_cases(db_session)
        ids = [str(c.id) for c in cases]
        assert str(rc.id) not in ids


# ---------------------------------------------------------------------------
# Unit Tests: process_received_case
# ---------------------------------------------------------------------------

class TestProcessReceivedCase:
    """Test processing a single RECEIVED case."""

    def test_transient_failure_becomes_pending_execution(self, db_session) -> None:
        pe = _create_test_payment_event(db_session, error_reason="bank_technical_error")
        rc = _create_test_recovery_case(db_session, pe)
        db_session.commit()

        result = process_received_case(db_session, str(rc.id))

        assert result.processed is True
        assert result.previous_status == RecoveryStatus.RECEIVED.value
        assert result.new_status == RecoveryStatus.PENDING_EXECUTION.value

        # Verify the case in DB
        db_session.refresh(rc)
        assert rc.status == RecoveryStatus.PENDING_EXECUTION.value
        assert rc.recommended_strategy == RecoveryStrategy.WAIT_AND_RETRY.value
        assert rc.next_run_at is not None
        assert rc.next_run_at > datetime.now(timezone.utc)

    def test_authentication_failure_gets_appropriate_decision(self, db_session) -> None:
        pe = _create_test_payment_event(
            db_session, error_reason="authentication_failed", amount_paise=50_000
        )
        rc = _create_test_recovery_case(db_session, pe)
        db_session.commit()

        result = process_received_case(db_session, str(rc.id))

        assert result.processed is True
        db_session.refresh(rc)
        assert rc.status == RecoveryStatus.PENDING_EXECUTION.value
        assert rc.recommended_strategy == RecoveryStrategy.CREATE_PAYMENT_LINK.value

    def test_unknown_failure_becomes_requires_human(self, db_session) -> None:
        pe = _create_test_payment_event(db_session, error_reason="some_unknown_reason")
        rc = _create_test_recovery_case(db_session, pe)
        db_session.commit()

        result = process_received_case(db_session, str(rc.id))

        assert result.processed is True
        db_session.refresh(rc)
        assert rc.status == RecoveryStatus.REQUIRES_HUMAN.value
        assert rc.requires_human_approval is True

    def test_hard_failure_becomes_resolved_failed(self, db_session) -> None:
        pe = _create_test_payment_event(
            db_session, error_reason="debit_instrument_blocked"
        )
        rc = _create_test_recovery_case(db_session, pe)
        db_session.commit()

        result = process_received_case(db_session, str(rc.id))

        assert result.processed is True
        db_session.refresh(rc)
        assert rc.status == RecoveryStatus.RESOLVED_FAILED.value
        assert rc.next_run_at is None

    def test_non_received_case_not_reprocessed(self, db_session) -> None:
        pe = _create_test_payment_event(db_session)
        rc = _create_test_recovery_case(
            db_session, pe, status=RecoveryStatus.PENDING_EXECUTION.value
        )
        db_session.commit()

        result = process_received_case(db_session, str(rc.id))

        assert result.processed is False
        assert result.previous_status == RecoveryStatus.PENDING_EXECUTION.value
        assert "not RECEIVED" in result.message

    def test_invalid_uuid_returns_error(self, db_session) -> None:
        result = process_received_case(db_session, "not-a-uuid")
        assert result.processed is False
        assert "Invalid" in result.message

    def test_nonexistent_case_returns_error(self, db_session) -> None:
        fake_id = str(uuid.uuid4())
        result = process_received_case(db_session, fake_id)
        assert result.processed is False
        assert "not found" in result.message


# ---------------------------------------------------------------------------
# Unit Tests: process_received_cases (batch)
# ---------------------------------------------------------------------------

class TestProcessReceivedCases:
    """Test batch processing of RECEIVED cases."""

    def test_processes_multiple_received_cases(self, db_session) -> None:
        pe1 = _create_test_payment_event(db_session, error_reason="bank_technical_error")
        rc1 = _create_test_recovery_case(db_session, pe1)
        pe2 = _create_test_payment_event(db_session, error_reason="network_error")
        rc2 = _create_test_recovery_case(db_session, pe2)
        db_session.commit()

        summary = process_received_cases(db_session)

        assert summary.received_processed == 2
        assert summary.received_skipped == 0
        assert len(summary.results) == 2

    def test_skips_non_received_cases(self, db_session) -> None:
        pe1 = _create_test_payment_event(db_session)
        rc1 = _create_test_recovery_case(db_session, pe1)  # RECEIVED
        pe2 = _create_test_payment_event(db_session)
        rc2 = _create_test_recovery_case(
            db_session, pe2, status=RecoveryStatus.PENDING_EXECUTION.value
        )
        db_session.commit()

        summary = process_received_cases(db_session)

        assert summary.received_processed == 1
        assert summary.received_skipped == 0  # Non-received are not even returned by get_received_cases

    def test_empty_when_no_received_cases(self, db_session) -> None:
        summary = process_received_cases(db_session)
        assert summary.received_processed == 0
        assert summary.results == []


# ---------------------------------------------------------------------------
# Unit Tests: discover_due_cases
# ---------------------------------------------------------------------------

class TestDiscoverDueCases:
    """Test discovery of due PENDING_EXECUTION cases."""

    def test_discovers_due_cases(self, db_session) -> None:
        pe = _create_test_payment_event(db_session)
        past_time = datetime.now(timezone.utc) - timedelta(hours=1)
        rc = _create_test_recovery_case(
            db_session,
            pe,
            status=RecoveryStatus.PENDING_EXECUTION.value,
            next_run_at=past_time,
        )
        db_session.commit()

        summary = discover_due_cases(db_session)

        assert summary.due_cases_found == 1
        assert len(summary.results) == 1
        assert summary.results[0].recovery_case_id == str(rc.id)

    def test_no_due_cases_when_all_future(self, db_session) -> None:
        pe = _create_test_payment_event(db_session)
        future_time = datetime.now(timezone.utc) + timedelta(hours=1)
        _create_test_recovery_case(
            db_session,
            pe,
            status=RecoveryStatus.PENDING_EXECUTION.value,
            next_run_at=future_time,
        )
        db_session.commit()

        summary = discover_due_cases(db_session)
        assert summary.due_cases_found == 0

    def test_due_cases_not_executed(self, db_session) -> None:
        """Due cases should be discovered but NOT executed in Milestone 4."""
        pe = _create_test_payment_event(db_session)
        past_time = datetime.now(timezone.utc) - timedelta(hours=1)
        rc = _create_test_recovery_case(
            db_session,
            pe,
            status=RecoveryStatus.PENDING_EXECUTION.value,
            next_run_at=past_time,
        )
        db_session.commit()

        summary = discover_due_cases(db_session)

        # Due cases found but not processed (no execution in M4)
        assert summary.due_cases_found == 1
        assert summary.received_processed == 0

        # Case remains in PENDING_EXECUTION
        db_session.refresh(rc)
        assert rc.status == RecoveryStatus.PENDING_EXECUTION.value


# ---------------------------------------------------------------------------
# Idempotency Tests
# ---------------------------------------------------------------------------

class TestIdempotency:
    """Verify processing the same case twice is safe."""

    def test_processing_same_case_twice_is_safe(self, db_session) -> None:
        pe = _create_test_payment_event(db_session, error_reason="bank_technical_error")
        rc = _create_test_recovery_case(db_session, pe)
        db_session.commit()

        # First processing
        result1 = process_received_case(db_session, str(rc.id))
        assert result1.processed is True

        # Second processing — should be skipped (not RECEIVED anymore)
        result2 = process_received_case(db_session, str(rc.id))
        assert result2.processed is False
        assert "not RECEIVED" in result2.message


# ---------------------------------------------------------------------------
# Safety Tests
# ---------------------------------------------------------------------------

class TestNoRazorpayExecution:
    """Verify the workflow never executes real Razorpay actions."""

    def test_workflow_does_not_create_execution_logs(self, db_session) -> None:
        """Processing through workflow should not create any ExecutionLog entries."""
        from app.models.execution_log import ExecutionLog

        pe = _create_test_payment_event(db_session, error_reason="bank_technical_error")
        rc = _create_test_recovery_case(db_session, pe)
        db_session.commit()

        process_received_case(db_session, str(rc.id))

        # No execution logs should be created
        from sqlalchemy import select

        logs = db_session.execute(
            select(ExecutionLog).where(
                ExecutionLog.recovery_case_id == rc.id
            )
        ).scalars().all()
        assert len(logs) == 0

    def test_workflow_result_has_no_execution_data(self, db_session) -> None:
        """WorkflowResult should not contain execution-related fields."""
        pe = _create_test_payment_event(db_session)
        rc = _create_test_recovery_case(db_session, pe)
        db_session.commit()

        result = process_received_case(db_session, str(rc.id))

        # Result should only contain workflow-level information
        assert hasattr(result, "recovery_case_id")
        assert hasattr(result, "previous_status")
        assert hasattr(result, "new_status")
        assert hasattr(result, "processed")
        assert hasattr(result, "message")


# ---------------------------------------------------------------------------
# Endpoint Tests
# ---------------------------------------------------------------------------

class TestWorkflowEndpoint:
    """Tests for POST /api/dev/process-recovery-workflow."""

    @pytest.mark.asyncio
    async def test_endpoint_works_in_development(self, db_session) -> None:
        """Endpoint processes cases when APP_ENV is development."""
        original_env = settings.APP_ENV
        settings.APP_ENV = "development"

        try:
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://testserver") as client:
                response = await client.post("/api/dev/process-recovery-workflow")

            assert response.status_code == 200
            data = response.json()
            assert data["success"] is True
            assert "received_processed" in data
            assert "due_cases_found" in data
        finally:
            settings.APP_ENV = original_env

    @pytest.mark.asyncio
    async def test_endpoint_works_in_test(self, db_session) -> None:
        """Endpoint processes cases when APP_ENV is test."""
        original_env = settings.APP_ENV
        settings.APP_ENV = "test"

        try:
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://testserver") as client:
                response = await client.post("/api/dev/process-recovery-workflow")

            assert response.status_code == 200
            data = response.json()
            assert data["success"] is True
        finally:
            settings.APP_ENV = original_env

    @pytest.mark.asyncio
    async def test_endpoint_returns_404_in_production(self, db_session) -> None:
        """Endpoint returns 404 when APP_ENV is production."""
        original_env = settings.APP_ENV
        settings.APP_ENV = "production"

        try:
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://testserver") as client:
                response = await client.post("/api/dev/process-recovery-workflow")

            assert response.status_code == 404
        finally:
            settings.APP_ENV = original_env

    @pytest.mark.asyncio
    async def test_endpoint_processes_received_cases(self, db_session) -> None:
        """Endpoint actually processes RECEIVED cases in the database."""
        original_env = settings.APP_ENV
        settings.APP_ENV = "development"

        try:
            # Create a RECEIVED case
            pe = _create_test_payment_event(db_session, error_reason="bank_technical_error")
            rc = _create_test_recovery_case(db_session, pe)
            db_session.commit()
            rc_id = str(rc.id)

            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://testserver") as client:
                response = await client.post("/api/dev/process-recovery-workflow")

            assert response.status_code == 200
            data = response.json()
            assert data["received_processed"] >= 1

            # Verify case was processed — re-query from DB since the endpoint
            # used its own session and committed changes independently
            from sqlalchemy import select

            processed_rc = db_session.execute(
                select(RecoveryCase).where(RecoveryCase.id == rc.id)
            ).scalar_one()
            assert processed_rc.status == RecoveryStatus.PENDING_EXECUTION.value
        finally:
            settings.APP_ENV = original_env
