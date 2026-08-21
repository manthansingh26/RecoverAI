"""Tests for the scenario-driven simulation endpoint (Milestone 8).

Verifies:
- Endpoint accessibility in dev/test vs production environments
- Each of 4 scenarios produces valid results through the real pipeline
- Ingestion, decision engine, and policy engine are actually invoked
- Human approval is never bypassed for high-value scenarios
- Permanent failure follows existing STOP_RECOVERY logic
- Duplicate/idempotency safety
- Input validation
"""

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.config import settings
from app.main import app
from app.models.enums import FailureCategory, RecoveryStatus, RecoveryStrategy
from app.models.recovery_case import RecoveryCase
from app.schemas.simulation import SimulationScenario

client = TestClient(app)


# ---------------------------------------------------------------------------
# Environment access tests
# ---------------------------------------------------------------------------


class TestEnvironmentAccess:
    """Verify endpoint is only available in dev/test environments."""

    def test_endpoint_available_in_development(self, db_session: Session) -> None:
        """Scenario endpoint should work in development environment."""
        with patch.object(settings, "APP_ENV", "development"):
            response = client.post(
                "/api/dev/simulate-payment-failure",
                json={"scenario": "LOW_VALUE_TRANSIENT"},
            )
        assert response.status_code == 200

    def test_endpoint_available_in_test(self, db_session: Session) -> None:
        """Scenario endpoint should work in test environment."""
        with patch.object(settings, "APP_ENV", "test"):
            response = client.post(
                "/api/dev/simulate-payment-failure",
                json={"scenario": "LOW_VALUE_TRANSIENT"},
            )
        assert response.status_code == 200

    def test_endpoint_blocked_in_production(self, db_session: Session) -> None:
        """Scenario endpoint must return 404 in production."""
        with patch.object(settings, "APP_ENV", "production"):
            response = client.post(
                "/api/dev/simulate-payment-failure",
                json={"scenario": "LOW_VALUE_TRANSIENT"},
            )
        assert response.status_code == 404
        assert "not available" in response.json()["detail"].lower()

    def test_endpoint_blocked_in_staging(self, db_session: Session) -> None:
        """Scenario endpoint must return 404 in staging."""
        with patch.object(settings, "APP_ENV", "staging"):
            response = client.post(
                "/api/dev/simulate-payment-failure",
                json={"scenario": "LOW_VALUE_TRANSIENT"},
            )
        assert response.status_code == 404


# ---------------------------------------------------------------------------
# Input validation tests
# ---------------------------------------------------------------------------


class TestInputValidation:
    """Verify request validation and error handling."""

    def test_invalid_scenario_returns_422(self, db_session: Session) -> None:
        """Unknown scenario value should be rejected."""
        response = client.post(
            "/api/dev/simulate-payment-failure",
            json={"scenario": "NONEXISTENT_SCENARIO"},
        )
        assert response.status_code == 422

    def test_missing_scenario_returns_422(self, db_session: Session) -> None:
        """Request without scenario field should be rejected."""
        response = client.post(
            "/api/dev/simulate-payment-failure",
            json={},
        )
        assert response.status_code == 422

    def test_empty_body_returns_422(self, db_session: Session) -> None:
        """Request with no body should be rejected."""
        response = client.post(
            "/api/dev/simulate-payment-failure",
        )
        assert response.status_code == 422


# ---------------------------------------------------------------------------
# Scenario: LOW_VALUE_TRANSIENT
# ---------------------------------------------------------------------------


