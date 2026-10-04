"""Networks: link entities that share trials. All counting happens here, like `analyze`."""

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
from clinical_trials_viz.ctgov.trial import Trial, arm_drugs, drug_identities


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


def same_arm_pairs(trial: Trial) -> dict[tuple[str, str], list[str]]:
    """Drug pairs given together in at least one arm, with the arms that give both."""
    pairs: dict[tuple[str, str], list[str]] = defaultdict(list)
    for label, drugs in arm_drugs(trial).items():
        for pair in combinations(sorted(drugs), 2):
            pairs[pair].append(label)
    return dict(pairs)


def drug_drug_network(trials: list[Trial]) -> Network:
    """Drugs linked when one arm of a trial gives both (Same-Arm Combination, not Co-Listing)."""
    drug_ids: dict[str, set[str]] = defaultdict(set)
    pair_ids: dict[tuple[str, str], set[str]] = defaultdict(set)
    no_arm_links = 0
    for trial in trials:
        pairs = same_arm_pairs(trial)
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
        "An arm can list alternatives (e.g. carboplatin or cisplatin at the investigator's choice); "
        "the registry does not say which, so such pairs also appear as links.",
        "Supplements, placebo, saline and 'standard of care' are not counted as drugs.",
        *capped,
    ]
    if no_arm_links:
        assumptions.append(
            f"{no_arm_links} trials list two or more drugs but do not record which arm receives them, "
            "so they cannot show a combination and are left out."
        )
    return Network(list(nodes.values()), edges, contributing, assumptions)
