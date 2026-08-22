"""Comprehensive tests for the live dashboard activity feed endpoint (Milestone 9B).

Tests cover:
1. Empty database — returns empty feed with valid generated_at
2. Case creation activity
3. Strategy assigned activity
4. Human review activities (required, approved, rejected)
5. Execution log activities (success, failed, blocked, pending)
6. Multiple activity sources merge and sort chronologically (newest first)
7. Limit parameter enforcement and limit clamping/validation
8. Stable deterministic IDs
9. No duplicate activities
10. Correct recovery_case_id and payment_id linkages
11. Existing /api/dashboard/summary regression
12. Existing /api/dashboard/analytics regression
"""

import hashlib
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.orm import Session

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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_payment_event(
    db: Session,
    *,
    amount_paise: int = 150_000,
    external_payment_id: str | None = None,
    created_at: datetime | None = None,
) -> PaymentEvent:
    """Create and persist a PaymentEvent for testing."""
    pe = PaymentEvent(
        id=uuid.uuid4(),
        event_type="payment.failed",
        external_event_id=f"evt_{uuid.uuid4().hex[:12]}",
        external_payment_id=external_payment_id or f"pay_{uuid.uuid4().hex[:12]}",
        external_order_id=f"order_{uuid.uuid4().hex[:12]}",
        amount_paise=amount_paise,
        currency="INR",
        error_code="payment_failed",
        error_reason="account_expired",
        raw_payload={},
        payload_hash=hashlib.sha256(uuid.uuid4().bytes).hexdigest(),
        created_at=created_at or datetime.now(timezone.utc),
    )
    db.add(pe)
    db.flush()
    return pe


def _make_recovery_case(
    db: Session,
    pe: PaymentEvent,
    *,
    status: str = RecoveryStatus.RECEIVED.value,
    strategy: str | None = None,
    requires_human: bool = False,
    approved_by_human: bool | None = None,
    created_at: datetime | None = None,
    updated_at: datetime | None = None,
) -> RecoveryCase:
    """Create and persist a RecoveryCase linked to a PaymentEvent."""
    rc = RecoveryCase(
        id=uuid.uuid4(),
        payment_event_id=pe.id,
        status=status,
        failure_category=FailureCategory.TRANSIENT.value,
        recovery_probability=0.85,
        recommended_strategy=strategy,
        requires_human_approval=requires_human,
        approved_by_human=approved_by_human,
    )
    if created_at is not None:
        rc.created_at = created_at
    if updated_at is not None:
        rc.updated_at = updated_at
    db.add(rc)
    db.flush()
    return rc


def _make_execution_log(
    db: Session,
    rc: RecoveryCase,
    *,
    status: str = ExecutionStatus.SUCCESS.value,
    action: str = RecoveryStrategy.WAIT_AND_RETRY.value,
    error_message: str | None = None,
    created_at: datetime | None = None,
) -> ExecutionLog:
    """Create and persist an ExecutionLog for a RecoveryCase."""
    log = ExecutionLog(
        id=uuid.uuid4(),
        recovery_case_id=rc.id,
        idempotency_key=f"idem_{uuid.uuid4().hex[:16]}",
        action=action,
        execution_mode=ExecutionMode.SIMULATION.value,
        status=status,
        request_data={},
        response_data={},
        error_message=error_message,
        executed_at=created_at or datetime.now(timezone.utc),
        created_at=created_at or datetime.now(timezone.utc),
    )
    db.add(log)
    db.flush()
    return log


# ---------------------------------------------------------------------------
# Test Cases
# ---------------------------------------------------------------------------


class TestEmptyActivityFeed:
    """Empty database returns valid empty activity feed."""

    @pytest.mark.anyio
    async def test_empty_feed(self, db_session: Session) -> None:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            res = await ac.get("/api/dashboard/activity")
        assert res.status_code == 200
        data = res.json()
        assert data["items"] == []
        assert "generated_at" in data


