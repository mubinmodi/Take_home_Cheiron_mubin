"""Hosted mode (docs/hosted-deployment.md): settings, shared state, access control, circuit breakers
and the run deadline. The local mode must keep working with none of it configured."""

import pytest
from pydantic import ValidationError

from clinical_trials_viz.catalog import Dimension
from clinical_trials_viz.config import HOSTED_QUERIES_PER_HOUR, HOSTED_RUN_DEADLINE_SECONDS, Settings
from clinical_trials_viz.models.plan import AnswerPlan, Filters, Operation
from tests.conftest import ScriptedPlanner, ask

TREND = AnswerPlan(operation=Operation.AGGREGATE, filters=Filters(drugs=["Keytruda"]), group_by=Dimension.START_YEAR)
KEY = "k" * 24


def local(**values) -> Settings:
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


def hosted(**values) -> Settings:
    return local(deployment="hosted", **values)


# --- Settings ---------------------------------------------------------------------------------------


def test_the_local_mode_needs_no_infrastructure():
    settings = local()
    assert settings.deployment == "local"
    assert settings.redis_url is None and settings.database_url is None and settings.api_users() == {}
    assert settings.user_queries_per_hour is None and settings.run_deadline_seconds is None


def test_hosted_mode_refuses_to_start_and_names_every_missing_setting():
    with pytest.raises(ValidationError) as info:
        hosted()
    assert "DEPLOYMENT=hosted needs REDIS_URL, DATABASE_URL, API_KEYS" in str(info.value)


def test_hosted_mode_defaults_the_per_user_limit_and_the_deadline():
    settings = hosted(redis_url="redis://cache", database_url="postgresql+asyncpg://db", api_keys=f"alice:{KEY}")
    assert settings.user_queries_per_hour == HOSTED_QUERIES_PER_HOUR
    assert settings.run_deadline_seconds == HOSTED_RUN_DEADLINE_SECONDS
    assert settings.api_users() == {KEY: "alice"}


def test_a_malformed_api_key_entry_is_rejected_without_quoting_any_key():
    with pytest.raises(ValidationError) as info:
        local(api_keys=f"alice:{KEY}, bob:too-short")
    assert "API_KEYS entry 2 must be name:key" in str(info.value)
    assert "too-short" not in str(info.value) and KEY not in str(info.value)


def test_connection_strings_and_keys_stay_out_of_reprs():
    settings = local(redis_url="redis://:hunter2@cache", api_keys=f"alice:{KEY}")
    assert "hunter2" not in repr(settings) and KEY not in repr(settings)


async def test_chart_links_use_the_public_address_when_one_is_set(make_client, settings):
    settings.public_base_url = "https://trials.example.org"
    body = await ask(make_client, ScriptedPlanner(TREND), query="Keytruda trials per year")
    assert body["chart_url"] == f"https://trials.example.org/v1/runs/{body['run_id']}/chart.png"
