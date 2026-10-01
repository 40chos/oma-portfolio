"""P13 item 4 / item 10(a) (docs/planning/PHASE30_OMA_SEAM_COVERAGE_AND_COMBINATORIAL_HARDENING_2026-07-29.md
§22.2): the deterministic, zero-LLM half of node-addressable state -- deriving
`ConstraintNode.predecessor_labels` from real `creates`/`requires` artifact-overlap, replacing
the coarse 5-bucket keyword match (`manager/replanning.py`'s `_dependency_tier_for_constraint_label()`)
as the PRIMARY independence signal.

Lives in `contracts/`, not `manager/`, for the same reason contracts/goal_facts.py does: both
`manager/` and `specialists/` already import freely from `contracts/`, and this function's own
caller (manager/replanning.py's decompose_into_constraints(), still the tier-fallback owner) must
be able to import it without contracts/ importing back into manager/ (which would be a cycle).

Deliberately NOT wired into decompose_into_constraints() yet -- the LLM-side schema extension
(emitting real `creates`/`requires` per constraint) is separate, larger-scoped work (item 10a) with
its own unverified-reliability caveat documented in item 4/10(a) of the source doc; this module is
the standalone, independently-testable derivation half, following the same incremental-build
pattern as tools_odoo/committed_symbols.py (P12 item 27): built and unit-tested as a pure function
first, wired into a live call site only once that separate piece exists and is spot-checked.
"""

from __future__ import annotations

from contracts.schema import ConstraintNode


def derive_predecessor_labels_from_overlap(
    nodes: dict[str, ConstraintNode],
) -> dict[str, list[str]]:
    """For every label L in `nodes`, returns the list of other labels M in the same dict whose
    `creates` set intersects L's own `requires` set -- real artifact overlap, not a keyword-
    category match. A label with an empty `requires` list gets an empty predecessor list here
    (never guessed) -- per item 4/10(a), the caller (manager/replanning.py) is responsible for
    falling back to `_dependency_tier_for_constraint_label()`'s bucket order for exactly those
    labels, since that fallback function lives in `manager/` and this module must not import it
    (contracts/ cannot depend on manager/ without creating an import cycle).

    Order within each returned list matches `nodes`' own iteration order -- callers that need a
    stable order should pass an already-ordered dict (e.g. by `constraint_order`).
    """
    result: dict[str, list[str]] = {}
    for label, node in nodes.items():
        requires = set(node.requires)
        predecessors: list[str] = []
        if requires:
            for other_label, other_node in nodes.items():
                if other_label == label:
                    continue
                if requires & set(other_node.creates):
                    predecessors.append(other_label)
        result[label] = predecessors
    return result


def labels_needing_tier_fallback(nodes: dict[str, ConstraintNode]) -> list[str]:
    """Labels whose `creates` AND `requires` both came back empty -- a malformed extraction, or a
    goal that genuinely names no concrete artifact -- for exactly which the caller must fall back
    to the existing tier-bucket order, per item 4/10(a)'s explicit "never worse than today's
    tier-only behavior" requirement.
    """
    return [
        label
        for label, node in nodes.items()
        if not node.creates and not node.requires
    ]


def not_yet_built_artifacts(
    nodes: dict[str, ConstraintNode],
    constraint_status: dict[str, str],
) -> set[str]:
    """P13 item 11, Unit 2 (docs/planning/PHASE30_OMA_SEAM_COVERAGE_AND_COMBINATORIAL_HARDENING_2026-07-29.md
    §22.2 item 11): the set of real Odoo identifiers (model/field/view/etc. names) named by every
    `ConstraintNode` whose own label is NOT YET `"satisfied"` in `constraint_status` -- the real,
    live-updated per-round field (`TaskContract.constraint_status`), never the separate, additive
    `ConstraintNode.state` field, which is populated once at decomposition time and never
    transitioned during real round execution (confirmed by direct search -- no call site anywhere
    in manager/ ever writes `ConstraintNode.state`, only reads `contract.constraint_nodes[label]`
    for `FailureRecord` population).

    Deliberately errs toward inclusion, not exclusion: a label absent from `constraint_status`
    entirely (never yet attempted) is treated the same as `"pending"`/`"failing"` -- its own
    `creates` artifacts are just as legitimately "not yet built" as an in-progress or failing
    sibling's. Only `"satisfied"` labels are excluded.
    """
    unresolved: set[str] = set()
    for label, node in nodes.items():
        if constraint_status.get(label) != "satisfied":
            unresolved.update(node.creates)
    return unresolved


