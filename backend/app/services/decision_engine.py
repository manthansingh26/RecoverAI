"""Decision Engine — orchestrates recovery intelligence pipeline.

Flow:
1. Classify the failure (failure_classifier + optional AI diagnosis)
2. Obtain strategy recommendation (strategy_advisor + optional AI recommendation)
3. Validate with deterministic policy (policy_engine — FINAL AUTHORITY)
4. Persist final decision on RecoveryCase

Milestone 16A/B: the LLM is an ADVISOR ONLY. It may diagnose, recommend, and
explain, but the deterministic PolicyEngine remains the final authority over
financial state transitions. When the LLM is unavailable, times out, produces
invalid output, or is disabled, the existing deterministic classifiers are used
as the fallback — the system is indistinguishable from the pre-16A path.

The Decision Engine operates on an existing RecoveryCase and its associated
PaymentEvent. It NEVER directly executes financial actions.
"""

import copy
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.agents.diagnostician import diagnose_failure
from app.agents.explainer import explain_decision
from app.agents.recommender import recommend_strategy_for_diagnosis
from app.core.config import settings
from app.models.enums import FailureCategory, RecoveryStatus, RecoveryStrategy
from app.models.recovery_case import RecoveryCase
from app.services.failure_classifier import classify_failure
from app.services.policy_engine import evaluate_policy
from app.services.strategy_advisor import recommend_strategy

logger = logging.getLogger(__name__)


def _compute_priority_score(
    failure_category: FailureCategory,
    amount_paise: int,
    recovery_probability: float | None,
) -> float:
    """Compute a deterministic priority score for the recovery case.

    Formula (documented for transparency):
        base = amount_paise / 100.0  (convert to rupees for scale)
        category_weight:
            TRANSIENT      = 1.0
            AUTHENTICATION = 0.8
            UNKNOWN        = 0.5
            HARD_FAILURE   = 0.2
        probability_factor = recovery_probability if not None else 0.5
        priority = base * category_weight * probability_factor

    This is a simple heuristic, NOT an ML prediction.
    """
    category_weights: dict[FailureCategory, float] = {
        FailureCategory.TRANSIENT: 1.0,
        FailureCategory.AUTHENTICATION: 0.8,
        FailureCategory.UNKNOWN: 0.5,
        FailureCategory.HARD_FAILURE: 0.2,
    }
    base = amount_paise / 100.0
    weight = category_weights.get(failure_category, 0.5)
    probability_factor = recovery_probability if recovery_probability is not None else 0.5
    return round(base * weight * probability_factor, 2)


def _compute_expected_value_paise(
    amount_paise: int,
    recovery_probability: float | None,
) -> int:
    """Compute expected value in paise.

    Formula: amount_paise * recovery_probability
    If probability is None, defaults to 0.0 (conservative).
    """
    prob = recovery_probability if recovery_probability is not None else 0.0
    return int(amount_paise * prob)


def _determine_status_and_next_run(
    final_strategy: RecoveryStrategy,
    requires_human: bool,
) -> tuple[str, datetime | None]:
    """Determine RecoveryCase status and next_run_at based on final strategy.

    Returns:
        (status_value, next_run_at_or_None)
    """
    if requires_human or final_strategy == RecoveryStrategy.HUMAN_REVIEW:
        return RecoveryStatus.REQUIRES_HUMAN.value, None

    if final_strategy == RecoveryStrategy.STOP_RECOVERY:
        return RecoveryStatus.RESOLVED_FAILED.value, None

    if final_strategy == RecoveryStrategy.WAIT_AND_RETRY:
        next_run = datetime.now(timezone.utc) + timedelta(
            seconds=settings.RECOVERY_RETRY_DELAY_SECONDS
        )
        return RecoveryStatus.PENDING_EXECUTION.value, next_run

    if final_strategy == RecoveryStrategy.CREATE_PAYMENT_LINK:
        next_run = datetime.now(timezone.utc)
        return RecoveryStatus.PENDING_EXECUTION.value, next_run

    # Fallback — should not be reached with valid strategies
    return RecoveryStatus.REQUIRES_HUMAN.value, None


