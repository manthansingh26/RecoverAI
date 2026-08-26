"""Tests for Milestone 16A/B.1 fallback behavior — deterministic path when LLM fails.

These tests verify the advisory layer degrades gracefully when the LLM is
unavailable, produces invalid output, or hits schema violations, and that the
AdvisoryResult provenance is correct in each case.
"""

from unittest.mock import MagicMock, patch

import pytest

from app.agents.base import AdvisoryResult, LLMResult
from app.agents.diagnostician import diagnose_failure
from app.agents.recommender import recommend_strategy_for_diagnosis
from app.agents.schemas import Diagnosis
from app.core.config import settings


class TestDeterministicFallback:
    def test_llm_disabled_uses_deterministic(self) -> None:
        """When LLM_FALLBACK_ENABLED=False, the LLM is never called."""
        with patch.object(settings, "LLM_FALLBACK_ENABLED", False):
            d = diagnose_failure(error_reason="network_error")
        assert d.value.category == "TRANSIENT"
        assert d.confidence == 0.8  # deterministic confidence
        assert d.used_llm is False
        assert d.fallback_used is False
        assert d.source == "deterministic"

    def test_missing_api_key_uses_deterministic(self) -> None:
        """Empty API key falls back to deterministic without an LLM call."""
        with patch.object(settings, "LLM_API_KEY", ""):
            with patch.object(settings, "LLM_FALLBACK_ENABLED", True):
                d = diagnose_failure(error_reason="authentication_failed")
        assert d.value.category == "AUTHENTICATION"
        assert d.used_llm is False
        assert d.fallback_used is False

    def test_provider_timeout_falls_back(self) -> None:
        """A provider timeout returns a valid Diagnosis from deterministic."""
        fake = MagicMock()
        fake.call.return_value = LLMResult(error="timeout")
        with patch.object(settings, "LLM_API_KEY", "test-key"):
            with patch.object(settings, "LLM_FALLBACK_ENABLED", True):
                d = diagnose_failure(error_reason="network_error", provider=fake)
        assert isinstance(d, AdvisoryResult)
        assert d.value.category == "TRANSIENT"
        assert d.used_llm is False
        assert d.fallback_used is True

    def test_provider_exception_falls_back(self) -> None:
        """A provider exception returns a valid Diagnosis from deterministic."""
        fake = MagicMock()
        fake.call.return_value = LLMResult(error="APIError: 503 Service Unavailable")
        with patch.object(settings, "LLM_API_KEY", "test-key"):
            with patch.object(settings, "LLM_FALLBACK_ENABLED", True):
                d = diagnose_failure(error_reason="network_error", provider=fake)
        assert isinstance(d, AdvisoryResult)
        assert d.fallback_used is True

    def test_malformed_json_falls_back(self) -> None:
        """Non-JSON LLM output triggers deterministic fallback."""
        fake = MagicMock()
        fake.call.return_value = LLMResult(content="not json at all")
        with patch.object(settings, "LLM_API_KEY", "test-key"):
            with patch.object(settings, "LLM_FALLBACK_ENABLED", True):
                d = diagnose_failure(error_reason="network_error", provider=fake)
        assert isinstance(d, AdvisoryResult)
        assert d.value.category == "TRANSIENT"
        assert d.fallback_used is True

    def test_non_dict_json_falls_back(self) -> None:
        """JSON that is not a dict triggers deterministic fallback."""
        fake = MagicMock()
        fake.call.return_value = LLMResult(content=["not", "a", "dict"])
        with patch.object(settings, "LLM_API_KEY", "test-key"):
            with patch.object(settings, "LLM_FALLBACK_ENABLED", True):
                d = diagnose_failure(error_reason="network_error", provider=fake)
        assert isinstance(d, AdvisoryResult)
        assert d.fallback_used is True

    def test_empty_response_falls_back(self) -> None:
        """Empty LLM response triggers deterministic fallback."""
        fake = MagicMock()
        fake.call.return_value = LLMResult(content="")
        with patch.object(settings, "LLM_API_KEY", "test-key"):
            with patch.object(settings, "LLM_FALLBACK_ENABLED", True):
                d = diagnose_failure(error_reason="network_error", provider=fake)
        assert isinstance(d, AdvisoryResult)
        assert d.fallback_used is True

    def test_invalid_category_from_llm_falls_back(self) -> None:
        """LLM returning an invalid category triggers deterministic fallback."""
        fake = MagicMock()
        fake.call.return_value = LLMResult(
            content={"category": "BOGUS", "confidence": 0.9, "reasoning": "bad"}
        )
        with patch.object(settings, "LLM_API_KEY", "test-key"):
            with patch.object(settings, "LLM_FALLBACK_ENABLED", True):
                d = diagnose_failure(error_reason="network_error", provider=fake)
        assert isinstance(d, AdvisoryResult)
        assert d.value.category == "TRANSIENT"
        assert d.fallback_used is True

    def test_invalid_strategy_from_llm_falls_back(self) -> None:
        """LLM returning an invalid strategy triggers deterministic fallback."""
        fake = MagicMock()
        fake.call.return_value = LLMResult(
            content={"strategy": "MAGIC", "confidence": 0.9, "reasoning": "bad"}
        )
        d = Diagnosis(category="TRANSIENT", confidence=0.8, reasoning="transient")
        with patch.object(settings, "LLM_API_KEY", "test-key"):
            with patch.object(settings, "LLM_FALLBACK_ENABLED", True):
                r = recommend_strategy_for_diagnosis(diagnosis=d, amount_paise=10000, provider=fake)
        assert r.value.strategy == "WAIT_AND_RETRY"
        assert r.fallback_used is True

    def test_confidence_out_of_range_falls_back(self) -> None:
        """LLM returning confidence outside [0,1] triggers deterministic fallback."""
        fake = MagicMock()
        fake.call.return_value = LLMResult(
            content={"category": "TRANSIENT", "confidence": 99.9, "reasoning": "bad"}
        )
        with patch.object(settings, "LLM_API_KEY", "test-key"):
            with patch.object(settings, "LLM_FALLBACK_ENABLED", True):
                d = diagnose_failure(error_reason="network_error", provider=fake)
        assert isinstance(d, AdvisoryResult)
        assert d.value.category == "TRANSIENT"
        assert d.fallback_used is True

    def test_recommender_fallback_uses_deterministic(self) -> None:
        """When LLM is disabled, recommender uses deterministic advisor."""
        with patch.object(settings, "LLM_API_KEY", ""):
            d = Diagnosis(category="TRANSIENT", confidence=0.8, reasoning="transient")
            r = recommend_strategy_for_diagnosis(diagnosis=d, amount_paise=100000)
        assert r.value.strategy == "WAIT_AND_RETRY"
        assert r.value.confidence == 0.75  # deterministic confidence
        assert r.used_llm is False
        assert r.fallback_used is False


