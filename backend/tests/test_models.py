"""Tests for SQLAlchemy model definitions, enums, and schema metadata.

These tests do NOT require a running PostgreSQL instance.
They validate model structure and metadata only.
"""

import pytest

from app.models import (
    Base,
    Customer,
    ExecutionLog,
    ExecutionMode,
    ExecutionStatus,
    FailureCategory,
    PaymentEvent,
    RecoveryCase,
    RecoveryStatus,
    RecoveryStrategy,
)


# ---------------------------------------------------------------------------
# Enum tests
# ---------------------------------------------------------------------------

class TestRecoveryStatus:
    def test_all_values(self) -> None:
        expected = {
            "RECEIVED",
            "DECISION_PENDING",
            "PENDING_EXECUTION",
            "REQUIRES_HUMAN",
            "EXECUTING",
            "RESOLVED_SUCCESS",
            "RESOLVED_FAILED",
        }
        assert {s.value for s in RecoveryStatus} == expected

    def test_string_enum(self) -> None:
        assert isinstance(RecoveryStatus.RECEIVED, str)
        assert RecoveryStatus.RECEIVED == "RECEIVED"


class TestFailureCategory:
    def test_all_values(self) -> None:
        expected = {"TRANSIENT", "AUTHENTICATION", "HARD_FAILURE", "UNKNOWN"}
        assert {c.value for c in FailureCategory} == expected


class TestRecoveryStrategy:
    def test_all_values(self) -> None:
        expected = {
            "WAIT_AND_RETRY",
            "CREATE_PAYMENT_LINK",
            "HUMAN_REVIEW",
            "STOP_RECOVERY",
        }
        assert {s.value for s in RecoveryStrategy} == expected


class TestExecutionMode:
    def test_all_values(self) -> None:
        expected = {"SIMULATION", "RAZORPAY"}
        assert {m.value for m in ExecutionMode} == expected


class TestExecutionStatus:
    def test_all_values(self) -> None:
        expected = {"PENDING", "SUCCESS", "FAILED", "BLOCKED"}
        assert {s.value for s in ExecutionStatus} == expected


# ---------------------------------------------------------------------------
# Model metadata / table structure tests
# ---------------------------------------------------------------------------

class TestCustomerModel:
    def test_table_name(self) -> None:
        assert Customer.__tablename__ == "customers"

    def test_has_required_columns(self) -> None:
        column_names = {c.name for c in Customer.__table__.columns}
        expected = {
            "id", "email", "phone", "lifetime_value_paise",
            "historical_success_rate", "created_at", "updated_at",
        }
        assert expected == column_names

    def test_pk_is_uuid(self) -> None:
        pk = Customer.__table__.c.id
        assert pk.primary_key is True


class TestPaymentEventModel:
    def test_table_name(self) -> None:
        assert PaymentEvent.__tablename__ == "payment_events"

    def test_has_required_columns(self) -> None:
        column_names = {c.name for c in PaymentEvent.__table__.columns}
        expected = {
            "id", "customer_id", "event_type", "external_event_id",
            "external_payment_id", "external_order_id", "amount_paise",
            "currency", "error_code", "error_reason", "error_description",
            "raw_payload", "payload_hash", "created_at",
        }
        assert expected == column_names

    def test_customer_id_nullable(self) -> None:
        col = PaymentEvent.__table__.c.customer_id
        assert col.nullable is True

    def test_amount_paise_not_nullable(self) -> None:
        col = PaymentEvent.__table__.c.amount_paise
        assert col.nullable is False


class TestRecoveryCaseModel:
    def test_table_name(self) -> None:
        assert RecoveryCase.__tablename__ == "recovery_cases"

    def test_has_required_columns(self) -> None:
        column_names = {c.name for c in RecoveryCase.__table__.columns}
        expected = {
            "id", "payment_event_id", "status", "failure_category",
            "recovery_probability", "priority_score", "recommended_strategy",
            "expected_value_paise", "decision_audit_trail", "next_run_at",
            "retry_count", "requires_human_approval", "approved_by_human",
            "created_at", "updated_at",
        }
        assert expected == column_names

    def test_payment_event_id_unique(self) -> None:
        unique_constraints = [
            c for c in RecoveryCase.__table__.constraints
            if c.__class__.__name__ == "UniqueConstraint"
        ]
        assert any(
            "payment_event_id" in [col.name for col in uq.columns]
            for uq in unique_constraints
        ), "payment_event_id should have a unique constraint"

    def test_has_check_constraints(self) -> None:
        check_constraints = {
            c.name for c in RecoveryCase.__table__.constraints
            if c.__class__.__name__ == "CheckConstraint"
        }
        assert "ck_recovery_cases_retry_count_non_negative" in check_constraints
        assert "ck_recovery_cases_probability_range" in check_constraints


class TestExecutionLogModel:
    def test_table_name(self) -> None:
        assert ExecutionLog.__tablename__ == "execution_logs"

    def test_has_required_columns(self) -> None:
        column_names = {c.name for c in ExecutionLog.__table__.columns}
        expected = {
            "id", "recovery_case_id", "idempotency_key", "action",
            "execution_mode", "status", "request_data", "response_data",
            "error_message", "executed_at", "created_at",
        }
        assert expected == column_names

    def test_idempotency_key_unique(self) -> None:
        unique_constraints = [
            c for c in ExecutionLog.__table__.constraints
            if c.__class__.__name__ == "UniqueConstraint"
        ]
        assert any(
            "idempotency_key" in [col.name for col in uq.columns]
            for uq in unique_constraints
        ), "idempotency_key should have a unique constraint"


# ---------------------------------------------------------------------------
# Table count sanity check
# ---------------------------------------------------------------------------

class TestRegistry:
    def test_expected_table_count(self) -> None:
        """Ensure exactly the 4 core tables are registered."""
        table_names = {
            name for name, _ in Base.metadata.tables.items()
            if name != "alembic_version"
        }
        assert table_names == {
            "customers",
            "payment_events",
            "recovery_cases",
            "execution_logs",
        }
