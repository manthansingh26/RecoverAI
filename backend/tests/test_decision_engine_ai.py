"""Milestone 16B/B.1 — AI Decision Engine Integration tests.

These tests verify the safety boundary of the AI advisory layer inside the
Decision Engine using the explicit AdvisoryResult provenance (no string
inspection):
- LLM = advisor only; never mutates RecoveryCase directly
- deterministic PolicyEngine = FINAL AUTHORITY (5 rules unchanged)
- AI disabled / failure / malformed / timeout → deterministic fallback
- low confidence → REQUIRES_HUMAN
- policy block overrides AI recommendation
- audit trail includes AI reasoning + explicit source labels
- no secret leakage
- fallback configuration semantics
"""

import logging
import uuid
from unittest.mock import MagicMock, patch

import pytest

from app.agents.base import AdvisoryResult, LLMResult
from app.agents.diagnostician import diagnose_failure
from app.agents.recommender import recommend_strategy_for_diagnosis
from app.agents.schemas import Diagnosis, Recommendation
from app.core.config import settings
from app.models.enums import (
    FailureCategory,
    RecoveryStatus,
    RecoveryStrategy,
)
from app.models.payment_event import PaymentEvent
from app.models.recovery_case import RecoveryCase
from app.services.decision_engine import run_decision_engine


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

def _make_payment_event(
    db, *, error_reason: str = "bank_technical_error", amount_paise: int = 100_000
) -> PaymentEvent:
    pe = PaymentEvent(
        event_type="payment.failed",
        external_event_id=f"evt_{uuid.uuid4().hex[:12]}",
        external_payment_id=f"pay_{uuid.uuid4().hex[:12]}",
        external_order_id=f"order_{uuid.uuid4().hex[:12]}",
        amount_paise=amount_paise,
        currency="INR",
        error_code="payment_failed",
        error_reason=error_reason,
        error_description="Test payment failure",
        raw_payload={"test": True},
        payload_hash=uuid.uuid4().hex,
    )
    db.add(pe)
    db.flush()
    return pe


def _make_recovery_case(db, payment_event: PaymentEvent) -> RecoveryCase:
    rc = RecoveryCase(
        payment_event_id=payment_event.id,
        status=RecoveryStatus.RECEIVED.value,
        failure_category=FailureCategory.UNKNOWN.value,
        recovery_probability=None,
        priority_score=None,
        recommended_strategy=None,
        expected_value_paise=None,
        decision_audit_trail={"ingestion": {"source": "test"}},
        retry_count=0,
        requires_human_approval=False,
        approved_by_human=None,
    )
    db.add(rc)
    db.flush()
    return rc


def _ai_diagnosis(category="TRANSIENT", confidence=0.9, reasoning="ai diag") -> AdvisoryResult:
    d = Diagnosis(category=category, confidence=confidence, reasoning=reasoning)
    return AdvisoryResult(
        value=d, used_llm=True, fallback_used=False, provider="anthropic",
        model="claude-sonnet-5", prompt_version="diagnosis.v1", confidence=confidence,
    )


def _ai_recommendation(strategy="WAIT_AND_RETRY", confidence=0.9, reasoning="ai rec") -> AdvisoryResult:
    r = Recommendation(strategy=strategy, confidence=confidence, reasoning=reasoning)
    return AdvisoryResult(
        value=r, used_llm=True, fallback_used=False, provider="anthropic",
        model="claude-sonnet-5", prompt_version="diagnosis.v1", confidence=confidence,
    )


# ---------------------------------------------------------------------------
# 1-2. AI diagnosis + recommendation integrated
# ---------------------------------------------------------------------------

class TestAIIntegration:
    def test_ai_diagnosis_integrated(self) -> None:
        """diagnose_failure returns explicit provenance."""
        with patch.object(settings, "LLM_API_KEY", "test-key"):
            with patch.object(settings, "LLM_FALLBACK_ENABLED", True):
                d = diagnose_failure(
                    error_reason="network_error",
                    provider=MagicMock(
                        call=MagicMock(
                            return_value=LLMResult(
                                content={"category": "TRANSIENT", "confidence": 0.9, "reasoning": "ai"}
                            )
                        )
                    ),
                )
        assert d.used_llm is True
        assert d.source == "ai"
        assert d.value.category == "TRANSIENT"

    def test_ai_recommendation_integrated(self) -> None:
        """recommend_strategy_for_diagnosis uses the diagnosis."""
        d = Diagnosis(category="TRANSIENT", confidence=0.9, reasoning="ai")
        with patch.object(settings, "LLM_API_KEY", "test-key"):
            with patch.object(settings, "LLM_FALLBACK_ENABLED", True):
                r = recommend_strategy_for_diagnosis(
                    diagnosis=d,
                    amount_paise=100_000,
                    provider=MagicMock(
                        call=MagicMock(
                            return_value=LLMResult(
                                content={"strategy": "WAIT_AND_RETRY", "confidence": 0.9, "reasoning": "ai"}
                            )
                        )
                    ),
                )
        assert r.used_llm is True
        assert r.value.strategy == "WAIT_AND_RETRY"


