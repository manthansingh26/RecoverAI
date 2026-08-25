# RecoverAI

**Event-driven payment recovery system** that analyzes failed payments, calculates deterministic recovery strategies, and safely executes approved recovery actions.

## Architecture

RecoverAI follows a three-engine pipeline:

1. **Ingestion Engine** — receives and normalizes failed payment events.
2. **Decision Engine** — scores recovery strategies using deterministic algorithms and applies policy guardrails.
3. **Execution Engine** — executes approved actions via Razorpay APIs with idempotency enforcement.

**Core principle:** AI recommends and explains → deterministic policy validates → controlled executor executes.

## Project Structure

```
recoverai/
├── backend/       — FastAPI backend (Python)
├── frontend/      — React admin dashboard (TypeScript)
└── docs/          — Architecture decisions and design notes
```

## Local Setup

### Prerequisites

- Python 3.11+
- Node.js 18+
- Docker & Docker Compose (for PostgreSQL)

### 1. Start PostgreSQL

```bash
docker compose up -d
```

### 2. Set up the backend

```bash
cd backend
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

### 3. Run the backend

```bash
uvicorn app.main:app --reload
```

The API will be available at `http://127.0.0.1:8000`.

### 4. Set up the frontend

```bash
cd frontend
npm install
cp .env.example .env
```

### 5. Run the frontend

```bash
npm run dev
```

The dashboard will be available at `http://localhost:3000`.

The Vite dev server proxies API requests to the backend automatically.

### 6. Run tests

```bash
# Backend tests
cd backend
source .venv/bin/activate
pytest -v

# Frontend build check
cd frontend
npm run build
```

## Environment Variables

### Backend (`.env`)

| Variable | Default | Description |
|----------|---------|-------------|
| `APP_ENV` | `development` | Environment: `development`, `staging`, `production` |
| `LOG_LEVEL` | `INFO` | Python logging level: `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL` |
| `DATABASE_URL` | `postgresql+psycopg://recoverai:recoverai@localhost:5432/recoverai` | PostgreSQL connection string |
| `RAZORPAY_WEBHOOK_SECRET` | (empty) | Razorpay webhook HMAC secret |
| `RAZORPAY_WEBHOOK_MAX_BODY_BYTES` | `1048576` | Max accepted webhook body size (1 MiB) |
| `WEBHOOK_MAX_EVENT_AGE_SECONDS` | `300` | Max webhook event age (replay/freshness window, 5 min) |
| `RAZORPAY_KEY_ID` | (empty) | Razorpay Test Mode key ID |
| `RAZORPAY_KEY_SECRET` | (empty) | Razorpay Test Mode key secret |
| `EXECUTION_MODE` | `SIMULATION` | Execution mode: `SIMULATION` or `RAZORPAY` |
| `SCHEDULER_ENABLED` | `false` | Enable the automatic recovery scheduler |
| `SCHEDULER_INTERVAL_SECONDS` | `30` | Scheduler cycle interval (must be ≥ 1) |
| `CORS_ORIGINS` | (empty) | Comma-separated allowed cross-origin origins (production) |
| `TRUSTED_HOSTS` | (empty) | Comma-separated allowed Host header values |
| `ALLOWED_ORIGINS` | (empty) | Origins allowed for Origin/Referer CSRF validation |
| `SESSION_COOKIE_TTL_SECONDS` | `28800` | Absolute session TTL (8 h) |
| `SESSION_IDLE_TTL_SECONDS` | `1800` | Idle session timeout (30 min) |
| `SESSION_LAST_SEEN_THROTTLE_SECONDS` | `60` | Minimum interval between last_seen_at updates |
| `LOGIN_MAX_ATTEMPTS` | `5` | Login failures before lockout |
| `LOGIN_ATTEMPT_WINDOW_SECONDS` | `900` | Window in which login failures count |
| `LOGIN_LOCKOUT_SECONDS` | `900` | Login lockout duration |
| `STUCK_CASE_RECEIVED_SECONDS` | `3600` | RECEIVED cases older than this are flagged as stuck |
| `STUCK_CASE_HUMAN_REVIEW_SECONDS` | `86400` | REQUIRES_HUMAN cases older than this are flagged as stuck |
| `STUCK_CASE_MAX_RESULTS` | `100` | Max stuck-case results returned |

### Frontend (`.env`)

