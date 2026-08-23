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

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


settings = Settings()
