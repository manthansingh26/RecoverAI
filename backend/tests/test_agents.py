"""Tests for Milestone 16A/B.1 — AI Revenue Recovery Advisory Layer.

These tests verify the advisory layer in ISOLATION:
- structured output validation (Diagnosis / Recommendation)
- AdvisoryResult provenance (used_llm / fallback_used / source)
- the LLM = advisor, never authority boundary
- confidence surfacing
- explainer uses only supplied facts
- provider abstraction is vendor-agnostic for unit tests
"""

import logging
from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError

from app.agents.base import AdvisoryResult, LLMProvider, LLMResult
from app.agents.diagnostician import diagnose_failure
from app.agents.explainer import explain_decision
from app.agents.recommender import recommend_strategy_for_diagnosis
from app.agents.schemas import Diagnosis, Recommendation
from app.core.config import settings


# ---------------------------------------------------------------------------
# Structured output validation
# ---------------------------------------------------------------------------

class TestSchemaValidation:
    def test_structured_diagnosis_validates(self) -> None:
        d = Diagnosis(category="TRANSIENT", confidence=0.8, reasoning="network error")
        assert d.category == "TRANSIENT"
        assert 0.0 <= d.confidence <= 1.0

    def test_structured_recommendation_validates(self) -> None:
        r = Recommendation(strategy="WAIT_AND_RETRY", confidence=0.7, reasoning="retry")
        assert r.strategy == "WAIT_AND_RETRY"
        assert 0.0 <= r.confidence <= 1.0

    def test_invalid_confidence_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Diagnosis(category="TRANSIENT", confidence=1.5, reasoning="x")
        with pytest.raises(ValidationError):
            Recommendation(strategy="WAIT_AND_RETRY", confidence=-0.1, reasoning="x")

    def test_invalid_category_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Diagnosis(category="NOT_A_CATEGORY", confidence=0.5, reasoning="x")

    def test_invalid_strategy_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Recommendation(strategy="MAGIC_FIX", confidence=0.5, reasoning="x")


# ---------------------------------------------------------------------------
# AdvisoryResult provenance
# ---------------------------------------------------------------------------

class TestAdvisoryResultProvenance:
    def test_advisory_result_shape(self) -> None:
        """AdvisoryResult carries explicit, machine-readable provenance."""
        d = Diagnosis(category="TRANSIENT", confidence=0.9, reasoning="ai")
        r = AdvisoryResult(
            value=d,
            used_llm=True,
            fallback_used=False,
            provider="anthropic",
            model="claude-sonnet-5",
            prompt_version="diagnosis.v1",
            confidence=d.confidence,
        )
        assert r.value is d
        assert r.used_llm is True
        assert r.fallback_used is False
        assert r.source == "ai"
        assert r.provider == "anthropic"
        assert r.model == "claude-sonnet-5"

    def test_source_deterministic(self) -> None:
        d = Diagnosis(category="TRANSIENT", confidence=0.9, reasoning="det")
        r = AdvisoryResult(
            value=d, used_llm=False, fallback_used=True, provider=None,
            model=None, prompt_version="diagnosis.v1", confidence=d.confidence,
        )
        assert r.source == "deterministic"


# ---------------------------------------------------------------------------
# Advisory boundary: LLM cannot mutate state, only returns structured output
# ---------------------------------------------------------------------------

class TestAdvisoryBoundary:
    def test_diagnosis_is_advisory_only(self) -> None:
        """diagnose_failure returns a value object, never touches DB/state."""
        with patch.object(settings, "LLM_API_KEY", ""):
            result = diagnose_failure(error_reason="network_error")
        assert isinstance(result, AdvisoryResult)
        assert result.value.category in {
            "TRANSIENT", "AUTHENTICATION", "HARD_FAILURE", "UNKNOWN"
        }

    def test_recommendation_is_advisory_only(self) -> None:
        """recommend returns a value object, never executes anything."""
        with patch.object(settings, "LLM_API_KEY", ""):
            d = diagnose_failure(error_reason="network_error")
            r = recommend_strategy_for_diagnosis(diagnosis=d.value, amount_paise=10000)
        assert isinstance(r, AdvisoryResult)
        assert r.value.strategy in {
            "WAIT_AND_RETRY", "CREATE_PAYMENT_LINK", "HUMAN_REVIEW", "STOP_RECOVERY"
        }


