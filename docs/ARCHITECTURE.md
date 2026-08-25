# RecoverAI — Architecture

## Overview

RecoverAI is an event-driven system that analyzes failed payments, calculates deterministic recovery strategies, applies strict financial policy guardrails, and safely executes approved recovery actions.

## Monorepo Structure

```
recoverai/
├── backend/          — Python / FastAPI API server
├── frontend/         — Web dashboard (future milestone)
└── docs/             — Architecture decisions and design notes
```

**Backend** owns all business logic: ingestion of failed payment events, deterministic scoring, policy validation, and safe execution of recovery actions via Razorpay APIs.

**Frontend** will provide a dashboard for monitoring failed payments, reviewing AI-generated explanations, and approving or overriding recovery actions.

## Backend Engines

The backend is organized into three conceptual engines, each responsible for a distinct phase of the recovery pipeline:

### 1. Ingestion Engine
- Receives failed payment events via Razorpay webhooks.
- Verifies HMAC-SHA256 webhook signatures using timing-safe comparison.
- Uses `x-razorpay-event-id` header for idempotent event processing.
- Normalizes Razorpay payloads into PaymentEvent-compatible data.
- Persists PaymentEvent and creates exactly one RecoveryCase per event.
- Handles duplicate events safely using database-level uniqueness constraints.
- Provides a development-only simulation endpoint (`POST /api/dev/simulate/payment-failed`).

### 2. Decision Engine
- Analyzes failed payment events using deterministic scoring.
- Calculates recovery probability, retry windows, and recommended actions.
- Applies strict financial policy guardrails (rate limits, attempt caps, amount thresholds).
- AI may provide contextual explanations, but **never** directly computes scores or makes execution decisions.

### 3. Execution Engine
- Executes only those recovery actions that have been approved by the Decision Engine.
- Interacts with Razorpay APIs for payment retries, refunds, and communication.
- Enforces idempotency keys before every external call.
- Every action is logged for audit purposes.

## Core Principle

```
AI recommends / explains
        ↓
Deterministic policy validates
        ↓
Controlled executor executes
```

AI is a **advisor**, not an **actor**. The deterministic policy engine has final authority over all financial actions.

## Database

RecoverAI uses PostgreSQL with SQLAlchemy 2.x ORM and Alembic for schema migrations.

### Core Tables

| Table | Purpose |
|---|---|
| **customers** | Minimal customer records (UUID PK, email, phone, lifetime value in paise, historical success rate). |
| **payment_events** | Raw failed payment events ingested from providers. Stores event type, external IDs, amount, error details, raw JSONB payload, and a payload hash for duplicate detection. |
| **recovery_cases** | Core state-machine record. One per payment event. Tracks status, failure category, recovery probability, recommended strategy, retry count, and human approval state. |
| **execution_logs** | Audit log for every execution attempt. Records idempotency key, action taken, execution mode (simulation vs. live), request/response data, and outcome. |

### Relationships

```
Customer ──< PaymentEvent ──── RecoveryCase ──< ExecutionLog
 (1:N)         (N:1, unique)        (1:N)
```

- A **customer** may have many **payment_events**.
- A **payment_event** maps to at most one **recovery_case**.
- A **recovery_case** may produce many **execution_logs**.

### Constraints

- `recovery_cases.payment_event_id` has a unique constraint (one recovery case per payment event).
- `execution_logs.idempotency_key` is unique and indexed.
- `recovery_cases.retry_count` is constrained to `>= 0`.
- `recovery_cases.recovery_probability` is constrained between 0 and 1 when not null.
- Monetary amounts use **integer paise** (BigInteger) — no floating-point for money.
- Timestamps are timezone-aware.
- Primary keys are UUIDs.

## Webhook Ingestion Flow

```
Razorpay POST /webhooks/razorpay
        ↓
Read raw body bytes (size cap enforced)
        ↓
Verify HMAC-SHA256 signature (timing-safe)
        ↓
Validate x-razorpay-event-id header
        ↓
Parse JSON only after verification
        ↓
Extract event type (payment.failed / payment.captured / order.paid / other)
        ↓
Evaluate top-level created_at freshness (Milestone 15A)
   ┌────┴────┐
   │ stale   │ fresh
   v         v
200 stale   event-type branch
(true)      │
            ├── payment.failed
            │   ├── known event-id → duplicate ACK (idempotent)
            │   └── novel → ingest → decision engine → auto-execute (SIMULATION)
            ├── payment.captured
            │   ├── correlated (notes / order_id / recovery_order)
            │   │   ├── stale → 200 stale(true), no mutation
            │   │   └── fresh → amount/currency gates → RESOLVED_SUCCESS
            │   └── uncorrelated → 200 accepted (unrelated order)
            ├── order.paid → 200 accepted (acknowledgement only)
            └── other → 200 accepted (no state change)
```

