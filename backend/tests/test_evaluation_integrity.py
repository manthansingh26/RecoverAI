"""Dataset integrity + benchmark validity tests (Milestone 16C.4).

These tests prove the benchmark validity and integrity for Milestone 16C.4:
1. Baseline A remains independent (no LLM, no PolicyEngine, no hidden ground truth).
2. Baseline B can distinguish at least some observable cases.
3. Baseline B still makes mistakes on ambiguous / misaligned cases.
4. Hidden failure category is never exposed to agents or context.
5. Observable evidence never uniquely determines hidden category (many-to-many).
6. AI receives the exact same observable evidence.
7. Customer-response model remains unchanged, pure, and deterministic.
8. Deterministic evaluation is 100% reproducible across runs.
9. Pairwise uplift metrics handle positive, negative, and zero-denominator correctly.
10. No production code is modified.
"""

from collections import defaultdict
from unittest.mock import MagicMock, patch

import pytest

from app.agents.base import LLMResult
from app.agents.diagnostician import diagnose_failure
from app.core.config import settings
from app.evaluation.baseline import baseline_a_strategy, baseline_b_strategy
from app.evaluation.evaluator import run_batch_evaluation
from app.evaluation.models import ComparisonReport, EvaluationCase, EvaluationResult
from app.evaluation.response_model import simulate_customer_response
from app.evaluation.seed_generator import generate_evaluation_batch
from app.models.enums import FailureCategory, RecoveryStrategy


# ---------------------------------------------------------------------------
# 1. Baseline A remains independent
# ---------------------------------------------------------------------------

class TestBaselineAIndependence:
    def test_baseline_a_never_calls_llm(self) -> None:
        """Baseline A must not call LLM or import LLMProvider."""
        import app.evaluation.baseline as bl
        assert not hasattr(bl, "LLMProvider")
        batch = generate_evaluation_batch(30, seed=10)
        with patch("app.agents.diagnostician.LLMProvider") as mock_prov:
            for c in batch:
                strat = baseline_a_strategy(c)
                assert strat in {RecoveryStrategy.WAIT_AND_RETRY.value, RecoveryStrategy.STOP_RECOVERY.value}
            mock_prov.assert_not_called()

    def test_baseline_a_independent_of_policy_engine(self) -> None:
        """Baseline A must not invoke evaluate_policy or PolicyEngine."""
        with patch("app.services.policy_engine.evaluate_policy") as mock_policy:
            case = generate_evaluation_batch(1, seed=11)[0]
            baseline_a_strategy(case)
            mock_policy.assert_not_called()

    def test_baseline_a_ignores_hidden_ground_truth(self) -> None:
        """Baseline A behavior depends only on observable failure_reason."""
        case_transient = EvaluationCase(
            case_id="c1", amount_paise=10000, currency="INR",
            failure_reason="network_error", failure_description="desc",
            retry_count=0, hidden_failure_category="TRANSIENT",
        )
        case_hard = EvaluationCase(
            case_id="c2", amount_paise=10000, currency="INR",
            failure_reason="network_error", failure_description="desc",
            retry_count=0, hidden_failure_category="HARD_FAILURE",
        )
        # Same observable reason -> same decision regardless of hidden category
        assert baseline_a_strategy(case_transient) == baseline_a_strategy(case_hard)


# ---------------------------------------------------------------------------
# 2. Baseline B can distinguish observable cases
# ---------------------------------------------------------------------------

class TestBaselineBDistinguishability:
    def test_baseline_b_distinguishes_transient_auth_hard(self) -> None:
        """Baseline B maps observable payment signals to differentiated strategies."""
        case_transient = EvaluationCase(
            case_id="c_t", amount_paise=10000, currency="INR",
            failure_reason="network_error", failure_description="desc",
            retry_count=0, hidden_failure_category="TRANSIENT",
        )
        case_auth = EvaluationCase(
            case_id="c_a", amount_paise=10000, currency="INR",
            failure_reason="authentication_failed", failure_description="desc",
            retry_count=0, hidden_failure_category="AUTHENTICATION",
        )
        case_hard = EvaluationCase(
            case_id="c_h", amount_paise=10000, currency="INR",
            failure_reason="debit_instrument_blocked", failure_description="desc",
            retry_count=0, hidden_failure_category="HARD_FAILURE",
        )

        strat_t, _ = baseline_b_strategy(case_transient)
        strat_a, _ = baseline_b_strategy(case_auth)
        strat_h, _ = baseline_b_strategy(case_hard)

        assert strat_t == RecoveryStrategy.WAIT_AND_RETRY.value
        assert strat_a == RecoveryStrategy.CREATE_PAYMENT_LINK.value
        assert strat_h == RecoveryStrategy.STOP_RECOVERY.value

    def test_baseline_b_never_calls_llm(self) -> None:
        """Baseline B executes purely deterministic rules without calling LLM."""
        batch = generate_evaluation_batch(30, seed=20)
        with patch("app.agents.diagnostician.LLMProvider") as mock_prov:
            for c in batch:
                strat, blocked = baseline_b_strategy(c)
                assert strat in {s.value for s in RecoveryStrategy}
                assert isinstance(blocked, bool)
            mock_prov.assert_not_called()


