"""Evaluation runner — three-strategy batch evaluation (Milestone 16C.2).

Runs Baseline A, Baseline B, and AI-Augmented RecoverAI over the same
frozen batch. Records true per-case AI provenance from AdvisoryResult
and true policy-block from PolicyDecision. Computes machine-derived
metrics with safe zero-denominator handling.

The evaluator NEVER mutates production DB rows.
"""

from app.evaluation.baseline import baseline_a_strategy, baseline_b_strategy
from app.evaluation.models import (
    ComparisonReport,
    EvaluationCase,
    EvaluationResult,
    StrategyOutcome,
)
from app.evaluation.response_model import simulate_customer_response
from app.models.enums import RecoveryStrategy


def _evaluate_one_case(
    case: EvaluationCase,
    strategy: str,
    *,
    diagnosis_source: str,
    diagnosis_used_llm: bool,
    diagnosis_fallback_used: bool,
    recommendation_source: str,
    recommendation_used_llm: bool,
    recommendation_fallback_used: bool,
    ai_recommended_strategy: str,
    policy_final_strategy: str,
    policy_blocked: bool,
    escalated_to_human: bool,
    safe_stop: bool,
) -> StrategyOutcome:
    response = simulate_customer_response(case, strategy)
    return StrategyOutcome(
        case_id=case.case_id,
        amount_paise=case.amount_paise,
        strategy=strategy,
        diagnosis_source=diagnosis_source,
        diagnosis_used_llm=diagnosis_used_llm,
        diagnosis_fallback_used=diagnosis_fallback_used,
        recommendation_source=recommendation_source,
        recommendation_used_llm=recommendation_used_llm,
        recommendation_fallback_used=recommendation_fallback_used,
        ai_recommended_strategy=ai_recommended_strategy,
        policy_final_strategy=policy_final_strategy,
        policy_blocked=policy_blocked,
        escalated_to_human=escalated_to_human,
        safe_stop=safe_stop,
        customer_paid=response.customer_paid,
        recovered_amount_paise=response.recovered_amount_paise,
        outreach_cost_paise=response.outreach_cost_paise,
        churn_cost_paise=response.churn_cost_paise,
    )


def _recoverai_decision(case: EvaluationCase, *, llm_available: bool):
    """Run the full RecoverAI decision pipeline for one case.

    Returns (strategy, provenance_dict) where provenance_dict contains
    all fields needed for StrategyOutcome.

    Task 2 (16C.2): Passes ALL observable evidence to the diagnostician
    so the AI has the same information as a human operator.
    """
    from app.agents.diagnostician import diagnose_failure
    from app.agents.recommender import recommend_strategy_for_diagnosis
    from app.models.enums import FailureCategory
    from app.services.policy_engine import evaluate_policy

    diag_result = diagnose_failure(
        error_reason=case.failure_reason,
        error_description=case.failure_description,
        amount_paise=case.amount_paise,
        currency=case.currency,
        retry_count=case.retry_count,
        gateway_description=case.gateway_description,
        customer_note=case.customer_note,
        retry_history_summary=case.retry_history_summary,
        customer_history_score=case.customer_history_score,
    )
    diagnosis = diag_result.value

    rec_result = recommend_strategy_for_diagnosis(
        diagnosis=diagnosis,
        amount_paise=case.amount_paise,
        currency=case.currency,
        retry_count=case.retry_count,
    )
    recommendation = rec_result.value

    try:
        failure_cat = FailureCategory(diagnosis.category)
    except ValueError:
        failure_cat = FailureCategory.UNKNOWN

    try:
        proposed = RecoveryStrategy(recommendation.strategy)
    except ValueError:
        proposed = RecoveryStrategy.STOP_RECOVERY

    policy = evaluate_policy(
        amount_paise=case.amount_paise,
        failure_category=failure_cat,
        proposed_strategy=proposed,
        recovery_probability=rec_result.confidence,
        retry_count=case.retry_count,
    )

    ai_rec_str = proposed.value
    policy_final_str = policy.final_strategy.value
    blocked = len(policy.violations) > 0

    return policy_final_str, {
        "diagnosis_source": diag_result.source,
        "diagnosis_used_llm": diag_result.used_llm,
        "diagnosis_fallback_used": diag_result.fallback_used,
        "recommendation_source": rec_result.source,
        "recommendation_used_llm": rec_result.used_llm,
        "recommendation_fallback_used": rec_result.fallback_used,
        "ai_recommended_strategy": ai_rec_str,
        "policy_final_strategy": policy_final_str,
        "policy_blocked": blocked,
        "escalated_to_human": policy.requires_human_approval,
        "safe_stop": policy_final_str == RecoveryStrategy.STOP_RECOVERY.value,
    }


