"""Tests for Milestone 14A authentication and authorization.

These tests exercise REAL auth (the ``get_current_operator`` dependency), not
the default conftest override. Each test that needs real auth removes
``app.dependency_overrides[get_current_operator]``; the conftest restores a
clean state after every test.

Coverage (per the Milestone 14A spec):
- login success / invalid password / unknown user / generic errors
- session cookie flags
- token rotation + fixation regression
- /me, logout, expired session, idle timeout, revoked session
- throttled last_seen_at
- password change + revoke other sessions
- role checks (401 / 403), disabled operator
- 404 for nonexistent/unauthorized case (no existence oracle)
- brute-force lockout (per-account + per-IP)
- actor attribution
- Origin/Referer CSRF validation
- public routes + no APP_ENV leak on GET /
- mandatory route-coverage walk of app.routes
"""

import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import select

from app.core.roles import OperatorRole, Permission
from app.core.security import (
    SESSION_COOKIE_NAME,
    hash_password,
    hash_session_token,
)
from app.main import app
from app.models.operator import Operator
from app.models.session import AuthSession
from app.api.deps import get_current_operator

pytestmark = pytest.mark.usefixtures("db_session")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _client() -> httpx.AsyncClient:
    """Create an async test client against the FastAPI app."""
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://testserver")


def _disable_auth_override() -> None:
    """Remove the default authenticated-operator override (real auth)."""
    app.dependency_overrides.pop(get_current_operator, None)


