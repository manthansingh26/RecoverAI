"""Operator-facing explainer for validated AI decisions (Milestone 16A).

The explainer converts a validated Diagnosis + Recommendation into a concise,
operator-facing explanation. It is a pure function of the validated inputs —
it NEVER calls the LLM and NEVER invents facts. This keeps explanations
deterministic, auditable, and free of hallucinated customer/bank/payment
claims.
"""

from app.agents.schemas import Diagnosis, Recommendation


def explain_decision(
    *,
    diagnosis: Diagnosis,
    recommendation: Recommendation,
    amount_paise: int | None = None,
    currency: str | None = None,
) -> str:
    """Return a concise operator-facing explanation.

    Only facts present in the validated inputs are used:
    - diagnosis.category
    - diagnosis.confidence
    - diagnosis.reasoning
    - recommendation.strategy
    - recommendation.confidence
    - recommendation.reasoning
    - amount/currency if provided

    The explanation never claims an action was executed or money recovered.
    """
    amount_str = ""
    if amount_paise is not None and currency:
        amount_str = f" ({amount_paise} {currency})"
    elif amount_paise is not None:
        amount_str = f" ({amount_paise} paise)"

    lines = [
        f"AI advisory for payment{amount_str}.",
        (
            f"Diagnosis: {diagnosis.category} "
            f"(confidence {diagnosis.confidence:.2f}). {diagnosis.reasoning}"
        ),
        (
            f"Recommended strategy: {recommendation.strategy} "
            f"(confidence {recommendation.confidence:.2f}). {recommendation.reasoning}"
        ),
        (
            "This is an advisory only — the deterministic policy engine "
            "remains the final authority, and no action has been executed."
        ),
    ]
    return " ".join(lines)
