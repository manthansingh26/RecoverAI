"""Tests for Milestone 16C.2 — Benchmark Validity Repair.

Covers all required integrity tests including hidden-label leak prevention,
many-to-many ground truth, observable evidence passthrough, explicit pairwise
uplift, and adversarial leak checks. Default tests make ZERO network calls.
"""

from unittest.mock import patch

import pytest

from app.evaluation.baseline import baseline_a_strategy, baseline_b_strategy
from app.evaluation.evaluator import run_batch_evaluation
from app.evaluation.models import (
    ComparisonReport,
    EvaluationCase,
    StrategyOutcome,
)
from app.evaluation.response_model import simulate_customer_response
from app.evaluation.seed_generator import generate_evaluation_batch
from app.models.enums import RecoveryStrategy


def _make_case(**kwargs) -> EvaluationCase:
    defaults = dict(
        case_id="c1", amount_paise=10000, currency="INR",
        failure_reason="payment_failed", failure_description="Payment failed",
        retry_count=0, high_value=False, customer_history_score=50,
        gateway_description="Gateway returned an error",
        customer_note="No customer note available",
        retry_history_summary="First attempt",
        hidden_failure_category="TRANSIENT",
    )
    defaults.update(kwargs)
    return EvaluationCase(**defaults)


# ---- 1. Same seed => identical complete batch ----
class TestSeedDeterminism:
    def test_same_seed_same_batch(self):
        b1 = generate_evaluation_batch(50, seed=42)
        b2 = generate_evaluation_batch(50, seed=42)
        assert len(b1) == len(b2) == 50
        for a, c in zip(b1, b2):
            assert a.case_id == c.case_id
            assert a.amount_paise == c.amount_paise
            assert a.failure_reason == c.failure_reason
            assert a.hidden_failure_category == c.hidden_failure_category
            assert a.gateway_description == c.gateway_description
            assert a.customer_note == c.customer_note
            assert a.retry_history_summary == c.retry_history_summary

    # ---- 2. Different seed => different batch ----
    def test_different_seed_different_batch(self):
        b1 = generate_evaluation_batch(50, seed=42)
        b2 = generate_evaluation_batch(50, seed=99)
        assert not all(
            a.amount_paise == c.amount_paise and a.failure_reason == c.failure_reason
            for a, c in zip(b1, b2)
        )


# ---- 3. Same failure_reason maps to multiple hidden categories ----
class TestManyToManyGroundTruth:
    def test_same_reason_multiple_categories(self):
        batch = generate_evaluation_batch(500, seed=77)
        reason_to_cats: dict[str, set[str]] = {}
        for c in batch:
            reason_to_cats.setdefault(c.failure_reason, set()).add(c.hidden_failure_category)
        multi_cat_reasons = [r for r, cats in reason_to_cats.items() if len(cats) > 1]
        assert len(multi_cat_reasons) >= 3, (
            f"Expected >=3 reasons mapping to multiple categories, got {len(multi_cat_reasons)}"
        )

    # ---- 4. Same hidden category maps to multiple failure reasons ----
    def test_same_category_multiple_reasons(self):
        batch = generate_evaluation_batch(500, seed=88)
        cat_to_reasons: dict[str, set[str]] = {}
        for c in batch:
            cat_to_reasons.setdefault(c.hidden_failure_category, set()).add(c.failure_reason)
        for cat in ["TRANSIENT", "AUTHENTICATION", "HARD_FAILURE", "UNKNOWN"]:
            assert cat in cat_to_reasons, f"Missing category {cat}"
            assert len(cat_to_reasons[cat]) >= 2, (
                f"Category {cat} has only {len(cat_to_reasons[cat])} reason(s)"
            )


