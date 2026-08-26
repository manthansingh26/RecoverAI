"""Strongly typed domain models for the Milestone 16C.1 evaluation framework.

All money is integer paise — never floats — matching the production system's
money representation (ADR-011).
"""

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class EvaluationCase:
    """A single synthetic payment-failure case fed to all strategies.

    Contains ONLY information available at decision time. Ground-truth
    outcomes are NOT part of the case. The hidden_failure_category is
    internal — never passed to any agent or decision prompt.
    """

    case_id: str
    amount_paise: int
    currency: str
    failure_reason: str | None
    failure_description: str | None
    retry_count: int
    high_value: bool = False
    customer_history_score: int = 0

    # Observable evidence fields (Task 3: noisy/ambiguous surface data).
    gateway_description: str | None = None
    customer_note: str | None = None
    retry_history_summary: str | None = None

    # Internal ground truth — NEVER fed to agents.
    hidden_failure_category: str | None = field(
        default=None, repr=False, metadata={"internal": True, "do_not_feed_to_agent": True}
    )


@dataclass(frozen=True)
class CustomerResponse:
    """Frozen, deterministic outcome of a recovery action on a case."""

    customer_paid: bool
    recovered_amount_paise: int
    outreach_cost_paise: int
    churn_cost_paise: int

    @property
    def net_recovered_paise(self) -> int:
        return self.recovered_amount_paise - self.outreach_cost_paise - self.churn_cost_paise


@dataclass(frozen=True)
class StrategyOutcome:
    """Full recorded outcome of one case under one strategy.

    Provenance fields come from the actual AdvisoryResult, not inferred.
    """

    case_id: str
    amount_paise: int
    strategy: str
    # True per-case AI provenance (Task 1): from AdvisoryResult directly.
    diagnosis_source: str
    diagnosis_used_llm: bool
    diagnosis_fallback_used: bool
    recommendation_source: str
    recommendation_used_llm: bool
    recommendation_fallback_used: bool
    # True policy block measurement (Task 2): from PolicyDecision directly.
    ai_recommended_strategy: str
    policy_final_strategy: str
    policy_blocked: bool
    escalated_to_human: bool
    safe_stop: bool
    customer_paid: bool
    recovered_amount_paise: int
    outreach_cost_paise: int
    churn_cost_paise: int
    net_recovered_paise: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "net_recovered_paise",
            self.recovered_amount_paise - self.outreach_cost_paise - self.churn_cost_paise,
        )


@dataclass
class EvaluationResult:
    """Aggregate metrics for one strategy across a full batch.

    All money in integer paise. Separate results per strategy.
    """

    strategy_name: str
    total_cases: int
    gross_at_risk_paise: int
    recovered_amount_paise: int
    net_recovered_paise: int
    recovery_rate: float
    escalation_rate: float
    safe_stop_rate: float
    policy_block_rate: float
    ai_usage_rate: float
    ai_fallback_rate: float
    outreach_cost_paise: int
    churn_cost_paise: int
    cost_per_recovered_rupee: float
    outcomes: list[StrategyOutcome] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "strategy_name": self.strategy_name,
            "total_cases": self.total_cases,
            "gross_at_risk_paise": self.gross_at_risk_paise,
            "recovered_amount_paise": self.recovered_amount_paise,
            "net_recovered_paise": self.net_recovered_paise,
            "recovery_rate": self.recovery_rate,
            "escalation_rate": self.escalation_rate,
            "safe_stop_rate": self.safe_stop_rate,
            "policy_block_rate": self.policy_block_rate,
            "ai_usage_rate": self.ai_usage_rate,
            "ai_fallback_rate": self.ai_fallback_rate,
            "outreach_cost_paise": self.outreach_cost_paise,
            "churn_cost_paise": self.churn_cost_paise,
            "cost_per_recovered_rupee": self.cost_per_recovered_rupee,
            "outcome_count": len(self.outcomes),
        }


@dataclass
class ComparisonReport:
    """Three-way comparison across Baseline A, Baseline B, and AI-Augmented.

    Pairwise uplifts are explicit and computed safely (zero denominators -> 0).
    All uplift uses net_recovered_paise unless documented otherwise.
    """

    baseline_a: EvaluationResult
    baseline_b: EvaluationResult
    ai_augmented: EvaluationResult
    # Explicit pairwise uplifts on net_recovered_paise.
    uplift_ai_vs_naive: float = 0.0
    uplift_ai_vs_deterministic: float = 0.0
    uplift_deterministic_vs_naive: float = 0.0
    # Backward-compat aliases (older consumers).
    uplift_det_vs_retry: float = 0.0
    uplift_ai_vs_det: float = 0.0
    uplift_ai_vs_retry: float = 0.0

    def __post_init__(self) -> None:
        # Compute the canonical pairwise uplifts from the three results.
        a_net = self.baseline_a.net_recovered_paise
        b_net = self.baseline_b.net_recovered_paise
        ai_net = self.ai_augmented.net_recovered_paise

        self.uplift_ai_vs_naive = _safe_uplift(ai_net, a_net)
        self.uplift_ai_vs_deterministic = _safe_uplift(ai_net, b_net)
        self.uplift_deterministic_vs_naive = _safe_uplift(b_net, a_net)

        # Keep backward-compat aliases in sync.
        self.uplift_det_vs_retry = self.uplift_deterministic_vs_naive
        self.uplift_ai_vs_det = self.uplift_ai_vs_deterministic
        self.uplift_ai_vs_retry = self.uplift_ai_vs_naive

    def to_dict(self) -> dict[str, Any]:
        return {
            "baseline_a": self.baseline_a.to_dict(),
            "baseline_b": self.baseline_b.to_dict(),
            "ai_augmented": self.ai_augmented.to_dict(),
            "uplift_ai_vs_naive": self.uplift_ai_vs_naive,
            "uplift_ai_vs_deterministic": self.uplift_ai_vs_deterministic,
            "uplift_deterministic_vs_naive": self.uplift_deterministic_vs_naive,
            "uplift_deterministic_vs_retry_everything": self.uplift_det_vs_retry,
            "uplift_ai_vs_deterministic_alias": self.uplift_ai_vs_det,
            "uplift_ai_vs_retry_everything": self.uplift_ai_vs_retry,
        }


def _safe_uplift(numerator_net: int, denominator_net: int) -> float:
    """(num - den) / den when den != 0, else 0.0 (safe)."""
    if denominator_net == 0:
        return 0.0
    return (numerator_net - denominator_net) / denominator_net
