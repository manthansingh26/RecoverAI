"""Deterministic seed generator for evaluation batches (Milestone 16C.4).

Generates a reproducible batch of ``EvaluationCase`` records from a fixed
(seed, size) pair. The same seed always produces the same dataset.

KEY DESIGN (16C.4 — deterministic baseline signal repair):
  - hidden_failure_category is the internal ground truth used by the response
    model and is NEVER exposed to agents or decision prompts.
  - Observable evidence (failure_reason, gateway_description, customer_note,
    retry_history_summary) provides realistic, imperfect signals.
  - MANY-TO-MANY RELATIONSHIP:
      * Each hidden category can produce multiple observable patterns (category-aligned,
        ambiguous, and cross-category noise).
      * Each observable pattern can correspond to multiple hidden categories.
      * No single observable field uniquely reveals the hidden category.
  - Baseline B (Deterministic RecoverAI) can legitimately interpret recognized
    production failure reasons without exposing ground truth.
  - No LLM is called here. The generator is pure and deterministic.
"""

from app.evaluation.models import EvaluationCase

# ---------------------------------------------------------------------------
# Deterministic PRNG (splitmix64-style) — fully portable reproducibility.
# ---------------------------------------------------------------------------


class _DeterministicRandom:
    """Minimal, deterministic PRNG using a splitmix64-like hash."""

    def __init__(self, seed: int) -> None:
        self._state = seed & 0xFFFFFFFFFFFFFFFF

    def _next(self) -> int:
        self._state = (self._state + 0x9E3779B97F4A7C15) & 0xFFFFFFFFFFFFFFFF
        z = self._state
        z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & 0xFFFFFFFFFFFFFFFF
        z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & 0xFFFFFFFFFFFFFFFF
        return z ^ (z >> 31)

    def int_in_range(self, lo: int, hi: int) -> int:
        return lo + (self._next() % (hi - lo + 1))

    def pick(self, items: list[str]) -> str:
        return items[self._next() % len(items)]


# ---------------------------------------------------------------------------
# Ground-truth categories and distributions
# ---------------------------------------------------------------------------

_HIDDEN_CATEGORIES = ["TRANSIENT", "AUTHENTICATION", "HARD_FAILURE", "UNKNOWN"]
_CATEGORY_WEIGHTS = [40, 25, 15, 20]  # TRANSIENT, AUTH, HARD, UNKNOWN


# ---------------------------------------------------------------------------
# Observable failure reason pools
# Includes production classifier rules + ambiguous/generic reasons.
# ---------------------------------------------------------------------------

_TRANSIENT_REASONS = [
    "network_error",
    "bank_technical_error",
    "gateway_technical_error",
    "bank_cutoff_in_progress",
]

_AUTH_REASONS = [
    "authentication_failed",
    "authorisation_declined_by_psp",
]

_HARD_REASONS = [
    "debit_instrument_blocked",
    "beneficiary_account_dormant",
]

_AMBIGUOUS_REASONS = [
    "payment_failed",
    "transaction_declined",
    "processing_error",
    "bank_returned_error",
    "gateway_timeout",
    "verification_required",
    "account_restriction",
    "insufficient_balance",
    "instrument_rejected",
    "connectivity_lost",
]


# ---------------------------------------------------------------------------
# Observable evidence pools (descriptions, notes, summaries)
# ---------------------------------------------------------------------------

_GENERIC_DESCRIPTIONS = [
    "Payment failed during transaction processing",
    "Transaction could not be completed at this time",
    "Payment processing error reported by switch",
    "Unable to process payment at this time",
    "Payment declined by upstream processor",
    "Bank returned an unhandled error response",
    "Temporary processing issue encountered",
    "Request timed out during processing cycle",
    "Card network returned transaction declined",
    "Payment transaction failed validation check",
]

_TRANSIENT_GATEWAY_DESCRIPTIONS = [
    "Gateway timeout during switch routing",
    "Bank connection reset by peer during authorization",
    "PSP reported upstream latency spike",
    "Temporary service disruption at issuing bank switch",
    "Incomplete gateway response due to connection drop",
    "Network connectivity lost during switch handshake",
]

_AUTH_GATEWAY_DESCRIPTIONS = [
    "Customer 3DS challenge verification incomplete",
    "OTP entry expired on issuer page",
    "PSP reported customer verification timeout",
    "Cardholder second factor verification failed",
    "Issuer ACS returned verification failure response",
    "3DS transaction declined during customer verification",
]

_HARD_GATEWAY_DESCRIPTIONS = [
    "Issuer reported card or instrument blocked",
    "Beneficiary account marked inactive or dormant",
    "Instrument permanently rejected by issuing bank",
    "Card reported lost or restricted by issuer",
    "Account frozen by issuing financial institution",
]

_AMBIGUOUS_GATEWAY_DESCRIPTIONS = [
    "Gateway returned generic failure status",
    "PSP reported transaction failure code 99",
    "Issuer declined transaction without specific subcode",
    "Switch declined payment processing request",
    "Transaction terminated without authorization confirmation",
    "Gateway returned an unmapped error response",
]