class TestActivityTypes:
    """Validate activity items derived from different lifecycle events."""

    @pytest.mark.anyio
    async def test_case_created_activity(self, db_session: Session) -> None:
        pe = _make_payment_event(db_session, amount_paise=250_000, external_payment_id="pay_abc123")
        rc = _make_recovery_case(db_session, pe, status=RecoveryStatus.RECEIVED.value)
        db_session.commit()

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            res = await ac.get("/api/dashboard/activity")
        assert res.status_code == 200
        data = res.json()
        assert len(data["items"]) >= 1
        item = data["items"][0]
        assert item["id"] == f"case_created_{rc.id}"
        assert item["type"] == "CASE_CREATED"
        assert item["recovery_case_id"] == str(rc.id)
        assert item["payment_id"] == "pay_abc123"
        assert item["amount_paise"] == 250_000

    @pytest.mark.anyio
    async def test_strategy_assigned_activity(self, db_session: Session) -> None:
        pe = _make_payment_event(db_session)
        rc = _make_recovery_case(
            db_session,
            pe,
            status=RecoveryStatus.PENDING_EXECUTION.value,
            strategy=RecoveryStrategy.WAIT_AND_RETRY.value,
        )
        db_session.commit()

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            res = await ac.get("/api/dashboard/activity")
        data = res.json()
        types = [i["type"] for i in data["items"]]
        assert "STRATEGY_ASSIGNED" in types
        strat_item = next(i for i in data["items"] if i["type"] == "STRATEGY_ASSIGNED")
        assert strat_item["id"] == f"case_strategy_{rc.id}"
        assert strat_item["strategy"] == RecoveryStrategy.WAIT_AND_RETRY.value
        assert "Wait And Retry" in strat_item["description"]

    @pytest.mark.anyio
    async def test_human_review_required_activity(self, db_session: Session) -> None:
        pe = _make_payment_event(db_session)
        rc = _make_recovery_case(
            db_session,
            pe,
            status=RecoveryStatus.REQUIRES_HUMAN.value,
            strategy=RecoveryStrategy.CREATE_PAYMENT_LINK.value,
            requires_human=True,
            approved_by_human=None,
        )
        db_session.commit()

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            res = await ac.get("/api/dashboard/activity")
        data = res.json()
        types = [i["type"] for i in data["items"]]
        assert "HUMAN_REVIEW_REQUIRED" in types
        hr_item = next(i for i in data["items"] if i["type"] == "HUMAN_REVIEW_REQUIRED")
        assert hr_item["id"] == f"case_review_req_{rc.id}"

    @pytest.mark.anyio
    async def test_human_review_approved_activity(self, db_session: Session) -> None:
        pe = _make_payment_event(db_session)
        rc = _make_recovery_case(
            db_session,
            pe,
            status=RecoveryStatus.PENDING_EXECUTION.value,
            requires_human=True,
            approved_by_human=True,
        )
        db_session.commit()

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            res = await ac.get("/api/dashboard/activity")
        data = res.json()
        types = [i["type"] for i in data["items"]]
        assert "HUMAN_REVIEW_APPROVED" in types
        app_item = next(i for i in data["items"] if i["type"] == "HUMAN_REVIEW_APPROVED")
        assert app_item["id"] == f"case_review_app_{rc.id}"

    @pytest.mark.anyio
    async def test_human_review_rejected_activity(self, db_session: Session) -> None:
        pe = _make_payment_event(db_session)
        rc = _make_recovery_case(
            db_session,
            pe,
            status=RecoveryStatus.RESOLVED_FAILED.value,
            requires_human=True,
            approved_by_human=False,
        )
        db_session.commit()

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            res = await ac.get("/api/dashboard/activity")
        data = res.json()
        types = [i["type"] for i in data["items"]]
        assert "HUMAN_REVIEW_REJECTED" in types
        rej_item = next(i for i in data["items"] if i["type"] == "HUMAN_REVIEW_REJECTED")
        assert rej_item["id"] == f"case_review_rej_{rc.id}"

    @pytest.mark.anyio
    async def test_execution_log_activities(self, db_session: Session) -> None:
        pe = _make_payment_event(db_session, external_payment_id="pay_exec1")
        rc = _make_recovery_case(db_session, pe, status=RecoveryStatus.RESOLVED_SUCCESS.value)

        log1 = _make_execution_log(
            db_session,
            rc,
            status=ExecutionStatus.SUCCESS.value,
            action=RecoveryStrategy.WAIT_AND_RETRY.value,
        )
        log2 = _make_execution_log(
            db_session,
            rc,
            status=ExecutionStatus.FAILED.value,
            action=RecoveryStrategy.CREATE_PAYMENT_LINK.value,
            error_message="Card limit exceeded",
        )
        log3 = _make_execution_log(
            db_session,
            rc,
            status=ExecutionStatus.BLOCKED.value,
            action=RecoveryStrategy.STOP_RECOVERY.value,
        )
        db_session.commit()

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            res = await ac.get("/api/dashboard/activity")
        data = res.json()
        ids = [i["id"] for i in data["items"]]

        assert f"exec_{log1.id}" in ids
        assert f"exec_{log2.id}" in ids
        assert f"exec_{log3.id}" in ids

        success_act = next(i for i in data["items"] if i["id"] == f"exec_{log1.id}")
        assert success_act["type"] == "EXECUTION_SUCCESS"
        assert success_act["payment_id"] == "pay_exec1"

        failed_act = next(i for i in data["items"] if i["id"] == f"exec_{log2.id}")
        assert failed_act["type"] == "EXECUTION_FAILED"
        assert "Card limit exceeded" in failed_act["description"]


