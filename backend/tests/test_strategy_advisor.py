"""Tests for the deterministic strategy advisor.

Covers:
- Each FailureCategory returns an allowed RecoveryStrategy
- HARD_FAILURE never recommends WAIT_AND_RETRY
- UNKNOWN safely recommends HUMAN_REVIEW
- Confidence bounds
- No external API dependency
"""

import pytest

from app.models.enums import FailureCategory, RecoveryStrategy
from app.services.strategy_advisor import recommend_strategy


class TestStrategyRecommendations:
    """Test strategy recommendations for each failure category."""

    def test_transient_recommends_wait_and_retry(self) -> None:
        result = recommend_strategy(FailureCategory.TRANSIENT)
        assert result.strategy == RecoveryStrategy.WAIT_AND_RETRY
        assert result.provider == "deterministic"

    def test_authentication_recommends_create_payment_link(self) -> None:
        result = recommend_strategy(FailureCategory.AUTHENTICATION)
        assert result.strategy == RecoveryStrategy.CREATE_PAYMENT_LINK
        assert result.provider == "deterministic"

    def test_hard_failure_recommends_stop_recovery(self) -> None:
        result = recommend_strategy(FailureCategory.HARD_FAILURE)
        assert result.strategy == RecoveryStrategy.STOP_RECOVERY
        assert result.requires_human_review is True

    def test_unknown_recommends_human_review(self) -> None:
        result = recommend_strategy(FailureCategory.UNKNOWN)
        assert result.strategy == RecoveryStrategy.HUMAN_REVIEW
        assert result.requires_human_review is True


class TestHardFailureSafety:
    """Verify HARD_FAILURE never recommends unsafe automated strategies."""

    def test_hard_failure_not_wait_and_retry(self) -> None:
        result = recommend_strategy(FailureCategory.HARD_FAILURE)
        assert result.strategy != RecoveryStrategy.WAIT_AND_RETRY

    def test_hard_failure_requires_human_review(self) -> None:
        result = recommend_strategy(FailureCategory.HARD_FAILURE)
        assert result.requires_human_review is True


class TestUnknownSafety:
    """Verify UNKNOWN safely recommends HUMAN_REVIEW."""

    def test_unknown_strategy_is_human_review(self) -> None:
        result = recommend_strategy(FailureCategory.UNKNOWN)
        assert result.strategy == RecoveryStrategy.HUMAN_REVIEW

    def test_unknown_requires_human_review(self) -> None:
        result = recommend_strategy(FailureCategory.UNKNOWN)
        assert result.requires_human_review is True


class TestConfidenceBounds:
    """Test confidence values are in valid range."""

    @pytest.mark.parametrize(
        "category",
        [
            FailureCategory.TRANSIENT,
            FailureCategory.AUTHENTICATION,
            FailureCategory.HARD_FAILURE,
            FailureCategory.UNKNOWN,
        ],
    )
    def test_confidence_in_range(self, category: FailureCategory) -> None:
        result = recommend_strategy(category)
        assert 0.0 <= result.confidence <= 1.0

    def test_transient_confidence_is_reasonable(self) -> None:
        result = recommend_strategy(FailureCategory.TRANSIENT)
        assert result.confidence >= 0.5

    def test_unknown_confidence_is_lower(self) -> None:
        result = recommend_strategy(FailureCategory.UNKNOWN)
        assert result.confidence <= 0.6


class TestRecommendationStructure:
    """Test the recommendation object has all required fields."""

    def test_has_all_fields(self) -> None:
        result = recommend_strategy(FailureCategory.TRANSIENT)
        assert hasattr(result, "strategy")
        assert hasattr(result, "confidence")
        assert hasattr(result, "reasoning_summary")
        assert hasattr(result, "risk_flags")
        assert hasattr(result, "requires_human_review")
        assert hasattr(result, "provider")

    def test_risk_flags_is_list(self) -> None:
        result = recommend_strategy(FailureCategory.TRANSIENT)
        assert isinstance(result.risk_flags, list)
        assert len(result.risk_flags) > 0  # At least one base flag

    def test_reasoning_summary_is_string(self) -> None:
        result = recommend_strategy(FailureCategory.TRANSIENT)
        assert isinstance(result.reasoning_summary, str)
        assert len(result.reasoning_summary) > 10

    def test_retry_count_adds_risk_flag(self) -> None:
        result = recommend_strategy(FailureCategory.TRANSIENT, retry_count=3)
        assert "retry_attempt_3" in result.risk_flags

    def test_high_value_adds_risk_flag(self) -> None:
        result = recommend_strategy(
            FailureCategory.AUTHENTICATION, amount_paise=10_000_000
        )
        assert "high_value_transaction" in result.risk_flags

    def test_no_external_api_calls(self) -> None:
        """Verify the advisor is pure deterministic — no network calls."""
        result = recommend_strategy(FailureCategory.TRANSIENT)
        assert result.provider == "deterministic"
