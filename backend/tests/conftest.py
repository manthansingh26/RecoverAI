"""Shared test fixtures for RecoverAI test suite."""

import hashlib
import hmac
import json
import os

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import settings
from app.db.session import get_db
from app.main import app
from app.models.base import Base

# Test database — use the same PostgreSQL credentials but a separate database
# to avoid polluting development data.
# Parse the URL and replace only the database name part.
_base_url = settings.DATABASE_URL
# postgresql+psycopg://user:pass@host:port/dbname -> replace dbname
if _base_url.endswith("/recoverai"):
    TEST_DATABASE_URL = _base_url[: -len("recoverai")] + "recoverai_test"
else:
    # Fallback: use env var
    TEST_DATABASE_URL = os.environ.get(
        "TEST_DATABASE_URL",
        "postgresql+psycopg://recoverai:recoverai@localhost:5432/recoverai_test",
    )


@pytest.fixture(scope="session", autouse=True)
def _setup_test_database() -> None:
    """Create the test database if it does not exist, then create all tables."""
    # Connect to default 'postgres' database to create the test DB
    # Use the same user/pass from settings, just target the postgres database
    admin_url = _base_url.rsplit("/", 1)[0] + "/postgres"
    admin_engine = create_engine(admin_url, isolation_level="AUTOCOMMIT")

    db_name = TEST_DATABASE_URL.rsplit("/", 1)[1]
    with admin_engine.connect() as conn:
        # Terminate existing connections to the test DB
        result = conn.execute(
            text(
                f"SELECT 1 FROM pg_database WHERE datname = '{db_name}'"
            )
        )
        if result.scalar():
            conn.execute(
                text(
                    f"SELECT pg_terminate_backend(pg_stat_activity.pid) "
                    f"FROM pg_stat_activity "
                    f"WHERE pg_stat_activity.datname = '{db_name}' "
                    f"AND pid != pg_backend_pid()"
                )
            )
            conn.execute(text(f"DROP DATABASE IF EXISTS {db_name}"))
        conn.execute(text(f"CREATE DATABASE {db_name}"))
    admin_engine.dispose()

    # Now connect to test DB and create all tables
    test_engine = create_engine(TEST_DATABASE_URL)
    Base.metadata.create_all(bind=test_engine)
    test_engine.dispose()


@pytest.fixture()
def db_session() -> Session:
    """Provide a transactional database session for a single test.

    Truncates all data tables before each test to ensure clean state,
    then yields a session. Rolls back after the test.
    """
    test_engine = create_engine(TEST_DATABASE_URL)
    TestSessionLocal = sessionmaker(bind=test_engine)
    session = TestSessionLocal()

    # Clean data tables before each test to avoid cross-test contamination
    # (some services call db.commit() internally, persisting across sessions)
    with test_engine.connect() as conn:
        conn.execute(text("TRUNCATE execution_logs, recovery_cases, payment_events, customers CASCADE"))
        conn.commit()

    # Override the get_db dependency for FastAPI test client
    def override_get_db():
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = override_get_db

    yield session

    # Rollback after test to ensure isolation
    session.rollback()
    session.close()
    app.dependency_overrides.clear()
    test_engine.dispose()


def make_razorpay_signature(body: bytes, secret: str) -> str:
    """Helper to compute a valid Razorpay HMAC-SHA256 signature."""
    return hmac.new(
        key=secret.encode("utf-8"),
        msg=body,
        digestmod=hashlib.sha256,
    ).hexdigest()


def make_valid_payment_failed_body() -> dict:
    """Helper to create a valid Razorpay payment.failed payload."""
    return {
        "entity": "event",
        "event": "payment.failed",
        "account_id": "acc_test123",
        "created_at": 1700000000,
        "payload": {
            "payment": {
                "id": "pay_test123",
                "entity": "payment",
                "amount": 100000,
                "currency": "INR",
                "status": "failed",
                "order_id": "order_test123",
                "error_code": "payment_failed",
                "error_reason": "account_expired",
                "error_description": "The payment has failed.",
            }
        },
    }