def _compute_metrics(
    outcomes: list[StrategyOutcome],
    strategy_name: str,
) -> EvaluationResult:
    n = len(outcomes)
    if n == 0:
        return EvaluationResult(
            strategy_name=strategy_name,
            total_cases=0, gross_at_risk_paise=0, recovered_amount_paise=0,
            net_recovered_paise=0, recovery_rate=0.0, escalation_rate=0.0,
            safe_stop_rate=0.0, policy_block_rate=0.0, ai_usage_rate=0.0,
            ai_fallback_rate=0.0, outreach_cost_paise=0, churn_cost_paise=0,
            cost_per_recovered_rupee=0.0,
        )

    gross = sum(o.amount_paise for o in outcomes)
    recovered = sum(o.recovered_amount_paise for o in outcomes)
    net = sum(o.net_recovered_paise for o in outcomes)
    paid = sum(1 for o in outcomes if o.customer_paid)
    escalated = sum(1 for o in outcomes if o.escalated_to_human)
    stopped = sum(1 for o in outcomes if o.safe_stop)
    blocked = sum(1 for o in outcomes if o.policy_blocked)
    ai_count = sum(1 for o in outcomes if o.recommendation_source == "ai")
    fallback = sum(1 for o in outcomes if o.diagnosis_fallback_used or o.recommendation_fallback_used)
    outreach = sum(o.outreach_cost_paise for o in outcomes)
    churn = sum(o.churn_cost_paise for o in outcomes)

    return EvaluationResult(
        strategy_name=strategy_name,
        total_cases=n,
        gross_at_risk_paise=gross,
        recovered_amount_paise=recovered,
        net_recovered_paise=net,
        recovery_rate=paid / n,
        escalation_rate=escalated / n,
        safe_stop_rate=stopped / n,
        policy_block_rate=blocked / n,
        ai_usage_rate=ai_count / n,
        ai_fallback_rate=fallback / n,
        outreach_cost_paise=outreach,
        churn_cost_paise=churn,
        cost_per_recovered_rupee=(outreach / recovered) if recovered > 0 else 0.0,
        outcomes=outcomes,
    )


def _safe_uplift(numerator_net: int, denominator_net: int) -> float:
    """(num - den) / den when den != 0, else 0.0 (safe).

    Mirrors models._safe_uplift; kept here for callers that import from the
    evaluator module.
    """
    if denominator_net == 0:
        return 0.0
    return (numerator_net - denominator_net) / denominator_net


def _deterministic_combined_fallback(case: EvaluationCase, *, fallback_used: bool):
    strat_b, policy_blocked_b = baseline_b_strategy(case)
    is_stop_b = strat_b == RecoveryStrategy.STOP_RECOVERY.value
    is_human_b = strat_b == RecoveryStrategy.HUMAN_REVIEW.value
    return strat_b, {
        "diagnosis_source": "deterministic",
        "diagnosis_used_llm": False,
        "diagnosis_fallback_used": fallback_used,
        "recommendation_source": "deterministic",
        "recommendation_used_llm": False,
        "recommendation_fallback_used": fallback_used,
        "ai_recommended_strategy": strat_b,
        "policy_final_strategy": strat_b,
        "policy_blocked": policy_blocked_b,
        "escalated_to_human": is_human_b,
        "safe_stop": is_stop_b,
    }


