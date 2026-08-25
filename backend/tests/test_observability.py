"""Tests for Milestone 15B: Observability & Operational Reliability.

Covers:
- correlation ID generation and propagation
- structured JSON logger output
- LOG_LEVEL wiring
- /health/ready readiness endpoint
- /metrics counters
- /api/ops/stuck-cases diagnostics
- scheduler status / heartbeat
- webhook counter increments
- no regression on existing invariants (webhook body, auth, etc.)
"""

import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.core.config import settings
from app.core.logging import (
    JsonFormatter,
    configure_logging,
    current_correlation_id,
)
from app.core.metrics import metrics, grouped_snapshot
from app.main import app
from app.models.enums import RecoveryStatus, FailureCategory
from app.models.recovery_case import RecoveryCase
from app.models.operator import Operator
from app.models.payment_event import PaymentEvent
from app.services.recovery_scheduler import (
    SchedulerStatus,
    get_scheduler_status,
    run_one_cycle,
)
from app.api.deps import get_current_operator
from tests.conftest import make_razorpay_signature, make_valid_payment_failed_body

pytestmark = pytest.mark.usefixtures("db_session")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _client() -> AsyncClient:
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://testserver")


def _disable_auth_override() -> None:
    app.dependency_overrides.pop(get_current_operator, None)


# ---------------------------------------------------------------------------
# Correlation ID
# ---------------------------------------------------------------------------

class TestCorrelationId:
    @pytest.mark.asyncio
    async def test_response_has_x_request_id(self) -> None:
        async with await _client() as c:
            resp = await c.get("/health")
            assert "x-request-id" in resp.headers
            assert len(resp.headers["x-request-id"]) > 0

    @pytest.mark.asyncio
    async def test_incoming_x_request_id_propagated(self) -> None:
        async with await _client() as c:
            resp = await c.get("/health", headers={"X-Request-ID": "my-custom-id-abc"})
            assert resp.headers["x-request-id"] == "my-custom-id-abc"

    @pytest.mark.asyncio
    async def test_malformed_incoming_x_request_id_replaced(self) -> None:
        async with await _client() as c:
            resp = await c.get("/health", headers={"X-Request-ID": "test;invalid id"})
            rid = resp.headers["x-request-id"]
            # ';' and space are outside the sane-ID pattern, so a new random
            # ID must be generated instead of propagated.
            assert rid != "test;invalid id"
            assert len(rid) > 0

    @pytest.mark.asyncio
    async def test_webhook_receives_correlation_id(self) -> None:
        """Webhook path also gets a correlation ID. We verify the response header
        even though the webhook may fail due to missing signature — the important
        thing is that the middleware ran before the handler."""
        async with await _client() as c:
            resp = await c.post("/webhooks/razorpay", content=b"{}")
            assert "x-request-id" in resp.headers


# ---------------------------------------------------------------------------
# Structured logging
# ---------------------------------------------------------------------------

class TestStructuredLogging:
    def test_json_formatter_produces_valid_json(self) -> None:
        """Verify JsonFormatter emits a parseable JSON object with expected keys."""
        fmt = JsonFormatter()
        record = logging.LogRecord(
            name="test.logger",
            level=logging.INFO,
            pathname=__file__,
            lineno=42,
            msg="hello %s",
            args=("world",),
            exc_info=None,
        )
        line = fmt.format(record)
        parsed = json.loads(line)
        assert parsed["ts"]
        assert parsed["level"] == "INFO"
        assert parsed["logger"] == "test.logger"
        assert parsed["message"] == "hello world"

    def test_json_formatter_includes_correlation_id(self) -> None:
        """When a correlation ID is active, the JSON output includes it."""
        from app.core.logging import _correlation_id
        token = _correlation_id.set("test-corr-123")
        try:
            fmt = JsonFormatter()
            record = logging.LogRecord("x", logging.INFO, "", 0, "msg", (), None)
            parsed = json.loads(fmt.format(record))
            assert parsed["correlation_id"] == "test-corr-123"
        finally:
            _correlation_id.reset(token)

    def test_configure_logging_is_idempotent(self) -> None:
        """Calling configure_logging twice does not double-handle."""
        configure_logging()
        root = logging.getLogger()
        count_before = len(root.handlers)
        configure_logging()
        assert len(root.handlers) == count_before


