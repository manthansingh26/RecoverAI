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
├── frontend/      — Web dashboard (future)
└── docs/          — Architecture decisions and design notes
```

## Local Setup

### Prerequisites

- Python 3.11+
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

### 4. Test the health endpoint

```bash
curl http://127.0.0.1:8000/health
```

Expected response:

```json
{"status": "ok", "service": "recoverai-backend"}
```

### 5. Run tests

```bash
cd backend
pytest -v
```
