# RecoverAI Backend

FastAPI backend for the RecoverAI payment recovery system.

## Prerequisites

- Python 3.11+
- Docker & Docker Compose (for PostgreSQL)

## Quick Start

### 1. Start PostgreSQL

```bash
cd recoverai
docker compose up -d
```

PostgreSQL will start on port 5432 with the default credentials from `.env.example`.

### 2. Set up the backend

```bash
cd backend
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

### 3. Run Alembic migrations

```bash
# Apply all migrations to create/update tables
alembic upgrade head

# Create a new migration after model changes
alembic revision --autogenerate -m "description of change"

# Review migration history
alembic history

# Downgrade one step
alembic downgrade -1
```

### 4. Start the backend

```bash
uvicorn app.main:app --reload
```

The API will be available at `http://127.0.0.1:8000`.

### 5. Test the health endpoint

```bash
curl http://127.0.0.1:8000/health
```

Expected response:

```json
{"status": "ok", "service": "recoverai-backend"}
```

## Running Tests

```bash
pytest -v
```

## Razorpay Webhook Ingestion

### Configuration

Set `RAZORPAY_WEBHOOK_SECRET` in your `.env` file:

```
RAZORPAY_WEBHOOK_SECRET=your_webhook_secret_here
```

### Webhook Endpoint

The real Razorpay webhook endpoint is:

```
POST /webhooks/razorpay
```

**Headers required:**
- `X-Razorpay-Signature` — HMAC-SHA256 signature over the raw request body.
- `x-razorpay-event-id` — Unique event identifier for idempotency.

**Signature verification:**
- Uses HMAC-SHA256 with `RAZORPAY_WEBHOOK_SECRET` as the key.
- Verifies against the **raw request body bytes** (not parsed JSON).
- Uses timing-safe comparison (`hmac.compare_digest`) to prevent timing attacks.

**Idempotency:**
- `x-razorpay-event-id` is the primary duplicate identity.
- Duplicate events return `{ accepted: true, duplicate: true }`.
- Race conditions are handled safely via database constraints.

**Event processing:**
- Only `payment.failed` events are ingested (creates PaymentEvent + RecoveryCase).
- Other valid event types are acknowledged but not processed.

### Development Simulation Endpoint

For local testing and demos:

```
POST /api/dev/simulate/payment-failed
```

**IMPORTANT:** This is a separate, development-only endpoint. It does NOT bypass or weaken the real webhook route.

- Only available when `APP_ENV=development` or `APP_ENV=test`.
- Returns 404 outside those environments.
- Uses the same core ingestion service as the real webhook.
- Duplicate simulation event IDs are idempotent.
- No signature verification (it's a dev tool, not a webhook).

### Testing Webhooks Locally

1. Start the backend with `APP_ENV=development`.
2. Use the simulation endpoint for quick tests.
3. For real webhook testing, use the Razorpay CLI or a tool like ngrok.

## Database Configuration

The database connection is configured via the `DATABASE_URL` environment variable in `.env`:

```
DATABASE_URL=postgresql+psycopg://recoverai:recoverai@localhost:5432/recoverai
```

**Important:** Alembic manages all schema changes. Never call `create_all()` from application code.
