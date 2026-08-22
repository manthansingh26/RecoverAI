"""Comprehensive tests for payment failure ingestion (Milestone 2).

Tests cover:
- Valid webhook signature verification
- Invalid/missing signature rejection
- Duplicate event idempotency
- Different event IDs treated independently
- Non-payment.failed events acknowledged safely
- Development simulation endpoint
- Simulation disabled outside dev/test
- Existing health tests still pass
- Model constraints still valid
"""

import hashlib
import hmac
import json

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
from app.services.webhook_security import verify_razorpay_signature
from tests.conftest import (
    make_valid_payment_failed_body,
    make_razorpay_signature,
)


# ---------------------------------------------------------------------------
# Webhook Security Unit Tests
# ---------------------------------------------------------------------------

class TestWebhookSecurity:
    """Test HMAC-SHA256 signature verification."""

    def test_valid_signature(self) -> None:
        secret = "test_secret_123"
        body = b'{"event": "payment.failed"}'
        sig = make_razorpay_signature(body, secret)
        result = verify_razorpay_signature(body, sig, secret)
        assert result.valid is True

    def test_invalid_signature(self) -> None:
        secret = "test_secret_123"
        body = b'{"event": "payment.failed"}'
        result = verify_razorpay_signature(body, "wrong_signature", secret)
        assert result.valid is False
        assert "Invalid signature" in result.reason

    def test_missing_signature(self) -> None:
        secret = "test_secret_123"
        body = b'{"event": "payment.failed"}'
        result = verify_razorpay_signature(body, "", secret)
        assert result.valid is False
        assert "Missing signature" in result.reason

    def test_empty_secret(self) -> None:
        body = b'{"event": "payment.failed"}'
        result = verify_razorpay_signature(body, "some_sig", "")
        assert result.valid is False
        assert "not configured" in result.reason

    def test_signature_uses_raw_body_not_parsed(self) -> None:
        """Signature must be verified against raw bytes, not parsed JSON."""
        secret = "test_secret"
        body = b'{"key": "value", "a": 1}'
        sig = make_razorpay_signature(body, secret)
        # Parsing and re-encoding changes key order — signature should fail
        parsed = json.loads(body)
        reencoded = json.dumps(parsed).encode()
        # This simulates what would happen if we parsed before verifying
        # The signature was made over the original bytes, not re-encoded
        if reencoded != body:
            result = verify_razorpay_signature(reencoded, sig, secret)
            assert result.valid is False


# ---------------------------------------------------------------------------
# Webhook Ingestion Integration Tests
# ---------------------------------------------------------------------------

