"""P11 fifth pass item 182 (docs/planning/PHASE30_SECOND_PASS_FINAL_CONSOLIDATED_2026-07-30.md
§1.3): tests for _validate_xml_record_field_value_within_char_size_limit().
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from specialists.build.specialist import (
    GeneratedModuleFiles,
    ManifestFields,
    _validate_xml_record_field_value_within_char_size_limit,
)

_MANIFEST = ManifestFields(
    name="x", version="16.0.1.0.0", category="Tools", summary="x", author="x", depends=["base"], data=[],
)


def _gen(models_py="", views_xml=None, extra_data_files=None) -> GeneratedModuleFiles:
    return GeneratedModuleFiles(
        manifest_fields=_MANIFEST, models_py=models_py, views_xml=views_xml,
        security_csv="x", extra_data_files=extra_data_files, notes="",
    )


def _raises(fn, *args):
    try:
        fn(*args)
        return None
    except ValueError as exc:
        return str(exc)


def test_raises_when_literal_value_exceeds_declared_size():
    models_py = "class X:\n    _name = 'x.model'\n    code = fields.Char(size=5)\n"
    views_xml = '<record id="r1" model="x.model"><field name="code">too-long-value</field></record>'
    assert _raises(_validate_xml_record_field_value_within_char_size_limit, _gen(models_py, views_xml))
    print("PASS item182 raises")


def test_never_raises_when_within_size():
    models_py = "class X:\n    _name = 'x.model'\n    code = fields.Char(size=20)\n"
    views_xml = '<record id="r1" model="x.model"><field name="code">short</field></record>'
    assert _raises(_validate_xml_record_field_value_within_char_size_limit, _gen(models_py, views_xml)) is None
    print("PASS item182 negative")


def test_never_raises_for_a_field_with_no_size_kwarg():
    models_py = "class X:\n    _name = 'x.model'\n    code = fields.Char()\n"
    views_xml = '<record id="r1" model="x.model"><field name="code">a very long literal value here</field></record>'
    assert _raises(_validate_xml_record_field_value_within_char_size_limit, _gen(models_py, views_xml)) is None
    print("PASS item182 no size kwarg negative")


def test_never_raises_for_an_eval_expression_value():
    models_py = "class X:\n    _name = 'x.model'\n    code = fields.Char(size=5)\n"
    views_xml = '<record id="r1" model="x.model"><field name="code" eval="\'too-long-value\'"/></record>'
    assert _raises(_validate_xml_record_field_value_within_char_size_limit, _gen(models_py, views_xml)) is None
    print("PASS item182 eval-expression negative")


def test_reaches_extra_data_files():
    models_py = "class X:\n    _name = 'x.model'\n    code = fields.Char(size=5)\n"
    extra = {"data/x.xml": '<record id="r1" model="x.model"><field name="code">too-long-value</field></record>'}
    assert _raises(_validate_xml_record_field_value_within_char_size_limit, _gen(models_py, extra_data_files=extra))
    print("PASS item182 reaches extra_data_files")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
    print(f"\nALL {len(fns)} P11 FIFTH-PASS ITEM 182 TESTS PASSED")