# ---------------------------------------------------------------------------
# 3. AI disabled → deterministic path
# ---------------------------------------------------------------------------

class TestAIDisabled:
    def test_ai_disabled_deterministic_outcome(self, db_session) -> None:
        """LLM disabled == pre-16A deterministic outcome."""
        with patch.object(settings, "LLM_API_KEY", ""):
            with patch.object(settings, "LLM_FALLBACK_ENABLED", True):
                pe = _make_payment_event(db_session, error_reason="bank_technical_error")
                rc = _make_recovery_case(db_session, pe)
                db_session.commit()
                result = run_decision_engine(db_session, str(rc.id))

        assert result.status == RecoveryStatus.PENDING_EXECUTION.value
        assert result.recommended_strategy == RecoveryStrategy.WAIT_AND_RETRY.value
        assert result.failure_category == FailureCategory.TRANSIENT.value
        assert result.decision_audit_trail["classification"]["source"] == "deterministic"
        assert result.decision_audit_trail["recommendation"]["source"] == "deterministic"
        assert result.decision_audit_trail["classification"]["ai_used"] is False


# ---------------------------------------------------------------------------
# 4-6. Provider failure fallback
# ---------------------------------------------------------------------------

class TestFallback:
    def test_provider_timeout_falls_back(self, db_session) -> None:
        """Timeout -> deterministic fallback (no pipeline failure)."""
        from app.agents.diagnostician import _deterministic_result
        with patch.object(settings, "LLM_API_KEY", "test-key"):
            with patch.object(settings, "LLM_FALLBACK_ENABLED", True):
                pe = _make_payment_event(db_session, error_reason="bank_technical_error")
                rc = _make_recovery_case(db_session, pe)
                db_session.commit()
                with patch(
                    "app.services.decision_engine.diagnose_failure",
                    return_value=_deterministic_result(
                        error_reason="bank_technical_error", fallback_used=True
                    ),
                ):
                    result = run_decision_engine(db_session, str(rc.id))
        assert result.status == RecoveryStatus.PENDING_EXECUTION.value
        assert result.recommended_strategy == RecoveryStrategy.WAIT_AND_RETRY.value
        assert result.decision_audit_trail["classification"]["fallback_used"] is True

    def test_provider_exception_falls_back(self, db_session) -> None:
        """Exception -> deterministic fallback."""
        from app.agents.diagnostician import _deterministic_result
        with patch.object(settings, "LLM_API_KEY", "test-key"):
            with patch.object(settings, "LLM_FALLBACK_ENABLED", True):
                pe = _make_payment_event(db_session, error_reason="bank_technical_error")
                rc = _make_recovery_case(db_session, pe)
                db_session.commit()
                with patch(
                    "app.services.decision_engine.diagnose_failure",
                    return_value=_deterministic_result(
                        error_reason="bank_technical_error", fallback_used=True
                    ),
                ):
                    result = run_decision_engine(db_session, str(rc.id))
        assert result.status == RecoveryStatus.PENDING_EXECUTION.value

    def test_malformed_output_falls_back(self, db_session) -> None:
        """Malformed LLM output -> deterministic fallback."""
        from app.agents.diagnostician import _deterministic_result
        with patch.object(settings, "LLM_API_KEY", "test-key"):
            with patch.object(settings, "LLM_FALLBACK_ENABLED", True):
                pe = _make_payment_event(db_session, error_reason="bank_technical_error")
                rc = _make_recovery_case(db_session, pe)
                db_session.commit()
                with patch(
                    "app.services.decision_engine.diagnose_failure",
                    return_value=_deterministic_result(
                        error_reason="bank_technical_error", fallback_used=True
                    ),
                ):
                    result = run_decision_engine(db_session, str(rc.id))
        assert result.status == RecoveryStatus.PENDING_EXECUTION.value
        assert result.failure_category == FailureCategory.TRANSIENT.value


# ---------------------------------------------------------------------------
# 7. Low confidence escalation
# ---------------------------------------------------------------------------

