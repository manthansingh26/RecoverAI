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

    @property
    def cors_origins_list(self) -> list[str]:
        """Parse CORS_ORIGINS into a clean list of origins.

        Splits on commas, strips whitespace, and drops empty entries so the
        parser is safe for empty, whitespace-padded, and trailing-comma input.
        """
        return [o.strip() for o in self.CORS_ORIGINS.split(",") if o.strip()]

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


settings = Settings()
