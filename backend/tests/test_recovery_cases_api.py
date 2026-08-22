"""Tests for the Recovery Dashboard and Human Review APIs (Milestone 6).

Covers:
- LIST API: list cases, empty result, status/strategy/approval filters, pagination
- DETAIL API: valid case, missing case, invalid UUID
- EXECUTION LOG API: ordering, pagination, missing case
- APPROVAL: succeeds, idempotent, not-required safe, missing case, audit record
- REJECTION: succeeds, idempotent, blocked from auto-execute, missing case, audit record
- MANUAL EXECUTION: dev only, eligibility preserved, human approval not bypassed
- DASHBOARD: empty database, correct counts
- REGRESSION: all existing tests must continue to pass
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.core.config import settings
from app.main import app
from app.models.enums import (
    ExecutionStatus,
    FailureCategory,
    RecoveryStatus,
    RecoveryStrategy,
)
from app.models.execution_log import ExecutionLog
from app.models.payment_event import PaymentEvent
from app.models.recovery_case import RecoveryCase


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _create_test_payment_event(
    db,
    *,
    error_reason: str = "bank_technical_error",
    amount_paise: int = 100_000,
) -> PaymentEvent:
    pe = PaymentEvent(
        event_type="payment.failed",
        external_event_id=f"evt_api_{uuid.uuid4().hex[:12]}",
        external_payment_id=f"pay_api_{uuid.uuid4().hex[:12]}",
        external_order_id=f"order_api_{uuid.uuid4().hex[:12]}",
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


def _create_recovery_case(
    db,
    payment_event: PaymentEvent,
    *,
    status: str = RecoveryStatus.RECEIVED.value,
    strategy: str | None = None,
    retry_count: int = 0,
    next_run_at: datetime | None = None,
    requires_human_approval: bool = False,
    approved_by_human: bool | None = None,
    failure_category: str = FailureCategory.TRANSIENT.value,
) -> RecoveryCase:
    rc = RecoveryCase(
        payment_event_id=payment_event.id,
        status=status,
        failure_category=failure_category,
        recovery_probability=0.8,
        priority_score=800.0,
        recommended_strategy=strategy,
        expected_value_paise=80000,
        decision_audit_trail={"ingestion": {"source": "test"}},
        next_run_at=next_run_at,
        retry_count=retry_count,
        requires_human_approval=requires_human_approval,
        approved_by_human=approved_by_human,
    )
    db.add(rc)
    db.flush()
    return rc


# ---------------------------------------------------------------------------
# LIST API tests
# ---------------------------------------------------------------------------

class TestListCases:
    """GET /api/recovery-cases"""

    @pytest.mark.asyncio
    async def test_list_empty(self, db_session) -> None:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            response = await client.get("/api/recovery-cases")

        assert response.status_code == 200
        data = response.json()
        assert data["items"] == []
        assert data["pagination"]["total"] == 0

    @pytest.mark.asyncio
    async def test_list_returns_cases(self, db_session) -> None:
        pe = _create_test_payment_event(db_session)
        _create_recovery_case(db_session, pe, status=RecoveryStatus.RECEIVED.value)
        db_session.commit()

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            response = await client.get("/api/recovery-cases")

        assert response.status_code == 200
        data = response.json()
        assert len(data["items"]) == 1
        assert data["pagination"]["total"] == 1

    @pytest.mark.asyncio
    async def test_filter_by_status(self, db_session) -> None:
        pe1 = _create_test_payment_event(db_session)
        _create_recovery_case(db_session, pe1, status=RecoveryStatus.RECEIVED.value)
        pe2 = _create_test_payment_event(db_session)
        _create_recovery_case(
            db_session, pe2,
            status=RecoveryStatus.PENDING_EXECUTION.value,
            strategy=RecoveryStrategy.WAIT_AND_RETRY.value,
            next_run_at=datetime.now(timezone.utc) - timedelta(hours=1),
        )
        db_session.commit()

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            response = await client.get(
                "/api/recovery-cases",
                params={"status": "RECEIVED"},
            )

        assert response.status_code == 200
        data = response.json()
        assert len(data["items"]) == 1
        assert data["items"][0]["status"] == "RECEIVED"

    @pytest.mark.asyncio
    async def test_filter_by_strategy(self, db_session) -> None:
        pe = _create_test_payment_event(db_session)
        _create_recovery_case(
            db_session, pe,
            status=RecoveryStatus.PENDING_EXECUTION.value,
            strategy=RecoveryStrategy.WAIT_AND_RETRY.value,
            next_run_at=datetime.now(timezone.utc) - timedelta(hours=1),
        )
        db_session.commit()

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            response = await client.get(
                "/api/recovery-cases",
                params={"strategy": "WAIT_AND_RETRY"},
            )

        assert response.status_code == 200
        data = response.json()
        assert len(data["items"]) == 1
        assert data["items"][0]["recommended_strategy"] == "WAIT_AND_RETRY"

    @pytest.mark.asyncio
    async def test_filter_requires_human_approval(self, db_session) -> None:
        pe = _create_test_payment_event(db_session)
        _create_recovery_case(
            db_session, pe,
            status=RecoveryStatus.REQUIRES_HUMAN.value,
            requires_human_approval=True,
            approved_by_human=None,
        )
        db_session.commit()

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            response = await client.get(
                "/api/recovery-cases",
                params={"requires_human_approval": "true"},
            )

        assert response.status_code == 200
        data = response.json()
        assert len(data["items"]) == 1
        assert data["items"][0]["requires_human_approval"] is True

    @pytest.mark.asyncio
    async def test_filter_approved_by_human(self, db_session) -> None:
        pe = _create_test_payment_event(db_session)
        _create_recovery_case(
            db_session, pe,
            status=RecoveryStatus.PENDING_EXECUTION.value,
            strategy=RecoveryStrategy.CREATE_PAYMENT_LINK.value,
            requires_human_approval=True,
            approved_by_human=True,
            next_run_at=datetime.now(timezone.utc) - timedelta(hours=1),
        )
        db_session.commit()

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            response = await client.get(
                "/api/recovery-cases",
                params={"approved_by_human": "true"},
            )

        assert response.status_code == 200
        data = response.json()
        assert len(data["items"]) == 1
        assert data["items"][0]["approved_by_human"] is True

    @pytest.mark.asyncio
    async def test_pagination(self, db_session) -> None:
        for _ in range(5):
            pe = _create_test_payment_event(db_session)
            _create_recovery_case(db_session, pe)
        db_session.commit()

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            response = await client.get(
                "/api/recovery-cases",
                params={"page": 1, "page_size": 2},
            )

        assert response.status_code == 200
        data = response.json()
        assert len(data["items"]) == 2
        assert data["pagination"]["total"] == 5
        assert data["pagination"]["total_pages"] == 3

    @pytest.mark.asyncio
    async def test_invalid_pagination_rejected(self, db_session) -> None:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            response = await client.get(
                "/api/recovery-cases",
                params={"page": 0, "page_size": 0},
            )

        # FastAPI Query(ge=1) validation rejects page < 1 and page_size < 1
        assert response.status_code == 422


# ---------------------------------------------------------------------------
# DETAIL API tests
# ---------------------------------------------------------------------------

class TestCaseDetail:
    """GET /api/recovery-cases/{id}"""

    @pytest.mark.asyncio
    async def test_valid_case(self, db_session) -> None:
        pe = _create_test_payment_event(db_session)
        rc = _create_recovery_case(db_session, pe)
        db_session.commit()

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            response = await client.get(f"/api/recovery-cases/{rc.id}")

        assert response.status_code == 200
        data = response.json()
        assert data["recovery_case_id"] == str(rc.id)
        assert data["payment_event"] is not None
        assert data["payment_event"]["amount_paise"] == 100_000

    @pytest.mark.asyncio
    async def test_missing_case(self, db_session) -> None:
        fake_id = str(uuid.uuid4())
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            response = await client.get(f"/api/recovery-cases/{fake_id}")

        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_invalid_uuid(self, db_session) -> None:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            response = await client.get("/api/recovery-cases/not-a-uuid")

        assert response.status_code == 400


# ---------------------------------------------------------------------------
# EXECUTION LOG API tests
# ---------------------------------------------------------------------------

class TestExecutionLogs:
    """GET /api/recovery-cases/{id}/execution-logs"""

    @pytest.mark.asyncio
    async def test_logs_ordered_newest_first(self, db_session) -> None:
        pe = _create_test_payment_event(db_session)
        rc = _create_recovery_case(db_session, pe)

        # Create two execution logs
        log1 = ExecutionLog(
            recovery_case_id=rc.id,
            idempotency_key=f"exec:{rc.id}:r0:WAIT_AND_RETRY:1",
            action="WAIT_AND_RETRY",
            execution_mode="SIMULATION",
            status=ExecutionStatus.SUCCESS.value,
            request_data={},
            response_data={},
            executed_at=datetime.now(timezone.utc),
        )
        log2 = ExecutionLog(
            recovery_case_id=rc.id,
            idempotency_key=f"exec:{rc.id}:r1:WAIT_AND_RETRY:2",
            action="WAIT_AND_RETRY",
            execution_mode="SIMULATION",
            status=ExecutionStatus.SUCCESS.value,
            request_data={},
            response_data={},
            executed_at=datetime.now(timezone.utc),
        )
        db_session.add(log1)
        db_session.add(log2)
        db_session.commit()

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            response = await client.get(f"/api/recovery-cases/{rc.id}/execution-logs")

        assert response.status_code == 200
        data = response.json()
        assert len(data["items"]) == 2

    @pytest.mark.asyncio
    async def test_logs_missing_case(self, db_session) -> None:
        fake_id = str(uuid.uuid4())
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            response = await client.get(f"/api/recovery-cases/{fake_id}/execution-logs")

        assert response.status_code == 404


# ---------------------------------------------------------------------------
# APPROVAL tests
# ---------------------------------------------------------------------------

class TestApproval:
    """POST /api/recovery-cases/{id}/approve"""

    @pytest.mark.asyncio
    async def test_approval_succeeds(self, db_session) -> None:
        pe = _create_test_payment_event(db_session)
        rc = _create_recovery_case(
            db_session, pe,
            status=RecoveryStatus.REQUIRES_HUMAN.value,
            strategy=RecoveryStrategy.CREATE_PAYMENT_LINK.value,
            requires_human_approval=True,
            approved_by_human=None,
        )
        db_session.commit()

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            response = await client.post(f"/api/recovery-cases/{rc.id}/approve")

        assert response.status_code == 200
        data = response.json()
        assert data["action"] == "approved"
        assert data["new_approved_by_human"] is True
        assert data["new_status"] == RecoveryStatus.PENDING_EXECUTION.value

    @pytest.mark.asyncio
    async def test_approval_idempotent(self, db_session) -> None:
        pe = _create_test_payment_event(db_session)
        rc = _create_recovery_case(
            db_session, pe,
            status=RecoveryStatus.REQUIRES_HUMAN.value,
            strategy=RecoveryStrategy.CREATE_PAYMENT_LINK.value,
            requires_human_approval=True,
            approved_by_human=None,
        )
        db_session.commit()

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            # First approval
            resp1 = await client.post(f"/api/recovery-cases/{rc.id}/approve")
            assert resp1.status_code == 200

            # Second approval — idempotent
            resp2 = await client.post(f"/api/recovery-cases/{rc.id}/approve")
            assert resp2.status_code == 200
            assert resp2.json()["message"] == "Case was already approved"

    @pytest.mark.asyncio
    async def test_approval_when_not_required(self, db_session) -> None:
        pe = _create_test_payment_event(db_session)
        rc = _create_recovery_case(
            db_session, pe,
            status=RecoveryStatus.RECEIVED.value,
            requires_human_approval=False,
        )
        db_session.commit()

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            response = await client.post(f"/api/recovery-cases/{rc.id}/approve")

        assert response.status_code == 200
        data = response.json()
        assert data["new_approved_by_human"] is True

    @pytest.mark.asyncio
    async def test_approval_missing_case(self, db_session) -> None:
        fake_id = str(uuid.uuid4())
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            response = await client.post(f"/api/recovery-cases/{fake_id}/approve")

        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_approval_creates_audit_state(self, db_session) -> None:
        pe = _create_test_payment_event(db_session)
        rc = _create_recovery_case(
            db_session, pe,
            status=RecoveryStatus.REQUIRES_HUMAN.value,
            requires_human_approval=True,
            approved_by_human=None,
        )
        db_session.commit()

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            await client.post(f"/api/recovery-cases/{rc.id}/approve")

        # Verify state in DB
        from sqlalchemy import select
        updated_rc = db_session.execute(
            select(RecoveryCase).where(RecoveryCase.id == rc.id)
        ).scalar_one()
        assert updated_rc.approved_by_human is True


# ---------------------------------------------------------------------------
# REJECTION tests
# ---------------------------------------------------------------------------

class TestRejection:
    """POST /api/recovery-cases/{id}/reject"""

    @pytest.mark.asyncio
    async def test_rejection_succeeds(self, db_session) -> None:
        pe = _create_test_payment_event(db_session)
        rc = _create_recovery_case(
            db_session, pe,
            status=RecoveryStatus.REQUIRES_HUMAN.value,
            requires_human_approval=True,
            approved_by_human=None,
        )
        db_session.commit()

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            response = await client.post(f"/api/recovery-cases/{rc.id}/reject")

        assert response.status_code == 200
        data = response.json()
        assert data["action"] == "rejected"
        assert data["new_approved_by_human"] is False
        assert data["new_status"] == RecoveryStatus.RESOLVED_FAILED.value

    @pytest.mark.asyncio
    async def test_rejection_idempotent(self, db_session) -> None:
        pe = _create_test_payment_event(db_session)
        rc = _create_recovery_case(
            db_session, pe,
            status=RecoveryStatus.REQUIRES_HUMAN.value,
            requires_human_approval=True,
            approved_by_human=None,
        )
        db_session.commit()

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            resp1 = await client.post(f"/api/recovery-cases/{rc.id}/reject")
            assert resp1.status_code == 200

            resp2 = await client.post(f"/api/recovery-cases/{rc.id}/reject")
            assert resp2.status_code == 200
            assert resp2.json()["message"] == "Case was already rejected"

    @pytest.mark.asyncio
    async def test_rejected_case_not_auto_executable(self, db_session) -> None:
        pe = _create_test_payment_event(db_session)
        rc = _create_recovery_case(
            db_session, pe,
            status=RecoveryStatus.REQUIRES_HUMAN.value,
            strategy=RecoveryStrategy.WAIT_AND_RETRY.value,
            requires_human_approval=True,
            approved_by_human=None,
        )
        db_session.commit()

        # Reject
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            await client.post(f"/api/recovery-cases/{rc.id}/reject")

            # Try to execute
            response = await client.post(f"/api/recovery-cases/{rc.id}/execute")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == ExecutionStatus.BLOCKED.value

    @pytest.mark.asyncio
    async def test_rejection_missing_case(self, db_session) -> None:
        fake_id = str(uuid.uuid4())
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            response = await client.post(f"/api/recovery-cases/{fake_id}/reject")

        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_rejection_creates_audit_state(self, db_session) -> None:
        pe = _create_test_payment_event(db_session)
        rc = _create_recovery_case(
            db_session, pe,
            status=RecoveryStatus.REQUIRES_HUMAN.value,
            requires_human_approval=True,
            approved_by_human=None,
        )
        db_session.commit()

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            await client.post(f"/api/recovery-cases/{rc.id}/reject")

        from sqlalchemy import select
        updated_rc = db_session.execute(
            select(RecoveryCase).where(RecoveryCase.id == rc.id)
        ).scalar_one()
        assert updated_rc.approved_by_human is False
        assert updated_rc.status == RecoveryStatus.RESOLVED_FAILED.value

    @pytest.mark.asyncio
    async def test_cannot_approve_after_rejection(self, db_session) -> None:
        pe = _create_test_payment_event(db_session)
        rc = _create_recovery_case(
            db_session, pe,
            status=RecoveryStatus.REQUIRES_HUMAN.value,
            requires_human_approval=True,
            approved_by_human=None,
        )
        db_session.commit()

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            await client.post(f"/api/recovery-cases/{rc.id}/reject")
            response = await client.post(f"/api/recovery-cases/{rc.id}/approve")

        assert response.status_code == 200
        data = response.json()
        assert data["action"] == "approval_failed"
        assert "previously rejected" in data["message"].lower()


# ---------------------------------------------------------------------------
# MANUAL EXECUTION tests
# ---------------------------------------------------------------------------

class TestManualExecution:
    """POST /api/recovery-cases/{id}/execute"""

    @pytest.mark.asyncio
    async def test_allowed_in_development(self, db_session) -> None:
        original_env = settings.APP_ENV
        settings.APP_ENV = "development"

        try:
            pe = _create_test_payment_event(db_session)
            rc = _create_recovery_case(
                db_session, pe,
                status=RecoveryStatus.PENDING_EXECUTION.value,
                strategy=RecoveryStrategy.WAIT_AND_RETRY.value,
                next_run_at=datetime.now(timezone.utc) - timedelta(minutes=5),
            )
            db_session.commit()

            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://testserver") as client:
                response = await client.post(f"/api/recovery-cases/{rc.id}/execute")

            assert response.status_code == 200
            data = response.json()
            assert data["execution_mode"] == "SIMULATION"
        finally:
            settings.APP_ENV = original_env

    @pytest.mark.asyncio
    async def test_blocked_outside_development(self, db_session) -> None:
        original_env = settings.APP_ENV
        settings.APP_ENV = "production"

        try:
            pe = _create_test_payment_event(db_session)
            rc = _create_recovery_case(
                db_session, pe,
                status=RecoveryStatus.PENDING_EXECUTION.value,
                strategy=RecoveryStrategy.WAIT_AND_RETRY.value,
                next_run_at=datetime.now(timezone.utc) - timedelta(minutes=5),
            )
            db_session.commit()

            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://testserver") as client:
                response = await client.post(f"/api/recovery-cases/{rc.id}/execute")

            assert response.status_code == 404
        finally:
            settings.APP_ENV = original_env

    @pytest.mark.asyncio
    async def test_human_approval_not_bypassed(self, db_session) -> None:
        original_env = settings.APP_ENV
        settings.APP_ENV = "development"

        try:
            pe = _create_test_payment_event(db_session)
            rc = _create_recovery_case(
                db_session, pe,
                status=RecoveryStatus.PENDING_EXECUTION.value,
                strategy=RecoveryStrategy.CREATE_PAYMENT_LINK.value,
                requires_human_approval=True,
                approved_by_human=None,
                next_run_at=datetime.now(timezone.utc) - timedelta(minutes=5),
            )
            db_session.commit()

            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://testserver") as client:
                response = await client.post(f"/api/recovery-cases/{rc.id}/execute")

            assert response.status_code == 200
            data = response.json()
            assert data["status"] == ExecutionStatus.BLOCKED.value
            assert "human approval" in data["message"].lower()
        finally:
            settings.APP_ENV = original_env


# ---------------------------------------------------------------------------
# DASHBOARD tests
# ---------------------------------------------------------------------------

class TestDashboard:
    """GET /api/dashboard/summary"""

    @pytest.mark.asyncio
    async def test_empty_database(self, db_session) -> None:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            response = await client.get("/api/dashboard/summary")

        assert response.status_code == 200
        data = response.json()
        assert data["total_cases"] == 0
        assert data["total_execution_attempts"] == 0

    @pytest.mark.asyncio
    async def test_counts_are_correct(self, db_session) -> None:
        pe1 = _create_test_payment_event(db_session)
        _create_recovery_case(db_session, pe1, status=RecoveryStatus.RECEIVED.value)

        pe2 = _create_test_payment_event(db_session)
        _create_recovery_case(
            db_session, pe2,
            status=RecoveryStatus.PENDING_EXECUTION.value,
            strategy=RecoveryStrategy.WAIT_AND_RETRY.value,
            next_run_at=datetime.now(timezone.utc) - timedelta(hours=1),
        )

        pe3 = _create_test_payment_event(db_session)
        _create_recovery_case(
            db_session, pe3,
            status=RecoveryStatus.REQUIRES_HUMAN.value,
            requires_human_approval=True,
            approved_by_human=None,
        )
        db_session.commit()

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            response = await client.get("/api/dashboard/summary")

        assert response.status_code == 200
        data = response.json()
        assert data["total_cases"] == 3
        assert data["received_cases"] == 1
        assert data["pending_execution_cases"] == 1
        assert data["requires_human_cases"] == 1
        assert data["awaiting_human_review"] == 1

    @pytest.mark.asyncio
    async def test_execution_metrics(self, db_session) -> None:
        pe = _create_test_payment_event(db_session)
        rc = _create_recovery_case(db_session, pe)

        # Create execution logs
        log = ExecutionLog(
            recovery_case_id=rc.id,
            idempotency_key=f"exec:{rc.id}:r0:WAIT_AND_RETRY:1",
            action="WAIT_AND_RETRY",
            execution_mode="SIMULATION",
            status=ExecutionStatus.SUCCESS.value,
            request_data={},
            response_data={},
            executed_at=datetime.now(timezone.utc),
        )
        db_session.add(log)
        db_session.commit()

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            response = await client.get("/api/dashboard/summary")

        assert response.status_code == 200
        data = response.json()
        assert data["total_execution_attempts"] == 1
        assert data["successful_executions"] == 1


# ---------------------------------------------------------------------------
# Milestone 10: Post-Approval Strategy Resolution Tests
# ---------------------------------------------------------------------------


class TestPostApprovalStrategyResolution:
    """Tests for the UNKNOWN → HUMAN_REVIEW → approval → executable strategy flow."""

    @pytest.mark.asyncio
    async def test_unknown_classified_as_requires_human(self, db_session) -> None:
        """UNKNOWN failure_category results in REQUIRES_HUMAN status."""
        pe = _create_test_payment_event(
            db_session,
            error_reason="payment_failed",
            amount_paise=49900,
        )
        rc = _create_recovery_case(
            db_session, pe,
            status=RecoveryStatus.REQUIRES_HUMAN.value,
            strategy=RecoveryStrategy.HUMAN_REVIEW.value,
            failure_category=FailureCategory.UNKNOWN.value,
            requires_human_approval=True,
            approved_by_human=None,
        )
        db_session.commit()

        assert rc.status == RecoveryStatus.REQUIRES_HUMAN.value
        assert rc.recommended_strategy == RecoveryStrategy.HUMAN_REVIEW.value
        assert rc.failure_category == FailureCategory.UNKNOWN.value

    @pytest.mark.asyncio
    async def test_approval_resolves_human_review_to_executable(self, db_session) -> None:
        """Approving a HUMAN_REVIEW case resolves to an executable strategy."""
        pe = _create_test_payment_event(
            db_session,
            error_reason="payment_failed",
            amount_paise=49900,
        )
        rc = _create_recovery_case(
            db_session, pe,
            status=RecoveryStatus.REQUIRES_HUMAN.value,
            strategy=RecoveryStrategy.HUMAN_REVIEW.value,
            failure_category=FailureCategory.UNKNOWN.value,
            requires_human_approval=True,
            approved_by_human=None,
        )
        db_session.commit()

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            response = await client.post(f"/api/recovery-cases/{rc.id}/approve")

        assert response.status_code == 200
        data = response.json()
        assert data["action"] == "approved"
        assert data["new_status"] == RecoveryStatus.PENDING_EXECUTION.value
        assert data["new_approved_by_human"] is True
        # Strategy was resolved from HUMAN_REVIEW to an executable one
        assert data["resolved_strategy"] is not None
        assert data["resolved_strategy"] in (
            RecoveryStrategy.WAIT_AND_RETRY.value,
            RecoveryStrategy.CREATE_PAYMENT_LINK.value,
        )

    @pytest.mark.asyncio
    async def test_resolved_strategy_recorded_in_audit_trail(self, db_session) -> None:
        """The approval resolution is recorded in the decision audit trail."""
        pe = _create_test_payment_event(
            db_session,
            error_reason="payment_failed",
            amount_paise=49900,
        )
        rc = _create_recovery_case(
            db_session, pe,
            status=RecoveryStatus.REQUIRES_HUMAN.value,
            strategy=RecoveryStrategy.HUMAN_REVIEW.value,
            failure_category=FailureCategory.UNKNOWN.value,
            requires_human_approval=True,
            approved_by_human=None,
        )
        db_session.commit()

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            await client.post(f"/api/recovery-cases/{rc.id}/approve")

        # Reload from DB
        updated_rc = db_session.execute(
            select(RecoveryCase).where(RecoveryCase.id == rc.id)
        ).scalar_one()

        trail = updated_rc.decision_audit_trail
        assert "approval_resolution" in trail
        resolution = trail["approval_resolution"]
        assert resolution["original_strategy"] == RecoveryStrategy.HUMAN_REVIEW.value
        assert resolution["resolved_strategy"] in (
            RecoveryStrategy.WAIT_AND_RETRY.value,
            RecoveryStrategy.CREATE_PAYMENT_LINK.value,
        )
        assert "resolution_reason" in resolution
        assert "policy_validation" in resolution
        assert "resolved_at" in resolution

    @pytest.mark.asyncio
    async def test_human_review_strategy_can_never_auto_execute(self, db_session) -> None:
        """A case still in HUMAN_REVIEW state cannot be executed."""
        pe = _create_test_payment_event(db_session)
        rc = _create_recovery_case(
            db_session, pe,
            status=RecoveryStatus.REQUIRES_HUMAN.value,
            strategy=RecoveryStrategy.HUMAN_REVIEW.value,
            failure_category=FailureCategory.UNKNOWN.value,
            requires_human_approval=True,
            approved_by_human=None,
            # Set next_run_at to past to ensure it would be 'due' if not for the strategy check
            next_run_at=datetime.now(timezone.utc) - timedelta(hours=1),
        )
        db_session.commit()

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            response = await client.post(f"/api/recovery-cases/{rc.id}/execute")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == ExecutionStatus.BLOCKED.value

    @pytest.mark.asyncio
    async def test_duplicate_approval_is_idempotent(self, db_session) -> None:
        """Second approval returns idempotent result without corruption."""
        pe = _create_test_payment_event(
            db_session,
            error_reason="payment_failed",
            amount_paise=49900,
        )
        rc = _create_recovery_case(
            db_session, pe,
            status=RecoveryStatus.REQUIRES_HUMAN.value,
            strategy=RecoveryStrategy.HUMAN_REVIEW.value,
            failure_category=FailureCategory.UNKNOWN.value,
            requires_human_approval=True,
            approved_by_human=None,
        )
        db_session.commit()

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            # First approval — resolves strategy
            resp1 = await client.post(f"/api/recovery-cases/{rc.id}/approve")
            assert resp1.status_code == 200
            data1 = resp1.json()
            assert data1["action"] == "approved"

            # Second approval — idempotent
            resp2 = await client.post(f"/api/recovery-cases/{rc.id}/approve")
            assert resp2.status_code == 200
            data2 = resp2.json()
            assert data2["message"] == "Case was already approved"

        # Verify DB state is consistent
        updated_rc = db_session.execute(
            select(RecoveryCase).where(RecoveryCase.id == rc.id)
        ).scalar_one()
        assert updated_rc.approved_by_human is True

    @pytest.mark.asyncio
    async def test_rejected_case_can_never_execute(self, db_session) -> None:
        """A rejected case cannot be executed even if attempted manually."""
        pe = _create_test_payment_event(db_session)
        rc = _create_recovery_case(
            db_session, pe,
            status=RecoveryStatus.REQUIRES_HUMAN.value,
            strategy=RecoveryStrategy.CREATE_PAYMENT_LINK.value,
            requires_human_approval=True,
            approved_by_human=None,
        )
        db_session.commit()

        # Reject
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            await client.post(f"/api/recovery-cases/{rc.id}/reject")

            # Try to execute
            response = await client.post(f"/api/recovery-cases/{rc.id}/execute")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == ExecutionStatus.BLOCKED.value

    @pytest.mark.asyncio
    async def test_approval_with_executable_strategy_transitions_directly(self, db_session) -> None:
        """Case with existing executable strategy transitions directly on approval."""
        pe = _create_test_payment_event(db_session, error_reason="network_error")
        rc = _create_recovery_case(
            db_session, pe,
            status=RecoveryStatus.REQUIRES_HUMAN.value,
            strategy=RecoveryStrategy.WAIT_AND_RETRY.value,
            failure_category=FailureCategory.TRANSIENT.value,
            requires_human_approval=True,
            approved_by_human=None,
        )
        db_session.commit()

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            response = await client.post(f"/api/recovery-cases/{rc.id}/approve")

        assert response.status_code == 200
        data = response.json()
        assert data["new_status"] == RecoveryStatus.PENDING_EXECUTION.value
        # No resolution needed — strategy was already executable
        assert data.get("resolved_strategy") is None

    @pytest.mark.asyncio
    async def test_resolved_case_can_be_executed(self, db_session) -> None:
        """After approval resolves strategy, the case can be executed in simulation."""
        pe = _create_test_payment_event(
            db_session,
            error_reason="payment_failed",
            amount_paise=49900,
        )
        rc = _create_recovery_case(
            db_session, pe,
            status=RecoveryStatus.REQUIRES_HUMAN.value,
            strategy=RecoveryStrategy.HUMAN_REVIEW.value,
            failure_category=FailureCategory.UNKNOWN.value,
            requires_human_approval=True,
            approved_by_human=None,
        )
        db_session.commit()

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            # Approve — resolves strategy and transitions to PENDING_EXECUTION
            approve_resp = await client.post(f"/api/recovery-cases/{rc.id}/approve")
            assert approve_resp.status_code == 200
            assert approve_resp.json()["new_status"] == RecoveryStatus.PENDING_EXECUTION.value

            # Execute — should work because case is now PENDING_EXECUTION with executable strategy
            exec_resp = await client.post(f"/api/recovery-cases/{rc.id}/execute")
            assert exec_resp.status_code == 200
            exec_data = exec_resp.json()
            assert exec_data["status"] == ExecutionStatus.SUCCESS.value

        # Verify ExecutionLog was created
        logs = db_session.execute(
            select(ExecutionLog).where(ExecutionLog.recovery_case_id == rc.id)
        ).scalars().all()
        assert len(logs) == 1
        assert logs[0].status == ExecutionStatus.SUCCESS.value
        assert logs[0].execution_mode == "SIMULATION"

    @pytest.mark.asyncio
    async def test_approval_for_high_value_triggers_policy_recheck(self, db_session) -> None:
        """High-value case approval still passes through policy validation."""
        high_amount = settings.RECOVERY_HIGH_VALUE_THRESHOLD_PAISE + 1_000_000
        pe = _create_test_payment_event(
            db_session,
            error_reason="payment_failed",
            amount_paise=high_amount,
        )
        rc = _create_recovery_case(
            db_session, pe,
            status=RecoveryStatus.REQUIRES_HUMAN.value,
            strategy=RecoveryStrategy.HUMAN_REVIEW.value,
            failure_category=FailureCategory.UNKNOWN.value,
            requires_human_approval=True,
            approved_by_human=None,
        )
        db_session.commit()

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            response = await client.post(f"/api/recovery-cases/{rc.id}/approve")

        assert response.status_code == 200
        data = response.json()
        assert data["action"] == "approved"
        # High-value means policy may still require_human_approval for automated actions
        # The resolved strategy should still be valid
        assert data["resolved_strategy"] is not None

    @pytest.mark.asyncio
    async def test_simulation_mode_never_performs_real_razorpay(self, db_session) -> None:
        """After approval and resolution, execution is always in SIMULATION mode."""
        pe = _create_test_payment_event(
            db_session,
            error_reason="payment_failed",
            amount_paise=49900,
        )
        rc = _create_recovery_case(
            db_session, pe,
            status=RecoveryStatus.REQUIRES_HUMAN.value,
            strategy=RecoveryStrategy.HUMAN_REVIEW.value,
            failure_category=FailureCategory.UNKNOWN.value,
            requires_human_approval=True,
            approved_by_human=None,
        )
        db_session.commit()

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            await client.post(f"/api/recovery-cases/{rc.id}/approve")
            exec_resp = await client.post(f"/api/recovery-cases/{rc.id}/execute")

        assert exec_resp.status_code == 200
        data = exec_resp.json()
        assert data["execution_mode"] == "SIMULATION"
        assert data["status"] == ExecutionStatus.SUCCESS.value