def _create_operator(
    db,
    *,
    email: str = "operator@recoverai.local",
    password: str = "correct-horse-battery-staple",
    role: str = OperatorRole.OPERATOR.value,
    enabled: bool = True,
) -> Operator:
    """Create an operator directly in the database."""
    op = Operator(
        id=uuid.uuid4(),
        email=email,
        password_hash=hash_password(password),
        role=role,
        enabled=enabled,
        must_change_password=False,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    db.add(op)
    db.commit()
    db.refresh(op)
    return op


def _create_session(
    db,
    operator: Operator,
    *,
    raw_token: str,
    expires_at: datetime | None = None,
    last_seen_at: datetime | None = None,
    revoked_at: datetime | None = None,
) -> AuthSession:
    """Create a session row directly (bypasses login) for expiry tests."""
    now = datetime.now(timezone.utc)
    session = AuthSession(
        id=uuid.uuid4(),
        operator_id=operator.id,
        token_hash=hash_session_token(raw_token),
        created_at=now,
        last_seen_at=last_seen_at or now,
        expires_at=expires_at or (now + timedelta(seconds=28800)),
        ip="127.0.0.1",
        user_agent="pytest",
        revoked_at=revoked_at,
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    return session


def _cookie_value(response: httpx.Response) -> str | None:
    """Extract the session cookie value from a response's Set-Cookie header."""
    for directive in response.headers.get_list("set-cookie"):
        if directive.startswith(SESSION_COOKIE_NAME + "="):
            return directive.split(";")[0].split("=", 1)[1]
    return None


# ---------------------------------------------------------------------------
# Public routes + no env leak
# ---------------------------------------------------------------------------

class TestPublicRoutes:
    @pytest.mark.asyncio
    async def test_root_does_not_leak_env(self) -> None:
        async with await _client() as c:
            resp = await c.get("/")
            assert resp.status_code == 200
            body = resp.json()
            assert body["message"] == "RecoverAI API"
            assert "env" not in body
            assert "development" not in body

    @pytest.mark.asyncio
    async def test_health_is_public(self) -> None:
        async with await _client() as c:
            resp = await c.get("/health")
            assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_webhook_is_public(self) -> None:
        # Webhook is machine-authenticated (HMAC), not operator-authenticated.
        async with await _client() as c:
            resp = await c.post("/webhooks/razorpay", content=b"{}")
            assert resp.status_code != 401  # not gated on operator auth


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------

class TestLogin:
    @pytest.mark.asyncio
    async def test_login_success(self, db_session) -> None:
        _disable_auth_override()
        _create_operator(db_session, email="admin@example.com", password="strong-password-123")
        async with await _client() as c:
            resp = await c.post(
                "/api/auth/login",
                json={"email": "admin@example.com", "password": "strong-password-123"},
            )
            assert resp.status_code == 200
            body = resp.json()
            assert body["email"] == "admin@example.com"
            assert body["role"] == OperatorRole.OPERATOR.value
            assert body["id"]
            assert _cookie_value(resp) is not None

    @pytest.mark.asyncio
    async def test_login_invalid_password(self, db_session) -> None:
        _disable_auth_override()
        _create_operator(db_session, email="admin@example.com", password="strong-password-123")
        async with await _client() as c:
            resp = await c.post(
                "/api/auth/login",
                json={"email": "admin@example.com", "password": "wrong-password"},
            )
            assert resp.status_code == 401
            assert resp.json() == {"detail": "Invalid email or password"}

    @pytest.mark.asyncio
    async def test_login_unknown_user(self, db_session) -> None:
        _disable_auth_override()
        async with await _client() as c:
            resp = await c.post(
                "/api/auth/login",
                json={"email": "nobody@example.com", "password": "anything"},
            )
            assert resp.status_code == 401
            assert resp.json() == {"detail": "Invalid email or password"}

    @pytest.mark.asyncio
    async def test_login_errors_are_generic(self, db_session) -> None:
        """Wrong password and unknown user must be indistinguishable."""
        _disable_auth_override()
        _create_operator(db_session, email="admin@example.com", password="strong-password-123")
        async with await _client() as c:
            wrong_pw = await c.post(
                "/api/auth/login",
                json={"email": "admin@example.com", "password": "nope"},
            )
            unknown = await c.post(
                "/api/auth/login",
                json={"email": "ghost@example.com", "password": "nope"},
            )
            assert wrong_pw.status_code == unknown.status_code == 401
            assert wrong_pw.json() == unknown.json()

    @pytest.mark.asyncio
    async def test_session_cookie_flags(self, db_session) -> None:
        _disable_auth_override()
        _create_operator(db_session, email="admin@example.com", password="strong-password-123")
        async with await _client() as c:
            resp = await c.post(
                "/api/auth/login",
                json={"email": "admin@example.com", "password": "strong-password-123"},
            )
            set_cookie = resp.headers["set-cookie"]
            lowered = set_cookie.lower()
            assert SESSION_COOKIE_NAME + "=" in set_cookie
            assert "httponly" in lowered
            assert "secure" in lowered
            assert "samesite=lax" in lowered
            assert "path=/" in lowered
            assert "max-age=28800" in lowered
            assert "domain=" not in lowered  # __Host- forbids Domain

    @pytest.mark.asyncio
    async def test_token_rotation_on_login(self, db_session) -> None:
        """Each login issues a fresh token (old cookie differs)."""
        _disable_auth_override()
        _create_operator(db_session, email="admin@example.com", password="strong-password-123")
        async with await _client() as c:
            r1 = await c.post(
                "/api/auth/login",
                json={"email": "admin@example.com", "password": "strong-password-123"},
            )
            t1 = _cookie_value(r1)
            r2 = await c.post(
                "/api/auth/login",
                json={"email": "admin@example.com", "password": "strong-password-123"},
            )
            t2 = _cookie_value(r2)
            assert t1 is not None and t2 is not None
            assert t1 != t2

    @pytest.mark.asyncio
    async def test_session_fixation_regression(self, db_session) -> None:
        """A pre-set (attacker-known) cookie is not accepted after login."""
        _disable_auth_override()
        _create_operator(db_session, email="admin@example.com", password="strong-password-123")
        attacker_cookie = "attacker-controlled-token-value"
        async with await _client() as c:
            # 1. Attacker pre-sets the victim's cookie to a known value.
            # 2. Victim logs in — a fresh server-generated token must replace it.
            login = await c.post(
                "/api/auth/login",
                json={"email": "admin@example.com", "password": "strong-password-123"},
                cookies={SESSION_COOKIE_NAME: attacker_cookie},
            )
            assert login.status_code == 200
            fresh_token = _cookie_value(login)
            assert fresh_token != attacker_cookie

            # 3. The attacker's known value must not authenticate.
            bad = await c.get(
                "/api/auth/me", cookies={SESSION_COOKIE_NAME: attacker_cookie}
            )
            assert bad.status_code == 401

            # 4. The fresh token works.
            good = await c.get(
                "/api/auth/me", cookies={SESSION_COOKIE_NAME: fresh_token}
            )
            assert good.status_code == 200
            assert good.json()["email"] == "admin@example.com"


# ---------------------------------------------------------------------------
# /me, logout, expiry
# ---------------------------------------------------------------------------

class TestMeAndSessionLifetime:
    @pytest.mark.asyncio
    async def test_me_returns_identity(self, db_session) -> None:
        _disable_auth_override()
        _create_operator(db_session, email="me@example.com", password="strong-password-123")
        async with await _client() as c:
            login = await c.post(
                "/api/auth/login",
                json={"email": "me@example.com", "password": "strong-password-123"},
            )
            token = _cookie_value(login)
            resp = await c.get("/api/auth/me", cookies={SESSION_COOKIE_NAME: token})
            assert resp.status_code == 200
            body = resp.json()
            assert body["email"] == "me@example.com"
            assert body["role"] == OperatorRole.OPERATOR.value

    @pytest.mark.asyncio
    async def test_me_requires_auth(self) -> None:
        _disable_auth_override()
        async with await _client() as c:
            resp = await c.get("/api/auth/me")
            assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_logout_is_idempotent_and_clears_cookie(self, db_session) -> None:
        _disable_auth_override()
        _create_operator(db_session, email="lo@example.com", password="strong-password-123")
        async with await _client() as c:
            login = await c.post(
                "/api/auth/login",
                json={"email": "lo@example.com", "password": "strong-password-123"},
            )
            token = _cookie_value(login)
            resp = await c.post(
                "/api/auth/logout", cookies={SESSION_COOKIE_NAME: token}
            )
            assert resp.status_code == 200
            assert "Max-Age=0" in resp.headers["set-cookie"] or "expires" in resp.headers["set-cookie"]

            # Session row deleted — token is no longer valid.
            me = await c.get("/api/auth/me", cookies={SESSION_COOKIE_NAME: token})
            assert me.status_code == 401

            # Logout again with no session — still 200 (idempotent).
            again = await c.post("/api/auth/logout")
            assert again.status_code == 200

    @pytest.mark.asyncio
    async def test_expired_session_rejected(self, db_session) -> None:
        _disable_auth_override()
        op = _create_operator(db_session, email="exp@example.com", password="strong-password-123")
        past = datetime.now(timezone.utc) - timedelta(seconds=100)
        _create_session(db_session, op, raw_token="expired-token", expires_at=past)
        async with await _client() as c:
            resp = await c.get(
                "/api/auth/me", cookies={SESSION_COOKIE_NAME: "expired-token"}
            )
            assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_idle_session_rejected(self, db_session) -> None:
        _disable_auth_override()
        op = _create_operator(db_session, email="idle@example.com", password="strong-password-123")
        # Idle beyond the 1800s SESSION_IDLE_TTL_SECONDS, absolute TTL still valid.
        idle = datetime.now(timezone.utc) - timedelta(seconds=3600)
        _create_session(db_session, op, raw_token="idle-token", last_seen_at=idle)
        async with await _client() as c:
            resp = await c.get(
                "/api/auth/me", cookies={SESSION_COOKIE_NAME: "idle-token"}
            )
            assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_revoked_session_rejected(self, db_session) -> None:
        _disable_auth_override()
        op = _create_operator(db_session, email="rev@example.com", password="strong-password-123")
        _create_session(
            db_session,
            op,
            raw_token="revoked-token",
            revoked_at=datetime.now(timezone.utc) - timedelta(seconds=10),
        )
        async with await _client() as c:
            resp = await c.get(
                "/api/auth/me", cookies={SESSION_COOKIE_NAME: "revoked-token"}
            )
            assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_last_seen_throttled(self, db_session) -> None:
        _disable_auth_override()
        op = _create_operator(db_session, email="thr@example.com", password="strong-password-123")
        # Stale last_seen_at (within idle window, beyond the 60s throttle).
        stale = datetime.now(timezone.utc) - timedelta(seconds=120)
        _create_session(db_session, op, raw_token="throttle-token", last_seen_at=stale)
        async with await _client() as c:
            # First /me refreshes last_seen_at (elapsed > 60s).
            await c.get("/api/auth/me", cookies={SESSION_COOKIE_NAME: "throttle-token"})
            session_row = db_session.execute(
                select(AuthSession).where(
                    AuthSession.token_hash == hash_session_token("throttle-token")
                )
            ).scalar_one()
            first_seen = session_row.last_seen_at

            # Second /me within the throttle window must NOT update again.
            await c.get("/api/auth/me", cookies={SESSION_COOKIE_NAME: "throttle-token"})
            db_session.expire_all()
            session_row2 = db_session.execute(
                select(AuthSession).where(
                    AuthSession.token_hash == hash_session_token("throttle-token")
                )
            ).scalar_one()
            assert session_row2.last_seen_at == first_seen


# ---------------------------------------------------------------------------
# Password change
# ---------------------------------------------------------------------------

class TestChangePassword:
    @pytest.mark.asyncio
    async def test_change_password_rotates_and_revokes_others(self, db_session) -> None:
        _disable_auth_override()
        op = _create_operator(db_session, email="pw@example.com", password="original-password-123")
        async with await _client() as c:
            # Two sessions for the same operator (two devices).
            s1 = await c.post(
                "/api/auth/login",
                json={"email": "pw@example.com", "password": "original-password-123"},
            )
            token1 = _cookie_value(s1)
            s2 = await c.post(
                "/api/auth/login",
                json={"email": "pw@example.com", "password": "original-password-123"},
            )
            token2 = _cookie_value(s2)
            assert token1 != token2

            # Change password using session 1.
            resp = await c.post(
                "/api/auth/change-password",
                json={
                    "current_password": "original-password-123",
                    "new_password": "brand-new-password-456",
                },
                cookies={SESSION_COOKIE_NAME: token1},
            )
            assert resp.status_code == 200

            # Session 2 (other device) is revoked.
            me2 = await c.get("/api/auth/me", cookies={SESSION_COOKIE_NAME: token2})
            assert me2.status_code == 401

            # Session 1 token is rotated — the old token1 is now invalid.
            me1 = await c.get("/api/auth/me", cookies={SESSION_COOKIE_NAME: token1})
            assert me1.status_code == 401

            # The new rotated token (set on the change-password response) works.
            new_token = _cookie_value(resp)
            assert new_token is not None and new_token != token1
            me_new = await c.get("/api/auth/me", cookies={SESSION_COOKIE_NAME: new_token})
            assert me_new.status_code == 200

            # Old password no longer works; new one does.
            old_login = await c.post(
                "/api/auth/login",
                json={"email": "pw@example.com", "password": "original-password-123"},
            )
            assert old_login.status_code == 401
            new_login = await c.post(
                "/api/auth/login",
                json={"email": "pw@example.com", "password": "brand-new-password-456"},
            )
            assert new_login.status_code == 200

    @pytest.mark.asyncio
    async def test_change_password_rejects_short(self, db_session) -> None:
        _disable_auth_override()
        op = _create_operator(db_session, email="pw2@example.com", password="original-password-123")
        async with await _client() as c:
            login = await c.post(
                "/api/auth/login",
                json={"email": "pw2@example.com", "password": "original-password-123"},
            )
            token = _cookie_value(login)
            resp = await c.post(
                "/api/auth/change-password",
                json={"current_password": "original-password-123", "new_password": "short"},
                cookies={SESSION_COOKIE_NAME: token},
            )
            assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_change_password_rejects_wrong_current(self, db_session) -> None:
        _disable_auth_override()
        _create_operator(db_session, email="pw3@example.com", password="original-password-123")
        async with await _client() as c:
            login = await c.post(
                "/api/auth/login",
                json={"email": "pw3@example.com", "password": "original-password-123"},
            )
            token = _cookie_value(login)
            resp = await c.post(
                "/api/auth/change-password",
                json={
                    "current_password": "wrong-current",
                    "new_password": "brand-new-password-456",
                },
                cookies={SESSION_COOKIE_NAME: token},
            )
            assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Authorization (401 / 403 / 404)
# ---------------------------------------------------------------------------

class TestAuthorization:
    @pytest.mark.asyncio
    async def test_unauthenticated_dashboard_is_401(self) -> None:
        _disable_auth_override()
        async with await _client() as c:
            resp = await c.get("/api/dashboard/summary")
            assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_viewer_cannot_approve_403(self, db_session) -> None:
        _disable_auth_override()
        _create_operator(
            db_session,
            email="viewer@example.com",
            password="strong-password-123",
            role=OperatorRole.VIEWER.value,
        )
        async with await _client() as c:
            login = await c.post(
                "/api/auth/login",
                json={"email": "viewer@example.com", "password": "strong-password-123"},
            )
            token = _cookie_value(login)
            resp = await c.post(
                f"/api/recovery-cases/{uuid.uuid4()}/approve",
                cookies={SESSION_COOKIE_NAME: token},
            )
            assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_operator_can_approve_and_actor_is_recorded(self, db_session) -> None:
        _disable_auth_override()
        op = _create_operator(
            db_session,
            email="op@example.com",
            password="strong-password-123",
            role=OperatorRole.OPERATOR.value,
        )
        async with await _client() as c:
            login = await c.post(
                "/api/auth/login",
                json={"email": "op@example.com", "password": "strong-password-123"},
            )
            token = _cookie_value(login)

            # Create a REQUIRES_HUMAN case directly (with a payment event to
            # satisfy the NOT NULL FK).
            from app.models.payment_event import PaymentEvent
            from app.models.recovery_case import RecoveryCase
            from app.models.enums import RecoveryStatus, FailureCategory
            pe = PaymentEvent(
                id=uuid.uuid4(),
                event_type="payment.failed",
                external_event_id=f"evt_{uuid.uuid4().hex}",
                external_payment_id="pay_test_actor",
                external_order_id="order_test_actor",
                amount_paise=100000,
                currency="INR",
                error_code="payment_failed",
                error_reason="account_expired",
                error_description="desc",
                raw_payload={},
                payload_hash="actor-test-hash",
                created_at=datetime.now(timezone.utc),
            )
            db_session.add(pe)
            db_session.flush()
            case = RecoveryCase(
                id=uuid.uuid4(),
                payment_event_id=pe.id,
                status=RecoveryStatus.REQUIRES_HUMAN.value,
                failure_category=FailureCategory.TRANSIENT.value,
                retry_count=0,
                requires_human_approval=True,
                approved_by_human=None,
                decision_audit_trail={},
            )
            db_session.add(case)
            db_session.commit()

            resp = await c.post(
                f"/api/recovery-cases/{case.id}/approve",
                cookies={SESSION_COOKIE_NAME: token},
            )
            assert resp.status_code == 200

            db_session.expire_all()
            updated = db_session.get(RecoveryCase, case.id)
            assert updated.decision_audit_trail["approved_by"]["actor"] == op.email

    @pytest.mark.asyncio
    async def test_nonexistent_case_is_404(self, db_session) -> None:
        _disable_auth_override()
        _create_operator(db_session, email="op2@example.com", password="strong-password-123")
        async with await _client() as c:
            login = await c.post(
                "/api/auth/login",
                json={"email": "op2@example.com", "password": "strong-password-123"},
            )
            token = _cookie_value(login)
            missing = await c.get(
                f"/api/recovery-cases/{uuid.uuid4()}",
                cookies={SESSION_COOKIE_NAME: token},
            )
            assert missing.status_code == 404

    @pytest.mark.asyncio
    async def test_disabled_operator_cannot_login(self, db_session) -> None:
        _disable_auth_override()
        _create_operator(
            db_session,
            email="disabled@example.com",
            password="strong-password-123",
            enabled=False,
        )
        async with await _client() as c:
            resp = await c.post(
                "/api/auth/login",
                json={"email": "disabled@example.com", "password": "strong-password-123"},
            )
            # Generic error — must not reveal the disabled state.
            assert resp.status_code == 401
            assert resp.json() == {"detail": "Invalid email or password"}

    @pytest.mark.asyncio
    async def test_disabled_operator_session_rejected(self, db_session) -> None:
        _disable_auth_override()
        op = _create_operator(
            db_session,
            email="disabled2@example.com",
            password="strong-password-123",
            enabled=True,
        )
        _create_session(db_session, op, raw_token="disabled-session-token")
        # Now disable the operator.
        op.enabled = False
        db_session.commit()
        async with await _client() as c:
            resp = await c.get(
                "/api/auth/me", cookies={SESSION_COOKIE_NAME: "disabled-session-token"}
            )
            assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Brute-force lockout
# ---------------------------------------------------------------------------

class TestBruteForce:
    @pytest.mark.asyncio
    async def test_lockout_after_max_attempts(self, db_session) -> None:
        _disable_auth_override()
        _create_operator(db_session, email="bf@example.com", password="strong-password-123")
        async with await _client() as c:
            # 5 wrong-password attempts from the same IP.
            for _ in range(5):
                resp = await c.post(
                    "/api/auth/login",
                    json={"email": "bf@example.com", "password": "wrong"},
                    headers={"X-Forwarded-For": "203.0.113.7"},
                )
                assert resp.status_code == 401
                assert resp.json() == {"detail": "Invalid email or password"}

            # Correct password is STILL rejected while locked out.
            locked = await c.post(
                "/api/auth/login",
                json={"email": "bf@example.com", "password": "strong-password-123"},
                headers={"X-Forwarded-For": "203.0.113.7"},
            )
            assert locked.status_code == 401

    @pytest.mark.asyncio
    async def test_lockout_is_per_ip(self, db_session) -> None:
        """A locked-out IP blocks other accounts; a fresh IP is unaffected."""
        _disable_auth_override()
        _create_operator(db_session, email="a@example.com", password="strong-password-123")
        _create_operator(db_session, email="b@example.com", password="strong-password-123")
        async with await _client() as c:
            # Exhaust the shared IP 203.0.113.50 by attacking account A.
            for _ in range(5):
                await c.post(
                    "/api/auth/login",
                    json={"email": "a@example.com", "password": "wrong"},
                    headers={"X-Forwarded-For": "203.0.113.50"},
                )

            # Account B is fine, but the SAME IP is locked → 401.
            same_ip = await c.post(
                "/api/auth/login",
                json={"email": "b@example.com", "password": "strong-password-123"},
                headers={"X-Forwarded-For": "203.0.113.50"},
            )
            assert same_ip.status_code == 401

            # Account B from a FRESH IP is not locked → 200.
            fresh_ip = await c.post(
                "/api/auth/login",
                json={"email": "b@example.com", "password": "strong-password-123"},
                headers={"X-Forwarded-For": "203.0.113.99"},
            )
            assert fresh_ip.status_code == 200


# ---------------------------------------------------------------------------
# Origin / Referer CSRF validation
# ---------------------------------------------------------------------------

class TestOriginValidation:
    @pytest.mark.asyncio
    async def test_disallowed_origin_rejected(self, db_session) -> None:
        _disable_auth_override()
        _create_operator(db_session, email="csrf@example.com", password="strong-password-123")
        async with await _client() as c:
            login = await c.post(
                "/api/auth/login",
                json={"email": "csrf@example.com", "password": "strong-password-123"},
            )
            token = _cookie_value(login)
            resp = await c.post(
                f"/api/recovery-cases/{uuid.uuid4()}/approve",
                cookies={SESSION_COOKIE_NAME: token},
                headers={"Origin": "https://evil.example"},
            )
            assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_allowed_origin_passes(self, db_session) -> None:
        _disable_auth_override()
        _create_operator(db_session, email="csrf2@example.com", password="strong-password-123")
        async with await _client() as c:
            login = await c.post(
                "/api/auth/login",
                json={"email": "csrf2@example.com", "password": "strong-password-123"},
            )
            token = _cookie_value(login)
            # Allowed dev origin → not a CSRF block (proceeds to 404 for a
            # missing case, proving the Origin check did not reject).
            resp = await c.post(
                f"/api/recovery-cases/{uuid.uuid4()}/approve",
                cookies={SESSION_COOKIE_NAME: token},
                headers={"Origin": "http://localhost:3000"},
            )
            assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_disallowed_referer_rejected(self, db_session) -> None:
        _disable_auth_override()
        _create_operator(db_session, email="csrf3@example.com", password="strong-password-123")
        async with await _client() as c:
            login = await c.post(
                "/api/auth/login",
                json={"email": "csrf3@example.com", "password": "strong-password-123"},
            )
            token = _cookie_value(login)
            resp = await c.post(
                f"/api/recovery-cases/{uuid.uuid4()}/approve",
                cookies={SESSION_COOKIE_NAME: token},
                headers={"Referer": "https://evil.example/csrf.html"},
            )
            assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Route-coverage: every protected route carries auth
# ---------------------------------------------------------------------------

class TestRouteCoverage:
    """Walk app.routes and verify the auth wiring is correct and complete."""

    # Routes that are intentionally NOT operator-authenticated.
    PUBLIC = {
        ("GET", "/"),
        ("GET", "/health"),
        ("POST", "/webhooks/razorpay"),
        ("POST", "/api/auth/login"),
        ("POST", "/api/auth/logout"),  # idempotent, works without a session
    }

    # Map of path -> required permission for the covered business routes.
    REQUIRED_PERMISSIONS = {
        "/api/recovery-cases": Permission.VIEW_CASES,
        "/api/recovery-cases/{recovery_case_id}": Permission.VIEW_CASES,
        "/api/recovery-cases/{recovery_case_id}/execution-logs": Permission.VIEW_EXECUTION_LOGS,
        "/api/recovery-cases/{recovery_case_id}/approve": Permission.APPROVE_CASE,
        "/api/recovery-cases/{recovery_case_id}/reject": Permission.REJECT_CASE,
        "/api/recovery-cases/{recovery_case_id}/execute": Permission.EXECUTE_CASE,
        "/api/recovery-cases/{recovery_case_id}/recovery-checkout": Permission.CREATE_RECOVERY_CHECKOUT,
        "/api/dashboard/summary": Permission.VIEW_DASHBOARD,
        "/api/dashboard/analytics": Permission.VIEW_ANALYTICS,
        "/api/dashboard/activity": Permission.VIEW_ACTIVITY,
        "/api/payments/create-order": Permission.CREATE_PAYMENT_ORDER,
        "/api/dev/simulate/payment-failed": Permission.RUN_SIMULATION,
        "/api/dev/simulate-payment-failure": Permission.RUN_SIMULATION,
        "/api/dev/process-recovery-workflow": Permission.RUN_WORKFLOW,
    }

    def _flatten_deps(self, route):
        """Yield all dependency callables for a route, depth-first."""
        stack = list(route.dependant.dependencies)
        seen = set()
        while stack:
            dep = stack.pop()
            if dep is None or id(dep) in seen:
                continue
            seen.add(id(dep))
            if dep.call is not None:
                yield dep.call
            stack.extend(dep.dependencies or [])

    def _route_has_auth(self, route) -> bool:
        return get_current_operator in list(self._flatten_deps(route))

    def _route_permissions(self, route) -> set[str]:
        """Extract permission strings captured by require_permission closures."""
        perms: set[str] = set()
        for call in self._flatten_deps(route):
            if call is None or not getattr(call, "__closure__", None):
                continue
            for cell in call.__closure__:
                try:
                    value = cell.cell_contents
                except ValueError:
                    continue
                if isinstance(value, str) and value in set(Permission.__dict__.values()):
                    perms.add(value)
        return perms

    def test_all_api_routes_protected(self) -> None:
        for route in app.routes:
            path = getattr(route, "path", "")
            if not path or path.startswith(("/openapi.json", "/docs", "/redoc")):
                continue
            methods = set(route.methods or []) if hasattr(route, "methods") else set()
            if not methods:
                continue
            for method in methods:
                if (method, path) in self.PUBLIC:
                    continue
                if path.startswith("/api") or path.startswith("/webhooks"):
                    assert self._route_has_auth(route), (
                        f"{method} {path} is not public but lacks auth"
                    )

    def test_protected_routes_require_correct_permission(self) -> None:
        for path, expected_permission in self.REQUIRED_PERMISSIONS.items():
            route = next(r for r in app.routes if getattr(r, "path", "") == path)
            perms = self._route_permissions(route)
            assert expected_permission in perms, (
                f"{path} must require {expected_permission}, got {perms}"
            )

    def test_public_routes_are_public(self) -> None:
        for method, path in self.PUBLIC:
            route = next(r for r in app.routes if getattr(r, "path", "") == path)
            assert not self._route_has_auth(route), (
                f"{method} {path} should be public but requires auth"
            )

    def test_root_and_health_do_not_require_auth(self) -> None:
        for path in ("/", "/health"):
            route = next(r for r in app.routes if getattr(r, "path", "") == path)
            assert not self._route_has_auth(route)


# ---------------------------------------------------------------------------
# End-to-End Authentication Lifecycle & Role Boundary Tests (Milestone 14B)
# ---------------------------------------------------------------------------

class TestEndToEndLifecycleAndRBAC:
    @pytest.mark.asyncio
    async def test_full_e2e_login_me_dashboard_logout_cycle(self, db_session) -> None:
        """Verify the complete user journey: unauth -> login -> restore -> access -> logout -> unauth."""
        _disable_auth_override()
        _create_operator(
            db_session,
            email="e2e-operator@recoverai.local",
            password="secure-password-123",
            role=OperatorRole.OPERATOR.value,
        )

        async with await _client() as c:
            # 1. Unauthenticated request to protected route must fail with 401
            unauth_resp = await c.get("/api/dashboard/summary")
            assert unauth_resp.status_code == 401

            # 2. Login with valid credentials succeeds and issues __Host- cookie
            login_resp = await c.post(
                "/api/auth/login",
                json={
                    "email": "e2e-operator@recoverai.local",
                    "password": "secure-password-123",
                },
            )
            assert login_resp.status_code == 200
            user_data = login_resp.json()
            assert user_data["email"] == "e2e-operator@recoverai.local"
            assert user_data["role"] == OperatorRole.OPERATOR.value
            session_token = _cookie_value(login_resp)
            assert session_token is not None

            # 3. Session restore on refresh (/api/auth/me) returns identity
            me_resp = await c.get(
                "/api/auth/me",
                cookies={SESSION_COOKIE_NAME: session_token},
            )
            assert me_resp.status_code == 200
            assert me_resp.json()["email"] == "e2e-operator@recoverai.local"

            # 4. Access protected dashboard endpoints
            dash_resp = await c.get(
                "/api/dashboard/summary",
                cookies={SESSION_COOKIE_NAME: session_token},
            )
            assert dash_resp.status_code == 200

            # 5. Logout deletes session server-side
            logout_resp = await c.post(
                "/api/auth/logout",
                cookies={SESSION_COOKIE_NAME: session_token},
            )
            assert logout_resp.status_code == 200

            # 6. Subsequent requests with old session cookie must now fail with 401
            post_logout_me = await c.get(
                "/api/auth/me",
                cookies={SESSION_COOKIE_NAME: session_token},
            )
            assert post_logout_me.status_code == 401

            post_logout_dash = await c.get(
                "/api/dashboard/summary",
                cookies={SESSION_COOKIE_NAME: session_token},
            )
            assert post_logout_dash.status_code == 401

    @pytest.mark.asyncio
    async def test_e2e_viewer_vs_operator_action_boundaries(self, db_session) -> None:
        """Verify strict authorization enforcement between VIEWER and OPERATOR roles across all endpoints."""
        _disable_auth_override()
        _create_operator(
            db_session,
            email="viewer-e2e@recoverai.local",
            password="viewer-pass-123",
            role=OperatorRole.VIEWER.value,
        )
        _create_operator(
            db_session,
            email="operator-e2e@recoverai.local",
            password="operator-pass-123",
            role=OperatorRole.OPERATOR.value,
        )

        async with await _client() as c:
            # Login as VIEWER
            viewer_login = await c.post(
                "/api/auth/login",
                json={"email": "viewer-e2e@recoverai.local", "password": "viewer-pass-123"},
            )
            assert viewer_login.status_code == 200
            viewer_token = _cookie_value(viewer_login)

            # VIEWER can read
            viewer_dash = await c.get("/api/dashboard/summary", cookies={SESSION_COOKIE_NAME: viewer_token})
            assert viewer_dash.status_code == 200
            viewer_cases = await c.get("/api/recovery-cases", cookies={SESSION_COOKIE_NAME: viewer_token})
            assert viewer_cases.status_code == 200

            # VIEWER is forbidden from write/mutation actions (403)
            random_id = str(uuid.uuid4())
            viewer_approve = await c.post(f"/api/recovery-cases/{random_id}/approve", cookies={SESSION_COOKIE_NAME: viewer_token})
            assert viewer_approve.status_code == 403
            viewer_reject = await c.post(f"/api/recovery-cases/{random_id}/reject", cookies={SESSION_COOKIE_NAME: viewer_token})
            assert viewer_reject.status_code == 403
            viewer_exec = await c.post(f"/api/recovery-cases/{random_id}/execute", cookies={SESSION_COOKIE_NAME: viewer_token})
            assert viewer_exec.status_code == 403
            viewer_checkout = await c.post(f"/api/recovery-cases/{random_id}/recovery-checkout", cookies={SESSION_COOKIE_NAME: viewer_token})
            assert viewer_checkout.status_code == 403
            viewer_order = await c.post("/api/payments/create-order", json={"amount": 500}, cookies={SESSION_COOKIE_NAME: viewer_token})
            assert viewer_order.status_code == 403
            viewer_sim = await c.post("/api/dev/simulate-payment-failure", json={"scenario": "LOW_VALUE_TRANSIENT"}, cookies={SESSION_COOKIE_NAME: viewer_token})
            assert viewer_sim.status_code == 403

            # Login as OPERATOR
            op_login = await c.post(
                "/api/auth/login",
                json={"email": "operator-e2e@recoverai.local", "password": "operator-pass-123"},
            )
            assert op_login.status_code == 200
            op_token = _cookie_value(op_login)

            # OPERATOR has access to run simulation and read operations
            op_dash = await c.get("/api/dashboard/summary", cookies={SESSION_COOKIE_NAME: op_token})
            assert op_dash.status_code == 200
            op_sim = await c.post(
                "/api/dev/simulate-payment-failure",
                json={"scenario": "LOW_VALUE_TRANSIENT"},
                cookies={SESSION_COOKIE_NAME: op_token},
            )
            assert op_sim.status_code == 200
