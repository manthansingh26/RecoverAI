"""Pydantic schemas for LLM advisory outputs (Milestone 16A).

These are the ONLY structured outputs that may be consumed by business
logic. Validation is strict:

- category must be a valid FailureCategory value
- strategy must be a valid RecoveryStrategy value
- confidence must be in [0.0, 1.0]
- reasoning must be non-empty

Malformed or out-of-range model output fails validation and triggers the
deterministic fallback path — it can never propagate into financial logic.
"""

from pydantic import BaseModel, Field, field_validator

from app.models.enums import FailureCategory, RecoveryStrategy


class Diagnosis(BaseModel):
    """Validated LLM root-cause diagnosis for a payment failure."""

    category: str = Field(description="One of the FailureCategory enum values")
    confidence: float = Field(ge=0.0, le=1.0, description="Model confidence 0.0-1.0")
    reasoning: str = Field(min_length=1, description="Concise reasoning for the diagnosis")

    @field_validator("category")
    @classmethod
    def _category_must_be_valid(cls, v: str) -> str:
        if v not in {c.value for c in FailureCategory}:
            raise ValueError(f"Unsupported failure category: {v!r}")
        return v

    @field_validator("confidence")
    @classmethod
    def _confidence_in_range(cls, v: float) -> float:
        if not (0.0 <= v <= 1.0):
            raise ValueError(f"Confidence must be in [0.0, 1.0], got {v}")
        return v


class Recommendation(BaseModel):
    """Validated LLM recovery-strategy recommendation."""

    strategy: str = Field(description="One of the RecoveryStrategy enum values")
    confidence: float = Field(ge=0.0, le=1.0, description="Model confidence 0.0-1.0")
    reasoning: str = Field(min_length=1, description="Concise reasoning for the recommendation")

    @field_validator("strategy")
    @classmethod
    def _strategy_must_be_valid(cls, v: str) -> str:
        if v not in {s.value for s in RecoveryStrategy}:
            raise ValueError(f"Unsupported recovery strategy: {v!r}")
        return v

    @field_validator("confidence")
    @classmethod
    def _confidence_in_range(cls, v: float) -> float:
        if not (0.0 <= v <= 1.0):
            raise ValueError(f"Confidence must be in [0.0, 1.0], got {v}")
        return v
