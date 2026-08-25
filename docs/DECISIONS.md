# RecoverAI — Architectural Decisions

## ADR-001: FastAPI monolith for MVP

**Decision:** Use a single FastAPI application as the monolith for the initial MVP.

**Rationale:** FastAPI provides async support, automatic OpenAPI docs, Pydantic validation, and a minimal learning curve. A monolith allows rapid iteration during a hackathon without premature service decomposition.

**Consequences:** All backend logic lives in one deployable unit. We will extract services later if scaling demands it.

---

## ADR-002: PostgreSQL as primary database

**Decision:** Use PostgreSQL as the primary persistent data store.

**Rationale:** PostgreSQL is a battle-tested relational database with strong ACID guarantees, JSON support, and mature tooling. It is well-suited for financial data that requires integrity and queryability.

**Consequences:** A PostgreSQL instance must be available for local development and deployment.

---

## ADR-003: Lightweight database polling/scheduling for MVP

**Decision:** Use database polling or simple scheduled tasks instead of a dedicated task queue for the MVP.

**Rationale:** For a hackathon MVP, a lightweight polling approach avoids the operational complexity of a full task queue. It is sufficient to demonstrate the recovery pipeline end-to-end.

**Consequences:** May not scale for production workloads. Replace with a proper task queue (e.g., Celery + Redis) in a later milestone if needed.

---

## ADR-004: No Celery/Redis initially

**Decision:** Do not introduce Celery or Redis in the MVP.

**Rationale:** Adding Celery and Redis increases operational complexity and setup time. The MVP can demonstrate the core recovery logic without them. This aligns with ADR-003.

**Consequences:** Background task processing will be basic. We will revisit this when production-grade scheduling is needed.

---

## ADR-005: Deterministic scoring instead of fake LLM probability scores

**Decision:** All recovery scores are calculated using deterministic algorithms, not simulated or fake LLM-generated probabilities.

**Rationale:** Financial decision-making requires transparency, reproducibility, and auditability. Fake probability scores create a false sense of sophistication and undermine trust in the system.

**Consequences:** Recovery scores are explainable and testable. AI can annotate scores with context but cannot override them.

---

## ADR-006: AI cannot directly execute financial actions

**Decision:** AI models are restricted to providing explanations, recommendations, and contextual annotations. They are never granted direct access to payment execution APIs.

**Rationale:** AI outputs can be hallucinated, inconsistent, or adversarially influenced. Financial actions must go through deterministic validation and explicit approval gates.

**Consequences:** The execution pipeline requires a human-approved or policy-approved step before any external API call. This adds a deliberate checkpoint but prevents catastrophic errors.

---

## ADR-007: Idempotency is mandatory before real execution

**Decision:** Every external execution call (payment retry, refund, communication) must include an idempotency key and must be checked for prior execution before proceeding.

**Rationale:** Network failures, retries, and race conditions can cause duplicate financial actions. Idempotency prevents double-charging, double-refunding, and duplicate communications.

**Consequences:** The execution engine must maintain an idempotency log. This is a non-negotiable requirement before any real Razorpay integration.

---

## ADR-008: Razorpay integration must be based only on officially verified capabilities

**Decision:** Any Razorpay API integration will only use officially documented and verified capabilities from Razorpay's API documentation.

**Rationale:** Inventing or assuming API capabilities leads to integration failures and potentially incorrect financial operations. Razorpay's API surface must be verified against their official docs before implementation.

**Consequences:** We will research and confirm Razorpay's supported endpoints for payment retries, refunds, and communications before writing any integration code. No placeholder implementations will pretend to work.

---

## ADR-009: Alembic is the source of truth for schema migrations

**Decision:** All database schema changes must be managed through Alembic migrations. No `create_all()` calls from application code.

**Rationale:** Alembic provides versioned, reversible migration scripts that can be reviewed, tested, and applied consistently across environments. Using `create_all()` bypasses migration tracking and makes schema drift invisible.

**Consequences:** Developers must run `alembic revision --autogenerate` after model changes and review the generated migration before applying it. The `alembic_version` table tracks the current migration state.

---

## ADR-010: PostgreSQL JSONB for raw payloads and audit trails

