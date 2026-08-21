"""Pydantic response models for the Recovery Dashboard API."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Recovery Case list item
# ---------------------------------------------------------------------------

class RecoveryCaseListItem(BaseModel):
    """Summary view of a RecoveryCase for list endpoints."""

    recovery_case_id: str
    status: str
    failure_category: str
    recommended_strategy: str | None = None
    retry_count: int
    next_run_at: datetime | None = None
    requires_human_approval: bool
    approved_by_human: bool | None = None
    created_at: datetime
    updated_at: datetime


# ---------------------------------------------------------------------------
# Recovery Case detail
# ---------------------------------------------------------------------------

class PaymentEventSummary(BaseModel):
    """Safe summary of a PaymentEvent for case detail view."""

    payment_event_id: str
    event_type: str
    external_payment_id: str | None = None
    external_order_id: str | None = None
    amount_paise: int
    currency: str
    error_code: str | None = None
    error_reason: str | None = None
    error_description: str | None = None
    created_at: datetime


class ExecutionLogSummary(BaseModel):
    """Summary of an ExecutionLog for case detail view."""

    execution_log_id: str
    action: str
    execution_mode: str
    status: str
    request_data: dict[str, Any] = {}
    response_data: dict[str, Any] = {}
    error_message: str | None = None
    executed_at: datetime | None = None
    created_at: datetime


class RecoveryCaseDetail(BaseModel):
    """Complete case detail view including related data."""

    recovery_case_id: str
    status: str
    failure_category: str
    recovery_probability: float | None = None
    priority_score: float | None = None
    recommended_strategy: str | None = None
    expected_value_paise: int | None = None
    retry_count: int
    next_run_at: datetime | None = None
    requires_human_approval: bool
    approved_by_human: bool | None = None
    created_at: datetime
    updated_at: datetime
    payment_event: PaymentEventSummary
    recent_execution_logs: list[ExecutionLogSummary] = []
    decision_audit_trail: dict[str, Any] = {}


# ---------------------------------------------------------------------------
# Pagination metadata
# ---------------------------------------------------------------------------

class PaginationMeta(BaseModel):
    """Pagination metadata."""

    total: int
    page: int
    page_size: int
    total_pages: int


# ---------------------------------------------------------------------------
# List response
# ---------------------------------------------------------------------------

class RecoveryCaseListResponse(BaseModel):
    """Paginated list of recovery cases."""

    items: list[RecoveryCaseListItem]
    pagination: PaginationMeta


# ---------------------------------------------------------------------------
# Execution logs response
# ---------------------------------------------------------------------------

class ExecutionLogsResponse(BaseModel):
    """Paginated list of execution logs for a case."""

    items: list[ExecutionLogSummary]
    pagination: PaginationMeta


# ---------------------------------------------------------------------------
# Approval/Rejection response
# ---------------------------------------------------------------------------

class ReviewActionResponse(BaseModel):
    """Response for approval or rejection action."""

    recovery_case_id: str
    previous_status: str
    new_status: str
    previous_approved_by_human: bool | None
    new_approved_by_human: bool | None
    action: str  # "approved" or "rejected"
    message: str


# ---------------------------------------------------------------------------
# Execution response
# ---------------------------------------------------------------------------

class ExecutionResponse(BaseModel):
    """Response for manual execution trigger."""

    recovery_case_id: str
    strategy: str
    execution_mode: str
    status: str
    previous_case_status: str
    new_case_status: str
    message: str


# ---------------------------------------------------------------------------
# Dashboard summary
# ---------------------------------------------------------------------------

class DashboardSummary(BaseModel):
    """Dashboard summary metrics."""

    total_cases: int = 0
    received_cases: int = 0
    pending_execution_cases: int = 0
    requires_human_cases: int = 0
    resolved_success_cases: int = 0
    resolved_failed_cases: int = 0
    awaiting_human_review: int = 0
    approved_cases: int = 0
    total_execution_attempts: int = 0
    successful_executions: int = 0
    failed_executions: int = 0
    blocked_executions: int = 0
