"""Deterministic strategy advisor for payment recovery recommendations.

Provides a deterministic baseline advisor that maps FailureCategory to
RecoveryStrategy. Designed so a future LLM-based provider can be added
without rewriting the Decision Engine.

All public functions are pure — no external API calls.
"""

from dataclasses import dataclass, field

from app.models.enums import FailureCategory, RecoveryStrategy


@dataclass(frozen=True)
class StrategyRecommendation:
    """Structured strategy recommendation."""

    strategy: RecoveryStrategy
    confidence: float
    reasoning_summary: str
    risk_flags: list[str] = field(default_factory=list)
    requires_human_review: bool = False
    provider: str = "deterministic"


# Deterministic mapping: FailureCategory -> (strategy, confidence, base flags)
_CATEGORY_STRATEGY_MAP: dict[
    FailureCategory, tuple[RecoveryStrategy, float, list[str]]
] = {
    FailureCategory.TRANSIENT: (
        RecoveryStrategy.WAIT_AND_RETRY,
        0.75,
        ["transient_error_possible_retry"],
    ),
    FailureCategory.AUTHENTICATION: (
        RecoveryStrategy.CREATE_PAYMENT_LINK,
        0.7,
        ["auth_failure_requires_user_action"],
    ),
    FailureCategory.HARD_FAILURE: (
        RecoveryStrategy.STOP_RECOVERY,
        0.9,
        ["hard_failure_likely_permanent"],
    ),
    FailureCategory.UNKNOWN: (
        RecoveryStrategy.HUMAN_REVIEW,
        0.5,
        ["unknown_failure_needs_triage"],
    ),
}


def recommend_strategy(
    category: FailureCategory,
    *,
    retry_count: int = 0,
    amount_paise: int = 0,
) -> StrategyRecommendation:
    """Recommend a recovery strategy based on the failure category.

    Uses deterministic rules as the baseline. Higher-level policy validation
    is performed by the PolicyEngine downstream.

    Args:
        category: The classified failure category.
        retry_count: Current retry count (informational for recommendation).
        amount_paise: Payment amount (informational for recommendation).

    Returns:
        StrategyRecommendation with strategy, confidence, reasoning, and flags.
    """
    strategy, base_confidence, base_flags = _CATEGORY_STRATEGY_MAP[category]

    # Build reasoning from deterministic templates
    reasoning_templates: dict[FailureCategory, str] = {
        FailureCategory.TRANSIENT: (
            "Transient error detected; waiting and retrying is the lowest-risk "
            "automated recovery action."
        ),
        FailureCategory.AUTHENTICATION: (
            "Authentication failure indicates user action is needed; "
            "creating a new payment link allows the customer to retry."
        ),
        FailureCategory.HARD_FAILURE: (
            "Hard failure indicates a permanent or structural issue; "
            "automatic recovery is unsafe. Stopping recovery."
        ),
        FailureCategory.UNKNOWN: (
            "Unable to classify failure reason with confidence; "
            "escalating to human review."
        ),
    }

    risk_flags = list(base_flags)

    # Add contextual risk flags
    if retry_count > 0:
        risk_flags.append(f"retry_attempt_{retry_count}")
    if amount_paise >= 5_000_000:
        risk_flags.append("high_value_transaction")

    requires_human = category in (
        FailureCategory.UNKNOWN,
        FailureCategory.HARD_FAILURE,
    )

    return StrategyRecommendation(
        strategy=strategy,
        confidence=base_confidence,
        reasoning_summary=reasoning_templates[category],
        risk_flags=risk_flags,
        requires_human_review=requires_human,
        provider="deterministic",
    )
