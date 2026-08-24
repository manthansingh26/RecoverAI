"""FastAPI dependencies for authentication, authorization, and CSRF protection.

Provides:
- ``get_current_operator`` — reads the session cookie, validates the session,
  and returns the authenticated Operator (or raises 401).
- ``require_permission`` — factory that returns a dependency guarding a route
  by operator permission.
- ``require_origin`` — validates Origin / Referer headers on state-changing
  browser endpoints (CSRF protection).
- ``get_client_ip`` — trusted-proxy-aware client IP extraction.
"""

import logging
import time

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.security import (
    SESSION_COOKIE_NAME,
    hash_session_token,
)
from app.db.session import get_db
from app.models.operator import Operator
from app.models.session import AuthSession
from app.core.roles import ROLE_PERMISSIONS, OperatorRole, role_has_permission

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Client IP (trusted-proxy aware)
# ---------------------------------------------------------------------------

def get_client_ip(request: Request) -> str:
    """Extract the client IP address, respecting trusted proxy headers.

    The application assumes it runs behind a trusted proxy (reverse proxy /
    load balancer) that sets ``X-Forwarded-For``. If the proxy is untrusted
    or misconfigured the header is ignored — the raw ``client.host`` is used.

    In production, always deploy behind a reverse proxy (nginx, ALB, etc.)
    and configure ``TRUSTED_HOSTS``.
    """
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded and settings.APP_ENV in ("development", "test"):
        # Trust X-Forwarded-For in dev/test environments where the proxy is
        # known to be local. In production this MUST be validated by the
        # reverse proxy stripping external X-Forwarded-For values.
        return forwarded.split(",")[0].strip()
    client = request.client
    return client.host if client is not None else "unknown"


# ---------------------------------------------------------------------------
# Session expiration helpers
# ---------------------------------------------------------------------------

def _session_is_expired(session: AuthSession) -> bool:
    """Check if a session is past its absolute TTL."""
    if session.expires_at is None:
        return True
    return session.expires_at.timestamp() < time.time()


def _session_is_idle(session: AuthSession) -> bool:
    """Check if a session has exceeded the idle timeout."""
    if session.last_seen_at is None:
        return True
    idle_limit = time.time() - settings.SESSION_IDLE_TTL_SECONDS
    return session.last_seen_at.timestamp() < idle_limit


def _session_is_revoked(session: AuthSession) -> bool:
    """Check if a session has been explicitly revoked."""
    if session.revoked_at is None:
        return False
    return session.revoked_at.timestamp() < time.time()


# ---------------------------------------------------------------------------
# Session lookup and throttled last_seen_at update
# ---------------------------------------------------------------------------

def _load_and_validate_session(
    db: Session,
    token_hash: str,
) -> AuthSession | None:
    """Look up a session by token hash and validate it is not expired/idle/revoked.

    Returns the session if valid, None otherwise (caller should raise 401).
    """
    stmt = select(AuthSession).where(AuthSession.token_hash == token_hash)
    session = db.execute(stmt).scalar_one_or_none()

    if session is None:
        return None

    # Check revocation
    if _session_is_revoked(session):
        return None

    # Check absolute TTL
    if _session_is_expired(session):
        return None

    # Check idle timeout
    if _session_is_idle(session):
        return None

    return session


def _maybe_update_last_seen(session: AuthSession, db: Session) -> None:
    """Throttled last_seen_at update — at most once per minute."""
    now = time.time()
    if session.last_seen_at is not None:
        elapsed = now - session.last_seen_at.timestamp()
        if elapsed < settings.SESSION_LAST_SEEN_THROTTLE_SECONDS:
            return

    from datetime import datetime, timezone
    session.last_seen_at = datetime.now(timezone.utc)
    db.commit()


# ---------------------------------------------------------------------------
# Main auth dependency
# ---------------------------------------------------------------------------

