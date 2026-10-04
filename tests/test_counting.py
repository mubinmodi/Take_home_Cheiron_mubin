"""Counting rules from docs/harness-design.md section 3, checked on real records."""

from hypothesis import given, settings
from hypothesis import strategies as st

from clinical_trials_viz.analyze import breakdown, comparison_groups
from clinical_trials_viz.catalog import DRUG_INTERVENTION_TYPES, NOT_REPORTED, OTHER_BUCKET, Dimension
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
    assert "NCT05553782" not in kept  # implantable microdevice study


def test_match_check_requires_a_drug_type_intervention(trials):
    """A Drug is a DRUG, BIOLOGICAL or COMBINATION_PRODUCT intervention (harness-design §3)."""
    identity = resolve_drug_identity(trials)
    kept = {t.nct_id for t in trials if lists_drug(t, "Keytruda", identity)}
    assert "NCT04408898" not in kept  # pembrolizumab only inside a GENETIC intervention
    assert "NCT02704156" not in kept  # "Cyberknife plus Pembrolizumab…", a DEVICE intervention
    assert "NCT02178722" in kept  # DRUG "MK-3475", matched through MeSH
    assert "NCT02600169" in kept  # DRUG "Pemprolizumab" (misspelt), matched through MeSH
    by_id = {t.nct_id: t for t in trials}
    for nct_id in kept:
        assert any(i.type in DRUG_INTERVENTION_TYPES for i in by_id[nct_id].interventions)


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


def test_arm_alternatives_are_not_combinations():
    """KEYNOTE-189 (real record): pembrolizumab PLUS pemetrexed PLUS cisplatin OR carboplatin."""
    import json
    from pathlib import Path

    from clinical_trials_viz.ctgov.trial import parse_trial
    from clinical_trials_viz.network import alternative_pairs, are_alternatives, same_arm_pairs

    study = json.loads((Path(__file__).parent / "fixtures" / "NCT02578680_keynote189.json").read_text())
    trial = parse_trial(study)
    combined = same_arm_pairs(trial)
    assert ("carboplatin", "pembrolizumab") in combined
    assert ("cisplatin", "pemetrexed") in combined
    assert ("carboplatin", "cisplatin") not in combined
    assert ("carboplatin", "cisplatin") in alternative_pairs(trial)

    assert are_alternatives("cisplatin 75 mg/m2 IV OR carboplatin AUC 5", {"cisplatin"}, {"carboplatin"})
    assert are_alternatives("investigator's choice of docetaxel/paclitaxel", {"docetaxel"}, {"paclitaxel"})
    assert not are_alternatives("pembrolizumab 200 mg IV PLUS pemetrexed", {"pembrolizumab"}, {"pemetrexed"})
    assert not are_alternatives("carboplatin and paclitaxel, or observation", {"carboplatin"}, {"paclitaxel"})
    # Real arm texts that offer whole regimens or use long dosing phrases (NCT03066778, NCT02853305, NCT03635567).
    long_gap = (
        "investigator's choice of platinum therapy (carboplatin titrated to an area under the plasma drug "
        "concentration-time curve [AUC] 5 IV on Day 1 OR cisplatin 75 mg/m^2 IV on Day 1)"
    )
    either = (
        "standard therapy chemotherapy with EITHER cisplatin 70 mg/m^2 IV on Day 1 (or Day 2 if required) "
        "+ gemcitabine IV infusion 1,000 mg/m^2 OR carboplatin AUC 5"
    )
    regimens = (
        "Investigator choice of chemotherapy for up to 6 cycles (paclitaxel 175 mg/m^2 PLUS cisplatin 50 mg/m^2 "
        "WITH or WITHOUT bevacizumab 15 mg/kg per local label OR paclitaxel 175 mg/m^2 PLUS carboplatin AUC 5"
    )
    for text in (long_gap, either, regimens):
        assert are_alternatives(text, {"cisplatin"}, {"carboplatin"})
    assert not are_alternatives(regimens, {"paclitaxel"}, {"cisplatin"})  # given together within a regimen
