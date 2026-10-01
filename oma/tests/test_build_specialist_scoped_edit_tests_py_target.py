"""Phase 29 (2026-07-29): unit test for the real, confirmed live gap in
_apply_scoped_edits() -- the school_student task's own final round
(automated_tests) was permanently blocked: a scoped edit targeting
'tests/test_student.py' was unconditionally rejected as "not one of
the fixed core files," even though GeneratedModuleFiles.tests_py (Phase
28A) already exists specifically to hold automated-test content -- the
scoped-edit path (used for round 2+) was simply never taught the same
'tests/<name>.py' shape the round-1 generation path already supports.

Two real bugs, both fixed here:
  1. _apply_scoped_edits() rejected any 'tests/*.py' target outright.
  2. Even if allowed through, the reconstruction logic in
     _run_scoped_edit_round() (or its equivalent) dumped everything
     outside the fixed core files into `extra_data_files` -- correct
     for a real data XML/CSV file (which other autofixes expect to
     ALSO appear in manifest_fields.data), but wrong for a Python test
     file, which Odoo auto-discovers via tests/__init__.py imports.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from specialists.build.specialist import (
    GeneratedModuleEdit,
    _apply_scoped_edits,
    _split_applied_files_into_tests_and_extra_data,
)

_KNOWN_FIXED_PATHS = {
    "__manifest__.py", "models/models.py", "views/views.xml",
    "security/ir.model.access.csv", "security/security.xml",
}

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


def test_scoped_edit_can_target_a_tests_py_file():
    edit = GeneratedModuleEdit(file="tests/test_student.py", operation="replace_file", content="# real test content\n")
    result = _apply_scoped_edits(dict(_PRIOR_FILES), [edit])
    assert result.get("tests/test_student.py") == "# real test content\n"
    print("PASS: a scoped edit targeting tests/*.py is no longer rejected")


def test_scoped_edit_can_target_tests_init_py():
    edit = GeneratedModuleEdit(file="tests/__init__.py", operation="replace_file", content="from . import test_student\n")
    result = _apply_scoped_edits(dict(_PRIOR_FILES), [edit])
    assert result.get("tests/__init__.py") == "from . import test_student\n"
    print("PASS: tests/__init__.py is also accepted (same 'tests/<name>.py' shape)")


def test_still_rejects_a_genuinely_arbitrary_path():
    edit = GeneratedModuleEdit(file="whatever/random.py", operation="replace_file", content="x")
    try:
        _apply_scoped_edits(dict(_PRIOR_FILES), [edit])
        raised = False
    except ValueError:
        raised = True
    assert raised, "a genuinely arbitrary, non-core, non-data, non-test path must still be rejected"
    print("PASS: a genuinely arbitrary path outside every known shape is still rejected")


def test_stray_bare_python_file_carried_forward_never_reaches_extra_data_files():
    """Real, confirmed bug found live (2026-07-29, same task, follow-up
    round): a bare-named 'test_student.py' left over from BEFORE
    tests/<name>.py support existed kept getting silently carried
    forward by _apply_scoped_edits() (never explicitly targeted by any
    edit) and re-entering extra_data_files every single round --
    dragging real coverage checking down to a false, repeated "unclaimed
    gap" failure on 62 genuinely dead statements no test runner ever
    touches. The fix must exclude it at the reconstruction source, not
    rely only on a downstream cleanup autofix.
    """
    edit = GeneratedModuleEdit(file="tests/test_student.py", operation="replace_file", content="updated real content")
    prior_files_with_stray = dict(_PRIOR_FILES)
    prior_files_with_stray["test_student.py"] = "STRAY OLD CONTENT, never targeted by any edit"
    prior_files_with_stray["tests/test_student.py"] = "old real content"

    applied = _apply_scoped_edits(prior_files_with_stray, [edit])
    tests_py, extra_data_files = _split_applied_files_into_tests_and_extra_data(applied, _KNOWN_FIXED_PATHS)

    assert extra_data_files is None or "test_student.py" not in extra_data_files, (
        "the stray bare test_student.py must never re-enter extra_data_files"
    )
    assert tests_py == {"tests/test_student.py": "updated real content"}
    print("PASS: a stray bare .py file carried forward from before tests/<name>.py support existed "
          "never re-enters extra_data_files, closing the real repeated coverage-pollution bug")


if __name__ == "__main__":
    test_scoped_edit_can_target_a_tests_py_file()
    test_scoped_edit_can_target_tests_init_py()
    test_still_rejects_a_genuinely_arbitrary_path()
    test_stray_bare_python_file_carried_forward_never_reaches_extra_data_files()
    print("\nALL SCOPED-EDIT TESTS_PY TARGET TESTS PASSED")