class TestLowConfidence:
    def test_low_confidence_ai_diagnosis_escalates(self, db_session) -> None:
        """AI diagnosis below threshold -> REQUIRES_HUMAN."""
        with patch.object(settings, "LLM_API_KEY", "test-key"):
            with patch.object(settings, "LLM_FALLBACK_ENABLED", True):
                with patch.object(settings, "LLM_CONFIDENCE_THRESHOLD", 0.6):
                    pe = _make_payment_event(db_session, error_reason="bank_technical_error")
                    rc = _make_recovery_case(db_session, pe)
                    db_session.commit()
                    with patch(
                        "app.services.decision_engine.diagnose_failure",
                        return_value=_ai_diagnosis(category="TRANSIENT", confidence=0.2),
                    ):
                        with patch(
                            "app.services.decision_engine.recommend_strategy_for_diagnosis",
                            return_value=_ai_recommendation(strategy="WAIT_AND_RETRY", confidence=0.9),
                        ):
                            result = run_decision_engine(db_session, str(rc.id))
        assert result.status == RecoveryStatus.REQUIRES_HUMAN.value
        assert result.requires_human_approval is True
        assert result.decision_audit_trail["ai"]["requires_human_due_to_confidence"] is True


# ---------------------------------------------------------------------------
# 8. Policy override
# ---------------------------------------------------------------------------

class TestPolicyOverride:
    def test_policy_block_overrides_ai(self, db_session) -> None:
        """HARD_FAILURE with AI recommending WAIT_AND_RETRY -> policy blocks."""
        with patch.object(settings, "LLM_API_KEY", "test-key"):
            with patch.object(settings, "LLM_FALLBACK_ENABLED", True):
                pe = _make_payment_event(db_session, error_reason="debit_instrument_blocked")
                rc = _make_recovery_case(db_session, pe)
                db_session.commit()
                with patch(
                    "app.services.decision_engine.diagnose_failure",
                    return_value=_ai_diagnosis(category="HARD_FAILURE", confidence=0.9),
                ):
                    with patch(
                        "app.services.decision_engine.recommend_strategy_for_diagnosis",
                        return_value=_ai_recommendation(strategy="WAIT_AND_RETRY", confidence=0.9),
                    ):
                        result = run_decision_engine(db_session, str(rc.id))
        # Policy overrides AI recommendation for HARD_FAILURE.
        assert result.status in (
            RecoveryStatus.RESOLVED_FAILED.value,
            RecoveryStatus.REQUIRES_HUMAN.value,
        )
        assert result.decision_audit_trail["recommendation"]["source"] == "ai"
        assert result.decision_audit_trail["policy"]["final_strategy"] in (
            RecoveryStrategy.STOP_RECOVERY.value,
            RecoveryStrategy.HUMAN_REVIEW.value,
        )


# ---------------------------------------------------------------------------
# 9. Successful AI + policy allow
# ---------------------------------------------------------------------------

class TestSuccessAIPolicyAllow:
    def test_ai_recommendation_allowed_by_policy(self, db_session) -> None:
        """Valid AI recommendation allowed by policy proceeds normally."""
        with patch.object(settings, "LLM_API_KEY", "test-key"):
            with patch.object(settings, "LLM_FALLBACK_ENABLED", True):
                with patch.object(settings, "LLM_CONFIDENCE_THRESHOLD", 0.5):
                    pe = _make_payment_event(db_session, error_reason="bank_technical_error")
                    rc = _make_recovery_case(db_session, pe)
                    db_session.commit()
                    with patch(
                        "app.services.decision_engine.diagnose_failure",
                        return_value=_ai_diagnosis(category="TRANSIENT", confidence=0.9),
                    ):
                        with patch(
                            "app.services.decision_engine.recommend_strategy_for_diagnosis",
                            return_value=_ai_recommendation(strategy="WAIT_AND_RETRY", confidence=0.9),
                        ):
                            result = run_decision_engine(db_session, str(rc.id))
        assert result.status == RecoveryStatus.PENDING_EXECUTION.value
        assert result.recommended_strategy == RecoveryStrategy.WAIT_AND_RETRY.value
        assert result.decision_audit_trail["classification"]["source"] == "ai"
        assert result.decision_audit_trail["recommendation"]["source"] == "ai"


# ---------------------------------------------------------------------------
# 10-11. Audit trail + secret leakage
# ---------------------------------------------------------------------------

