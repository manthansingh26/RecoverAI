"""Application configuration using Pydantic Settings."""

from pydantic import field_validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Environment-based application configuration.

    All values are loaded from environment variables (or a .env file).
    """

    APP_ENV: str = "development"
    LOG_LEVEL: str = "INFO"
    DATABASE_URL: str = "postgresql+psycopg://recoverai:recoverai@localhost:5432/recoverai"
    RAZORPAY_WEBHOOK_SECRET: str = ""

    # Milestone 13: Critical Production Hardening
    # Maximum accepted Razorpay webhook body size in bytes.
    # Enforced BEFORE the payload is fully buffered into application memory:
    #   - A Content-Length header above this limit is rejected early (413).
    #   - A streamed body that grows past this limit is also rejected (covers
    #     chunked / missing / lying Content-Length headers).
    # Default: 1 MiB. Legitimate Razorpay webhooks are a few KB.
    RAZORPAY_WEBHOOK_MAX_BODY_BYTES: int = 1048576

    # Comma-separated list of allowed cross-origin origins for production.
    # Development/test environments always allow the local frontend origins.
    # Leave empty in production to disable all cross-origin access.
    # Never set to "*" — this application uses credentials and a wildcard
    # origin combined with credentials is a security anti-pattern.
    CORS_ORIGINS: str = ""

    # Milestone 3: Recovery Decision & Policy Engine settings
    RECOVERY_MAX_RETRIES: int = 2
    RECOVERY_HIGH_VALUE_THRESHOLD_PAISE: int = 5000000
    RECOVERY_RETRY_DELAY_SECONDS: int = 1800

    # Milestone 5: Execution Engine settings
    EXECUTION_MODE: str = "SIMULATION"
    RAZORPAY_KEY_ID: str = ""
    RAZORPAY_KEY_SECRET: str = ""

    # Milestone 11: Automatic Recovery Scheduler settings
    # SCHEDULER_ENABLED defaults to False — must be explicitly enabled.
    # Reason: if EXECUTION_MODE is ever changed to RAZORPAY, an implicitly
    # enabled scheduler could trigger real financial actions without operator
    # intent. Explicit opt-in is required.
    SCHEDULER_ENABLED: bool = False
    # Must be >= 1 to prevent a busy loop.
    SCHEDULER_INTERVAL_SECONDS: int = 30

    # Milestone 14A: Authentication & Authorization settings
    # Session cookie lives at most SESSION_COOKIE_TTL_SECONDS (8h absolute TTL)
    # and is invalidated after SESSION_IDLE_TTL_SECONDS (30m) of inactivity.
    # last_seen_at is refreshed at most once per minute per session to avoid a
    # write on every authenticated request.
    SESSION_COOKIE_TTL_SECONDS: int = 28800
    SESSION_IDLE_TTL_SECONDS: int = 1800
    SESSION_LAST_SEEN_THROTTLE_SECONDS: int = 60

    # Login brute-force protection (login endpoint only). Failures are tracked
    # per-account AND per-client-IP; a key is locked out for
    # LOGIN_LOCKOUT_SECONDS once it reaches LOGIN_MAX_ATTEMPTS failures within
    # LOGIN_ATTEMPT_WINDOW_SECONDS.
    LOGIN_MAX_ATTEMPTS: int = 5
    LOGIN_ATTEMPT_WINDOW_SECONDS: int = 900
    LOGIN_LOCKOUT_SECONDS: int = 900

    # Comma-separated list of allowed Host headers for TrustedHostMiddleware.
    # Production must pin the real hostname(s) (e.g. "api.recoverai.example").
    # An empty value disables the host filter (dev/test convenience) — the
    # production startup assertion rejects an unsafe combination.
    TRUSTED_HOSTS: str = ""

    # The cookie is sent on requests to these origins (same-origin or sibling
    # subdomains of the same registrable domain). Used for server-side
    # Origin/Referer validation on state-changing endpoints.
    ALLOWED_ORIGINS: str = ""

    @field_validator("SCHEDULER_INTERVAL_SECONDS")
    @classmethod
    def _validate_interval(cls, v: int) -> int:
        if v < 1:
            raise ValueError(
                f"SCHEDULER_INTERVAL_SECONDS must be >= 1, got {v}"
            )
        return v

    @field_validator("RAZORPAY_WEBHOOK_MAX_BODY_BYTES")
    @classmethod
    def _validate_webhook_max_body_bytes(cls, v: int) -> int:
        if v < 1:
            raise ValueError(
                f"RAZORPAY_WEBHOOK_MAX_BODY_BYTES must be >= 1, got {v}"
            )
        return v

    @field_validator("CORS_ORIGINS")
    @classmethod
    def _validate_cors_origins(cls, v: str) -> str:
        for origin in (o.strip() for o in v.split(",")):
            if origin == "*":
                raise ValueError(
                    "CORS_ORIGINS must not contain '*' — this application uses "
                    "allow_credentials=True, which forbids wildcard origins."
                )
        return v

    @field_validator(
        "SESSION_COOKIE_TTL_SECONDS",
        "SESSION_IDLE_TTL_SECONDS",
        "SESSION_LAST_SEEN_THROTTLE_SECONDS",
        "LOGIN_MAX_ATTEMPTS",
        "LOGIN_ATTEMPT_WINDOW_SECONDS",
        "LOGIN_LOCKOUT_SECONDS",
    )
    @classmethod
    def _validate_positive_int(cls, v: int) -> int:
        if v < 1:
            raise ValueError(f"{cls.__name__} setting must be >= 1, got {v}")
        return v

    @property
    def cors_origins_list(self) -> list[str]:
        """Parse CORS_ORIGINS into a clean list of origins.

        Splits on commas, strips whitespace, and drops empty entries so the
        parser is safe for empty, whitespace-padded, and trailing-comma input.
        """
        return [o.strip() for o in self.CORS_ORIGINS.split(",") if o.strip()]

    @property
    def trusted_hosts_list(self) -> list[str]:
        """Parse TRUSTED_HOSTS into a clean list of hosts."""
        return [h.strip() for h in self.TRUSTED_HOSTS.split(",") if h.strip()]

    @property
    def allowed_origins_list(self) -> list[str]:
        """Parse ALLOWED_ORIGINS into a clean list of origins.

        Used by the CSRF Origin/Referer validation. The deployment assumption
        is same-origin or sibling subdomains under one registrable domain;
        cross-site flows are unsupported until SameSite=None + full CSRF.
        """
        return [o.strip() for o in self.ALLOWED_ORIGINS.split(",") if o.strip()]

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


settings = Settings()
