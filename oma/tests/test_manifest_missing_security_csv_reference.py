"""P11 finding #7, mechanism 1 (docs/planning/PHASE30_P11_ADDITIONS_FROM_P7_TIER3_2026-07-31.md
§1.7, real task 4acf4a9d-4d96-46e0-a533-b971b3f2dfc8): tests for
_autofix_manifest_missing_security_csv_data_reference() -- the same "third case" fix
_autofix_manifest_missing_views_reference() already has for views_xml, now for security_csv.
Pure, synchronous, zero LLM/GPU calls.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from specialists.build.specialist import (
    GeneratedModuleFiles,
    ManifestFields,
    _autofix_manifest_missing_security_csv_data_reference,
)


def _manifest(data):
    return ManifestFields(
        name="x", version="1.0", category="Uncategorized", summary="", author="", depends=["base"], data=data,
    )


def test_adds_the_missing_reference_when_security_csv_has_real_content():
    generated = GeneratedModuleFiles(
        manifest_fields=_manifest([]),
        models_py="", views_xml="", notes="",
        security_csv="id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink\n"
                     "access_x,access.x,model_x,base.group_user,1,1,1,1\n",
    )
    _autofix_manifest_missing_security_csv_data_reference(generated)
    assert "security/ir.model.access.csv" in generated.manifest_fields.data
    print("PASS: a real, written security_csv with no manifest reference at all gets one added")


def test_is_a_no_op_when_reference_already_present():
    generated = GeneratedModuleFiles(
        manifest_fields=_manifest(["security/ir.model.access.csv"]),
        models_py="", views_xml="", notes="",
        security_csv="id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink\n",
    )
    _autofix_manifest_missing_security_csv_data_reference(generated)
    assert generated.manifest_fields.data == ["security/ir.model.access.csv"]
    print("PASS: an already-correct reference is left untouched, never duplicated")


def test_is_a_no_op_when_security_csv_is_empty():
    generated = GeneratedModuleFiles(
        manifest_fields=_manifest([]), models_py="", views_xml="", notes="", security_csv="",
    )
    _autofix_manifest_missing_security_csv_data_reference(generated)
    assert generated.manifest_fields.data == []
    print("PASS: an empty security_csv never gets a manifest reference invented for it")


def test_never_removes_or_reorders_other_manifest_data_entries():
    generated = GeneratedModuleFiles(
        manifest_fields=_manifest(["views/views.xml", "security/security.xml"]),
        models_py="", views_xml="", notes="",
        security_csv="id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink\n"
                     "access_x,access.x,model_x,base.group_user,1,1,1,1\n",
    )
    _autofix_manifest_missing_security_csv_data_reference(generated)
    assert generated.manifest_fields.data == [
        "views/views.xml", "security/security.xml", "security/ir.model.access.csv",
    ]
    print("PASS: existing manifest data entries are preserved in order, only the missing one is appended")


if __name__ == "__main__":
    test_adds_the_missing_reference_when_security_csv_has_real_content()
    test_is_a_no_op_when_reference_already_present()
    test_is_a_no_op_when_security_csv_is_empty()
    test_never_removes_or_reorders_other_manifest_data_entries()
    print("\nALL MANIFEST-MISSING-SECURITY-CSV-REFERENCE TESTS PASSED")
