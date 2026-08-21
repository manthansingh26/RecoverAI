"""Pydantic schemas for the development simulation endpoints."""

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class SimulationRequest(BaseModel):
    """Request body for the raw development simulation endpoint (existing)."""

    event_id: str | None = None
    payment_id: str | None = None
    order_id: str | None = None
    amount_paise: int = Field(default=100000, ge=1)
    currency: str = "INR"
    error_code: str | None = "payment_failed"
    error_reason: str | None = "account_expired"
    error_description: str | None = "The payment has failed."


# ---------------------------------------------------------------------------
# Scenario-driven simulation (Milestone 8)
# ---------------------------------------------------------------------------


class SimulationScenario(str, Enum):
    """Pre-defined failure scenarios for demo workflows."""

    LOW_VALUE_TRANSIENT = "LOW_VALUE_TRANSIENT"
    MEDIUM_VALUE_RECOVERABLE = "MEDIUM_VALUE_RECOVERABLE"
    HIGH_VALUE_HUMAN_REVIEW = "HIGH_VALUE_HUMAN_REVIEW"
    PERMANENT_FAILURE = "PERMANENT_FAILURE"


class ScenarioSimulationRequest(BaseModel):
    """Request body for the scenario-driven simulation endpoint."""

    scenario: SimulationScenario


class WorkflowResultItem(BaseModel):
    """Single case result from workflow processing."""

    recovery_case_id: str
    previous_status: str
    new_status: str
    processed: bool
    message: str


class ExecutionResultItem(BaseModel):
    """Execution outcome when auto-execution occurred."""

    strategy: str
    execution_mode: str
    status: str
    previous_case_status: str
    new_case_status: str
    message: str


class SimulationResult(BaseModel):
    """Rich response from the scenario simulation endpoint."""

    success: bool
    scenario: str
    payment_id: str
    event_id: str
    recovery_case_id: str | None = None
    amount_paise: int
    currency: str = "INR"
    error_code: str | None = None
    error_reason: str | None = None

    # Fields populated after decision engine processing
    failure_category: str | None = None
    recommended_strategy: str | None = None
    recovery_probability: float | None = None
    status: str | None = None
    requires_human_approval: bool = False
    approved_by_human: bool | None = None

    # Execution result (only if auto-execution occurred)
    execution_result: ExecutionResultItem | None = None

    # Workflow summary
    workflow: WorkflowResultItem | None = None

    # Human-readable message
    message: str = ""

    # Whether this was a duplicate event
    duplicate: bool = False