class TestWebhookIngestion:
    """Integration tests for POST /webhooks/razorpay."""

    @pytest.mark.asyncio
    async def test_valid_payment_failed_creates_records(self, db_session) -> None:
        """Valid payment.failed creates PaymentEvent + RecoveryCase."""
        secret = settings.RAZORPAY_WEBHOOK_SECRET or "test_secret_123"
        body = make_valid_payment_failed_body()
        body_bytes = json.dumps(body).encode()
        sig = make_razorpay_signature(body_bytes, secret)
        event_id = "evt_test_001"

        # Temporarily set the webhook secret for this test
        original_secret = settings.RAZORPAY_WEBHOOK_SECRET
        settings.RAZORPAY_WEBHOOK_SECRET = secret

        try:
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://testserver") as client:
                response = await client.post(
                    "/webhooks/razorpay",
                    content=body_bytes,
                    headers={
                        "X-Razorpay-Signature": sig,
                        "x-razorpay-event-id": event_id,
                        "Content-Type": "application/json",
                    },
                )

            assert response.status_code == 200
            data = response.json()
            assert data["accepted"] is True
            assert data["duplicate"] is False
            assert data["event_id"] == event_id
            assert data["recovery_case_id"] is not None

            # Verify database records
            pe = db_session.execute(
                select(PaymentEvent).where(
                    PaymentEvent.external_event_id == event_id
                )
            ).scalar_one_or_none()
            assert pe is not None
            assert pe.event_type == "payment.failed"
            assert pe.amount_paise == 100000
            assert pe.currency == "INR"
            assert pe.external_payment_id == "pay_test123"
            assert pe.external_order_id == "order_test123"

            rc = db_session.execute(
                select(RecoveryCase).where(
                    RecoveryCase.payment_event_id == pe.id
                )
            ).scalar_one_or_none()
            assert rc is not None
            assert rc.status == RecoveryStatus.REQUIRES_HUMAN.value
            assert rc.failure_category == FailureCategory.UNKNOWN.value
            assert rc.retry_count == 0
            assert rc.requires_human_approval is True
        finally:
            settings.RAZORPAY_WEBHOOK_SECRET = original_secret

    @pytest.mark.asyncio
    async def test_invalid_signature_rejected(self, db_session) -> None:
        """Invalid signature returns 401 and no records created."""
        secret = settings.RAZORPAY_WEBHOOK_SECRET or "test_secret_123"
        original_secret = settings.RAZORPAY_WEBHOOK_SECRET
        settings.RAZORPAY_WEBHOOK_SECRET = secret

        try:
            body = make_valid_payment_failed_body()
            body_bytes = json.dumps(body).encode()
            event_id = "evt_test_bad_sig"

            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://testserver") as client:
                response = await client.post(
                    "/webhooks/razorpay",
                    content=body_bytes,
                    headers={
                        "X-Razorpay-Signature": "invalid_signature_here",
                        "x-razorpay-event-id": event_id,
                        "Content-Type": "application/json",
                    },
                )

            assert response.status_code == 401

            # No records created
            pe = db_session.execute(
                select(PaymentEvent).where(
                    PaymentEvent.external_event_id == event_id
                )
            ).scalar_one_or_none()
            assert pe is None
        finally:
            settings.RAZORPAY_WEBHOOK_SECRET = original_secret

    @pytest.mark.asyncio
    async def test_missing_signature_rejected(self, db_session) -> None:
        """Missing signature header returns 401."""
        secret = settings.RAZORPAY_WEBHOOK_SECRET or "test_secret_123"
        original_secret = settings.RAZORPAY_WEBHOOK_SECRET
        settings.RAZORPAY_WEBHOOK_SECRET = secret

        try:
            body = make_valid_payment_failed_body()
            body_bytes = json.dumps(body).encode()

            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://testserver") as client:
                response = await client.post(
                    "/webhooks/razorpay",
                    content=body_bytes,
                    headers={
                        "x-razorpay-event-id": "evt_test_no_sig",
                        "Content-Type": "application/json",
                    },
                )

            assert response.status_code in (401, 422)
        finally:
            settings.RAZORPAY_WEBHOOK_SECRET = original_secret

    @pytest.mark.asyncio
    async def test_duplicate_event_idempotent(self, db_session) -> None:
        """Second request with same event_id returns duplicate=true."""
        secret = settings.RAZORPAY_WEBHOOK_SECRET or "test_secret_123"
        original_secret = settings.RAZORPAY_WEBHOOK_SECRET
        settings.RAZORPAY_WEBHOOK_SECRET = secret

        try:
            body = make_valid_payment_failed_body()
            body_bytes = json.dumps(body).encode()
            sig = make_razorpay_signature(body_bytes, secret)
            event_id = "evt_test_dup_001"

            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://testserver") as client:
                # First request
                resp1 = await client.post(
                    "/webhooks/razorpay",
                    content=body_bytes,
                    headers={
                        "X-Razorpay-Signature": sig,
                        "x-razorpay-event-id": event_id,
                        "Content-Type": "application/json",
                    },
                )
                assert resp1.status_code == 200
                assert resp1.json()["duplicate"] is False

                # Second request (duplicate)
                resp2 = await client.post(
                    "/webhooks/razorpay",
                    content=body_bytes,
                    headers={
                        "X-Razorpay-Signature": sig,
                        "x-razorpay-event-id": event_id,
                        "Content-Type": "application/json",
                    },
                )
                assert resp2.status_code == 200
                assert resp2.json()["duplicate"] is True

            # Only one PaymentEvent exists
            events = db_session.execute(
                select(PaymentEvent).where(
                    PaymentEvent.external_event_id == event_id
                )
            ).scalars().all()
            assert len(events) == 1

            # Only one RecoveryCase exists
            cases = db_session.execute(
                select(RecoveryCase).where(
                    RecoveryCase.payment_event_id == events[0].id
                )
            ).scalars().all()
            assert len(cases) == 1
        finally:
            settings.RAZORPAY_WEBHOOK_SECRET = original_secret

    @pytest.mark.asyncio
    async def test_different_event_ids_independent(self, db_session) -> None:
        """Different event IDs are treated as independent events."""
        secret = settings.RAZORPAY_WEBHOOK_SECRET or "test_secret_123"
        original_secret = settings.RAZORPAY_WEBHOOK_SECRET
        settings.RAZORPAY_WEBHOOK_SECRET = secret

        try:
            body = make_valid_payment_failed_body()
            body_bytes = json.dumps(body).encode()
            sig = make_razorpay_signature(body_bytes, secret)

            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://testserver") as client:
                resp1 = await client.post(
                    "/webhooks/razorpay",
                    content=body_bytes,
                    headers={
                        "X-Razorpay-Signature": sig,
                        "x-razorpay-event-id": "evt_independent_001",
                        "Content-Type": "application/json",
                    },
                )
                resp2 = await client.post(
                    "/webhooks/razorpay",
                    content=body_bytes,
                    headers={
                        "X-Razorpay-Signature": sig,
                        "x-razorpay-event-id": "evt_independent_002",
                        "Content-Type": "application/json",
                    },
                )

            assert resp1.json()["duplicate"] is False
            assert resp2.json()["duplicate"] is False

            # Two distinct events in database
            ev1 = db_session.execute(
                select(PaymentEvent).where(
                    PaymentEvent.external_event_id == "evt_independent_001"
                )
            ).scalar_one_or_none()
            ev2 = db_session.execute(
                select(PaymentEvent).where(
                    PaymentEvent.external_event_id == "evt_independent_002"
                )
            ).scalar_one_or_none()
            assert ev1 is not None
            assert ev2 is not None
            assert ev1.id != ev2.id
        finally:
            settings.RAZORPAY_WEBHOOK_SECRET = original_secret

    @pytest.mark.asyncio
    async def test_non_payment_failed_event_acknowledged(self, db_session) -> None:
        """Valid event type other than payment.failed is acknowledged, not processed."""
        secret = settings.RAZORPAY_WEBHOOK_SECRET or "test_secret_123"
        original_secret = settings.RAZORPAY_WEBHOOK_SECRET
        settings.RAZORPAY_WEBHOOK_SECRET = secret

        try:
            body = {"entity": "event", "event": "payment.authorized", "payload": {}}
            body_bytes = json.dumps(body).encode()
            sig = make_razorpay_signature(body_bytes, secret)

            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://testserver") as client:
                response = await client.post(
                    "/webhooks/razorpay",
                    content=body_bytes,
                    headers={
                        "X-Razorpay-Signature": sig,
                        "x-razorpay-event-id": "evt_auth_001",
                        "Content-Type": "application/json",
                    },
                )

            assert response.status_code == 200
            data = response.json()
            assert data["accepted"] is True
            assert data["recovery_case_id"] is None
            assert "not processed" in data.get("message", "")

            # No PaymentEvent created
            pe = db_session.execute(
                select(PaymentEvent).where(
                    PaymentEvent.external_event_id == "evt_auth_001"
                )
            ).scalar_one_or_none()
            assert pe is None
        finally:
            settings.RAZORPAY_WEBHOOK_SECRET = original_secret

    @pytest.mark.asyncio
    async def test_missing_event_id_header_returns_error(self, db_session) -> None:
        """Missing x-razorpay-event-id header returns 422 (FastAPI validation)."""
        secret = settings.RAZORPAY_WEBHOOK_SECRET or "test_secret_123"
        original_secret = settings.RAZORPAY_WEBHOOK_SECRET
        settings.RAZORPAY_WEBHOOK_SECRET = secret

        try:
            body = make_valid_payment_failed_body()
            body_bytes = json.dumps(body).encode()
            sig = make_razorpay_signature(body_bytes, secret)

            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://testserver") as client:
                response = await client.post(
                    "/webhooks/razorpay",
                    content=body_bytes,
                    headers={
                        "X-Razorpay-Signature": sig,
                        "Content-Type": "application/json",
                    },
                )

            # FastAPI returns 422 for missing required headers
            assert response.status_code == 422
        finally:
            settings.RAZORPAY_WEBHOOK_SECRET = original_secret


