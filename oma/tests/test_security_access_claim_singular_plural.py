"""Real, confirmed bug found live (2026-08-09, task 07141af5's flagship run, ticket_access_rights
node): specialists.testing_qa.specialist._extract_security_access_claim() is deliberately blind to
the generated code (it must verify what the goal CLAIMS, not what Build wrote). When a goal only
describes a role in prose ("operations managers need full visibility...") rather than literally
naming an exact group string, Build's own independent generation ("Operations Manager", singular
Title Case) and the extraction call's own independent guess ("Operations Managers", plural) can
land on different grammatical numbers of the SAME role name -- check_group_exists's exact-string
match then fails forever, regardless of how correct Build's content is. Confirmed live: install
succeeded, the real security.xml/DB had the correctly-named group, and the check still failed
because it was checking for the wrong spelling entirely.

_singular_plural_variant() + _group_exists()'s new fallback close this: pure, isolated tests using
a non-fast-path db (task_id=None) so only the slow tools_odoo.spot_check.check_group_exists path
is exercised, monkeypatched to simulate the real registry without a live Odoo instance.
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import specialists.testing_qa.specialist as tq
from specialists.testing_qa.specialist import SecurityAccessClaim, TestingQASpecialist, _singular_plural_variant


def test_singular_plural_variant_covers_the_real_incident_shape():
    assert _singular_plural_variant("Operations Manager") == "Operations Managers"
    assert _singular_plural_variant("Operations Managers") == "Operations Manager"
    assert _singular_plural_variant("Field Technician") == "Field Technicians"
    assert _singular_plural_variant("Category") == "Categories"
    assert _singular_plural_variant("Categories") == "Category"
    assert _singular_plural_variant("") is None
    print("PASS: _singular_plural_variant covers singular/plural/ies shapes, including the real incident")


def _make_specialist():
    return TestingQASpecialist(client=None)


def test_verify_security_access_claim_accepts_the_plural_claim_against_a_singular_real_group(monkeypatch):
    # Real incident shape: goal said "operations managers" (plural), extraction guessed
    # "Operations Managers" (plural), but the real registry only has "Operations Manager"
    # (singular) -- must still pass, not escalate a real, correct implementation.
    def fake_check_group_exists(db, group_name):
        return group_name == "Operations Manager"

    monkeypatch.setattr(tq, "check_group_exists", fake_check_group_exists)
    specialist = _make_specialist()
    claim = SecurityAccessClaim(applicable=True, group_name="Operations Managers", model=None)
    passed, message = asyncio.run(specialist._verify_security_access_claim(claim, db="not_a_fast_path_db"))
    assert passed, message
    print("PASS: a plural claim against a real singular group name passes via the variant fallback")


def test_verify_security_access_claim_still_fails_for_a_genuinely_missing_group(monkeypatch):
    def fake_check_group_exists(db, group_name):
        return False

    monkeypatch.setattr(tq, "check_group_exists", fake_check_group_exists)
    specialist = _make_specialist()
    claim = SecurityAccessClaim(applicable=True, group_name="Totally Nonexistent Group", model=None)
    passed, message = asyncio.run(specialist._verify_security_access_claim(claim, db="not_a_fast_path_db"))
    assert not passed
    assert "does not exist" in message
    print("PASS: a genuinely missing group (no variant matches either) still correctly fails")


def test_verify_security_access_claim_resolves_the_variant_name_for_the_permission_check(monkeypatch):
    # The follow-up model-level permission check must use the RESOLVED (actually-real) name,
    # not the originally-claimed spelling, or it would fail identically right after existence
    # passes via the variant fallback.
    seen_permission_check_names = []

    def fake_check_group_exists(db, group_name):
        return group_name == "Operations Manager"

    def fake_check_group_model_access(db, group_name, model, er, ew, ec, eu):
        seen_permission_check_names.append(group_name)
        return True, "ok"

    monkeypatch.setattr(tq, "check_group_exists", fake_check_group_exists)
    monkeypatch.setattr(tq, "check_group_model_access", fake_check_group_model_access)
    specialist = _make_specialist()
    claim = SecurityAccessClaim(
        applicable=True, group_name="Operations Managers", model="oma.service.ticket",
        expects_read=True, expects_write=True, expects_create=True, expects_unlink=False,
    )
    passed, message = asyncio.run(specialist._verify_security_access_claim(claim, db="not_a_fast_path_db"))
    assert passed, message
    assert seen_permission_check_names == ["Operations Manager"], seen_permission_check_names
    print("PASS: the follow-up permission check uses the resolved (real) group name, not the claimed spelling")


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

    test_singular_plural_variant_covers_the_real_incident_shape()

    for fn in [
        test_verify_security_access_claim_accepts_the_plural_claim_against_a_singular_real_group,
        test_verify_security_access_claim_still_fails_for_a_genuinely_missing_group,
        test_verify_security_access_claim_resolves_the_variant_name_for_the_permission_check,
    ]:
        mp = _FakeMonkeypatch()
        try:
            fn(mp)
        finally:
            mp.undo()

    print("\nALL SECURITY-ACCESS-CLAIM SINGULAR/PLURAL TESTS PASSED")
