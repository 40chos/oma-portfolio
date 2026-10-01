"""Real, confirmed bug found live (2026-08-08/09, task 07141af5-9a4e-41b6-93ea-8b7af04fea9c's
flagship run, service_ticket_model node): recurred identically across 4 consecutive rounds/
resumes, even after frozen_old_files_by_relpath's own SEPARATE staleness bug (resume not
re-reading it fresh -- see tests/test_resume_orphaned_task_continuation.py's own regression
test) was already fixed -- proving this was a second, distinct, deterministic bug.

`contract.frozen_old_files_by_relpath` is stored WITH the module-name prefix (raw
vcs.read_last_validated_commit() output, e.g. 'oma_x_12345678/models/models.py'). The
generation-side branch in specialists/build/specialist.py's own _run_module_dev() correctly
strips this prefix before use (confirmed by tests/test_apply_node_result_to_module.py's own
existing coverage of the sibling logic, and by direct source inspection). But the call site
that hands this SAME contract field to apply_node_result_to_module() -- which looks up
`frozen_old_files_by_relpath.get(relpath, "")` using BARE relpaths, matching
`new_files_by_relpath`'s own keying -- used to pass the raw, prefixed dict straight through
unchanged. Every lookup then silently returned "" instead of the real prior content, forcing
_apply_line_hunks() to diff from nothing against a NON-empty current state, which can never
find a genuine shared anchor -- reporting a hard, deterministic conflict for every file any
earlier constraint had already written to (manifest, security CSV, etc.), every single time.

Source-level check (same discipline as tests/test_manager_loop_verification_notes_fold_choice.py)
rather than a full round drive -- proves the ACTUAL call site strips the prefix, not just that
apply_node_result_to_module() behaves correctly in isolation (already covered separately).
"""

import inspect
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import specialists.build.specialist as build_specialist


def _find_apply_call_block(source: str) -> str:
    idx = source.index("apply_outcome = apply_node_result_to_module(")
    # Walk back to the start of the enclosing statement/comment block so the prefix-stripping
    # code immediately preceding the call is included.
    start = source.rfind("\n\n", 0, idx)
    end = source.index(")\n", idx) + 2
    return source[start:end]


def test_apply_call_site_strips_module_prefix_before_passing_frozen_snapshot():
    source = inspect.getsource(build_specialist)
    block = _find_apply_call_block(source)
    assert "frozen_old_files_by_relpath=frozen_old_files_bare" in block, (
        f"expected the apply_node_result_to_module() call site to pass a module-prefix-stripped "
        f"dict (not contract.frozen_old_files_by_relpath directly), got:\n{block}"
    )
    assert 'contract.frozen_old_files_by_relpath=' not in block.replace(" ", ""), (
        "must never pass the raw, prefixed contract.frozen_old_files_by_relpath directly into "
        "apply_node_result_to_module() -- its own lookups use bare relpaths"
    )
    print("PASS: the apply_node_result_to_module() call site strips the module-name prefix "
          "before passing the frozen snapshot, closing the real live gap found on task "
          "07141af5's service_ticket_model node")


def test_prefix_strip_logic_matches_the_generation_sides_own_proven_pattern():
    """The fix must reuse the exact same `path.removeprefix(module_prefix)` shape the
    generation-side branch already uses just above it in the same function -- not a
    reimplementation that could itself diverge in behavior.
    """
    source = inspect.getsource(build_specialist)
    block = _find_apply_call_block(source)
    assert re.search(r'path\.removeprefix\(module_prefix\)', block), (
        f"expected the same removeprefix(module_prefix) pattern the generation-side branch "
        f"already uses -- got:\n{block}"
    )
    assert 'if path.startswith(module_prefix)' in block, (
        f"expected the same startswith(module_prefix) filter the generation-side branch "
        f"already uses -- got:\n{block}"
    )
    print("PASS: the prefix-stripping logic matches the generation side's own already-proven "
          "pattern exactly, not a divergent reimplementation")


if __name__ == "__main__":
    test_apply_call_site_strips_module_prefix_before_passing_frozen_snapshot()
    test_prefix_strip_logic_matches_the_generation_sides_own_proven_pattern()
    print("\nALL APPLY-NODE-RESULT CALL-SITE PREFIX-STRIP TESTS PASSED")