_TRANSIENT_CUSTOMER_NOTES = [
    "Customer reports seeing network error on banking app",
    "Customer mentioned app froze during checkout",
    "Customer says bank server seemed slow when paying",
    "Customer says payment was stuck on loading screen",
]

_AUTH_CUSTOMER_NOTES = [
    "Customer says OTP never arrived on phone",
    "Customer mentioned entering incorrect OTP code",
    "Customer asked for payment link to try UPI",
    "Customer says 3DS verification page failed to load",
]

_HARD_CUSTOMER_NOTES = [
    "Customer mentioned old card might be expired or replaced",
    "Customer unsure if bank account is currently active",
    "Customer reports card was blocked by bank yesterday",
]

_AMBIGUOUS_CUSTOMER_NOTES = [
    "Customer called asking why transaction failed",
    "No customer note available",
    "Customer confused about error message",
    "Customer asked whether they should retry now",
    "Customer confirmed balance is sufficient",
]

_RETRY_HISTORY_SUMMARIES = [
    "First attempt",
    "Retried once after 5 minutes",
    "Retried after 15 minutes",
    "Multiple retries over 1 hour",
    "Retried next day",
    "Three attempts across two days",
    "Single retry after brief wait",
    "Two prior attempts with same payment method",
]


def _pick_category(rng: _DeterministicRandom) -> str:
    """Pick a hidden category using the weighted distribution."""
    roll = rng.int_in_range(0, sum(_CATEGORY_WEIGHTS) - 1)
    cumulative = 0
    for cat, weight in zip(_HIDDEN_CATEGORIES, _CATEGORY_WEIGHTS):
        cumulative += weight
        if roll < cumulative:
            return cat
    return _HIDDEN_CATEGORIES[-1]


def _pick_failure_reason(hidden_cat: str, rng: _DeterministicRandom) -> str:
    """Sample an observable failure reason with realistic imperfect signal.

    Many-to-many guarantee:
    - Every reason can appear under multiple hidden categories.
    - Every hidden category can produce multiple reasons.
    - No failure reason uniquely identifies the hidden category.
    """
    roll = rng.int_in_range(0, 99)
    if hidden_cat == "TRANSIENT":
        if roll < 55:
            return rng.pick(_TRANSIENT_REASONS)
        elif roll < 85:
            return rng.pick(_AMBIGUOUS_REASONS)
        elif roll < 95:
            return rng.pick(_AUTH_REASONS)
        else:
            return rng.pick(_HARD_REASONS)
    elif hidden_cat == "AUTHENTICATION":
        if roll < 55:
            return rng.pick(_AUTH_REASONS)
        elif roll < 85:
            return rng.pick(_AMBIGUOUS_REASONS)
        elif roll < 95:
            return rng.pick(_TRANSIENT_REASONS)
        else:
            return rng.pick(_HARD_REASONS)
    elif hidden_cat == "HARD_FAILURE":
        if roll < 55:
            return rng.pick(_HARD_REASONS)
        elif roll < 85:
            return rng.pick(_AMBIGUOUS_REASONS)
        elif roll < 95:
            return rng.pick(_AUTH_REASONS)
        else:
            return rng.pick(_TRANSIENT_REASONS)
    else:  # UNKNOWN
        if roll < 55:
            return rng.pick(_AMBIGUOUS_REASONS)
        elif roll < 70:
            return rng.pick(_TRANSIENT_REASONS)
        elif roll < 85:
            return rng.pick(_AUTH_REASONS)
        else:
            return rng.pick(_HARD_REASONS)


def _pick_gateway_description(hidden_cat: str, rng: _DeterministicRandom) -> str:
    """Sample an observable gateway description with imperfect signal."""
    roll = rng.int_in_range(0, 99)
    if hidden_cat == "TRANSIENT":
        if roll < 50:
            return rng.pick(_TRANSIENT_GATEWAY_DESCRIPTIONS)
        elif roll < 80:
            return rng.pick(_AMBIGUOUS_GATEWAY_DESCRIPTIONS)
        elif roll < 90:
            return rng.pick(_AUTH_GATEWAY_DESCRIPTIONS)
        else:
            return rng.pick(_HARD_GATEWAY_DESCRIPTIONS)
    elif hidden_cat == "AUTHENTICATION":
        if roll < 50:
            return rng.pick(_AUTH_GATEWAY_DESCRIPTIONS)
        elif roll < 80:
            return rng.pick(_AMBIGUOUS_GATEWAY_DESCRIPTIONS)
        elif roll < 90:
            return rng.pick(_TRANSIENT_GATEWAY_DESCRIPTIONS)
        else:
            return rng.pick(_HARD_GATEWAY_DESCRIPTIONS)
    elif hidden_cat == "HARD_FAILURE":
        if roll < 50:
            return rng.pick(_HARD_GATEWAY_DESCRIPTIONS)
        elif roll < 80:
            return rng.pick(_AMBIGUOUS_GATEWAY_DESCRIPTIONS)
        elif roll < 90:
            return rng.pick(_AUTH_GATEWAY_DESCRIPTIONS)
        else:
            return rng.pick(_TRANSIENT_GATEWAY_DESCRIPTIONS)
    else:  # UNKNOWN
        if roll < 50:
            return rng.pick(_AMBIGUOUS_GATEWAY_DESCRIPTIONS)
        elif roll < 70:
            return rng.pick(_TRANSIENT_GATEWAY_DESCRIPTIONS)
        elif roll < 85:
            return rng.pick(_AUTH_GATEWAY_DESCRIPTIONS)
        else:
            return rng.pick(_HARD_GATEWAY_DESCRIPTIONS)