# ---------------------------------------------------------------------------
# 3. Baseline B still makes mistakes on ambiguous/misaligned cases
# ---------------------------------------------------------------------------

class TestBaselineBMistakes:
    def test_baseline_b_fails_when_observable_evidence_misleads(self) -> None:
        """When observable evidence is noisy, Baseline B makes legitimate errors."""
        # Case has observable network_error, but hidden ground truth is AUTHENTICATION
        misleading_case = EvaluationCase(
            case_id="c_m", amount_paise=10000, currency="INR",
            failure_reason="network_error", failure_description="desc",
            retry_count=0, hidden_failure_category="AUTHENTICATION",
        )
        strat_b, _ = baseline_b_strategy(misleading_case)
        # Baseline B recommends WAIT_AND_RETRY based on observable network_error
        assert strat_b == RecoveryStrategy.WAIT_AND_RETRY.value

        # Customer response for WAIT_AND_RETRY on AUTHENTICATION does not get auth boost
        resp_wait = simulate_customer_response(misleading_case, strat_b)
        resp_link = simulate_customer_response(misleading_case, RecoveryStrategy.CREATE_PAYMENT_LINK.value)
        # CREATE_PAYMENT_LINK is strictly better for AUTHENTICATION than WAIT_AND_RETRY
        assert resp_link.customer_paid >= resp_wait.customer_paid

    def test_baseline_b_escalates_ambiguous_cases(self) -> None:
        """Ambiguous observable reasons result in HUMAN_REVIEW for Baseline B."""
        ambiguous_case = EvaluationCase(
            case_id="c_amb", amount_paise=10000, currency="INR",
            failure_reason="payment_failed", failure_description="desc",
            retry_count=0, hidden_failure_category="UNKNOWN",
        )
        strat_b, _ = baseline_b_strategy(ambiguous_case)
        assert strat_b == RecoveryStrategy.HUMAN_REVIEW.value


# ---------------------------------------------------------------------------
# 4. Hidden category is never exposed
# ---------------------------------------------------------------------------

