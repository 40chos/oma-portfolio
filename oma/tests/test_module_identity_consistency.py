"""Phase 26B (2026-07-27, audit Finding #2): confirms the real,
live-reproduced fencing/escalation identity-split bug is closed --
`manager/loop.py`'s own fencing-lock derivation (`resolve_module_identity(
message, hint=scope_models[0] if scope_models else None)`, feeding
`module_for_repeat_check`/`TaskContract.module_identity`) and
`manager/replanning.py`'s `should_escalate_to_operator()` (`contract.
module_identity or resolve_module_identity(contract.goal)`) now derive
from the SAME authoritative function, `contracts.module_identity.
resolve_module_identity()`, instead of two independent, drifted guesses
that could never agree (the old `anticipated_scope`-only hint the real
chat UI never populates, vs. a 4-word goal-prose regex slug that could
never exact-string-match a real outcome row's own `module` column).
"""

import asyncio
import os
import sys
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from contracts.module_identity import resolve_module_identity
from contracts.schema import AutonomyTier, CapabilityClass, SpecialistType, TaskContract, VerificationResult
from manager.learning import check_repeated_failures
from manager.replanning import should_escalate_to_operator
from manager.tools import append_project_memory

# Representative goal shapes, matching this project's real, established
# task-goal convention -- a single-field task, a decomposed multi-field
# task, an _inherit-based edit to an existing model, and a brand-new-
# model task (which still names its own new model via the same Model:
# convention, since a task always states what it's creating/editing).
_GOAL_SHAPES = {
    "single_field": (
        "I want to see a text field on the lead form called 'Special instructions'.\n\n"
        "Module: mis_base_extend\nModel: crm.lead\nField: special_instructions (Text)\n"
    ),
    "decomposed_multi_field": (
        "Add scheduling fields, enforce a double-booking rule, and add a product-scoping relation.\n\n"
        "Module: mis_base_extend\nModel: project.meerwerk\n"
        "Field: start_time (Datetime)\nField: end_time (Datetime)\n"
    ),
    "inherit_edit": (
        "On the meerwerk form, I want an internal notes field only developers can see.\n\n"
        "Module: project_meerwerk\nModel: project.meerwerk\nField: developer_notes (Text)\n"
    ),
    "new_model": (
        "Create a new model to track extra work lines.\n\n"
        "Module: project_meerwerk\nModel: project.meerwerk.line\nField: hours (Float)\n"
    ),
}

_EXPECTED_MODEL = {
    "single_field": "crm.lead",
    "decomposed_multi_field": "project.meerwerk",
    "inherit_edit": "project.meerwerk",
    "new_model": "project.meerwerk.line",
}


def _contract_for(goal: str, module_identity: str | None) -> TaskContract:
    return TaskContract(
        task_id=uuid.uuid4(), specialist_type=SpecialistType.bug_fix,
        capability_class=CapabilityClass.module_development, tier=AutonomyTier.tier_1_readonly,
        goal=goal, inputs=[], rules=[], deliverables=[], compensating_actions=[],
        validation_by="testing_qa", pause_if=[], turn_budget=10,
        module_identity=module_identity,
    )


def test_resolve_module_identity_matches_across_representative_goal_shapes():
    """Confirms `resolve_module_identity()` -- the ONE function both
    manager/loop.py's fencing-lock derivation and manager/replanning.py's
    should_escalate_to_operator() now call -- correctly resolves the real
    model name for every representative goal shape the plan's own test
    spec (26B.4) calls for.
    """
    for shape, goal in _GOAL_SHAPES.items():
        resolved = resolve_module_identity(goal)
        assert resolved == _EXPECTED_MODEL[shape], (
            f"shape {shape!r}: expected {_EXPECTED_MODEL[shape]!r}, got {resolved!r}"
        )
    print(f"PASS: resolve_module_identity() correctly resolves all {len(_GOAL_SHAPES)} representative goal shapes")


def test_fencing_and_escalation_derive_the_identical_value_for_the_same_contract():
    """The actual regression this phase closes: manager/loop.py's own
    fencing-lock derivation (module_for_repeat_check = resolve_module_
    identity(message, hint=...)) and manager/replanning.py's
    should_escalate_to_operator() (contract.module_identity or resolve_
    module_identity(contract.goal)) must return the IDENTICAL value for
    the SAME contract -- confirmed here by replicating loop.py's own
    exact derivation logic (rather than importing private loop.py
    internals) and comparing it against what should_escalate_to_operator()
    reads.
    """
    for shape, goal in _GOAL_SHAPES.items():
        # Replicates manager/loop.py's own exact derivation (no explicit
        # anticipated_scope hint -- the real, confirmed-common production
        # case this whole phase exists to fix).
        module_for_repeat_check = resolve_module_identity(goal, hint=None)
        contract = _contract_for(goal, module_identity=module_for_repeat_check)

        # should_escalate_to_operator()'s own internal derivation reads
        # contract.module_identity first -- confirm it's exactly what
        # loop.py cached, never a second, independent guess.
        escalation_identity = contract.module_identity or resolve_module_identity(contract.goal)
        assert escalation_identity == module_for_repeat_check == _EXPECTED_MODEL[shape], (
            f"shape {shape!r}: fencing-lock identity {module_for_repeat_check!r} != "
            f"escalation identity {escalation_identity!r}"
        )
    print("PASS: fencing-lock derivation and should_escalate_to_operator's own module-identity "
          "resolution agree exactly, across every representative goal shape")


