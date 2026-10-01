"""Real, confirmed bug found live (2026-08-10, task e65381cc, ticket_access_restriction node):
`SecurityAccessClaim.group_name` is a single field -- built for Area 2's own original claim
shapes, each naming exactly ONE group -- but a goal restricting access to TWO named groups at
once (e.g. "so that only the existing group_field_technician and group_operations_manager groups
can access oma.service.ticket") gives the extraction LLM no single correct answer, and it left
`group_name` empty rather than guess, correctly (and unhelpfully) triggering "security claim
incomplete" even though the real, live, already-verified module content was fully correct.

`_verify_security_access_claim()`'s new fallback (parses the goal's own literal multi-group
restriction sentence directly, resolving each snake_case XML-ID-style group token to its real
display name via `resolve_group_xmlid_to_display_name()` before checking existence/access) is
tested here with pure, isolated, monkeypatched fakes -- no live Odoo instance required -- mirroring
the exact style already established in tests/test_security_access_claim_singular_plural.py.
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import specialists.testing_qa.specialist as tq
from specialists.testing_qa.specialist import SecurityAccessClaim, TestingQASpecialist

GOAL = (
    "Remove the base.group_user access row for oma.service.ticket from ir.model.access.csv, "
    "so that only the existing group_field_technician (restricted by the existing ir.rule to "
    "their own tickets) and group_operations_manager (full access) groups can access "
    "oma.service.ticket at all."
)


def _make_specialist():
    return TestingQASpecialist(client=None)


def test_multi_group_fallback_resolves_xmlid_tokens_and_verifies_both_groups(monkeypatch):
    real_groups = {
        "group_field_technician": "Field Technician",
        "group_operations_manager": "Operations Manager",
    }
    display_names_with_access = {"Field Technician", "Operations Manager"}

    def fake_resolve_group_xmlid_to_display_name(db, token):
        return real_groups.get(token)

    def fake_check_group_exists(db, group_name):
        return group_name in display_names_with_access

    def fake_check_group_model_access(db, group_name, model, er, ew, ec, eu):
        return (True, "ok") if group_name in display_names_with_access else (False, "no access")

    monkeypatch.setattr(tq, "resolve_group_xmlid_to_display_name", fake_resolve_group_xmlid_to_display_name)
    monkeypatch.setattr(tq, "check_group_exists", fake_check_group_exists)
    monkeypatch.setattr(tq, "check_group_model_access", fake_check_group_model_access)

    specialist = _make_specialist()
    claim = SecurityAccessClaim(applicable=True, model="oma.service.ticket")  # group_name left unset
    passed, message = asyncio.run(
        specialist._verify_security_access_claim(claim, db="not_a_fast_path_db", goal=GOAL)
    )
    assert passed, message
    assert "Field Technician" in message and "Operations Manager" in message
    print("PASS: an incomplete (no group_name) claim is verified directly from the goal's own "
          "real multi-group restriction sentence")


def test_multi_group_fallback_fails_if_a_goal_named_group_genuinely_does_not_exist(monkeypatch):
    def fake_resolve_group_xmlid_to_display_name(db, token):
        return None  # neither group resolves to a real record

    def fake_check_group_exists(db, group_name):
        return False

    monkeypatch.setattr(tq, "resolve_group_xmlid_to_display_name", fake_resolve_group_xmlid_to_display_name)
    monkeypatch.setattr(tq, "check_group_exists", fake_check_group_exists)

    specialist = _make_specialist()
    claim = SecurityAccessClaim(applicable=True, model="oma.service.ticket")
    passed, message = asyncio.run(
        specialist._verify_security_access_claim(claim, db="not_a_fast_path_db", goal=GOAL)
    )
    assert not passed
    assert "does not exist" in message
    print("PASS: a genuinely missing goal-named group still correctly fails, not silently passed")


def test_group_name_incomplete_still_fails_without_a_goal_to_fall_back_on():
    specialist = _make_specialist()
    claim = SecurityAccessClaim(applicable=True, model="oma.service.ticket")  # group_name unset
    passed, message = asyncio.run(
        specialist._verify_security_access_claim(claim, db="not_a_fast_path_db", goal="")
    )
    assert not passed
    assert "incomplete" in message
    print("PASS: with no goal text to fall back on, the original 'incomplete' behavior is preserved")


if __name__ == "__main__":
    class _FakeMonkeypatch:
        def __init__(self):
            self._restores = []

        def setattr(self, obj, name, value):
            self._restores.append((obj, name, getattr(obj, name)))
            setattr(obj, name, value)

        def undo(self):
            for obj, name, old in reversed(self._restores):
                setattr(obj, name, old)

    for fn in [
        test_multi_group_fallback_resolves_xmlid_tokens_and_verifies_both_groups,
        test_multi_group_fallback_fails_if_a_goal_named_group_genuinely_does_not_exist,
    ]:
        mp = _FakeMonkeypatch()
        try:
            fn(mp)
        finally:
            mp.undo()

    test_group_name_incomplete_still_fails_without_a_goal_to_fall_back_on()

    print("\nALL MULTI-GROUP SECURITY-ACCESS-CLAIM GOAL-FALLBACK TESTS PASSED")
