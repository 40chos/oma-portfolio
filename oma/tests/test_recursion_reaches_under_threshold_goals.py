"""Real, live-confirmed bug (2026-08-08, the project owner's own database-verified finding): manager/loop.py
used to call recursively_decompose_constraints() only INSIDE the
`len(constraint_labels) >= 3 or unsupported_touches` branch -- so a goal the top-level call itself
bundled into fewer than 3 giant constraints (confirmed live: a whole three-stage approval workflow
returned as ONE constraint with 9 creates, task d5edeef6-ba7e-4e6c-9cbf-53a8f672ef34) never once
reached Tier-1 recursion, because under-splitting at the top level is exactly what produces fewer
than 3 labels in the first place -- the one case Tier-1 recursion exists to catch was structurally
excluded from ever calling it.

manager/loop.py's own body is heavily coupled to governance gates, memory reads, and contract
construction -- a full live run_turn() reproduction is impractical in a unit test (same reasoning
as test_constraint_nodes_run_turn_wiring.py's own docstring). These are source-inspection
regression guards, zero LLM/GPU calls, confirming the real fix's structure directly in the code.
"""

import inspect
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import manager.loop as loop_module


def test_recursively_decompose_constraints_call_is_outside_the_3plus_gate():
    source = inspect.getsource(loop_module)
    gate_index = source.index("if len(constraint_labels) >= 3 or unsupported_touches:\n")
    recursion_index = source.index("constraint_artifacts = await recursively_decompose_constraints(")
    architect_index = source.index("constraint_artifacts = await maybe_upgrade_decomposition_with_architect_stage(")

    # The architect-stage upgrade call must still be INSIDE the original gate (unchanged scope,
    # it's an expensive quality/parallelism pass for decompositions that already show real
    # multi-piece structure, not the general-purpose "did we miss internal structure" catch-all).
    assert gate_index < architect_index < recursion_index, (
        "the architect-stage upgrade call must stay inside the >=3-or-unsupported gate, and the "
        "recursion call must come after it"
    )

    # The recursion call itself must NOT be inside that same gated block -- it must be reachable
    # regardless of how many top-level labels the baseline decomposition produced. We confirm this
    # by checking there is no unindented (i.e. dedented back to the `if`'s own level) code between
    # the gate and the recursion call other than the architect-stage call's own indented body --
    # concretely: the recursion call's own line must be indented one level LESS than the architect
    # call's line (back out of the `if` block), not still nested inside it.
    architect_line = source[source.rindex("\n", 0, architect_index) + 1:architect_index]
    recursion_line = source[source.rindex("\n", 0, recursion_index) + 1:recursion_index]
    architect_indent = len(architect_line) - len(architect_line.lstrip())
    recursion_indent = len(recursion_line) - len(recursion_line.lstrip())
    assert recursion_indent < architect_indent, (
        f"recursively_decompose_constraints() must be called OUTSIDE (less indented than) the "
        f"architect-stage call's own if-block, so it runs unconditionally -- got recursion "
        f"indent={recursion_indent}, architect indent={architect_indent}"
    )
    print("PASS: recursively_decompose_constraints() runs unconditionally, outside the "
          "3+-label-or-unsupported-touch gate that used to structurally exclude under-split goals")


def test_a_genuine_split_found_below_the_original_threshold_still_routes_to_the_decomposed_path():
    source = inspect.getsource(loop_module)
    recursion_index = source.index("constraint_artifacts = await recursively_decompose_constraints(")
    routing_index = source.index(
        "if len(constraint_labels) >= 3 or unsupported_touches or genuine_split_found_below_threshold:",
        recursion_index,
    )
    window = source[recursion_index:routing_index]
    assert "pre_recursion_label_count = len(constraint_labels)" in source[:recursion_index + 200] or \
        "pre_recursion_label_count" in window or \
        "pre_recursion_label_count" in source[max(0, recursion_index - 400):recursion_index], (
        "the pre-recursion label count must be captured before the recursion call so a genuine "
        "post-recursion increase can be detected"
    )
    assert "genuine_split_found_below_threshold = len(constraint_labels) > pre_recursion_label_count" in source, (
        "a goal that started under the original 3-label/unsupported-touch threshold but that "
        "Tier-1 recursion found genuine internal structure in must be detected via a real count "
        "comparison, not silently dropped"
    )
    run_decomposed_index = source.index("return await _run_decomposed_task(", routing_index)
    build_nodes_index = source.index("build_constraint_nodes(constraint_artifacts)", routing_index)
    assert routing_index < build_nodes_index < run_decomposed_index, (
        "the widened routing condition (including genuine_split_found_below_threshold) must be "
        "what gates building the real constraint-node graph and taking the decomposed, "
        "graph-scheduled _run_decomposed_task() path -- otherwise a genuine split found below the "
        "original threshold would be found by recursion and then silently discarded by falling "
        "through to the plain, single-constraint _execute_contract() path"
    )
    print("PASS: a genuine split found by Tier-1 recursion below the original 3-label threshold "
          "now correctly routes to the decomposed, graph-scheduled execution path instead of "
          "being silently discarded")


if __name__ == "__main__":
    test_recursively_decompose_constraints_call_is_outside_the_3plus_gate()
    test_a_genuine_split_found_below_the_original_threshold_still_routes_to_the_decomposed_path()
    print("\nALL UNDER-THRESHOLD RECURSION ROUTING TESTS PASSED")