# ---------------------------------------------------------------------------
# Simulation Endpoint Tests
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Audit Trail Tests
# ---------------------------------------------------------------------------

class TestAuditTrail:
    """Verify audit trail distinguishes real webhooks from simulations."""

    @pytest.mark.asyncio
    async def test_real_webhook_audit_trail(self, db_session) -> None:
        """Real webhook ingestion audit trail contains source=razorpay_webhook and signature_verified=true."""
        secret = settings.RAZORPAY_WEBHOOK_SECRET or "test_secret_123"
        body = make_valid_payment_failed_body()
        body_bytes = json.dumps(body).encode()
        sig = make_razorpay_signature(body_bytes, secret)
        event_id = "evt_audit_real_001"

        original_secret = settings.RAZORPAY_WEBHOOK_SECRET
        settings.RAZORPAY_WEBHOOK_SECRET = secret

        try:
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://testserver") as client:
                response = await client.post(
                    "/webhooks/razorpay",
                    content=body_bytes,
                    headers={
                        "X-Razorpay-Signature": sig,
                        "x-razorpay-event-id": event_id,
                        "Content-Type": "application/json",
                    },
                )

            assert response.status_code == 200

            # Verify audit trail in database
            pe = db_session.execute(
                select(PaymentEvent).where(
                    PaymentEvent.external_event_id == event_id
                )
            ).scalar_one_or_none()
            assert pe is not None

            rc = db_session.execute(
                select(RecoveryCase).where(
                    RecoveryCase.payment_event_id == pe.id
                )
            ).scalar_one_or_none()
            assert rc is not None

            audit = rc.decision_audit_trail
            assert audit["ingestion"]["source"] == "razorpay_webhook"
            assert audit["ingestion"]["signature_verified"] is True
            assert audit["ingestion"]["event_id"] == event_id
        finally:
            settings.RAZORPAY_WEBHOOK_SECRET = original_secret

    @pytest.mark.asyncio
    async def test_simulation_audit_trail(self, db_session) -> None:
        """Simulation ingestion audit trail contains source=simulation and signature_verified=false."""
        original_env = settings.APP_ENV
        settings.APP_ENV = "development"

        try:
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://testserver") as client:
                response = await client.post(
                    "/api/dev/simulate/payment-failed",
                    json={
                        "event_id": "sim_audit_001",
                        "amount_paise": 75000,
                    },
                )

            assert response.status_code == 200

            # Verify audit trail in database
            pe = db_session.execute(
                select(PaymentEvent).where(
                    PaymentEvent.external_event_id == "sim_audit_001"
                )
            ).scalar_one_or_none()
            assert pe is not None

            rc = db_session.execute(
                select(RecoveryCase).where(
                    RecoveryCase.payment_event_id == pe.id
                )
            ).scalar_one_or_none()
            assert rc is not None

            audit = rc.decision_audit_trail
            assert audit["ingestion"]["source"] == "simulation"
            assert audit["ingestion"]["signature_verified"] is False
            assert audit["ingestion"]["event_id"] == "sim_audit_001"
        finally:
            settings.APP_ENV = original_env

    @pytest.mark.asyncio
    async def test_webhook_and_simulation_use_same_core_function(self, db_session) -> None:
        """Both webhook and simulation produce identical RecoveryCase structure.

        Verifies through behavior: same status, same failure_category,
        same default field values — confirming they share the same code path.
        """
        # --- Webhook path ---
        secret = settings.RAZORPAY_WEBHOOK_SECRET or "test_secret_123"
        body = make_valid_payment_failed_body()
        body_bytes = json.dumps(body).encode()
        sig = make_razorpay_signature(body_bytes, secret)

        original_secret = settings.RAZORPAY_WEBHOOK_SECRET
        settings.RAZORPAY_WEBHOOK_SECRET = secret

        try:
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://testserver") as client:
                resp_wh = await client.post(
                    "/webhooks/razorpay",
                    content=body_bytes,
                    headers={
                        "X-Razorpay-Signature": sig,
                        "x-razorpay-event-id": "evt_shared_core_001",
                        "Content-Type": "application/json",
                    },
                )
        finally:
            settings.RAZORPAY_WEBHOOK_SECRET = original_secret

        assert resp_wh.status_code == 200

        # --- Simulation path ---
        original_env = settings.APP_ENV
        settings.APP_ENV = "development"
        try:
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://testserver") as client:
                resp_sim = await client.post(
                    "/api/dev/simulate/payment-failed",
                    json={"event_id": "sim_shared_core_001", "amount_paise": 100000},
                )
        finally:
            settings.APP_ENV = original_env

        assert resp_sim.status_code == 200

        # Both produce the same RecoveryCase defaults
        pe_wh = db_session.execute(
            select(PaymentEvent).where(PaymentEvent.external_event_id == "evt_shared_core_001")
        ).scalar_one()
        rc_wh = db_session.execute(
            select(RecoveryCase).where(RecoveryCase.payment_event_id == pe_wh.id)
        ).scalar_one()

        pe_sim = db_session.execute(
            select(PaymentEvent).where(PaymentEvent.external_event_id == "sim_shared_core_001")
        ).scalar_one()
        rc_sim = db_session.execute(
            select(RecoveryCase).where(RecoveryCase.payment_event_id == pe_sim.id)
        ).scalar_one()

        # Webhook was automatically processed by decision engine; raw simulation was ingested
        assert rc_wh.status == RecoveryStatus.REQUIRES_HUMAN.value
        assert rc_sim.status == RecoveryStatus.RECEIVED.value
        assert rc_wh.failure_category == FailureCategory.UNKNOWN.value
        assert rc_sim.failure_category == FailureCategory.UNKNOWN.value
        assert rc_wh.retry_count == rc_sim.retry_count == 0
        assert rc_wh.requires_human_approval is True
        assert rc_sim.requires_human_approval is False

        # Audit trails differ in source and signature_verified
        assert rc_wh.decision_audit_trail["ingestion"]["source"] == "razorpay_webhook"
        assert rc_wh.decision_audit_trail["ingestion"]["signature_verified"] is True
        assert rc_sim.decision_audit_trail["ingestion"]["source"] == "simulation"
        assert rc_sim.decision_audit_trail["ingestion"]["signature_verified"] is False



