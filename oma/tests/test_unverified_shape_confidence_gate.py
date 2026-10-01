"""P12 Tier A item 11 (docs/planning/PHASE30_ARCH_SYNTHESIS_CRITIQUE_FINAL_2026-07-30.md §4):
tests for the unverified_shapes_targeted() confidence gate -- previously a purely informational
flag (Phase 25E), now a real, interim downgrade when a goal targets a shape with no registered
ground-truth verification AND no Code-Review pass corroborated the round.

Real, confirmed bug found live (2026-08-03, task019): this gate used to live inside
await_verification() itself, checking `code_review_output is None`. That's correct for a caller
that already has Code-Review's real result in hand, but Phase 22's own concurrent-execution
optimization (manager/loop.py) deliberately calls await_verification() with
code_review_output=None as a placeholder, folding the real result in SEPARATELY afterward once
both specialists return concurrently. The gate, living inside await_verification() itself, was
structurally blind to that later fold -- it always saw None for every concurrently-run round,
even when Code-Review genuinely ran and returned real findings. Fixed by extracting the gate into
its own function, `apply_unverified_shape_confidence_gate()`, called explicitly by each real
caller in manager/loop.py AFTER its own true code_review_output is known -- matching the real
production call pattern this test file now exercises directly, not through await_verification()
alone.
"""

import asyncio
import os
import sys
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from contracts.schema import AutonomyTier, CapabilityClass, SpecialistOutput, SpecialistType, TaskContract
from manager.tools import apply_unverified_shape_confidence_gate, await_verification
from specialists import registry


class _FakeValidator:
    def __init__(self, detail: dict, claims_complete: bool = True):
        self._detail = detail
        self._claims_complete = claims_complete

    async def run(self, contract: TaskContract) -> SpecialistOutput:
        return SpecialistOutput(
            task_id=contract.task_id, specialist_type=SpecialistType.testing_qa,
            summary="fake validator output", detail=self._detail, claims_complete=self._claims_complete,
        )


def _contract(goal: str = "Add a computed field for the total.") -> TaskContract:
    # `goal` must genuinely match one of REPRODUCTION_SHAPE_REGISTRY's own signal regexes
    # (contracts/verifier_registry.py) -- apply_unverified_shape_confidence_gate() now computes
    # unverified_shapes_targeted(contract) fresh from the real goal text, exactly like
    # testing_qa/specialist.py's own real call, rather than trusting a stuffed detail key.
    return TaskContract(
        task_id=uuid.uuid4(), specialist_type=SpecialistType.bug_fix,
        capability_class=CapabilityClass.module_development, tier=AutonomyTier.tier_1_readonly,
        goal=goal, inputs=[], rules=[], deliverables=[], compensating_actions=[],
        validation_by="testing_qa", pause_if=[], turn_budget=10,
    )


def _build_output() -> SpecialistOutput:
    return SpecialistOutput(
        task_id=uuid.uuid4(), specialist_type=SpecialistType.bug_fix, summary="fake build",
        detail={}, claims_complete=True,
    )


def _run_with_fake_validator(
    detail: dict, code_review_output=None, claims_complete: bool = True,
    goal: str = "Add a computed field for the total.",
):
    """Matches manager/loop.py's own real sequential call pattern: await_verification() (which no
    longer applies the gate itself, only folds code_review_output when given), then the gate
    applied explicitly right after, using the same real code_review_output value.
    """
    registry.clear()
    try:
        registry.register(SpecialistType.testing_qa, _FakeValidator(detail, claims_complete=claims_complete))
        contract = _contract(goal)
        result = asyncio.run(
            await_verification(contract, _build_output(), client=None, model="x", code_review_output=code_review_output)
        )
        return apply_unverified_shape_confidence_gate(result, contract, code_review_output)
    finally:
        registry.clear()


def test_unverified_shape_with_no_code_review_downgrades_an_otherwise_passing_result():
    result = _run_with_fake_validator({
        "reproduction_confirmed": True, "spot_check_mismatch": False,
        "unverified_shapes_targeted": ["computed_field"],
    })
    assert result.passed is False
    assert result.spot_check_mismatch is True
    assert "Confidence gate" in result.notes
    print("PASS: an otherwise-passing result targeting an unverified shape, with no Code-Review corroboration, is downgraded")


