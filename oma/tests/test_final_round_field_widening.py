"""P12 Tier A item 25 (docs/planning/PHASE30_ARCH_SYNTHESIS_CRITIQUE_FINAL_2026-07-30.md §4,
A Finding 15): tests for _reverify_earlier_constraints_field_targets() -- the field-existence
sibling of the final-round widening the security/menu checks already had. Real, confirmed gap:
field-existence reproduction never re-verified earlier sub-contracts' targets, even on the
final round; regression protection relied entirely on Build's own non-independent text diff.
Mocks _extract_all_reproduction_targets()/_check_field_exists_on_model() directly, zero LLM/GPU
calls.
"""

import asyncio
import os
import sys
import uuid
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import specialists.testing_qa.specialist as testing_qa_module
from contracts.schema import AutonomyTier, CapabilityClass, SpecialistType, TaskContract
from specialists.testing_qa.specialist import ReproductionTarget, ReproductionTargetList


def _contract() -> TaskContract:
    return TaskContract(
        task_id=uuid.uuid4(), specialist_type=SpecialistType.bug_fix,
        capability_class=CapabilityClass.module_development, tier=AutonomyTier.tier_1_readonly,
        goal="x", inputs=[], rules=[], deliverables=[], compensating_actions=[],
        validation_by="testing_qa", pause_if=[], turn_budget=10,
    )


def _make_specialist():
    return testing_qa_module.TestingQASpecialist(client=None, routine_model="fake-model")


def test_returns_true_when_every_earlier_target_still_confirmed():
    specialist = _make_specialist()
    contract = _contract()
    already_checked = ReproductionTarget(model="student.model", field_name="age")
    target_list = ReproductionTargetList(targets=[
        already_checked,
        ReproductionTarget(model="student.model", field_name="name"),
    ])

    async def fake_extract(c, m):
        return target_list

    def fake_check(db, model, field_name, task_id):
        return True  # every earlier field genuinely confirmed

    async def run():
        with patch.object(specialist, "_extract_all_reproduction_targets", fake_extract):
            with patch.object(testing_qa_module, "_check_field_exists_on_model", fake_check):
                return await specialist._reverify_earlier_constraints_field_targets(contract, "mod", "odoo16_dev", already_checked)

    confirmed, notes = asyncio.run(run())
    assert confirmed is True
    assert notes == ""
    print("PASS: every earlier constraint's own claimed field still confirmed -- returns True, empty notes")


def test_returns_false_when_an_earlier_field_is_genuinely_missing():
    specialist = _make_specialist()
    contract = _contract()
    already_checked = ReproductionTarget(model="student.model", field_name="age")
    target_list = ReproductionTargetList(targets=[
        already_checked,
        ReproductionTarget(model="student.model", field_name="regressed_field"),
    ])

    async def fake_extract(c, m):
        return target_list

    def fake_check(db, model, field_name, task_id):
        return field_name != "regressed_field"  # every field real EXCEPT the regressed one

    async def run():
        with patch.object(specialist, "_extract_all_reproduction_targets", fake_extract):
            with patch.object(testing_qa_module, "_check_field_exists_on_model", fake_check):
                return await specialist._reverify_earlier_constraints_field_targets(contract, "mod", "odoo16_dev", already_checked)

    confirmed, notes = asyncio.run(run())
    assert confirmed is False
    assert "regressed_field" in notes
    print("PASS: a genuinely regressed earlier field is caught -- returns False with the real field name in notes")


def test_the_already_checked_target_is_never_re_checked_twice():
    specialist = _make_specialist()
    contract = _contract()
    already_checked = ReproductionTarget(model="student.model", field_name="age")
    target_list = ReproductionTargetList(targets=[already_checked])  # only the already-checked one
    check_calls = []

    async def fake_extract(c, m):
        return target_list

    def fake_check(db, model, field_name, task_id):
        check_calls.append(field_name)
        return True

    async def run():
        with patch.object(specialist, "_extract_all_reproduction_targets", fake_extract):
            with patch.object(testing_qa_module, "_check_field_exists_on_model", fake_check):
                return await specialist._reverify_earlier_constraints_field_targets(contract, "mod", "odoo16_dev", already_checked)

    confirmed, notes = asyncio.run(run())
    assert confirmed is True
    assert check_calls == [], "the already-checked target must never be re-checked -- it was already independently verified moments ago"
    print("PASS: the already-checked target is skipped entirely, never re-verified twice")


def test_uncertain_result_does_not_fail_the_round():
    specialist = _make_specialist()
    contract = _contract()
    already_checked = ReproductionTarget(model="student.model", field_name="age")
    target_list = ReproductionTargetList(targets=[
        already_checked, ReproductionTarget(model="student.model", field_name="uncertain_field"),
    ])

    async def fake_extract(c, m):
        return target_list

    def fake_check(db, model, field_name, task_id):
        return None  # genuine infra uncertainty

    async def run():
        with patch.object(specialist, "_extract_all_reproduction_targets", fake_extract):
            with patch.object(testing_qa_module, "_check_field_exists_on_model", fake_check):
                return await specialist._reverify_earlier_constraints_field_targets(contract, "mod", "odoo16_dev", already_checked)

    confirmed, notes = asyncio.run(run())
    assert confirmed is True, "genuine uncertainty (None) must never fail the round -- only a CONFIRMED absence (False) does"
    print("PASS: genuine uncertainty (None) on an earlier field never fails the round, only a confirmed absence does")


def test_extraction_failure_fails_open():
    specialist = _make_specialist()
    contract = _contract()
    already_checked = ReproductionTarget(model="student.model", field_name="age")

    async def fake_extract_raises(c, m):
        raise RuntimeError("gateway error")

    async def run():
        with patch.object(specialist, "_extract_all_reproduction_targets", fake_extract_raises):
            return await specialist._reverify_earlier_constraints_field_targets(contract, "mod", "odoo16_dev", already_checked)

    confirmed, notes = asyncio.run(run())
    assert confirmed is True
    assert notes == ""
    print("PASS: a genuine extraction failure fails open (True, no notes) -- this is an additive regression check, not the primary verification path")


if __name__ == "__main__":
    test_returns_true_when_every_earlier_target_still_confirmed()
    test_returns_false_when_an_earlier_field_is_genuinely_missing()
    test_the_already_checked_target_is_never_re_checked_twice()
    test_uncertain_result_does_not_fail_the_round()
    test_extraction_failure_fails_open()
    print("\nALL FINAL-ROUND FIELD-WIDENING TESTS PASSED")