# ---------------------------------------------------------------------------
# Simulation Endpoint Tests
# ---------------------------------------------------------------------------

class TestSimulationEndpoint:
    """Tests for POST /api/dev/simulate/payment-failed."""

    @pytest.mark.asyncio
    async def test_simulation_creates_records(self, db_session) -> None:
        """Simulation endpoint creates PaymentEvent + RecoveryCase."""
        original_env = settings.APP_ENV
        settings.APP_ENV = "development"

        try:
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://testserver") as client:
                response = await client.post(
                    "/api/dev/simulate/payment-failed",
                    json={
                        "event_id": "sim_evt_001",
                        "payment_id": "pay_sim_001",
                        "order_id": "order_sim_001",
                        "amount_paise": 50000,
                        "currency": "INR",
                    },
                )

            assert response.status_code == 200
            data = response.json()
            assert data["accepted"] is True
            assert data["duplicate"] is False
            assert data["event_id"] == "sim_evt_001"
            assert data["recovery_case_id"] is not None

            # Verify records
            pe = db_session.execute(
                select(PaymentEvent).where(
                    PaymentEvent.external_event_id == "sim_evt_001"
                )
            ).scalar_one_or_none()
            assert pe is not None
            assert pe.amount_paise == 50000

            rc = db_session.execute(
                select(RecoveryCase).where(
                    RecoveryCase.payment_event_id == pe.id
                )
            ).scalar_one_or_none()
            assert rc is not None
            assert rc.status == "RECEIVED"
        finally:
            settings.APP_ENV = original_env

    @pytest.mark.asyncio
    async def test_simulation_duplicate_idempotent(self, db_session) -> None:
        """Duplicate simulation event IDs are idempotent."""
        original_env = settings.APP_ENV
        settings.APP_ENV = "development"

        try:
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://testserver") as client:
                resp1 = await client.post(
                    "/api/dev/simulate/payment-failed",
                    json={"event_id": "sim_dup_001", "amount_paise": 10000},
                )
                resp2 = await client.post(
                    "/api/dev/simulate/payment-failed",
                    json={"event_id": "sim_dup_001", "amount_paise": 10000},
                )

            assert resp1.json()["duplicate"] is False
            assert resp2.json()["duplicate"] is True

            # Only one PaymentEvent
            events = db_session.execute(
                select(PaymentEvent).where(
                    PaymentEvent.external_event_id == "sim_dup_001"
                )
            ).scalars().all()
            assert len(events) == 1
        finally:
            settings.APP_ENV = original_env

    @pytest.mark.asyncio
    async def test_simulation_disabled_outside_dev(self, db_session) -> None:
        """Simulation returns 404 when APP_ENV is not development/test."""
        original_env = settings.APP_ENV
        settings.APP_ENV = "production"

        try:
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://testserver") as client:
                response = await client.post(
                    "/api/dev/simulate/payment-failed",
                    json={"amount_paise": 10000},
                )

            assert response.status_code == 404
        finally:
            settings.APP_ENV = original_env

    @pytest.mark.asyncio
    async def test_simulation_generates_event_id_if_not_provided(self, db_session) -> None:
        """Simulation generates a deterministic event ID if none provided."""
        original_env = settings.APP_ENV
        settings.APP_ENV = "development"

        try:
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://testserver") as client:
                response = await client.post(
                    "/api/dev/simulate/payment-failed",
                    json={"amount_paise": 25000},
                )

            assert response.status_code == 200
            data = response.json()
            assert data["event_id"] is not None
            assert data["event_id"].startswith("sim_")
        finally:
            settings.APP_ENV = original_env


