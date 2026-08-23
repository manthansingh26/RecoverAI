"""Comprehensive test suite for Milestone 12 — Recovery Resolution & Payment Lifecycle Completion.

Tests cover all 20 specified scenarios:
1. payment.captured resolves via recovery_case_id notes
2. payment.captured resolves via original order_id fallback
3. payment.captured resolves via recovery order_id in decision_audit_trail
4. amount mismatch rejected
5. currency mismatch rejected
6. invalid notes UUID handled safely
7. notes case/order mismatch rejected
8. duplicate payment.captured idempotency
9. order.paid does not mutate financial state
10. late recovery after RESOLVED_FAILED
11. direct payment on REQUIRES_HUMAN
12. unrelated payment acknowledged without case mutation
13. invalid signature rejected
14. missing event ID rejected
15. recovery checkout creates TEST order with case notes
16. recovery checkout reuses existing active recovery order
17. repeated checkout requests do not create duplicate active orders
18. payment recovery disarms scheduler (next_run_at is None)
19. PAYMENT_RECOVERED activity generated in activity feed
20. dashboard recovered revenue updates accurately on webhook resolution
"""

import hashlib
import json
import uuid
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

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
from app.services.payment_normalizer import normalize_payment_captured
from app.services.recovery_resolver import resolve_recovery_by_payment
from app.services.recovery_review import get_dashboard_activity, get_dashboard_analytics
from app.services.webhook_security import verify_razorpay_signature
from tests.conftest import make_razorpay_signature


# ---------------------------------------------------------------------------
# Test Fixtures & Helpers
# ---------------------------------------------------------------------------

def _create_test_case(
    db_session,
    *,
    status_val: str = RecoveryStatus.PENDING_EXECUTION.value,
    amount_paise: int = 250000,
    currency: str = "INR",
    order_id: str = "order_orig_12345",
    strategy: str = RecoveryStrategy.CREATE_PAYMENT_LINK.value,
    retry_count: int = 1,
) -> tuple[PaymentEvent, RecoveryCase]:
    """Helper to create a linked PaymentEvent and RecoveryCase in the test DB."""
    pe = PaymentEvent(
        event_type="payment.failed",
        external_event_id=f"evt_fail_{uuid.uuid4().hex[:10]}",
        external_payment_id=f"pay_fail_{uuid.uuid4().hex[:10]}",
        external_order_id=order_id,
        amount_paise=amount_paise,
        currency=currency,
        error_code="BAD_REQUEST_ERROR",
        error_reason="authentication_failed",
        error_description="Customer dropped OTP verification",
        raw_payload={"mock": "failed_payload"},
        payload_hash="mock_hash",
    )
    db_session.add(pe)
    db_session.flush()

    rc = RecoveryCase(
        payment_event_id=pe.id,
        status=status_val,
        failure_category=FailureCategory.AUTHENTICATION.value,
        recommended_strategy=strategy,
        next_run_at=datetime.now(timezone.utc),
        retry_count=retry_count,
        requires_human_approval=False,
        approved_by_human=True,
        decision_audit_trail={
            "initial": "failure_logged",
        },
    )
    db_session.add(rc)
    db_session.commit()
    db_session.refresh(rc)
    db_session.refresh(pe)
    return pe, rc


def _make_captured_payload(
    *,
    payment_id: str = "pay_succ_123",
    order_id: str = "order_orig_12345",
    amount_paise: int = 250000,
    currency: str = "INR",
    status_val: str = "captured",
    notes: dict | None = None,
) -> dict:
    """Construct a real Razorpay payment.captured webhook payload dictionary."""
    return {
        "event": "payment.captured",
        "payload": {
            "payment": {
                "entity": {
                    "id": payment_id,
                    "order_id": order_id,
                    "amount": amount_paise,
                    "currency": currency,
                    "status": status_val,
                    "notes": notes or {},
                    "method": "card",
                    "captured": True,
                }
            }
        },
    }


# ---------------------------------------------------------------------------
# Unit & Integration Tests for Recovery Resolver
# ---------------------------------------------------------------------------

