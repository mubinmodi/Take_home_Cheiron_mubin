"""Networks: link entities that share trials. All counting happens here, like `analyze`."""

import re
from collections import defaultdict
from dataclasses import dataclass, field
from itertools import combinations

from clinical_trials_viz.catalog import (
    DRUG_INTERVENTION_TYPES,
    NETWORK_MAX_EDGES,
    NETWORK_MIN_EDGE_TRIALS,
    NETWORK_TOP_DRUGS,
    NETWORK_TOP_SPONSORS,
)
from clinical_trials_viz.ctgov.trial import Trial, arm_drugs, drug_identities, drug_name_variants


@dataclass
class Node:
    kind: str  # "sponsor" or "drug"
    label: str
    trial_ids: list[str]

    @property
    def id(self) -> str:
        return f"{self.kind}:{self.label}"


@dataclass
class Edge:
    source: Node
    target: Node
    kind: str
    trial_ids: list[str]


@dataclass
class Network:
    nodes: list[Node]  # display order: by kind, then by trial count
    edges: list[Edge]  # heaviest first
    contributing_trials: int  # trials that had at least one entity of each kind
    assumptions: list[str] = field(default_factory=list)


def trial_drugs(trial: Trial) -> tuple[str, ...]:
    """Drugs a trial gives; empty when it has no drug-type intervention (e.g. device-only trials)."""
    if not any(i.type in DRUG_INTERVENTION_TYPES for i in trial.interventions):
        return ()
    return drug_identities(trial)


