"""Comprehensive tests for the dashboard analytics endpoint (Milestone 9A).

Tests cover:
1. Empty database — safe zeros
2. Mixed recovery statuses
3. Strategy distribution
4. Success rate and zero-safe behavior
5. Financial metrics via PaymentEvent JOIN
6. Human review metrics
7. Daily activity
8. Existing /api/dashboard/summary regression
9. /api/dashboard/analytics integration
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
    amount_paise: int = 100_000,
    error_code: str = "payment_failed",
) -> PaymentEvent:
    """Create and persist a PaymentEvent for testing."""
    pe = PaymentEvent(
        id=uuid.uuid4(),
        event_type="payment.failed",
        external_event_id=f"evt_{uuid.uuid4().hex[:12]}",
        external_payment_id=f"pay_{uuid.uuid4().hex[:12]}",
        external_order_id=f"order_{uuid.uuid4().hex[:12]}",
        amount_paise=amount_paise,
        currency="INR",
        error_code=error_code,
        error_reason="account_expired",
        raw_payload={},
        payload_hash=hashlib.sha256(uuid.uuid4().bytes).hexdigest(),
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
) -> RecoveryCase:
    """Create and persist a RecoveryCase linked to a PaymentEvent."""
    rc = RecoveryCase(
        id=uuid.uuid4(),
        payment_event_id=pe.id,
        status=status,
        failure_category=FailureCategory.TRANSIENT.value,
        recommended_strategy=strategy,
        requires_human_approval=requires_human,
        approved_by_human=approved_by_human,
    )
    if created_at is not None:
        rc.created_at = created_at
    db.add(rc)
    db.flush()
    return rc


def _make_execution_log(
    db: Session,
    rc: RecoveryCase,
    *,
    status: str = ExecutionStatus.SUCCESS.value,
    action: str = RecoveryStrategy.WAIT_AND_RETRY.value,
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
        executed_at=datetime.now(timezone.utc),
    )
    db.add(log)
    db.flush()
    return log


# ---------------------------------------------------------------------------
# 1. Empty database
# ---------------------------------------------------------------------------


class TestEmptyDatabase:
    """Analytics must return safe defaults with no data."""

    @pytest.mark.anyio
    async def test_analytics_empty(self, db_session: Session) -> None:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            res = await ac.get("/api/dashboard/analytics")
        assert res.status_code == 200
        data = res.json()

        assert data["status_distribution"] == []
        assert data["strategy_distribution"] == []
        assert data["performance"]["total_cases"] == 0
        assert data["performance"]["successful_cases"] == 0
        assert data["performance"]["failed_cases"] == 0
        assert data["performance"]["pending_cases"] == 0
        assert data["performance"]["human_review_cases"] == 0
        assert data["performance"]["success_rate"] == 0.0
        assert data["financial"]["total_failed_amount_paise"] == 0
        assert data["financial"]["simulated_recovered_amount_paise"] == 0
        assert data["financial"]["pending_recovery_amount_paise"] == 0
        assert data["financial"]["human_review_amount_paise"] == 0
        assert data["human_review"]["awaiting_review"] == 0
        assert data["human_review"]["approved"] == 0
        assert data["human_review"]["rejected"] == 0
        assert data["daily_activity"] == []


# ---------------------------------------------------------------------------
# 2. Mixed recovery statuses
# ---------------------------------------------------------------------------


class TestStatusDistribution:
    """Verify status counts match actual database records."""

    @pytest.mark.anyio
    async def test_mixed_statuses(self, db_session: Session) -> None:
        # Create cases with different statuses
        for status in [
            RecoveryStatus.RECEIVED.value,
            RecoveryStatus.RECEIVED.value,
            RecoveryStatus.PENDING_EXECUTION.value,
            RecoveryStatus.REQUIRES_HUMAN.value,
            RecoveryStatus.RESOLVED_SUCCESS.value,
            RecoveryStatus.RESOLVED_FAILED.value,
        ]:
            pe = _make_payment_event(db_session)
            _make_recovery_case(db_session, pe, status=status)
        db_session.commit()

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            res = await ac.get("/api/dashboard/analytics")
        assert res.status_code == 200
        data = res.json()

        dist = {item["status"]: item["count"] for item in data["status_distribution"]}
        assert dist[RecoveryStatus.RECEIVED.value] == 2
        assert dist[RecoveryStatus.PENDING_EXECUTION.value] == 1
        assert dist[RecoveryStatus.REQUIRES_HUMAN.value] == 1
        assert dist[RecoveryStatus.RESOLVED_SUCCESS.value] == 1
        assert dist[RecoveryStatus.RESOLVED_FAILED.value] == 1

        perf = data["performance"]
        assert perf["total_cases"] == 6
        assert perf["successful_cases"] == 1
        assert perf["failed_cases"] == 1
        assert perf["pending_cases"] == 3  # 2 RECEIVED + 1 PENDING_EXECUTION
        assert perf["human_review_cases"] == 1


# ---------------------------------------------------------------------------
# 3. Strategy distribution
# ---------------------------------------------------------------------------


class TestStrategyDistribution:
    """Verify strategy counts match actual database records."""

    @pytest.mark.anyio
    async def test_multiple_strategies(self, db_session: Session) -> None:
        strategies = [
            RecoveryStrategy.WAIT_AND_RETRY.value,
            RecoveryStrategy.WAIT_AND_RETRY.value,
            RecoveryStrategy.WAIT_AND_RETRY.value,
            RecoveryStrategy.CREATE_PAYMENT_LINK.value,
            RecoveryStrategy.HUMAN_REVIEW.value,
            RecoveryStrategy.STOP_RECOVERY.value,
        ]
        for strat in strategies:
            pe = _make_payment_event(db_session)
            _make_recovery_case(db_session, pe, strategy=strat)
        db_session.commit()

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            res = await ac.get("/api/dashboard/analytics")
        assert res.status_code == 200
        data = res.json()

        dist = {
            item["strategy"]: item["count"]
            for item in data["strategy_distribution"]
        }
        assert dist[RecoveryStrategy.WAIT_AND_RETRY.value] == 3
        assert dist[RecoveryStrategy.CREATE_PAYMENT_LINK.value] == 1
        assert dist[RecoveryStrategy.HUMAN_REVIEW.value] == 1
        assert dist[RecoveryStrategy.STOP_RECOVERY.value] == 1

    @pytest.mark.anyio
    async def test_null_strategy_excluded(self, db_session: Session) -> None:
        pe = _make_payment_event(db_session)
        _make_recovery_case(db_session, pe, strategy=None)
        db_session.commit()

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            res = await ac.get("/api/dashboard/analytics")
        data = res.json()
        assert data["strategy_distribution"] == []


# ---------------------------------------------------------------------------
# 4. Success rate
# ---------------------------------------------------------------------------


class TestSuccessRate:
    """Success rate = successful / (successful + failed) * 100."""

    @pytest.mark.anyio
    async def test_success_rate_calculation(self, db_session: Session) -> None:
        # 3 success, 1 failed → 75%
        for _ in range(3):
            pe = _make_payment_event(db_session)
            _make_recovery_case(
                db_session, pe,
                status=RecoveryStatus.RESOLVED_SUCCESS.value,
            )
        pe = _make_payment_event(db_session)
        _make_recovery_case(
            db_session, pe,
            status=RecoveryStatus.RESOLVED_FAILED.value,
        )
        db_session.commit()

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            res = await ac.get("/api/dashboard/analytics")
        data = res.json()
        assert data["performance"]["success_rate"] == 75.0

    @pytest.mark.anyio
    async def test_success_rate_zero_safe(self, db_session: Session) -> None:
        """No resolved cases → success_rate is 0.0, no division error."""
        pe = _make_payment_event(db_session)
        _make_recovery_case(
            db_session, pe,
            status=RecoveryStatus.RECEIVED.value,
        )
        db_session.commit()

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            res = await ac.get("/api/dashboard/analytics")
        data = res.json()
        assert data["performance"]["success_rate"] == 0.0

    @pytest.mark.anyio
    async def test_all_successful(self, db_session: Session) -> None:
        for _ in range(2):
            pe = _make_payment_event(db_session)
            _make_recovery_case(
                db_session, pe,
                status=RecoveryStatus.RESOLVED_SUCCESS.value,
            )
        db_session.commit()

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            res = await ac.get("/api/dashboard/analytics")
        data = res.json()
        assert data["performance"]["success_rate"] == 100.0


# ---------------------------------------------------------------------------
# 5. Financial metrics
# ---------------------------------------------------------------------------


class TestFinancialMetrics:
    """Financial metrics JOIN PaymentEvent amounts by case status."""

    @pytest.mark.anyio
    async def test_financial_aggregation(self, db_session: Session) -> None:
        # Success case: 500₹ (50000 paise)
        pe1 = _make_payment_event(db_session, amount_paise=50_000)
        _make_recovery_case(
            db_session, pe1,
            status=RecoveryStatus.RESOLVED_SUCCESS.value,
        )

        # Pending case: 1000₹ (100000 paise)
        pe2 = _make_payment_event(db_session, amount_paise=100_000)
        _make_recovery_case(
            db_session, pe2,
            status=RecoveryStatus.PENDING_EXECUTION.value,
        )

        # Human review case: 2000₹ (200000 paise)
        pe3 = _make_payment_event(db_session, amount_paise=200_000)
        _make_recovery_case(
            db_session, pe3,
            status=RecoveryStatus.REQUIRES_HUMAN.value,
            requires_human=True,
        )

        # Failed case: 300₹ (30000 paise)
        pe4 = _make_payment_event(db_session, amount_paise=30_000)
        _make_recovery_case(
            db_session, pe4,
            status=RecoveryStatus.RESOLVED_FAILED.value,
        )

        db_session.commit()

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            res = await ac.get("/api/dashboard/analytics")
        data = res.json()
        fin = data["financial"]

        assert fin["total_failed_amount_paise"] == 380_000
        assert fin["simulated_recovered_amount_paise"] == 50_000
        assert fin["pending_recovery_amount_paise"] == 100_000
        assert fin["human_review_amount_paise"] == 200_000


# ---------------------------------------------------------------------------
# 6. Human review metrics
# ---------------------------------------------------------------------------


class TestHumanReviewMetrics:
    """Human review counts based on actual approval state fields."""

    @pytest.mark.anyio
    async def test_human_review_counts(self, db_session: Session) -> None:
        # Awaiting: requires_human=True, approved=None
        pe1 = _make_payment_event(db_session)
        _make_recovery_case(
            db_session, pe1,
            status=RecoveryStatus.REQUIRES_HUMAN.value,
            requires_human=True,
            approved_by_human=None,
        )

        # Approved
        pe2 = _make_payment_event(db_session)
        _make_recovery_case(
            db_session, pe2,
            status=RecoveryStatus.PENDING_EXECUTION.value,
            requires_human=True,
            approved_by_human=True,
        )

        # Rejected
        pe3 = _make_payment_event(db_session)
        _make_recovery_case(
            db_session, pe3,
            status=RecoveryStatus.RESOLVED_FAILED.value,
            requires_human=True,
            approved_by_human=False,
        )

        # NOT human-review case (should not count)
        pe4 = _make_payment_event(db_session)
        _make_recovery_case(
            db_session, pe4,
            status=RecoveryStatus.RECEIVED.value,
            requires_human=False,
        )

        db_session.commit()

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            res = await ac.get("/api/dashboard/analytics")
        data = res.json()
        hr = data["human_review"]

        assert hr["awaiting_review"] == 1
        assert hr["approved"] == 1
        assert hr["rejected"] == 1


# ---------------------------------------------------------------------------
# 7. Daily activity
# ---------------------------------------------------------------------------


class TestDailyActivity:
    """Daily activity aggregates cases created per day within last 30 days."""

    @pytest.mark.anyio
    async def test_daily_grouping(self, db_session: Session) -> None:
        now = datetime.now(timezone.utc)
        today = now.replace(hour=10, minute=0, second=0, microsecond=0)
        yesterday = today - timedelta(days=1)

        # Two today, one yesterday
        for _ in range(2):
            pe = _make_payment_event(db_session)
            _make_recovery_case(db_session, pe, created_at=today)

        pe = _make_payment_event(db_session)
        _make_recovery_case(db_session, pe, created_at=yesterday)

        db_session.commit()

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            res = await ac.get("/api/dashboard/analytics")
        data = res.json()

        activity = data["daily_activity"]
        assert len(activity) == 2

        # Sorted chronologically
        activity_lookup = {item["date"]: item["count"] for item in activity}
        assert activity_lookup[yesterday.strftime("%Y-%m-%d")] == 1
        assert activity_lookup[today.strftime("%Y-%m-%d")] == 2

    @pytest.mark.anyio
    async def test_old_cases_excluded(self, db_session: Session) -> None:
        """Cases older than 30 days are not included."""
        old_date = datetime.now(timezone.utc) - timedelta(days=35)
        pe = _make_payment_event(db_session)
        _make_recovery_case(db_session, pe, created_at=old_date)
        db_session.commit()

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            res = await ac.get("/api/dashboard/analytics")
        data = res.json()
        assert data["daily_activity"] == []


# ---------------------------------------------------------------------------
# 8. Existing endpoint regression
# ---------------------------------------------------------------------------


class TestDashboardSummaryRegression:
    """Ensure GET /api/dashboard/summary still works unchanged."""

    @pytest.mark.anyio
    async def test_summary_endpoint_unchanged(self, db_session: Session) -> None:
        pe = _make_payment_event(db_session)
        _make_recovery_case(
            db_session, pe,
            status=RecoveryStatus.RESOLVED_SUCCESS.value,
        )
        db_session.commit()

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            res = await ac.get("/api/dashboard/summary")
        assert res.status_code == 200
        data = res.json()

        # Verify original schema fields exist
        assert "total_cases" in data
        assert "received_cases" in data
        assert "pending_execution_cases" in data
        assert "requires_human_cases" in data
        assert "resolved_success_cases" in data
        assert "resolved_failed_cases" in data
        assert "awaiting_human_review" in data
        assert "approved_cases" in data
        assert "total_execution_attempts" in data
        assert "successful_executions" in data
        assert "failed_executions" in data
        assert "blocked_executions" in data

        assert data["total_cases"] == 1
        assert data["resolved_success_cases"] == 1

    @pytest.mark.anyio
    async def test_summary_empty_database(self, db_session: Session) -> None:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            res = await ac.get("/api/dashboard/summary")
        assert res.status_code == 200
        data = res.json()
        assert data["total_cases"] == 0


# ---------------------------------------------------------------------------
# 9. Analytics endpoint integration
# ---------------------------------------------------------------------------


class TestAnalyticsEndpointIntegration:
    """Full integration test for GET /api/dashboard/analytics."""

    @pytest.mark.anyio
    async def test_full_response_schema(self, db_session: Session) -> None:
        """Verify the response has all expected top-level keys."""
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            res = await ac.get("/api/dashboard/analytics")
        assert res.status_code == 200
        data = res.json()

        assert "status_distribution" in data
        assert "strategy_distribution" in data
        assert "performance" in data
        assert "financial" in data
        assert "human_review" in data
        assert "daily_activity" in data

    @pytest.mark.anyio
    async def test_comprehensive_scenario(self, db_session: Session) -> None:
        """Create a realistic mix of cases and verify all analytics."""
        now = datetime.now(timezone.utc)

        # Case 1: Successful recovery (WAIT_AND_RETRY, 1000₹)
        pe1 = _make_payment_event(db_session, amount_paise=100_000)
        rc1 = _make_recovery_case(
            db_session, pe1,
            status=RecoveryStatus.RESOLVED_SUCCESS.value,
            strategy=RecoveryStrategy.WAIT_AND_RETRY.value,
            created_at=now,
        )

        # Case 2: Failed recovery (STOP_RECOVERY, 500₹)
        pe2 = _make_payment_event(db_session, amount_paise=50_000)
        _make_recovery_case(
            db_session, pe2,
            status=RecoveryStatus.RESOLVED_FAILED.value,
            strategy=RecoveryStrategy.STOP_RECOVERY.value,
            created_at=now,
        )

        # Case 3: Awaiting human review (CREATE_PAYMENT_LINK, 5000₹)
        pe3 = _make_payment_event(db_session, amount_paise=500_000)
        _make_recovery_case(
            db_session, pe3,
            status=RecoveryStatus.REQUIRES_HUMAN.value,
            strategy=RecoveryStrategy.CREATE_PAYMENT_LINK.value,
            requires_human=True,
            approved_by_human=None,
            created_at=now,
        )

        # Case 4: Pending execution (WAIT_AND_RETRY, 750₹)
        pe4 = _make_payment_event(db_session, amount_paise=75_000)
        _make_recovery_case(
            db_session, pe4,
            status=RecoveryStatus.PENDING_EXECUTION.value,
            strategy=RecoveryStrategy.WAIT_AND_RETRY.value,
            created_at=now,
        )

        db_session.commit()

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            res = await ac.get("/api/dashboard/analytics")
        assert res.status_code == 200
        data = res.json()

        # Performance
        perf = data["performance"]
        assert perf["total_cases"] == 4
        assert perf["successful_cases"] == 1
        assert perf["failed_cases"] == 1
        assert perf["pending_cases"] == 1  # PENDING_EXECUTION
        assert perf["human_review_cases"] == 1
        # 1 / (1+1) * 100 = 50.0
        assert perf["success_rate"] == 50.0

        # Financial
        fin = data["financial"]
        assert fin["total_failed_amount_paise"] == 725_000
        assert fin["simulated_recovered_amount_paise"] == 100_000
        assert fin["pending_recovery_amount_paise"] == 75_000
        assert fin["human_review_amount_paise"] == 500_000

        # Human review
        hr = data["human_review"]
        assert hr["awaiting_review"] == 1
        assert hr["approved"] == 0
        assert hr["rejected"] == 0

        # Daily activity
        assert len(data["daily_activity"]) >= 1
