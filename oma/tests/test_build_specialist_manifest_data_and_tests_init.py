"""Phase 29 (2026-07-29): unit tests for two real, confirmed live bugs
found on the school_student task's own automated_tests round:

1. _autofix_restore_dropped_manifest_data_reference() -- a scoped edit
   using replace_manifest_fields to add tests/*.py silently dropped
   'data/demo_data.xml' from the manifest's own data list, even though
   demo_data.xml itself was still being carried forward unchanged and
   the demo_data constraint was already satisfied. Same class of bug as
   the earlier security_xml/views_xml content-preservation fixes, but
   for the manifest's data list this time.

2. _autofix_ensure_tests_init_imports_every_test_module() -- a scoped
   edit wrote tests/test_student.py directly with no tests/__init__.py
   at all, so Odoo's test runner (which discovers tests exclusively via
   __init__.py imports) silently never ran it.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from specialists.build.specialist import (
    GeneratedModuleFiles,
    ManifestFields,
    _autofix_ensure_tests_init_imports_every_test_module,
    _autofix_restore_dropped_manifest_data_reference,
)

_MANIFEST = ManifestFields(
    name="x", version="1.0.0", category="Education", summary="x", author="x", depends=["base"],
    data=["views/views.xml", "security/security.xml", "security/ir.model.access.csv"],
)
_OLD_MANIFEST_PY_WITH_DEMO_DATA = str({
    "name": "x", "version": "1.0.0", "category": "Education", "summary": "x", "description": "",
    "author": "x", "depends": ["base"],
    "data": ["views/views.xml", "security/security.xml", "security/ir.model.access.csv", "data/demo_data.xml"],
    "installable": True, "auto_install": False, "license": "LGPL-3",
})


def test_restores_the_real_dropped_demo_data_reference():
    generated = GeneratedModuleFiles(
        manifest_fields=_MANIFEST.model_copy(deep=True), models_py="", security_csv="x",
        security_xml="<odoo/>", views_xml="<odoo/>",
        extra_data_files={"data/demo_data.xml": "<odoo/>"}, notes="",
    )
    _autofix_restore_dropped_manifest_data_reference(generated, _OLD_MANIFEST_PY_WITH_DEMO_DATA)
    assert "data/demo_data.xml" in generated.manifest_fields.data
    print("PASS: the real, confirmed live dropped demo_data.xml manifest reference is restored")


def test_never_restores_a_reference_with_no_real_backing_content():
    generated = GeneratedModuleFiles(
        manifest_fields=_MANIFEST.model_copy(deep=True), models_py="", security_csv="x",
        security_xml="<odoo/>", views_xml="<odoo/>", extra_data_files=None, notes="",
    )
    _autofix_restore_dropped_manifest_data_reference(generated, _OLD_MANIFEST_PY_WITH_DEMO_DATA)
    assert "data/demo_data.xml" not in generated.manifest_fields.data, (
        "must never restore a reference whose backing content genuinely doesn't exist this round"
    )
    print("PASS: never restores a reference with no real backing content this round")


def test_never_touches_a_deliberate_removal_the_prior_manifest_never_had():
    generated = GeneratedModuleFiles(
        manifest_fields=_MANIFEST.model_copy(deep=True), models_py="", security_csv="x",
        security_xml="<odoo/>", views_xml="<odoo/>", notes="",
    )
    old_manifest_no_demo = str({
        "name": "x", "data": ["views/views.xml", "security/security.xml", "security/ir.model.access.csv"],
    })
    before = list(generated.manifest_fields.data)
    _autofix_restore_dropped_manifest_data_reference(generated, old_manifest_no_demo)
    assert generated.manifest_fields.data == before
    print("PASS: never adds a reference the prior manifest never had either")


def test_adds_the_real_missing_tests_init():
    generated = GeneratedModuleFiles(
        manifest_fields=_MANIFEST.model_copy(deep=True), models_py="", security_csv="x",
        tests_py={"tests/test_student.py": "content"}, notes="",
    )
    _autofix_ensure_tests_init_imports_every_test_module(generated)
    assert generated.tests_py["tests/__init__.py"] == "from . import test_student\n"
    print("PASS: the real, confirmed live missing tests/__init__.py is added")


def test_never_touches_an_already_correct_init():
    generated = GeneratedModuleFiles(
        manifest_fields=_MANIFEST.model_copy(deep=True), models_py="", security_csv="x",
        tests_py={
            "tests/test_student.py": "content",
            "tests/__init__.py": "from . import test_student\n",
        }, notes="",
    )
    before = dict(generated.tests_py)
    _autofix_ensure_tests_init_imports_every_test_module(generated)
    assert generated.tests_py == before
    print("PASS: never touches an already-correct tests/__init__.py")


def test_no_op_when_no_tests_py_at_all():
    generated = GeneratedModuleFiles(
        manifest_fields=_MANIFEST.model_copy(deep=True), models_py="", security_csv="x", notes="",
    )
    _autofix_ensure_tests_init_imports_every_test_module(generated)
    assert not generated.tests_py
    print("PASS: no-op when this round has no tests_py at all")


if __name__ == "__main__":
    test_restores_the_real_dropped_demo_data_reference()
    test_never_restores_a_reference_with_no_real_backing_content()
    test_never_touches_a_deliberate_removal_the_prior_manifest_never_had()
    test_adds_the_real_missing_tests_init()
    test_never_touches_an_already_correct_init()
    test_no_op_when_no_tests_py_at_all()
    print("\nALL MANIFEST-DATA-AND-TESTS-INIT TESTS PASSED")
