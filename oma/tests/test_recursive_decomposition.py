"""Phase 31 §1.6: tests for the universal, cost-bounded recursive decomposition mechanism --
_artifact_signal_count(), maybe_decompose_piece_further(), recursively_decompose_constraints(),
and the three real recursion caps.

The Tier-1 recursion-check probe (maybe_decompose_piece_further()'s own call) routes through
`_run_recursion_probe_allowing_reasoning()`, which calls `client.generate()` DIRECTLY (2026-08-08,
fourth and final layer of a real, live-confirmed bug -- see that function's own docstring) rather
than `call_structured()`. Every test below that exercises the probe therefore mocks a fake
client's `.generate()` coroutine, returning raw text (optionally with a `<think>...</think>`
reasoning trace prefix, exactly like the real model) rather than mocking `call_structured`. No
real model gateway calls anywhere in this file.
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
    _artifact_signal_count,
    maybe_decompose_piece_further,
    recursively_decompose_constraints,
)


def _artifacts(label, creates=None, requires=None):
    return _ConstraintArtifacts(label=label, creates=creates or [], requires=requires or [])


class _FakeClient:
    """Stands in for ModelGatewayClient for the probe's direct `client.generate()` call. `handler`
    receives the same kwargs generate() would and returns the raw response text.
    """

    def __init__(self, handler):
        self._handler = handler
        self.calls = []

    async def generate(self, **kwargs):
        self.calls.append(kwargs)
        return self._handler(**kwargs)


def _decomposition_json(items):
    return json.dumps({
        "constraints": [
            {"label": i.label, "creates": i.creates, "requires": i.requires} for i in items
        ]
    })


def test_artifact_signal_count_zero_or_one_is_atomic():
    assert _artifact_signal_count(_artifacts("x")) == 0
    assert _artifact_signal_count(_artifacts("x", creates=["m.f"])) == 1
    assert _artifact_signal_count(_artifacts("x", creates=["m.f"], requires=["m.g"])) == 2
    print("PASS: _artifact_signal_count() sums creates+requires exactly")


def test_tier0_atomic_piece_makes_zero_calls():
    piece = _artifacts("small_piece", creates=["model.field"])
    budget = _RecursionBudget(max_depth=3, max_calls=8, max_nodes=40, starting_node_count=1)
    client = _FakeClient(lambda **kw: (_ for _ in ()).throw(AssertionError("Tier 0 atomic piece must never call the model")))

    result = asyncio.run(
        maybe_decompose_piece_further(piece, "goal", client=client, model="x", budget=budget)
    )
    assert result == [piece]
    assert budget.calls_made == 0
    print("PASS: a Tier-0-atomic piece (<=1 signal) makes zero LLM calls and returns itself unchanged")


def test_genuine_split_recurses_and_stops_when_children_are_atomic():
    parent = _artifacts("big_piece", creates=["model.a", "model.b"], requires=[])
    call_count = {"n": 0}

    def handler(**kwargs):
        call_count["n"] += 1
        assert call_count["n"] == 1, "children (signal count <=1) must be atomic and never probed again"
        return _decomposition_json([
            _artifacts("child_a", creates=["model.a"]),
            _artifacts("child_b", creates=["model.b"]),
        ])

    client = _FakeClient(handler)
    budget = _RecursionBudget(max_depth=3, max_calls=8, max_nodes=40, starting_node_count=1)

    result = asyncio.run(
        maybe_decompose_piece_further(parent, "goal", client=client, model="x", budget=budget)
    )
    labels = sorted(item.label for item in result)
    assert labels == ["child_a", "child_b"], labels
    assert budget.calls_made == 1, "one Tier-1 probe call, children are atomic so no further recursion"
    print("PASS: a genuine split recurses into its children and stops once they're atomic; exactly 1 call made")


def test_single_returned_piece_is_not_treated_as_a_real_split():
    parent = _artifacts("piece", creates=["a"], requires=["b"])
    client = _FakeClient(lambda **kw: _decomposition_json(
        [_artifacts("piece_restated", creates=["a"], requires=["b"])]
    ))
    budget = _RecursionBudget(max_depth=3, max_calls=8, max_nodes=40, starting_node_count=1)

    result = asyncio.run(
        maybe_decompose_piece_further(parent, "goal", client=client, model="x", budget=budget)
    )
    assert result == [parent], "a 1-item probe result is not a genuine split, must keep the original parent"
    print("PASS: a probe call returning exactly 1 piece is treated as atomic, original parent kept")


def test_creates_coverage_gap_falls_back_to_unsplit_parent():
    parent = _artifacts("piece", creates=["a", "b"], requires=[])
    # Drops 'b' entirely -- a real coverage gap.
    client = _FakeClient(lambda **kw: _decomposition_json([
        _artifacts("child1", creates=["a"]), _artifacts("child2", creates=[]),
    ]))
    budget = _RecursionBudget(max_depth=3, max_calls=8, max_nodes=40, starting_node_count=1)

    result = asyncio.run(
        maybe_decompose_piece_further(parent, "goal", client=client, model="x", budget=budget)
    )
    assert result == [parent], "a creates-coverage gap must fall back to the unsplit parent, never silently drop 'b'"
    print("PASS: a split that drops creates coverage falls back to the unsplit parent")


def test_requires_coverage_gap_falls_back_to_unsplit_parent():
    parent = _artifacts("piece", creates=["a"], requires=["dep1", "dep2"])
    client = _FakeClient(lambda **kw: _decomposition_json([
        _artifacts("child1", creates=["a"], requires=["dep1"]), _artifacts("child2"),
    ]))
    budget = _RecursionBudget(max_depth=3, max_calls=8, max_nodes=40, starting_node_count=1)

    result = asyncio.run(
        maybe_decompose_piece_further(parent, "goal", client=client, model="x", budget=budget)
    )
    assert result == [parent], "a requires-coverage gap must fall back to the unsplit parent"
    print("PASS: a split that drops requires coverage falls back to the unsplit parent")


def test_duplicate_creates_across_siblings_is_stripped_not_dropped():
    parent = _artifacts("piece", creates=["shared", "unique_a", "unique_b"], requires=[])
    client = _FakeClient(lambda **kw: _decomposition_json([
        _artifacts("child1", creates=["shared", "unique_a"]),
        _artifacts("child2", creates=["shared", "unique_b"]),
    ]))
    budget = _RecursionBudget(max_depth=3, max_calls=8, max_nodes=40, starting_node_count=1)

    result = asyncio.run(
        maybe_decompose_piece_further(parent, "goal", client=client, model="x", budget=budget)
    )
    by_label = {item.label: item.creates for item in result}
    assert by_label["child1"] == ["shared", "unique_a"], by_label
    assert by_label["child2"] == ["unique_b"], (
        f"the LATER entry's duplicate 'shared' claim must be stripped, not the whole piece dropped: {by_label}"
    )
    print("PASS: a duplicate creates claim across siblings is stripped from the later entry only, never a dropped piece")


def _adversarial_always_splits_mock(call_count):
    """Every call returns 2 children, each carrying forward ALL of the piece it was actually
    called on (parsed back out of the real recursion-probe prompt text --
    _build_recursive_split_probe_prompt()'s own "which so far concretely builds: [...]" /
    ", and requires already existing: [...]" phrasing, 2026-08-08) plus one fresh creates/
    requires pair each, so the artifact-coverage checks always trivially pass (this mock is
    testing the recursion CAPS, not the coverage-gap fallback) while every child's own signal
    count stays above the Tier-0 threshold -- a real, unbounded recursion mechanism would split
    forever without the caps this test exists to prove stop it.
    """
    import ast
    import re

    def handler(**kwargs):
        call_count["n"] += 1
        n = call_count["n"]
        prompt = kwargs["messages"][0]["content"]
        # Unlike the old execution-time narrowing this replaced, base_goal_text is passed down
        # UNCHANGED at every recursion level (never nested/growing) -- exactly one real match per
        # call, for the piece actually being decomposed this call.
        creates_matches = re.findall(r"which so far concretely builds: (\[.*?\])", prompt)
        requires_matches = re.findall(r"and requires already existing: (\[.*?\])", prompt)
        parent_creates = ast.literal_eval(creates_matches[-1]) if creates_matches else []
        parent_requires = ast.literal_eval(requires_matches[-1]) if requires_matches else []
        return _decomposition_json([
            _artifacts(f"piece_{n}_a", creates=[*parent_creates, f"m.f{n}a"], requires=[*parent_requires, f"m.f{n}a_dep"]),
            _artifacts(f"piece_{n}_b", creates=[*parent_creates, f"m.f{n}b"], requires=[*parent_requires, f"m.f{n}b_dep"]),
        ])

    return handler


def test_recursion_depth_cap_stops_runaway_splitting():
    call_count = {"n": 0}
    client = _FakeClient(_adversarial_always_splits_mock(call_count))

    parent = _artifacts("adversarial_root", creates=["m.root_a", "m.root_b"], requires=[])
    budget = _RecursionBudget(max_depth=2, max_calls=100, max_nodes=1000, starting_node_count=1)

    result = asyncio.run(
        maybe_decompose_piece_further(parent, "goal", client=client, model="x", budget=budget)
    )
    # depth cap of 2 means: depth0 (root, splits), depth1 (splits again), depth2 (stops -- must
    # not recurse a 3rd time). 1 call at depth0 + 2 calls at depth1 = 3 calls total, never more.
    assert budget.calls_made == 3, f"depth cap must stop recursion after depth 2, got {budget.calls_made} calls"
    assert len(result) == 4, f"depth-2 leaves (2 depth0 children x 2 depth1 children each) = 4, got {len(result)}"
    print(f"PASS: OMA_DECOMPOSITION_MAX_RECURSION_DEPTH=2 stops an adversarial always-splits goal after exactly {budget.calls_made} calls, never runaway")


def test_call_budget_cap_stops_runaway_splitting():
    call_count = {"n": 0}
    client = _FakeClient(_adversarial_always_splits_mock(call_count))

    parent = _artifacts("root", creates=["root_a", "root_b"], requires=[])
    budget = _RecursionBudget(max_depth=10, max_calls=2, max_nodes=1000, starting_node_count=1)

    asyncio.run(
        maybe_decompose_piece_further(parent, "goal", client=client, model="x", budget=budget)
    )
    assert budget.calls_made <= 2, f"OMA_ARCHITECT_STAGE_MAX_TOTAL_CALLS=2 must hard-cap total calls, got {budget.calls_made}"
    print(f"PASS: OMA_ARCHITECT_STAGE_MAX_TOTAL_CALLS=2 hard-caps total calls at {budget.calls_made}, never exceeded")


def test_node_count_cap_checked_before_each_tier1_call_not_just_between_siblings():
    """Round 11's own fix: a single deeply-recursing piece must not alone consume the node budget
    before the between-siblings check would otherwise have fired -- the cap must be checked
    immediately before EVERY Tier-1 call's own initiation.
    """
    call_count = {"n": 0}
    client = _FakeClient(_adversarial_always_splits_mock(call_count))

    parent = _artifacts("root", creates=["root_a", "root_b"], requires=[])
    # max_nodes=2, starting_node_count=1: the first split (1 node -> 2 nodes) lands exactly on
    # the ceiling, so no further Tier-1 call may ever fire past it.
    budget = _RecursionBudget(max_depth=10, max_calls=100, max_nodes=2, starting_node_count=1)

    result = asyncio.run(
        maybe_decompose_piece_further(parent, "goal", client=client, model="x", budget=budget)
    )
    assert budget.calls_made == 1, f"node cap must stop after the first split hits the ceiling, got {budget.calls_made} calls"
    assert len(result) == 2, result
    print("PASS: OMA_DECOMPOSITION_MAX_TOTAL_NODES stops further Tier-1 calls once the node ceiling is hit, checked before each call")


def test_recursively_decompose_constraints_flattens_and_dedupes_labels():
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
    assert len(labels) == len(set(labels)), f"every label in the flattened output must be globally unique: {labels}"
    assert "atomic_piece" in labels
    print(f"PASS: recursively_decompose_constraints() returns a flat, globally-unique-labeled list: {labels}")


def test_recursion_probe_call_uses_generate_directly_with_no_think_false():
    """Layers two through four of the same real bug (2026-08-08): a fixed, unbiased prompt alone
    was not enough -- confirmed live, directly, that no_think=True, then grammar-constrained
    call_structured(), then even ungrammared call_structured() all made the local model echo an
    obviously-splittable piece (11 creates spanning three distinct workflow stages) back
    completely unchanged, because this is a genuine open judgment call ("does this still hide
    more structure?"), not extraction. The fourth, final root cause was call_structured()'s own
    hardcoded "no prose, no explanation" system prompt -- so the probe now calls
    `client.generate()` directly (see _run_recursion_probe_allowing_reasoning()), with no_think
    explicitly False and no restrictive system message. This test inspects the actual kwargs
    passed to the fake client's generate() for the recursion probe specifically.
    """
    piece = _artifacts("big_piece", creates=["model.a", "model.b"], requires=[])
    client = _FakeClient(lambda **kw: _decomposition_json(
        [_artifacts("big_piece", creates=["model.a", "model.b"])]
    ))
    budget = _RecursionBudget(max_depth=3, max_calls=8, max_nodes=40, starting_node_count=1)

    asyncio.run(
        maybe_decompose_piece_further(piece, "goal", client=client, model="x", budget=budget)
    )
    assert len(client.calls) == 1
    assert client.calls[0]["no_think"] is False, (
        "the recursion-check probe must run with no_think=False -- it's a genuine judgment "
        "call, not extraction; no_think=True was confirmed live to make the model just echo "
        "the input back unchanged regardless of how well-framed the prompt itself is"
    )
    # No system message and no grammar constraint at all -- generate() has no such parameters,
    # confirming the probe bypasses call_structured()'s own hardcoded "no prose, no explanation"
    # system prompt entirely (the fourth, final confirmed suppressor).
    assert "response_format" not in client.calls[0] or client.calls[0].get("response_format") is None
    print("PASS: the recursion probe calls client.generate() directly with no_think=False, bypassing call_structured() entirely")


def test_original_whole_goal_decomposition_still_defaults_to_no_think_true():
    """The recursion-probe fix must be scoped to ONLY the probe call -- the original, pre-
    existing whole-goal decomposition call (and the architect-stage biased drafts, which share
    the same _run_decomposition_with_prompt()/call_structured() helper) must keep their exact
    prior no_think=True behavior, unchanged.
    """
    captured_kwargs = []

    async def fake_call_structured(**kwargs):
        captured_kwargs.append(kwargs)
        return _ConstraintDecomposition(constraints=[_artifacts("x", creates=["m.a"])])

    async def run():
        with patch("manager.replanning.call_structured", new=AsyncMock(side_effect=fake_call_structured)):
            from manager.replanning import decompose_into_constraints_with_artifacts
            return await decompose_into_constraints_with_artifacts("goal", client=None, model="x")

    asyncio.run(run())
    assert len(captured_kwargs) == 1
    assert captured_kwargs[0]["no_think"] is True, (
        "the ORIGINAL whole-goal decomposition call must keep its existing no_think=True "
        "behavior unchanged -- the fix is scoped to the recursion probe only"
    )
    print("PASS: the original whole-goal decomposition call is unaffected, still no_think=True, still via call_structured()")


def test_recursion_probe_prompt_never_contains_execution_time_focus_only_framing():
    """Real bug found and root-caused live (2026-08-08, the project owner's own finding, confirmed against
    8 real, varied live production goals -- including a 10-piece equipment-rental task with
    obviously complex individual pieces -- that never once triggered a genuine recursive split):
    the recursion-check call used to reuse _narrow_goal_text_for_label()'s own execution-time
    "Focus ONLY on this one piece of the goal: X" framing, fed into the SAME decomposition
    prompt whose own explicit rule says "never invent extra items just to pad the list."
    Combined, these structurally guaranteed a "just one piece" answer regardless of real
    complexity -- and every existing test in this file mocked the model's RESPONSE without ever
    inspecting the actual PROMPT TEXT sent, so this real, live-confirmed bug was invisible to
    the whole existing test suite. This test closes exactly that gap: it captures the real
    prompt sent to the model for the recursion probe and asserts, directly, that the
    execution-time framing is gone and the piece is presented as context for a genuine question
    instead.
    """
    piece = _artifacts("big_piece", creates=["model.a", "model.b", "model.c"], requires=["model.dep"])
    captured_prompts = []

    def handler(**kwargs):
        captured_prompts.append(kwargs["messages"][0]["content"])
        return _decomposition_json([_artifacts("big_piece", creates=["model.a", "model.b", "model.c"])])

    client = _FakeClient(handler)
    budget = _RecursionBudget(max_depth=3, max_calls=8, max_nodes=40, starting_node_count=1)
    base_goal = "A rich, multi-part original goal describing several real, independent requirements."

    asyncio.run(
        maybe_decompose_piece_further(piece, base_goal, client=client, model="x", budget=budget)
    )
    assert len(captured_prompts) == 1
    prompt = captured_prompts[0]

    # The real bug's exact phrasing must never appear again.
    assert "Focus ONLY on this one piece" not in prompt, (
        f"the recursion probe must never reuse the execution-time 'Focus ONLY on X' command "
        f"framing -- it structurally biases the model toward always answering 'just one piece' "
        f"-- got:\n{prompt}"
    )
    assert "never invent extra items just to pad the list" not in prompt, (
        "the recursion probe must not reuse the whole-goal decomposition prompt's own anti-"
        "padding rule verbatim either -- that rule is correct for the ORIGINAL decomposition "
        "call, but combined with 'Focus ONLY on X' it doubly discouraged ever finding a real "
        "split in a piece that's already been told it's one, singular thing"
    )
    # The real fix's own, genuinely different framing must be present: the piece as CONTEXT for
    # an open question that explicitly allows either a real single-item answer or a real split.
    assert "big_piece" in prompt
    assert base_goal in prompt, "the full original goal must still be present, as context"
    assert "genuinely still bundles two or more separately-checkable things" in prompt or \
        "genuinely independent" in prompt, (
        f"the probe must explicitly frame this as an open question allowing a real split, not "
        f"just ask the model to restate/confirm the piece -- got:\n{prompt}"
    )
    print("PASS: the recursion-check prompt is genuinely different from the execution-time "
          "narrowing framing -- no 'Focus ONLY' command, no reused anti-padding rule, and the "
          "piece is presented as context for a real, open question")


def test_recursion_probe_extracts_json_from_a_genuine_reasoning_trace():
    """Direct regression test for the fourth, final layer of the bug: a genuine reasoning trace
    (a <think> block, PLUS prose after it mentioning bracket-like fragments like "creates: [...]"
    before the real final answer) must not fool a naive greedy first-brace-to-last-brace parse.
    _extract_last_balanced_json_object() must find the correct, LAST, schema-valid JSON object.
    """
    piece = _artifacts("workflow", creates=["a", "b"], requires=[])
    real_answer = _decomposition_json([
        _artifacts("part_a", creates=["a"]), _artifacts("part_b", creates=["b"]),
    ])
    raw_response = (
        "<think>\n"
        "Let me consider this. The piece currently has creates: [\"a\", \"b\", \"c\"] and "
        "requires: []. One candidate split would be {\"a\": 1} vs {\"b\": 2} but that's not "
        "the real answer, just me thinking out loud with some braces.\n"
        "</think>\n"
        f"Here is my final answer:\n{real_answer}\n"
    )
    client = _FakeClient(lambda **kw: raw_response)
    budget = _RecursionBudget(max_depth=3, max_calls=8, max_nodes=40, starting_node_count=1)

    result = asyncio.run(
        maybe_decompose_piece_further(piece, "goal", client=client, model="x", budget=budget)
    )
    labels = sorted(item.label for item in result)
    assert labels == ["part_a", "part_b"], (
        f"a genuine reasoning trace with inline bracket-like fragments before the real answer "
        f"must not fool the parser into grabbing the wrong span -- got {labels}"
    )
    print("PASS: the reasoning-preserving probe correctly extracts the LAST, real JSON object "
          "even when the reasoning trace itself contains earlier bracket-like fragments")


if __name__ == "__main__":
    test_artifact_signal_count_zero_or_one_is_atomic()
    test_tier0_atomic_piece_makes_zero_calls()
    test_genuine_split_recurses_and_stops_when_children_are_atomic()
    test_single_returned_piece_is_not_treated_as_a_real_split()
    test_creates_coverage_gap_falls_back_to_unsplit_parent()
    test_requires_coverage_gap_falls_back_to_unsplit_parent()
    test_duplicate_creates_across_siblings_is_stripped_not_dropped()
    test_recursion_depth_cap_stops_runaway_splitting()
    test_call_budget_cap_stops_runaway_splitting()
    test_node_count_cap_checked_before_each_tier1_call_not_just_between_siblings()
    test_recursively_decompose_constraints_flattens_and_dedupes_labels()
    test_recursion_probe_call_uses_generate_directly_with_no_think_false()
    test_original_whole_goal_decomposition_still_defaults_to_no_think_true()
    test_recursion_probe_prompt_never_contains_execution_time_focus_only_framing()
    test_recursion_probe_extracts_json_from_a_genuine_reasoning_trace()
    print("\nALL RECURSIVE-DECOMPOSITION TESTS PASSED")
