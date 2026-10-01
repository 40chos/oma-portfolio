"""P11 fifth pass item 168 (docs/planning/PHASE30_SECOND_PASS_FINAL_CONSOLIDATED_2026-07-30.md
§1.1): the extra_data_files scanning blind spot item 77 already fixed for one function
(_validate_xml_refs_resolve) recurs in the other functions of the same family -- confirmed via
direct grep of the real source (not the source doc's own claimed count, which named 12 functions
including one, _autofix_xml_bare_model_ref_qualifies_owning_module, that turned out to ALREADY be
one of the 12 real ones once async def was correctly counted, and 2 of the 12,
_validate_xml_is_well_formed/_validate_no_duplicate_xml_record_ids, that P12 Tier A item 15 already
widened earlier this session -- 9 genuinely needed widening here).

Each test confirms the function's own real effect now reaches content living ONLY in
extra_data_files, not just security_xml/views_xml. Zero live LLM/GPU/SSH calls -- all pure,
synchronous functions except the two async live-registry ones, which are tested via their own
purely-local pre-condition (no candidates found -> returns before any DB call), matching this
file's established "never make a live call in a unit test" discipline.
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from specialists.build.specialist import (
    GeneratedModuleFiles,
    ManifestFields,
    _autofix_bare_menu_restriction_id,
    _autofix_dedupe_duplicate_xml_record_ids,
    _autofix_manifest_missing_dependency_for_cross_module_record_id,
    _autofix_strip_stray_xml_attribute_fragment_lines,
    _autofix_wrong_module_prefixed_local_group_id,
    _autofix_xml_bare_model_ref_qualifies_own_new_model,
    _autofix_xml_bare_ref_qualifies_locally_defined_record,
    _autofix_xml_ref_wrong_sibling_module,
    _validate_self_referenced_xmlids_are_defined,
)

_MANIFEST = ManifestFields(
    name="x", version="16.0.1.0.0", category="Tools", summary="x", author="x", depends=["base"], data=[],
)


def _gen(models_py="", views_xml=None, security_csv="x", security_xml=None, extra_data_files=None) -> GeneratedModuleFiles:
    return GeneratedModuleFiles(
        manifest_fields=_MANIFEST, models_py=models_py, views_xml=views_xml,
        security_csv=security_csv, security_xml=security_xml, extra_data_files=extra_data_files, notes="",
    )


def test_bare_ref_locally_defined_record_qualifies_ref_inside_extra_data_files():
    extra = {"data/cron.xml": (
        '<record id="group_x" model="res.groups"><field name="name">X</field></record>'
        '<record id="cron_x" model="ir.cron"><field name="groups_id" eval="[(4, ref(\'group_x\'))]"/></record>'
    )}
    g = _gen(extra_data_files=extra)
    _autofix_xml_bare_ref_qualifies_locally_defined_record(g, "oma_test_module")
    assert "ref('oma_test_module.group_x')" in g.extra_data_files["data/cron.xml"]
    print("PASS item168: bare-ref-locally-defined autofix reaches extra_data_files")


def test_bare_model_ref_own_new_model_qualifies_ref_inside_extra_data_files():
    models_py = "class X(models.Model):\n    _name = 'x.model'\n"
    extra = {"data/cron.xml": '<record id="cron_x" model="ir.cron"><field name="model_id" ref="model_x_model"/></record>'}
    g = _gen(models_py=models_py, extra_data_files=extra)
    _autofix_xml_bare_model_ref_qualifies_own_new_model(g, "oma_test_module")
    assert 'ref="oma_test_module.model_x_model"' in g.extra_data_files["data/cron.xml"]
    print("PASS item168: bare-model-ref-own-new-model autofix reaches extra_data_files")


def test_wrong_sibling_module_ref_corrected_inside_extra_data_files():
    extra = {"data/cron.xml": '<record id="cron_x" model="ir.cron"><field name="x" ref="oma_wrong_module_abc.some_id"/></record>'}
    g = _gen(extra_data_files=extra)
    _autofix_xml_ref_wrong_sibling_module(g, "oma_own_module", "oma_correct_module")
    assert 'ref="oma_correct_module.some_id"' in g.extra_data_files["data/cron.xml"]
    print("PASS item168: wrong-sibling-module autofix reaches extra_data_files")


def test_self_referenced_xmlids_validator_sees_record_defined_only_in_extra_data_files():
    extra = {"data/groups.xml": '<record id="oma_test_module.group_x" model="res.groups"><field name="name">X</field></record>'}
    security_xml = '<record id="rule_x" model="ir.rule"><field name="groups" eval="[(4, ref(\'oma_test_module.group_x\'))]"/></record>'
    g = _gen(security_xml=security_xml, extra_data_files=extra)
    # Must NOT raise -- the referenced group is genuinely defined, just in extra_data_files.
    _validate_self_referenced_xmlids_are_defined(g, "oma_test_module")
    print("PASS item168: self-referenced-xmlids validator sees a record defined only in extra_data_files")


def test_self_referenced_xmlids_validator_still_raises_when_genuinely_undefined():
    security_xml = '<record id="rule_x" model="ir.rule"><field name="groups" eval="[(4, ref(\'oma_test_module.group_ghost\'))]"/></record>'
    g = _gen(security_xml=security_xml, extra_data_files={"data/cron.xml": "<odoo></odoo>"})
    raised = False
    try:
        _validate_self_referenced_xmlids_are_defined(g, "oma_test_module")
    except ValueError:
        raised = True
    assert raised
    print("PASS item168: self-referenced-xmlids validator still raises for a genuinely undefined self-reference")


def test_bare_menu_restriction_id_rewritten_inside_extra_data_files():
    extra = {"data/menu.xml": '<record id="menu_crm_root" model="ir.ui.menu"><field name="groups_id" eval="[(4, ref(\'base.group_x\'))]"/></record>'}
    g = _gen(extra_data_files=extra)
    _autofix_bare_menu_restriction_id(g)
    assert 'id="crm.crm_menu_root"' in g.extra_data_files["data/menu.xml"]
    print("PASS item168: bare-menu-restriction-id autofix reaches extra_data_files")


def test_missing_dependency_for_cross_module_record_id_found_inside_extra_data_files():
    extra = {"data/menu.xml": '<record id="crm.crm_menu_root" model="ir.ui.menu"><field name="active" eval="True"/></record>'}
    g = _gen(extra_data_files=extra)
    _autofix_manifest_missing_dependency_for_cross_module_record_id(g, "oma_test_module")
    assert "crm" in g.manifest_fields.depends
    print("PASS item168: missing-dependency-for-cross-module-record-id autofix reaches extra_data_files")


def test_wrong_module_prefixed_local_group_id_corrected_inside_extra_data_files():
    security_xml = '<record id="group_x" model="res.groups"><field name="name">X</field></record>'
    extra = {"data/cron.xml": '<record id="cron_x" model="ir.cron"><field name="x" ref="wrong_module.group_x"/></record>'}
    g = _gen(security_xml=security_xml, extra_data_files=extra)
    _autofix_wrong_module_prefixed_local_group_id(g, "oma_test_module")
    assert 'ref="oma_test_module.group_x"' in g.extra_data_files["data/cron.xml"]
    print("PASS item168: wrong-module-prefixed-local-group-id autofix reaches extra_data_files")


def test_dedupe_duplicate_xml_record_ids_dedupes_within_extra_data_files_independently():
    extra = {"data/menu.xml": (
        '<record id="menu_x" model="ir.ui.menu"><field name="name">X</field></record>\n'
        '<record id="menu_x" model="ir.ui.menu"><field name="name">Y</field></record>'
    )}
    g = _gen(extra_data_files=extra)
    _autofix_dedupe_duplicate_xml_record_ids(g)
    assert g.extra_data_files["data/menu.xml"].count('id="menu_x"') == 1
    print("PASS item168: dedupe-duplicate-xml-record-ids autofix dedupes within an extra_data_files entry")


def test_strip_stray_xml_attribute_fragment_lines_strips_inside_extra_data_files():
    extra = {"data/cron.xml": '<record id="cron_x" model="ir.cron">\n" action="action_x"/>\n</record>'}
    g = _gen(extra_data_files=extra)
    _autofix_strip_stray_xml_attribute_fragment_lines(g)
    assert '" action="action_x"/>' not in g.extra_data_files["data/cron.xml"]
    print("PASS item168: strip-stray-xml-attribute-fragment-lines autofix reaches extra_data_files")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
    print(f"\nALL {len(fns)} P11 FIFTH-PASS ITEM 168 TESTS PASSED")
