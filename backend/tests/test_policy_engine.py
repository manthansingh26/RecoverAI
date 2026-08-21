"""Tests for the deterministic policy engine.

Covers:
- Hard failure blocks unsafe automatic strategy
- Unknown failure requires human review
- Max retry limit blocks WAIT_AND_RETRY
- Below retry limit permits valid transient retry
- High-value CREATE_PAYMENT_LINK requires human review
- Invalid strategy fails safely
- Valid low-value authentication case can pass
- All policy decisions contain audit fields
"""

import pytest

from app.models.enums import FailureCategory, RecoveryStrategy
from app.services.policy_engine import evaluate_policy


# Defaults for tests
DEFAULT_AMOUNT = 100_000  # 1000 INR — low value
DEFAULT_RETRY = 0


class TestHardFailureRule:
    """RULE 1 — HARD_FAILURE blocks unsafe automatic strategies."""

    def test_hard_failure_blocks_wait_and_retry(self) -> None:
        decision = evaluate_policy(
            amount_paise=DEFAULT_AMOUNT,
            failure_category=FailureCategory.HARD_FAILURE,
            proposed_strategy=RecoveryStrategy.WAIT_AND_RETRY,
            recovery_probability=0.8,
            retry_count=DEFAULT_RETRY,
        )
        assert decision.approved is False
        assert decision.final_strategy == RecoveryStrategy.STOP_RECOVERY
        assert any("HARD_FAILURE" in v for v in decision.violations)

    def test_hard_failure_blocks_auto_create_payment_link(self) -> None:
        decision = evaluate_policy(
            amount_paise=DEFAULT_AMOUNT,
            failure_category=FailureCategory.HARD_FAILURE,
            proposed_strategy=RecoveryStrategy.CREATE_PAYMENT_LINK,
            recovery_probability=0.8,
            retry_count=DEFAULT_RETRY,
        )
        assert decision.approved is False
        assert decision.final_strategy == RecoveryStrategy.HUMAN_REVIEW
        assert decision.requires_human_approval is True

    def test_hard_failure_allows_stop_recovery(self) -> None:
        decision = evaluate_policy(
            amount_paise=DEFAULT_AMOUNT,
            failure_category=FailureCategory.HARD_FAILURE,
            proposed_strategy=RecoveryStrategy.STOP_RECOVERY,
            recovery_probability=0.1,
            retry_count=DEFAULT_RETRY,
        )
        assert decision.approved is True
        assert decision.final_strategy == RecoveryStrategy.STOP_RECOVERY


class TestUnknownFailureRule:
    """RULE 2 — UNKNOWN failure requires human review."""

    def test_unknown_blocks_wait_and_retry(self) -> None:
        decision = evaluate_policy(
            amount_paise=DEFAULT_AMOUNT,
            failure_category=FailureCategory.UNKNOWN,
            proposed_strategy=RecoveryStrategy.WAIT_AND_RETRY,
            recovery_probability=0.5,
            retry_count=DEFAULT_RETRY,
        )
        assert decision.approved is False
        assert decision.final_strategy == RecoveryStrategy.HUMAN_REVIEW
        assert decision.requires_human_approval is True

    def test_unknown_blocks_create_payment_link(self) -> None:
        decision = evaluate_policy(
            amount_paise=DEFAULT_AMOUNT,
            failure_category=FailureCategory.UNKNOWN,
            proposed_strategy=RecoveryStrategy.CREATE_PAYMENT_LINK,
            recovery_probability=0.5,
            retry_count=DEFAULT_RETRY,
        )
        assert decision.approved is False
        assert decision.final_strategy == RecoveryStrategy.HUMAN_REVIEW
        assert decision.requires_human_approval is True

    def test_unknown_allows_human_review(self) -> None:
        decision = evaluate_policy(
            amount_paise=DEFAULT_AMOUNT,
            failure_category=FailureCategory.UNKNOWN,
            proposed_strategy=RecoveryStrategy.HUMAN_REVIEW,
            recovery_probability=0.5,
            retry_count=DEFAULT_RETRY,
        )
        assert decision.approved is True
        assert decision.final_strategy == RecoveryStrategy.HUMAN_REVIEW


class TestMaxRetryRule:
    """RULE 3 — Max retry limit blocks WAIT_AND_RETRY."""

    def test_max_retries_blocks_wait_and_retry(self) -> None:
        from app.core.config import settings

        decision = evaluate_policy(
            amount_paise=DEFAULT_AMOUNT,
            failure_category=FailureCategory.TRANSIENT,
            proposed_strategy=RecoveryStrategy.WAIT_AND_RETRY,
            recovery_probability=0.8,
            retry_count=settings.RECOVERY_MAX_RETRIES,
        )
        assert decision.approved is False
        assert decision.final_strategy == RecoveryStrategy.STOP_RECOVERY

    def test_below_retry_limit_permits_wait_and_retry(self) -> None:
        decision = evaluate_policy(
            amount_paise=DEFAULT_AMOUNT,
            failure_category=FailureCategory.TRANSIENT,
            proposed_strategy=RecoveryStrategy.WAIT_AND_RETRY,
            recovery_probability=0.8,
            retry_count=0,
        )
        assert decision.approved is True
        assert decision.final_strategy == RecoveryStrategy.WAIT_AND_RETRY

    def test_one_below_limit_permits_retry(self) -> None:
        from app.core.config import settings

        decision = evaluate_policy(
            amount_paise=DEFAULT_AMOUNT,
            failure_category=FailureCategory.TRANSIENT,
            proposed_strategy=RecoveryStrategy.WAIT_AND_RETRY,
            recovery_probability=0.8,
            retry_count=settings.RECOVERY_MAX_RETRIES - 1,
        )
        assert decision.approved is True
        assert decision.final_strategy == RecoveryStrategy.WAIT_AND_RETRY


