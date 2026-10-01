"""P11 fifth pass item 176 (docs/planning/PHASE30_SECOND_PASS_FINAL_CONSOLIDATED_2026-07-30.md
§1.1): source-level regression guard confirming _autofix_ensure_compute_method_has_api_depends()
is genuinely called AFTER both _autofix_goal_named_computed_field_missing_declaration() and
_autofix_goal_named_field_declaration_missing() in _validate_generated_module()'s own call chain,
not before. Real bug this reorder closes: at the old position, this function snapshotted
real_fields/required a compute= field to already exist -- both conditions only became true once
the two field-declaring autofixes (which run later) had a chance to run, so the inferred
@api.depends(...) either silently omitted the dependency field or the whole call was skipped
entirely, depending on which case fired.

This ordering lives inside _validate_generated_module()'s own long, sequential call chain with
heavy real dependencies (DB, async live-registry calls interleaved) -- a full live reproduction is
impractical here, same reasoning already established for this project's other call-order
regression guards. Zero LLM/GPU calls -- pure source inspection.
"""

import inspect
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import specialists.build.specialist as build_specialist


def test_compute_depends_autofix_runs_after_both_field_declaring_autofixes():
    source = inspect.getsource(build_specialist)
    depends_call_index = source.index("_autofix_ensure_compute_method_has_api_depends(generated)")
    computed_field_call_index = source.index(
        "_autofix_goal_named_computed_field_missing_declaration(generated, goal, constraint_status or {}, goal_facts)"
    )
    field_decl_call_index = source.index(
        "_autofix_goal_named_field_declaration_missing(generated, goal, constraint_status or {}, goal_facts)"
    )
    assert depends_call_index > computed_field_call_index, (
        "_autofix_ensure_compute_method_has_api_depends() must run AFTER "
        "_autofix_goal_named_computed_field_missing_declaration()"
    )
    assert depends_call_index > field_decl_call_index, (
        "_autofix_ensure_compute_method_has_api_depends() must run AFTER "
        "_autofix_goal_named_field_declaration_missing()"
    )
    print("PASS item176: the compute-depends autofix runs after both field-declaring autofixes")


def test_only_one_real_call_site_exists():
    """Guards against a future edit accidentally leaving a stray second call site behind (e.g. if
    someone re-adds the old call at its original position without removing this one).
    """
    source = inspect.getsource(build_specialist)
    count = source.count("_autofix_ensure_compute_method_has_api_depends(generated)")
    assert count == 1, f"expected exactly one real call site, found {count}"
    print("PASS item176: exactly one real call site exists, no stray duplicate")


if __name__ == "__main__":
    test_compute_depends_autofix_runs_after_both_field_declaring_autofixes()
    test_only_one_real_call_site_exists()
    print("\nALL P11 FIFTH-PASS ITEM 176 TESTS PASSED")