def _evaluate_ai_combined_decision(case: EvaluationCase, *, llm_available: bool):
    """Run evaluation-only combined decision pipeline (1 LLM call per case).

    Combines diagnosis and recommendation into a single structured LLM prompt,
    reducing provider calls from 2 to 1 while preserving all observable inputs,
    schema validation, confidence gating, and PolicyEngine final authority.

    The model receives ONLY observable evidence and NEVER hidden ground truth.
    """
    from app.agents.base import LLMProvider
    from app.agents.schemas import Diagnosis, Recommendation
    from app.core.config import settings
    from app.models.enums import FailureCategory, RecoveryStrategy
    from app.services.policy_engine import evaluate_policy

    active_provider = (settings.LLM_PROVIDER or "anthropic").lower()
    has_key = bool(LLMProvider._get_api_key(active_provider))

    # Fast path: LLM disabled or no key -> deterministic (no fallback needed)
    if not llm_available or not settings.LLM_FALLBACK_ENABLED or not has_key:
        return _deterministic_combined_fallback(case, fallback_used=False)

    # Combined LLM Call: Single prompt delivering all observable context
    system_prompt = (
        "You are RecoverAI's specialized revenue recovery agent.\n\n"
        "Your task is to analyze a failed payment using ONLY the observable evidence provided, "
        "diagnose the root-cause failure category, and recommend the optimal recovery strategy.\n\n"
        "RULES:\n"
        "1. Return ONLY a single valid JSON object with 'diagnosis' and 'recommendation' keys.\n"
        "2. 'diagnosis' MUST contain:\n"
        "   - 'category': one of [\"TRANSIENT\", \"AUTHENTICATION\", \"HARD_FAILURE\", \"UNKNOWN\"]\n"
        "   - 'confidence': float between 0.0 and 1.0\n"
        "   - 'reasoning': concise 1-2 sentence explanation\n"
        "3. 'recommendation' MUST contain:\n"
        "   - 'strategy': one of [\"WAIT_AND_RETRY\", \"CUSTOMER_NOTIFICATION\", \"HUMAN_REVIEW\", \"STOP_RECOVERY\"]\n"
        "   - 'confidence': float between 0.0 and 1.0\n"
        "   - 'reasoning': concise 1-2 sentence justification\n"
        "4. DO NOT assume or guess unavailable external data."
    )

    user_prompt = (
        f"Analyze the following failed transaction:\n"
        f"- failure_reason: {case.failure_reason}\n"
        f"- failure_description: {case.failure_description}\n"
        f"- amount_paise: {case.amount_paise}\n"
        f"- currency: {case.currency}\n"
        f"- retry_count: {case.retry_count}\n"
        f"- gateway_description: {case.gateway_description or 'N/A'}\n"
        f"- customer_note: {case.customer_note or 'N/A'}\n"
        f"- retry_history_summary: {case.retry_history_summary or 'N/A'}\n"
        f"- customer_history_score: {case.customer_history_score if case.customer_history_score is not None else 'N/A'}"
    )

    prov = LLMProvider(settings.LLM_MODEL_DIAGNOSIS)
    result = prov.call(system_prompt=system_prompt, user_prompt=user_prompt)

    # Provider call failed -> deterministic fallback
    if result.error is not None or not isinstance(result.content, dict):
        return _deterministic_combined_fallback(case, fallback_used=True)

    try:
        diag_data = result.content.get("diagnosis", {})
        rec_data = result.content.get("recommendation", {})
        diagnosis = Diagnosis.model_validate(diag_data)
        recommendation = Recommendation.model_validate(rec_data)
    except Exception:
        # Schema validation error -> deterministic fallback
        return _deterministic_combined_fallback(case, fallback_used=True)

    # Extract validated enums
    try:
        failure_cat = FailureCategory(diagnosis.category)
    except ValueError:
        failure_cat = FailureCategory.UNKNOWN

    try:
        proposed = RecoveryStrategy(recommendation.strategy)
    except ValueError:
        proposed = RecoveryStrategy.STOP_RECOVERY

    # Confidence gating: if recommendation confidence is below threshold, escalate to human review
    if recommendation.confidence < settings.LLM_CONFIDENCE_THRESHOLD:
        proposed = RecoveryStrategy.HUMAN_REVIEW

    # PolicyEngine is the FINAL AUTHORITY
    policy = evaluate_policy(
        amount_paise=case.amount_paise,
        failure_category=failure_cat,
        proposed_strategy=proposed,
        recovery_probability=recommendation.confidence,
        retry_count=case.retry_count,
    )

    ai_rec_str = proposed.value
    policy_final_str = policy.final_strategy.value
    blocked = not policy.approved and policy_final_str != ai_rec_str

    return policy_final_str, {
        "diagnosis_source": "ai",
        "diagnosis_used_llm": True,
        "diagnosis_fallback_used": False,
        "recommendation_source": "ai",
        "recommendation_used_llm": True,
        "recommendation_fallback_used": False,
        "ai_recommended_strategy": ai_rec_str,
        "policy_final_strategy": policy_final_str,
        "policy_blocked": blocked,
        "escalated_to_human": policy_final_str == RecoveryStrategy.HUMAN_REVIEW.value,
        "safe_stop": policy_final_str == RecoveryStrategy.STOP_RECOVERY.value,
    }


