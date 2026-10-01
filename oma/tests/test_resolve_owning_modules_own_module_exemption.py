"""Phase 20 Area 2 (2026-07-20, UPDATE 25): unit test for
tools_odoo/module_dev/toolchain.py's resolve_owning_modules() new
own_module_name exemption parameter -- verifies the SQL it generates,
not a live DB call (that part is already covered by the many live
reproductions documented in this investigation).

Real bug this fixes: found live on #44, pass 11 -- a decomposed task's
constraint 2 referenced `ref="model_asset_registry"`, a model an
EARLIER constraint of the SAME task (same reused module) genuinely
created. resolve_owning_modules()'s blanket 'oma_*' exclusion (added
2026-07-16 for a DIFFERENT problem: cross-task ir_model_data
contamination) also blocked this genuinely legitimate same-task
self-reference, returning None and leaving
_autofix_xml_bare_model_ref_qualifies_owning_module() unable to
qualify it -- confirmed as the direct cause of #44 failing 5/5 rounds
identically even after fix 8 (which only covers refs to records
written via `<record>` in the SAME generation; an auto-generated model
xmlid isn't written that way).
"""

import os
import sys
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tools_odoo.module_dev.toolchain import resolve_owning_modules


def test_sql_exempts_own_module_name_from_the_oma_exclusion():
    captured = {}

    def fake_run_in_container(cmd, timeout=90):
        captured["cmd"] = cmd
        result = MagicMock()
        result.returncode = 0
        result.stdout = "model_asset_registry|oma_create_a_small_new_6f9ecc20"
        return result

    with patch("tools_odoo.module_dev.toolchain._run_in_container", side_effect=fake_run_in_container):
        result = resolve_owning_modules(
            ["model_asset_registry"], "odoo16_dev", own_module_name="oma_create_a_small_new_6f9ecc20",
        )

    assert result == {"model_asset_registry": "oma_create_a_small_new_6f9ecc20"}
    import base64
    decoded = base64.b64decode(captured["cmd"].split("|")[0].replace("echo ", "").strip()).decode()
    assert "own_module = 'oma_create_a_small_new_6f9ecc20'" in decoded
    assert "OR module = %s" in decoded
    print("PASS: own_module_name is exempted from the blanket oma_* exclusion and correctly resolves")


def test_sql_still_excludes_other_oma_modules_when_own_module_name_not_given():
    captured = {}

    def fake_run_in_container(cmd, timeout=90):
        captured["cmd"] = cmd
        result = MagicMock()
        result.returncode = 0
        result.stdout = "model_asset_registry|"
        return result

    with patch("tools_odoo.module_dev.toolchain._run_in_container", side_effect=fake_run_in_container):
        result = resolve_owning_modules(["model_asset_registry"], "odoo16_dev")

    assert result == {"model_asset_registry": None}
    import base64
    decoded = base64.b64decode(captured["cmd"].split("|")[0].replace("echo ", "").strip()).decode()
    assert "own_module = None" in decoded
    print("PASS: with no own_module_name given, behavior is unchanged (still excludes all oma_* modules)")


if __name__ == "__main__":
    test_sql_exempts_own_module_name_from_the_oma_exclusion()
    test_sql_still_excludes_other_oma_modules_when_own_module_name_not_given()
    print("\nALL RESOLVE-OWNING-MODULES OWN-MODULE-EXEMPTION TESTS PASSED")
