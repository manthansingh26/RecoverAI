"""Tests for the Decision Engine — complete recovery decision flows.

Covers:
1. TRANSIENT: RECEIVED -> WAIT_AND_RETRY -> PENDING_EXECUTION + next_run_at
2. AUTHENTICATION low-value: RECEIVED -> CREATE_PAYMENT_LINK -> PENDING_EXECUTION
3. HARD_FAILURE: RECEIVED -> STOP_RECOVERY/HUMAN_REVIEW -> safe terminal state
4. UNKNOWN: RECEIVED -> HUMAN_REVIEW -> REQUIRES_HUMAN
5. Max retries: retry_count == limit -> STOP_RECOVERY
6. High-value authentication: CREATE_PAYMENT_LINK -> REQUIRES_HUMAN
7. Audit trail: ingestion data preserved, classification/recommendation/policy added
"""

import uuid
from datetime import datetime, timezone

import pytest

from app.core.config import settings
from app.models.enums import FailureCategory, RecoveryStatus, RecoveryStrategy
from app.models.payment_event import PaymentEvent
from app.models.recovery_case import RecoveryCase
from app.services.decision_engine import run_decision_engine


def _create_test_payment_event(db, *, error_reason: str = "bank_technical_error", amount_paise: int = 100_000) -> PaymentEvent:
    """Create a test PaymentEvent in the database."""
    pe = PaymentEvent(
        event_type="payment.failed",
        external_event_id=f"evt_test_{uuid.uuid4().hex[:12]}",
        external_payment_id=f"pay_test_{uuid.uuid4().hex[:12]}",
        external_order_id=f"order_test_{uuid.uuid4().hex[:12]}",
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


def _create_test_recovery_case(db, payment_event: PaymentEvent, *, retry_count: int = 0) -> RecoveryCase:
    """Create a test RecoveryCase in RECEIVED state."""
    rc = RecoveryCase(
        payment_event_id=payment_event.id,
        status=RecoveryStatus.RECEIVED.value,
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
    )
    db.add(rc)
    db.flush()
    return rc


class TestTransientFlow:
    """Flow 1: TRANSIENT -> WAIT_AND_RETRY -> PENDING_EXECUTION"""

    def test_transient_classification_and_retry(self, db_session) -> None:
        pe = _create_test_payment_event(db_session, error_reason="bank_technical_error")
        rc = _create_test_recovery_case(db_session, pe)
        db_session.commit()

        result = run_decision_engine(db_session, str(rc.id))

        assert result is not None
        assert result.status == RecoveryStatus.PENDING_EXECUTION.value
        assert result.recommended_strategy == RecoveryStrategy.WAIT_AND_RETRY.value
        assert result.failure_category == FailureCategory.TRANSIENT.value
        assert result.next_run_at is not None
        assert result.next_run_at > datetime.now(timezone.utc)
        assert result.requires_human_approval is False
        assert result.recovery_probability is not None
        assert result.priority_score is not None


class TestAuthenticationLowValueFlow:
    """Flow 2: AUTHENTICATION low-value -> CREATE_PAYMENT_LINK -> PENDING_EXECUTION"""

    def test_auth_low_value_creates_payment_link(self, db_session) -> None:
        pe = _create_test_payment_event(
            db_session, error_reason="authentication_failed", amount_paise=50_000
        )
        rc = _create_test_recovery_case(db_session, pe)
        db_session.commit()

        result = run_decision_engine(db_session, str(rc.id))

        assert result is not None
        assert result.status == RecoveryStatus.PENDING_EXECUTION.value
        assert result.recommended_strategy == RecoveryStrategy.CREATE_PAYMENT_LINK.value
        assert result.failure_category == FailureCategory.AUTHENTICATION.value
        assert result.next_run_at is not None
        # next_run_at should be approximately now (within a few seconds)
        now = datetime.now(timezone.utc)
        diff = abs((result.next_run_at - now).total_seconds())
        assert diff < 5


class TestHardFailureFlow:
    """Flow 3: HARD_FAILURE -> STOP_RECOVERY or HUMAN_REVIEW"""

    def test_hard_failure_stops_recovery(self, db_session) -> None:
        pe = _create_test_payment_event(
            db_session, error_reason="debit_instrument_blocked"
        )
        rc = _create_test_recovery_case(db_session, pe)
        db_session.commit()

        result = run_decision_engine(db_session, str(rc.id))

        assert result is not None
        assert result.status == RecoveryStatus.RESOLVED_FAILED.value
        assert result.recommended_strategy == RecoveryStrategy.STOP_RECOVERY.value
        assert result.failure_category == FailureCategory.HARD_FAILURE.value
        assert result.next_run_at is None
        assert result.requires_human_approval is False

    def test_hard_failure_with_auto_proposal_goes_to_human(self, db_session) -> None:
        """If the advisor proposes CREATE_PAYMENT_LINK for HARD_FAILURE, policy blocks it."""
        pe = _create_test_payment_event(
            db_session, error_reason="beneficiary_account_dormant"
        )
        rc = _create_test_recovery_case(db_session, pe)
        db_session.commit()

        result = run_decision_engine(db_session, str(rc.id))

        assert result is not None
        # Policy should have overridden to HUMAN_REVIEW or STOP_RECOVERY
        assert result.status in (
            RecoveryStatus.REQUIRES_HUMAN.value,
            RecoveryStatus.RESOLVED_FAILED.value,
        )


class TestUnknownFlow:
    """Flow 4: UNKNOWN -> HUMAN_REVIEW -> REQUIRES_HUMAN"""

    def test_unknown_goes_to_human_review(self, db_session) -> None:
        pe = _create_test_payment_event(
            db_session, error_reason="some_totally_unknown_reason"
        )
        rc = _create_test_recovery_case(db_session, pe)
        db_session.commit()

        result = run_decision_engine(db_session, str(rc.id))

        assert result is not None
        assert result.status == RecoveryStatus.REQUIRES_HUMAN.value
        assert result.recommended_strategy == RecoveryStrategy.HUMAN_REVIEW.value
        assert result.failure_category == FailureCategory.UNKNOWN.value
        assert result.requires_human_approval is True
        assert result.next_run_at is None

    def test_unknown_none_reason_goes_to_human_review(self, db_session) -> None:
        pe = _create_test_payment_event(db_session, error_reason=None)
        rc = _create_test_recovery_case(db_session, pe)
        db_session.commit()

        result = run_decision_engine(db_session, str(rc.id))

        assert result is not None
        assert result.status == RecoveryStatus.REQUIRES_HUMAN.value
        assert result.requires_human_approval is True


class TestMaxRetriesFlow:
    """Flow 5: retry_count == RECOVERY_MAX_RETRIES -> STOP_RECOVERY"""

    def test_max_retries_blocks_retry(self, db_session) -> None:
        pe = _create_test_payment_event(
            db_session, error_reason="bank_technical_error"
        )
        rc = _create_test_recovery_case(
            db_session, pe, retry_count=settings.RECOVERY_MAX_RETRIES
        )
        db_session.commit()

        result = run_decision_engine(db_session, str(rc.id))

        assert result is not None
        assert result.status == RecoveryStatus.RESOLVED_FAILED.value
        assert result.recommended_strategy == RecoveryStrategy.STOP_RECOVERY.value

    def test_below_max_retries_allows_retry(self, db_session) -> None:
        pe = _create_test_payment_event(
            db_session, error_reason="bank_technical_error"
        )
        rc = _create_test_recovery_case(
            db_session, pe, retry_count=settings.RECOVERY_MAX_RETRIES - 1
        )
        db_session.commit()

        result = run_decision_engine(db_session, str(rc.id))

        assert result is not None
        assert result.status == RecoveryStatus.PENDING_EXECUTION.value
        assert result.recommended_strategy == RecoveryStrategy.WAIT_AND_RETRY.value


class TestHighValueAuthFlow:
    """Flow 6: High-value authentication -> CREATE_PAYMENT_LINK -> REQUIRES_HUMAN"""

    def test_high_value_auth_requires_human(self, db_session) -> None:
        pe = _create_test_payment_event(
            db_session,
            error_reason="authentication_failed",
            amount_paise=settings.RECOVERY_HIGH_VALUE_THRESHOLD_PAISE,
        )
        rc = _create_test_recovery_case(db_session, pe)
        db_session.commit()

        result = run_decision_engine(db_session, str(rc.id))

        assert result is not None
        assert result.requires_human_approval is True
        # Strategy may be kept as CREATE_PAYMENT_LINK but status requires human
        assert result.status == RecoveryStatus.REQUIRES_HUMAN.value


class TestAuditTrail:
    """Flow 7: Audit trail preserves ingestion and adds decision data."""

    def test_ingestion_data_preserved(self, db_session) -> None:
        pe = _create_test_payment_event(
            db_session, error_reason="bank_technical_error"
        )
        rc = _create_test_recovery_case(db_session, pe)
        db_session.commit()

        result = run_decision_engine(db_session, str(rc.id))

        assert result is not None
        trail = result.decision_audit_trail
        # Ingestion preserved
        assert "ingestion" in trail
        assert trail["ingestion"]["source"] == "test"
        assert trail["ingestion"]["event_id"] == pe.external_event_id

    def test_classification_added(self, db_session) -> None:
        pe = _create_test_payment_event(
            db_session, error_reason="bank_technical_error"
        )
        rc = _create_test_recovery_case(db_session, pe)
        db_session.commit()

        result = run_decision_engine(db_session, str(rc.id))

        assert result is not None
        trail = result.decision_audit_trail
        assert "classification" in trail
        assert trail["classification"]["category"] == "TRANSIENT"
        assert "confidence" in trail["classification"]
        assert "rule_id" in trail["classification"]

    def test_recommendation_added(self, db_session) -> None:
        pe = _create_test_payment_event(
            db_session, error_reason="bank_technical_error"
        )
        rc = _create_test_recovery_case(db_session, pe)
        db_session.commit()

        result = run_decision_engine(db_session, str(rc.id))

        assert result is not None
        trail = result.decision_audit_trail
        assert "recommendation" in trail
        assert trail["recommendation"]["strategy"] == "WAIT_AND_RETRY"
        assert trail["recommendation"]["provider"] == "deterministic"
        assert isinstance(trail["recommendation"]["risk_flags"], list)

    def test_policy_added(self, db_session) -> None:
        pe = _create_test_payment_event(
            db_session, error_reason="bank_technical_error"
        )
        rc = _create_test_recovery_case(db_session, pe)
        db_session.commit()

        result = run_decision_engine(db_session, str(rc.id))

        assert result is not None
        trail = result.decision_audit_trail
        assert "policy" in trail
        assert "approved" in trail["policy"]
        assert "final_strategy" in trail["policy"]
        assert "violations" in trail["policy"]
        assert "applied_rules" in trail["policy"]

    def test_full_audit_trail_structure(self, db_session) -> None:
        pe = _create_test_payment_event(
            db_session, error_reason="bank_technical_error"
        )
        rc = _create_test_recovery_case(db_session, pe)
        db_session.commit()

        result = run_decision_engine(db_session, str(rc.id))

        assert result is not None
        trail = result.decision_audit_trail
        expected_keys = {"ingestion", "classification", "recommendation", "policy"}
        assert set(trail.keys()) == expected_keys


class TestDecisionEngineEdgeCases:
    """Edge cases and error handling."""

    def test_invalid_uuid_returns_none(self, db_session) -> None:
        result = run_decision_engine(db_session, "not-a-uuid")
        assert result is None

    def test_nonexistent_case_returns_none(self, db_session) -> None:
        fake_id = str(uuid.uuid4())
        result = run_decision_engine(db_session, fake_id)
        assert result is None

    def test_non_received_status_skipped(self, db_session) -> None:
        pe = _create_test_payment_event(db_session)
        rc = _create_test_recovery_case(db_session, pe)
        # Set to non-RECEIVED status
        rc.status = RecoveryStatus.PENDING_EXECUTION.value
        db_session.commit()

        result = run_decision_engine(db_session, str(rc.id))

        assert result is not None
        # Status should remain unchanged
        assert result.status == RecoveryStatus.PENDING_EXECUTION.value