def _cap_edges(pairs: dict[tuple[str, str], set[str]], assumptions: list[str]) -> dict[tuple[str, str], set[str]]:
    """Keep the heaviest links so the image stays readable; report the cut."""
    if len(pairs) <= NETWORK_MAX_EDGES:
        return pairs
    ranked = sorted(pairs.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    assumptions.append(f"Showing the {NETWORK_MAX_EDGES} strongest of {len(pairs)} links.")
    return dict(ranked[:NETWORK_MAX_EDGES])


def _top(ids: dict[str, set[str]], n: int) -> list[str]:
    return [label for label, _ in sorted(ids.items(), key=lambda kv: (-len(kv[1]), kv[0]))[:n]]


def sponsor_drug_network(trials: list[Trial]) -> Network:
    """Lead sponsors linked to the drugs their trials give; edge weight = trials with both."""
    sponsor_ids: dict[str, set[str]] = defaultdict(set)
    drug_ids: dict[str, set[str]] = defaultdict(set)
    pair_ids: dict[tuple[str, str], set[str]] = defaultdict(set)
    contributing = 0
    for trial in trials:
        drugs = trial_drugs(trial)
        if not trial.lead_sponsor or not drugs:
            continue
        contributing += 1
        sponsor_ids[trial.lead_sponsor].add(trial.nct_id)
        for drug in drugs:
            drug_ids[drug].add(trial.nct_id)
            pair_ids[(trial.lead_sponsor, drug)].add(trial.nct_id)

    sponsors = set(_top(sponsor_ids, NETWORK_TOP_SPONSORS))
    drugs = set(_top(drug_ids, NETWORK_TOP_DRUGS))
    kept_pairs = {
        pair: ids
        for pair, ids in pair_ids.items()
        if pair[0] in sponsors and pair[1] in drugs and len(ids) >= NETWORK_MIN_EDGE_TRIALS
    }
    capped: list[str] = []
    kept_pairs = _cap_edges(kept_pairs, capped)
    linked_sponsors = {s for s, _ in kept_pairs}
    linked_drugs = {d for _, d in kept_pairs}
    nodes = {
        ("sponsor", s): Node("sponsor", s, sorted(sponsor_ids[s])) for s in _top(sponsor_ids, len(sponsor_ids))
        if s in linked_sponsors
    } | {("drug", d): Node("drug", d, sorted(drug_ids[d])) for d in _top(drug_ids, len(drug_ids)) if d in linked_drugs}  # fmt: skip
    edges = [
        Edge(nodes[("sponsor", s)], nodes[("drug", d)], "sponsors_trials_of", sorted(ids))
        for (s, d), ids in sorted(kept_pairs.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    ]

    unlinked = (len(sponsors) - len(linked_sponsors)) + (len(drugs) - len(linked_drugs))
    assumptions = [
        f"Network shows the top {NETWORK_TOP_SPONSORS} lead sponsors and top {NETWORK_TOP_DRUGS} drugs by trial "
        f"count, linked when at least {NETWORK_MIN_EDGE_TRIALS} trials share them; "
        f"{len(sponsor_ids)} sponsors and {len(drug_ids)} drugs were found in total.",
        "A link means the sponsor leads trials that give the drug (lead sponsors only; collaborators not included).",
        "Supplements, placebo, saline and 'standard of care' are not counted as drugs.",
    ]
    assumptions.extend(capped)
    if unlinked:
        assumptions.append(f"{unlinked} top sponsors or drugs had no link above the threshold and are not shown.")
    if skipped := len(trials) - contributing:
        assumptions.append(
            f"{skipped} trials have no drug intervention (or no lead sponsor) and are not in the network."
        )
    return Network(list(nodes.values()), edges, contributing, assumptions)


_ALTERNATIVE_WINDOW = 150  # characters between the two drug names
_CUE_WINDOW = 120  # characters before the first name to look for "either" / "choice"
_OR = re.compile(r"\bor\b|^\s*/\s*$")  # "or", or a slash directly between the names (not "mg/m2")
_JOINED = re.compile(r"\bplus\b|\band\b|\bwith\b|\+|followed by|;")
_CHOICE = re.compile(r"\beither\b|\bchoice\b")


def _positions(text: str, names: set[str]) -> list[tuple[int, int]]:
    spans = []
    for name in names:
        if len(name) < 3:
            continue
        spans += [(m.start(), m.end()) for m in re.finditer(r"\b" + re.escape(name) + r"\b", text)]
    return spans


def are_alternatives(description: str, a_names: set[str], b_names: set[str]) -> bool:
    """True when the arm description offers the two drugs as alternatives. Between a mention of each
    (within a window) there is "or" or "/", and either
    - nothing joins them ("plus", "and", "with", "+", "followed by", ";"): "cisplatin 75 mg/m2 OR carboplatin", or
    - "either" / "choice" comes just before the first: whole regimens offered as options, as in
      "EITHER cisplatin + gemcitabine OR carboplatin + ..." or "investigator's choice of (A PLUS B OR A PLUS C)".

    A direct join anywhere ("paclitaxel PLUS cisplatin", no "or" between) means they are given together,
    even if another mention of one of them sits in a different option.
    """
    text = description.lower()
    alternative = False
    for a_start, a_end in _positions(text, a_names):
        for b_start, b_end in _positions(text, b_names):
            first_start, gap = (a_start, text[a_end:b_start]) if a_end <= b_start else (b_start, text[b_end:a_start])
            if len(gap) > _ALTERNATIVE_WINDOW:
                continue
            if not _OR.search(gap):
                if _JOINED.search(gap):
                    return False  # joined directly: a combination
                continue
            cue = _CHOICE.search(text[max(0, first_start - _CUE_WINDOW) : first_start])
            alternative = alternative or not _JOINED.search(gap) or bool(cue)
    return alternative


def _arm_pairs(trial: Trial) -> tuple[dict[tuple[str, str], list[str]], set[tuple[str, str]]]:
    combined: dict[tuple[str, str], list[str]] = defaultdict(list)
    alternatives: set[tuple[str, str]] = set()
    descriptions = dict(trial.arm_descriptions)
    variants = drug_name_variants(trial)
    for label, drugs in arm_drugs(trial).items():
        for a, b in combinations(sorted(drugs), 2):
            text = descriptions.get(label)
            if text and are_alternatives(text, variants.get(a, {a}), variants.get(b, {b})):
                alternatives.add((a, b))
            else:
                combined[(a, b)].append(label)
    return dict(combined), alternatives - set(combined)


def same_arm_pairs(trial: Trial) -> dict[tuple[str, str], list[str]]:
    """Drug pairs given together in at least one arm, with the arms that give both. A pair the arm
    description offers as alternatives ("A or B") is not a combination in that arm."""
    return _arm_pairs(trial)[0]


def alternative_pairs(trial: Trial) -> set[tuple[str, str]]:
    """Drug pairs that share an arm only as alternatives, never as a combination."""
    return _arm_pairs(trial)[1]


def drug_drug_network(trials: list[Trial]) -> Network:
    """Drugs linked when one arm of a trial gives both (Same-Arm Combination, not Co-Listing)."""
    drug_ids: dict[str, set[str]] = defaultdict(set)
    pair_ids: dict[tuple[str, str], set[str]] = defaultdict(set)
    no_arm_links = 0
    alternatives = 0
    for trial in trials:
        pairs = same_arm_pairs(trial)
        alternatives += len(alternative_pairs(trial))
        if not pairs:
            if len(trial_drugs(trial)) >= 2 and not arm_drugs(trial):
                no_arm_links += 1
            continue
        for a, b in pairs:
            pair_ids[(a, b)].add(trial.nct_id)
            drug_ids[a].add(trial.nct_id)
            drug_ids[b].add(trial.nct_id)

    top = set(_top(drug_ids, NETWORK_TOP_DRUGS))
    kept = {p: ids for p, ids in pair_ids.items() if set(p) <= top and len(ids) >= NETWORK_MIN_EDGE_TRIALS}
    capped: list[str] = []
    kept = _cap_edges(kept, capped)
    linked = {d for p in kept for d in p}
    nodes = {d: Node("drug", d, sorted(drug_ids[d])) for d in _top(drug_ids, len(drug_ids)) if d in linked}
    edges = [
        Edge(nodes[a], nodes[b], "same_arm", sorted(ids))
        for (a, b), ids in sorted(kept.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    ]
    contributing = len({i for ids in pair_ids.values() for i in ids})
    assumptions = [
        f"A link means both drugs are given in the same arm of at least {NETWORK_MIN_EDGE_TRIALS} trials; drugs "
        "only listed in different arms of a trial (e.g. drug vs comparator) are not linked.",
        f"Network shows the top {NETWORK_TOP_DRUGS} drugs by number of trials with a same-arm combination; "
        f"{len(drug_ids)} such drugs were found.",
        "Drugs an arm offers as alternatives (its description reads e.g. 'cisplatin or carboplatin') are not "
        "a combination; arms without a description are taken as given together.",
        "Supplements, placebo, saline and 'standard of care' are not counted as drugs.",
        *capped,
    ]
    if alternatives:
        assumptions.append(
            f"{alternatives} drug pairs appear in an arm only as alternatives and are not counted as combinations."
        )
    if no_arm_links:
        assumptions.append(
            f"{no_arm_links} trials list two or more drugs but do not record which arm receives them, "
            "so they cannot show a combination and are left out."
        )
    return Network(list(nodes.values()), edges, contributing, assumptions)