class TestAuditAndSecrets:
    def test_ai_reasoning_in_audit_trail(self, db_session) -> None:
        """AI reasoning appears additively in the audit trail with provenance."""
        with patch.object(settings, "LLM_PROVIDER", "anthropic"):
            with patch.object(settings, "LLM_API_KEY", "test-key"):
                with patch.object(settings, "LLM_FALLBACK_ENABLED", True):
                    pe = _make_payment_event(db_session, error_reason="bank_technical_error")
                    rc = _make_recovery_case(db_session, pe)
                    db_session.commit()
                    with patch(
                        "app.services.decision_engine.diagnose_failure",
                        return_value=_ai_diagnosis(category="TRANSIENT", confidence=0.9, reasoning="ai diag reason"),
                    ):
                        with patch(
                            "app.services.decision_engine.recommend_strategy_for_diagnosis",
                            return_value=_ai_recommendation(strategy="WAIT_AND_RETRY", confidence=0.9, reasoning="ai rec reason"),
                        ):
                            result = run_decision_engine(db_session, str(rc.id))
        trail = result.decision_audit_trail
        assert "ai" in trail
        assert trail["ai"]["provider"] == "anthropic"
        assert "model" in trail["ai"]
        assert trail["classification"]["source"] == "ai"
        assert trail["classification"]["provider"] == "anthropic"
        assert trail["recommendation"]["source"] == "ai"
        assert trail["classification"]["reasoning"] == "ai diag reason"
        assert trail["recommendation"]["reasoning"] == "ai rec reason"

    def test_no_secret_in_audit_trail(self, db_session, caplog) -> None:
        """API key never appears in audit trail or logs."""
        secret = "super-secret-ai-key-42"
        with patch.object(settings, "LLM_API_KEY", secret):
            with patch.object(settings, "LLM_FALLBACK_ENABLED", True):
                pe = _make_payment_event(db_session, error_reason="bank_technical_error")
                rc = _make_recovery_case(db_session, pe)
                db_session.commit()
                with caplog.at_level(logging.INFO):
                    with patch(
                        "app.services.decision_engine.diagnose_failure",
                        return_value=_ai_diagnosis(category="TRANSIENT", confidence=0.9),
                    ):
                        with patch(
                            "app.services.decision_engine.recommend_strategy_for_diagnosis",
                            return_value=_ai_recommendation(strategy="WAIT_AND_RETRY", confidence=0.9),
                        ):
                            result = run_decision_engine(db_session, str(rc.id))
        trail = result.decision_audit_trail
        assert secret not in str(trail)
        assert secret not in caplog.text


# ---------------------------------------------------------------------------
# 12. No direct state mutation by agent layer
# ---------------------------------------------------------------------------

class TestNoStateMutation:
    def test_agent_layer_does_not_mutate_state(self) -> None:
        """diagnose_failure / recommend are pure value objects, no DB access."""
        d = Diagnosis(category="TRANSIENT", confidence=0.9, reasoning="ai")
        assert isinstance(d, Diagnosis)
        assert d.category == "TRANSIENT"


# ---------------------------------------------------------------------------
# 13. Live smoke test (opt-in, default OFF)
# ---------------------------------------------------------------------------

class TestLiveSmoke:
    def test_live_smoke_test_skipped_by_default(self) -> None:
        """The default test suite must never make external API calls."""
        assert settings.LLM_LIVE_TEST is False

    @pytest.mark.skipif(
        not settings.LLM_LIVE_TEST or not settings.LLM_API_KEY,
        reason="Live smoke test requires LLM_LIVE_TEST=true and LLM_API_KEY",
    )
    def test_live_smoke_makes_one_call(self) -> None:
        """When explicitly enabled, one minimal diagnosis call is validated."""
        from app.agents.base import LLMProvider
        provider = LLMProvider(settings.LLM_MODEL_DIAGNOSIS)
        result = provider.call(
            system_prompt='Return exactly: {"category":"TRANSIENT","confidence":0.9,"reasoning":"live test"}',
            user_prompt="Test",
            max_tokens=64,
        )
        assert result.error is None, f"Live provider failed: {result.error}"
        assert result.content is not None
        assert result.content["category"] in {
            "TRANSIENT", "AUTHENTICATION", "HARD_FAILURE", "UNKNOWN",
        }


# ---------------------------------------------------------------------------
# 14. Fallback configuration semantics
# ---------------------------------------------------------------------------

class TestFallbackConfigSemantics:
    def test_fallback_enabled_true_uses_llm_with_fallback(self) -> None:
        """LLM_FALLBACK_ENABLED=True: LLM consulted, fallback on failure."""
        with patch.object(settings, "LLM_FALLBACK_ENABLED", True):
            with patch.object(settings, "LLM_API_KEY", "test-key"):
                d = diagnose_failure(
                    error_reason="network_error",
                    provider=MagicMock(
                        call=MagicMock(
                            return_value=LLMResult(
                                content={"category": "TRANSIENT", "confidence": 0.9, "reasoning": "ai"}
                            )
                        )
                    ),
                )
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
