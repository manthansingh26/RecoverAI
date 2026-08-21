"""Tests for the deterministic failure classifier.

Covers:
- All known TRANSIENT mappings
- All known AUTHENTICATION mappings
- All known HARD_FAILURE mappings
- Unknown reason fallback
- None input
- Case normalization
- Whitespace normalization
- Confidence range validation
"""

import pytest

from app.models.enums import FailureCategory
from app.services.failure_classifier import classify_failure


class TestTransientClassification:
    """Test TRANSIENT error reason mappings."""

    @pytest.mark.parametrize(
        "reason, expected_rule",
        [
            ("bank_technical_error", "rule_transient_bank_tech"),
            ("gateway_technical_error", "rule_transient_gateway_tech"),
            ("bank_cutoff_in_progress", "rule_transient_bank_cutoff"),
            ("network_error", "rule_transient_network"),
        ],
    )
    def test_transient_reasons(self, reason: str, expected_rule: str) -> None:
        result = classify_failure(reason)
        assert result.category == FailureCategory.TRANSIENT
        assert result.rule_id == expected_rule
        assert 0.0 <= result.confidence <= 1.0
        assert result.confidence > 0.5


class TestAuthenticationClassification:
    """Test AUTHENTICATION error reason mappings."""

    @pytest.mark.parametrize(
        "reason, expected_rule",
        [
            ("authentication_failed", "rule_auth_failed"),
            ("authorisation_declined_by_psp", "rule_auth_declined_psp"),
        ],
    )
    def test_authentication_reasons(self, reason: str, expected_rule: str) -> None:
        result = classify_failure(reason)
        assert result.category == FailureCategory.AUTHENTICATION
        assert result.rule_id == expected_rule
        assert 0.0 <= result.confidence <= 1.0
        assert result.confidence > 0.5


class TestHardFailureClassification:
    """Test HARD_FAILURE error reason mappings."""

    @pytest.mark.parametrize(
        "reason, expected_rule",
        [
            ("debit_instrument_blocked", "rule_hard_instrument_blocked"),
            ("beneficiary_account_dormant", "rule_hard_beneficiary_dormant"),
        ],
    )
    def test_hard_failure_reasons(self, reason: str, expected_rule: str) -> None:
        result = classify_failure(reason)
        assert result.category == FailureCategory.HARD_FAILURE
        assert result.rule_id == expected_rule
        assert 0.0 <= result.confidence <= 1.0
        assert result.confidence > 0.5


class TestUnknownClassification:
    """Test UNKNOWN fallback behavior."""

    def test_unknown_unmapped_reason(self) -> None:
        result = classify_failure("some_new_unknown_reason")
        assert result.category == FailureCategory.UNKNOWN
        assert result.confidence == 0.0
        assert result.rule_id == "rule_unknown_unmapped"

    def test_none_input(self) -> None:
        result = classify_failure(None)
        assert result.category == FailureCategory.UNKNOWN
        assert result.confidence == 0.0
        assert result.rule_id == "rule_unknown_missing"
        assert "None" in result.reason

    def test_empty_string(self) -> None:
        result = classify_failure("")
        assert result.category == FailureCategory.UNKNOWN
        assert result.confidence == 0.0
        assert result.rule_id == "rule_unknown_empty"

    def test_whitespace_only(self) -> None:
        result = classify_failure("   ")
        assert result.category == FailureCategory.UNKNOWN
        assert result.confidence == 0.0


class TestNormalization:
    """Test input normalization behavior."""

    def test_case_normalization_uppercase(self) -> None:
        result = classify_failure("BANK_TECHNICAL_ERROR")
        assert result.category == FailureCategory.TRANSIENT
        assert result.rule_id == "rule_transient_bank_tech"

    def test_case_normalization_mixed_case(self) -> None:
        result = classify_failure("Network_Error")
        assert result.category == FailureCategory.TRANSIENT

    def test_whitespace_trimmed(self) -> None:
        result = classify_failure("  network_error  ")
        assert result.category == FailureCategory.TRANSIENT
        assert result.rule_id == "rule_transient_network"

    def test_whitespace_and_case_combined(self) -> None:
        result = classify_failure("  Authentication_Failed  ")
        assert result.category == FailureCategory.AUTHENTICATION


class TestConfidenceRange:
    """Test that confidence values are bounded."""

    def test_all_known_reasons_have_valid_confidence(self) -> None:
        reasons = [
            "bank_technical_error",
            "gateway_technical_error",
            "bank_cutoff_in_progress",
            "network_error",
            "authentication_failed",
            "authorisation_declined_by_psp",
            "debit_instrument_blocked",
            "beneficiary_account_dormant",
        ]
        for reason in reasons:
            result = classify_failure(reason)
            assert 0.0 <= result.confidence <= 1.0, (
                f"Confidence {result.confidence} out of range for '{reason}'"
            )

    def test_unknown_confidence_is_zero(self) -> None:
        result = classify_failure("unmapped_reason")
        assert result.confidence == 0.0

    def test_result_is_frozen_dataclass(self) -> None:
        result = classify_failure("network_error")
        assert hasattr(result, "category")
        assert hasattr(result, "confidence")
        assert hasattr(result, "rule_id")
        assert hasattr(result, "reason")