| Variable | Default | Description |
|----------|---------|-------------|
| `VITE_API_BASE_URL` | (empty) | Backend API base URL. Leave empty for Vite proxy in dev. |

## Dashboard Routes

| Route | Description |
|-------|-------------|
| `/` | Dashboard Overview — summary metrics for the recovery system |
| `/recovery-cases` | Recovery Cases — paginated list with status/strategy filters |
| `/recovery-cases/:id` | Case Detail — full case info, human review, simulation execution, execution history |

## Dashboard Features

- **Dashboard Overview** — real-time metrics from `GET /api/dashboard/summary`
- **Recovery Cases List** — filterable, paginated table of all cases
- **Case Detail View** — complete case information with payment event details
- **Human Review UI** — approve or reject cases requiring human approval with confirmation dialogs
- **Simulation Execution** — manually trigger safe simulation execution (dev only)
- **Execution History** — expandable logs with request/response data
- **Responsive Design** — works on desktop, laptop, tablet, and mobile

## Simulation Mode

**⚠ This system operates in SIMULATION mode by default.**

- No real financial actions are performed
- The dashboard clearly displays the simulation indicator
- Manual execution endpoint is only available in development/test environments
- All backend safety and eligibility checks remain active
- The UI explicitly communicates that this is not a production financial system

## API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/health` | Liveness — process is alive (static) |
| `GET` | `/health/ready` | Readiness — DB reachable (+ scheduler running when enabled) |
| `GET` | `/metrics` | In-process operational counters (JSON) |
| `GET` | `/api/auth/me` | Current authenticated operator |
| `POST` | `/api/auth/login` | Operator login |
| `POST` | `/api/auth/logout` | Operator logout (idempotent) |
| `POST` | `/api/auth/change-password` | Change password (revokes other sessions) |
| `GET` | `/api/dashboard/summary` | Dashboard summary metrics |
| `GET` | `/api/dashboard/analytics` | Dashboard analytics |
| `GET` | `/api/dashboard/activity` | Live activity feed |
| `GET` | `/api/recovery-cases` | List cases with filtering/pagination |
| `GET` | `/api/recovery-cases/{id}` | Case detail with payment event and logs |
| `GET` | `/api/recovery-cases/{id}/execution-logs` | Paginated execution history |
| `POST` | `/api/recovery-cases/{id}/approve` | Approve human-review case (OPERATOR+) |
| `POST` | `/api/recovery-cases/{id}/reject` | Reject human-review case (OPERATOR+) |
| `POST` | `/api/recovery-cases/{id}/execute` | Manual simulation execution (dev only, OPERATOR+) |
| `POST` | `/api/recovery-cases/{id}/recovery-checkout` | Create/reuse recovery checkout order (OPERATOR+) |
| `POST` | `/api/payments/create-order` | Create Razorpay Test order (OPERATOR+) |
| `GET` | `/api/ops/stuck-cases` | Stuck-case diagnostics (OPERATOR+) |
| `POST` | `/api/dev/simulate/payment-failed` | Simulate payment.failed event (dev only) |
| `POST` | `/api/dev/simulate-payment-failure` | Scenario-driven simulation (dev only) |
| `POST` | `/api/dev/process-recovery-workflow` | Trigger workflow processing (dev only) |
| `POST` | `/webhooks/razorpay` | Razorpay webhook ingestion |

## Observability (Milestone 15B)

- **Correlation IDs** — every request (including webhooks) receives an
  `X-Request-ID` (propagated if provided and sane, otherwise generated). The
  ID is echoed on the response and included in every structured JSON log line.
- **Structured logging** — logs are emitted as JSON lines with
  `ts`/`level`/`logger`/`message`/`correlation_id`, honoring `LOG_LEVEL`.
- **`/health/ready`** — distinguishes *process alive* (`/health`) from
  *process + dependencies ready* (DB `SELECT 1`, plus the scheduler when
  `SCHEDULER_ENABLED=true`). Returns 503 when a required dependency is down.
- **`/metrics`** — lightweight in-process counters (webhook, scheduler,
  execution). Process-local only; no secrets or PII. Exposed as JSON.
- **`/api/ops/stuck-cases`** — OPERATOR+ read-only diagnostic that flags
  stuck `RECEIVED`, stuck `REQUIRES_HUMAN`, and overdue `PENDING_EXECUTION`
  cases using the `STUCK_CASE_*` thresholds.
