"""Counting rules from docs/harness-design.md section 3, checked on real records."""

from hypothesis import given, settings
from hypothesis import strategies as st

from clinical_trials_viz.analyze import breakdown, comparison_groups
from clinical_trials_viz.catalog import NOT_REPORTED, OTHER_BUCKET, Dimension
from clinical_trials_viz.cohort import lists_drug, resolve_drug_identity
from clinical_trials_viz.ctgov.trial import clean_drug_name, dimension_values


def test_multi_phase_trial_counts_under_each_phase(trials):
    multi = next(t for t in trials if len(t.phases) > 1)
    result = breakdown(trials, Dimension.PHASE, None)
    for label in dimension_values(multi, Dimension.PHASE):
        bucket = next(b for b in result.buckets if b.label == label)
        assert multi.nct_id in bucket.trial_ids


def test_phase_buckets_follow_phase_order(trials):
    labels = [b.label for b in breakdown(trials, Dimension.PHASE, None).buckets]
    order = ["Early Phase 1", "Phase 1", "Phase 2", "Phase 3", "Phase 4", "Not applicable", NOT_REPORTED]
    assert labels == [label for label in order if label in labels]


def test_start_years_have_no_gaps(trials):
    years = [int(b.label) for b in breakdown(trials, Dimension.START_YEAR, None).buckets if b.label != NOT_REPORTED]
    assert years == list(range(min(years), max(years) + 1))


def test_country_counts_trials_not_sites(trials):
    us = next(b for b in breakdown(trials, Dimension.COUNTRY, None).buckets if b.label == "United States")
    assert len(us.trial_ids) == len(set(us.trial_ids))
    assert len(us.trial_ids) == sum(1 for t in trials if "United States" in t.countries)


def test_top_n_folds_the_rest_into_other(trials):
    result = breakdown(trials, Dimension.COUNTRY, 3)
    assert [b.label for b in result.buckets][-1] == OTHER_BUCKET
    assert len(result.buckets) == 4
    assert result.folded > 0


def test_missing_values_are_their_own_state(trials):
    no_phase = [t for t in trials if not t.phases]
    if no_phase:
        bucket = next(b for b in breakdown(trials, Dimension.PHASE, None).buckets if b.label == NOT_REPORTED)
        assert bucket.count == len(no_phase)


@settings(max_examples=25, deadline=None)
@given(st.randoms())
def test_order_and_duplicates_do_not_change_counts(trials, rnd):
    shuffled = trials + trials[:10]
    rnd.shuffle(shuffled)
    a = {b.label: b.count for b in breakdown(trials, Dimension.PHASE, None).buckets}
    b = {b.label: b.count for b in breakdown(shuffled, Dimension.PHASE, None).buckets}
    assert a == b


def test_drug_identity_resolves_to_mesh_term(trials):
    assert resolve_drug_identity(trials) == "pembrolizumab"


def test_match_check_excludes_trials_that_only_mention_the_drug(trials):
    identity = resolve_drug_identity(trials)
    kept = {t.nct_id for t in trials if lists_drug(t, "Keytruda", identity)}
    # 45 trials carry the pembrolizumab MeSH term. Of the 5 without it, NCT02704156 names it in an
    # intervention ("Cyberknife plus Pembrolizumab…") and stays; the other 4 only mention it elsewhere.
    assert len(kept) == 46
    assert "NCT02704156" in kept
    assert "NCT05553782" not in kept  # implantable microdevice study


def test_clean_drug_name():
    assert clean_drug_name("Pembrolizumab (KEYTRUDA®) 200 mg") == "pembrolizumab (keytruda)"
    assert clean_drug_name("Cisplatin 75 mg/m2 IV") == "cisplatin"


def test_comparison_groups_put_shared_trials_in_overlap(trials):
    a, b = trials[:30], trials[20:50]
    groups = comparison_groups({"A": a, "B": b})
    assert groups.labels == ["A only", "B only", "Both"]
    assert len(groups.trials["Both"]) == 10
    assert len(groups.trials["A only"]) == 20
    assert len(groups.trials["B only"]) == 20
