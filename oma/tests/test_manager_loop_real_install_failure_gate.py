"""Phase 20 Area 2 (2026-07-20): unit tests for
manager/loop.py's real_install_genuinely_failed() -- pure logic, no
live SSH/DB/gateway needed, same style as
test_manager_loop_round_cleanup.py.

Real bug this fixes: found live on task #49
(hr.employee.badge_expiry_date). Build's sandbox preflight passed and
files were written for real, but the REAL install into the real
target then failed (install_result.success=False, e.g. rc=255).
Nothing short-circuited Testing/QA, which went on to verify the
security-group restriction against the live DB and (correctly, since
nothing installed) reported "NO group restriction at all -- visible
to everyone" -- an accurate but misleading symptom that became the
round's own reported failure reason for 3 consecutive rounds, while
the real cause (the actual install error) was never surfaced to
Build/bug_fix's next-round retry. Same failure family as the existing
sandbox_failed short-circuit, just triggered by the LATER install
call instead of the earlier preflight one.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from manager.loop import real_install_genuinely_failed


def test_true_when_real_install_reached_and_failed():
    detail = {"module_name": "oma_x", "db": "odoo16_dev", "install_error_kind": "install_failed"}
    assert real_install_genuinely_failed(detail, False) is True
    print("PASS: a real install attempt that failed is correctly flagged")


def test_false_when_real_install_succeeded():
    detail = {"module_name": "oma_x", "db": "odoo16_dev", "install_error_kind": None}
    assert real_install_genuinely_failed(detail, True) is False
    print("PASS: a successful real install is never flagged")


def test_false_when_install_never_reached_db_absent():
    # A pre-write validation rejection (e.g. _validate_new_model_name_not_already_real)
    # or a sandbox-preflight failure never includes "db" in detail at all.
    detail = {"module_name": "oma_x", "sandbox_failed": True}
    assert real_install_genuinely_failed(detail, False) is False
    print("PASS: a round that never reached the real install step (no 'db' key) is not flagged")


def test_false_when_db_present_but_claims_complete_true():
    # Defensive: claims_complete=True should never coexist with "db"
    # present in practice, but the predicate must not misfire if it did.
    detail = {"module_name": "oma_x", "db": "odoo16_dev"}
    assert real_install_genuinely_failed(detail, True) is False
    print("PASS: claims_complete=True is never treated as a real install failure regardless of 'db'")


if __name__ == "__main__":
    test_true_when_real_install_reached_and_failed()
    test_false_when_real_install_succeeded()
    test_false_when_install_never_reached_db_absent()
    test_false_when_db_present_but_claims_complete_true()
    print("\nALL REAL-INSTALL-FAILURE GATE TESTS PASSED")
