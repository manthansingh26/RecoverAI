"""RecoverAI evaluation framework (Milestone 16C.1).

A reproducible batch evaluation framework that measures whether RecoverAI
produces better revenue-recovery outcomes than deterministic baselines.

Three strategies compared:
- Baseline A: Retry Everything Except Hard Failures
- Baseline B: Deterministic RecoverAI (classifier + recommender + policy)
- AI-Augmented: Full RecoverAI with optional LLM
"""

from app.evaluation.models import (
    ComparisonReport,
    CustomerResponse,
    EvaluationCase,
    EvaluationResult,
    StrategyOutcome,
)
from app.evaluation.seed_generator import generate_evaluation_batch
from app.evaluation.response_model import simulate_customer_response
from app.evaluation.baseline import baseline_a_strategy, baseline_b_strategy, baseline_strategy_for_case
from app.evaluation.evaluator import run_batch_evaluation

__all__ = [
    "ComparisonReport",
    "CustomerResponse",
    "EvaluationCase",
    "EvaluationResult",
    "StrategyOutcome",
    "generate_evaluation_batch",
    "simulate_customer_response",
    "baseline_a_strategy",
    "baseline_b_strategy",
    "baseline_strategy_for_case",
    "run_batch_evaluation",
]
