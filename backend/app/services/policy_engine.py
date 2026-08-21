"""Deterministic policy engine for recovery strategy validation.

Independently validates proposed recovery strategies against a set of
hard business rules. AI recommendations MUST pass through this engine
before any financial action is taken.

No external dependencies. All rules are deterministic and auditable.
"""

from dataclasses import dataclass, field

from app.core.config import settings
from app.models.enums import FailureCategory, RecoveryStrategy


@dataclass(frozen=True)
class PolicyDecision:
    """Structured result of policy evaluation."""

    approved: bool
    final_strategy: RecoveryStrategy
    requires_human_approval: bool
    violations: list[str] = field(default_factory=list)
    applied_rules: list[str] = field(default_factory=list)
    policy_reason: str = ""


def _is_automated_action(strategy: RecoveryStrategy) -> bool:
    """Check if a strategy would perform an automated customer-facing action."""
    return strategy in (
        RecoveryStrategy.WAIT_AND_RETRY,
        RecoveryStrategy.CREATE_PAYMENT_LINK,
    )


def evaluate_policy(
    *,
    amount_paise: int,
    failure_category: FailureCategory,
    proposed_strategy: RecoveryStrategy,
    recovery_probability: float | None,
    retry_count: int,
) -> PolicyDecision:
    """Evaluate a proposed strategy against deterministic policy rules.

    Rules enforced:
    1. HARD_FAILURE: Blocks WAIT_AND_RETRY, blocks auto CREATE_PAYMENT_LINK.
    2. UNKNOWN: Blocks WAIT_AND_RETRY and auto CREATE_PAYMENT_LINK.
    3. MAX_RETRIES: Blocks WAIT_AND_RETRY when retry_count >= limit.
    4. HIGH_VALUE: Requires human approval for automated actions above threshold.
    5. STRATEGY_ALLOWLIST: Ensures strategy is a valid RecoveryStrategy.

    Args:
        amount_paise: Payment amount in paise.
        failure_category: Classified failure category.
        proposed_strategy: Strategy recommended by the advisor.
        recovery_probability: Model confidence in recovery (0-1), or None.
        retry_count: Number of retries already attempted.

    Returns:
        PolicyDecision with approval status and applied rules.
    """
    violations: list[str] = []
    applied_rules: list[str] = []
    requires_human = False

    max_retries = settings.RECOVERY_MAX_RETRIES
    high_value_threshold = settings.RECOVERY_HIGH_VALUE_THRESHOLD_PAISE

    # RULE 5 — STRATEGY ALLOWLIST: Validate strategy is a known enum member
    applied_rules.append("RULE_5_STRATEGY_ALLOWLIST")
    strategy_value = proposed_strategy.value if hasattr(proposed_strategy, "value") else str(proposed_strategy)
    try:
        RecoveryStrategy(strategy_value)
    except ValueError:
        # Invalid strategy — fail safe to HUMAN_REVIEW
        violations.append(f"Invalid strategy value: {strategy_value!r}")
        return PolicyDecision(
            approved=False,
            final_strategy=RecoveryStrategy.HUMAN_REVIEW,
            requires_human_approval=True,
            violations=violations,
            applied_rules=applied_rules,
            policy_reason="Invalid strategy rejected; defaulting to HUMAN_REVIEW.",
        )
    # Ensure proposed_strategy is the enum from here on
    proposed_strategy = RecoveryStrategy(strategy_value)

    final_strategy = proposed_strategy

    # RULE 1 — HARD FAILURE
    applied_rules.append("RULE_1_HARD_FAILURE")
    if failure_category == FailureCategory.HARD_FAILURE:
        if proposed_strategy == RecoveryStrategy.WAIT_AND_RETRY:
            violations.append(
                "WAIT_AND_RETRY blocked for HARD_FAILURE category."
            )
            final_strategy = RecoveryStrategy.STOP_RECOVERY
        elif proposed_strategy == RecoveryStrategy.CREATE_PAYMENT_LINK:
            violations.append(
                "Automated CREATE_PAYMENT_LINK not allowed for HARD_FAILURE."
            )
            requires_human = True
            final_strategy = RecoveryStrategy.HUMAN_REVIEW

    # RULE 2 — UNKNOWN FAILURE
    applied_rules.append("RULE_2_UNKNOWN_FAILURE")
    if failure_category == FailureCategory.UNKNOWN:
        if proposed_strategy in (
            RecoveryStrategy.WAIT_AND_RETRY,
            RecoveryStrategy.CREATE_PAYMENT_LINK,
        ):
            violations.append(
                f"Automated strategy {proposed_strategy.value} blocked for UNKNOWN category."
            )
            final_strategy = RecoveryStrategy.HUMAN_REVIEW
            requires_human = True

    # RULE 3 — MAX RETRIES
    applied_rules.append("RULE_3_MAX_RETRIES")
    if retry_count >= max_retries:
        if final_strategy == RecoveryStrategy.WAIT_AND_RETRY:
            violations.append(
                f"Max retries ({max_retries}) reached; WAIT_AND_RETRY blocked."
            )
            final_strategy = RecoveryStrategy.STOP_RECOVERY

    # RULE 4 — HIGH VALUE THRESHOLD
    applied_rules.append("RULE_4_HIGH_VALUE_THRESHOLD")
    if amount_paise >= high_value_threshold:
        if _is_automated_action(final_strategy):
            violations.append(
                f"High-value transaction ({amount_paise} paise >= {high_value_threshold}); "
                f"automated action {final_strategy.value} requires human approval."
            )
            requires_human = True
            if final_strategy == RecoveryStrategy.CREATE_PAYMENT_LINK:
                final_strategy = RecoveryStrategy.CREATE_PAYMENT_LINK
            # Keep the strategy but flag for human approval

    # Determine overall approval
    approved = len(violations) == 0

    # Build policy reason
    if violations:
        policy_reason = (
            f"Policy applied {len(applied_rules)} rules with "
            f"{len(violations)} violation(s). Final strategy: {final_strategy.value}."
        )
    else:
        policy_reason = (
            f"Policy approved proposed strategy {proposed_strategy.value} "
            f"with no violations across {len(applied_rules)} rules."
        )

    return PolicyDecision(
        approved=approved,
        final_strategy=final_strategy,
        requires_human_approval=requires_human,
        violations=violations,
        applied_rules=applied_rules,
        policy_reason=policy_reason,
    )