class TestHighValueRule:
    """RULE 4 — High-value transactions require human approval."""

    def test_high_value_create_payment_link_requires_human(self) -> None:
        from app.core.config import settings

        decision = evaluate_policy(
            amount_paise=settings.RECOVERY_HIGH_VALUE_THRESHOLD_PAISE,
            failure_category=FailureCategory.AUTHENTICATION,
            proposed_strategy=RecoveryStrategy.CREATE_PAYMENT_LINK,
            recovery_probability=0.8,
            retry_count=DEFAULT_RETRY,
        )
        assert decision.requires_human_approval is True
        # Strategy is kept but flagged for human approval
        assert decision.final_strategy == RecoveryStrategy.CREATE_PAYMENT_LINK

    def test_high_value_wait_and_retry_requires_human(self) -> None:
        from app.core.config import settings

        decision = evaluate_policy(
            amount_paise=settings.RECOVERY_HIGH_VALUE_THRESHOLD_PAISE,
            failure_category=FailureCategory.TRANSIENT,
            proposed_strategy=RecoveryStrategy.WAIT_AND_RETRY,
            recovery_probability=0.8,
            retry_count=DEFAULT_RETRY,
        )
        assert decision.requires_human_approval is True

    def test_low_value_no_extra_human_requirement(self) -> None:
        decision = evaluate_policy(
            amount_paise=DEFAULT_AMOUNT,
            failure_category=FailureCategory.AUTHENTICATION,
            proposed_strategy=RecoveryStrategy.CREATE_PAYMENT_LINK,
            recovery_probability=0.8,
            retry_count=DEFAULT_RETRY,
        )
        # Low value, valid strategy, no violations
        assert decision.requires_human_approval is False
        assert decision.approved is True


class TestStrategyAllowlist:
    """RULE 5 — Invalid strategies fail safely."""

    def test_invalid_strategy_fails_to_human_review(self) -> None:
        decision = evaluate_policy(
            amount_paise=DEFAULT_AMOUNT,
            failure_category=FailureCategory.TRANSIENT,
            proposed_strategy="BOGUS_STRATEGY",  # type: ignore[arg-type]
            recovery_probability=0.8,
            retry_count=DEFAULT_RETRY,
        )
        assert decision.approved is False
        assert decision.final_strategy == RecoveryStrategy.HUMAN_REVIEW
        assert decision.requires_human_approval is True
        assert any("Invalid strategy" in v for v in decision.violations)


class TestValidLowValueAuthCase:
    """Valid low-value authentication case should pass policy."""

    def test_valid_low_value_auth_passes(self) -> None:
        decision = evaluate_policy(
            amount_paise=50_000,
            failure_category=FailureCategory.AUTHENTICATION,
            proposed_strategy=RecoveryStrategy.CREATE_PAYMENT_LINK,
            recovery_probability=0.7,
            retry_count=0,
        )
        assert decision.approved is True
        assert decision.final_strategy == RecoveryStrategy.CREATE_PAYMENT_LINK
        assert decision.requires_human_approval is False
        assert len(decision.violations) == 0


class TestAuditFields:
    """All policy decisions must contain audit fields."""

    def test_all_decisions_have_required_fields(self) -> None:
        test_cases = [
            (FailureCategory.TRANSIENT, RecoveryStrategy.WAIT_AND_RETRY),
            (FailureCategory.AUTHENTICATION, RecoveryStrategy.CREATE_PAYMENT_LINK),
            (FailureCategory.HARD_FAILURE, RecoveryStrategy.STOP_RECOVERY),
            (FailureCategory.UNKNOWN, RecoveryStrategy.HUMAN_REVIEW),
        ]
        for category, strategy in test_cases:
            decision = evaluate_policy(
                amount_paise=DEFAULT_AMOUNT,
                failure_category=category,
                proposed_strategy=strategy,
                recovery_probability=0.5,
                retry_count=DEFAULT_RETRY,
            )
            assert hasattr(decision, "approved")
            assert hasattr(decision, "final_strategy")
            assert hasattr(decision, "requires_human_approval")
            assert hasattr(decision, "violations")
            assert hasattr(decision, "applied_rules")
            assert hasattr(decision, "policy_reason")
            assert isinstance(decision.applied_rules, list)
            assert len(decision.applied_rules) >= 5  # All5 rules applied
            assert isinstance(decision.policy_reason, str)
            assert len(decision.policy_reason) > 0
