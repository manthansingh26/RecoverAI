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
| `DATABASE_URL` | `postgresql+psycopg://recoverai:recoverai@localhost:5432/recoverai` | PostgreSQL connection string |
| `RAZORPAY_WEBHOOK_SECRET` | (empty) | Razorpay webhook HMAC secret |
| `EXECUTION_MODE` | `SIMULATION` | Execution mode: `SIMULATION` or `LIVE` |

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
| `GET` | `/health` | Health check |
| `GET` | `/api/dashboard/summary` | Dashboard summary metrics |
| `GET` | `/api/recovery-cases` | List cases with filtering/pagination |
| `GET` | `/api/recovery-cases/{id}` | Case detail with payment event and logs |
| `GET` | `/api/recovery-cases/{id}/execution-logs` | Paginated execution history |
| `POST` | `/api/recovery-cases/{id}/approve` | Approve human-review case |
| `POST` | `/api/recovery-cases/{id}/reject` | Reject human-review case |
| `POST` | `/api/recovery-cases/{id}/execute` | Manual simulation execution (dev only) |
| `POST` | `/api/dev/simulate/payment-failed` | Simulate payment.failed event (dev only) |
| `POST` | `/api/dev/process-recovery-workflow` | Trigger workflow processing (dev only) |
| `POST` | `/webhooks/razorpay` | Razorpay webhook ingestion |
