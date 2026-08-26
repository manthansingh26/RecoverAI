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

    # Milestone 15A: Webhook replay protection
    # Razorpay's documented replay rule: reject events whose top-level
    # created_at is more than this many seconds in the past. Stale events are
    # acknowledged with HTTP 200 (stale=True) so Razorpay stops retrying them.
    # Must be >= 0 (0 rejects any event whose created_at is in the past).
    WEBHOOK_MAX_EVENT_AGE_SECONDS: int = 300

    # Milestone 15B: Operational reliability — stuck-case diagnostics.
    # /api/ops/stuck-cases flags RECEIVED cases older than
    # STUCK_CASE_RECEIVED_SECONDS and REQUIRES_HUMAN cases older than
    # STUCK_CASE_HUMAN_REVIEW_SECONDS (age measured from updated_at), plus
    # PENDING_EXECUTION cases whose next_run_at is in the past. The endpoint
    # is read-only and returns at most STUCK_CASE_MAX_RESULTS rows.
    STUCK_CASE_RECEIVED_SECONDS: int = 3600
    STUCK_CASE_HUMAN_REVIEW_SECONDS: int = 86400
    STUCK_CASE_MAX_RESULTS: int = 100

    # Milestone 16A: AI advisory layer configuration.
    # LLM provider — the application uses a provider-agnostic abstraction.
    LLM_PROVIDER: str = "anthropic"
    LLM_API_KEY: str = ""
    GEMINI_API_KEY: str = ""
    GEMINI_BASE_URL: str = ""
    OPENAI_API_KEY: str = ""
    OPENAI_BASE_URL: str = ""
    ANTHROPIC_API_KEY: str = ""
    # Model for root-cause diagnosis. Defaults:
    # Gemini: "gemini-2.5-flash"
    # Anthropic: "claude-sonnet-5"
    # OpenAI: "gpt-4o-mini" / "gpt-4o"
    LLM_MODEL_DIAGNOSIS: str = "claude-sonnet-5"
    # Model for explanations. Claude Haiku 4.5 — exact Claude API ID
    # "claude-haiku-4-5-20251001" per Anthropic's official model docs.
    LLM_MODEL_EXPLAIN: str = "claude-haiku-4-5-20251001"
    # Timeout in seconds for each LLM API call. Minimum 5 s enforced at call time.
    LLM_TIMEOUT_SECONDS: int = 30
    # When True, the LLM is used for diagnosis/recommendation with deterministic
    # fallback on failure. When False, the system uses the existing deterministic
    # logic exclusively (no LLM calls made). Competition-safe default: True
    # means the system always produces a decision even if the LLM is unavailable.
    LLM_FALLBACK_ENABLED: bool = True
    # When LLM confidence is below this threshold, the downstream integration
    # should consider escalating to REQUIRES_HUMAN. Not enforced in this
    # advisory layer — the Decision Engine integration uses it.
    LLM_CONFIDENCE_THRESHOLD: float = 0.6
    # Opt-in live smoke test flag. When True (and LLM_API_KEY is set), a single
    # test exercises the real provider. Default False — the normal test suite
    # never makes external API calls.
    LLM_LIVE_TEST: bool = False

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

    @field_validator("WEBHOOK_MAX_EVENT_AGE_SECONDS")
    @classmethod
    def _validate_webhook_max_event_age(cls, v: int) -> int:
        if v < 0:
            raise ValueError(
                f"WEBHOOK_MAX_EVENT_AGE_SECONDS must be >= 0, got {v}"
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
        "STUCK_CASE_RECEIVED_SECONDS",
        "STUCK_CASE_HUMAN_REVIEW_SECONDS",
        "STUCK_CASE_MAX_RESULTS",
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
