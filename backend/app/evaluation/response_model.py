"""Frozen, deterministic customer-response model for evaluation (Milestone 16C).

This is the most important evaluation-integrity component.

The response model determines whether a customer would pay after a given
recovery strategy. It is:

- **Deterministic** — the same (case, strategy) always gives the same outcome.
- **Frozen** — the model is locked before any RecoverAI evaluation is run.
- **Independent** — never calls the LLM, never depends on RecoverAI's own
  predictions. The ground truth is separate from the agent.

Strategy:
  A deterministic "score" is computed from the case attributes and the
  selected strategy. If the score >= a fixed threshold, the customer pays.

Design rationale:
  Using a deterministic score + threshold rather than true random probability
  eliminates the risk of "lucky" stochastic variation favoring one evaluation
  run over another. The same case always recovers or fails in the same way,
  making the baseline vs RecoverAI comparison purely about decision quality.
"""

from app.evaluation.models import CustomerResponse, EvaluationCase
from app.models.enums import RecoveryStrategy

# Cost assumptions (in paise). These are intentionally simple and documented
# so they can be challenged. Source: placeholder estimates — should be
# replaced with real data before submission.
_OUTREACH_COST_PAISE = 100  # ₹1 per outreach (SMS/email)
_CHURN_COST_PAISE_BASE = 500  # ₹5 base churn cost for over-contacting
_MAX_RETRIES_BEFORE_CHURN = 2


def _score(case: EvaluationCase, strategy: str) -> int:
    """Compute a deterministic recovery score (0-100) for (case, strategy).

    The score is based on:
    - The hidden failure category (ground truth, not fed to RecoverAI).
    - The strategy chosen.
    - Retry count (retries beyond a threshold reduce score).
    - Customer history score (higher = more reliable).
    - High-value flag (high-value cases have slightly lower base recovery).

    Higher score = more likely to recover.
    """
    cat = case.hidden_failure_category or "UNKNOWN"

    # Base score by failure category.
    base_score: int = 0
    if cat == "TRANSIENT":
        base_score = 70
    elif cat == "AUTHENTICATION":
        base_score = 50
    elif cat == "HARD_FAILURE":
        base_score = 5  # very low — hard failures are rarely recoverable
    else:
        base_score = 30  # UNKNOWN

    # Strategy modifier.
    strategy_mod: int = 0
    if strategy == RecoveryStrategy.WAIT_AND_RETRY.value:
        strategy_mod = 10 if cat == "TRANSIENT" else 0
    elif strategy == RecoveryStrategy.CREATE_PAYMENT_LINK.value:
        strategy_mod = 15 if cat == "AUTHENTICATION" else 5
    elif strategy == RecoveryStrategy.HUMAN_REVIEW.value:
        strategy_mod = 5  # human review helps but doesn't guarantee payment
    elif strategy == RecoveryStrategy.STOP_RECOVERY.value:
        strategy_mod = 0  # stopping recovery means no recovery

    # Retry penalty.
    retry_penalty = 10 * max(0, case.retry_count - _MAX_RETRIES_BEFORE_CHURN)

    # Customer history bonus (0-100 -> 0-10).
    history_bonus = case.customer_history_score // 10

    # High-value discount (larger amounts are harder to recover).
    value_discount = 5 if case.high_value else 0

    score = base_score + strategy_mod - retry_penalty + history_bonus - value_discount
    return max(0, min(100, score))


# Threshold: a case is "recovered" if score >= 40.
_RECOVERY_THRESHOLD = 40


def simulate_customer_response(case: EvaluationCase, strategy: str) -> CustomerResponse:
    """Return the frozen, deterministic outcome of applying *strategy* to *case*.

    This function is PURE — no side effects, no IO, no randomness. The same
    (case, strategy) always returns the same CustomerResponse.

    Args:
        case: The evaluation case (must have a ``hidden_failure_category``).
        strategy: A ``RecoveryStrategy`` value string.

    Returns:
        A frozen ``CustomerResponse``.
    """
    score = _score(case, strategy)
    customer_paid = score >= _RECOVERY_THRESHOLD

    recovered = case.amount_paise if customer_paid else 0

    # Churn cost: if the customer was contacted too many times.
    churn = 0
    if case.retry_count > _MAX_RETRIES_BEFORE_CHURN and not customer_paid:
        churn = _CHURN_COST_PAISE_BASE

    return CustomerResponse(
        customer_paid=customer_paid,
        recovered_amount_paise=recovered,
        outreach_cost_paise=_OUTREACH_COST_PAISE,
        churn_cost_paise=churn,
    )