class TestFallbackConfigSemantics:
    def test_fallback_enabled_true_uses_llm_with_fallback(self) -> None:
        """LLM_FALLBACK_ENABLED=True: LLM consulted, fallback on failure."""
        fake = MagicMock()
        fake.call.return_value = LLMResult(
            content={"category": "TRANSIENT", "confidence": 0.9, "reasoning": "ai"}
        )
        with patch.object(settings, "LLM_FALLBACK_ENABLED", True):
            with patch.object(settings, "LLM_API_KEY", "test-key"):
                d = diagnose_failure(error_reason="network_error", provider=fake)
        assert d.used_llm is True
        assert d.fallback_used is False
        assert d.source == "ai"

    def test_fallback_enabled_false_disables_llm(self) -> None:
        """LLM_FALLBACK_ENABLED=False: no LLM calls, deterministic only."""
        with patch.object(settings, "LLM_FALLBACK_ENABLED", False):
            with patch.object(settings, "LLM_API_KEY", "test-key"):
                d = diagnose_failure(error_reason="network_error")
        assert d.value.category == "TRANSIENT"
        assert d.used_llm is False
        assert d.fallback_used is False

    def test_no_key_uses_deterministic_even_when_enabled(self) -> None:
        """Missing API key -> deterministic even if fallback enabled."""
        with patch.object(settings, "LLM_FALLBACK_ENABLED", True):
            with patch.object(settings, "LLM_API_KEY", ""):
                d = diagnose_failure(error_reason="network_error")
        assert d.value.category == "TRANSIENT"
        assert d.used_llm is False
        assert d.fallback_used is False
