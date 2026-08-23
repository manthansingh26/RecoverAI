"""Pydantic response models for the Recovery Dashboard API."""

from datetime import date, datetime
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
    resolved_strategy: str | None = None


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


# ---------------------------------------------------------------------------
# Dashboard analytics (Milestone 9A)
# ---------------------------------------------------------------------------

class StatusDistributionItem(BaseModel):
    """Count of recovery cases for a single status value."""

    status: str
    count: int = 0


class StrategyDistributionItem(BaseModel):
    """Count of recovery cases for a single recommended strategy."""

    strategy: str
    count: int = 0


class RecoveryPerformanceMetrics(BaseModel):
    """Aggregate recovery performance derived from actual case statuses.

    success_rate is calculated as:
        successful_cases / (successful_cases + failed_cases) * 100
    Protected from division-by-zero: returns 0.0 when denominator is 0.
    """

    total_cases: int = 0
    successful_cases: int = 0
    failed_cases: int = 0
    pending_cases: int = 0
    human_review_cases: int = 0
    success_rate: float = Field(
        default=0.0,
        description=(
            "Percentage of resolved cases that were successful. "
            "Zero when no resolved cases exist."
        ),
    )


class FinancialMetrics(BaseModel):
    """Financial impact aggregated from PaymentEvent amounts (in paise).

    All values represent simulated recovery since the project operates
    in SIMULATION execution mode. Frontend labels must indicate this.
    """

    total_failed_amount_paise: int = 0
    simulated_recovered_amount_paise: int = 0
    pending_recovery_amount_paise: int = 0
    human_review_amount_paise: int = 0


class HumanReviewMetrics(BaseModel):
    """Human review state counts.

    - awaiting_review: requires_human_approval=True AND approved_by_human IS NULL
    - approved: approved_by_human=True
    - rejected: approved_by_human=False
    """

    awaiting_review: int = 0
    approved: int = 0
    rejected: int = 0


class DailyActivityItem(BaseModel):
    """Recovery cases created on a single day."""

    date: date
    count: int = 0


class DashboardAnalytics(BaseModel):
    """Complete analytics response for the Recovery Intelligence dashboard."""

    status_distribution: list[StatusDistributionItem] = []
    strategy_distribution: list[StrategyDistributionItem] = []
    performance: RecoveryPerformanceMetrics = Field(
        default_factory=RecoveryPerformanceMetrics,
    )
    financial: FinancialMetrics = Field(default_factory=FinancialMetrics)
    human_review: HumanReviewMetrics = Field(
        default_factory=HumanReviewMetrics,
    )
    daily_activity: list[DailyActivityItem] = []


# ---------------------------------------------------------------------------
# Live Activity Feed (Milestone 9B)
# ---------------------------------------------------------------------------

class ActivityItem(BaseModel):
    """Single activity item derived from RecoveryCase, ExecutionLog, or PaymentEvent."""

    id: str
    type: str
    title: str
    description: str
    occurred_at: datetime
    recovery_case_id: str | None = None
    payment_id: str | None = None
    status: str | None = None
    strategy: str | None = None
    amount_paise: int | None = None


class ActivityFeed(BaseModel):
    """Live activity feed response."""

    items: list[ActivityItem] = []
    generated_at: datetime


# ---------------------------------------------------------------------------
# Recovery Checkout (Milestone 12)
# ---------------------------------------------------------------------------

class RecoveryCheckoutResponse(BaseModel):
    """Response model for recovery checkout order generation."""

    key_id: str
    order_id: str
    amount: int
    currency: str
    receipt: str
    recovery_case_id: str
    is_reused: bool = False