**Decision:** Use PostgreSQL JSONB columns for `payment_events.raw_payload` and `recovery_cases.decision_audit_trail`.

**Rationale:** Raw provider event payloads are inherently schema-less and may vary across providers. JSONB allows flexible storage while still supporting indexing and querying. The decision audit trail captures variable-length structured data that doesn't warrant a separate normalized table at MVP stage.

**Consequences:** Raw payloads and audit trails are stored efficiently with binary indexing. We accept that these columns won't have the same referential integrity guarantees as normalized tables.

---

## ADR-011: Money stored in paise as integers

**Decision:** All monetary amounts are stored as `BigInteger` representing paise (or the smallest currency unit). No floating-point for money.

**Rationale:** Floating-point arithmetic introduces rounding errors that are unacceptable in financial systems. Integer representation in the smallest currency unit (paise for INR) avoids this entirely.

**Consequences:** All money calculations must use integer arithmetic. Display formatting (converting paise to rupees) happens only at the presentation layer.

---

## ADR-012: One payment event maps to at most one recovery case

**Decision:** Each payment event can produce zero or one recovery case. This is enforced at the database level with a unique constraint on `recovery_cases.payment_event_id`.

**Rationale:** A single payment failure should have a single recovery path tracked through its lifecycle. Multiple competing recovery attempts on the same event would create conflicts and audit confusion.

**Consequences:** If multiple recovery strategies are needed, they must be sequenced within a single recovery case rather than creating parallel cases.

---

## ADR-013: Execution idempotency keys are unique

**Decision:** Every execution log entry must have a unique `idempotency_key`, enforced at the database level.

**Rationale:** Idempotency keys prevent duplicate external API calls (e.g., double-charging, double-refunding). The database constraint is the final safety net, even if application logic also checks for duplicates.

**Consequences:** The idempotency key generation strategy must produce globally unique keys before any execution attempt. Duplicate keys will be rejected at the database level.

---

## ADR-014: Database constraints as a second line of defense

**Decision:** Database-level constraints (CHECK, UNIQUE, NOT NULL) are used as a safety net alongside application-level validation.

**Rationale:** Application code can have bugs. Database constraints catch violations at the data layer regardless of how the data was written (direct SQL, ORM, migration scripts, etc.).

**Consequences:** Key constraints include: `retry_count >= 0`, `recovery_probability` between 0 and 1, unique `idempotency_key`, unique `payment_event_id` on recovery cases, and non-null `amount_paise`. These constraints are defined in the ORM models and applied via Alembic migrations.

---

## ADR-015: Raw body verified before JSON parsing

**Decision:** The raw HTTP request body bytes must be captured and used for HMAC signature verification before any JSON parsing occurs.

**Rationale:** Razorpay's webhook signature is computed over the exact raw bytes. Parsing the JSON and re-encoding it could change key ordering or whitespace, producing a different byte sequence and a mismatched signature. Verifying the raw body ensures the signature check is faithful to Razorpay's computation.

**Consequences:** The webhook route reads `await request.body()` first, computes HMAC-SHA256 over those bytes, and only parses JSON after successful verification. This prevents both security bypass and false rejections.

---

## ADR-016: x-razorpay-event-id as primary webhook idempotency identity

**Decision:** Use the `x-razorpay-event-id` HTTP header as the primary identity for webhook idempotency, mapped to `PaymentEvent.external_event_id`.

**Rationale:** Razorpay guarantees unique event IDs per webhook delivery. This provides a reliable deduplication mechanism that survives network retries and duplicates. The database unique constraint on `external_event_id` is the safety net.

**Consequences:** Duplicate webhook deliveries with the same event ID are acknowledged with `duplicate: true` without creating new records. Race conditions are handled by catching IntegrityError on the unique constraint.

---

## ADR-017: Timing-safe signature comparison

**Decision:** Use `hmac.compare_digest()` for webhook signature verification instead of standard string equality (`==`).

**Rationale:** Standard string comparison short-circuits on the first mismatched character, leaking timing information that could be exploited to guess the correct signature character by character. `hmac.compare_digest()` takes constant time regardless of where the mismatch occurs.

