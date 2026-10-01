"""Phase 29 (2026-07-29): unit test for
_autofix_strip_misplaced_python_file_from_extra_data() -- the real,
confirmed live root cause behind a persistent, repeated coverage
failure on the school_student task's own automated_tests round: a
stray, bare-named 'test_student.py' sat in `extra_data_files` (module
root, never imported by anything, never discovered by Odoo's own test
runner) alongside the correct `tests/test_student.py`, silently carried
forward from an earlier round and dragging coverage down to 0% on 62
genuinely dead statements every single round since.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from specialists.build.specialist import (
    GeneratedModuleFiles,
    ManifestFields,
    _autofix_strip_misplaced_python_file_from_extra_data,
)

_MANIFEST = ManifestFields(
    name="x", version="1.0.0", category="Education", summary="x", author="x", depends=["base"], data=[],
)


def test_strips_the_real_live_stray_root_test_file():
    generated = GeneratedModuleFiles(
        manifest_fields=_MANIFEST.model_copy(deep=True), models_py="", security_csv="x",
        extra_data_files={"test_student.py": "stray content", "data/demo_data.xml": "<odoo/>"},
        tests_py={"tests/test_student.py": "real content", "tests/__init__.py": "from . import test_student\n"},
        notes="",
    )
    _autofix_strip_misplaced_python_file_from_extra_data(generated)
    assert "test_student.py" not in generated.extra_data_files
    assert "data/demo_data.xml" in generated.extra_data_files
    print("PASS: the real, confirmed live stray root-level test_student.py is stripped")


def test_never_touches_the_real_tests_py_dict():
    generated = GeneratedModuleFiles(
        manifest_fields=_MANIFEST.model_copy(deep=True), models_py="", security_csv="x",
        extra_data_files={"test_student.py": "stray"},
        tests_py={"tests/test_student.py": "real content", "tests/__init__.py": "from . import test_student\n"},
        notes="",
    )
    _autofix_strip_misplaced_python_file_from_extra_data(generated)
    assert generated.tests_py == {
        "tests/test_student.py": "real content", "tests/__init__.py": "from . import test_student\n",
    }
    print("PASS: the real, correctly-placed tests_py dict is never touched")


def test_never_touches_legitimate_xml_data_files():
    generated = GeneratedModuleFiles(
        manifest_fields=_MANIFEST.model_copy(deep=True), models_py="", security_csv="x",
        extra_data_files={"data/cron.xml": "<odoo/>", "data/mail_template.xml": "<odoo/>"},
        notes="",
    )
    before = dict(generated.extra_data_files)
    _autofix_strip_misplaced_python_file_from_extra_data(generated)
    assert generated.extra_data_files == before
    print("PASS: legitimate .xml extra_data_files entries are never touched")


def test_no_op_when_extra_data_files_is_empty_or_none():
    generated = GeneratedModuleFiles(
        manifest_fields=_MANIFEST.model_copy(deep=True), models_py="", security_csv="x", notes="",
    )
    _autofix_strip_misplaced_python_file_from_extra_data(generated)
    assert not generated.extra_data_files
    print("PASS: no-op, no crash when extra_data_files is None")


def test_sets_extra_data_files_to_none_when_only_stray_content_remains():
    generated = GeneratedModuleFiles(
        manifest_fields=_MANIFEST.model_copy(deep=True), models_py="", security_csv="x",
        extra_data_files={"test_student.py": "stray only"},
        notes="",
    )
    _autofix_strip_misplaced_python_file_from_extra_data(generated)
    assert generated.extra_data_files is None
    print("PASS: extra_data_files becomes None (not an empty dict) when nothing legitimate remains")


def test_re_prefixes_a_bare_key_directly_inside_tests_py():
    """Real, confirmed bug found live (2026-07-29, same task, follow-up
    round): the SAME class of bug, one level deeper -- a bare-named key
    ('test_student_duplicate.py') showed up directly INSIDE `tests_py`
    itself (not `extra_data_files`), written there by Build's own
    full-generation path rather than the scoped-edit reconstruction the
    other fixes cover. Real content, just filed under the wrong key --
    must be re-prefixed, never dropped.
    """
    generated = GeneratedModuleFiles(
        manifest_fields=_MANIFEST.model_copy(deep=True), models_py="", security_csv="x",
        tests_py={"tests/test_student.py": "real", "test_student_duplicate.py": "also real, wrong key"},
        notes="",
    )
    _autofix_strip_misplaced_python_file_from_extra_data(generated)
    assert generated.tests_py["tests/test_student_duplicate.py"] == "also real, wrong key"
    assert "test_student_duplicate.py" not in generated.tests_py
    print("PASS: a bare-named key directly inside tests_py is re-prefixed, not dropped")


def test_bare_stray_key_colliding_with_real_content_never_overwrites_it():
    """Real, confirmed live shape: a bare 'test_student.py' with STALE
    content coexisting alongside the correct 'tests/test_student.py'
    with the REAL, current content -- the stale one must never win.
    """
    generated = GeneratedModuleFiles(
        manifest_fields=_MANIFEST.model_copy(deep=True), models_py="", security_csv="x",
        tests_py={"test_student.py": "STALE OLD CONTENT", "tests/test_student.py": "real current content"},
        notes="",
    )
    _autofix_strip_misplaced_python_file_from_extra_data(generated)
    assert generated.tests_py == {"tests/test_student.py": "real current content"}
    print("PASS: a bare stray key colliding with real, correctly-keyed content is dropped, never overwrites it")


if __name__ == "__main__":
    test_strips_the_real_live_stray_root_test_file()
    test_never_touches_the_real_tests_py_dict()
    test_never_touches_legitimate_xml_data_files()
    test_no_op_when_extra_data_files_is_empty_or_none()
    test_sets_extra_data_files_to_none_when_only_stray_content_remains()
    test_re_prefixes_a_bare_key_directly_inside_tests_py()
    test_bare_stray_key_colliding_with_real_content_never_overwrites_it()
    print("\nALL STRIP-MISPLACED-PYTHON-FILE TESTS PASSED")
