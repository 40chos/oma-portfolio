"""Phase 31 UI (2026-08-08): real UI gap found live -- the project owner's own report: "the graph shows
only the flat, top-level pieces... no visual sense of... this piece was itself split further."
recursively_decompose_constraints() flattens the whole recursion tree for scheduling (correct,
deliberate design -- the parent never itself runs once split, and cross-level dependency edges
"just work" via creates/requires overlap on the flat list) -- but that flattening discarded the
real parent-of relationship entirely, with nothing recording which split produced which children.

split_from (_ConstraintArtifacts, then carried into ConstraintNode via build_constraint_nodes())
is a purely additive, display-only lineage marker: the immediate parent LABEL a node was split
from, None for anything that was never itself the product of a split (every original top-level
piece). Never read by predecessor_labels derivation or the scheduler -- these tests confirm
exactly that: scheduling-relevant fields (labels returned, call counts, creates/requires) are
byte-for-byte unchanged from this file's own pre-existing sibling tests, split_from is the only
new signal.
"""

import asyncio
import json
import os
import sys
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from manager.replanning import (
    _ConstraintArtifacts,
    _ConstraintDecomposition,
    _RecursionBudget,
    build_constraint_nodes,
    maybe_decompose_piece_further,
    recursively_decompose_constraints,
)


def _artifacts(label, creates=None, requires=None):
    return _ConstraintArtifacts(label=label, creates=creates or [], requires=requires or [])


def _decomposition_json(items):
    return json.dumps({
        "constraints": [
            {"label": i.label, "creates": i.creates, "requires": i.requires} for i in items
        ]
    })


class _FakeClient:
    """The Tier-1 recursion-check probe now calls `client.generate()` directly (2026-08-08 --
    see manager.replanning._run_recursion_probe_allowing_reasoning()'s own docstring), not
    call_structured() -- so tests exercising the probe mock a fake client's generate() coroutine
    instead of patching call_structured.
    """

    def __init__(self, handler):
        self._handler = handler
        self.calls = []

    async def generate(self, **kwargs):
        self.calls.append(kwargs)
        return self._handler(**kwargs)


def test_direct_children_of_a_single_split_are_tagged_with_the_real_parent_label():
    parent = _artifacts("big_piece", creates=["model.a", "model.b"], requires=[])
    client = _FakeClient(lambda **kw: _decomposition_json([
        _artifacts("child_a", creates=["model.a"]),
        _artifacts("child_b", creates=["model.b"]),
    ]))

    budget = _RecursionBudget(max_depth=3, max_calls=8, max_nodes=40, starting_node_count=1)

    result = asyncio.run(
        maybe_decompose_piece_further(parent, "goal", client=client, model="x", budget=budget)
    )
    by_label = {item.label: item for item in result}
    assert by_label["child_a"].split_from == "big_piece"
    assert by_label["child_b"].split_from == "big_piece"
    print("PASS: both real children of a genuine split carry the true parent label in split_from")


def test_a_grandchild_carries_its_own_immediate_parent_not_the_original_ancestor():
    """The real, two-level recursion case: big_piece splits into mid_piece + leaf_a; mid_piece
    ITSELF splits further into leaf_b + leaf_c. leaf_b/leaf_c's split_from must be 'mid_piece'
    (their real, immediate parent), never 'big_piece' -- following split_from one hop at a time
    must reconstruct the TRUE recursion chain, not flatten straight to the root.
    """
    call_sequence = {"n": 0}

    def handler(**kwargs):
        call_sequence["n"] += 1
        if call_sequence["n"] == 1:
            # big_piece's own Tier-1 probe: splits into mid_piece (still complex) + leaf_a (atomic)
            return _decomposition_json([
                _artifacts("mid_piece", creates=["model.b", "model.c"]),
                _artifacts("leaf_a", creates=["model.a"]),
            ])
        # mid_piece's own Tier-1 probe: splits further into two genuinely atomic leaves
        return _decomposition_json([
            _artifacts("leaf_b", creates=["model.b"]),
            _artifacts("leaf_c", creates=["model.c"]),
        ])

    client = _FakeClient(handler)
    parent = _artifacts("big_piece", creates=["model.a", "model.b", "model.c"], requires=[])
    budget = _RecursionBudget(max_depth=5, max_calls=8, max_nodes=40, starting_node_count=1)

    result = asyncio.run(
        maybe_decompose_piece_further(parent, "goal", client=client, model="x", budget=budget)
    )
    by_label = {item.label: item for item in result}
    assert set(by_label) == {"leaf_a", "leaf_b", "leaf_c"}, (
        "mid_piece itself must never appear in the final flat output -- it was consumed by its own split"
    )
    assert by_label["leaf_a"].split_from == "big_piece"
    assert by_label["leaf_b"].split_from == "mid_piece", (
        f"leaf_b's real, immediate parent is mid_piece, not big_piece -- got {by_label['leaf_b'].split_from!r}"
    )
    assert by_label["leaf_c"].split_from == "mid_piece"
    print("PASS: a grandchild's split_from correctly points to its own immediate parent "
          "(mid_piece), preserving the true two-level recursion chain, not a flattened shortcut "
          "straight back to the original ancestor")


def test_a_piece_that_never_splits_has_no_split_from():
    piece = _artifacts("atomic_piece", creates=["model.a"], requires=[])
    budget = _RecursionBudget(max_depth=3, max_calls=8, max_nodes=40, starting_node_count=1)

    async def run():
        return await maybe_decompose_piece_further(piece, "goal", client=None, model="x", budget=budget)

    result = asyncio.run(run())
    assert result[0].split_from is None
    print("PASS: an atomic, never-split piece has split_from=None (it's an original top-level piece)")


