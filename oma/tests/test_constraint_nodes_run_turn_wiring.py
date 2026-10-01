"""P13 item 4/10(a): regression guard confirming manager/loop.py's run_turn() actually populates
contract.constraint_nodes (via build_constraint_nodes()) before routing a 3+-constraint task into
_run_decomposed_task(). This call site sits inside run_turn()'s own large, heavily-dependent body
(governance gates, memory reads, contract construction) -- a full live reproduction is impractical
here, same reasoning as items 16/22/29's own source-level regression guards. Zero LLM/GPU calls.
"""

import inspect
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import manager.loop as loop_module


def test_build_constraint_nodes_is_called_before_run_decomposed_task():
    source = inspect.getsource(loop_module)
    decompose_index = source.index("constraint_artifacts = await decompose_into_constraints_with_artifacts(")
    run_decomposed_index = source.index("return await _run_decomposed_task(", decompose_index)
    window = source[decompose_index:run_decomposed_index]
    assert "build_constraint_nodes(constraint_artifacts)" in window
    assert '"constraint_nodes":' in window
    print("PASS: build_constraint_nodes() output is assigned to contract.constraint_nodes before _run_decomposed_task()")


def test_constraint_labels_list_itself_is_unaffected_by_the_artifacts_call():
    source = inspect.getsource(loop_module)
    decompose_index = source.index("constraint_artifacts = await decompose_into_constraints_with_artifacts(")
    window = source[decompose_index:decompose_index + 1200]
    assert "constraint_labels = [item.label for item in constraint_artifacts]" in window
    assert "sort_constraint_labels_by_dependency_tier(constraint_labels)" in window
    print("PASS: constraint_labels is still derived and sorted exactly as before this change")


if __name__ == "__main__":
    test_build_constraint_nodes_is_called_before_run_decomposed_task()
    test_constraint_labels_list_itself_is_unaffected_by_the_artifacts_call()
    print("\nALL CONSTRAINT-NODES RUN_TURN WIRING TESTS PASSED")
