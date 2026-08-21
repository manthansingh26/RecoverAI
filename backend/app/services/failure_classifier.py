"""Deterministic failure classification based on error_reason.

Classifies payment failures into FailureCategory using explicit normalized
mappings. No LLM or external dependency is used.
"""

from dataclasses import dataclass

from app.models.enums import FailureCategory

# Mapping from normalized error_reason to (FailureCategory, rule_id, confidence)
_REASON_MAP: dict[str, tuple[FailureCategory, str, float]] = {
    # TRANSIENT
    "bank_technical_error": (FailureCategory.TRANSIENT, "rule_transient_bank_tech", 0.9),
    "gateway_technical_error": (FailureCategory.TRANSIENT, "rule_transient_gateway_tech", 0.9),
    "bank_cutoff_in_progress": (FailureCategory.TRANSIENT, "rule_transient_bank_cutoff", 0.85),
    "network_error": (FailureCategory.TRANSIENT, "rule_transient_network", 0.8),
    # AUTHENTICATION
    "authentication_failed": (FailureCategory.AUTHENTICATION, "rule_auth_failed", 0.95),
    "authorisation_declined_by_psp": (FailureCategory.AUTHENTICATION, "rule_auth_declined_psp", 0.95),
    # HARD_FAILURE
    "debit_instrument_blocked": (FailureCategory.HARD_FAILURE, "rule_hard_instrument_blocked", 0.95),
    "beneficiary_account_dormant": (FailureCategory.HARD_FAILURE, "rule_hard_beneficiary_dormant", 0.9),
}


@dataclass(frozen=True)
class FailureClassification:
    """Structured result of failure classification."""

    category: FailureCategory
    confidence: float
    rule_id: str
    reason: str


def classify_failure(error_reason: str | None) -> FailureClassification:
    """Classify a payment failure based on its error_reason.

    Normalizes input safely:
    - handles None
    - trims whitespace
    - case-normalizes to lowercase
    - falls back to UNKNOWN for unmapped values

    Args:
        error_reason: The error_reason string from the payment event.

    Returns:
        FailureClassification with category, confidence, rule_id, and reason.
    """
    if error_reason is None:
        return FailureClassification(
            category=FailureCategory.UNKNOWN,
            confidence=0.0,
            rule_id="rule_unknown_missing",
            reason="error_reason is None",
        )

    normalized = error_reason.strip().lower()

    if not normalized:
        return FailureClassification(
            category=FailureCategory.UNKNOWN,
            confidence=0.0,
            rule_id="rule_unknown_empty",
            reason="error_reason is empty after normalization",
        )

    match = _REASON_MAP.get(normalized)
    if match is not None:
        category, rule_id, confidence = match
        return FailureClassification(
            category=category,
            confidence=confidence,
            rule_id=rule_id,
            reason=f"Matched '{normalized}' to {category.value}",
        )

    return FailureClassification(
        category=FailureCategory.UNKNOWN,
        confidence=0.0,
        rule_id="rule_unknown_unmapped",
        reason=f"Unmapped error_reason: '{normalized}'",
    )