def _append_to_audit_trail(
    existing_trail: dict[str, Any],
    classification: dict[str, Any],
    recommendation: dict[str, Any],
    policy: dict[str, Any],
    ai: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Append classification, recommendation, policy, and optional AI to audit trail.

    Preserves the existing 'ingestion' section without overwriting.
    """
    trail = copy.deepcopy(existing_trail)
    trail["classification"] = classification
    trail["recommendation"] = recommendation
    trail["policy"] = policy
    if ai is not None:
        trail["ai"] = ai
    return trail


def run_decision_engine(
    db: Session,
    recovery_case_id: str,
) -> RecoveryCase | None:
    """Run the full decision engine pipeline on a RecoveryCase.

    Orchestrates:
    1. Fetch RecoveryCase and PaymentEvent
    2. Classify failure
    3. Get strategy recommendation
    4. Run policy engine
    5. Persist results to RecoveryCase
    6. Update status and next_run_at

    Args:
        db: Active database session.
        recovery_case_id: UUID string of the RecoveryCase to process.

    Returns:
        Updated RecoveryCase, or None if the case was not found or not
        in a processable state (RECEIVED).
    """
    import uuid

    try:
        rc_uuid = uuid.UUID(recovery_case_id)
    except ValueError:
        logger.warning("Invalid recovery_case_id format: %s", recovery_case_id)
        return None

    rc = db.get(RecoveryCase, rc_uuid)
    if rc is None:
        logger.warning("RecoveryCase not found: %s", recovery_case_id)
        return None

    if rc.status != RecoveryStatus.RECEIVED.value:
        logger.info(
            "RecoveryCase %s is in state %s, not RECEIVED — skipping.",
            recovery_case_id,
            rc.status,
        )
        return rc

    # Fetch associated PaymentEvent
    payment_event = rc.payment_event
    if payment_event is None:
        logger.error(
            "RecoveryCase %s has no associated PaymentEvent.", recovery_case_id
        )
        return rc

    # 1. Classify failure (AI advisory + deterministic fallback)
    # ------------------------------------------------------------------
    # The AI is an ADVISOR ONLY. The result carries explicit provenance
    # (used_llm / fallback_used / source) so no string inspection is needed.
    diagnosis_result = diagnose_failure(
        error_reason=payment_event.error_reason,
        error_description=payment_event.error_description,
        amount_paise=payment_event.amount_paise,
        currency=payment_event.currency,
        retry_count=rc.retry_count,
    )
    diagnosis = diagnosis_result.value
    failure_category = FailureCategory(diagnosis.category)
    ai_used = diagnosis_result.used_llm
    ai_confidence = diagnosis_result.confidence

    # 2. Strategy recommendation (AI advisory + deterministic fallback)
    # ------------------------------------------------------------------
    recommendation_result = recommend_strategy_for_diagnosis(
        diagnosis=diagnosis,
        amount_paise=payment_event.amount_paise,
        currency=payment_event.currency,
        retry_count=rc.retry_count,
    )
    recommendation = recommendation_result.value
    recommendation_strategy = RecoveryStrategy(recommendation.strategy)
    rec_ai_used = recommendation_result.used_llm
    rec_ai_confidence = recommendation_result.confidence

    # 3. Confidence check
    # ------------------------------------------------------------------
    # If the AI was used (not fallback) and either diagnosis or
    # recommendation confidence is below the threshold, the case must
    # go to human review. Low-confidence AI decisions must never proceed
    # automatically.
    requires_human_due_to_confidence = (
        (ai_used and ai_confidence < settings.LLM_CONFIDENCE_THRESHOLD)
        or (rec_ai_used and rec_ai_confidence < settings.LLM_CONFIDENCE_THRESHOLD)
    )

    # 4. Policy evaluation (deterministic — FINAL AUTHORITY)
    # ------------------------------------------------------------------
    policy_decision = evaluate_policy(
        amount_paise=payment_event.amount_paise,
        failure_category=failure_category,
        proposed_strategy=recommendation_strategy,
        recovery_probability=recommendation.confidence,
        retry_count=rc.retry_count,
    )

    # 5. Compute deterministic fields
    # ------------------------------------------------------------------
    final_strategy = policy_decision.final_strategy
    recovery_probability = recommendation.confidence
    priority_score = _compute_priority_score(
        failure_category,
        payment_event.amount_paise,
        recovery_probability,
    )
    expected_value = _compute_expected_value_paise(
        payment_event.amount_paise,
        recovery_probability,
    )

    # 6. Determine status and next_run_at
    # ------------------------------------------------------------------
    # HUMAN_REVIEW strategy always requires human approval.
    # Low-confidence AI decisions also escalate to human.
    requires_human = (
        policy_decision.requires_human_approval
        or (final_strategy == RecoveryStrategy.HUMAN_REVIEW)
        or requires_human_due_to_confidence
    )
    status, next_run_at = _determine_status_and_next_run(
        final_strategy,
        requires_human,
    )

    # 7. Build audit trail (preserving ingestion data + explicit source labels
    #    + additive AI metadata)
    # ------------------------------------------------------------------
    # Preserve the original audit-trail field names for backward compatibility
    # AND add explicit source/fallback_used/provider/model/prompt_version.
    classification_audit = {
        "category": diagnosis.category,
        "confidence": diagnosis.confidence,
        "reasoning": diagnosis.reasoning,
        "source": diagnosis_result.source,  # "ai" | "deterministic"
        "ai_used": ai_used,
        "fallback_used": diagnosis_result.fallback_used,
        "provider": diagnosis_result.provider,
        "model": diagnosis_result.model,
        "prompt_version": diagnosis_result.prompt_version,
    }
    # If the AI was not used, restore the deterministic classifier's fields so
    # the audit shape is unchanged from pre-16A.
    if not ai_used:
        det = classify_failure(payment_event.error_reason)
        classification_audit["rule_id"] = det.rule_id
        classification_audit["reason"] = det.reason

    recommendation_audit = {
        "strategy": recommendation.strategy,
        "confidence": recommendation.confidence,
        "reasoning": recommendation.reasoning,
        "source": recommendation_result.source,  # "ai" | "deterministic"
        "ai_used": rec_ai_used,
        "fallback_used": recommendation_result.fallback_used,
        "provider": recommendation_result.provider,
        "model": recommendation_result.model,
        "prompt_version": recommendation_result.prompt_version,
    }
    if not rec_ai_used:
        det = recommend_strategy(
            category=failure_category,
            retry_count=rc.retry_count,
            amount_paise=payment_event.amount_paise,
        )
        recommendation_audit["reasoning_summary"] = det.reasoning_summary
        recommendation_audit["risk_flags"] = det.risk_flags
        recommendation_audit["requires_human_review"] = det.requires_human_review
        recommendation_audit["provider"] = "deterministic"
        recommendation_audit["provider_deterministic"] = det.provider
    policy_audit = {
        "approved": policy_decision.approved,
        "final_strategy": policy_decision.final_strategy.value,
        "requires_human_approval": policy_decision.requires_human_approval,
        "violations": policy_decision.violations,
        "applied_rules": policy_decision.applied_rules,
        "policy_reason": policy_decision.policy_reason,
    }
    ai_audit = {
        "provider": settings.LLM_PROVIDER,
        "model": settings.LLM_MODEL_DIAGNOSIS,
        "diagnosis_confidence_threshold": settings.LLM_CONFIDENCE_THRESHOLD,
        "requires_human_due_to_confidence": requires_human_due_to_confidence,
        "diagnosis_source": diagnosis_result.source,
        "recommendation_source": recommendation_result.source,
    }

    # Generate an operator-facing explanation for the AI advisory.
    ai_explanation = explain_decision(
        diagnosis=diagnosis,
        recommendation=recommendation,
        amount_paise=payment_event.amount_paise,
        currency=payment_event.currency,
    )
    if ai_used or rec_ai_used:
        ai_audit["explanation"] = ai_explanation

    updated_trail = _append_to_audit_trail(
        rc.decision_audit_trail or {},
        classification_audit,
        recommendation_audit,
        policy_audit,
        ai_audit,
    )

    # 8. Persist to RecoveryCase
    # ------------------------------------------------------------------
    rc.failure_category = failure_category.value
    rc.recovery_probability = recovery_probability
    rc.recommended_strategy = final_strategy.value
    rc.priority_score = priority_score
    rc.expected_value_paise = expected_value
    rc.status = status
    rc.next_run_at = next_run_at
    rc.requires_human_approval = requires_human
    rc.decision_audit_trail = updated_trail

    db.commit()
    db.refresh(rc)

    logger.info(
        "Decision engine completed for %s: status=%s strategy=%s",
        recovery_case_id,
        status,
        final_strategy.value,
    )

    return rc