def test_unverified_shape_with_a_code_review_pass_is_left_untouched():
    code_review_output = SpecialistOutput(
        task_id=uuid.uuid4(), specialist_type=SpecialistType.code_review, summary="clean review",
        detail={"findings": []}, claims_complete=True,
    )
    result = _run_with_fake_validator(
        {"reproduction_confirmed": True, "spot_check_mismatch": False, "unverified_shapes_targeted": ["computed_field"]},
        code_review_output=code_review_output,
    )
    assert result.passed is True
    assert result.spot_check_mismatch is False
    print("PASS: a round with a real Code-Review pass this round is never downgraded by this gate, even targeting an unverified shape")


def test_no_unverified_shapes_is_a_pure_no_op():
    result = _run_with_fake_validator(
        {"reproduction_confirmed": True, "spot_check_mismatch": False, "unverified_shapes_targeted": []},
        goal="Change the description text shown on the form.",
    )
    assert result.passed is True
    assert result.spot_check_mismatch is False
    print("PASS: no targeted unverified shapes at all is a pure no-op")


def test_already_failing_result_is_never_touched_by_this_gate():
    result = _run_with_fake_validator({
        "reproduction_confirmed": False, "spot_check_mismatch": True,
        "unverified_shapes_targeted": ["ir_cron_schedule"],
    }, code_review_output=None, claims_complete=False, goal="Set up a scheduled cron job every night.")
    assert result.passed is False
    assert "Confidence gate" not in result.notes
    print("PASS: an already-failing result is never touched -- nothing to gate on a result that already fails")


def test_deferred_concurrent_code_review_result_is_correctly_seen_by_the_gate():
    """Real, direct regression test for the actual bug: reproduces manager/loop.py's own real
    concurrent-execution call pattern -- await_verification() called with code_review_output=None
    (the deliberate "folded in below" placeholder), THEN fold_code_review_findings() applied
    separately with the REAL, concurrently-obtained result, THEN the gate applied with that SAME
    real result -- exactly the sequence the concurrent path in manager/loop.py now uses. The gate
    must NOT fire here: Code-Review genuinely did run this round (task019's own real shape,
    confirmed live), even though await_verification() itself never saw it directly.
    """
    from manager.tools import fold_code_review_findings

    real_code_review_output = SpecialistOutput(
        task_id=uuid.uuid4(), specialist_type=SpecialistType.code_review,
        summary="real concurrent Code-Review result",
        detail={"findings": [{"severity": "blocking", "explanation": "a real, genuine finding"}]},
        claims_complete=False,
    )
    registry.clear()
    try:
        registry.register(SpecialistType.testing_qa, _FakeValidator({
            "reproduction_confirmed": True, "spot_check_mismatch": False,
            "unverified_shapes_targeted": ["onchange_behavior"],
        }))
        contract = _contract()
        # Step 1: exactly manager/loop.py's own concurrent call -- code_review_output=None.
        result = asyncio.run(
            await_verification(contract, _build_output(), client=None, model="x", code_review_output=None)
        )
        # Step 2: the real, concurrently-obtained Code-Review result is folded in separately.
        result = fold_code_review_findings(result, real_code_review_output)
        # Step 3: the gate, applied with the SAME real value -- must recognize Code-Review
        # genuinely ran, not the None await_verification() itself was given.
        result = apply_unverified_shape_confidence_gate(result, contract, real_code_review_output)
    finally:
        registry.clear()
    assert "Confidence gate" not in result.notes, (
        "the gate must not claim 'no Code-Review pass ran this round' when a real, "
        "concurrently-obtained Code-Review result is genuinely available"
    )
    assert "a real, genuine finding" in result.notes, "Code-Review's own real finding must still be folded in"
    print("PASS: the gate correctly sees a real, concurrently-obtained Code-Review result instead "
          "of the None await_verification() itself was deliberately given -- the real task019 bug")


if __name__ == "__main__":
    test_unverified_shape_with_no_code_review_downgrades_an_otherwise_passing_result()
    test_unverified_shape_with_a_code_review_pass_is_left_untouched()
    test_no_unverified_shapes_is_a_pure_no_op()
    test_already_failing_result_is_never_touched_by_this_gate()
    test_deferred_concurrent_code_review_result_is_correctly_seen_by_the_gate()
    print("\nALL UNVERIFIED-SHAPE CONFIDENCE-GATE TESTS PASSED")