# ---------------------------------------------------------------------------
# Confidence surfacing
# ---------------------------------------------------------------------------

class TestConfidenceSurfacing:
    def test_confidence_is_surfaced_correctly(self) -> None:
        """The validated confidence value must round-trip through the API."""
        with patch.object(settings, "LLM_API_KEY", ""):
            d = diagnose_failure(error_reason="authentication_failed")
        assert isinstance(d.confidence, float)
        assert 0.0 <= d.confidence <= 1.0

    def test_confidence_threshold_is_exposed_in_config(self) -> None:
        t = settings.LLM_CONFIDENCE_THRESHOLD
        assert 0.0 <= t <= 1.0

    def test_low_confidence_diagnosis_is_surfaceable(self) -> None:
        """A low-confidence diagnosis is still a valid Diagnosis (integration
        decides escalation, not the advisory layer)."""
        d = Diagnosis(category="UNKNOWN", confidence=0.2, reasoning="insufficient evidence")
        assert d.confidence < settings.LLM_CONFIDENCE_THRESHOLD


# ---------------------------------------------------------------------------
# Explainer
# ---------------------------------------------------------------------------

class TestExplainer:
    def test_explanation_contains_only_supplied_facts(self) -> None:
        d = Diagnosis(category="TRANSIENT", confidence=0.8, reasoning="gateway timeout")
        r = Recommendation(strategy="WAIT_AND_RETRY", confidence=0.7, reasoning="transient retry")
        text = explain_decision(diagnosis=d, recommendation=r, amount_paise=10000, currency="INR")
        assert "TRANSIENT" in text
        assert "WAIT_AND_RETRY" in text
        assert "10000 INR" in text
        assert "no action has been executed" in text

    def test_explanation_never_claims_execution(self) -> None:
        d = Diagnosis(category="HARD_FAILURE", confidence=0.9, reasoning="blocked")
        r = Recommendation(strategy="STOP_RECOVERY", confidence=0.9, reasoning="permanent")
        text = explain_decision(diagnosis=d, recommendation=r)
        assert "no action has been executed" in text

    def test_explanation_without_amount(self) -> None:
        d = Diagnosis(category="UNKNOWN", confidence=0.1, reasoning="ambiguous")
        r = Recommendation(strategy="HUMAN_REVIEW", confidence=0.1, reasoning="escalate")
        text = explain_decision(diagnosis=d, recommendation=r)
        assert "UNKNOWN" in text
        assert "HUMAN_REVIEW" in text


# ---------------------------------------------------------------------------
# Provider abstraction: no concrete vendor required for unit tests
# ---------------------------------------------------------------------------

