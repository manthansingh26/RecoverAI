/**
 * TypeScript types matching the actual backend Pydantic schemas.
 * Based on: backend/app/schemas/recovery_case.py
 */

// --- Pagination ---

export interface PaginationMeta {
  total: number;
  page: number;
  page_size: number;
  total_pages: number;
}

// --- Recovery Case List ---

export interface RecoveryCaseListItem {
  recovery_case_id: string;
  status: string;
  failure_category: string;
  recommended_strategy: string | null;
  retry_count: number;
  next_run_at: string | null;
  requires_human_approval: boolean;
  approved_by_human: boolean | null;
  created_at: string;
  updated_at: string;
}

export interface RecoveryCaseListResponse {
  items: RecoveryCaseListItem[];
  pagination: PaginationMeta;
}

// --- Payment Event Summary ---

export interface PaymentEventSummary {
  payment_event_id: string;
  event_type: string;
  external_payment_id: string | null;
  external_order_id: string | null;
  amount_paise: number;
  currency: string;
  error_code: string | null;
  error_reason: string | null;
  error_description: string | null;
  created_at: string;
}

// --- Execution Log Summary ---

export interface ExecutionLogSummary {
  execution_log_id: string;
  action: string;
  execution_mode: string;
  status: string;
  request_data: Record<string, unknown>;
  response_data: Record<string, unknown>;
  error_message: string | null;
  executed_at: string | null;
  created_at: string;
}

// --- Recovery Case Detail ---

export interface RecoveryCaseDetail {
  recovery_case_id: string;
  status: string;
  failure_category: string;
  recovery_probability: number | null;
  priority_score: number | null;
  recommended_strategy: string | null;
  expected_value_paise: number | null;
  retry_count: number;
  next_run_at: string | null;
  requires_human_approval: boolean;
  approved_by_human: boolean | null;
  created_at: string;
  updated_at: string;
  payment_event: PaymentEventSummary | null;
  recent_execution_logs: ExecutionLogSummary[];
  decision_audit_trail: Record<string, unknown>;
}

// --- Execution Logs Response ---

export interface ExecutionLogsResponse {
  items: ExecutionLogSummary[];
  pagination: PaginationMeta;
}

// --- Review Action Response ---

export interface ReviewActionResponse {
  recovery_case_id: string;
  previous_status: string;
  new_status: string;
  previous_approved_by_human: boolean | null;
  new_approved_by_human: boolean | null;
  action: string;
  message: string;
}

// --- Execution Response ---

export interface ExecutionResponse {
  recovery_case_id: string;
  strategy: string;
  execution_mode: string;
  status: string;
  previous_case_status: string;
  new_case_status: string;
  message: string;
}

// --- Dashboard Summary ---

export interface DashboardSummary {
  total_cases: number;
  received_cases: number;
  pending_execution_cases: number;
  requires_human_cases: number;
  resolved_success_cases: number;
  resolved_failed_cases: number;
  awaiting_human_review: number;
  approved_cases: number;
  total_execution_attempts: number;
  successful_executions: number;
  failed_executions: number;
  blocked_executions: number;
}

// --- Dashboard Analytics (Milestone 9A) ---

export interface StatusDistributionItem {
  status: string;
  count: number;
}

export interface StrategyDistributionItem {
  strategy: string;
  count: number;
}

export interface RecoveryPerformanceMetrics {
  total_cases: number;
  successful_cases: number;
  failed_cases: number;
  pending_cases: number;
  human_review_cases: number;
  success_rate: number;
}

export interface FinancialMetrics {
  total_failed_amount_paise: number;
  simulated_recovered_amount_paise: number;
  pending_recovery_amount_paise: number;
  human_review_amount_paise: number;
}

export interface HumanReviewMetrics {
  awaiting_review: number;
  approved: number;
  rejected: number;
}

export interface DailyActivityItem {
  date: string;
  count: number;
}

