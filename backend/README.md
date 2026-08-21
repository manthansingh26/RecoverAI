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

## Database Configuration

The database connection is configured via the `DATABASE_URL` environment variable in `.env`:

```
DATABASE_URL=postgresql+psycopg://recoverai:recoverai@localhost:5432/recoverai
```

**Important:** Alembic manages all schema changes. Never call `create_all()` from application code.
