"""Phase 28C (2026-07-28): unit tests for the real, confirmed live bug
in _find_all_xml_refs() -- <menuitem action="..."> is Odoo's own THIRD
real xmlid-reference syntax (neither `ref="..."` nor `ref('...')`),
and was never covered by either of _find_all_xml_refs()'s own two
existing patterns. Confirmed live as a genuine 5-round non-convergent
failure on the `school_student` task: a generated
`<menuitem action="oma_....action_school_student">` referenced an
`ir.actions.act_window` record that was never actually defined
anywhere in the same views.xml, and NEITHER `_validate_xml_refs_
resolve()` (skips self-prefixed refs) NOR
`_validate_self_referenced_xmlids_are_defined()` (exists specifically
to catch an unresolvable self-prefixed ref) ever saw the reference at
all, since both share this one extraction function.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import asyncio

from specialists.build.specialist import (
    GeneratedModuleFiles,
    ManifestFields,
    _autofix_menuitem_missing_action_record,
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


def test_find_all_xml_refs_extracts_menuitem_action_attribute():
    content = (
        '<odoo><menuitem id="menu_school_student" name="Students" '
        f'action="{_MODULE_NAME}.action_school_student" parent="menu_school_root"/></odoo>'
    )
    refs = _find_all_xml_refs(content)
    assert f"{_MODULE_NAME}.action_school_student" in refs, (
        f"<menuitem action=...> must be extracted as a real xmlid reference, got {refs!r}"
    )
    print("PASS: <menuitem action=\"...\"> is correctly extracted as a real xmlid reference")


def test_catches_a_menuitem_action_referencing_an_undefined_record():
    generated = _make_generated(
        '<odoo><menuitem id="menu_school_student" name="Students" '
        f'action="{_MODULE_NAME}.action_school_student"/></odoo>'
    )
    raised = False
    try:
        _validate_self_referenced_xmlids_are_defined(generated, _MODULE_NAME)
    except ValueError as exc:
        raised = True
        assert "action_school_student" in str(exc)
    assert raised, (
        "a <menuitem action=...> referencing an ir.actions.act_window record that is never "
        "actually defined anywhere must be caught before it ever reaches Odoo's own install "
        "and crashes with 'External ID not found in the system'"
    )
    print("PASS: an undefined menuitem action reference is caught deterministically at "
          "generation time, closing the real 5-round non-convergent loop found live on "
          "school_student")


def test_does_not_false_positive_when_the_action_record_is_genuinely_defined():
    generated = _make_generated(
        f'<odoo><record id="action_school_student" model="ir.actions.act_window">'
        f'<field name="name">Students</field>'
        f'<field name="res_model">school.student</field>'
        f'</record>'
        f'<menuitem id="menu_school_student" name="Students" '
        f'action="{_MODULE_NAME}.action_school_student"/></odoo>'
    )
    _validate_self_referenced_xmlids_are_defined(generated, _MODULE_NAME)
    print("PASS: no false positive when the referenced action record is genuinely defined "
          "in the same generation")


def test_autofix_synthesizes_the_missing_action_record_before_its_own_reference():
    """Real, confirmed bug found live (2026-07-28, Phase 28C,
    `school_student` task): a real non-convergent loop -- the two
    validators above correctly, deterministically rejected a menuitem
    referencing an undefined action every round, but the LLM
    regenerated the identical mistake 5 straight times. Converted into
    a deterministic autofix that synthesizes the missing `ir.actions.
    act_window` record automatically, matching this project's own
    "give Build nothing left to get wrong" discipline for a
    mechanically synthesizable shape.

    Also the real regression test for a second bug found live building
    this exact fix: a naive append right before `</odoo>` placed the
    synthesized record AFTER the menuitem that references it whenever
    the menuitem isn't the file's last element -- Odoo's own XML
    loader resolves references top-to-bottom, so a record defined
    after its own reference crashes exactly like an undefined one.
    """
    manifest = ManifestFields(
        name="x", version="1.0.0", category="Tools", summary="x", author="x",
        depends=["base"], data=[],
    )
    generated = GeneratedModuleFiles(
        manifest_fields=manifest,
        models_py="from odoo import models, fields\n\nclass SchoolStudent(models.Model):\n    _name = 'school.student'\n",
        security_csv="id,name\n",
        views_xml=(
            '<odoo>\n'
            '    <menuitem id="menu_school_student_root" name="School"/>\n'
            '    <menuitem id="menu_school_student" name="Students" '
            'parent="menu_school_student_root" action="action_school_student"/>\n'
            '</odoo>'
        ),
        notes="",
    )
    _autofix_menuitem_missing_action_record(generated)
    assert 'model="ir.actions.act_window"' in generated.views_xml
    assert "<field name=\"res_model\">school.student</field>" in generated.views_xml

    # The real, critical requirement: the record must appear BEFORE the
    # menuitem that references it, not merely exist somewhere in the file.
    record_pos = generated.views_xml.index('id="action_school_student" model="ir.actions.act_window"')
    menuitem_pos = generated.views_xml.index('action="action_school_student"')
    assert record_pos < menuitem_pos, (
        "the synthesized action record must be defined BEFORE the menuitem that references it -- "
        "Odoo's own XML loader resolves references top-to-bottom, a record defined after its own "
        "reference crashes exactly like a genuinely undefined one"
    )

    # Must pass the real validator cleanly now, with no manual fix needed.
    _validate_self_referenced_xmlids_are_defined(generated, "oma_simple_custom_module_task_595ad7bc")
    print("PASS: the missing action record is synthesized automatically, correctly positioned "
          "before its own reference, and passes the real validator with no further fix needed")


def test_autofix_bails_when_the_module_defines_no_new_model_or_more_than_one():
    manifest = ManifestFields(
        name="x", version="1.0.0", category="Tools", summary="x", author="x",
        depends=["base"], data=[],
    )
    generated = GeneratedModuleFiles(
        manifest_fields=manifest,
        models_py="from odoo import models\n\nclass X(models.Model):\n    _inherit = 'res.partner'\n",
        security_csv="id,name\n",
        views_xml='<odoo><menuitem id="m" name="M" action="action_x"/></odoo>',
        notes="",
    )
    before = generated.views_xml
    _autofix_menuitem_missing_action_record(generated)
    assert generated.views_xml == before, "must never guess which model an action belongs to"
    print("PASS: never synthesizes a record when there's no single, unambiguous new model to target")


def test_xml_refs_resolve_recognizes_a_bare_locally_defined_self_reference():
    """Real, confirmed bug found live (2026-07-28, Phase 28C,
    `school_student` task): a SECOND, real non-convergent loop that
    survived the synthesizing autofix above -- `_validate_xml_refs_
    resolve()`'s own exclusion logic only ever skipped a PREFIXED
    self-reference (`own_module.X`); a genuinely bare, unprefixed
    `action="X"` referencing a bare `<record id="X">` also defined in
    the same generation (exactly what the synthesizing autofix
    produces) has no dot at all, so it always fell through to the LIVE
    REGISTRY check -- which, for a record that only exists in this
    round's own not-yet-installed content, always incorrectly reports
    it as a fabricated external id. Reproduced here end to end: the
    autofix synthesizes a bare-id record, and this validator must not
    reject it.
    """
    manifest = ManifestFields(
        name="x", version="1.0.0", category="Tools", summary="x", author="x",
        depends=["base"], data=[],
    )
    generated = GeneratedModuleFiles(
        manifest_fields=manifest,
        models_py="from odoo import models, fields\n\nclass SchoolStudent(models.Model):\n    _name = 'school.student'\n",
        security_csv="id,name\n",
        views_xml=(
            '<odoo>\n'
            '    <menuitem id="menu_school_student_root" name="School"/>\n'
            '    <menuitem id="menu_school_student" name="Students" '
            'parent="menu_school_student_root" action="action_school_student"/>\n'
            '</odoo>'
        ),
        notes="",
    )
    _autofix_menuitem_missing_action_record(generated)
    raised = False
    try:
        asyncio.run(_validate_xml_refs_resolve(
            generated, "odoo16_dev", "oma_simple_custom_module_task_595ad7bc", task_id="test",
        ))
    except ValueError as exc:
        raised = True
        print(f"unexpectedly raised: {exc}")
    assert not raised, (
        "a bare, locally-defined self-reference must never be sent to the live registry as if "
        "it were a fabricated external id"
    )
    print("PASS: a bare, locally-defined self-reference is correctly recognized as local, "
          "not incorrectly checked against the live registry as an external id")


if __name__ == "__main__":
    test_find_all_xml_refs_extracts_menuitem_action_attribute()
    test_catches_a_menuitem_action_referencing_an_undefined_record()
    test_does_not_false_positive_when_the_action_record_is_genuinely_defined()
    test_autofix_synthesizes_the_missing_action_record_before_its_own_reference()
    test_autofix_bails_when_the_module_defines_no_new_model_or_more_than_one()
    test_xml_refs_resolve_recognizes_a_bare_locally_defined_self_reference()
    print("\nALL MENUITEM-ACTION-REF TESTS PASSED")