export interface DashboardAnalytics {
  status_distribution: StatusDistributionItem[];
  strategy_distribution: StrategyDistributionItem[];
  performance: RecoveryPerformanceMetrics;
  financial: FinancialMetrics;
  human_review: HumanReviewMetrics;
  daily_activity: DailyActivityItem[];
}

// --- Live Activity Feed (Milestone 9B) ---

export interface ActivityItem {
  id: string;
  type: string;
  title: string;
  description: string;
  occurred_at: string;
  recovery_case_id: string | null;
  payment_id: string | null;
  status: string | null;
  strategy: string | null;
  amount_paise: number | null;
}

export interface ActivityFeed {
  items: ActivityItem[];
  generated_at: string;
}

// --- API Error ---

export interface ApiError {
  detail: string;
}

// --- Simulation (Milestone 8) ---

export type SimulationScenario =
  | 'LOW_VALUE_TRANSIENT'
  | 'MEDIUM_VALUE_RECOVERABLE'
  | 'HIGH_VALUE_HUMAN_REVIEW'
  | 'PERMANENT_FAILURE';

export interface WorkflowResultItem {
  recovery_case_id: string;
  previous_status: string;
  new_status: string;
  processed: boolean;
  message: string;
}

export interface SimulationExecutionResult {
  strategy: string;
  execution_mode: string;
  status: string;
  previous_case_status: string;
  new_case_status: string;
  message: string;
}

export interface SimulationResult {
  success: boolean;
  scenario: string;
  payment_id: string;
  event_id: string;
  recovery_case_id: string | null;
  amount_paise: number;
  currency: string;
  error_code: string | null;
  error_reason: string | null;
  failure_category: string | null;
  recommended_strategy: string | null;
  recovery_probability: number | null;
  status: string | null;
  requires_human_approval: boolean;
  approved_by_human: boolean | null;
  execution_result: SimulationExecutionResult | null;
  workflow: WorkflowResultItem | null;
  message: string;
  duplicate: boolean;
}

// --- Razorpay Payment & Checkout ---

export interface CreateOrderRequest {
  amount: number;
  amount_in_rupees?: boolean;
  currency?: string;
  receipt?: string;
  notes?: Record<string, string>;
}

export interface CreateOrderResponse {
  key_id: string;
  order_id: string;
  amount: number;
  currency: string;
  receipt?: string | null;
}

export interface RazorpayPaymentSuccessResponse {
  razorpay_payment_id: string;
  razorpay_order_id: string;
  razorpay_signature: string;
}

export interface RazorpayPaymentErrorResponse {
  error: {
    code: string;
    description: string;
    source: string;
    step: string;
    reason: string;
    metadata: {
      order_id: string;
      payment_id?: string;
    };
  };
}

export interface RazorpayCheckoutOptions {
  key: string;
  amount: number;
  currency: string;
  name: string;
  description?: string;
  image?: string;
  order_id: string;
  handler?: (response: RazorpayPaymentSuccessResponse) => void;
  prefill?: {
    name?: string;
    email?: string;
    contact?: string;
    method?: string;
  };
  notes?: Record<string, string>;
  theme?: {
    color?: string;
    backdrop_color?: string;
    hide_topbar?: boolean;
  };
  modal?: {
    backdropclose?: boolean;
    escape?: boolean;
    handleback?: boolean;
    confirm_close?: boolean;
    ondismiss?: () => void;
    animation?: boolean;
  };
  retry?: {
    enabled?: boolean;
    max_count?: number;
  };
}

export interface RazorpayInstance {
  open(): void;
  on(
    event: 'payment.failed',
    handler: (response: RazorpayPaymentErrorResponse) => void,
  ): void;
}

export interface RazorpayConstructor {
  new (options: RazorpayCheckoutOptions): RazorpayInstance;
}

declare global {
  interface Window {
    Razorpay?: RazorpayConstructor;
  }
}