class TestProviderAbstraction:
    def test_provider_requires_no_concrete_vendor_for_unit_tests(self) -> None:
        """Unit tests can inject a fake provider; no anthropic import required."""
        fake = MagicMock()
        fake.call.return_value = LLMResult(
            content={"category": "TRANSIENT", "confidence": 0.9, "reasoning": "fake"}
        )
        with patch.object(settings, "LLM_API_KEY", "test-key"):
            with patch.object(settings, "LLM_FALLBACK_ENABLED", True):
                d = diagnose_failure(error_reason="network_error", provider=fake)
        assert d.used_llm is True
        assert d.source == "ai"
        assert d.value.category == "TRANSIENT"
        assert d.confidence == 0.9

    def test_provider_error_result_surfaces(self) -> None:
        """A provider returning an error is surfaced as a fallback, not a crash."""
        fake = MagicMock()
        fake.call.return_value = LLMResult(error="timeout")
        with patch.object(settings, "LLM_API_KEY", "test-key"):
            with patch.object(settings, "LLM_FALLBACK_ENABLED", True):
                d = diagnose_failure(error_reason="network_error", provider=fake)
        assert isinstance(d, AdvisoryResult)
        assert d.used_llm is False
        assert d.fallback_used is True
        assert d.source == "deterministic"

    def test_anthropic_adapter_signature_compatibility(self) -> None:
        """Ensure LLMProvider._do_call invokes messages.create with SDK 1.0.0 compatible kwargs."""
        prov = LLMProvider("claude-sonnet-5")
        with patch.object(settings, "LLM_PROVIDER", "anthropic"), patch.object(settings, "LLM_API_KEY", "test-key"):
            with patch("anthropic.Anthropic") as mock_cls:
                mock_client = MagicMock()
                mock_block = MagicMock()
                mock_block.text = '{"category": "TRANSIENT", "confidence": 0.9, "reasoning": "ok"}'
                mock_client.messages.create.return_value = MagicMock(content=[mock_block])
                mock_cls.return_value = mock_client

                prov.call(system_prompt="sys", user_prompt="user", temperature=0.0)

                mock_client.messages.create.assert_called_once()
                _, kwargs = mock_client.messages.create.call_args
                # Verify temperature is not passed as direct kwarg (would cause TypeError in SDK 1.0.0)
                assert "temperature" not in kwargs
                assert kwargs["extra_body"] == {"temperature": 0.0}
                assert kwargs["model"] == "claude-sonnet-5"
                assert kwargs["max_tokens"] == 1024



# ---------------------------------------------------------------------------
# No secrets in logs or reasoning
# ---------------------------------------------------------------------------

class TestNoSecretLeakage:
    def test_api_key_never_appears_in_logs(self, caplog) -> None:
        """A provider failure must not log the API key or raw error string."""
        with patch.object(settings, "LLM_API_KEY", "super-secret-key-123"):
            with patch.object(settings, "LLM_FALLBACK_ENABLED", True):
                fake = MagicMock()
                fake.call.return_value = LLMResult(error="boom: super-secret-key-123")
                with caplog.at_level(logging.INFO):
                    diagnose_failure(error_reason="network_error", provider=fake)
        combined = caplog.text
        assert "super-secret-key-123" not in combined
        assert "boom" not in combined

    def test_reasoning_does_not_contain_secrets(self) -> None:
        """A diagnosis reasoning string must not leak the API key."""
        with patch.object(settings, "LLM_API_KEY", ""):
            d = diagnose_failure(error_reason="network_error")
        assert "secret" not in d.value.reasoning.lower()


# ---------------------------------------------------------------------------
# Milestone 16B.2 — Model configuration validation (no network calls)
# ---------------------------------------------------------------------------

class TestModelConfiguration:
    """Verify configured model IDs against the installed SDKs WITHOUT making network calls."""

    def test_diagnosis_model_is_known_to_sdk(self) -> None:
        if settings.LLM_PROVIDER == "anthropic":
            from anthropic.types import Model
            known = set()
            for arg in Model.__args__:
                if hasattr(arg, "__args__"):
                    known.update(arg.__args__)
            assert settings.LLM_MODEL_DIAGNOSIS in known
        elif settings.LLM_PROVIDER == "openai":
            assert settings.LLM_MODEL_DIAGNOSIS.startswith(("gpt-", "o1", "o3", "chatgpt-"))
        elif settings.LLM_PROVIDER == "gemini":
            assert settings.LLM_MODEL_DIAGNOSIS.startswith(("gemini-", "gemma-"))

    def test_explain_model_is_known_to_sdk(self) -> None:
        if settings.LLM_PROVIDER == "anthropic":
            from anthropic.types import Model
            known = set()
            for arg in Model.__args__:
                if hasattr(arg, "__args__"):
                    known.update(arg.__args__)
            assert settings.LLM_MODEL_EXPLAIN in known
        elif settings.LLM_PROVIDER == "openai":
            assert settings.LLM_MODEL_EXPLAIN.startswith(("gpt-", "o1", "o3", "chatgpt-"))
        elif settings.LLM_PROVIDER == "gemini":
            assert settings.LLM_MODEL_EXPLAIN.startswith(("gemini-", "gemma-"))


