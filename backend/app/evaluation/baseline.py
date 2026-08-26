"""Baseline strategies for evaluation comparison (Milestone 16C.4).

BASELINE A — "Retry Everything Except Hard Failures":
  Deliberately simple naive rule-based strategy using only surface failure_reason.
  No LLM, no PolicyEngine, no deterministic classifier.

BASELINE B — "Deterministic RecoverAI":
  Uses the existing deterministic failure classifier + deterministic
  strategy advisor + PolicyEngine. Does NOT call the LLM.
  Measures: naive baseline → deterministic system gap.
"""

from app.evaluation.models import EvaluationCase
from app.models.enums import RecoveryStrategy


def baseline_a_strategy(case: EvaluationCase) -> str:
    """Baseline A: Retry everything except hard failures.

    Uses only the surface-level failure_reason string. No LLM, no policy engine.
    """
    reason = (case.failure_reason or "").strip().lower()
    hard_failure_reasons = {
        "debit_instrument_blocked",
        "beneficiary_account_dormant",
    }
    if reason in hard_failure_reasons:
        return RecoveryStrategy.STOP_RECOVERY.value
    return RecoveryStrategy.WAIT_AND_RETRY.value


# Keep backward-compatible alias.
baseline_strategy_for_case = baseline_a_strategy


def baseline_b_strategy(case: EvaluationCase) -> tuple[str, bool]:
    """Baseline B: Deterministic RecoverAI (classifier + recommender + policy).

    Returns (final_strategy, policy_blocked).
    Never calls the LLM — uses the same deterministic fallback path that
    the AI system uses when LLM is unavailable.
    """
    from app.models.enums import FailureCategory
    from app.services.failure_classifier import classify_failure
    from app.services.policy_engine import evaluate_policy
    from app.services.strategy_advisor import recommend_strategy

    classification = classify_failure(case.failure_reason)

    try:
        failure_cat = FailureCategory(classification.category.value)
    except ValueError:
        failure_cat = FailureCategory.UNKNOWN

    rec = recommend_strategy(
        failure_cat,
        retry_count=case.retry_count,
        amount_paise=case.amount_paise,
    )

    policy = evaluate_policy(
        amount_paise=case.amount_paise,
        failure_category=failure_cat,
        proposed_strategy=rec.strategy,
        recovery_probability=float(classification.confidence),
        retry_count=case.retry_count,
    )

    policy_blocked = not policy.approved and policy.final_strategy != rec.strategy
    return policy.final_strategy.value, policy_blocked
