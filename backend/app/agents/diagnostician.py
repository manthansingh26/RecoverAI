"""LLM root-cause diagnostician (Milestone 16A/B.1).

Uses the provider-agnostic ``LLMProvider`` to classify a payment failure.
Always returns an ``AdvisoryResult[Diagnosis]`` with explicit provenance
(``used_llm`` / ``fallback_used`` / ``source``). On any failure — LLM disabled,
missing key, timeout, exception, malformed JSON, or invalid schema — it falls
back to the EXISTING deterministic classifier and marks ``fallback_used=True``.

The returned ``AdvisoryResult`` is advisory only; it never mutates state.
"""

import logging

from app.agents.base import AdvisoryResult, LLMProvider
from app.agents.prompts import (
    PROMPT_VERSION,
    diagnosis_user_prompt,
)
from app.agents.schemas import Diagnosis
from app.core.config import settings
from app.services.failure_classifier import classify_failure

logger = logging.getLogger(__name__)

# Reuse the existing deterministic classifier as the fallback authority.
_deterministic_classify = classify_failure


def _build_context(
    *,
    error_reason: str | None,
    error_description: str | None,
    amount_paise: int | None,
    currency: str | None,
    retry_count: int,
    gateway_description: str | None = None,
    customer_note: str | None = None,
    retry_history_summary: str | None = None,
    customer_history_score: int | None = None,
) -> dict:
    """Build the context dict passed to the diagnosis prompt (no secrets).

    Only observable evidence is included — never the hidden failure category.
    """
    ctx: dict = {
        "error_reason": error_reason,
        "error_description": error_description,
        "amount_paise": amount_paise,
        "currency": currency,
        "retry_count": retry_count,
        "prompt_version": PROMPT_VERSION,
    }
    if gateway_description is not None:
        ctx["gateway_description"] = gateway_description
    if customer_note is not None:
        ctx["customer_note"] = customer_note
    if retry_history_summary is not None:
        ctx["retry_history_summary"] = retry_history_summary
    if customer_history_score is not None:
        ctx["customer_history_score"] = customer_history_score
    return ctx


def _deterministic_result(
    *,
    error_reason: str | None,
    fallback_used: bool,
) -> AdvisoryResult[Diagnosis]:
    """Wrap the existing deterministic classifier output in an AdvisoryResult."""
    classification = _deterministic_classify(error_reason)
    diagnosis = Diagnosis(
        category=classification.category.value,
        confidence=float(classification.confidence),
        reasoning=(
            f"deterministic classification: {classification.reason} "
            f"(rule_id={classification.rule_id})"
        ),
    )
    return AdvisoryResult(
        value=diagnosis,
        used_llm=False,
        fallback_used=fallback_used,
        provider=None,
        model=None,
        prompt_version=PROMPT_VERSION,
        confidence=diagnosis.confidence,
    )


def _llm_result(
    *,
    diagnosis: Diagnosis,
    provider: str | None,
    model: str | None,
) -> AdvisoryResult[Diagnosis]:
    """Wrap a validated LLM diagnosis in an AdvisoryResult (used_llm=True)."""
    return AdvisoryResult(
        value=diagnosis,
        used_llm=True,
        fallback_used=False,
        provider=provider,
        model=model,
        prompt_version=PROMPT_VERSION,
        confidence=diagnosis.confidence,
    )


def diagnose_failure(
    *,
    error_reason: str | None,
    error_description: str | None = None,
    amount_paise: int | None = None,
    currency: str | None = None,
    retry_count: int = 0,
    gateway_description: str | None = None,
    customer_note: str | None = None,
    retry_history_summary: str | None = None,
    customer_history_score: int | None = None,
    provider: LLMProvider | None = None,
) -> AdvisoryResult[Diagnosis]:
    """Return a validated root-cause diagnosis with explicit provenance.

    Falls back to the deterministic classifier when the LLM cannot produce a
    valid, schema-conforming diagnosis. Never raises for LLM failures — the
    fallback always produces a valid result.

    Args:
        error_reason: Normalized failure reason (e.g. "network_error").
        error_description: Human-readable failure description, if any.
        amount_paise: Payment amount in paise (context only).
        currency: Payment currency (context only).
        retry_count: Number of prior retries (context only).
        provider: Optional provider override (for tests). Defaults to a
            provider bound to settings.LLM_MODEL_DIAGNOSIS.

    Returns:
        An AdvisoryResult wrapping a valid Diagnosis.
    """
    context = _build_context(
        error_reason=error_reason,
        error_description=error_description,
        amount_paise=amount_paise,
        currency=currency,
        retry_count=retry_count,
        gateway_description=gateway_description,
        customer_note=customer_note,
        retry_history_summary=retry_history_summary,
        customer_history_score=customer_history_score,
    )

    # Fast path: LLM disabled or no key -> deterministic (no fallback needed,
    # because the LLM was never attempted).
    if not settings.LLM_FALLBACK_ENABLED or not LLMProvider._get_api_key(settings.LLM_PROVIDER):
        return _deterministic_result(error_reason=error_reason, fallback_used=False)

    prov = provider or LLMProvider(settings.LLM_MODEL_DIAGNOSIS)
    result = prov.call(
        system_prompt=_diagnosis_system_prompt(),
        user_prompt=diagnosis_user_prompt(context=context),
    )

    if result.error is not None:
        logger.info("LLM diagnosis unavailable — using deterministic fallback")
        return _deterministic_result(error_reason=error_reason, fallback_used=True)

    try:
        diagnosis = Diagnosis.model_validate(result.content or {})
    except Exception as e:
        logger.info("LLM diagnosis schema rejected (%s) — using deterministic fallback", e)
        return _deterministic_result(error_reason=error_reason, fallback_used=True)

    return _llm_result(
        diagnosis=diagnosis,
        provider=settings.LLM_PROVIDER,
        model=prov.model,
    )


# Lazy import to avoid a circular import at module load time.
def _diagnosis_system_prompt() -> str:
    from app.agents.prompts import _DIAGNOSIS_SYSTEM
    return _DIAGNOSIS_SYSTEM