class TestGeminiProvider:
    """Unit tests for GeminiProvider adapter with zero network calls."""

    def test_gemini_call_success_and_provenance(self) -> None:
        prov = LLMProvider("gemini-2.5-flash")
        fake_resp = MagicMock()
        fake_resp.status_code = 200
        fake_resp.json.return_value = {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {
                                "text": '{"category": "TRANSIENT", "confidence": 0.95, "reasoning": "Gateway timeout"}'
                            }
                        ]
                    }
                }
            ]
        }
        with patch.object(settings, "LLM_PROVIDER", "gemini"):
            with patch.object(settings, "GEMINI_API_KEY", "fake-gemini-key"):
                with patch("httpx.post", return_value=fake_resp) as mock_post:
                    result = diagnose_failure(
                        error_reason="network_error",
                        provider=prov,
                    )
                    assert result.used_llm is True
                    assert result.fallback_used is False
                    assert result.source == "ai"
                    assert result.provider == "gemini"
                    assert result.model == "gemini-2.5-flash"
                    assert result.value.category == "TRANSIENT"
                    assert result.confidence == 0.95

                    mock_post.assert_called_once()
                    _, kwargs = mock_post.call_args
                    assert "key" in kwargs["params"]
                    assert kwargs["json"]["generationConfig"]["responseMimeType"] == "application/json"

    def test_gemini_recommendation_success(self) -> None:
        prov = LLMProvider("gemini-2.5-flash")
        fake_resp = MagicMock()
        fake_resp.status_code = 200
        fake_resp.json.return_value = {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {
                                "text": '{"strategy": "WAIT_AND_RETRY", "confidence": 0.9, "reasoning": "Retry after backoff"}'
                            }
                        ]
                    }
                }
            ]
        }
        with patch.object(settings, "LLM_PROVIDER", "gemini"):
            with patch.object(settings, "GEMINI_API_KEY", "fake-gemini-key"):
                with patch("httpx.post", return_value=fake_resp):
                    diag = Diagnosis(category="TRANSIENT", confidence=0.9, reasoning="Transient")
                    rec = recommend_strategy_for_diagnosis(diagnosis=diag, amount_paise=10000, provider=prov)
                    assert rec.used_llm is True
                    assert rec.fallback_used is False
                    assert rec.source == "ai"
                    assert rec.provider == "gemini"
                    assert rec.model == "gemini-2.5-flash"
                    assert rec.value.strategy == "WAIT_AND_RETRY"
                    assert rec.confidence == 0.9

    def test_gemini_missing_candidates_triggers_fallback(self) -> None:
        prov = LLMProvider("gemini-2.5-flash")
        fake_resp = MagicMock()
        fake_resp.status_code = 200
        fake_resp.json.return_value = {"candidates": []}
        with patch.object(settings, "LLM_PROVIDER", "gemini"):
            with patch.object(settings, "GEMINI_API_KEY", "fake-gemini-key"):
                with patch("httpx.post", return_value=fake_resp):
                    result = diagnose_failure(error_reason="network_error", provider=prov)
                    assert result.used_llm is False
                    assert result.fallback_used is True
                    assert result.source == "deterministic"

    def test_gemini_malformed_json_triggers_fallback(self) -> None:
        prov = LLMProvider("gemini-2.5-flash")
        fake_resp = MagicMock()
        fake_resp.status_code = 200
        fake_resp.json.return_value = {
            "candidates": [{"content": {"parts": [{"text": "NOT JSON"}]}}]
        }
        with patch.object(settings, "LLM_PROVIDER", "gemini"):
            with patch.object(settings, "GEMINI_API_KEY", "fake-gemini-key"):
                with patch("httpx.post", return_value=fake_resp):
                    result = diagnose_failure(error_reason="network_error", provider=prov)
                    assert result.used_llm is False
                    assert result.fallback_used is True
                    assert result.source == "deterministic"

    def test_gemini_http_400_triggers_fallback(self) -> None:
        prov = LLMProvider("gemini-2.5-flash")
        fake_resp = MagicMock()
        fake_resp.status_code = 400
        with patch.object(settings, "LLM_PROVIDER", "gemini"):
            with patch.object(settings, "GEMINI_API_KEY", "fake-gemini-key"):
                with patch("httpx.post", return_value=fake_resp):
                    result = diagnose_failure(error_reason="network_error", provider=prov)
                    assert result.used_llm is False
                    assert result.fallback_used is True
                    assert result.source == "deterministic"

    def test_gemini_http_401_triggers_fallback(self) -> None:
        prov = LLMProvider("gemini-2.5-flash")
        fake_resp = MagicMock()
        fake_resp.status_code = 401
        with patch.object(settings, "LLM_PROVIDER", "gemini"):
            with patch.object(settings, "GEMINI_API_KEY", "fake-gemini-key"):
                with patch("httpx.post", return_value=fake_resp):
                    result = diagnose_failure(error_reason="network_error", provider=prov)
                    assert result.used_llm is False
                    assert result.fallback_used is True
                    assert result.source == "deterministic"

    def test_gemini_http_429_triggers_fallback(self) -> None:
        prov = LLMProvider("gemini-2.5-flash")
        fake_resp = MagicMock()
        fake_resp.status_code = 429
        with patch.object(settings, "LLM_PROVIDER", "gemini"):
            with patch.object(settings, "GEMINI_API_KEY", "fake-gemini-key"):
                with patch("httpx.post", return_value=fake_resp), patch("time.sleep"):
                    result = diagnose_failure(error_reason="network_error", provider=prov)
                    assert result.used_llm is False
                    assert result.fallback_used is True
                    assert result.source == "deterministic"

    def test_gemini_http_500_triggers_fallback(self) -> None:
        prov = LLMProvider("gemini-2.5-flash")
        fake_resp = MagicMock()
        fake_resp.status_code = 500
        with patch.object(settings, "LLM_PROVIDER", "gemini"):
            with patch.object(settings, "GEMINI_API_KEY", "fake-gemini-key"):
                with patch("httpx.post", return_value=fake_resp), patch("time.sleep"):
                    result = diagnose_failure(error_reason="network_error", provider=prov)
                    assert result.used_llm is False
                    assert result.fallback_used is True
                    assert result.source == "deterministic"

    def test_gemini_timeout_triggers_fallback(self) -> None:
        import httpx
        prov = LLMProvider("gemini-2.5-flash")
        with patch.object(settings, "LLM_PROVIDER", "gemini"):
            with patch.object(settings, "GEMINI_API_KEY", "fake-gemini-key"):
                with patch("httpx.post", side_effect=httpx.TimeoutException("timeout")), patch("time.sleep"):
                    result = diagnose_failure(error_reason="network_error", provider=prov)
                    assert result.used_llm is False
                    assert result.fallback_used is True
                    assert result.source == "deterministic"



