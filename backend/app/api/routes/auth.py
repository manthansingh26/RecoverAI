"""Authentication routes — login, logout, me, change-password.

Security properties (Milestone 14A):
- Opaque 256-bit CSPRNG session tokens (``secrets.token_urlsafe(32)``); the
  database stores only SHA-256 hashes. The raw token lives ONLY in the cookie.
- ``__Host-recoverai_session`` cookie: HttpOnly, Secure, SameSite=Lax,
  Path=/, Max-Age = absolute session TTL, no Domain attribute.
- Generic invalid-credentials responses (no account/field enumeration).
- Brute-force protection on login only: per-account + per-IP with lockout.
- Session rotation: a fresh token is issued on every login.
- Password change revokes all OTHER sessions and rotates the current token.
- Server-side logout deletes the session row (idempotent).
"""

import logging
import uuid

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel
from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from app.api.deps import (
    get_client_ip,
    get_current_operator,
    require_origin,
)
from app.core.config import settings
from app.core.security import (
    SESSION_COOKIE_NAME,
    generate_session_token,
    hash_password,
    hash_session_token,
    login_attempt_tracker,
    verify_password,
)
from app.core.roles import OperatorRole
from app.db.session import get_db
from app.models.operator import Operator
from app.models.session import AuthSession

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth", tags=["auth"])


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class LoginRequest(BaseModel):
    """Login credentials."""

    email: str
    password: str


class OperatorMe(BaseModel):
    """Authenticated operator identity (subset of Operator)."""

    id: str
    email: str
    role: OperatorRole
    must_change_password: bool


class ChangePasswordRequest(BaseModel):
    """Change-password request."""

    current_password: str
    new_password: str


class SuccessResponse(BaseModel):
    """Generic success envelope."""

    success: bool


# Dummy hash verified when an account does not exist, so login timing is
# indistinguishable between "no such account" and "wrong password".
_DUMMY_PASSWORD_HASH = hash_password("dummy-timing-equalizer")

# Login failure audit is written to the application log (WARNING) plus the
# in-memory LoginAttemptTracker. There is no dedicated audit table.
def _record_login_failure(email: str, ip: str) -> None:
    """Record a login failure for account + IP and emit an audit log line."""
    login_attempt_tracker.record_failure(f"acct:{email.lower()}")
    login_attempt_tracker.record_failure(f"ip:{ip}")
    logger.warning("Login failure audit: email=%s ip=%s", email, ip)


def _raise_invalid_credentials() -> None:
    """Raise a generic 401 that never reveals which field was wrong."""
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid email or password",
    )


def _login_locked(email: str, ip: str) -> bool:
    """Return True if either the account or IP is currently locked out."""
    return (
        login_attempt_tracker.is_locked(f"acct:{email.lower()}")
        or login_attempt_tracker.is_locked(f"ip:{ip}")
    )


def _set_session_cookie(response: Response, token: str) -> None:
    """Set the session cookie with the mandated attributes.

    ``__Host-`` prefix forces Secure + Path=/ and forbids Domain; FastAPI
    omits the Domain attribute when ``domain`` is left at its default (None).
    """
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        max_age=settings.SESSION_COOKIE_TTL_SECONDS,
        httponly=True,
        secure=True,
        samesite="lax",
        path="/",
    )


def _clear_session_cookie(response: Response) -> None:
    """Expire the session cookie (used on logout)."""
    response.delete_cookie(key=SESSION_COOKIE_NAME, path="/")


def _create_session(
    db: Session,
    operator: Operator,
    request: Request,
) -> tuple[str, AuthSession]:
    """Create a fresh session row and return (raw_token, session)."""
    token = generate_session_token()
    now = datetime.now(timezone.utc)
    session = AuthSession(
        id=uuid.uuid4(),
        operator_id=operator.id,
        token_hash=hash_session_token(token),
        created_at=now,
        last_seen_at=now,
        expires_at=now + timedelta(seconds=settings.SESSION_COOKIE_TTL_SECONDS),
        ip=get_client_ip(request),
        user_agent=(request.headers.get("User-Agent") or "")[:512],
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    return token, session


# ---------------------------------------------------------------------------
# POST /api/auth/login
# ---------------------------------------------------------------------------

@router.post("/login", response_model=OperatorMe)
async def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
) -> OperatorMe:
    """Authenticate an operator and establish a server-side session.

    Brute-force lockout applies per-account and per-IP before verification.
    On success, a fresh session token is issued (rotation) and the session
    cookie is set. ``last_login_at`` is updated and login failures are
    recorded for audit.
    """
    email = payload.email.strip().lower()
    ip = get_client_ip(request)

    # Brute-force lockout check (before credential verification).
    if _login_locked(email, ip):
        logger.warning(
            "Login blocked by lockout: email=%s ip=%s", email, ip
        )
        _raise_invalid_credentials()

    operator = db.execute(
        select(Operator).where(Operator.email == email)
    ).scalar_one_or_none()

    if operator is None:
        # Equalize timing against the wrong-password path, then record failure.
        verify_password(payload.password, _DUMMY_PASSWORD_HASH)
        _record_login_failure(email, ip)
        _raise_invalid_credentials()

    if not operator.enabled:
        # Disabled accounts get the same generic response (no account oracle).
        verify_password(payload.password, operator.password_hash)
        _record_login_failure(email, ip)
        _raise_invalid_credentials()

    if not verify_password(payload.password, operator.password_hash):
        _record_login_failure(email, ip)
        _raise_invalid_credentials()

    # Success — clear failure history, update last_login_at, rotate session.
    login_attempt_tracker.record_success(f"acct:{email}")
    login_attempt_tracker.record_success(f"ip:{ip}")

    operator.last_login_at = datetime.now(timezone.utc)
    db.commit()

    token, _ = _create_session(db, operator, request)
    _set_session_cookie(response, token)

    logger.info(
        "Login success audit: operator=%s role=%s ip=%s",
        operator.id, operator.role, ip,
    )

    return OperatorMe(
        id=str(operator.id),
        email=operator.email,
        role=operator.role,
        must_change_password=operator.must_change_password,
    )


