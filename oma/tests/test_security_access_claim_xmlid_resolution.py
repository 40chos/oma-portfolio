"""Real, confirmed bug found live overnight (2026-08-14), recurring on 2 separate real tasks,
both of which correctly generated a real, valid `base.group_user` ir.model.access.csv row:
_group_exists()'s fast/direct paths only ever match res.groups' own DISPLAY name ("Internal
User"), never an XML-ID-style token ("base.group_user") -- the exact naming-convention mismatch
resolve_group_xmlid_to_display_name() (tools_odoo/spot_check.py) was already built to close, and
was already used for this exact purpose one call site over (the multi-group goal-restriction
branch), but was never applied in _group_exists() itself, the main path most real security-claim
checks actually go through. Confirmed live: `base.group_user` genuinely exists in the real
registry (verified directly via `env["ir.model.data"].search(...)`), yet the claim verification
reported "does not exist" both times.
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import specialists.testing_qa.specialist as tq
from specialists.testing_qa.specialist import (
    SecurityAccessClaim,
    TestingQASpecialist,
    _looks_like_group_xmlid,
)


def test_looks_like_group_xmlid_recognizes_real_xmlid_shapes():
    assert _looks_like_group_xmlid("base.group_user")
    assert _looks_like_group_xmlid("group_user")
    assert _looks_like_group_xmlid("my_module.group_field_technician")
    assert not _looks_like_group_xmlid("Internal User")
    assert not _looks_like_group_xmlid("Operations Manager")
    print("PASS: _looks_like_group_xmlid distinguishes XML-ID tokens from real display names")


def _make_specialist():
    return TestingQASpecialist(client=None)


def test_xmlid_claim_resolves_to_the_real_display_name_and_passes(monkeypatch):
    # The real incident shape: the CSV correctly says 'base.group_user' (an XML-ID), the display
    # name search alone finds nothing, but the group genuinely exists under its real display name.
    def fake_check_group_exists(db, group_name):
        return group_name == "Internal User"  # only the real display name ever matches

    def fake_resolve_xmlid(db, group_token):
        return "Internal User" if group_token == "base.group_user" else None

    monkeypatch.setattr(tq, "check_group_exists", fake_check_group_exists)
    monkeypatch.setattr("tools_odoo.spot_check.resolve_group_xmlid_to_display_name", fake_resolve_xmlid)
    specialist = _make_specialist()
    claim = SecurityAccessClaim(applicable=True, group_name="base.group_user", model=None)
    passed, message = asyncio.run(specialist._verify_security_access_claim(claim, db="not_a_fast_path_db"))
    assert passed, message
    print("PASS: an XML-ID-shaped claim ('base.group_user') resolves to its real display name and passes")


def test_bare_group_xmlid_without_module_prefix_also_resolves(monkeypatch):
    def fake_check_group_exists(db, group_name):
        return group_name == "Internal User"

    def fake_resolve_xmlid(db, group_token):
        return "Internal User" if group_token == "group_user" else None

    monkeypatch.setattr(tq, "check_group_exists", fake_check_group_exists)
    monkeypatch.setattr("tools_odoo.spot_check.resolve_group_xmlid_to_display_name", fake_resolve_xmlid)
    specialist = _make_specialist()
    claim = SecurityAccessClaim(applicable=True, group_name="group_user", model=None)
    passed, message = asyncio.run(specialist._verify_security_access_claim(claim, db="not_a_fast_path_db"))
    assert passed, message
    print("PASS: a bare, module-prefix-less XML-ID token ('group_user') also resolves correctly")


def test_a_genuinely_nonexistent_xmlid_still_fails(monkeypatch):
    def fake_check_group_exists(db, group_name):
        return False

    def fake_resolve_xmlid(db, group_token):
        return None  # genuinely doesn't resolve to anything real

    monkeypatch.setattr(tq, "check_group_exists", fake_check_group_exists)
    monkeypatch.setattr("tools_odoo.spot_check.resolve_group_xmlid_to_display_name", fake_resolve_xmlid)
    specialist = _make_specialist()
    claim = SecurityAccessClaim(applicable=True, group_name="base.group_totally_fake", model=None)
    passed, message = asyncio.run(specialist._verify_security_access_claim(claim, db="not_a_fast_path_db"))
    assert not passed
    assert "does not exist" in message
    print("PASS: a genuinely nonexistent XML-ID-shaped group still correctly fails, not silently passed")


def test_a_real_display_name_claim_is_unaffected_by_the_new_fallback(monkeypatch):
    # Zero regression: a claim that's already a real display name must keep working exactly as
    # before -- the xmlid fallback must never even be consulted for a non-xmlid-shaped claim.
    resolve_calls = []

    def fake_check_group_exists(db, group_name):
        return group_name == "Operations Manager"

    def fake_resolve_xmlid(db, group_token):
        resolve_calls.append(group_token)
        return None

    monkeypatch.setattr(tq, "check_group_exists", fake_check_group_exists)
    monkeypatch.setattr("tools_odoo.spot_check.resolve_group_xmlid_to_display_name", fake_resolve_xmlid)
    specialist = _make_specialist()
    claim = SecurityAccessClaim(applicable=True, group_name="Operations Manager", model=None)
    passed, message = asyncio.run(specialist._verify_security_access_claim(claim, db="not_a_fast_path_db"))
    assert passed, message
    assert resolve_calls == [], "the xmlid resolver must never be called for an already-real display name"
    print("PASS: a real display-name claim is unaffected, xmlid fallback never consulted")


if __name__ == "__main__":
    class _FakeMonkeypatch:
        def __init__(self):
            self._restores = []

        def setattr(self, target, name_or_value=None, value=None):
            if isinstance(target, str):
                import importlib
                module_path, _, attr = target.rpartition(".")
                obj = importlib.import_module(module_path)
                name = attr
                val = name_or_value
            else:
                obj, name, val = target, name_or_value, value
            self._restores.append((obj, name, getattr(obj, name)))
            setattr(obj, name, val)

        def undo(self):
            for obj, name, old in reversed(self._restores):
                setattr(obj, name, old)

    test_looks_like_group_xmlid_recognizes_real_xmlid_shapes()

    for fn in [
        test_xmlid_claim_resolves_to_the_real_display_name_and_passes,
        test_bare_group_xmlid_without_module_prefix_also_resolves,
        test_a_genuinely_nonexistent_xmlid_still_fails,
        test_a_real_display_name_claim_is_unaffected_by_the_new_fallback,
    ]:
        mp = _FakeMonkeypatch()
        try:
            fn(mp)
        finally:
            mp.undo()

    print("\nALL SECURITY-ACCESS-CLAIM XMLID-RESOLUTION TESTS PASSED")