**Consequences:** Signature verification is resistant to timing attacks. The slight performance cost is negligible for webhook processing.

---

## ADR-018: Simulation endpoint isolated from real webhook authentication

**Decision:** The development simulation endpoint (`POST /api/dev/simulate/payment-failed`) does not verify Razorpay webhook signatures, and is disabled outside development/test environments.

**Rationale:** The simulation endpoint exists for local testing and hackathon demos. It is a separate endpoint with its own URL path, not a bypass mode on the real webhook route. Disabling it outside dev/test prevents accidental use in production.

**Consequences:** The simulation endpoint uses the same core ingestion service as the real webhook, ensuring identical persistence behavior. It is clearly documented as a development-only tool.

---

## ADR-019: One payment.failed ingestion creates at most one PaymentEvent and one RecoveryCase

**Decision:** Each successful `payment.failed` webhook ingestion creates exactly one `PaymentEvent` and exactly one `RecoveryCase`, enforced atomically within a single database transaction.

**Rationale:** A single payment failure should produce a single recovery case. Multiple recovery attempts on the same event would create conflicts. Atomicity ensures no orphaned PaymentEvent without a RecoveryCase.

**Consequences:** The ingestion service uses `db.flush()` within a transaction to ensure both records are persisted together. If either operation fails, the entire transaction is rolled back. Duplicate events are detected and handled without creating additional records.

## ADR-020: Structured logging and per-request correlation IDs

**Decision:** All backend logs are emitted as structured JSON lines (timestamp, level, logger, message, correlation_id) via a JSON formatter, and every HTTP request (including webhooks) receives an `X-Request-ID` that is propagated from a sane incoming header or generated with a CSPRNG. The correlation ID is exposed on the response and threaded through logging via a `contextvars` context var.

**Rationale:** Plain-text logs without timestamps made it impossible to reconstruct a request's timeline or correlate log lines across services/stages. A per-request ID lets an operator trace a payment event from webhook ingress through execution without parsing prose.

**Consequences:** A pure-ASGI middleware assigns the ID; it NEVER reads the request body, so the webhook's raw-body HMAC pipeline is unaffected. `LOG_LEVEL` is now honored by `configure_logging()`. No database column is added — correlation is a log-context concern only.

## ADR-021: Webhook replay/freshness protection

**Decision:** Reject webhook events whose top-level `created_at` is older than `WEBHOOK_MAX_EVENT_AGE_SECONDS` (default 300 s / 5 min), but only for *novel* event-ids that would create new state. Known event-ids (delivery retries) bypass the freshness gate and are acknowledged idempotently. Stale events return HTTP 200 with `stale=true` — never 4xx — so Razorpay stops retrying.

**Rationale:** Razorpay's documented replay rule ("reject events where created_at is more than 5 minutes in the past") protects against replayed signed payloads, while at-least-once delivery requires that legitimate retries of already-processed events never be dropped. Late recovery stays valid because a `payment.captured` event is born at capture time, so its `created_at` is fresh even when a customer pays hours later.

**Consequences:** HMAC remains the first trust boundary (raw body → HMAC → event-id → JSON parse → freshness → processing). Missing/malformed `created_at` is tolerated (compatibility policy). No schema change.

## ADR-022: Server-side session authentication and RBAC

**Decision:** Operator authentication uses server-side, DB-backed sessions keyed by an opaque 256-bit CSPRNG token stored only in an HttpOnly, Secure, SameSite=Lax, `__Host-recoverai_session` cookie (8 h absolute TTL, 30 min idle TTL, throttled `last_seen_at` updates). Passwords are hashed with Argon2id. Authorization uses a VIEWER < OPERATOR < ADMIN role hierarchy with granular permissions enforced by route-level dependencies.

**Rationale:** Server-side sessions allow immediate revocation (logout, password change, disable), are resilient to XSS (HttpOnly), and provide a clean audit seam for actor attribution. RBAC keeps read-only viewers separate from operators who can approve/execute.

**Consequences:** Protected routes carry `require_permission(...)` dependencies; webhook and health endpoints remain machine-authenticated (HMAC) / public. Session rows are deleted on logout and rotated on password change. No JWT, no refresh tokens, no OAuth.