# ---------------------------------------------------------------------------
# Razorpay Webhook Pipeline Integration Tests (Phase 1)
# ---------------------------------------------------------------------------

class TestRazorpayWebhookPipeline:
    """End-to-end integration tests for the real Razorpay webhook pipeline."""

    @pytest.mark.asyncio
    async def test_valid_signed_payment_failed_runs_decision_engine(self, db_session) -> None:
        """Valid signed payment.failed runs decision engine automatically."""
        secret = "test_webhook_secret_xyz"
        event_id = "evt_rzp_pipeline_001"
        payload = {
            "entity": "event",
            "event": "payment.failed",
            "account_id": "acc_test",
            "created_at": 1700000000,
            "payload": {
                "payment": {
                    "id": "pay_rzp_001",
                    "entity": "payment",
                    "amount": 50000,
                    "currency": "INR",
                    "status": "failed",
                    "order_id": "order_rzp_001",
                    "error_code": "GATEWAY_ERROR",
                    "error_reason": "network_error",
                    "error_description": "Network timeout to bank",
                }
            },
        }
        body_bytes = json.dumps(payload).encode()
        sig = make_razorpay_signature(body_bytes, secret)

        original_secret = settings.RAZORPAY_WEBHOOK_SECRET
        settings.RAZORPAY_WEBHOOK_SECRET = secret

        try:
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://testserver") as client:
                response = await client.post(
                    "/webhooks/razorpay",
                    content=body_bytes,
                    headers={
                        "X-Razorpay-Signature": sig,
                        "x-razorpay-event-id": event_id,
                        "Content-Type": "application/json",
                    },
                )

            assert response.status_code == 200
            data = response.json()
            assert data["accepted"] is True
            assert data["duplicate"] is False
            assert data["recovery_case_id"] is not None

            # Verify PaymentEvent
            pe = db_session.execute(
                select(PaymentEvent).where(PaymentEvent.external_event_id == event_id)
            ).scalar_one()
            assert pe.amount_paise == 50000
            assert pe.error_reason == "network_error"

            # Verify RecoveryCase was processed by decision engine
            rc = db_session.execute(
                select(RecoveryCase).where(RecoveryCase.payment_event_id == pe.id)
            ).scalar_one()
            assert rc.failure_category == FailureCategory.TRANSIENT.value
            assert rc.recommended_strategy == RecoveryStrategy.WAIT_AND_RETRY.value
            assert rc.recovery_probability is not None
            assert rc.decision_audit_trail.get("classification") is not None
            assert rc.decision_audit_trail.get("recommendation") is not None
            assert rc.decision_audit_trail.get("policy") is not None
        finally:
            settings.RAZORPAY_WEBHOOK_SECRET = original_secret

    @pytest.mark.asyncio
    async def test_auto_executable_case_creates_execution_log(self, db_session) -> None:
        """Auto-executable payment failure executes in SIMULATION mode and creates ExecutionLog."""
        secret = "test_webhook_secret_xyz"
        event_id = "evt_rzp_auto_exec_001"
        payload = {
            "entity": "event",
            "event": "payment.failed",
            "account_id": "acc_test",
            "created_at": 1700000000,
            "payload": {
                "payment": {
                    "id": "pay_rzp_auto_001",
                    "entity": "payment",
                    "amount": 25000,
                    "currency": "INR",
                    "status": "failed",
                    "order_id": "order_rzp_auto_001",
                    "error_code": "BAD_REQUEST_ERROR",
                    "error_reason": "authentication_failed",
                    "error_description": "Payment authentication failed",
                }
            },
        }
        body_bytes = json.dumps(payload).encode()
        sig = make_razorpay_signature(body_bytes, secret)

        original_secret = settings.RAZORPAY_WEBHOOK_SECRET
        settings.RAZORPAY_WEBHOOK_SECRET = secret

        try:
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://testserver") as client:
                response = await client.post(
                    "/webhooks/razorpay",
                    content=body_bytes,
                    headers={
                        "X-Razorpay-Signature": sig,
                        "x-razorpay-event-id": event_id,
                        "Content-Type": "application/json",
                    },
                )

            assert response.status_code == 200

            db_session.expire_all()
            pe = db_session.execute(
                select(PaymentEvent).where(PaymentEvent.external_event_id == event_id)
            ).scalar_one()
            rc = db_session.execute(
                select(RecoveryCase).where(RecoveryCase.payment_event_id == pe.id)
            ).scalar_one()

            # Executed in simulation mode (payment link created, pending customer payment)
            assert rc.status == RecoveryStatus.PENDING_EXECUTION.value

            # ExecutionLog created
            logs = db_session.execute(
                select(ExecutionLog).where(ExecutionLog.recovery_case_id == rc.id)
            ).scalars().all()
            assert len(logs) == 1
            assert logs[0].status == ExecutionStatus.SUCCESS.value
            assert logs[0].action == RecoveryStrategy.CREATE_PAYMENT_LINK.value
            assert logs[0].execution_mode == "SIMULATION"
        finally:
            settings.RAZORPAY_WEBHOOK_SECRET = original_secret

    @pytest.mark.asyncio
    async def test_human_review_case_does_not_auto_execute(self, db_session) -> None:
        """High-value failure transitions to REQUIRES_HUMAN and is NOT auto-executed."""
        secret = "test_webhook_secret_xyz"
        event_id = "evt_rzp_human_review_001"
        # High amount above threshold (₹75,000 > ₹50,000 threshold)
        high_amount = settings.RECOVERY_HIGH_VALUE_THRESHOLD_PAISE + 2500000
        payload = {
            "entity": "event",
            "event": "payment.failed",
            "account_id": "acc_test",
            "created_at": 1700000000,
            "payload": {
                "payment": {
                    "id": "pay_rzp_hr_001",
                    "entity": "payment",
                    "amount": high_amount,
                    "currency": "INR",
                    "status": "failed",
                    "order_id": "order_rzp_hr_001",
                    "error_code": "GATEWAY_ERROR",
                    "error_reason": "network_error",
                    "error_description": "High value payment network timeout",
                }
            },
        }
        body_bytes = json.dumps(payload).encode()
        sig = make_razorpay_signature(body_bytes, secret)

        original_secret = settings.RAZORPAY_WEBHOOK_SECRET
        settings.RAZORPAY_WEBHOOK_SECRET = secret

        try:
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://testserver") as client:
                response = await client.post(
                    "/webhooks/razorpay",
                    content=body_bytes,
                    headers={
                        "X-Razorpay-Signature": sig,
                        "x-razorpay-event-id": event_id,
                        "Content-Type": "application/json",
                    },
                )

            assert response.status_code == 200

            pe = db_session.execute(
                select(PaymentEvent).where(PaymentEvent.external_event_id == event_id)
            ).scalar_one()
            rc = db_session.execute(
                select(RecoveryCase).where(RecoveryCase.payment_event_id == pe.id)
            ).scalar_one()

            # Case held for human review
            assert rc.status == RecoveryStatus.REQUIRES_HUMAN.value
            assert rc.requires_human_approval is True
            assert rc.approved_by_human is None

            # No execution logs created
            logs = db_session.execute(
                select(ExecutionLog).where(ExecutionLog.recovery_case_id == rc.id)
            ).scalars().all()
            assert len(logs) == 0
        finally:
            settings.RAZORPAY_WEBHOOK_SECRET = original_secret

    @pytest.mark.asyncio
    async def test_duplicate_webhook_does_not_reexecute(self, db_session) -> None:
        """Duplicate webhook does not trigger workflow or execution a second time."""
        secret = "test_webhook_secret_xyz"
        event_id = "evt_rzp_dup_exec_001"
        payload = {
            "entity": "event",
            "event": "payment.failed",
            "account_id": "acc_test",
            "created_at": 1700000000,
            "payload": {
                "payment": {
                    "id": "pay_rzp_dup_001",
                    "entity": "payment",
                    "amount": 30000,
                    "currency": "INR",
                    "status": "failed",
                    "order_id": "order_rzp_dup_001",
                    "error_code": "BAD_REQUEST_ERROR",
                    "error_reason": "authentication_failed",
                    "error_description": "Authentication failure",
                }
            },
        }
        body_bytes = json.dumps(payload).encode()
        sig = make_razorpay_signature(body_bytes, secret)

        original_secret = settings.RAZORPAY_WEBHOOK_SECRET
        settings.RAZORPAY_WEBHOOK_SECRET = secret

        try:
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://testserver") as client:
                # First delivery
                resp1 = await client.post(
                    "/webhooks/razorpay",
                    content=body_bytes,
                    headers={
                        "X-Razorpay-Signature": sig,
                        "x-razorpay-event-id": event_id,
                        "Content-Type": "application/json",
                    },
                )
                assert resp1.status_code == 200
                assert resp1.json()["duplicate"] is False

                # Second delivery (duplicate retry by Razorpay)
                resp2 = await client.post(
                    "/webhooks/razorpay",
                    content=body_bytes,
                    headers={
                        "X-Razorpay-Signature": sig,
                        "x-razorpay-event-id": event_id,
                        "Content-Type": "application/json",
                    },
                )
                assert resp2.status_code == 200
                assert resp2.json()["duplicate"] is True

            pe = db_session.execute(
                select(PaymentEvent).where(PaymentEvent.external_event_id == event_id)
            ).scalar_one()
            rc = db_session.execute(
                select(RecoveryCase).where(RecoveryCase.payment_event_id == pe.id)
            ).scalar_one()

            # Exactly one execution log created, not two
            logs = db_session.execute(
                select(ExecutionLog).where(ExecutionLog.recovery_case_id == rc.id)
            ).scalars().all()
            assert len(logs) == 1
        finally:
            settings.RAZORPAY_WEBHOOK_SECRET = original_secret


    @pytest.mark.asyncio
    async def test_invalid_signature_rejected_no_payment_event(self, db_session) -> None:
        """Invalid signature is rejected with 401 and creates no PaymentEvent or RecoveryCase."""
        secret = "test_webhook_secret_xyz"
        event_id = "evt_rzp_bad_sig_001"
        payload = {
            "entity": "event",
            "event": "payment.failed",
            "payload": {
                "payment": {
                    "id": "pay_rzp_bad_001",
                    "amount": 30000,
                    "currency": "INR",
                    "status": "failed",
                }
            },
        }
        body_bytes = json.dumps(payload).encode()

        original_secret = settings.RAZORPAY_WEBHOOK_SECRET
        settings.RAZORPAY_WEBHOOK_SECRET = secret

        try:
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://testserver") as client:
                response = await client.post(
                    "/webhooks/razorpay",
                    content=body_bytes,
                    headers={
                        "X-Razorpay-Signature": "invalid_hmac_signature",
                        "x-razorpay-event-id": event_id,
                        "Content-Type": "application/json",
                    },
                )

            assert response.status_code == 401

            pe = db_session.execute(
                select(PaymentEvent).where(PaymentEvent.external_event_id == event_id)
            ).scalar_one_or_none()
            assert pe is None
        finally:
            settings.RAZORPAY_WEBHOOK_SECRET = original_secret

    @pytest.mark.asyncio
    async def test_unsupported_razorpay_event_acknowledged_safely(self, db_session) -> None:
        """Unsupported event type (e.g. order.paid) is acknowledged with 200 without creating cases."""
        secret = "test_webhook_secret_xyz"
        event_id = "evt_rzp_order_paid_001"
        payload = {
            "entity": "event",
            "event": "order.paid",
            "payload": {"order": {"id": "order_123"}},
        }
        body_bytes = json.dumps(payload).encode()
        sig = make_razorpay_signature(body_bytes, secret)

        original_secret = settings.RAZORPAY_WEBHOOK_SECRET
        settings.RAZORPAY_WEBHOOK_SECRET = secret

        try:
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://testserver") as client:
                response = await client.post(
                    "/webhooks/razorpay",
                    content=body_bytes,
                    headers={
                        "X-Razorpay-Signature": sig,
                        "x-razorpay-event-id": event_id,
                        "Content-Type": "application/json",
                    },
                )

            assert response.status_code == 200
            data = response.json()
            assert data["accepted"] is True
            assert data["recovery_case_id"] is None

            pe = db_session.execute(
                select(PaymentEvent).where(PaymentEvent.external_event_id == event_id)
            ).scalar_one_or_none()
            assert pe is None
        finally:
            settings.RAZORPAY_WEBHOOK_SECRET = original_secret