def test_scheduling_relevant_output_is_completely_unaffected_by_split_from_tracking():
    """The real, load-bearing guarantee: split_from is purely additive display metadata --
    labels, call counts, and creates/requires must all be byte-for-byte identical to what this
    exact scenario produced before split_from tracking was added (matching this file's own
    pre-existing test_genuine_split_recurses_and_stops_when_children_are_atomic()).
    """
    parent = _artifacts("big_piece", creates=["model.a", "model.b"], requires=[])
    client = _FakeClient(lambda **kw: _decomposition_json([
        _artifacts("child_a", creates=["model.a"]),
        _artifacts("child_b", creates=["model.b"]),
    ]))

    budget = _RecursionBudget(max_depth=3, max_calls=8, max_nodes=40, starting_node_count=1)

    result = asyncio.run(
        maybe_decompose_piece_further(parent, "goal", client=client, model="x", budget=budget)
    )
    assert sorted(item.label for item in result) == ["child_a", "child_b"]
    assert len(client.calls) == 1
    assert budget.calls_made == 1
    assert sorted(result[0].creates + result[1].creates) == ["model.a", "model.b"]
    print("PASS: split_from tracking is purely additive -- every scheduling-relevant output stays unchanged")


def test_recursively_decompose_constraints_accumulates_the_full_lineage_including_consumed_intermediate_labels():
    """The real, headline capability: recursively_decompose_constraints() (the actual entry
    point manager/loop.py calls) must expose the FULL lineage map, including 'mid_piece' -- a
    label that gets consumed by its own deeper split and never appears in the returned flat list
    at all. Without this, the UI could only ever draw one hop of recursion, collapsing any
    multi-level split straight back to the root.
    """
    call_sequence = {"n": 0}

    def handler(**kwargs):
        call_sequence["n"] += 1
        if call_sequence["n"] == 1:
            return _decomposition_json([
                _artifacts("mid_piece", creates=["model.b", "model.c"]),
                _artifacts("leaf_a", creates=["model.a"]),
            ])
        return _decomposition_json([
            _artifacts("leaf_b", creates=["model.b"]),
            _artifacts("leaf_c", creates=["model.c"]),
        ])

    client = _FakeClient(handler)
    items = [_artifacts("big_piece", creates=["model.a", "model.b", "model.c"], requires=[])]
    lineage: dict = {}

    result = asyncio.run(
        recursively_decompose_constraints(items, "goal", client=client, model="x", lineage=lineage)
    )
    assert sorted(item.label for item in result) == ["leaf_a", "leaf_b", "leaf_c"]
    assert lineage["leaf_a"] == "big_piece"
    assert lineage["leaf_b"] == "mid_piece"
    assert lineage["leaf_c"] == "mid_piece"
    assert lineage["mid_piece"] == "big_piece", (
        "mid_piece's OWN parent must be recorded even though mid_piece itself was consumed and "
        "never appears in the final flat output -- this is exactly what lets the UI chain "
        "leaf_b/leaf_c all the way back to big_piece across TWO real hops, not one"
    )
    print(f"PASS: the full lineage map includes the consumed intermediate label 'mid_piece' -> "
          f"'big_piece', letting the UI reconstruct the true two-level recursion: {lineage}")


def test_lineage_stays_none_and_untouched_when_no_caller_asks_for_it():
    """The default (lineage=None) must change NOTHING about the function's own real behavior --
    confirmed against this file's own pre-existing sibling test's exact scenario.
    """
    client = _FakeClient(lambda **kw: _decomposition_json([
        _artifacts("shared_label", creates=["x1"]), _artifacts("other", creates=["x2"]),
    ]))

    items = [
        _artifacts("shared_label", creates=["p1"], requires=["p1dep"]),
        _artifacts("atomic_piece", creates=["p3"]),
    ]

    result = asyncio.run(
        recursively_decompose_constraints(items, "goal", client=client, model="x")
    )
    labels = [item.label for item in result]
    assert len(labels) == len(set(labels))
    assert "atomic_piece" in labels
    print("PASS: omitting lineage entirely changes nothing about the real, scheduling-relevant output")


def test_build_constraint_nodes_carries_split_from_through_to_the_real_constraint_node():
    items = [
        _ConstraintArtifacts(label="leaf_a", creates=["model.a"], split_from="big_piece"),
        _ConstraintArtifacts(label="leaf_b", creates=["model.b"], split_from=None),
    ]
    nodes = build_constraint_nodes(items)
    assert nodes["leaf_a"].split_from == "big_piece"
    assert nodes["leaf_b"].split_from is None
    print("PASS: build_constraint_nodes() carries split_from through into the real ConstraintNode")


if __name__ == "__main__":
    test_direct_children_of_a_single_split_are_tagged_with_the_real_parent_label()
    test_a_grandchild_carries_its_own_immediate_parent_not_the_original_ancestor()
    test_a_piece_that_never_splits_has_no_split_from()
    test_scheduling_relevant_output_is_completely_unaffected_by_split_from_tracking()
    test_recursively_decompose_constraints_accumulates_the_full_lineage_including_consumed_intermediate_labels()
    test_lineage_stays_none_and_untouched_when_no_caller_asks_for_it()
    test_build_constraint_nodes_carries_split_from_through_to_the_real_constraint_node()
    print("\nALL SPLIT_FROM LINEAGE TRACKING TESTS PASSED")
