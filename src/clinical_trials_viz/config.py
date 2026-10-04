"""Service settings, read from the environment and an optional `.env` file."""

from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Planner models, as pydantic-ai model strings ("provider:model").
    # Changing models is a configuration change, not a code change.
    planner_primary: str = "openai:gpt-5.4-mini"
    planner_fallback: str | None = "anthropic:claude-sonnet-5-5"

    ctgov_base_url: str = "https://clinicaltrials.gov/api/v2"
    ctgov_requests_per_minute: int = 40  # stays under the reported ~50/min limit
    ctgov_timeout_seconds: float = 30.0
    max_pages: int = 20  # 20 pages x 1,000 trials; larger cohorts return scope_required

    runs_dir: Path = Path("data/runs")
    public_base_url: str = "http://localhost:8000"

    otel_exporter: str = "none"  # "none", "console" or "otlp"


@lru_cache
def get_settings() -> Settings:
    # Provider SDKs read API keys from os.environ, so load .env into it too.
    load_dotenv()
    return Settings()