class TestLowValueTransient:
    """Verify LOW_VALUE_TRANSIENT scenario end-to-end."""

    def test_creates_payment_event_and_recovery_case(self, db_session: Session) -> None:
        """Should create both PaymentEvent and RecoveryCase."""
        response = client.post(
            "/api/dev/simulate-payment-failure",
            json={"scenario": "LOW_VALUE_TRANSIENT"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["scenario"] == "LOW_VALUE_TRANSIENT"
        assert data["recovery_case_id"] is not None
        assert data["payment_id"].startswith("pay_sim_")
        assert data["duplicate"] is False

    def test_uses_existing_pipeline_classification(self, db_session: Session) -> None:
        """Should be classified as TRANSIENT by existing failure classifier."""
        response = client.post(
            "/api/dev/simulate-payment-failure",
            json={"scenario": "LOW_VALUE_TRANSIENT"},
        )
        data = response.json()
        assert data["failure_category"] == FailureCategory.TRANSIENT.value

    def test_amount_is_low_value(self, db_session: Session) -> None:
        """Amount should be below the high-value threshold."""
        response = client.post(
            "/api/dev/simulate-payment-failure",
            json={"scenario": "LOW_VALUE_TRANSIENT"},
        )
        data = response.json()
        assert data["amount_paise"] < settings.RECOVERY_HIGH_VALUE_THRESHOLD_PAISE

    def test_does_not_require_human_approval(self, db_session: Session) -> None:
        """Low-value transient should not require human approval."""
        response = client.post(
            "/api/dev/simulate-payment-failure",
            json={"scenario": "LOW_VALUE_TRANSIENT"},
        )
        data = response.json()
        assert data["requires_human_approval"] is False

    def test_has_workflow_result(self, db_session: Session) -> None:
        """Workflow should have processed the case."""
        response = client.post(
            "/api/dev/simulate-payment-failure",
            json={"scenario": "LOW_VALUE_TRANSIENT"},
        )
        data = response.json()
        assert data["workflow"] is not None
        assert data["workflow"]["processed"] is True

    def test_strategy_from_existing_engine(self, db_session: Session) -> None:
        """Strategy should be determined by existing decision engine, not hardcoded."""
        response = client.post(
            "/api/dev/simulate-payment-failure",
            json={"scenario": "LOW_VALUE_TRANSIENT"},
        )
        data = response.json()
        # The existing engine should assign WAIT_AND_RETRY for TRANSIENT
        assert data["recommended_strategy"] is not None
        assert data["recommended_strategy"] in [s.value for s in RecoveryStrategy]


# ---------------------------------------------------------------------------
# Scenario: MEDIUM_VALUE_RECOVERABLE
# ---------------------------------------------------------------------------


class TestMediumValueRecoverable:
    """Verify MEDIUM_VALUE_RECOVERABLE scenario end-to-end."""

    def test_creates_valid_case(self, db_session: Session) -> None:
        """Should create payment event and recovery case."""
        response = client.post(
            "/api/dev/simulate-payment-failure",
            json={"scenario": "MEDIUM_VALUE_RECOVERABLE"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["recovery_case_id"] is not None

    def test_classified_as_authentication(self, db_session: Session) -> None:
        """Should be classified as AUTHENTICATION by existing classifier."""
        response = client.post(
            "/api/dev/simulate-payment-failure",
            json={"scenario": "MEDIUM_VALUE_RECOVERABLE"},
        )
        data = response.json()
        assert data["failure_category"] == FailureCategory.AUTHENTICATION.value

    def test_amount_is_medium(self, db_session: Session) -> None:
        """Amount should be moderate, below high-value threshold."""
        response = client.post(
            "/api/dev/simulate-payment-failure",
            json={"scenario": "MEDIUM_VALUE_RECOVERABLE"},
        )
        data = response.json()
        assert data["amount_paise"] > 0
        assert data["amount_paise"] < settings.RECOVERY_HIGH_VALUE_THRESHOLD_PAISE

    def test_has_valid_strategy(self, db_session: Session) -> None:
        """Should get a valid strategy from the existing pipeline."""
        response = client.post(
            "/api/dev/simulate-payment-failure",
            json={"scenario": "MEDIUM_VALUE_RECOVERABLE"},
        )
        data = response.json()
        assert data["recommended_strategy"] is not None


# ---------------------------------------------------------------------------
# Scenario: HIGH_VALUE_HUMAN_REVIEW
# ---------------------------------------------------------------------------


class TestHighValueHumanReview:
    """Verify HIGH_VALUE_HUMAN_REVIEW scenario triggers human review."""

    def test_creates_valid_case(self, db_session: Session) -> None:
        """Should create payment event and recovery case."""
        response = client.post(
            "/api/dev/simulate-payment-failure",
            json={"scenario": "HIGH_VALUE_HUMAN_REVIEW"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["recovery_case_id"] is not None

    def test_exceeds_configured_threshold(self, db_session: Session) -> None:
        """Amount MUST exceed the actual configured high-value threshold."""
        response = client.post(
            "/api/dev/simulate-payment-failure",
            json={"scenario": "HIGH_VALUE_HUMAN_REVIEW"},
        )
        data = response.json()
        assert data["amount_paise"] >= settings.RECOVERY_HIGH_VALUE_THRESHOLD_PAISE

    def test_requires_human_approval(self, db_session: Session) -> None:
        """Policy engine should flag this for human review."""
        response = client.post(
            "/api/dev/simulate-payment-failure",
            json={"scenario": "HIGH_VALUE_HUMAN_REVIEW"},
        )
        data = response.json()
        assert data["requires_human_approval"] is True

    def test_status_requires_human(self, db_session: Session) -> None:
        """Case status should be REQUIRES_HUMAN, not auto-executed."""
        response = client.post(
            "/api/dev/simulate-payment-failure",
            json={"scenario": "HIGH_VALUE_HUMAN_REVIEW"},
        )
        data = response.json()
        assert data["status"] == RecoveryStatus.REQUIRES_HUMAN.value

    def test_not_auto_executed(self, db_session: Session) -> None:
        """Human review cases must NOT be auto-executed."""
        response = client.post(
            "/api/dev/simulate-payment-failure",
            json={"scenario": "HIGH_VALUE_HUMAN_REVIEW"},
        )
        data = response.json()
        assert data["execution_result"] is None

    def test_human_approval_not_bypassed(self, db_session: Session) -> None:
        """approved_by_human should still be None (not pre-approved)."""
        response = client.post(
            "/api/dev/simulate-payment-failure",
            json={"scenario": "HIGH_VALUE_HUMAN_REVIEW"},
        )
        data = response.json()
        assert data["approved_by_human"] is None


# ---------------------------------------------------------------------------
# Scenario: PERMANENT_FAILURE
# ---------------------------------------------------------------------------


class TestPermanentFailure:
    """Verify PERMANENT_FAILURE scenario results in safe stop."""

    def test_creates_valid_case(self, db_session: Session) -> None:
        """Should create payment event and recovery case."""
        response = client.post(
            "/api/dev/simulate-payment-failure",
            json={"scenario": "PERMANENT_FAILURE"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["recovery_case_id"] is not None

    def test_classified_as_hard_failure(self, db_session: Session) -> None:
        """Should be classified as HARD_FAILURE by existing classifier."""
        response = client.post(
            "/api/dev/simulate-payment-failure",
            json={"scenario": "PERMANENT_FAILURE"},
        )
        data = response.json()
        assert data["failure_category"] == FailureCategory.HARD_FAILURE.value

    def test_strategy_is_stop_recovery(self, db_session: Session) -> None:
        """Policy engine should select STOP_RECOVERY for hard failures."""
        response = client.post(
            "/api/dev/simulate-payment-failure",
            json={"scenario": "PERMANENT_FAILURE"},
        )
        data = response.json()
        assert data["recommended_strategy"] == RecoveryStrategy.STOP_RECOVERY.value

    def test_status_resolved_failed(self, db_session: Session) -> None:
        """Case should be immediately resolved as failed."""
        response = client.post(
            "/api/dev/simulate-payment-failure",
            json={"scenario": "PERMANENT_FAILURE"},
        )
        data = response.json()
        assert data["status"] == RecoveryStatus.RESOLVED_FAILED.value

    def test_not_executed(self, db_session: Session) -> None:
        """Permanent failure should NOT trigger execution."""
        response = client.post(
            "/api/dev/simulate-payment-failure",
            json={"scenario": "PERMANENT_FAILURE"},
        )
        data = response.json()
        # RESOLVED_FAILED means no execution was attempted
        assert data["execution_result"] is None


# ---------------------------------------------------------------------------
# Pipeline integration tests
# ---------------------------------------------------------------------------


class TestPipelineIntegration:
    """Verify the endpoint actually reuses existing services."""

    def test_ingestion_creates_payment_event_in_db(self, db_session: Session) -> None:
        """Ingestion service should persist a PaymentEvent in the database."""
        from app.models.payment_event import PaymentEvent

        response = client.post(
            "/api/dev/simulate-payment-failure",
            json={"scenario": "LOW_VALUE_TRANSIENT"},
        )
        data = response.json()
        event_id = data["event_id"]

        pe = db_session.query(PaymentEvent).filter(
            PaymentEvent.external_event_id == event_id
        ).first()
        assert pe is not None
        assert pe.amount_paise == data["amount_paise"]

    def test_recovery_case_exists_in_db(self, db_session: Session) -> None:
        """RecoveryCase should exist in the database after simulation."""
        import uuid as uuid_module
        response = client.post(
            "/api/dev/simulate-payment-failure",
            json={"scenario": "MEDIUM_VALUE_RECOVERABLE"},
        )
        data = response.json()
        rc_id = uuid_module.UUID(data["recovery_case_id"])

        rc = db_session.get(RecoveryCase, rc_id)
        assert rc is not None
        assert rc.failure_category != FailureCategory.UNKNOWN.value  # Decision engine ran

    def test_decision_audit_trail_populated(self, db_session: Session) -> None:
        """Decision engine should populate the audit trail."""
        import uuid as uuid_module
        response = client.post(
            "/api/dev/simulate-payment-failure",
            json={"scenario": "LOW_VALUE_TRANSIENT"},
        )
        data = response.json()
        rc_id = uuid_module.UUID(data["recovery_case_id"])

        rc = db_session.get(RecoveryCase, rc_id)
        assert rc is not None
        trail = rc.decision_audit_trail
        # Should have ingestion + classification + recommendation + policy sections
        assert "ingestion" in trail
        assert "classification" in trail
        assert "recommendation" in trail
        assert "policy" in trail

    def test_recovery_probability_set(self, db_session: Session) -> None:
        """Decision engine should set recovery probability."""
        response = client.post(
            "/api/dev/simulate-payment-failure",
            json={"scenario": "LOW_VALUE_TRANSIENT"},
        )
        data = response.json()
        assert data["recovery_probability"] is not None
        assert 0 <= data["recovery_probability"] <= 1

    def test_existing_raw_endpoint_still_works(self, db_session: Session) -> None:
        """The original simulation endpoint must continue to function."""
        response = client.post(
            "/api/dev/simulate/payment-failed",
            json={"amount_paise": 100000},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["accepted"] is True


# ---------------------------------------------------------------------------
# Safety tests
# ---------------------------------------------------------------------------


class TestSafetyGuarantees:
    """Verify safety guarantees are maintained."""

    def test_no_real_execution_mode(self, db_session: Session) -> None:
        """If execution occurs, it must be in SIMULATION mode.

        Uses MEDIUM_VALUE_RECOVERABLE (CREATE_PAYMENT_LINK) which sets
        next_run_at to now() and should be immediately executable.
        WAIT_AND_RETRY sets next_run_at 30min in the future, so it
        correctly won't execute immediately.
        """
        response = client.post(
            "/api/dev/simulate-payment-failure",
            json={"scenario": "MEDIUM_VALUE_RECOVERABLE"},
        )
        data = response.json()
        if data["execution_result"] is not None:
            assert data["execution_result"]["execution_mode"] == "SIMULATION"

    def test_unique_event_ids_per_call(self, db_session: Session) -> None:
        """Each simulation call should generate unique event IDs."""
        r1 = client.post(
            "/api/dev/simulate-payment-failure",
            json={"scenario": "LOW_VALUE_TRANSIENT"},
        )
        r2 = client.post(
            "/api/dev/simulate-payment-failure",
            json={"scenario": "LOW_VALUE_TRANSIENT"},
        )
        assert r1.json()["event_id"] != r2.json()["event_id"]
        assert r1.json()["payment_id"] != r2.json()["payment_id"]
        assert r1.json()["recovery_case_id"] != r2.json()["recovery_case_id"]

    def test_all_four_scenarios_are_valid(self, db_session: Session) -> None:
        """All scenario enum values should produce valid responses."""
        for scenario in SimulationScenario:
            response = client.post(
                "/api/dev/simulate-payment-failure",
                json={"scenario": scenario.value},
            )
            assert response.status_code == 200, f"Failed for {scenario.value}"
            data = response.json()
            assert data["success"] is True, f"Failed for {scenario.value}"
            assert data["recovery_case_id"] is not None, f"No case ID for {scenario.value}"

    def test_message_is_present(self, db_session: Session) -> None:
        """Human-readable message should be present."""
        response = client.post(
            "/api/dev/simulate-payment-failure",
            json={"scenario": "LOW_VALUE_TRANSIENT"},
        )
        data = response.json()
        assert data["message"]
        assert len(data["message"]) > 10

    def test_case_visible_in_cases_api(self, db_session: Session) -> None:
        """Newly created case should be visible in the recovery cases list API."""
        sim_response = client.post(
            "/api/dev/simulate-payment-failure",
            json={"scenario": "LOW_VALUE_TRANSIENT"},
        )
        recovery_case_id = sim_response.json()["recovery_case_id"]

        list_response = client.get("/api/recovery-cases")
        assert list_response.status_code == 200
        items = list_response.json()["items"]
        case_ids = [item["recovery_case_id"] for item in items]
        assert recovery_case_id in case_ids

    def test_case_detail_api_works(self, db_session: Session) -> None:
        """Newly created case should be viewable in the detail API."""
        sim_response = client.post(
            "/api/dev/simulate-payment-failure",
            json={"scenario": "HIGH_VALUE_HUMAN_REVIEW"},
        )
        recovery_case_id = sim_response.json()["recovery_case_id"]

        detail_response = client.get(f"/api/recovery-cases/{recovery_case_id}")
        assert detail_response.status_code == 200
        detail = detail_response.json()
        assert detail["recovery_case_id"] == recovery_case_id
        assert detail["requires_human_approval"] is True

    def test_dashboard_reflects_new_case(self, db_session: Session) -> None:
        """Dashboard summary should count the newly created case."""
        client.post(
            "/api/dev/simulate-payment-failure",
            json={"scenario": "LOW_VALUE_TRANSIENT"},
        )

        dashboard_response = client.get("/api/dashboard/summary")
        assert dashboard_response.status_code == 200
        summary = dashboard_response.json()
        assert summary["total_cases"] >= 1
