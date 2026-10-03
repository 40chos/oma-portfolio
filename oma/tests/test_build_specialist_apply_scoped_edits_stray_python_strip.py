"""Phase 29 (2026-07-29): unit test for a real, confirmed live bug --
the THIRD occurrence, same task, same round shape, after two earlier
fixes each closed only ONE of the paths that could re-introduce a
stray bare-named 'test_student.py' (no 'tests/' prefix) into a fresh
round's content. This test targets the fix at `_apply_scoped_edits()`'s
own single entry point, the one place guaranteed to run for every
round regardless of which `prior_files` baseline (immediately-prior
round vs. an older "last validated" checkpoint) fed it.

A real, serious bug was caught and fixed WHILE building this exact
fix: the first version deleted any `.py` key not starting with
`models/`/`tests/`, which would have also deleted `__manifest__.py`
itself (also a bare `.py` file at the module root) -- caught before
deployment by writing this test first.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from specialists.build.specialist import (
    GeneratedModuleEdit,
    ManifestFields,
    _apply_scoped_edits,
    _render_manifest_py,
    _scoped_edit_missing_required_files,
)

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


def test_strips_a_stray_bare_python_file_at_the_true_entry_point():
    prior_files = dict(_PRIOR_FILES)
    prior_files["test_student.py"] = "STRAY OLD CONTENT from way back"
    prior_files["tests/test_student.py"] = "real content"

    edit = GeneratedModuleEdit(file="tests/test_student.py", operation="replace_file", content="updated real content")
    applied = _apply_scoped_edits(prior_files, [edit])

    assert "test_student.py" not in applied, "the stray bare .py file must be stripped, regardless of which prior_files baseline fed this call"
    assert applied["tests/test_student.py"] == "updated real content"
    print("PASS: the real, confirmed live stray bare test_student.py is stripped at the true entry point")


def test_never_deletes_the_manifest_itself():
    """The real, serious bug caught while building this exact fix: a
    naive '.py, not under models/ or tests/' filter also matches
    '__manifest__.py' (also a bare .py file at the module root).
    """
    prior_files = dict(_PRIOR_FILES)
    applied = _apply_scoped_edits(prior_files, [])
    assert applied["__manifest__.py"] == _PRIOR_FILES["__manifest__.py"], (
        "__manifest__.py must NEVER be stripped -- it is also a bare .py file at the module root, "
        "the exact same shape as the genuinely stray file, and must be excluded explicitly"
    )
    print("PASS: __manifest__.py survives untouched -- the real bug caught while building this fix")


def test_never_deletes_models_py_or_tests_content():
    prior_files = dict(_PRIOR_FILES)
    prior_files["tests/test_student.py"] = "real test content"
    applied = _apply_scoped_edits(prior_files, [])
    assert applied["models/models.py"] == _PRIOR_FILES["models/models.py"]
    assert applied["tests/test_student.py"] == "real test content"
    print("PASS: models/models.py and tests/*.py content both survive untouched")


def test_no_op_when_no_stray_file_exists_at_all():
    prior_files = dict(_PRIOR_FILES)
    applied = _apply_scoped_edits(prior_files, [])
    assert set(applied.keys()) == set(prior_files.keys())
    print("PASS: no-op when there was never a stray file to begin with")


# --- _scoped_edit_missing_required_files (real task010 shape) ---
# Real, confirmed bug found live (2026-08-03, task010's own repeated real sandbox install crash):
# a scoped edit's own final manifest still referenced 'security/security.xml' in its data list,
# but the file itself never survived into `applied` -- Odoo's own install crashed loading a data
# file that doesn't exist. Neither prior_files nor a fixed required-file tuple caught this before.

def test_flags_security_xml_referenced_in_manifest_but_missing_from_applied():
    manifest_fields = ManifestFields(
        name="oma_x", version="16.0.1.0.0", category="Hidden", summary="x", author="x",
        depends=["base", "project_fieldjob"], data=["security/security.xml", "security/ir.model.access.csv"],
    )
    prior_files = dict(_PRIOR_FILES)
    prior_files["__manifest__.py"] = _render_manifest_py(manifest_fields)
    # security/security.xml was never in prior_files at all -- this round's own first attempt
    # already lost it before ever committing (the real task010 shape).
    applied = _apply_scoped_edits(prior_files, [])
    missing = _scoped_edit_missing_required_files(prior_files, applied)
    assert missing == ["security/security.xml"], missing
    print("PASS: a manifest still referencing security.xml, with the file itself missing, is flagged")


def test_flags_security_xml_present_in_prior_files_but_dropped():
    prior_files = dict(_PRIOR_FILES)
    prior_files["security/security.xml"] = "<odoo><record id='g' model='res.groups'/></odoo>"
    # Simulate a broken edit result that somehow drops it despite _apply_scoped_edits' own
    # carry-forward guarantee -- directly exercising the check function's own contract.
    applied = dict(prior_files)
    del applied["security/security.xml"]
    missing = _scoped_edit_missing_required_files(prior_files, applied)
    assert missing == ["security/security.xml"], missing
    print("PASS: security.xml present in prior_files but absent from applied is flagged")


def test_never_flags_a_module_that_legitimately_has_no_security_xml():
    manifest_fields = ManifestFields(
        name="oma_y", version="16.0.1.0.0", category="Tools", summary="x", author="x",
        depends=["base"], data=["views/views.xml"],
    )
    prior_files = dict(_PRIOR_FILES)
    prior_files["__manifest__.py"] = _render_manifest_py(manifest_fields)
    prior_files["views/views.xml"] = "<odoo></odoo>"
    applied = _apply_scoped_edits(prior_files, [])
    missing = _scoped_edit_missing_required_files(prior_files, applied)
    assert missing == [], missing
    print("PASS: a module that legitimately never defines security.xml is never falsely flagged")


def test_never_flags_when_security_xml_correctly_survives():
    manifest_fields = ManifestFields(
        name="oma_x", version="16.0.1.0.0", category="Hidden", summary="x", author="x",
        depends=["base", "project_fieldjob"], data=["security/security.xml", "security/ir.model.access.csv"],
    )
    prior_files = dict(_PRIOR_FILES)
    prior_files["__manifest__.py"] = _render_manifest_py(manifest_fields)
    prior_files["security/security.xml"] = "<odoo><record id='g' model='res.groups'/></odoo>"
    applied = _apply_scoped_edits(prior_files, [])
    missing = _scoped_edit_missing_required_files(prior_files, applied)
    assert missing == [], missing
    print("PASS: correctly-carried-forward security.xml is never falsely flagged")


if __name__ == "__main__":
    test_strips_a_stray_bare_python_file_at_the_true_entry_point()
    test_never_deletes_the_manifest_itself()
    test_never_deletes_models_py_or_tests_content()
    test_no_op_when_no_stray_file_exists_at_all()
    test_flags_security_xml_referenced_in_manifest_but_missing_from_applied()
    test_flags_security_xml_present_in_prior_files_but_dropped()
    test_never_flags_a_module_that_legitimately_has_no_security_xml()
    test_never_flags_when_security_xml_correctly_survives()
    print("\nALL APPLY-SCOPED-EDITS STRAY-PYTHON-STRIP TESTS PASSED")