# ---------------------------------------------------------------------------
# /health and /health/ready
# ---------------------------------------------------------------------------

class TestHealth:
    @pytest.mark.asyncio
    async def test_health_unchanged(self) -> None:
        async with await _client() as c:
            resp = await c.get("/health")
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] == "ok"
            assert data["service"] == "recoverai-backend"

    @pytest.mark.asyncio
    async def test_health_ready_returns_200(self) -> None:
        """/health/ready returns 200 when the DB is reachable and the scheduler
        is not an enabled-but-stopped dependency."""
        from app.core.config import settings as cfg
        with patch.object(cfg, "SCHEDULER_ENABLED", False):
            async with await _client() as c:
                resp = await c.get("/health/ready")
            assert resp.status_code == 200
            data = resp.json()
            assert data["ready"] is True
            assert data["db"] == "ok"

    @pytest.mark.asyncio
    async def test_health_ready_public(self) -> None:
        """Readiness does not require authentication (infrastructure-facing)."""
        _disable_auth_override()
        from app.core.config import settings as cfg
        with patch.object(cfg, "SCHEDULER_ENABLED", False):
            async with await _client() as c:
                resp = await c.get("/health/ready")
            assert resp.status_code == 200


# ---------------------------------------------------------------------------
# /metrics
# ---------------------------------------------------------------------------

class TestMetrics:
    @pytest.mark.asyncio
    async def test_metrics_returns_expected_keys(self) -> None:
        async with await _client() as c:
            resp = await c.get("/metrics")
            assert resp.status_code == 200
            data = resp.json()
            assert "webhook" in data
            assert "scheduler" in data
            assert "execution" in data
            assert "received" in data["webhook"]
            assert "verified" in data["webhook"]

    @pytest.mark.asyncio
    async def test_metrics_public(self) -> None:
        """Metrics are public (aggregate counters only, no secrets)."""
        _disable_auth_override()
        async with await _client() as c:
            resp = await c.get("/metrics")
            assert resp.status_code == 200

    def test_grouped_snapshot_returns_expected_shape(self) -> None:
        snapshot = grouped_snapshot()
        assert "webhook" in snapshot
        assert "scheduler" in snapshot
        assert "execution" in snapshot

    def test_metrics_increment_and_add(self) -> None:
        before = metrics.get("test_counter")
        metrics.increment("test_counter")
        assert metrics.get("test_counter") == before + 1.0
        metrics.add("test_sum", 3.5)
        assert metrics.get("test_sum") == 3.5


# ---------------------------------------------------------------------------
# Scheduler status
# ---------------------------------------------------------------------------

