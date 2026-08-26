"""LLM recovery-strategy recommender (Milestone 16A/B.1).

Uses the provider-agnostic ``LLMProvider`` to recommend a recovery strategy.
Always returns an ``AdvisoryResult[Recommendation]`` with explicit provenance
(``used_llm`` / ``fallback_used`` / ``source``). On any failure it falls back
to the EXISTING deterministic strategy advisor with ``fallback_used=True``.

The recommendation is advisory only — the PolicyEngine remains the final
authority over financial state transitions.
"""

import logging

from app.agents.base import AdvisoryResult, LLMProvider
from app.agents.prompts import (
    PROMPT_VERSION,
    recommendation_user_prompt,
)
from app.agents.schemas import Diagnosis, Recommendation
from app.core.config import settings
from app.models.enums import FailureCategory
from app.services.strategy_advisor import recommend_strategy

logger = logging.getLogger(__name__)

# Reuse the existing deterministic strategy advisor as the fallback authority.
_deterministic_recommend = recommend_strategy


def _build_context(
    *,
    diagnosis: Diagnosis,
    amount_paise: int | None,
    currency: str | None,
    retry_count: int,
) -> dict:
    """Build the context dict for the recommendation prompt (no secrets)."""
    return {
        "diagnosis": {
            "category": diagnosis.category,
            "confidence": diagnosis.confidence,
            "reasoning": diagnosis.reasoning,
        },
        "amount_paise": amount_paise,
        "currency": currency,
        "retry_count": retry_count,
        "prompt_version": PROMPT_VERSION,
    }


def _deterministic_result(
    *,
    category: FailureCategory,
    retry_count: int,
    amount_paise: int,
    fallback_used: bool,
) -> AdvisoryResult[Recommendation]:
    """Wrap the existing deterministic advisor output in an AdvisoryResult."""
    rec = _deterministic_recommend(
        category=category,
        retry_count=retry_count,
        amount_paise=amount_paise,
    )
    recommendation = Recommendation(
        strategy=rec.strategy.value,
        confidence=float(rec.confidence),
        reasoning=(
            f"deterministic advisor: {rec.reasoning_summary} "
            f"(flags={rec.risk_flags})"
        ),
    )
    return AdvisoryResult(
        value=recommendation,
        used_llm=False,
        fallback_used=fallback_used,
        provider=None,
        model=None,
        prompt_version=PROMPT_VERSION,
        confidence=recommendation.confidence,
    )


def _llm_result(
    *,
    recommendation: Recommendation,
    provider: str | None,
    model: str | None,
) -> AdvisoryResult[Recommendation]:
    """Wrap a validated LLM recommendation in an AdvisoryResult (used_llm=True)."""
    return AdvisoryResult(
        value=recommendation,
        used_llm=True,
        fallback_used=False,
        provider=provider,
        model=model,
        prompt_version=PROMPT_VERSION,
        confidence=recommendation.confidence,
    )


def recommend_strategy_for_diagnosis(
    *,
    diagnosis: Diagnosis,
    amount_paise: int | None = None,
    currency: str | None = None,
    retry_count: int = 0,
    provider: LLMProvider | None = None,
) -> AdvisoryResult[Recommendation]:
    """Return a validated recovery-strategy recommendation with explicit provenance.

    Falls back to the deterministic strategy advisor when the LLM cannot
    produce a valid, schema-conforming recommendation. Never raises for LLM
    failures — the fallback always produces a valid result.

    Args:
        diagnosis: A validated Diagnosis (from ``diagnose_failure``).
        amount_paise: Payment amount in paise (context + fallback input).
        currency: Payment currency (context only).
        retry_count: Number of prior retries (context + fallback input).
        provider: Optional provider override (for tests).

    Returns:
        An AdvisoryResult wrapping a valid Recommendation.
    """
    context = _build_context(
        diagnosis=diagnosis,
        amount_paise=amount_paise,
        currency=currency,
        retry_count=retry_count,
    )

    try:
        category = FailureCategory(diagnosis.category)
    except ValueError:
        category = FailureCategory.UNKNOWN

    # Fast path: LLM disabled or no key -> deterministic (no fallback needed).
    if not settings.LLM_FALLBACK_ENABLED or not LLMProvider._get_api_key(settings.LLM_PROVIDER):
        return _deterministic_result(
            category=category,
            retry_count=retry_count,
            amount_paise=amount_paise or 0,
            fallback_used=False,
        )

    prov = provider or LLMProvider(settings.LLM_MODEL_DIAGNOSIS)
    result = prov.call(
        system_prompt=_recommendation_system_prompt(),
        user_prompt=recommendation_user_prompt(context=context),
    )

    if result.error is not None:
        logger.info("LLM recommendation unavailable — using deterministic fallback")
        return _deterministic_result(
            category=category,
            retry_count=retry_count,
            amount_paise=amount_paise or 0,
            fallback_used=True,
        )

    try:
        recommendation = Recommendation.model_validate(result.content or {})
    except Exception as e:
        logger.info("LLM recommendation schema rejected (%s) — using deterministic fallback", e)
        return _deterministic_result(
            category=category,
            retry_count=retry_count,
            amount_paise=amount_paise or 0,
            fallback_used=True,
        )

    return _llm_result(
        recommendation=recommendation,
        provider=settings.LLM_PROVIDER,
        model=prov.model,
    )


def _recommendation_system_prompt() -> str:
    from app.agents.prompts import _RECOMMENDATION_SYSTEM
    return _RECOMMENDATION_SYSTEM