# ---- 5. Observable evidence does not directly expose hidden_failure_category ----
class TestHiddenLabelIsolation:
    def test_hidden_category_not_in_observable_repr(self):
        batch = generate_evaluation_batch(200, seed=55)
        for c in batch:
            hidden = c.hidden_failure_category
            assert hidden is not None
            # hidden_failure_category is excluded from repr (repr=False)
            assert f"hidden_failure_category='{hidden}'" not in repr(c)
            assert hidden not in c.case_id.upper()

    # ---- 6. hidden_failure_category never enters agent input ----
    def test_hidden_category_not_in_agent_context(self):
        from app.agents.diagnostician import _build_context
        case = _make_case(hidden_failure_category="TRANSIENT")
        ctx = _build_context(
            error_reason=case.failure_reason,
            error_description=case.failure_description,
            amount_paise=case.amount_paise,
            currency=case.currency,
            retry_count=case.retry_count,
            gateway_description=case.gateway_description,
            customer_note=case.customer_note,
            retry_history_summary=case.retry_history_summary,
        )
        ctx_str = str(ctx).upper()
        assert "TRANSIENT" not in ctx_str
        assert "HIDDEN" not in ctx_str
        assert "FAILURE_CATEGORY" not in ctx_str

    # ---- 7. Agent receives gateway/customer/retry evidence ----
    def test_agent_receives_observable_evidence(self):
        from app.agents.diagnostician import _build_context
        ctx = _build_context(
            error_reason="payment_failed",
            error_description="Payment failed",
            amount_paise=10000,
            currency="INR",
            retry_count=1,
            gateway_description="Bank connection issue",
            customer_note="Customer called about failed payment",
            retry_history_summary="Retried once after 5 minutes",
        )
        assert ctx["gateway_description"] == "Bank connection issue"
        assert ctx["customer_note"] == "Customer called about failed payment"
        assert ctx["retry_history_summary"] == "Retried once after 5 minutes"


# ---- 8-9. Baselines do not call LLM ----
class TestBaselinesLLMFree:
    def test_baseline_a_never_calls_llm(self):
        batch = generate_evaluation_batch(20, seed=60)
        with patch("app.agents.diagnostician.LLMProvider") as mock_prov:
            for c in batch:
                baseline_a_strategy(c)
            mock_prov.assert_not_called()

    def test_baseline_b_never_calls_llm(self):
        batch = generate_evaluation_batch(20, seed=61)
        with patch("app.agents.diagnostician.LLMProvider") as mock_prov:
            for c in batch:
                baseline_b_strategy(c)
            mock_prov.assert_not_called()


# ---- 10-11. All three arms use same batch and response model ----
class TestFairComparison:
    def test_all_strategies_same_case_count(self):
        batch = generate_evaluation_batch(25, seed=70)
        report = run_batch_evaluation(batch, enable_llm=False)
        n = len(batch)
        assert report.baseline_a.total_cases == n
        assert report.baseline_b.total_cases == n
        assert report.ai_augmented.total_cases == n

    def test_shared_response_model(self):
        case = _make_case()
        strategy = RecoveryStrategy.WAIT_AND_RETRY.value
        r1 = simulate_customer_response(case, strategy)
        r2 = simulate_customer_response(case, strategy)
        assert r1.customer_paid == r2.customer_paid
        assert r1.recovered_amount_paise == r2.recovered_amount_paise


# ---- 12. Pairwise uplift calculations correct ----
class TestPairwiseUplift:
    def test_uplift_formulas(self):
        batch = generate_evaluation_batch(30, seed=80)
        report = run_batch_evaluation(batch, enable_llm=False)
        net_a = report.baseline_a.net_recovered_paise
        net_b = report.baseline_b.net_recovered_paise
        net_ai = report.ai_augmented.net_recovered_paise

        expected_det_vs_retry = (net_b - net_a) / abs(net_a) if net_a != 0 else 0.0
        expected_ai_vs_det = (net_ai - net_b) / abs(net_b) if net_b != 0 else 0.0
        expected_ai_vs_retry = (net_ai - net_a) / abs(net_a) if net_a != 0 else 0.0

        assert report.uplift_det_vs_retry == pytest.approx(expected_det_vs_retry)
        assert report.uplift_ai_vs_det == pytest.approx(expected_ai_vs_det)
        assert report.uplift_ai_vs_retry == pytest.approx(expected_ai_vs_retry)

    def test_uplift_zero_denominator(self):
        from app.evaluation.evaluator import _safe_uplift
        assert _safe_uplift(100, 0) == 0.0
        assert _safe_uplift(-50, 0) == 0.0

    def test_uplift_positive_and_negative(self):
        from app.evaluation.evaluator import _safe_uplift
        assert _safe_uplift(200, 100) == pytest.approx(1.0)
        assert _safe_uplift(50, 100) == pytest.approx(-0.5)