def detect_cycle(nodes: dict[str, ConstraintNode]) -> list[list[str]]:
    """Phase 31 §1.5: returns every strongly-connected component of size > 1 in the
    `predecessor_labels` graph, in one pass (Tarjan's algorithm) -- not just the first cycle found,
    and not a loop-until-acyclic wrapper. A label absent from `nodes` (a stale/malformed
    `predecessor_labels` entry) is silently skipped, never guessed into existence.

    Returns `[]` when the graph is already a DAG. Each inner list is one real cycle (order is
    Tarjan's own discovery order, not meaningful beyond "these labels co-depend").
    """
    index_counter = [0]
    stack: list[str] = []
    on_stack: set[str] = set()
    indices: dict[str, int] = {}
    lowlink: dict[str, int] = {}
    sccs: list[list[str]] = []

    def strongconnect(label: str) -> None:
        indices[label] = index_counter[0]
        lowlink[label] = index_counter[0]
        index_counter[0] += 1
        stack.append(label)
        on_stack.add(label)

        for predecessor in nodes[label].predecessor_labels:
            if predecessor not in nodes:
                continue
            if predecessor not in indices:
                strongconnect(predecessor)
                lowlink[label] = min(lowlink[label], lowlink[predecessor])
            elif predecessor in on_stack:
                lowlink[label] = min(lowlink[label], indices[predecessor])

        if lowlink[label] == indices[label]:
            component: list[str] = []
            while True:
                member = stack.pop()
                on_stack.discard(member)
                component.append(member)
                if member == label:
                    break
            if len(component) > 1:
                sccs.append(component)

    for label in nodes:
        if label not in indices:
            strongconnect(label)

    return sccs


def collapse_cycle_to_tier_chain(
    nodes: dict[str, ConstraintNode],
    cyclic_labels: list[str],
) -> None:
    """Phase 31 §1.5: mutates `nodes` in place to break one real cycle -- strips every
    `predecessor_labels` edge that points from one member of `cyclic_labels` to another (intra-SCC
    edges only; edges to/from labels outside the cycle are untouched), then re-inserts a synthetic
    linear chain ordered by `sort_constraint_labels_by_dependency_tier(cyclic_labels)` (called
    directly on just the cyclic subset -- no precomputed `tier_order` parameter, since the tier
    sort is cheap and deterministic and a stale precomputed order would be a real correctness risk
    across repeated collapse calls).

    Import is local (not module-level) to avoid a contracts/ -> manager/ import cycle at module
    load time -- `manager/replanning.py` already imports from `contracts/`, so a top-level import
    the other direction would be circular; deferring it to call time breaks that.
    """
    from manager.replanning import sort_constraint_labels_by_dependency_tier

    cyclic_set = set(cyclic_labels)
    for label in cyclic_labels:
        node = nodes[label]
        node.predecessor_labels = [
            predecessor for predecessor in node.predecessor_labels if predecessor not in cyclic_set
        ]

    ordered = sort_constraint_labels_by_dependency_tier(cyclic_labels)
    for position in range(1, len(ordered)):
        current = nodes[ordered[position]]
        predecessor_label = ordered[position - 1]
        if predecessor_label not in current.predecessor_labels:
            current.predecessor_labels.append(predecessor_label)


def is_expected_incomplete_finding(
    finding_artifact_names: list[str],
    not_yet_built: set[str],
) -> bool:
    """P13 item 11, Unit 2: True only when a finding names at least one real artifact AND every
    one of its named artifacts is claimed by some not-yet-`"satisfied"` sibling constraint --
    i.e. this finding is fully, positively explained by "a later constraint hasn't run yet," never
    partially. A finding naming an artifact no unresolved node claims (or naming nothing at all)
    is a genuine, blocking failure exactly as today -- this function never widens suppression
    beyond what the real, persisted `creates` data actually supports.
    """
    return bool(finding_artifact_names) and all(
        name in not_yet_built for name in finding_artifact_names
    )
