"""Service settings, read from the environment and an optional `.env` file."""

import logging
from functools import lru_cache
from pathlib import Path
from typing import Literal, Self

from dotenv import load_dotenv
from pydantic import SecretStr, model_validator
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


# Hosted defaults from docs/hosted-deployment.md: every question costs model calls, and a request
# must finish within the platform's deadline.
HOSTED_QUERIES_PER_HOUR = 30
HOSTED_RUN_DEADLINE_SECONDS = 30.0
MIN_API_KEY_LENGTH = 16


class Settings(BaseSettings):
    # Errors never echo input values: they include connection strings and API keys.
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", hide_input_in_errors=True)

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
    # Base of chart_url links. Unset, it is the address each request arrived on (behind a proxy, from
    # its X-Forwarded headers); set it when clients reach the service by another address.
    public_base_url: str | None = None

    otel_exporter: str = "none"  # "none", "console" or "otlp"

    # Hosted mode (docs/hosted-deployment.md). Locally all of these stay unset and nothing changes.
    deployment: Literal["local", "hosted"] = "local"
    redis_url: SecretStr | None = None  # page cache, one shared ClinicalTrials.gov budget, Idempotency-Keys
    database_url: SecretStr | None = None  # run history, e.g. postgresql+asyncpg://user:password@host/db
    api_keys: SecretStr | None = None  # "name:key,name:key"; POST /v1/query then needs an X-API-Key header
    user_queries_per_hour: int | None = None  # per API-key user
    run_deadline_seconds: float | None = None  # a whole Run: planning, retrieval and every part
    breaker_failures: int = 5  # outage-type failures in a row that open a circuit breaker
    breaker_cooldown_seconds: float = 30.0  # how long an open breaker fails fast before one trial call

    @model_validator(mode="after")
    def _check_hosted(self) -> Self:
        """Hosted mode refuses to start without its dependencies, naming each missing setting."""
        if self.deployment == "hosted":
            required = {"REDIS_URL": self.redis_url, "DATABASE_URL": self.database_url, "API_KEYS": self.api_keys}
            missing = [name for name, value in required.items() if value is None or not value.get_secret_value()]
            if missing:
                raise ValueError(f"DEPLOYMENT=hosted needs {', '.join(missing)} (see docs/hosted-deployment.md)")
            self.user_queries_per_hour = self.user_queries_per_hour or HOSTED_QUERIES_PER_HOUR
            self.run_deadline_seconds = self.run_deadline_seconds or HOSTED_RUN_DEADLINE_SECONDS
        self.api_users()  # reject a malformed API_KEYS at startup
        return self

    def api_users(self) -> dict[str, str]:
        """API key -> user name, from API_KEYS. Errors never quote a key."""
        users: dict[str, str] = {}
        entries = self.api_keys.get_secret_value().split(",") if self.api_keys else []
        for number, entry in enumerate((e.strip() for e in entries if e.strip()), start=1):
            name, _, key = (part.strip() for part in entry.partition(":"))
            if not name or len(key) < MIN_API_KEY_LENGTH:
                raise ValueError(
                    f"API_KEYS entry {number} must be name:key, with a key of at least {MIN_API_KEY_LENGTH} characters"
                )
            users[key] = name
        return users


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
