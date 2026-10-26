"""The Run's model-call ledger: every attempt recorded once, caps enforced in one place."""

import pytest

from clinical_trials_viz.ledger import CallBudgetExhausted, Step, current_ledger, run_ledger


def test_attempts_are_recorded_per_step_and_totalled():
    with run_ledger() as ledger:
        with ledger.step(Step.SPLIT):
            ledger.record()
        with ledger.step(Step.PLANNING):
            ledger.record()
            ledger.record()
    assert (ledger.used(Step.SPLIT), ledger.used(Step.PLANNING), ledger.total) == (1, 2, 3)


def test_each_step_has_its_own_cap():
    with run_ledger() as ledger:
        with ledger.step(Step.SPLIT):
            ledger.record()
            ledger.record()
            with pytest.raises(CallBudgetExhausted):
                ledger.record()  # split: one call, two if the primary fails
        with ledger.step(Step.PLANNING):
            for _ in range(3):
                ledger.record()
            with pytest.raises(CallBudgetExhausted):
                ledger.record()
    assert ledger.total == 5


def test_a_part_can_be_held_to_fewer_calls_so_later_parts_keep_one():
    with run_ledger() as ledger, ledger.step(Step.PLANNING):
        with ledger.cap(1):
            ledger.record()
            with pytest.raises(CallBudgetExhausted):
                ledger.record()
        assert ledger.remaining(Step.PLANNING) == 2  # the narrower cap ended with the part


def test_a_failed_model_is_remembered_for_the_rest_of_the_run():
    with run_ledger() as ledger:
        ledger.model_failed("primary-model", "HTTP 503")
        assert ledger.failure_of("primary-model") == "HTTP 503"
        assert ledger.failure_of("fallback-model") is None
    with run_ledger() as fresh:
        assert fresh.failure_of("primary-model") is None  # a new Run starts clean


def test_the_ledger_is_found_through_the_run_context():
    assert current_ledger() is None
    with run_ledger() as ledger:
        assert current_ledger() is ledger
    assert current_ledger() is None