class TestOpenAIProvider:
    """Unit tests for OpenAIProvider adapter with zero network calls."""

    def test_openai_call_success_and_provenance(self) -> None:
        prov = LLMProvider("gpt-4o-mini")
        with patch.object(settings, "LLM_PROVIDER", "openai"):
            with patch.object(settings, "OPENAI_API_KEY", "sk-fake-test-key"):
                with patch("openai.OpenAI") as mock_cls:
                    mock_client = MagicMock()
                    mock_choice = MagicMock()
                    mock_choice.message.content = '{"category": "TRANSIENT", "confidence": 0.85, "reasoning": "Network timeout to bank"}'
                    mock_client.chat.completions.create.return_value = MagicMock(choices=[mock_choice])
                    mock_cls.return_value = mock_client

                    result = diagnose_failure(
                        error_reason="network_error",
                        provider=prov,
                    )

                    assert result.used_llm is True
                    assert result.fallback_used is False
                    assert result.source == "ai"
                    assert result.provider == "openai"
                    assert result.model == "gpt-4o-mini"
                    assert result.value.category == "TRANSIENT"
                    assert result.confidence == 0.85

                    mock_client.chat.completions.create.assert_called_once()
                    _, kwargs = mock_client.chat.completions.create.call_args
                    assert kwargs["model"] == "gpt-4o-mini"
                    assert kwargs["response_format"] == {"type": "json_object"}
                    assert kwargs["temperature"] == 0.0

    def test_openai_invalid_json_triggers_fallback(self) -> None:
        prov = LLMProvider("gpt-4o-mini")
        with patch.object(settings, "LLM_PROVIDER", "openai"):
            with patch.object(settings, "OPENAI_API_KEY", "sk-fake-test-key"):
                with patch("openai.OpenAI") as mock_cls:
                    mock_client = MagicMock()
                    mock_choice = MagicMock()
                    mock_choice.message.content = 'NOT VALID JSON'
                    mock_client.chat.completions.create.return_value = MagicMock(choices=[mock_choice])
                    mock_cls.return_value = mock_client

                    result = diagnose_failure(error_reason="network_error", provider=prov)
                    assert result.used_llm is False
                    assert result.fallback_used is True
                    assert result.source == "deterministic"
                    assert result.value.category == "TRANSIENT"

    def test_openai_invalid_schema_triggers_fallback(self) -> None:
        prov = LLMProvider("gpt-4o-mini")
        with patch.object(settings, "LLM_PROVIDER", "openai"):
            with patch.object(settings, "OPENAI_API_KEY", "sk-fake-test-key"):
                with patch("openai.OpenAI") as mock_cls:
                    mock_client = MagicMock()
                    mock_choice = MagicMock()
                    mock_choice.message.content = '{"category": "TRANSIENT"}'
                    mock_client.chat.completions.create.return_value = MagicMock(choices=[mock_choice])
                    mock_cls.return_value = mock_client

                    result = diagnose_failure(error_reason="network_error", provider=prov)
                    assert result.used_llm is False
                    assert result.fallback_used is True
                    assert result.source == "deterministic"

    def test_openai_invalid_enum_triggers_fallback(self) -> None:
        prov = LLMProvider("gpt-4o-mini")
        with patch.object(settings, "LLM_PROVIDER", "openai"):
            with patch.object(settings, "OPENAI_API_KEY", "sk-fake-test-key"):
                with patch("openai.OpenAI") as mock_cls:
                    mock_client = MagicMock()
                    mock_choice = MagicMock()
                    mock_choice.message.content = '{"category": "UNSUPPORTED_CATEGORY", "confidence": 0.8, "reasoning": "bad"}'
                    mock_client.chat.completions.create.return_value = MagicMock(choices=[mock_choice])
                    mock_cls.return_value = mock_client

                    result = diagnose_failure(error_reason="network_error", provider=prov)
                    assert result.used_llm is False
                    assert result.fallback_used is True
                    assert result.source == "deterministic"

    def test_openai_invalid_confidence_triggers_fallback(self) -> None:
        prov = LLMProvider("gpt-4o-mini")
        with patch.object(settings, "LLM_PROVIDER", "openai"):
            with patch.object(settings, "OPENAI_API_KEY", "sk-fake-test-key"):
                with patch("openai.OpenAI") as mock_cls:
                    mock_client = MagicMock()
                    mock_choice = MagicMock()
                    mock_choice.message.content = '{"category": "TRANSIENT", "confidence": 1.5, "reasoning": "bad"}'
                    mock_client.chat.completions.create.return_value = MagicMock(choices=[mock_choice])
                    mock_cls.return_value = mock_client

                    result = diagnose_failure(error_reason="network_error", provider=prov)
                    assert result.used_llm is False
                    assert result.fallback_used is True
                    assert result.source == "deterministic"

    def test_openai_auth_error_triggers_fallback(self) -> None:
        from openai import AuthenticationError
        prov = LLMProvider("gpt-4o-mini")
        with patch.object(settings, "LLM_PROVIDER", "openai"):
            with patch.object(settings, "OPENAI_API_KEY", "sk-fake-test-key"):
                with patch("openai.OpenAI") as mock_cls:
                    mock_client = MagicMock()
                    mock_client.chat.completions.create.side_effect = AuthenticationError(
                        message="Invalid API key", response=MagicMock(status_code=401), body={}
                    )
                    mock_cls.return_value = mock_client

                    result = diagnose_failure(error_reason="network_error", provider=prov)
                    assert result.used_llm is False
                    assert result.fallback_used is True
                    assert result.source == "deterministic"

    def test_openai_rate_limit_error_triggers_fallback(self) -> None:
        from openai import RateLimitError
        prov = LLMProvider("gpt-4o-mini")
        with patch.object(settings, "LLM_PROVIDER", "openai"):
            with patch.object(settings, "OPENAI_API_KEY", "sk-fake-test-key"):
                with patch("openai.OpenAI") as mock_cls:
                    mock_client = MagicMock()
                    mock_client.chat.completions.create.side_effect = RateLimitError(
                        message="Rate limit exceeded", response=MagicMock(status_code=429), body={}
                    )
                    mock_cls.return_value = mock_client

                    result = diagnose_failure(error_reason="network_error", provider=prov)
                    assert result.used_llm is False
                    assert result.fallback_used is True
                    assert result.source == "deterministic"

    def test_openai_server_error_triggers_fallback(self) -> None:
        from openai import InternalServerError
        prov = LLMProvider("gpt-4o-mini")
        with patch.object(settings, "LLM_PROVIDER", "openai"):
            with patch.object(settings, "OPENAI_API_KEY", "sk-fake-test-key"):
                with patch("openai.OpenAI") as mock_cls:
                    mock_client = MagicMock()
                    mock_client.chat.completions.create.side_effect = InternalServerError(
                        message="Internal server error", response=MagicMock(status_code=500), body={}
                    )
                    mock_cls.return_value = mock_client

                    result = diagnose_failure(error_reason="network_error", provider=prov)
                    assert result.used_llm is False
                    assert result.fallback_used is True
                    assert result.source == "deterministic"

    def test_openai_empty_response_triggers_fallback(self) -> None:
        prov = LLMProvider("gpt-4o-mini")
        with patch.object(settings, "LLM_PROVIDER", "openai"):
            with patch.object(settings, "OPENAI_API_KEY", "sk-fake-test-key"):
                with patch("openai.OpenAI") as mock_cls:
                    mock_client = MagicMock()
                    mock_choice = MagicMock()
                    mock_choice.message.content = ""
                    mock_client.chat.completions.create.return_value = MagicMock(choices=[mock_choice])
                    mock_cls.return_value = mock_client

                    result = diagnose_failure(error_reason="network_error", provider=prov)
                    assert result.used_llm is False
                    assert result.fallback_used is True
                    assert result.source == "deterministic"

    def test_openai_recommendation_success(self) -> None:
        prov = LLMProvider("gpt-4o-mini")
        with patch.object(settings, "LLM_PROVIDER", "openai"):
            with patch.object(settings, "OPENAI_API_KEY", "sk-fake-test-key"):
                with patch("openai.OpenAI") as mock_cls:
                    mock_client = MagicMock()
                    mock_choice = MagicMock()
                    mock_choice.message.content = '{"strategy": "WAIT_AND_RETRY", "confidence": 0.88, "reasoning": "Transient issue"}'
                    mock_client.chat.completions.create.return_value = MagicMock(choices=[mock_choice])
                    mock_cls.return_value = mock_client

                    diag = Diagnosis(category="TRANSIENT", confidence=0.9, reasoning="Transient")
                    rec = recommend_strategy_for_diagnosis(diagnosis=diag, amount_paise=10000, provider=prov)

                    assert rec.used_llm is True
                    assert rec.fallback_used is False
                    assert rec.source == "ai"
                    assert rec.provider == "openai"
                    assert rec.model == "gpt-4o-mini"
                    assert rec.value.strategy == "WAIT_AND_RETRY"
                    assert rec.confidence == 0.88


def anthropic_version() -> str:
    import anthropic
    return anthropic.__version__

