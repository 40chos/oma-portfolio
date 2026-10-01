"""P11 fifth pass items 181, 185
(docs/planning/PHASE30_SECOND_PASS_FINAL_CONSOLIDATED_2026-07-30.md §1.3): new validators.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from specialists.build.specialist import (
    GeneratedModuleFiles,
    ManifestFields,
    _validate_no_python_keyword_field_names,
    _validate_security_csv_row_column_count_matches_header,
)

_MANIFEST = ManifestFields(
    name="x", version="16.0.1.0.0", category="Tools", summary="x", author="x", depends=["base"], data=[],
)


def _gen(models_py="", security_csv="x") -> GeneratedModuleFiles:
    return GeneratedModuleFiles(manifest_fields=_MANIFEST, models_py=models_py, security_csv=security_csv, notes="")


def _raises(fn, *args):
    try:
        fn(*args)
        return None
    except ValueError as exc:
        return str(exc)


# --- Item 181 ---
def test_item181_raises_on_reserved_keyword_field_name():
    models_py = "class X(models.Model):\n    _name = 'x.model'\n    class = fields.Char()\n"
    assert _raises(_validate_no_python_keyword_field_names, _gen(models_py))
    print("PASS item181")


def test_item181_never_raises_on_ordinary_field_names():
    models_py = "class X(models.Model):\n    _name = 'x.model'\n    class_name = fields.Char()\n"
    assert _raises(_validate_no_python_keyword_field_names, _gen(models_py)) is None
    print("PASS item181 negative")


def test_item181_never_raises_with_no_models_py():
    assert _raises(_validate_no_python_keyword_field_names, _gen("")) is None
    print("PASS item181 no-op on empty models_py")


# --- Item 185 ---
def test_item185_raises_on_a_ragged_row():
    csv = (
        "id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink\n"
        "access_x,x,model_x,base.group_user,1,1,1\n"  # missing one column
    )
    assert _raises(_validate_security_csv_row_column_count_matches_header, _gen(security_csv=csv))
    print("PASS item185")


def test_item185_never_raises_on_a_well_formed_csv():
    csv = (
        "id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink\n"
        "access_x,x,model_x,base.group_user,1,1,1,0\n"
    )
    assert _raises(_validate_security_csv_row_column_count_matches_header, _gen(security_csv=csv)) is None
    print("PASS item185 negative")


def test_item185_never_raises_on_header_only_csv():
    csv = "id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink\n"
    assert _raises(_validate_security_csv_row_column_count_matches_header, _gen(security_csv=csv)) is None
    print("PASS item185 header-only no-op")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
    print(f"\nALL {len(fns)} P11 FIFTH-PASS ITEMS 181/185 TESTS PASSED")
