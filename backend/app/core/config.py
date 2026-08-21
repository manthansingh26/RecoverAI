"""Application configuration using Pydantic Settings."""

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Environment-based application configuration.

    All values are loaded from environment variables (or a .env file).
    """

    APP_ENV: str = "development"
    LOG_LEVEL: str = "INFO"
    DATABASE_URL: str = "postgresql+psycopg://recoverai:recoverai@localhost:5432/recoverai"

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


settings = Settings()