# ---------------------------------------------------------------------------
# POST /api/auth/logout
# ---------------------------------------------------------------------------

@router.post(
    "/logout",
    dependencies=[Depends(require_origin)],
    response_model=SuccessResponse,
)
async def logout(
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
) -> SuccessResponse:
    """End the current session server-side and clear the cookie.

    Idempotent: calling logout with no (or an already-deleted) session
    returns success. The session row is deleted from the database.
    """
    raw_token = request.cookies.get(SESSION_COOKIE_NAME)
    if raw_token:
        token_hash = hash_session_token(raw_token)
        deleted = db.execute(
            delete(AuthSession).where(AuthSession.token_hash == token_hash)
        )
        db.commit()
        if deleted.rowcount:
            logger.info("Logout: deleted session for token hash %s", token_hash[:12])
        else:
            logger.info("Logout: session already gone (idempotent)")

    _clear_session_cookie(response)
    return SuccessResponse(success=True)


# ---------------------------------------------------------------------------
# GET /api/auth/me
# ---------------------------------------------------------------------------

@router.get("/me", response_model=OperatorMe)
async def me(
    operator: Operator = Depends(get_current_operator),
) -> OperatorMe:
    """Return the identity of the authenticated operator.

    Requires a valid session cookie. Used by the frontend to bootstrap the
    auth context on page load.
    """
    return OperatorMe(
        id=str(operator.id),
        email=operator.email,
        role=operator.role,
        must_change_password=operator.must_change_password,
    )


# ---------------------------------------------------------------------------
# POST /api/auth/change-password
# ---------------------------------------------------------------------------

@router.post(
    "/change-password",
    dependencies=[Depends(require_origin)],
    response_model=SuccessResponse,
)
async def change_password(
    payload: ChangePasswordRequest,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    operator: Operator = Depends(get_current_operator),
) -> SuccessResponse:
    """Change the authenticated operator's password.

    Rules:
    1. The current password must verify.
    2. The new password must be at least 12 characters.
    3. The new password must differ from the current one.
    4. All OTHER sessions are revoked (revoked_at set).
    5. The CURRENT session token is rotated and a new cookie is set.

    The ``must_change_password`` flag (set by bootstrap) is cleared on
    success.
    """
    if not verify_password(payload.current_password, operator.password_hash):
        logger.warning(
            "Change-password failed: wrong current password, operator=%s",
            operator.id,
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Current password is incorrect",
        )

    new_password = payload.new_password
    if len(new_password) < 12:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="New password must be at least 12 characters",
        )

    if verify_password(new_password, operator.password_hash):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="New password must be different from the current password",
        )

    # Find the current session (needed for token rotation and revocation).
    raw_token = request.cookies.get(SESSION_COOKIE_NAME)
    current_session = None
    if raw_token:
        current_session = db.execute(
            select(AuthSession).where(
                AuthSession.token_hash == hash_session_token(raw_token)
            )
        ).scalar_one_or_none()

    # Update the password hash and clear the force-change flag.
    operator.password_hash = hash_password(new_password)
    operator.must_change_password = False
    db.commit()

    # Revoke all OTHER sessions for this operator.
    if current_session is not None:
        db.execute(
            update(AuthSession)
            .where(
                AuthSession.operator_id == operator.id,
                AuthSession.id != current_session.id,
                AuthSession.revoked_at.is_(None),
            )
            .values(revoked_at=datetime.now(timezone.utc))
        )

    # Rotate the current session token (new cookie).
    if current_session is not None:
        new_token = generate_session_token()
        current_session.token_hash = hash_session_token(new_token)
        current_session.expires_at = datetime.now(timezone.utc) + timedelta(
            seconds=settings.SESSION_COOKIE_TTL_SECONDS
        )
        db.commit()
        _set_session_cookie(response, new_token)

    logger.info(
        "Password change audit: operator=%s", operator.id,
    )

    return SuccessResponse(success=True)