class TestSchedulerStatus:
    def test_initial_status(self) -> None:
        status = get_scheduler_status()
        assert isinstance(status, SchedulerStatus)
        assert status.total_cycles == 0

    def test_run_one_cycle_updates_status(self, db_session) -> None:
        # Insert a PENDING_EXECUTION case that is due, so the cycle has work.
        pe = PaymentEvent(
            id=uuid.uuid4(),
            event_type="payment.failed",
            external_event_id=f"evt_{uuid.uuid4().hex}",
            external_payment_id=f"pay_{uuid.uuid4().hex}",
            external_order_id=f"order_{uuid.uuid4().hex}",
            amount_paise=50000,
            currency="INR",
            error_code="GATEWAY_ERROR",
            error_reason="network_error",
            error_description="transient",
            raw_payload={},
            payload_hash="h",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(pe)
        db_session.flush()
        from app.models.enums import RecoveryStrategy
        rc = RecoveryCase(
            id=uuid.uuid4(),
            payment_event_id=pe.id,
            status=RecoveryStatus.PENDING_EXECUTION.value,
            failure_category=FailureCategory.TRANSIENT.value,
            recommended_strategy=RecoveryStrategy.WAIT_AND_RETRY.value,
            next_run_at=datetime.now(timezone.utc) - timedelta(seconds=60),
            retry_count=0,
            requires_human_approval=False,
            approved_by_human=True,
            decision_audit_trail={},
        )
        db_session.add(rc)
        db_session.commit()

        run_one_cycle(db=db_session)
        status = get_scheduler_status()
        assert status.total_cycles >= 1
        assert status.last_cycle_started_at is not None
        assert status.last_cycle_finished_at is not None
        assert status.last_cycle_duration_ms is not None
        assert status.last_attempted > 0


# ---------------------------------------------------------------------------
# Webhook counters
# ---------------------------------------------------------------------------

class TestWebhookCounters:
    @pytest.mark.asyncio
    async def test_webhook_stale_counter_increments(self, db_session) -> None:
        """A stale payment.failed increments the rejected_stale counter."""
        secret = "test_counter_secret"
        event_id = "evt_counter_stale_001"
        body = make_valid_payment_failed_body()
        body["created_at"] = int(datetime.now(timezone.utc).timestamp()) - 301
        body_bytes = json.dumps(body).encode()
        sig = make_razorpay_signature(body_bytes, secret)

        before = metrics.get("webhook_rejected_stale")
        original_secret = settings.RAZORPAY_WEBHOOK_SECRET
        settings.RAZORPAY_WEBHOOK_SECRET = secret
        try:
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://testserver") as c:
                resp = await c.post(
                    "/webhooks/razorpay",
                    content=body_bytes,
                    headers={
                        "X-Razorpay-Signature": sig,
                        "x-razorpay-event-id": event_id,
                        "Content-Type": "application/json",
                    },
                )
            assert resp.status_code == 200
            assert resp.json()["stale"] is True
            assert metrics.get("webhook_rejected_stale") == before + 1.0
        finally:
            settings.RAZORPAY_WEBHOOK_SECRET = original_secret

    @pytest.mark.asyncio
    async def test_webhook_hmac_counter_increments(self, db_session) -> None:
        """An invalid HMAC increments the rejected_hmac counter."""
        secret = "test_counter_hmac"
        event_id = "evt_counter_hmac_001"
        body = make_valid_payment_failed_body()
        body_bytes = json.dumps(body).encode()
        wrong_sig = make_razorpay_signature(body_bytes, "wrong_secret")

        before = metrics.get("webhook_rejected_hmac")
        original_secret = settings.RAZORPAY_WEBHOOK_SECRET
        settings.RAZORPAY_WEBHOOK_SECRET = secret
        try:
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://testserver") as c:
                resp = await c.post(
                    "/webhooks/razorpay",
                    content=body_bytes,
                    headers={
                        "X-Razorpay-Signature": wrong_sig,
                        "x-razorpay-event-id": event_id,
                        "Content-Type": "application/json",
                    },
                )
            assert resp.status_code == 401
            assert metrics.get("webhook_rejected_hmac") == before + 1.0
        finally:
            settings.RAZORPAY_WEBHOOK_SECRET = original_secret

    @pytest.mark.asyncio
    async def test_webhook_duplicate_counter_increments(self, db_session) -> None:
        """A duplicate event-id increments the duplicate counter."""
        secret = "test_counter_dup"
        event_id = "evt_counter_dup_001"
        body = make_valid_payment_failed_body()
        body_bytes = json.dumps(body).encode()
        sig = make_razorpay_signature(body_bytes, secret)

        before = metrics.get("webhook_duplicate")
        original_secret = settings.RAZORPAY_WEBHOOK_SECRET
        settings.RAZORPAY_WEBHOOK_SECRET = secret
        try:
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://testserver") as c:
                # First delivery.
                r1 = await c.post(
                    "/webhooks/razorpay",
                    content=body_bytes,
                    headers={
                        "X-Razorpay-Signature": sig,
                        "x-razorpay-event-id": event_id,
                        "Content-Type": "application/json",
                    },
                )
                assert r1.status_code == 200
                assert r1.json()["duplicate"] is False
                # Second delivery (duplicate).
                r2 = await c.post(
                    "/webhooks/razorpay",
                    content=body_bytes,
                    headers={
                        "X-Razorpay-Signature": sig,
                        "x-razorpay-event-id": event_id,
                        "Content-Type": "application/json",
                    },
                )
                assert r2.status_code == 200
                assert r2.json()["duplicate"] is True
            assert metrics.get("webhook_duplicate") == before + 1.0
        finally:
            settings.RAZORPAY_WEBHOOK_SECRET = original_secret


# ---------------------------------------------------------------------------
# /api/ops/stuck-cases
# ---------------------------------------------------------------------------

class TestStuckCases:
    @pytest.mark.asyncio
    async def test_stuck_cases_requires_auth(self) -> None:
        _disable_auth_override()
        async with await _client() as c:
            resp = await c.get("/api/ops/stuck-cases")
            assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_viewer_gets_403(self, db_session) -> None:
        _disable_auth_override()
        from app.core.roles import OperatorRole
        from app.core.security import hash_password
        op = Operator(
            id=uuid.uuid4(),
            email="viewer@stuck.local",
            password_hash=hash_password("test"),
            role=OperatorRole.VIEWER.value,
            enabled=True,
            must_change_password=False,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        db_session.add(op)
        db_session.commit()
        app.dependency_overrides[get_current_operator] = lambda: op
        async with await _client() as c:
            resp = await c.get("/api/ops/stuck-cases")
            assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_stuck_cases_returns_expected_shape(self, db_session) -> None:
        """An old RECEIVED case appears in the results."""
        now = datetime.now(timezone.utc)
        pe = PaymentEvent(
            id=uuid.uuid4(),
            event_type="payment.failed",
            external_event_id=f"evt_{uuid.uuid4().hex}",
            external_payment_id=f"pay_{uuid.uuid4().hex}",
            external_order_id=f"order_{uuid.uuid4().hex}",
            amount_paise=50000,
            currency="INR",
            error_code="GATEWAY_ERROR",
            error_reason="network_error",
            error_description="transient",
            raw_payload={},
            payload_hash="h",
            created_at=now - timedelta(hours=2),
        )
        db_session.add(pe)
        db_session.flush()
        rc = RecoveryCase(
            id=uuid.uuid4(),
            payment_event_id=pe.id,
            status=RecoveryStatus.RECEIVED.value,
            failure_category=FailureCategory.TRANSIENT.value,
            retry_count=0,
            requires_human_approval=False,
            decision_audit_trail={},
            created_at=now - timedelta(hours=2),
            updated_at=now - timedelta(hours=2),
        )
        db_session.add(rc)
        db_session.commit()

        async with await _client() as c:
            resp = await c.get("/api/ops/stuck-cases")
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] >= 1
        assert data["items"][0]["status"] == RecoveryStatus.RECEIVED.value
        assert data["items"][0]["age_seconds"] >= 3600

    @pytest.mark.asyncio
    async def test_stuck_cases_does_not_mutate(self, db_session) -> None:
        """The endpoint is read-only — the case status must not change."""
        now = datetime.now(timezone.utc)
        pe = PaymentEvent(
            id=uuid.uuid4(),
            event_type="payment.failed",
            external_event_id=f"evt_{uuid.uuid4().hex}",
            external_payment_id=f"pay_{uuid.uuid4().hex}",
            external_order_id=f"order_{uuid.uuid4().hex}",
            amount_paise=50000,
            currency="INR",
            error_code="GATEWAY_ERROR",
            error_reason="network_error",
            error_description="transient",
            raw_payload={},
            payload_hash="h",
            created_at=now - timedelta(hours=2),
        )
        db_session.add(pe)
        db_session.flush()
        rc = RecoveryCase(
            id=uuid.uuid4(),
            payment_event_id=pe.id,
            status=RecoveryStatus.RECEIVED.value,
            failure_category=FailureCategory.TRANSIENT.value,
            retry_count=0,
            requires_human_approval=False,
            decision_audit_trail={},
            created_at=now - timedelta(hours=2),
            updated_at=now - timedelta(hours=2),
        )
        db_session.add(rc)
        db_session.commit()

        async with await _client() as c:
            resp = await c.get("/api/ops/stuck-cases")
        assert resp.status_code == 200

        # Re-read the case from DB — status must be unchanged.
        db_session.expire_all()
        from sqlalchemy import select
        rc2 = db_session.execute(select(RecoveryCase).where(RecoveryCase.id == rc.id)).scalar_one()
        assert rc2.status == RecoveryStatus.RECEIVED.value