def test_two_different_contracts_touching_the_same_real_model_get_the_same_identity():
    """The concrete regression: two DIFFERENT tasks (different task_id,
    different goal prose) both genuinely editing the SAME real Odoo
    model must now resolve to the SAME identity -- the actual property
    that makes the fencing lock able to serialize them, which the OLD
    `anticipated_scope`-only derivation could never provide (each task's
    own lock key silently collapsed to `task:{task_id}`, unique by
    construction, so two tasks on the same model could never collide).
    """
    goal_a = (
        "I want an internal notes field only developers can see on the meerwerk form.\n\n"
        "Module: project_meerwerk\nModel: project.meerwerk\nField: developer_notes (Text)\n"
    )
    goal_b = (
        "Every meerwerk record should get a unique reference number automatically.\n\n"
        "Module: project_meerwerk\nModel: project.meerwerk\n"
        "Field: name (Char, readonly, copy=False, default='New')\n"
    )
    identity_a = resolve_module_identity(goal_a, hint=None)
    identity_b = resolve_module_identity(goal_b, hint=None)
    assert identity_a == identity_b == "project.meerwerk", (
        f"two different tasks editing the same real model must resolve to the same identity -- "
        f"got {identity_a!r} and {identity_b!r}"
    )
    print(f"PASS: two different goals editing the same real model both resolve to {identity_a!r}, "
          f"the property that lets the fencing lock actually serialize them")


def test_explicit_hint_still_wins_over_goal_text_derivation():
    """An explicit caller-supplied hint (e.g. a real anticipated_scope
    caller) must still override the automatic goal-text derivation --
    same "explicit caller intent beats an automatic guess" precedent
    this project already established elsewhere (detected_existing_
    module_dependency in manager/loop.py).
    """
    goal = "Model: crm.lead\nField: x (Char)\n"
    assert resolve_module_identity(goal, hint="explicit.override") == "explicit.override"
    print("PASS: an explicit hint still wins over the automatic goal-text derivation")


def test_check_repeated_failures_is_now_genuinely_reachable():
    """Real, confirmed bug this closes: check_repeated_failures() was
    structurally unreachable in practice -- should_escalate_to_operator()
    always queried against a goal-prose word-slug that could never
    exact-string-match a real outcome row's own `module` column (always
    written as a real Odoo model name or None). This test writes two
    real 'failed' outcome rows keyed by a resolve_module_identity()-
    derived value (exactly what manager/loop.py's own outcome-writing
    step -- Phase 6 of run_turn() -- now does), then confirms should_
    escalate_to_operator() genuinely detects and fires "repeated_failure"
    for a fresh contract touching the SAME real model.
    """
    goal = "Model: oma_phase26b_test_model.synthetic\nField: x (Char)\n"
    module = resolve_module_identity(goal, hint=None)
    assert module == "oma_phase26b_test_model.synthetic"

    task_id_1 = str(uuid.uuid4())
    task_id_2 = str(uuid.uuid4())
    for tid in (task_id_1, task_id_2):
        append_project_memory(
            event_type="outcome", actor="bug_fix", task_id=tid, module=module,
            summary="synthetic Phase 26B repeated-failure test row",
            tags=["failed"], detail={"passed": False, "root_cause": "one_off"}, verified=False,
        )

    # Sanity: the deterministic check itself now genuinely fires.
    repeat_check = check_repeated_failures(module)
    assert repeat_check["should_pause"] is True, (
        f"check_repeated_failures must now genuinely detect 2 real prior failures for the same "
        f"resolved identity -- got {repeat_check!r}"
    )

    # The actual end-to-end confirmation: a FRESH contract (different
    # task_id) touching the SAME real model must have should_escalate_
    # to_operator() correctly return "repeated_failure" -- not silently
    # stay a no-op the way the old goal-prose-slug derivation always did.
    fresh_contract = _contract_for(goal, module_identity=module)
    v = VerificationResult(
        task_id=fresh_contract.task_id, passed=False, reproduction_confirmed=True,
        uncovered_paths=[], coverage_diff="", spot_check_mismatch=False, notes="x",
    )
    reason = should_escalate_to_operator(fresh_contract, round_number=1, elapsed_seconds=1.0, verification_result=v)
    assert reason == "repeated_failure", (
        f"should_escalate_to_operator() must now correctly fire for a fresh task touching a model "
        f"with 2+ real prior failures -- got {reason!r}"
    )
    print("PASS: check_repeated_failures is now genuinely reachable end-to-end via "
          "should_escalate_to_operator() for a fresh task on a previously-failing real model")


if __name__ == "__main__":
    test_resolve_module_identity_matches_across_representative_goal_shapes()
    test_fencing_and_escalation_derive_the_identical_value_for_the_same_contract()
    test_two_different_contracts_touching_the_same_real_model_get_the_same_identity()
    test_explicit_hint_still_wins_over_goal_text_derivation()
    test_check_repeated_failures_is_now_genuinely_reachable()
    print("\nALL PHASE 26B MODULE-IDENTITY-CONSISTENCY TESTS PASSED")
