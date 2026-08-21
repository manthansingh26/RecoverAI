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