Idempotency is enforced at three levels:
1. Application-level: check existing external_event_id before insert.
2. Database-level: unique constraint on `payment_events.external_event_id`.
3. ExecutionLog: unique idempotency_key prevents duplicate PAYMENT_RECOVERED logs.

## Webhook Replay Protection (Milestone 15A)

Razorpay delivers webhooks **at least once** — duplicate and out-of-order
delivery is expected. The system protects against replay attacks using a
three-layer model:

1. **Idempotency** — `x-razorpay-event-id` is the primary idempotency key.
   A known event-id is always acknowledged as a duplicate regardless of
   age, so legitimate delivery retries are preserved.

2. **Replay / freshness** — The top-level webhook event `created_at` is
   compared against the current time. Events whose `created_at` is more
   than `WEBHOOK_MAX_EVENT_AGE_SECONDS` (default 300 s / 5 min) in the past
   are **ignored with HTTP 200 stale=true** so Razorpay stops retrying them.
   HMAC verification always runs first — an invalid signature is rejected
   at 401 regardless of the timestamp.

   Scoping (Design B): freshness is enforced only for *novel* event-ids
   that would create new state. A known event-id falls through to the
   normal idempotency path.

3. **Business correlation** — `payment.captured` resolves to a case via
   `notes.recovery_case_id`, original `external_order_id`, or the recovery
   order in the audit trail. Exact amount and currency validation gates
   prevent data-corruption attacks.

**Late recovery is preserved.** A `payment.captured` event is born at the
time the payment is captured, not the time of the original failure. When a
customer pays a recovery order hours later, the captured event has a fresh
`created_at` and passes the freshness gate normally. The five-minute rule
measures the age of the *event*, not the age of the business flow.

**Missing or malformed `created_at`** is tolerated (compatibility policy for
older fixtures and trimmed test payloads): the event is accepted without
freshness evaluation.

**`order.paid`** is never a resolution trigger. It is always acknowledged
without state mutation regardless of its `created_at`.

## Observability & Operational Reliability (Milestone 15B)

The system is observable and diagnosable without any new infrastructure:

- **Correlation IDs** — a pure-ASGI middleware assigns an `X-Request-ID` to
  every HTTP request (propagated if the incoming header is sane, otherwise
  generated with a CSPRNG). The ID is echoed on the response and threaded
  through logging via a `contextvars` context var. The middleware never reads
  the request body, so the webhook raw-body HMAC pipeline is unaffected.
- **Structured logging** — all logs are JSON lines (`ts`, `level`, `logger`,
  `message`, optional `correlation_id`) honoring `LOG_LEVEL`. Logs are
  time-lineable and machine-queryable.
- **Liveness vs readiness** — `GET /health` remains a static liveness probe.
  `GET /health/ready` additionally verifies DB connectivity (`SELECT 1`) and,
  when the scheduler is enabled, that it is running. Readiness returns 503
  when a required dependency is down, distinguishing "process alive" from
  "process + dependencies ready".
- **Scheduler status / heartbeat** — an in-process `SchedulerStatus` records
  `running`, last-cycle start/finish, duration, attempted/succeeded/failed/
  blocked counts, last error, and total cycles. It is exposed via
  `get_scheduler_status()` and consumed by `/health/ready`.
- **Lightweight metrics** — in-process counters (webhook received/verified/
  rejected_hmac/rejected_stale/duplicate/malformed/captured_resolved/
  captured_stale/processing_seconds; scheduler cycles/failed_cycles; execution
  attempts/failures) are exposed as JSON at `GET /metrics`. Counters are
  process-local, reset on restart, and contain only aggregate counts — never
  secrets, tokens, payloads, or PII.
- **Stuck-case diagnostics** — `GET /api/ops/stuck-cases` (OPERATOR+) is a
  read-only query flagging stuck `RECEIVED`, stuck `REQUIRES_HUMAN`, and
  overdue `PENDING_EXECUTION` cases using the `STUCK_CASE_*` thresholds. It
  never mutates state.

## Key Design Constraints

- Raw body is verified before JSON parsing.
- `x-razorpay-event-id` is the primary webhook idempotency identity.
- Signature verification uses timing-safe comparison (`hmac.compare_digest`).
- Simulation is isolated from real webhook authentication.
- One successful `payment.failed` ingestion creates at most one PaymentEvent and one RecoveryCase.
- AI must never directly execute financial actions.
- The deterministic policy engine has final authority.
- All actions must be auditable.
- Schema changes are managed exclusively through Alembic migrations.
- Observability (correlation IDs, logs, metrics, scheduler status, stuck-case
  diagnostics) adds zero infrastructure and zero schema changes.
