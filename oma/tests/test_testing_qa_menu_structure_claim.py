"""Phase 28C (2026-07-28): unit tests for MenuStructureClaim /
_verify_menu_structure_claim -- the real, genuine ground-truth
verification capability built to close the structural field-only gap
in Testing/QA's own reproduction check.

Root cause: `ReproductionTarget` (the ONLY reproduction-check shape
that existed before this) is `{model, field_name}` -- structurally
incapable of representing a menu-structure claim (`school_student`
task's own `menu_structure` constraint: "Menu structure: School ->
Students"). A menu round's own `field_name` is forced to be something
that was never a real ORM field, so the field-existence check could
NEVER pass for this shape, regardless of how correct the real
generated content was.

Fixed by following this project's own already-established, extensible
pattern (`SecurityAccessClaim` / `_verify_security_access_claim`,
which itself real-verifies via live ORM/DB reads, never the LLM's own
self-report) rather than any fallback/shortcut: a new
`MenuStructureClaim` extracted from `contract.goal`/`deliverables`
ALONE (never from generated code, so a missing/wrong menu can't
trivially "pass" by having extraction just describe the code), verified
against the real live `ir.ui.menu`/`ir_actions` registry via
`check_menu_exists()` (tools_odoo/spot_check.py) -- deterministic, no
LLM in the verification path at all.

Same style as test_testing_qa_reproduction_target_autocorrect.py:
monkeypatched dependencies, no live SSH/DB/gateway needed.
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import specialists.testing_qa.specialist as testing_qa_module
from specialists.testing_qa.specialist import (
    MenuStructureClaim,
    TestingQASpecialist,
)


def _make_specialist():
    return TestingQASpecialist(client=None, routine_model="fake-model")


def test_verify_menu_structure_claim_not_applicable_case_needs_no_call():
    claim = MenuStructureClaim(applicable=False)
    assert not claim.menu_names
    print("PASS: a non-applicable claim carries no menu_names, matching the run() gate that "
          "never even reaches _verify_menu_structure_claim for this case")


def test_verify_menu_structure_claim_no_menu_names_fails_conservatively():
    specialist = _make_specialist()
    claim = MenuStructureClaim(applicable=True, menu_names=[])
    passed, notes = asyncio.run(specialist._verify_menu_structure_claim(claim, "odoo16_dev"))
    assert not passed
    print(f"PASS: an incomplete claim (applicable but no menu_names) fails conservatively: {notes!r}")


def test_verify_menu_structure_claim_root_menu_missing_fails(monkeypatch):
    def fake_check_menu_exists(db, menu_name, parent_menu_name=None, action_model=None):
        return False, f"no ir.ui.menu record named {menu_name!r} exists in the real registry"

    monkeypatch.setattr(testing_qa_module, "check_menu_exists", fake_check_menu_exists)

    specialist = _make_specialist()
    claim = MenuStructureClaim(applicable=True, menu_names=["School", "Students"])
    passed, notes = asyncio.run(specialist._verify_menu_structure_claim(claim, "odoo16_dev"))
    assert not passed
    assert "School" in notes
    print(f"PASS: a genuinely missing root menu fails the real registry check: {notes!r}")


def test_verify_menu_structure_claim_leaf_wrong_parent_fails(monkeypatch):
    def fake_check_menu_exists(db, menu_name, parent_menu_name=None, action_model=None):
        if menu_name == "School":
            return True, "menu 'School' genuinely exists"
        # The leaf exists, but under a DIFFERENT real parent than claimed.
        return False, f"menu {menu_name!r} exists but its real parent is ['Settings'], not the claimed {parent_menu_name!r}"

    monkeypatch.setattr(testing_qa_module, "check_menu_exists", fake_check_menu_exists)

    specialist = _make_specialist()
    claim = MenuStructureClaim(applicable=True, menu_names=["School", "Students"])
    passed, notes = asyncio.run(specialist._verify_menu_structure_claim(claim, "odoo16_dev"))
    assert not passed
    assert "Students" in notes
    print(f"PASS: a leaf menu nested under the wrong real parent fails: {notes!r}")


def test_verify_menu_structure_claim_wrong_action_model_fails(monkeypatch):
    def fake_check_menu_exists(db, menu_name, parent_menu_name=None, action_model=None):
        if menu_name == "School":
            return True, "menu 'School' genuinely exists"
        if action_model is not None:
            return False, (
                f"menu {menu_name!r} exists but its real window action targets "
                f"['res.partner'], not the claimed {action_model!r}"
            )
        return True, f"menu {menu_name!r} genuinely exists"

    monkeypatch.setattr(testing_qa_module, "check_menu_exists", fake_check_menu_exists)

    specialist = _make_specialist()
    claim = MenuStructureClaim(applicable=True, menu_names=["School", "Students"], action_model="school.student")
    passed, notes = asyncio.run(specialist._verify_menu_structure_claim(claim, "odoo16_dev"))
    assert not passed
    assert "res.partner" in notes
    print(f"PASS: a leaf menu whose real action targets the wrong model fails: {notes!r}")


def test_verify_menu_structure_claim_genuinely_correct_hierarchy_passes(monkeypatch):
    def fake_check_menu_exists(db, menu_name, parent_menu_name=None, action_model=None):
        return True, f"menu {menu_name!r} genuinely exists in the real registry, matching every claimed detail"

    monkeypatch.setattr(testing_qa_module, "check_menu_exists", fake_check_menu_exists)

    specialist = _make_specialist()
    claim = MenuStructureClaim(applicable=True, menu_names=["School", "Students"], action_model="school.student")
    passed, notes = asyncio.run(specialist._verify_menu_structure_claim(claim, "odoo16_dev"))
    assert passed
    print(f"PASS: a genuinely correct, fully-matching real menu hierarchy passes: {notes!r}")


def test_verify_menu_structure_claim_genuine_uncertainty_fails_conservatively(monkeypatch):
    def fake_check_menu_exists(db, menu_name, parent_menu_name=None, action_model=None):
        return None  # "never guess on None" -- the established discipline in spot_check.py

    monkeypatch.setattr(testing_qa_module, "check_menu_exists", fake_check_menu_exists)

    specialist = _make_specialist()
    claim = MenuStructureClaim(applicable=True, menu_names=["School"])
    passed, notes = asyncio.run(specialist._verify_menu_structure_claim(claim, "odoo16_dev"))
    assert not passed
    print(f"PASS: genuine uncertainty (None from the real check) fails conservatively, never guesses: {notes!r}")


class _FakeMonkeypatch:
    """Minimal standalone stand-in for pytest's monkeypatch fixture, so
    this file can also be run directly with `python3` (no pytest, no
    live SSH/DB/gateway needed), same convention as the sibling
    test_testing_qa_reproduction_target_autocorrect.py.
    """

    def __init__(self):
        self._saved = []

    def setattr(self, obj, name, value):
        self._saved.append((obj, name, getattr(obj, name)))
        setattr(obj, name, value)

    def undo(self):
        for obj, name, value in reversed(self._saved):
            setattr(obj, name, value)


if __name__ == "__main__":
    test_verify_menu_structure_claim_not_applicable_case_needs_no_call()
    test_verify_menu_structure_claim_no_menu_names_fails_conservatively()
    for fn in (
        test_verify_menu_structure_claim_root_menu_missing_fails,
        test_verify_menu_structure_claim_leaf_wrong_parent_fails,
        test_verify_menu_structure_claim_wrong_action_model_fails,
        test_verify_menu_structure_claim_genuinely_correct_hierarchy_passes,
        test_verify_menu_structure_claim_genuine_uncertainty_fails_conservatively,
    ):
        mp = _FakeMonkeypatch()
        try:
            fn(mp)
        finally:
            mp.undo()
    print("\nALL TESTING/QA MENU-STRUCTURE-CLAIM TESTS PASSED")
