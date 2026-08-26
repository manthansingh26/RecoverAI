"""Milestone 16C.7.2: Comprehensive Unit Tests for Combined Evaluation Call.

Zero external API calls. Tests schema validation, provenance, fallback,
confidence gating, observable context integrity, and PolicyEngine authority.
"""

from unittest.mock import MagicMock, patch
import pytest

from app.core.config import settings
from app.evaluation.models import EvaluationCase
from app.evaluation.evaluator import _evaluate_ai_combined_decision, run_batch_evaluation
from app.evaluation.seed_generator import generate_evaluation_batch
from app.models.enums import FailureCategory, RecoveryStrategy


@pytest.fixture
def sample_case():
    return EvaluationCase(
        case_id="eval_case_0001",
        amount_paise=5000,
        currency="INR",
        failure_reason="gateway_timeout",
        failure_description="Gateway timed out",
        retry_count=0,
        customer_history_score=85,
        retry_history_summary="No retries",
        gateway_description="Gateway timeout",
        customer_note="Customer tried paying",
        hidden_failure_category="TRANSIENT",
    )


class TestCombinedEvaluationCall:
    """Test suite for evaluation-only combined decision inference."""

    def test_combined_gemini_success_and_provenance(self, sample_case):
        """1 & 11: Combined Gemini success with true AI provenance."""
        fake_resp = MagicMock()
        fake_resp.status_code = 200
        fake_resp.json.return_value = {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {
                                "text": (
                                    '{"diagnosis": {"category": "TRANSIENT", "confidence": 0.95, "reasoning": "Timeout"}, '
                                    '"recommendation": {"strategy": "WAIT_AND_RETRY", "confidence": 0.90, "reasoning": "Backoff"}}'
                                )
                            }
                        ]
                    }
                }
            ]
        }
        with patch.object(settings, "LLM_PROVIDER", "gemini"):
            with patch.object(settings, "GEMINI_API_KEY", "fake-key"):
                with patch("httpx.post", return_value=fake_resp):
                    strat, prov = _evaluate_ai_combined_decision(sample_case, llm_available=True)
                    assert strat == RecoveryStrategy.WAIT_AND_RETRY.value
                    assert prov["diagnosis_source"] == "ai"
                    assert prov["diagnosis_used_llm"] is True
                    assert prov["diagnosis_fallback_used"] is False
                    assert prov["recommendation_source"] == "ai"
                    assert prov["recommendation_used_llm"] is True
                    assert prov["recommendation_fallback_used"] is False
                    assert prov["ai_recommended_strategy"] == RecoveryStrategy.WAIT_AND_RETRY.value

    def test_diagnosis_schema_validation(self, sample_case):
        """2: Diagnosis schema is validated strictly."""
        fake_resp = MagicMock()
        fake_resp.status_code = 200
        fake_resp.json.return_value = {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {
                                "text": '{"diagnosis": {"category": "TRANSIENT", "confidence": 0.95, "reasoning": "OK"}, "recommendation": {"strategy": "WAIT_AND_RETRY", "confidence": 0.9, "reasoning": "OK"}}'
                            }
                        ]
                    }
                }
            ]
        }
        with patch.object(settings, "LLM_PROVIDER", "gemini"):
            with patch.object(settings, "GEMINI_API_KEY", "fake-key"):
                with patch("httpx.post", return_value=fake_resp):
                    strat, prov = _evaluate_ai_combined_decision(sample_case, llm_available=True)
                    assert prov["diagnosis_source"] == "ai"

    def test_recommendation_schema_validation(self, sample_case):
        """3: Recommendation schema is validated strictly."""
        fake_resp = MagicMock()
        fake_resp.status_code = 200
        fake_resp.json.return_value = {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {
                                "text": '{"diagnosis": {"category": "HARD_FAILURE", "confidence": 0.99, "reasoning": "Stolen"}, "recommendation": {"strategy": "STOP_RECOVERY", "confidence": 0.99, "reasoning": "Stop"}}'
                            }
                        ]
                    }
                }
            ]
        }
        with patch.object(settings, "LLM_PROVIDER", "gemini"):
            with patch.object(settings, "GEMINI_API_KEY", "fake-key"):
                with patch("httpx.post", return_value=fake_resp):
                    strat, prov = _evaluate_ai_combined_decision(sample_case, llm_available=True)
                    assert strat == RecoveryStrategy.STOP_RECOVERY.value
                    assert prov["safe_stop"] is True

    def test_malformed_json_triggers_fallback(self, sample_case):
        """4: Malformed JSON cleanly falls back to deterministic."""
        fake_resp = MagicMock()
        fake_resp.status_code = 200
        fake_resp.json.return_value = {
            "candidates": [
                {"content": {"parts": [{"text": "THIS IS NOT JSON"}]}}
            ]
        }
        with patch.object(settings, "LLM_PROVIDER", "gemini"):
            with patch.object(settings, "GEMINI_API_KEY", "fake-key"):
                with patch("httpx.post", return_value=fake_resp):
                    strat, prov = _evaluate_ai_combined_decision(sample_case, llm_available=True)
                    assert prov["diagnosis_source"] == "deterministic"
                    assert prov["diagnosis_used_llm"] is False
                    assert prov["diagnosis_fallback_used"] is True

    def test_invalid_diagnosis_category_triggers_fallback(self, sample_case):
        """5: Unknown diagnosis category triggers schema fallback."""
        fake_resp = MagicMock()
        fake_resp.status_code = 200
        fake_resp.json.return_value = {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {
                                "text": '{"diagnosis": {"category": "INVALID_CAT", "confidence": 0.9, "reasoning": "Bad"}, "recommendation": {"strategy": "WAIT_AND_RETRY", "confidence": 0.9, "reasoning": "OK"}}'
                            }
                        ]
                    }
                }
            ]
        }
        with patch.object(settings, "LLM_PROVIDER", "gemini"):
            with patch.object(settings, "GEMINI_API_KEY", "fake-key"):
                with patch("httpx.post", return_value=fake_resp):
                    strat, prov = _evaluate_ai_combined_decision(sample_case, llm_available=True)
                    assert prov["diagnosis_source"] == "deterministic"
                    assert prov["diagnosis_fallback_used"] is True

    def test_invalid_recommendation_strategy_triggers_fallback(self, sample_case):
        """6: Unknown recommendation strategy triggers schema fallback."""
        fake_resp = MagicMock()
        fake_resp.status_code = 200
        fake_resp.json.return_value = {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {
                                "text": '{"diagnosis": {"category": "TRANSIENT", "confidence": 0.9, "reasoning": "OK"}, "recommendation": {"strategy": "INVALID_STRAT", "confidence": 0.9, "reasoning": "Bad"}}'
                            }
                        ]
                    }
                }
            ]
        }
        with patch.object(settings, "LLM_PROVIDER", "gemini"):
            with patch.object(settings, "GEMINI_API_KEY", "fake-key"):
                with patch("httpx.post", return_value=fake_resp):
                    strat, prov = _evaluate_ai_combined_decision(sample_case, llm_available=True)
                    assert prov["recommendation_source"] == "deterministic"
                    assert prov["recommendation_fallback_used"] is True

    def test_low_confidence_diagnosis(self, sample_case):
        """7: Low confidence diagnosis is processed safely."""
        fake_resp = MagicMock()
        fake_resp.status_code = 200
        fake_resp.json.return_value = {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {
                                "text": '{"diagnosis": {"category": "UNKNOWN", "confidence": 0.3, "reasoning": "Unsure"}, "recommendation": {"strategy": "HUMAN_REVIEW", "confidence": 0.8, "reasoning": "Escalate"}}'
                            }
                        ]
                    }
                }
            ]
        }
        with patch.object(settings, "LLM_PROVIDER", "gemini"):
            with patch.object(settings, "GEMINI_API_KEY", "fake-key"):
                with patch("httpx.post", return_value=fake_resp):
                    strat, prov = _evaluate_ai_combined_decision(sample_case, llm_available=True)
                    assert strat == RecoveryStrategy.HUMAN_REVIEW.value
                    assert prov["escalated_to_human"] is True

    def test_low_confidence_recommendation_forces_human_review(self, sample_case):
        """8: Low confidence recommendation (<0.6) forces HUMAN_REVIEW."""
        fake_resp = MagicMock()
        fake_resp.status_code = 200
        fake_resp.json.return_value = {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {
                                "text": '{"diagnosis": {"category": "TRANSIENT", "confidence": 0.9, "reasoning": "OK"}, "recommendation": {"strategy": "WAIT_AND_RETRY", "confidence": 0.4, "reasoning": "Unsure"}}'
                            }
                        ]
                    }
                }
            ]
        }
        with patch.object(settings, "LLM_PROVIDER", "gemini"):
            with patch.object(settings, "GEMINI_API_KEY", "fake-key"):
                with patch("httpx.post", return_value=fake_resp):
                    strat, prov = _evaluate_ai_combined_decision(sample_case, llm_available=True)
                    assert strat == RecoveryStrategy.HUMAN_REVIEW.value
                    assert prov["escalated_to_human"] is True

    def test_provider_timeout_triggers_fallback(self, sample_case):
        """9: Provider timeout triggers deterministic fallback."""
        import httpx
        with patch.object(settings, "LLM_PROVIDER", "gemini"):
            with patch.object(settings, "GEMINI_API_KEY", "fake-key"):
                with patch("httpx.post", side_effect=httpx.TimeoutException("timeout")), patch("time.sleep"):
                    strat, prov = _evaluate_ai_combined_decision(sample_case, llm_available=True)
                    assert prov["diagnosis_source"] == "deterministic"
                    assert prov["diagnosis_fallback_used"] is True

    def test_provider_429_triggers_fallback(self, sample_case):
        """10: HTTP 429 triggers clean fallback without raising."""
        fake_resp = MagicMock()
        fake_resp.status_code = 429
        with patch.object(settings, "LLM_PROVIDER", "gemini"):
            with patch.object(settings, "GEMINI_API_KEY", "fake-key"):
                with patch("httpx.post", return_value=fake_resp), patch("time.sleep"):
                    strat, prov = _evaluate_ai_combined_decision(sample_case, llm_available=True)
                    assert prov["diagnosis_source"] == "deterministic"
                    assert prov["diagnosis_fallback_used"] is True

    def test_hidden_label_never_sent(self):
        """12: hidden_failure_category is strictly excluded from prompt."""
        captured_payloads = []

        case = EvaluationCase(
            case_id="eval_case_secret",
            amount_paise=5000,
            currency="INR",
            failure_reason="network_timeout",
            failure_description="Timeout",
            retry_count=0,
            customer_history_score=85,
            retry_history_summary="None",
            gateway_description="None",
            customer_note="None",
            hidden_failure_category="TOP_SECRET_HIDDEN_LABEL",
        )

        def mock_post(url, **kwargs):
            captured_payloads.append(kwargs.get("json", {}))
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = {
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {
                                    "text": '{"diagnosis": {"category": "TRANSIENT", "confidence": 0.9, "reasoning": "OK"}, "recommendation": {"strategy": "WAIT_AND_RETRY", "confidence": 0.9, "reasoning": "OK"}}'
                                }
                            ]
                        }
                    }
                ]
            }
            return resp

        with patch.object(settings, "LLM_PROVIDER", "gemini"):
            with patch.object(settings, "GEMINI_API_KEY", "fake-key"):
                with patch("httpx.post", side_effect=mock_post):
                    _evaluate_ai_combined_decision(case, llm_available=True)
                    assert len(captured_payloads) == 1
                    prompt_text = str(captured_payloads[0])
                    assert "hidden_failure_category" not in prompt_text
                    assert "TOP_SECRET_HIDDEN_LABEL" not in prompt_text

    def test_observable_evidence_included(self, sample_case):
        """13: All observable context fields are included."""
        captured_payloads = []

        def mock_post(url, **kwargs):
            captured_payloads.append(kwargs.get("json", {}))
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = {
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {
                                    "text": '{"diagnosis": {"category": "TRANSIENT", "confidence": 0.9, "reasoning": "OK"}, "recommendation": {"strategy": "WAIT_AND_RETRY", "confidence": 0.9, "reasoning": "OK"}}'
                                }
                            ]
                        }
                    }
                ]
            }
            return resp

        with patch.object(settings, "LLM_PROVIDER", "gemini"):
            with patch.object(settings, "GEMINI_API_KEY", "fake-key"):
                with patch("httpx.post", side_effect=mock_post):
                    _evaluate_ai_combined_decision(sample_case, llm_available=True)
                    prompt_text = str(captured_payloads[0])
                    assert sample_case.failure_reason in prompt_text
                    assert str(sample_case.amount_paise) in prompt_text
                    assert sample_case.currency in prompt_text
                    assert str(sample_case.retry_count) in prompt_text

    def test_fallback_correctness(self, sample_case):
        """14: Fallback provenance fields accurately set."""
        with patch.object(settings, "LLM_FALLBACK_ENABLED", False):
            strat, prov = _evaluate_ai_combined_decision(sample_case, llm_available=False)
            assert prov["diagnosis_source"] == "deterministic"
            assert prov["diagnosis_used_llm"] is False
            assert prov["diagnosis_fallback_used"] is False

    def test_policy_engine_remains_authoritative(self, sample_case):
        """15: PolicyEngine overrides unsafe LLM recommendation."""
        # LLM suggests retry on HARD_FAILURE
        fake_resp = MagicMock()
        fake_resp.status_code = 200
        fake_resp.json.return_value = {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {
                                "text": '{"diagnosis": {"category": "HARD_FAILURE", "confidence": 0.95, "reasoning": "Card stolen"}, "recommendation": {"strategy": "WAIT_AND_RETRY", "confidence": 0.95, "reasoning": "Retry anyway"}}'
                            }
                        ]
                    }
                }
            ]
        }
        with patch.object(settings, "LLM_PROVIDER", "gemini"):
            with patch.object(settings, "GEMINI_API_KEY", "fake-key"):
                with patch("httpx.post", return_value=fake_resp):
                    strat, prov = _evaluate_ai_combined_decision(sample_case, llm_available=True)
                    # PolicyEngine MUST block retry on HARD_FAILURE and force STOP_RECOVERY
                    assert strat == RecoveryStrategy.STOP_RECOVERY.value
                    assert prov["policy_blocked"] is True
                    assert prov["safe_stop"] is True