async def get_current_operator(
    request: Request,
    db: Session = Depends(get_db),
) -> Operator:
    """Authenticate the current request via the session cookie.

    Flow:
    1. Read the ``__Host-recoverai_session`` cookie.
    2. Hash it with SHA-256.
    3. Look up the session row.
    4. Validate expiry, idle timeout, revocation.
    5. Load the associated Operator.
    6. Check the operator is enabled.
    7. Throttled ``last_seen_at`` update.

    Returns:
        The authenticated Operator.

    Raises:
        HTTPException 401: Missing, invalid, or expired session.
    """
    raw_token = request.cookies.get(SESSION_COOKIE_NAME)
    if raw_token is None:
        logger.debug("No session cookie found")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
        )

    token_hash = hash_session_token(raw_token)
    session = _load_and_validate_session(db, token_hash)

    if session is None:
        logger.debug("Invalid or expired session token")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired session",
        )

    # Load the operator
    operator = db.get(Operator, session.operator_id)
    if operator is None:
        logger.warning("Session %s references missing operator %s", session.id, session.operator_id)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid session",
        )

    # Check operator is enabled
    if not operator.enabled:
        logger.info("Operator %s is disabled", operator.id)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Account disabled",
        )

    # Throttled last_seen_at update
    _maybe_update_last_seen(session, db)

    return operator


# ---------------------------------------------------------------------------
# Permission-based authorization
# ---------------------------------------------------------------------------

class PermissionDenied(HTTPException):
    """Raised when the authenticated operator lacks a required permission."""

    def __init__(self, permission: str) -> None:
        super().__init__(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Missing required permission: {permission}",
        )


def require_permission(permission: str):
    """Factory: return a dependency that checks the operator has *permission*.

    Usage::

        @router.post("/api/recovery-cases/{id}/approve")
        async def approve(
            *,
            operator: Operator = Depends(require_permission(Permission.APPROVE_CASE)),
            ...
        ):
            ...

    The dependency declares ``operator`` as its return type, so the route
    handler can use it for audit attribution.
    """
    async def _require_permission(
        operator: Operator = Depends(get_current_operator),
    ) -> Operator:
        if not role_has_permission(operator.role, permission):
            logger.info(
                "Operator %s (role=%s) denied permission %s",
                operator.id, operator.role, permission,
            )
            raise PermissionDenied(permission)
        return operator

    return _require_permission


# ---------------------------------------------------------------------------
# CSRF Origin / Referer validation (state-changing browser endpoints)
# ---------------------------------------------------------------------------

def _get_allowed_origins() -> set[str]:
    """Return the set of allowed origins for Origin/Referer validation.

    Merges CORS origins with explicit ALLOWED_ORIGINS config so the CSRF
    check works for both API and browser-origin callers.
    """
    origins: set[str] = set()
    if settings.APP_ENV in ("development", "test"):
        from app.main import _DEV_CORS_ORIGINS  # noqa: F811
        origins.update(_DEV_CORS_ORIGINS)
    origins.update(settings.cors_origins_list)
    origins.update(settings.allowed_origins_list)
    return origins


def _origin_is_allowed(origin: str) -> bool:
    """Check if a single origin value is in the allowed set."""
    allowed = _get_allowed_origins()
    return origin in allowed


async def require_origin(request: Request) -> None:
    """Validate Origin / Referer header on state-changing browser endpoints.

    CSRF protection for ``SameSite=Lax`` cookies. ``SameSite=Lax`` already
    blocks cross-site ``POST`` from top-level navigations, but this is a
    defence-in-depth check.

    The check is:
    1. If ``Origin`` is present, validate it against the allowed origins set.
    2. If ``Origin`` is absent (older UA / non-browser client), fall back to
       ``Referer``.
    3. If neither header is present, allow the request (non-browser client).

    Strict mode: only ``POST`` endpoints that are state-changing should use
    this dependency. GET endpoints are safe by definition (``SameSite=Lax``
    already protects them).

    Raises:
        HTTPException 403: Origin/Referer validation failed.
    """
    # Only check state-changing methods
    if request.method not in ("POST", "PUT", "PATCH", "DELETE"):
        return

    origin = request.headers.get("Origin")
    referer = request.headers.get("Referer")

    if origin:
        # Origin is the most reliable header
        if not _origin_is_allowed(origin):
            logger.warning("CSRF check failed: Origin %s not allowed", origin)
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Origin not allowed",
            )
    elif referer:
        # Fall back to Referer — extract the origin portion
        from urllib.parse import urlparse
        parsed = urlparse(referer)
        ref_origin = f"{parsed.scheme}://{parsed.netloc}".lower()
        if not _origin_is_allowed(ref_origin):
            logger.warning("CSRF check failed: Referer %s not allowed", ref_origin)
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Referer origin not allowed",
            )
    # else: no Origin or Referer — non-browser client, allow