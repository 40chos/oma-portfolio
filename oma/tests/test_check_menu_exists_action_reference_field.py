"""Real, confirmed bug found live (2026-08-10, task e65381cc, equipment_views_menu node,
deterministically reproduced EVERY single round since round 1 of that task -- confirmed via a
direct SSH+odoo-shell repro against the real odoo16_dev db, not guessed): `check_menu_exists()`'s
own SQL had two stacked defects that made it raise a hard SQL error for ANY menu with an action
at all, regardless of correctness, which `_run_odoo_shell_script()`'s own conservative
"nonzero exit -> None" contract then surfaced as permanent, unrecoverable "genuine uncertainty":

1. `ir_ui_menu.action` is a real Odoo `fields.Reference` column, stored as plain TEXT in
   Postgres (e.g. `'ir.actions.act_window,45'`), never a bare integer FK -- `a.id = m.action`
   raised `operator does not exist: integer = character varying`.
2. `ir_actions` is the polymorphic BASE table Odoo's own table inheritance uses for every action
   type -- it has no `res_model` column; that lives only on the child table `ir_act_window`.

See tools_odoo/spot_check.py's `check_menu_exists()` for the full incident and fix.
"""

import os
import sys
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tools_odoo.spot_check import check_menu_exists


def _fake_proc(stdout="", returncode=0, stderr=""):
    proc = MagicMock()
    proc.stdout = stdout
    proc.stderr = stderr
    proc.returncode = returncode
    return proc


def test_query_parses_the_reference_field_and_joins_ir_act_window_not_ir_actions():
    with patch("tools_odoo.spot_check._run_in_container") as mock_run:
        mock_run.return_value = _fake_proc(stdout="MENU_CHECK rows=[]\n")
        check_menu_exists("db", "Equipment")
    cmd = mock_run.call_args[0][0]
    import base64
    encoded = cmd.split("echo ", 1)[1].split(" | base64", 1)[0]
    script = base64.b64decode(encoded).decode()
    assert "split_part(m.action, ',', 2)::integer" in script, (
        f"expected the reference-field parse to be present in the generated SQL, got: {script!r}"
    )
    assert "ir_act_window" in script, "must join against ir_act_window (has res_model), not ir_actions"
    assert " ir_actions " not in f" {script} ", "must never join the polymorphic base table directly"
    print("PASS: the generated SQL correctly parses the Reference-field action column and "
          "joins ir_act_window (which actually has res_model), never the base ir_actions table")


def test_a_real_row_with_no_action_still_resolves_correctly():
    with patch("tools_odoo.spot_check._run_in_container") as mock_run:
        mock_run.return_value = _fake_proc(stdout="MENU_CHECK rows=[(5, None, None)]\n")
        result = check_menu_exists("db", "Settings")
    assert result == (True, "menu 'Settings' genuinely exists in the real registry, matching every claimed detail")
    print("PASS: a menu with no action at all (a pure parent/folder menu) still resolves correctly")


def test_a_real_row_with_a_window_action_resolves_the_real_model():
    with patch("tools_odoo.spot_check._run_in_container") as mock_run:
        mock_run.return_value = _fake_proc(stdout="MENU_CHECK rows=[(5, 'Equipment', 'oma.equipment')]\n")
        result = check_menu_exists("db", "Equipment", action_model="oma.equipment")
    assert result == (True, "menu 'Equipment' genuinely exists in the real registry, matching every claimed detail")
    print("PASS: a menu with a real window action correctly resolves its res_model")


def test_a_sql_error_still_degrades_to_none_never_a_false_guess():
    with patch("tools_odoo.spot_check._run_in_container") as mock_run:
        mock_run.return_value = _fake_proc(stdout="", returncode=1, stderr="some other genuine SQL error")
        result = check_menu_exists("db", "Equipment")
    assert result is None, "any remaining genuine failure must still degrade to None, never a guessed answer"
    print("PASS: a genuine (different) SQL/tooling failure still degrades to None, never guessed")


if __name__ == "__main__":
    test_query_parses_the_reference_field_and_joins_ir_act_window_not_ir_actions()
    test_a_real_row_with_no_action_still_resolves_correctly()
    test_a_real_row_with_a_window_action_resolves_the_real_model()
    test_a_sql_error_still_degrades_to_none_never_a_false_guess()
    print("\nALL CHECK-MENU-EXISTS-ACTION-REFERENCE-FIELD TESTS PASSED")
