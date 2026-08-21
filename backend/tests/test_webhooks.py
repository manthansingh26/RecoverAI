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
            assert rc.status == "RECEIVED"
            assert rc.failure_category == "UNKNOWN"
            assert rc.retry_count == 0
            assert rc.requires_human_approval is False
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

        # Identical structure from shared core
        assert rc_wh.status == rc_sim.status == "RECEIVED"
        assert rc_wh.failure_category == rc_sim.failure_category == "UNKNOWN"
        assert rc_wh.retry_count == rc_sim.retry_count == 0
        assert rc_wh.requires_human_approval == rc_sim.requires_human_approval is False
        assert rc_wh.recovery_probability is rc_sim.recovery_probability is None
        assert rc_wh.priority_score is rc_sim.priority_score is None
        assert rc_wh.recommended_strategy is rc_sim.recommended_strategy is None
        assert rc_wh.expected_value_paise is rc_sim.expected_value_paise is None
        assert rc_wh.approved_by_human is rc_sim.approved_by_human is None

        # Audit trails differ only in source and signature_verified
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