def _pick_customer_note(hidden_cat: str, rng: _DeterministicRandom) -> str:
    """Sample an observable customer note with imperfect signal."""
    roll = rng.int_in_range(0, 99)
    if hidden_cat == "TRANSIENT":
        if roll < 45:
            return rng.pick(_TRANSIENT_CUSTOMER_NOTES)
        elif roll < 85:
            return rng.pick(_AMBIGUOUS_CUSTOMER_NOTES)
        elif roll < 93:
            return rng.pick(_AUTH_CUSTOMER_NOTES)
        else:
            return rng.pick(_HARD_CUSTOMER_NOTES)
    elif hidden_cat == "AUTHENTICATION":
        if roll < 45:
            return rng.pick(_AUTH_CUSTOMER_NOTES)
        elif roll < 85:
            return rng.pick(_AMBIGUOUS_CUSTOMER_NOTES)
        elif roll < 93:
            return rng.pick(_TRANSIENT_CUSTOMER_NOTES)
        else:
            return rng.pick(_HARD_CUSTOMER_NOTES)
    elif hidden_cat == "HARD_FAILURE":
        if roll < 45:
            return rng.pick(_HARD_CUSTOMER_NOTES)
        elif roll < 85:
            return rng.pick(_AMBIGUOUS_CUSTOMER_NOTES)
        elif roll < 93:
            return rng.pick(_AUTH_CUSTOMER_NOTES)
        else:
            return rng.pick(_TRANSIENT_CUSTOMER_NOTES)
    else:  # UNKNOWN
        if roll < 60:
            return rng.pick(_AMBIGUOUS_CUSTOMER_NOTES)
        elif roll < 75:
            return rng.pick(_TRANSIENT_CUSTOMER_NOTES)
        elif roll < 90:
            return rng.pick(_AUTH_CUSTOMER_NOTES)
        else:
            return rng.pick(_HARD_CUSTOMER_NOTES)


def _generate_case(idx: int, rng: _DeterministicRandom) -> EvaluationCase:
    """Generate one EvaluationCase deterministically.

    CRITICAL: hidden_failure_category is internal ground truth. Observable
    fields provide realistic, imperfect signals with many-to-many mappings.
    """
    # 1. Hidden ground truth — used ONLY by response model / evaluator.
    hidden_cat = _pick_category(rng)

    # 2. Observable failure reason — imperfect signal mapped many-to-many.
    reason = _pick_failure_reason(hidden_cat, rng)

    # 3. Case attributes.
    is_high_value = rng.int_in_range(0, 99) < 20  # 20% high-value
    amount = (
        rng.int_in_range(500000, 5000000)
        if is_high_value
        else rng.int_in_range(5000, 500000)
    )
    retry = rng.int_in_range(0, 3)
    history = rng.int_in_range(0, 100)

    # 4. Observable evidence fields.
    failure_desc = rng.pick(_GENERIC_DESCRIPTIONS)
    gateway_desc = _pick_gateway_description(hidden_cat, rng)
    customer_note = _pick_customer_note(hidden_cat, rng)
    retry_summary = rng.pick(_RETRY_HISTORY_SUMMARIES)

    return EvaluationCase(
        case_id=f"eval_case_{idx:04d}",
        amount_paise=amount,
        currency="INR",
        failure_reason=reason,
        failure_description=failure_desc,
        retry_count=retry,
        high_value=is_high_value,
        customer_history_score=history,
        gateway_description=gateway_desc,
        customer_note=customer_note,
        retry_history_summary=retry_summary,
        hidden_failure_category=hidden_cat,
    )


def generate_evaluation_batch(size: int, seed: int = 42) -> list[EvaluationCase]:
    """Generate a deterministic batch of ``EvaluationCase`` records.

    Args:
        size: Number of cases to generate.
        seed: Any integer. The same seed always produces the same batch.

    Returns:
        A list of ``EvaluationCase`` objects, deterministically ordered.
    """
    if size < 1:
        raise ValueError(f"size must be >= 1, got {size}")
    rng = _DeterministicRandom(seed)
    return [_generate_case(i, rng) for i in range(size)]
