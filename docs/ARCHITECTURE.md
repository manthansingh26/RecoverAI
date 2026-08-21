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
Read raw body bytes (no parsing)
        ↓
Verify HMAC-SHA256 signature (timing-safe)
        ↓
Parse JSON only after verification
        ↓
Check event type == payment.failed
        ↓
Normalize payload → PaymentEvent columns
        ↓
Persist PaymentEvent + RecoveryCase (atomic)
        ↓
Return { accepted: true, duplicate: false }
```

Idempotency is enforced at two levels:
1. Application-level: check existing external_event_id before insert.
2. Database-level: unique constraint on `payment_events.external_event_id`.

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
