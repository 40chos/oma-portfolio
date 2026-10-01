"""50-task deep-dive (docs/reports/PHASE30_50TASK_DEEP_DIVE_MASTER_2026-08-05.md, P1 item 1):
`<menuitem parent="...">` is Odoo's own FOURTH real xmlid-reference syntax on the `<menuitem>`
element -- `_XML_MENUITEM_ACTION_ATTR_RE` already covered `action=`, but `parent=` (naming the
xmlid of the parent menu this one nests under) had zero coverage anywhere in this file, confirmed
via a live grep for `menuitem.*parent`/`MENUITEM_PARENT` returning zero hits before this fix. This
is the same defect family already documented in
docs/planning/PHASE30_P11_ADDITIONS_FROM_P7_TIER3_2026-07-31.md item #1 (5 prior real failures) --
it recurred a 3rd time on tasks 014, 020, and 029 of the 50-task benchmark.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import asyncio

from specialists.build.specialist import (
    GeneratedModuleFiles,
    ManifestFields,
    _find_all_xml_refs,
    _validate_self_referenced_xmlids_are_defined,
    _validate_xml_refs_resolve,
)

_MODULE_NAME = "oma_simple_custom_module_task_595ad7bc"


def _make_generated(views_xml: str) -> GeneratedModuleFiles:
    manifest = ManifestFields(
        name="x", version="1.0.0", category="Tools", summary="x", author="x",
        depends=["base"], data=[],
    )
    return GeneratedModuleFiles(
        manifest_fields=manifest, models_py="", security_csv="id,name\n",
        views_xml=views_xml, notes="",
    )


def test_find_all_xml_refs_extracts_menuitem_parent_attribute():
    content = (
        '<odoo><menuitem id="menu_x" name="X" '
        'parent="project.menu_project_root" action="oma_x.action_x"/></odoo>'
    )
    refs = _find_all_xml_refs(content)
    assert "project.menu_project_root" in refs, (
        f"<menuitem parent=...> must be extracted as a real xmlid reference, got {refs!r}"
    )
    print("PASS: <menuitem parent=\"...\"> is correctly extracted as a real xmlid reference")


def test_xml_refs_resolve_catches_a_menuitem_parent_referencing_a_nonexistent_external_menu():
    generated = _make_generated(
        '<odoo><menuitem id="menu_x" name="X" '
        'parent="project.menu_this_does_not_exist_xyz_999"/></odoo>'
    )
    raised = False
    try:
        asyncio.run(_validate_xml_refs_resolve(
            generated, "odoo16_dev", _MODULE_NAME, task_id="test",
        ))
    except ValueError as exc:
        raised = True
        assert "menu_this_does_not_exist_xyz_999" in str(exc)
    assert raised, (
        "a <menuitem parent=...> referencing a real-Odoo-looking but genuinely nonexistent "
        "external menu id must be caught before it ever reaches Odoo's own install and crashes "
        "with 'External ID not found in the system' -- the exact Task 014/029 failure shape"
    )
    print("PASS: a hallucinated menuitem parent= external reference is caught deterministically")


def test_xml_refs_resolve_does_not_false_positive_on_a_real_base_menu_parent():
    generated = _make_generated(
        '<odoo><menuitem id="menu_x" name="X" parent="base.menu_administration"/></odoo>'
    )
    asyncio.run(_validate_xml_refs_resolve(
        generated, "odoo16_dev", _MODULE_NAME, task_id="test",
    ))
    print("PASS: no false positive when menuitem parent= points at a real, existing base menu")


def test_xml_refs_resolve_recognizes_a_bare_same_module_menuitem_parent_as_local():
    """The explicit regression risk called out in the deep-dive report: a same-module nested
    menu (`<menuitem id="menu_root">` ... `<menuitem parent="menu_root">`, both in THIS
    generation's own views.xml) must be recognized as a local self-reference, never sent to the
    live registry as if it were external.
    """
    generated = _make_generated(
        '<odoo>\n'
        '    <menuitem id="menu_root" name="Root"/>\n'
        '    <menuitem id="menu_child" name="Child" parent="menu_root"/>\n'
        '</odoo>'
    )
    raised = False
    try:
        asyncio.run(_validate_xml_refs_resolve(
            generated, "odoo16_dev", _MODULE_NAME, task_id="test",
        ))
    except ValueError as exc:
        raised = True
        print(f"unexpectedly raised: {exc}")
    assert not raised, (
        "a bare, same-module <menuitem parent=...> referencing a <menuitem id=...> (not a "
        "<record id=...>) also defined in this same generation must never be treated as an "
        "external, not-yet-real xmlid -- this is the explicit regression risk flagged in the "
        "50-task deep-dive report's own P1 item 1"
    )
    print("PASS: a bare, same-module menuitem-to-menuitem parent= self-reference is correctly "
          "recognized as local, not sent to the live registry")


def test_self_referenced_xmlids_recognizes_prefixed_menuitem_parent_as_defined():
    """Same regression risk, but exercised through the self-prefixed path
    (_validate_self_referenced_xmlids_are_defined) rather than the bare path above."""
    generated = _make_generated(
        '<odoo>\n'
        f'    <menuitem id="{_MODULE_NAME}.menu_root" name="Root"/>\n'
        f'    <menuitem id="menu_child" name="Child" parent="{_MODULE_NAME}.menu_root"/>\n'
        '</odoo>'
    )
    _validate_self_referenced_xmlids_are_defined(generated, _MODULE_NAME)
    print("PASS: a self-prefixed menuitem parent= referencing a <menuitem id=...> (not a "
          "<record id=...>) also defined in the same generation is correctly recognized as defined")


def test_self_referenced_xmlids_still_raises_when_menuitem_parent_is_genuinely_undefined():
    generated = _make_generated(
        '<odoo>\n'
        f'    <menuitem id="menu_child" name="Child" parent="{_MODULE_NAME}.menu_never_defined"/>\n'
        '</odoo>'
    )
    raised = False
    try:
        _validate_self_referenced_xmlids_are_defined(generated, _MODULE_NAME)
    except ValueError as exc:
        raised = True
        assert "menu_never_defined" in str(exc)
    assert raised, (
        "a self-prefixed menuitem parent= referencing an id that is genuinely never defined "
        "anywhere in this generation (neither a <record> nor a <menuitem>) must still raise"
    )
    print("PASS: a genuinely undefined self-prefixed menuitem parent= is still caught")


if __name__ == "__main__":
    test_find_all_xml_refs_extracts_menuitem_parent_attribute()
    test_xml_refs_resolve_catches_a_menuitem_parent_referencing_a_nonexistent_external_menu()
    test_xml_refs_resolve_does_not_false_positive_on_a_real_base_menu_parent()
    test_xml_refs_resolve_recognizes_a_bare_same_module_menuitem_parent_as_local()
    test_self_referenced_xmlids_recognizes_prefixed_menuitem_parent_as_defined()
    test_self_referenced_xmlids_still_raises_when_menuitem_parent_is_genuinely_undefined()
    print("\nALL MENUITEM-PARENT-REF TESTS PASSED")