# ---- 13. Deterministic evaluation remains reproducible ----
class TestReproducibility:
    def test_same_batch_twice_identical(self):
        batch = generate_evaluation_batch(30, seed=14)
        r1 = run_batch_evaluation(batch, enable_llm=False)
        r2 = run_batch_evaluation(batch, enable_llm=False)
        assert r1.ai_augmented.net_recovered_paise == r2.ai_augmented.net_recovered_paise
        assert r1.baseline_a.recovery_rate == r2.baseline_a.recovery_rate
        assert [o.customer_paid for o in r1.baseline_a.outcomes] == \
               [o.customer_paid for o in r2.baseline_a.outcomes]


# ---- Adversarial leak check ----
class TestAdversarialLeakCheck:
    def test_no_indirect_label_leakage(self):
        """Verify no field value uniquely identifies the hidden category."""
        batch = generate_evaluation_batch(500, seed=99)
        hidden_cats = {"TRANSIENT", "AUTHENTICATION", "HARD_FAILURE", "UNKNOWN"}

        for field_name in [
            "failure_reason", "failure_description",
            "gateway_description", "customer_note",
            "retry_history_summary",
        ]:
            value_to_cats: dict[str, set[str]] = {}
            for c in batch:
                val = getattr(c, field_name, None)
                if val is not None:
                    value_to_cats.setdefault(val, set()).add(c.hidden_failure_category)
            for val, cats in value_to_cats.items():
                assert len(cats) > 1 or cats.isdisjoint(hidden_cats), (
                    f"Field '{field_name}' value '{val}' uniquely maps to {cats}"
                )

    def test_case_id_does_not_encode_category(self):
        batch = generate_evaluation_batch(100, seed=101)
        for c in batch:
            assert c.hidden_failure_category not in c.case_id.upper()


# ---- Metric calculations ----
class TestMetricCalculations:
    def test_recovery_rate_computed_correctly(self):
        batch = generate_evaluation_batch(20, seed=30)
        report = run_batch_evaluation(batch, enable_llm=False)
        result = report.baseline_a
        paid = sum(1 for o in result.outcomes if o.customer_paid)
        assert result.recovery_rate == pytest.approx(paid / result.total_cases)

    def test_net_recovered_is_integer_arithmetic(self):
        batch = generate_evaluation_batch(20, seed=31)
        report = run_batch_evaluation(batch, enable_llm=False)
        for outcome in report.baseline_a.outcomes:
            expected = outcome.recovered_amount_paise - outcome.outreach_cost_paise - outcome.churn_cost_paise
            assert outcome.net_recovered_paise == expected


# ---- No DB mutation ----
class TestNoDBMutation:
    def test_evaluator_does_not_mutate_production_db(self, db_session):
        from app.models.payment_event import PaymentEvent
        from app.models.recovery_case import RecoveryCase
        from sqlalchemy import select

        before_cases = db_session.execute(select(RecoveryCase.id)).scalars().all()
        before_events = db_session.execute(select(PaymentEvent.id)).scalars().all()

        batch = generate_evaluation_batch(10, seed=15)
        report = run_batch_evaluation(batch, enable_llm=False)
        assert report.ai_augmented.total_cases == 10

        after_cases = db_session.execute(select(RecoveryCase.id)).scalars().all()
        after_events = db_session.execute(select(PaymentEvent.id)).scalars().all()
        assert len(after_cases) == len(before_cases)
        assert len(after_events) == len(before_events)


# ---- No secrets ----
class TestNoSecrets:
    def test_no_secrets_in_evaluation_records(self):
        batch = generate_evaluation_batch(10, seed=16)
        from app.core.config import settings as cfg
        with patch.object(cfg, "LLM_API_KEY", "super-secret-key-99"):
            report = run_batch_evaluation(batch, enable_llm=True)
        serialized = str(report.to_dict())
        assert "super-secret-key-99" not in serialized


# ---- Comparison report structure ----
class TestComparisonReport:
    def test_report_has_three_strategies(self):
        batch = generate_evaluation_batch(15, seed=50)
        report = run_batch_evaluation(batch, enable_llm=False)
        assert isinstance(report, ComparisonReport)
        assert report.baseline_a.strategy_name == "retry_everything"
        assert report.baseline_b.strategy_name == "deterministic_recoverai"
        assert report.ai_augmented.strategy_name == "ai_augmented"


# ---- Live guard ----
class TestLiveGuard:
    def test_default_suite_no_network(self):
        from app.core.config import settings
        assert settings.LLM_LIVE_TEST is False
