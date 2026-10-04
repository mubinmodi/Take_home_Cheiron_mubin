"""Service settings, read from the environment and an optional `.env` file."""

import logging
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv
from pydantic_settings import BaseSettings, SettingsConfigDict

log = logging.getLogger(__name__)

# OpenAI models this project's key may use.
ALLOWED_OPENAI_MODELS = frozenset({
    "gpt-4o-mini", "gpt-4o-2024-08-06", "gpt-4.1-nano", "gpt-4.1-mini", "gpt-4.1",
    "gpt-5-mini", "gpt-5", "gpt-5.1", "gpt-5.2", "gpt-5.4-nano", "gpt-5.4-mini", "gpt-5.4",
})  # fmt: skip

# Roughly equivalent models per provider, by size tier. A starting point for picking a
# fallback in the same class as the primary; the eval set (evals/run.py) is the real test.
MODEL_TIERS: dict[str, dict[str, str]] = {
    "nano": {
        "openai": "openai:gpt-5.4-nano",  # also gpt-4.1-nano, gpt-4o-mini
        "anthropic": "anthropic:claude-haiku-4-5",
        "google": "google:gemini-3.5-flash-lite",
    },
    "mini": {
        "openai": "openai:gpt-5.4-mini",  # also gpt-5-mini, gpt-4.1-mini
        "anthropic": "anthropic:claude-haiku-4-5",
        "google": "google:gemini-3.5-flash",
    },
    "full": {
        "openai": "openai:gpt-5.4",  # also gpt-5.2, gpt-5.1, gpt-5, gpt-4.1, gpt-4o-2024-08-06
        "anthropic": "anthropic:claude-sonnet-5-5",
        "google": "google:gemini-3.1-pro-preview",
    },
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Planner models, as pydantic-ai model strings ("provider:model").
    # Changing models is a configuration change, not a code change.
    planner_primary: str = MODEL_TIERS["mini"]["openai"]
    planner_fallback: str | None = MODEL_TIERS["mini"]["anthropic"]
    # Each model attempt may take this long (the SDK defaults are 600 s with 2 retries, which would
    # stall a request for minutes before the fallback runs); the whole planning step has a deadline.
    planner_timeout_seconds: float = 20.0
    planner_deadline_seconds: float = 60.0
    render_timeout_seconds: float = 30.0  # one chart image
    log_level: str = "WARNING"

    ctgov_base_url: str = "https://clinicaltrials.gov/api/v2"
    ctgov_requests_per_minute: int = 40  # stays under the reported ~50/min limit
    ctgov_timeout_seconds: float = 30.0
    max_pages: int = 20  # 20 pages x 1,000 trials; larger cohorts return scope_required

    runs_dir: Path = Path("data/runs")
    public_base_url: str = "http://localhost:8000"

    otel_exporter: str = "none"  # "none", "console" or "otlp"


def disallowed_openai_models(*names: str | None) -> list[str]:
    """OpenAI planner models outside the allowed list; requests to them would fail."""
    return [n for n in names if n and n.startswith("openai:") and n.split(":", 1)[1] not in ALLOWED_OPENAI_MODELS]


@lru_cache
def get_settings() -> Settings:
    # Provider SDKs read API keys from os.environ, so load .env into it too.
    load_dotenv()
    settings = Settings()
    for name in disallowed_openai_models(settings.planner_primary, settings.planner_fallback):
        log.warning("%s is not in the allowed OpenAI models: %s", name, ", ".join(sorted(ALLOWED_OPENAI_MODELS)))
    return settings