class TestRecoveryResolver:
    """Test suite for payment.captured canonical resolution."""

    def test_payment_captured_resolves_pending_case_via_notes(self, db_session):
        """1. payment.captured with notes.recovery_case_id resolves case to RESOLVED_SUCCESS."""
        pe, rc = _create_test_case(db_session, amount_paise=250000)
        assert rc.status == RecoveryStatus.PENDING_EXECUTION.value
        assert rc.next_run_at is not None

        captured_body = _make_captured_payload(
            payment_id="pay_succ_001",
            order_id="order_rec_999",
            amount_paise=250000,
            notes={"recovery_case_id": str(rc.id)},
        )

        normalized = normalize_payment_captured(
            event_id="evt_succ_001",
            payload_data=captured_body,
            raw_payload=captured_body,
        )

        response = resolve_recovery_by_payment(db_session, normalized)

        assert response.accepted is True
        assert response.duplicate is False
        assert response.recovery_case_id == str(rc.id)

        db_session.refresh(rc)
        assert rc.status == RecoveryStatus.RESOLVED_SUCCESS.value
        assert rc.next_run_at is None
        assert "recovery_completion" in rc.decision_audit_trail
        assert rc.decision_audit_trail["recovery_completion"]["payment_id"] == "pay_succ_001"

        # Check execution log
        logs = db_session.query(ExecutionLog).filter(
            ExecutionLog.recovery_case_id == rc.id,
            ExecutionLog.action == "PAYMENT_RECOVERED",
        ).all()
        assert len(logs) == 1
        assert logs[0].status == ExecutionStatus.SUCCESS.value

    def test_payment_captured_resolves_pending_case_via_original_order_id(self, db_session):
        """2. payment.captured without notes resolves via original external_order_id."""
        pe, rc = _create_test_case(db_session, order_id="order_orig_match_100")

        captured_body = _make_captured_payload(
            payment_id="pay_succ_002",
            order_id="order_orig_match_100",
            amount_paise=250000,
            notes={},  # No notes provided — direct retry
        )

        normalized = normalize_payment_captured(
            event_id="evt_succ_002",
            payload_data=captured_body,
            raw_payload=captured_body,
        )

        response = resolve_recovery_by_payment(db_session, normalized)

        assert response.accepted is True
        assert response.recovery_case_id == str(rc.id)

        db_session.refresh(rc)
        assert rc.status == RecoveryStatus.RESOLVED_SUCCESS.value
        assert rc.next_run_at is None

    def test_payment_captured_resolves_via_recovery_order_in_audit_trail(self, db_session):
        """3. payment.captured resolves via recovery_order stored in decision_audit_trail."""
        pe, rc = _create_test_case(db_session, order_id="order_orig_old_001")

        # Simulate that a recovery order was created earlier
        trail = dict(rc.decision_audit_trail)
        trail["recovery_order"] = {
            "order_id": "order_rec_stored_555",
            "amount_paise": 250000,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        rc.decision_audit_trail = trail
        db_session.commit()

        captured_body = _make_captured_payload(
            payment_id="pay_succ_003",
            order_id="order_rec_stored_555",
            amount_paise=250000,
            notes={},
        )

        normalized = normalize_payment_captured(
            event_id="evt_succ_003",
            payload_data=captured_body,
            raw_payload=captured_body,
        )

        response = resolve_recovery_by_payment(db_session, normalized)

        assert response.accepted is True
        assert response.recovery_case_id == str(rc.id)
        db_session.refresh(rc)
        assert rc.status == RecoveryStatus.RESOLVED_SUCCESS.value

    def test_amount_mismatch_rejected(self, db_session):
        """4. Payment amount mismatch is rejected with HTTP 400 (Gate 5)."""
        pe, rc = _create_test_case(db_session, amount_paise=5000000)  # ₹50,000 case

        # Attacker tries to resolve ₹50,000 case with ₹1 (100 paise)
        captured_body = _make_captured_payload(
            payment_id="pay_fraud_001",
            amount_paise=100,  # ₹1
            notes={"recovery_case_id": str(rc.id)},
        )

        normalized = normalize_payment_captured(
            event_id="evt_fraud_001",
            payload_data=captured_body,
            raw_payload=captured_body,
        )

        with pytest.raises(Exception) as exc_info:
            resolve_recovery_by_payment(db_session, normalized)

        assert "amount mismatch" in str(exc_info.value).lower()
        db_session.refresh(rc)
        assert rc.status == RecoveryStatus.PENDING_EXECUTION.value  # Unchanged!

    def test_currency_mismatch_rejected(self, db_session):
        """5. Currency mismatch is rejected with HTTP 400 (Gate 6)."""
        pe, rc = _create_test_case(db_session, currency="INR")

        captured_body = _make_captured_payload(
            payment_id="pay_usd_001",
            currency="USD",
            notes={"recovery_case_id": str(rc.id)},
        )

        normalized = normalize_payment_captured(
            event_id="evt_usd_001",
            payload_data=captured_body,
            raw_payload=captured_body,
        )

        with pytest.raises(Exception) as exc_info:
            resolve_recovery_by_payment(db_session, normalized)

        assert "currency mismatch" in str(exc_info.value).lower()
        db_session.refresh(rc)
        assert rc.status == RecoveryStatus.PENDING_EXECUTION.value

    def test_invalid_notes_uuid_handled_safely(self, db_session):
        """6. Malformed notes recovery_case_id UUID does not crash; falls back or acknowledges."""
        captured_body = _make_captured_payload(
            payment_id="pay_bad_uuid",
            order_id="order_unrelated_999",
            notes={"recovery_case_id": "not-a-valid-uuid"},
        )

        normalized = normalize_payment_captured(
            event_id="evt_bad_uuid",
            payload_data=captured_body,
            raw_payload=captured_body,
        )

        response = resolve_recovery_by_payment(db_session, normalized)
        assert response.accepted is True
        assert response.recovery_case_id is None

    def test_notes_case_and_order_conflict_rejected(self, db_session):
        """7. If notes point to Case A but order belongs to Case B, reject resolution."""
        pe_a, rc_a = _create_test_case(db_session, order_id="order_AAA")
        pe_b, rc_b = _create_test_case(db_session, order_id="order_BBB")

        captured_body = _make_captured_payload(
            payment_id="pay_conflict_001",
            order_id="order_BBB",  # Belongs to Case B
            notes={"recovery_case_id": str(rc_a.id)},  # Claims Case A
            amount_paise=250000,
        )

        normalized = normalize_payment_captured(
            event_id="evt_conflict_001",
            payload_data=captured_body,
            raw_payload=captured_body,
        )

        with pytest.raises(Exception) as exc_info:
            resolve_recovery_by_payment(db_session, normalized)

        assert "conflict" in str(exc_info.value).lower()
        db_session.refresh(rc_a)
        db_session.refresh(rc_b)
        assert rc_a.status == RecoveryStatus.PENDING_EXECUTION.value
        assert rc_b.status == RecoveryStatus.PENDING_EXECUTION.value

    def test_duplicate_payment_captured_idempotency(self, db_session):
        """8. Repeated payment.captured events return duplicate=True with no duplicate logs."""
        pe, rc = _create_test_case(db_session)

        captured_body = _make_captured_payload(
            payment_id="pay_dup_001",
            notes={"recovery_case_id": str(rc.id)},
        )

        normalized = normalize_payment_captured(
            event_id="evt_dup_001",
            payload_data=captured_body,
            raw_payload=captured_body,
        )

        # First resolution
        res1 = resolve_recovery_by_payment(db_session, normalized)
        assert res1.duplicate is False
        assert res1.recovery_case_id == str(rc.id)

        # Second delivery of same resolution
        res2 = resolve_recovery_by_payment(db_session, normalized)
        assert res2.accepted is True
        assert res2.duplicate is True
        assert res2.recovery_case_id == str(rc.id)

        # Confirm exactly 1 ExecutionLog was written
        logs = db_session.query(ExecutionLog).filter(
            ExecutionLog.recovery_case_id == rc.id,
            ExecutionLog.action == "PAYMENT_RECOVERED",
        ).all()
        assert len(logs) == 1

    def test_late_recovery_after_resolved_failed(self, db_session):
        """10. Late payment on RESOLVED_FAILED case safely transitions to RESOLVED_SUCCESS."""
        pe, rc = _create_test_case(
            db_session,
            status_val=RecoveryStatus.RESOLVED_FAILED.value,
        )

        captured_body = _make_captured_payload(
            payment_id="pay_late_001",
            notes={"recovery_case_id": str(rc.id)},
        )

        normalized = normalize_payment_captured(
            event_id="evt_late_001",
            payload_data=captured_body,
            raw_payload=captured_body,
        )

        res = resolve_recovery_by_payment(db_session, normalized)
        assert res.accepted is True
        assert res.recovery_case_id == str(rc.id)

        db_session.refresh(rc)
        assert rc.status == RecoveryStatus.RESOLVED_SUCCESS.value
        assert rc.decision_audit_trail["recovery_completion"]["previous_status"] == RecoveryStatus.RESOLVED_FAILED.value

    def test_direct_payment_on_requires_human(self, db_session):
        """11. Direct customer payment on REQUIRES_HUMAN case resolves it cleanly."""
        pe, rc = _create_test_case(
            db_session,
            status_val=RecoveryStatus.REQUIRES_HUMAN.value,
        )

        captured_body = _make_captured_payload(
            payment_id="pay_direct_001",
            notes={"recovery_case_id": str(rc.id)},
        )

        normalized = normalize_payment_captured(
            event_id="evt_direct_001",
            payload_data=captured_body,
            raw_payload=captured_body,
        )

        res = resolve_recovery_by_payment(db_session, normalized)
        assert res.accepted is True
        db_session.refresh(rc)
        assert rc.status == RecoveryStatus.RESOLVED_SUCCESS.value

    def test_unrelated_payment_acknowledged_safely(self, db_session):
        """12. Payment on an unrelated merchant order is acknowledged without touching any cases."""
        captured_body = _make_captured_payload(
            payment_id="pay_unrelated_111",
            order_id="order_random_merchant_999",
            notes={"some_other_app": "123"},
        )

        normalized = normalize_payment_captured(
            event_id="evt_unrelated_111",
            payload_data=captured_body,
            raw_payload=captured_body,
        )

        res = resolve_recovery_by_payment(db_session, normalized)
        assert res.accepted is True
        assert res.recovery_case_id is None
        assert "unrelated" in res.message.lower()

    def test_payment_recovery_disarms_scheduler(self, db_session):
        """18. Resolution sets next_run_at to None, disarming the automatic scheduler."""
        pe, rc = _create_test_case(db_session)
        assert rc.next_run_at is not None

        captured_body = _make_captured_payload(
            payment_id="pay_disarm_001",
            notes={"recovery_case_id": str(rc.id)},
        )

        normalized = normalize_payment_captured(
            event_id="evt_disarm_001",
            payload_data=captured_body,
            raw_payload=captured_body,
        )

        resolve_recovery_by_payment(db_session, normalized)
        db_session.refresh(rc)
        assert rc.next_run_at is None

    def test_activity_feed_includes_payment_recovered(self, db_session):
        """19. Activity feed returns PAYMENT_RECOVERED item for resolved case."""
        pe, rc = _create_test_case(db_session)

        captured_body = _make_captured_payload(
            payment_id="pay_feed_001",
            notes={"recovery_case_id": str(rc.id)},
        )
        normalized = normalize_payment_captured(
            event_id="evt_feed_001",
            payload_data=captured_body,
            raw_payload=captured_body,
        )
        resolve_recovery_by_payment(db_session, normalized)

        feed = get_dashboard_activity(db_session, limit=20)
        items = feed.get("items", [])
        recovered_items = [i for i in items if i["type"] == "PAYMENT_RECOVERED"]
        assert len(recovered_items) >= 1
        assert recovered_items[0]["recovery_case_id"] == str(rc.id)
        assert recovered_items[0]["payment_id"] == "pay_feed_001"

    def test_dashboard_analytics_updates_recovered_revenue(self, db_session):
        """20. Dashboard analytics reflects verified recovered revenue on RESOLVED_SUCCESS."""
        pe, rc = _create_test_case(db_session, amount_paise=75000)

        # Before resolution
        analytics_before = get_dashboard_analytics(db_session)
        before_recovered = analytics_before["financial"]["simulated_recovered_amount_paise"]

        # Resolve via webhook
        captured_body = _make_captured_payload(
            payment_id="pay_analytics_001",
            amount_paise=75000,
            notes={"recovery_case_id": str(rc.id)},
        )
        normalized = normalize_payment_captured(
            event_id="evt_analytics_001",
            payload_data=captured_body,
            raw_payload=captured_body,
        )
        resolve_recovery_by_payment(db_session, normalized)

        # After resolution
        analytics_after = get_dashboard_analytics(db_session)
        after_recovered = analytics_after["financial"]["simulated_recovered_amount_paise"]

        assert after_recovered == before_recovered + 75000


# ---------------------------------------------------------------------------
# HTTP Webhook Route Integration Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestWebhookRouteSuccessIntegration:
    """Integration tests for POST /webhooks/razorpay handling payment.captured and order.paid."""

    async def test_webhook_payment_captured_end_to_end(self, db_session):
        """Valid signed payment.captured webhook resolves case end-to-end."""
        pe, rc = _create_test_case(db_session, amount_paise=250000)
        secret = settings.RAZORPAY_WEBHOOK_SECRET or "test_secret_123"

        payload = _make_captured_payload(
            payment_id="pay_e2e_001",
            amount_paise=250000,
            notes={"recovery_case_id": str(rc.id)},
        )
        body_bytes = json.dumps(payload).encode()
        sig = make_razorpay_signature(body_bytes, secret)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            resp = await client.post(
                "/webhooks/razorpay",
                content=body_bytes,
                headers={
                    "Content-Type": "application/json",
                    "X-Razorpay-Signature": sig,
                    "x-razorpay-event-id": "evt_e2e_webhook_001",
                },
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["accepted"] is True
        assert data["recovery_case_id"] == str(rc.id)

        updated_rc = db_session.get(RecoveryCase, rc.id)
        assert updated_rc is not None
        assert updated_rc.status == RecoveryStatus.RESOLVED_SUCCESS.value

    async def test_webhook_order_paid_does_not_mutate_state(self, db_session):
        """9. order.paid event is acknowledged 200 OK without mutating case state."""
        pe, rc = _create_test_case(db_session)
        secret = settings.RAZORPAY_WEBHOOK_SECRET or "test_secret_123"

        payload = {
            "event": "order.paid",
            "payload": {
                "order": {
                    "entity": {
                        "id": "order_some_123",
                        "amount": 250000,
                        "status": "paid",
                    }
                }
            },
        }
        body_bytes = json.dumps(payload).encode()
        sig = make_razorpay_signature(body_bytes, secret)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            resp = await client.post(
                "/webhooks/razorpay",
                content=body_bytes,
                headers={
                    "Content-Type": "application/json",
                    "X-Razorpay-Signature": sig,
                    "x-razorpay-event-id": "evt_order_paid_001",
                },
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["accepted"] is True
        assert "order.paid" in data["message"]
        updated_rc = db_session.get(RecoveryCase, rc.id)
        assert updated_rc is not None
        assert updated_rc.status == RecoveryStatus.PENDING_EXECUTION.value  # Intact!

    async def test_webhook_invalid_signature_rejected(self, db_session):
        """13. Invalid webhook signature returns 401 Unauthorized."""
        payload = _make_captured_payload()
        body_bytes = json.dumps(payload).encode()

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            resp = await client.post(
                "/webhooks/razorpay",
                content=body_bytes,
                headers={
                    "Content-Type": "application/json",
                    "X-Razorpay-Signature": "invalid_signature_hex",
                    "x-razorpay-event-id": "evt_invalid_sig_001",
                },
            )

        assert resp.status_code == 401

    async def test_webhook_missing_event_id_rejected(self, db_session):
        """14. Missing x-razorpay-event-id header returns 400 or 422 Bad Request."""
        secret = settings.RAZORPAY_WEBHOOK_SECRET or "test_secret_123"
        payload = _make_captured_payload()
        body_bytes = json.dumps(payload).encode()
        sig = make_razorpay_signature(body_bytes, secret)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            resp = await client.post(
                "/webhooks/razorpay",
                content=body_bytes,
                headers={
                    "Content-Type": "application/json",
                    "X-Razorpay-Signature": sig,
                    # No x-razorpay-event-id
                },
            )

        assert resp.status_code in (400, 422)


# ---------------------------------------------------------------------------
# Recovery Checkout Endpoint Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestRecoveryCheckoutEndpoint:
    """Tests for POST /api/recovery-cases/{id}/recovery-checkout."""

    async def test_recovery_checkout_creates_test_order_with_notes(self, db_session):
        """15. Recovery checkout endpoint creates Razorpay Test order tagged with case notes."""
        pe, rc = _create_test_case(db_session, amount_paise=150000)

        mock_order = {
            "id": "order_rec_mock_150",
            "amount": 150000,
            "currency": "INR",
            "receipt": f"rcpt_rec_{str(rc.id)[:12]}",
            "status": "created",
        }

        with (
            patch.object(settings, "RAZORPAY_KEY_ID", "rzp_test_key_mock"),
            patch.object(settings, "RAZORPAY_KEY_SECRET", "secret_mock"),
            patch("razorpay.Client") as mock_client_cls,
        ):
            mock_client = MagicMock()
            mock_client.order.create.return_value = mock_order
            mock_client_cls.return_value = mock_client

            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://testserver") as client:
                resp = await client.post(
                    f"/api/recovery-cases/{rc.id}/recovery-checkout",
                )

        assert resp.status_code == 200
        data = resp.json()
        assert data["order_id"] == "order_rec_mock_150"
        assert data["key_id"] == "rzp_test_key_mock"
        assert data["amount"] == 150000
        assert data["recovery_case_id"] == str(rc.id)
        assert data["is_reused"] is False

        # Verify order creation payload contained metadata notes
        call_kwargs = mock_client.order.create.call_args[1]["data"]
        assert call_kwargs["notes"]["recovery_case_id"] == str(rc.id)
        assert call_kwargs["amount"] == 150000

        # Verify order was saved in decision_audit_trail
        updated_rc = db_session.get(RecoveryCase, rc.id)
        assert updated_rc is not None
        assert updated_rc.decision_audit_trail["recovery_order"]["order_id"] == "order_rec_mock_150"

    async def test_recovery_checkout_reuses_existing_active_order(self, db_session):
        """16 & 17. Repeated checkout requests reuse active recovery order without creating new orders."""
        pe, rc = _create_test_case(db_session, amount_paise=150000)


        # Pre-seed active recovery order
        trail = dict(rc.decision_audit_trail)
        trail["recovery_order"] = {
            "order_id": "order_rec_existing_777",
            "amount_paise": 150000,
            "currency": "INR",
            "receipt": "rcpt_rec_test",
        }
        rc.decision_audit_trail = trail
        db_session.commit()

        with (
            patch.object(settings, "RAZORPAY_KEY_ID", "rzp_test_key_mock"),
            patch.object(settings, "RAZORPAY_KEY_SECRET", "secret_mock"),
            patch("razorpay.Client") as mock_client_cls,
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://testserver") as client:
                resp = await client.post(
                    f"/api/recovery-cases/{rc.id}/recovery-checkout",
                )

            # SDK client should NOT be called because order is reused
            mock_client_cls.assert_not_called()

        assert resp.status_code == 200
        data = resp.json()
        assert data["order_id"] == "order_rec_existing_777"
        assert data["is_reused"] is True

    async def test_recovery_checkout_rejects_resolved_success_case(self, db_session):
        """Recovery checkout rejects cases that are already RESOLVED_SUCCESS."""
        pe, rc = _create_test_case(
            db_session,
            status_val=RecoveryStatus.RESOLVED_SUCCESS.value,
        )

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            resp = await client.post(
                f"/api/recovery-cases/{rc.id}/recovery-checkout",
            )

        assert resp.status_code == 400
        assert "already" in resp.json()["detail"].lower()
