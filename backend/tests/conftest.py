"""Shared test fixtures for RecoverAI test suite."""

import hashlib
import hmac
import os
import time
import uuid

from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import settings
from app.core.roles import OperatorRole
from app.core.security import hash_password
from app.db.session import get_db
from app.main import app
from app.models.base import Base
from app.models.operator import Operator
from app.api.deps import get_current_operator

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


@pytest.fixture(autouse=True)
def _default_auth_override() -> None:
    """Provide a default authenticated ADMIN operator to every test.

    Milestone 14A: protected routes require operator auth. Existing tests
    (pre-14A) make unauthenticated requests; this override keeps them green.
    New auth tests pop ``app.dependency_overrides[get_current_operator]`` to
    exercise REAL authentication (see tests/test_auth.py).

    The default operator is a lightweight in-memory object — routes only read
    ``email``/``role`` for authorization and audit attribution.
    """
    default_operator = Operator(
        id=uuid.uuid4(),
        email="test@recoverai.local",
        password_hash=hash_password("test-password-123!"),
        role=OperatorRole.ADMIN.value,
        enabled=True,
        must_change_password=False,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    app.dependency_overrides[get_current_operator] = (
        lambda: default_operator
    )

    yield

    # Clear ALL overrides so a later test starts clean (db_session also sets
    # its own get_db override for the tests that request it).
    app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def _isolate_llm_keys() -> None:
    """Ensure tests run isolated from external LLM API keys by default."""
    orig_llm = settings.LLM_API_KEY
    orig_gemini = settings.GEMINI_API_KEY
    orig_openai = settings.OPENAI_API_KEY
    orig_anthropic = settings.ANTHROPIC_API_KEY
    settings.LLM_API_KEY = ""
    settings.GEMINI_API_KEY = ""
    settings.OPENAI_API_KEY = ""
    settings.ANTHROPIC_API_KEY = ""
    yield
    settings.LLM_API_KEY = orig_llm
    settings.GEMINI_API_KEY = orig_gemini
    settings.OPENAI_API_KEY = orig_openai
    settings.ANTHROPIC_API_KEY = orig_anthropic



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
        conn.execute(
            text(
                "TRUNCATE execution_logs, recovery_cases, payment_events, "
                "customers, sessions, operators CASCADE"
            )
        )
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
    app.dependency_overrides.pop(get_db, None)
    test_engine.dispose()


def make_razorpay_signature(body: bytes, secret: str) -> str:
    """Helper to compute a valid Razorpay HMAC-SHA256 signature."""
    return hmac.new(
        key=secret.encode("utf-8"),
        msg=body,
        digestmod=hashlib.sha256,
    ).hexdigest()


def make_valid_payment_failed_body() -> dict:
    """Helper to create a valid Razorpay payment.failed payload.

    ``created_at`` is set to ``int(time.time())`` (the current timestamp)
    so the freshness gate in the replay-protection path does not reject it.
    Tests that need a specific or stale ``created_at`` override this field
    on the returned dict.
    """
    return {
        "entity": "event",
        "event": "payment.failed",
        "account_id": "acc_test123",
        "created_at": int(time.time()),
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
