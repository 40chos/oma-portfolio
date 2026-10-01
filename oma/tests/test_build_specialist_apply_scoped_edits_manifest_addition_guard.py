"""P12 Tier S item 5 (docs/planning/PHASE30_ARCH_SYNTHESIS_CRITIQUE_FINAL_2026-07-30.md §4,
promoted from Tier A): _apply_scoped_edits()'s existing manifest drop-guard
(`still_real_files = dropped - new_data`... check inside the 'replace_manifest_fields'
branch) only diffs `prior_data - new_data` -- structurally blind to a brand-new
`data/foo.xml` file written via `replace_file` in the SAME edit batch that the accompanying
`replace_manifest_fields` edit simply never references, since a file that was never in
`prior_data` can never appear in `dropped`. Confirmed live-reachable: `_apply_scoped_edits`
is the path every round-2+ edit goes through (specialists/build/specialist.py:9430).

This test targets the new reconciliation step added right before `_apply_scoped_edits`
returns: any `data/*.xml` key present in the final applied file set that isn't listed in
the final manifest's `data` gets appended automatically (mirrors
`_autofix_ensure_extra_data_files_are_referenced_in_manifest`'s own silent-autofix posture,
just applied directly to the scoped-edit result instead of relying on that later step to
catch it). Zero LLM/GPU calls -- pure deterministic function, direct construction only.
"""

import ast
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from specialists.build.specialist import GeneratedModuleEdit, ManifestFields, _apply_scoped_edits

_PRIOR_FILES = {
    "__manifest__.py": (
        "{'name': 'x', 'version': '1.0.0', 'category': 'Tools', 'summary': 'x', 'description': '', "
        "'author': 'x', 'depends': ['base'], 'data': [], 'installable': True, 'auto_install': False, "
        "'license': 'LGPL-3'}"
    ),
    "models/models.py": "from odoo import models\n",
    "security/ir.model.access.csv": (
        "id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink\n"
    ),
}


def _manifest_fields(**overrides) -> ManifestFields:
    base = dict(
        name="x", version="1.0.0", category="Tools", summary="x", description="",
        author="x", depends=["base"], data=[], installable=True, auto_install=False,
    )
    base.update(overrides)
    return ManifestFields(**base)


def test_brand_new_data_file_forgotten_in_same_batch_manifest_edit_gets_appended():
    """The exact confirmed-live gap: a replace_file for a NEW data/demo.xml, combined with
    a replace_manifest_fields edit whose data list forgets it entirely (data=[] here, not
    even a partial list) -- the old drop-guard's `dropped = prior_data - new_data` is empty
    (prior_data was also empty), so it never fires. The new addition-direction guard must
    still catch it.
    """
    edits = [
        GeneratedModuleEdit(file="data/demo.xml", operation="replace_file", content="<odoo/>"),
        GeneratedModuleEdit(
            file="__manifest__.py", operation="replace_manifest_fields",
            manifest_fields=_manifest_fields(data=[]),
        ),
    ]
    applied = _apply_scoped_edits(dict(_PRIOR_FILES), edits)
    final_data = ast.literal_eval(applied["__manifest__.py"])["data"]
    assert "data/demo.xml" in final_data, (
        f"data/demo.xml was written this round but never referenced in the manifest's data "
        f"list -- Odoo silently never loads it -- final data list was {final_data!r}"
    )
    print("PASS: a brand-new data file forgotten in the same batch's manifest edit gets appended automatically")


def test_existing_manifest_reference_is_not_duplicated():
    edits = [
        GeneratedModuleEdit(file="data/demo.xml", operation="replace_file", content="<odoo/>"),
        GeneratedModuleEdit(
            file="__manifest__.py", operation="replace_manifest_fields",
            manifest_fields=_manifest_fields(data=["data/demo.xml"]),
        ),
    ]
    applied = _apply_scoped_edits(dict(_PRIOR_FILES), edits)
    final_data = ast.literal_eval(applied["__manifest__.py"])["data"]
    assert final_data.count("data/demo.xml") == 1
    print("PASS: a data file already correctly referenced is not duplicated by the new guard")


def test_no_data_files_at_all_is_a_pure_no_op():
    applied = _apply_scoped_edits(dict(_PRIOR_FILES), [])
    final_data = ast.literal_eval(applied["__manifest__.py"])["data"]
    assert final_data == []
    print("PASS: no-op when there are no data/*.xml files in play at all")


def test_carried_forward_prior_data_file_not_touched_this_round_stays_referenced():
    """A data/*.xml file that already existed (carried forward in prior_files, untouched by
    this round's edits) and was already correctly referenced must survive unchanged -- this
    guard must not interfere with the existing regression-direction drop-guard's own territory.
    """
    prior_files = dict(_PRIOR_FILES)
    prior_files["__manifest__.py"] = (
        "{'name': 'x', 'version': '1.0.0', 'category': 'Tools', 'summary': 'x', 'description': '', "
        "'author': 'x', 'depends': ['base'], 'data': ['data/existing.xml'], 'installable': True, "
        "'auto_install': False, 'license': 'LGPL-3'}"
    )
    prior_files["data/existing.xml"] = "<odoo/>"
    edits = [
        GeneratedModuleEdit(
            file="__manifest__.py", operation="replace_manifest_fields",
            manifest_fields=_manifest_fields(data=["data/existing.xml"]),
        ),
    ]
    applied = _apply_scoped_edits(prior_files, edits)
    final_data = ast.literal_eval(applied["__manifest__.py"])["data"]
    assert final_data == ["data/existing.xml"]
    print("PASS: an already-referenced, untouched prior data file is left exactly as-is")


if __name__ == "__main__":
    test_brand_new_data_file_forgotten_in_same_batch_manifest_edit_gets_appended()
    test_existing_manifest_reference_is_not_duplicated()
    test_no_data_files_at_all_is_a_pure_no_op()
    test_carried_forward_prior_data_file_not_touched_this_round_stays_referenced()
    print("\nALL APPLY-SCOPED-EDITS MANIFEST-ADDITION-GUARD TESTS PASSED")