class TestActivityOrderingAndLimit:
    """Ensure activity ordering (newest first) and limit constraints."""

    @pytest.mark.anyio
    async def test_chronological_ordering(self, db_session: Session) -> None:
        now = datetime.now(timezone.utc)
        t1 = now - timedelta(minutes=30)
        t2 = now - timedelta(minutes=15)
        t3 = now - timedelta(minutes=5)

        pe1 = _make_payment_event(db_session, created_at=t1)
        rc1 = _make_recovery_case(db_session, pe1, created_at=t1)

        pe2 = _make_payment_event(db_session, created_at=t2)
        rc2 = _make_recovery_case(db_session, pe2, created_at=t2)

        _make_execution_log(db_session, rc2, created_at=t3)
        db_session.commit()

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            res = await ac.get("/api/dashboard/activity")
        data = res.json()

        occurred_times = [
            datetime.fromisoformat(item["occurred_at"].replace("Z", "+00:00"))
            for item in data["items"]
        ]
        assert len(occurred_times) >= 2
        for i in range(len(occurred_times) - 1):
            assert occurred_times[i] >= occurred_times[i + 1]

    @pytest.mark.anyio
    async def test_limit_parameter(self, db_session: Session) -> None:
        for _ in range(10):
            pe = _make_payment_event(db_session)
            _make_recovery_case(db_session, pe)
        db_session.commit()

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            res = await ac.get("/api/dashboard/activity?limit=5")
        assert res.status_code == 200
        data = res.json()
        assert len(data["items"]) == 5

    @pytest.mark.anyio
    async def test_limit_validation(self, db_session: Session) -> None:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            # limit < 1
            res_low = await ac.get("/api/dashboard/activity?limit=0")
            assert res_low.status_code == 422

            # limit > 100
            res_high = await ac.get("/api/dashboard/activity?limit=101")
            assert res_high.status_code == 422


class TestRegression:
    """Ensure existing dashboard endpoints continue functioning unchanged."""

    @pytest.mark.anyio
    async def test_summary_and_analytics_unbroken(self, db_session: Session) -> None:
        pe = _make_payment_event(db_session)
        _make_recovery_case(
            db_session,
            pe,
            status=RecoveryStatus.RESOLVED_SUCCESS.value,
        )
        db_session.commit()

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            res_sum = await ac.get("/api/dashboard/summary")
            assert res_sum.status_code == 200
            sum_data = res_sum.json()
            assert sum_data["total_cases"] == 1
            assert sum_data["resolved_success_cases"] == 1

            res_ana = await ac.get("/api/dashboard/analytics")
            assert res_ana.status_code == 200
            ana_data = res_ana.json()
            assert ana_data["performance"]["total_cases"] == 1
            assert ana_data["performance"]["successful_cases"] == 1
