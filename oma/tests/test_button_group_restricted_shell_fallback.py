"""P12 Tier A item 12 (docs/planning/PHASE30_ARCH_SYNTHESIS_CRITIQUE_FINAL_2026-07-30.md §4):
tests for check_button_group_restricted() -- the odoo-bin-shell fallback that was previously
entirely missing (the button-restriction security check could never pass at all on a
non-fast-path DB, regardless of correctness). Mocks _run_odoo_shell_script() directly (no live
SSH/DB calls), same pattern as this file's own siblings would use.
"""

import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tools_odoo.spot_check import check_button_group_restricted


def test_none_when_shell_script_itself_fails():
    with patch("tools_odoo.spot_check._run_odoo_shell_script", return_value=None):
        result = check_button_group_restricted("odoo16_dev", "res.partner", "action_send", group_xmlid="base.group_system")
    assert result is None
    print("PASS: returns None (never a guessed answer) when the shell script itself fails")


def test_none_when_no_form_view_resolvable():
    with patch("tools_odoo.spot_check._run_odoo_shell_script", return_value="BUTTON_CHECK_ARCH=NONE\n"):
        result = check_button_group_restricted("odoo16_dev", "res.partner", "action_send", group_xmlid="base.group_system")
    assert result is None
    print("PASS: returns None when no form view is resolvable at all")


def test_none_when_button_not_found_in_arch():
    arch = "<form><button name=\"other_button\" groups=\"base.group_system\"/></form>"
    with patch("tools_odoo.spot_check._run_odoo_shell_script", return_value=f"BUTTON_CHECK_ARCH={arch!r}\n"):
        result = check_button_group_restricted("odoo16_dev", "res.partner", "action_send", group_xmlid="base.group_system")
    assert result is None
    print("PASS: returns None when the named button isn't found in the resolved arch at all")


def test_false_when_button_has_no_group_restriction():
    arch = "<form><button name=\"action_send\"/></form>"
    with patch("tools_odoo.spot_check._run_odoo_shell_script", return_value=f"BUTTON_CHECK_ARCH={arch!r}\n"):
        result = check_button_group_restricted("odoo16_dev", "res.partner", "action_send", group_xmlid="base.group_system")
    assert result == (False, "button 'action_send' on 'res.partner' has NO group restriction at all -- visible to everyone")
    print("PASS: correctly reports False when the button has no groups= attribute at all")


def test_true_when_button_correctly_restricted_by_xmlid():
    arch = "<form><button name=\"action_send\" groups=\"base.group_system\"/></form>"
    with patch("tools_odoo.spot_check._run_odoo_shell_script", return_value=f"BUTTON_CHECK_ARCH={arch!r}\n"):
        result = check_button_group_restricted("odoo16_dev", "res.partner", "action_send", group_xmlid="base.group_system")
    assert result == (True, "button 'action_send' on 'res.partner' is correctly restricted to 'base.group_system'")
    print("PASS: correctly matches on the raw xmlid, no second shell call needed")


def test_false_when_restricted_to_a_different_group():
    arch = "<form><button name=\"action_send\" groups=\"base.group_erp_manager\"/></form>"

    def fake_shell(db, script):
        if "BUTTON_CHECK_ARCH" in script or True:
            if "get_views" in script:
                return f"BUTTON_CHECK_ARCH={arch!r}\n"
            return "BUTTON_CHECK_NAMES=['Administration / Access Rights']\n"

    with patch("tools_odoo.spot_check._run_odoo_shell_script", side_effect=fake_shell):
        result = check_button_group_restricted("odoo16_dev", "res.partner", "action_send", group_xmlid="base.group_system")
    assert result[0] is False
    assert "not the claimed group" in result[1]
    print("PASS: correctly reports False and names the real restricting group when it doesn't match the claim")


if __name__ == "__main__":
    test_none_when_shell_script_itself_fails()
    test_none_when_no_form_view_resolvable()
    test_none_when_button_not_found_in_arch()
    test_false_when_button_has_no_group_restriction()
    test_true_when_button_correctly_restricted_by_xmlid()
    test_false_when_restricted_to_a_different_group()
    print("\nALL BUTTON-GROUP-RESTRICTED SHELL-FALLBACK TESTS PASSED")
