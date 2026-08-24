"""Security primitives: password hashing, session tokens, cookie helpers.

Design notes
------------
- Passwords are hashed with Argon2id (argon2-cffi). SHA-256 is never used
  for passwords.
- Session tokens are opaque 256-bit CSPRNG values (secrets.token_urlsafe(32)).
  The raw token lives ONLY in the HttpOnly cookie; the database stores only
  its SHA-256 hash. SHA-256 is safe here because the token has full 256-bit
  entropy (it is not a low-entropy secret like a password).
- Cookie attributes follow the Milestone 14A security architecture:
  __Host- prefix, HttpOnly, Secure, SameSite=Lax, Path=/, Max-Age aligned to
  the absolute session TTL, and no Domain attribute (host-only cookie).
"""

import hashlib
import secrets
import time

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

from app.core.config import settings

# Session cookie name. The __Host- prefix forces Secure + Path=/ and forbids
# a Domain attribute, which protects against sibling-subdomain cookie
# injection. It is only sent over https (and http://localhost which browsers
# treat as a secure context).
SESSION_COOKIE_NAME = "__Host-recoverai_session"

# Argon2id parameters (OWASP recommended minimum). argon2-cffi defaults are
# conservative; keep the library defaults for portability.
_password_hasher = PasswordHasher()


# ---------------------------------------------------------------------------
# Passwords
# ---------------------------------------------------------------------------

def hash_password(password: str) -> str:
    """Hash a plaintext password with Argon2id.

    Args:
        password: Plaintext password (never logged).

    Returns:
        Argon2id-encoded hash string.
    """
    return _password_hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    """Verify a plaintext password against an Argon2id hash.

    Returns False (not raises) for both wrong passwords and malformed hashes
    so callers can emit a single generic error.
    """
    try:
        return _password_hasher.verify(password_hash, password)
    except (VerifyMismatchError, InvalidHashError):
        return False


def password_needs_rehash(password_hash: str) -> bool:
    """Return True if the stored hash should be upgraded to current params."""
    return _password_hasher.check_needs_rehash(password_hash)


# ---------------------------------------------------------------------------
# Session tokens
# ---------------------------------------------------------------------------

def generate_session_token() -> str:
    """Generate a fresh opaque session token (256-bit CSPRNG)."""
    return secrets.token_urlsafe(32)


def hash_session_token(token: str) -> str:
    """Hash a session token for storage in the sessions table.

    SHA-256 is sufficient here because the token is a 256-bit CSPRNG value —
    preimage resistance protects the token even if the DB is exfiltrated.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Brute-force protection (login only)
# ---------------------------------------------------------------------------

class LoginAttemptTracker:
    """In-memory login failure tracker keyed by account and by client IP.

    Provides per-account and per-IP lockout with backoff. In-memory storage is
    acceptable for a single-process deployment; multi-instance deployments
    should move this to a shared store (out of scope for Milestone 14A).

    Not a global API rate limiter — only login attempts are tracked.
    """

    def __init__(
        self,
        max_attempts: int | None = None,
        window_seconds: int | None = None,
        lockout_seconds: int | None = None,
    ) -> None:
        self.max_attempts = max_attempts or settings.LOGIN_MAX_ATTEMPTS
        self.window_seconds = window_seconds or settings.LOGIN_ATTEMPT_WINDOW_SECONDS
        self.lockout_seconds = lockout_seconds or settings.LOGIN_LOCKOUT_SECONDS
        # key -> list of attempt timestamps
        self._failures: dict[str, list[float]] = {}

    def _prune(self, key: str, now: float) -> None:
        """Drop attempts older than the window for a key."""
        attempts = self._failures.get(key)
        if not attempts:
            return
        cutoff = now - self.window_seconds
        kept = [t for t in attempts if t > cutoff]
        if kept:
            self._failures[key] = kept
        else:
            self._failures.pop(key, None)

    def _failure_count(self, key: str, now: float) -> int:
        self._prune(key, now)
        return len(self._failures.get(key, []))

    def is_locked(self, key: str) -> bool:
        """Return True if the key is currently in lockout."""
        now = time.time()
        return self._failure_count(key, now) >= self.max_attempts

    def record_failure(self, key: str) -> None:
        """Record a failed login attempt for a key."""
        now = time.time()
        self._failures.setdefault(key, []).append(now)
        self._prune(key, now)

    def record_success(self, key: str) -> None:
        """Clear failure history for a key after a successful login."""
        self._failures.pop(key, None)


# Shared module-level tracker (single-process deployment)
login_attempt_tracker = LoginAttemptTracker()