def run_batch_evaluation(
    cases: list[EvaluationCase],
    *,
    enable_llm: bool = False,
) -> ComparisonReport:
    """Run all three strategies over the same frozen batch."""
    from app.core.config import settings
    from app.agents.base import LLMProvider
    from contextlib import ExitStack
    from unittest.mock import patch

    active_provider = (settings.LLM_PROVIDER or "anthropic").lower()
    has_key = bool(LLMProvider._get_api_key(active_provider))
    llm_available = enable_llm and has_key

    baseline_a_outcomes = []
    baseline_b_outcomes = []
    ai_outcomes = []

    with ExitStack() as stack:
        if not llm_available:
            stack.enter_context(patch.object(settings, "LLM_API_KEY", ""))
            stack.enter_context(patch.object(settings, "GEMINI_API_KEY", ""))
            stack.enter_context(patch.object(settings, "OPENAI_API_KEY", ""))
            stack.enter_context(patch.object(settings, "ANTHROPIC_API_KEY", ""))

        for case in cases:
            # --- Baseline A: Retry Everything Except Hard Failures ---
            strat_a = baseline_a_strategy(case)
            is_stop_a = strat_a == RecoveryStrategy.STOP_RECOVERY.value
            baseline_a_outcomes.append(_evaluate_one_case(
                case, strat_a,
                diagnosis_source="deterministic",
                diagnosis_used_llm=False,
                diagnosis_fallback_used=False,
                recommendation_source="deterministic",
                recommendation_used_llm=False,
                recommendation_fallback_used=False,
                ai_recommended_strategy=strat_a,
                policy_final_strategy=strat_a,
                policy_blocked=False,
                escalated_to_human=False,
                safe_stop=is_stop_a,
            ))

            # --- Baseline B: Deterministic RecoverAI ---
            strat_b, policy_blocked_b = baseline_b_strategy(case)
            is_stop_b = strat_b == RecoveryStrategy.STOP_RECOVERY.value
            is_human_b = strat_b == RecoveryStrategy.HUMAN_REVIEW.value
            baseline_b_outcomes.append(_evaluate_one_case(
                case, strat_b,
                diagnosis_source="deterministic",
                diagnosis_used_llm=False,
                diagnosis_fallback_used=False,
                recommendation_source="deterministic",
                recommendation_used_llm=False,
                recommendation_fallback_used=False,
                ai_recommended_strategy=strat_b,
                policy_final_strategy=strat_b,
                policy_blocked=policy_blocked_b,
                escalated_to_human=is_human_b,
                safe_stop=is_stop_b,
            ))

            # --- AI-Augmented RecoverAI (Combined Evaluation Inference: 1 call/case) ---
            ai_strat, prov = _evaluate_ai_combined_decision(case, llm_available=llm_available)
            ai_outcomes.append(_evaluate_one_case(
                case, ai_strat, **prov,
            ))

    result_a = _compute_metrics(baseline_a_outcomes, "retry_everything")
    result_b = _compute_metrics(baseline_b_outcomes, "deterministic_recoverai")
    result_ai = _compute_metrics(ai_outcomes, "ai_augmented")

    # ComparisonReport.__post_init__ computes the canonical pairwise uplifts
    # from the three per-strategy net amounts (safe zero-denominator handling).
    return ComparisonReport(
        baseline_a=result_a,
        baseline_b=result_b,
        ai_augmented=result_ai,
    )