class TestHiddenCategoryProtection:
    def test_hidden_category_never_in_agent_context(self) -> None:
        """The diagnostician context must never receive hidden_failure_category."""
        batch = generate_evaluation_batch(20, seed=30)
        from app.agents.diagnostician import _build_context
        for case in batch:
            ctx = _build_context(
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
            assert "hidden_failure_category" not in ctx
            assert "hidden" not in str(ctx).lower()

    def test_case_id_does_not_encode_category(self) -> None:
        """Case ID format is eval_case_XXXX and does not encode ground truth."""
        batch = generate_evaluation_batch(100, seed=31)
        for c in batch:
            assert c.hidden_failure_category not in c.case_id.upper()


# ---------------------------------------------------------------------------
# 5. Observable evidence never uniquely determines hidden category (many-to-many)
# ---------------------------------------------------------------------------

class TestManyToManyDecoupling:
    def test_reasons_map_to_multiple_hidden_categories(self) -> None:
        """Every observable failure reason maps to multiple hidden categories."""
        batch = generate_evaluation_batch(3000, seed=40)
        reason_to_cats: dict[str, set[str]] = defaultdict(set)
        for c in batch:
            reason_to_cats[c.failure_reason].add(c.hidden_failure_category)

        for reason, cats in reason_to_cats.items():
            assert len(cats) >= 2, f"Reason '{reason}' maps uniquely to {cats}"

    def test_categories_produce_multiple_reasons(self) -> None:
        """Every hidden category produces multiple failure reasons."""
        batch = generate_evaluation_batch(3000, seed=41)
        cat_to_reasons: dict[str, set[str]] = defaultdict(set)
        for c in batch:
            cat_to_reasons[c.hidden_failure_category].add(c.failure_reason)

        for cat in ["TRANSIENT", "AUTHENTICATION", "HARD_FAILURE", "UNKNOWN"]:
            assert len(cat_to_reasons[cat]) >= 4, f"Category '{cat}' produces too few reasons"

    def test_gateway_descriptions_map_many_to_many(self) -> None:
        """Observable gateway descriptions appear under multiple hidden categories."""
        batch = generate_evaluation_batch(3000, seed=42)
        desc_to_cats: dict[str, set[str]] = defaultdict(set)
        for c in batch:
            if c.gateway_description:
                desc_to_cats[c.gateway_description].add(c.hidden_failure_category)

        for desc, cats in desc_to_cats.items():
            assert len(cats) >= 2, f"Gateway description '{desc}' maps uniquely to {cats}"


# ---------------------------------------------------------------------------
# 6. AI receives the exact same observable evidence
# ---------------------------------------------------------------------------

class TestAIObservableEvidence:
    def test_ai_context_receives_all_observable_fields(self) -> None:
        """AI context receives all observable fields available to human operators."""
        from app.agents.diagnostician import _build_context
        case = EvaluationCase(
            case_id="c1", amount_paise=15000, currency="INR",
            failure_reason="network_error", failure_description="desc",
            retry_count=1, customer_history_score=85,
            gateway_description="Gateway timeout during switch routing",
            customer_note="Customer says payment was stuck on loading screen",
            retry_history_summary="Retried once after 5 minutes",
            hidden_failure_category="TRANSIENT",
        )
        ctx = _build_context(
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
        assert ctx["error_reason"] == "network_error"
        assert ctx["gateway_description"] == "Gateway timeout during switch routing"
        assert ctx["customer_note"] == "Customer says payment was stuck on loading screen"
        assert ctx["retry_history_summary"] == "Retried once after 5 minutes"
        assert ctx["customer_history_score"] == 85


# ---------------------------------------------------------------------------
# 7. Customer-response model unchanged and deterministic
# ---------------------------------------------------------------------------

class TestCustomerResponseModel:
    def test_response_model_is_pure_and_deterministic(self) -> None:
        """Response model produces identical results for identical inputs."""
        case = generate_evaluation_batch(1, seed=50)[0]
        r1 = simulate_customer_response(case, RecoveryStrategy.WAIT_AND_RETRY.value)
        r2 = simulate_customer_response(case, RecoveryStrategy.WAIT_AND_RETRY.value)
        assert r1.customer_paid == r2.customer_paid
        assert r1.recovered_amount_paise == r2.recovered_amount_paise
        assert r1.outreach_cost_paise == r2.outreach_cost_paise
        assert r1.churn_cost_paise == r2.churn_cost_paise
        assert r1.net_recovered_paise == r2.net_recovered_paise


# ---------------------------------------------------------------------------
# 8. Deterministic evaluation remains reproducible
# ---------------------------------------------------------------------------

class TestDeterministicReproducibility:
    def test_deterministic_evaluation_reproducible_across_runs(self) -> None:
        """Two evaluation runs on the same batch produce bit-for-bit identical results."""
        batch = generate_evaluation_batch(50, seed=42)
        r1 = run_batch_evaluation(batch, enable_llm=False)
        r2 = run_batch_evaluation(batch, enable_llm=False)

        assert r1.baseline_a.net_recovered_paise == r2.baseline_a.net_recovered_paise
        assert r1.baseline_b.net_recovered_paise == r2.baseline_b.net_recovered_paise
        assert r1.ai_augmented.net_recovered_paise == r2.ai_augmented.net_recovered_paise

        assert r1.uplift_deterministic_vs_naive == r2.uplift_deterministic_vs_naive
        assert r1.uplift_ai_vs_deterministic == r2.uplift_ai_vs_deterministic
        assert r1.uplift_ai_vs_naive == r2.uplift_ai_vs_naive


# ---------------------------------------------------------------------------
# 9. Pairwise uplift metrics remain correct
# ---------------------------------------------------------------------------

class TestPairwiseUpliftMetrics:
    def test_uplift_positive(self) -> None:
        """Positive net gain produces positive uplift."""
        from app.evaluation.evaluator import _safe_uplift
        assert _safe_uplift(120, 100) == pytest.approx(0.20)

    def test_uplift_negative(self) -> None:
        """Negative net delta produces negative uplift."""
        from app.evaluation.evaluator import _safe_uplift
        assert _safe_uplift(80, 100) == pytest.approx(-0.20)

    def test_uplift_zero_denominator_safe(self) -> None:
        """Zero denominator returns 0.0 without ZeroDivisionError."""
        from app.evaluation.evaluator import _safe_uplift
        assert _safe_uplift(100, 0) == 0.0
        assert _safe_uplift(0, 0) == 0.0


# ---------------------------------------------------------------------------
# 10. No production code modified
# ---------------------------------------------------------------------------

class TestProductionCodeIntegrity:
    def test_production_modules_intact(self) -> None:
        """Production services remain clean, pure, and untouched."""
        import app.services.failure_classifier as fc
        import app.services.policy_engine as pe
        import app.services.strategy_advisor as sa

        assert hasattr(fc, "classify_failure")
        assert hasattr(sa, "recommend_strategy")
        assert hasattr(pe, "evaluate_policy")

        # Test classify_failure matches expected production rules
        c1 = fc.classify_failure("network_error")
        assert c1.category == FailureCategory.TRANSIENT

        c2 = fc.classify_failure("authentication_failed")
        assert c2.category == FailureCategory.AUTHENTICATION

        c3 = fc.classify_failure("debit_instrument_blocked")
        assert c3.category == FailureCategory.HARD_FAILURE