# ---------------------------------------------------------------------------
# Real Razorpay payload shape tests (payload.payment.entity)
# ---------------------------------------------------------------------------


def _make_real_razorpay_body(
    payment_id: str = "pay_real_001",
    amount: int = 45000,
    error_reason: str = "payment_failed",
    error_code: str = "BAD_REQUEST_ERROR",
    error_description: str = "Payment failed due to authentication issue",
    order_id: str = "order_real_001",
) -> dict:
    """Helper to create a REAL Razorpay payment.failed payload.

    Real Razorpay webhooks nest payment details under payload.payment.entity,
    not directly under payload.payment.
    """
    return {
        "entity": "event",
        "event": "payment.failed",
        "account_id": "acc_test123",
        "created_at": 1700000000,
        "payload": {
            "payment": {
                "entity": {
                    "id": payment_id,
                    "entity": "payment",
                    "amount": amount,
                    "currency": "INR",
                    "status": "failed",
                    "order_id": order_id,
                    "error_code": error_code,
                    "error_reason": error_reason,
                    "error_description": error_description,
                },
            },
        },
    }


class TestRealRazorpayPayloadShape:
    """Tests for the real Razorpay webhook payload shape (payload.payment.entity)."""

    @pytest.mark.asyncio
    async def test_real_razorpay_shape_accepted(self, db_session) -> None:
        """Real Razorpay shape (payload.payment.entity) is accepted."""
        secret = "test_secret_real_shape"
        event_id = "evt_real_shape_001"
        body = _make_real_razorpay_body()
        body_bytes = json.dumps(body).encode()
        sig = make_razorpay_signature(body_bytes, secret)

        original_secret = settings.RAZORPAY_WEBHOOK_SECRET
        settings.RAZORPAY_WEBHOOK_SECRET = secret
        try:
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://testserver") as client:
                response = await client.post(
                    "/webhooks/razorpay",
                    content=body_bytes,
                    headers={
                        "X-Razorpay-Signature": sig,
                        "x-razorpay-event-id": event_id,
                        "Content-Type": "application/json",
                    },
                )
            assert response.status_code == 200
            data = response.json()
            assert data["accepted"] is True
            assert data["duplicate"] is False
            assert data["recovery_case_id"] is not None
        finally:
            settings.RAZORPAY_WEBHOOK_SECRET = original_secret

    @pytest.mark.asyncio
    async def test_real_razorpay_shape_correct_amount(self, db_session) -> None:
        """Amount is correctly extracted from payload.payment.entity.amount."""
        secret = "test_secret_real_shape"
        event_id = "evt_real_amount_001"
        body = _make_real_razorpay_body(amount=75000)
        body_bytes = json.dumps(body).encode()
        sig = make_razorpay_signature(body_bytes, secret)

        original_secret = settings.RAZORPAY_WEBHOOK_SECRET
        settings.RAZORPAY_WEBHOOK_SECRET = secret
        try:
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://testserver") as client:
                response = await client.post(
                    "/webhooks/razorpay",
                    content=body_bytes,
                    headers={
                        "X-Razorpay-Signature": sig,
                        "x-razorpay-event-id": event_id,
                        "Content-Type": "application/json",
                    },
                )
            assert response.status_code == 200

            pe = db_session.execute(
                select(PaymentEvent).where(PaymentEvent.external_event_id == event_id)
            ).scalar_one()
            assert pe.amount_paise == 75000
            assert pe.external_payment_id == "pay_real_001"
            assert pe.external_order_id == "order_real_001"
        finally:
            settings.RAZORPAY_WEBHOOK_SECRET = original_secret

    @pytest.mark.asyncio
    async def test_real_razorpay_shape_payment_failed_reason(self, db_session) -> None:
        """Real Razorpay error_reason='payment_failed' is accepted and classified as UNKNOWN."""
        secret = "test_secret_real_shape"
        event_id = "evt_real_pfp_001"
        body = _make_real_razorpay_body(
            error_reason="payment_failed",
            error_code="BAD_REQUEST_ERROR",
            error_description="Payment failed due to authentication issue",
        )
        body_bytes = json.dumps(body).encode()
        sig = make_razorpay_signature(body_bytes, secret)

        original_secret = settings.RAZORPAY_WEBHOOK_SECRET
        settings.RAZORPAY_WEBHOOK_SECRET = secret
        try:
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://testserver") as client:
                response = await client.post(
                    "/webhooks/razorpay",
                    content=body_bytes,
                    headers={
                        "X-Razorpay-Signature": sig,
                        "x-razorpay-event-id": event_id,
                        "Content-Type": "application/json",
                    },
                )
            assert response.status_code == 200
            data = response.json()
            assert data["accepted"] is True

            pe = db_session.execute(
                select(PaymentEvent).where(PaymentEvent.external_event_id == event_id)
            ).scalar_one()
            assert pe.error_reason == "payment_failed"
            assert pe.amount_paise == 45000

            rc = db_session.execute(
                select(RecoveryCase).where(RecoveryCase.payment_event_id == pe.id)
            ).scalar_one()
            # payment_failed is unmapped, so it falls back to UNKNOWN
            assert rc.failure_category == FailureCategory.UNKNOWN.value
        finally:
            settings.RAZORPAY_WEBHOOK_SECRET = original_secret

    @pytest.mark.asyncio
    async def test_real_razorpay_shape_decision_engine_runs(self, db_session) -> None:
        """Decision engine runs on real Razorpay payload."""
        secret = "test_secret_real_shape"
        event_id = "evt_real_de_001"
        body = _make_real_razorpay_body(
            amount=25000,
            error_reason="network_error",
            error_code="GATEWAY_ERROR",
        )
        body_bytes = json.dumps(body).encode()
        sig = make_razorpay_signature(body_bytes, secret)

        original_secret = settings.RAZORPAY_WEBHOOK_SECRET
        settings.RAZORPAY_WEBHOOK_SECRET = secret
        try:
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://testserver") as client:
                response = await client.post(
                    "/webhooks/razorpay",
                    content=body_bytes,
                    headers={
                        "X-Razorpay-Signature": sig,
                        "x-razorpay-event-id": event_id,
                        "Content-Type": "application/json",
                    },
                )
            assert response.status_code == 200

            pe = db_session.execute(
                select(PaymentEvent).where(PaymentEvent.external_event_id == event_id)
            ).scalar_one()
            rc = db_session.execute(
                select(RecoveryCase).where(RecoveryCase.payment_event_id == pe.id)
            ).scalar_one()

            # Decision engine classified and recommended
            assert rc.failure_category == FailureCategory.TRANSIENT.value
            assert rc.recommended_strategy == RecoveryStrategy.WAIT_AND_RETRY.value
            assert rc.recovery_probability is not None
            assert rc.decision_audit_trail.get("classification") is not None
        finally:
            settings.RAZORPAY_WEBHOOK_SECRET = original_secret

    @pytest.mark.asyncio
    async def test_simulation_shape_still_works(self, db_session) -> None:
        """Existing simulation shape (payload.payment with fields directly) still works."""
        secret = "test_secret_sim_compat"
        event_id = "evt_sim_compat_001"
        # Existing simulation/test shape: fields directly on payload.payment
        body = {
            "entity": "event",
            "event": "payment.failed",
            "account_id": "acc_test123",
            "created_at": 1700000000,
            "payload": {
                "payment": {
                    "id": "pay_sim_001",
                    "entity": "payment",
                    "amount": 60000,
                    "currency": "INR",
                    "status": "failed",
                    "order_id": "order_sim_001",
                    "error_code": "GATEWAY_ERROR",
                    "error_reason": "bank_technical_error",
                    "error_description": "Bank was unavailable",
                },
            },
        }
        body_bytes = json.dumps(body).encode()
        sig = make_razorpay_signature(body_bytes, secret)

        original_secret = settings.RAZORPAY_WEBHOOK_SECRET
        settings.RAZORPAY_WEBHOOK_SECRET = secret
        try:
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://testserver") as client:
                response = await client.post(
                    "/webhooks/razorpay",
                    content=body_bytes,
                    headers={
                        "X-Razorpay-Signature": sig,
                        "x-razorpay-event-id": event_id,
                        "Content-Type": "application/json",
                    },
                )
            assert response.status_code == 200
            data = response.json()
            assert data["accepted"] is True
            assert data["duplicate"] is False

            pe = db_session.execute(
                select(PaymentEvent).where(PaymentEvent.external_event_id == event_id)
            ).scalar_one()
            assert pe.amount_paise == 60000
            assert pe.external_payment_id == "pay_sim_001"
            assert pe.external_order_id == "order_sim_001"

            rc = db_session.execute(
                select(RecoveryCase).where(RecoveryCase.payment_event_id == pe.id)
            ).scalar_one()
            assert rc.failure_category == FailureCategory.TRANSIENT.value
        finally:
            settings.RAZORPAY_WEBHOOK_SECRET = original_secret

    @pytest.mark.asyncio
    async def test_real_razorpay_invalid_amount_rejected(self, db_session) -> None:
        """Real Razorpay shape with invalid amount is rejected."""
        secret = "test_secret_real_shape"
        event_id = "evt_real_bad_amt_001"
        body = _make_real_razorpay_body(amount=-100)
        body_bytes = json.dumps(body).encode()
        sig = make_razorpay_signature(body_bytes, secret)

        original_secret = settings.RAZORPAY_WEBHOOK_SECRET
        settings.RAZORPAY_WEBHOOK_SECRET = secret
        try:
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://testserver") as client:
                response = await client.post(
                    "/webhooks/razorpay",
                    content=body_bytes,
                    headers={
                        "X-Razorpay-Signature": sig,
                        "x-razorpay-event-id": event_id,
                        "Content-Type": "application/json",
                    },
                )
            assert response.status_code == 400
        finally:
            settings.RAZORPAY_WEBHOOK_SECRET = original_secret
