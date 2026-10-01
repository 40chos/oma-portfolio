"""Phase 7: a permanent regression test for the hardcoded Production-Odoo
safety guard. Per the project owner's explicit instruction: port 8070 / database
16_202012 is the live corporate Production instance and must NEVER be
touched, under any circumstance. This guard is deliberately an
ALLOW-LIST, not a deny-list -- it only lets the one known-good target
(and its own recognized duplicates) through, rather than trying to
blocklist the one known-bad value and hoping nothing else bad shows up.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from infra.odoo_settings import ProductionOdooGuardError, _assert_safe_odoo_target, load_odoo_settings


def test_canonical_dev_target_is_allowed():
    s = load_odoo_settings()
    assert s.db == "odoo16_dev"
    print("PASS: the canonical dev target (odoo16_dev, port 8071) is allowed")


def test_recognized_duplicate_name_is_allowed():
    s = load_odoo_settings(db_override="odoo16_dev_dup_20260707")
    assert s.db == "odoo16_dev_dup_20260707"
    print("PASS: a recognized duplicate name (odoo16_dev_dup_YYYYMMDD) is allowed")


def test_recognized_fresh_test_db_name_is_allowed():
    """Phase 9.5 addition: a genuinely fresh test database created via
    infra.odoo_admin.create_database(), used to isolate whether the
    Postgres-ownership install blocker is specific to duplicate_database()
    or systemic -- same narrow, exact-pattern allow-list treatment as
    the duplicate-name pattern above.
    """
    s = load_odoo_settings(db_override="odoo16_dev_fresh_20260707_123141")
    assert s.db == "odoo16_dev_fresh_20260707_123141"
    print("PASS: a recognized fresh-test-db name (odoo16_dev_fresh_YYYYMMDD_HHMMSS) is allowed")


def test_forbidden_production_db_is_refused():
    raised = False
    try:
        _assert_safe_odoo_target("16_202012", 8071)
    except ProductionOdooGuardError:
        raised = True
    assert raised, "the forbidden Production db (16_202012) MUST be refused"
    print("PASS: the forbidden Production database name is refused, even on the right port")


def test_forbidden_production_port_is_refused():
    raised = False
    try:
        _assert_safe_odoo_target("odoo16_dev", 8070)
    except ProductionOdooGuardError:
        raised = True
    assert raised, "the forbidden Production port (8070) MUST be refused"
    print("PASS: the forbidden Production port is refused, even with the right db name")


def test_forbidden_port_refused_even_with_a_legitimate_duplicate_name():
    raised = False
    try:
        _assert_safe_odoo_target("odoo16_dev_dup_20260707", 8070)
    except ProductionOdooGuardError:
        raised = True
    assert raised, "port 8070 must be refused even paired with an otherwise-legitimate duplicate name"
    print("PASS: a legitimate duplicate name on the forbidden port is still refused")


def test_unrecognized_target_is_refused_allowlist_not_denylist():
    """The core design property: this is an allow-list. An unrecognized
    db name that ISN'T the known Production value must ALSO be refused
    -- proving this doesn't just blocklist one bad value.
    """
    raised = False
    try:
        _assert_safe_odoo_target("some_other_db_nobody_named", 8071)
    except ProductionOdooGuardError:
        raised = True
    assert raised, "an unrecognized db name must be refused even though it isn't the known Production value"
    print("PASS: an unrecognized target is refused (allow-list behavior, not a deny-list)")


if __name__ == "__main__":
    test_canonical_dev_target_is_allowed()
    test_recognized_duplicate_name_is_allowed()
    test_recognized_fresh_test_db_name_is_allowed()
    test_forbidden_production_db_is_refused()
    test_forbidden_production_port_is_refused()
    test_forbidden_port_refused_even_with_a_legitimate_duplicate_name()
    test_unrecognized_target_is_refused_allowlist_not_denylist()
    print("\nALL ODOO SETTINGS GUARD TESTS PASSED")
