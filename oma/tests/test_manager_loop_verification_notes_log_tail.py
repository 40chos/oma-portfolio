"""Phase 20 Area 2 (2026-07-21): unit tests for manager/loop.py's
build_verification_notes_with_log_tail() -- pure logic, no live SSH/
DB/gateway needed, same style as test_manager_loop_real_install_failure_gate.py.

Real bug this fixes: an uninterrupted, no-mid-run-fixing 7-task retest
pass (2026-07-21, run specifically to get an honest current snapshot
after 11 earlier fixes) found the DOMINANT remaining failure mode was
a generic "Sandbox install failed: Install failed (rc=255) -- see
log_tail for detail" repeating IDENTICALLY across 3-5 rounds on 4 of 7
tasks (#43, #49, #51, #54). Direct DB inspection confirmed the real
diagnostic content (`sandbox_log_tail`/`install_log_tail`, already
computed by _extract_error_excerpt() -- a real, targeted excerpt
centered on the first error marker, not a blind tail) was present in
build_output.detail but NEVER reached `verification.notes` -- the one
place both classify_root_cause() and the next round's own contract
rules actually read from. Build had been retrying identical content
blind, with no real information to correct against, every round.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from manager.loop import build_verification_notes_with_log_tail


def test_appends_sandbox_log_tail_when_present():
    summary = "Sandbox install failed: Install failed (rc=255) -- see log_tail for detail."
    detail = {"sandbox_failed": True, "sandbox_log_tail": "Traceback (most recent call last):\n  ValueError: real cause"}
    notes = build_verification_notes_with_log_tail(summary, detail)
    assert "ValueError: real cause" in notes
    assert notes.startswith(summary)
    print("PASS: sandbox_log_tail is appended to the generic summary")


def test_appends_install_log_tail_when_present():
    summary = "Scaffolded, wrote, and linted module 'oma_x'; install FAILED: Install failed (rc=255) -- see log_tail for detail."
    detail = {"module_name": "oma_x", "db": "odoo16_dev", "install_log_tail": "IntegrityError: duplicate key value"}
    notes = build_verification_notes_with_log_tail(summary, detail)
    assert "IntegrityError: duplicate key value" in notes
    print("PASS: install_log_tail is appended to the generic summary")


def test_prefers_sandbox_log_tail_when_both_somehow_present():
    detail = {"sandbox_log_tail": "SANDBOX_REASON", "install_log_tail": "INSTALL_REASON"}
    notes = build_verification_notes_with_log_tail("summary", detail)
    assert "SANDBOX_REASON" in notes
    assert "INSTALL_REASON" not in notes
    print("PASS: sandbox_log_tail takes precedence (the earlier pipeline stage) when both are somehow present")


def test_returns_summary_unchanged_when_no_log_tail_at_all():
    summary = "Reproduction confirmed for hr.employee.badge_expiry_date."
    detail = {"module_name": "oma_x", "db": "odoo16_dev"}
    notes = build_verification_notes_with_log_tail(summary, detail)
    assert notes == summary
    print("PASS: a round with no log_tail content at all is returned unchanged, no spurious appendix")


def test_returns_summary_unchanged_when_log_tail_is_empty_string():
    detail = {"sandbox_log_tail": "", "install_log_tail": ""}
    notes = build_verification_notes_with_log_tail("summary text", detail)
    assert notes == "summary text"
    print("PASS: an empty-string log_tail is treated the same as absent, no blank appendix")


if __name__ == "__main__":
    test_appends_sandbox_log_tail_when_present()
    test_appends_install_log_tail_when_present()
    test_prefers_sandbox_log_tail_when_both_somehow_present()
    test_returns_summary_unchanged_when_no_log_tail_at_all()
    test_returns_summary_unchanged_when_log_tail_is_empty_string()
    print("\nALL VERIFICATION-NOTES-LOG-TAIL TESTS PASSED")
