"""Phase 28C (2026-07-28): unit tests for
_autofix_ensure_baseline_access_row_for_menu_exposed_models() -- the
real, confirmed fix for two separate 5-round escalations on the
`school_student` task's own `menu_structure` round.

Root cause: `_autofix_strip_premature_security_content_on_decomposed_
round()` (correctly) blanks security_csv down to header-only and drops
the manifest's own reference to it whenever the round's own focus
isn't security-related, deferring the task's own custom
`security_groups`/`record_rules` constraints to their own later round.
But it conflated that with Odoo's own SEPARATE, always-mandatory
requirement: the moment a real <menuitem>/<ir.actions.act_window> makes
a model reachable by a real (non-superuser) end user, Odoo raises a
genuine AccessError with zero ir.model.access.csv rows -- confirmed
real Odoo ACL behavior, not a hallucinated Code-Review complaint (only
the true superuser, uid=1, bypasses ir.model.access checks at all).
Code-Review correctly, repeatedly rejected this exact gap across two
separate live escalations; the fix is a genuine, deterministic
baseline-row synthesizer, not a filter that suppresses the complaint.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from specialists.build.specialist import (
    GeneratedModuleFiles,
    ManifestFields,
    _autofix_ensure_baseline_access_row_for_menu_exposed_models,
    _menu_exposed_models,
)

_MODULE_NAME = "oma_simple_custom_module_task_595ad7bc"


def _make_generated(views_xml: str | None, security_csv: str | None, data: list[str] | None = None) -> GeneratedModuleFiles:
    manifest = ManifestFields(
        name="x", version="1.0.0", category="Tools", summary="x", author="x",
        depends=["base"], data=data or [],
    )
    return GeneratedModuleFiles(
        manifest_fields=manifest, models_py="", security_csv=security_csv,
        views_xml=views_xml, notes="",
    )


_REAL_VIEWS_XML = (
    '<odoo>\n'
    '    <record id="action_school_student" model="ir.actions.act_window">\n'
    '        <field name="name">Students</field>\n'
    '        <field name="res_model">school.student</field>\n'
    '        <field name="view_mode">tree,form</field>\n'
    '    </record>\n'
    '    <menuitem id="menu_school_root" name="School" sequence="10"/>\n'
    '    <menuitem id="menu_school_students" name="Students" parent="menu_school_root" '
    'action="action_school_student"/>\n'
    '</odoo>'
)


def test_menu_exposed_models_detects_the_real_res_model():
    exposed = _menu_exposed_models(_REAL_VIEWS_XML)
    assert exposed == {"school.student"}
    print("PASS: a real <menuitem action=...> -> ir.actions.act_window chain correctly resolves its res_model")


def test_menu_exposed_models_empty_when_no_menu_exists():
    assert _menu_exposed_models("<odoo></odoo>") == set()
    assert _menu_exposed_models("") == set()
    print("PASS: no menuitem at all means no exposed models, never a false positive")


def test_adds_baseline_row_when_csv_is_header_only():
    """The exact real shape found live: the round-scoping stripper
    correctly blanked the CSV to header-only and dropped the manifest
    reference (menu_structure isn't a security-focused round), but a
    real menu now exposes the model -- the baseline row must be added
    back, and the manifest reference restored, regardless of the
    round's own focus label.
    """
    generated = _make_generated(
        views_xml=_REAL_VIEWS_XML,
        security_csv="id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink\n",
        data=["views/views.xml"],
    )
    _autofix_ensure_baseline_access_row_for_menu_exposed_models(generated)
    assert "model_school_student" in generated.security_csv
    assert "base.group_user" in generated.security_csv
    assert "security/ir.model.access.csv" in generated.manifest_fields.data
    print("PASS: a genuinely menu-exposed model gets its mandatory baseline access row and manifest "
          "reference restored, closing the real live non-convergent loop on school_student")


def test_adds_baseline_row_when_csv_is_empty_string():
    generated = _make_generated(views_xml=_REAL_VIEWS_XML, security_csv="")
    _autofix_ensure_baseline_access_row_for_menu_exposed_models(generated)
    assert generated.security_csv
    assert "model_school_student" in generated.security_csv
    print("PASS: works even when Build wrote an empty security_csv")


def test_never_adds_a_duplicate_row_when_one_already_covers_the_model():
    generated = _make_generated(
        views_xml=_REAL_VIEWS_XML,
        security_csv=(
            "id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink\n"
            "access_school_student,school.student,model_school_student,base.group_user,1,1,1,0\n"
        ),
        data=["views/views.xml", "security/ir.model.access.csv"],
    )
    before = generated.security_csv
    _autofix_ensure_baseline_access_row_for_menu_exposed_models(generated)
    assert generated.security_csv == before
    print("PASS: never duplicates a row when the model is already genuinely covered")


def test_never_fires_when_no_menu_exposes_any_model():
    generated = _make_generated(
        views_xml="<odoo></odoo>",
        security_csv="id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink\n",
        data=[],
    )
    before_csv, before_data = generated.security_csv, list(generated.manifest_fields.data)
    _autofix_ensure_baseline_access_row_for_menu_exposed_models(generated)
    assert generated.security_csv == before_csv
    assert generated.manifest_fields.data == before_data
    print("PASS: a round with no real menu exposure is never touched -- the round-scoping stripper's "
          "own deferral decision is respected when nothing actually needs the row yet")


def test_never_touches_an_unrelated_models_own_row():
    views_xml = (
        '<odoo>\n'
        '    <record id="action_a" model="ir.actions.act_window">\n'
        '        <field name="name">A</field>\n'
        '        <field name="res_model">school.student</field>\n'
        '        <field name="view_mode">tree,form</field>\n'
        '    </record>\n'
        '    <menuitem id="menu_a" name="A" action="action_a"/>\n'
        '</odoo>'
    )
    generated = _make_generated(
        views_xml=views_xml,
        security_csv=(
            "id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink\n"
            "access_other_model,other.model,model_other_model,base.group_user,1,1,1,0\n"
        ),
    )
    _autofix_ensure_baseline_access_row_for_menu_exposed_models(generated)
    assert "access_other_model,other.model,model_other_model,base.group_user,1,1,1,0" in generated.security_csv
    assert "model_school_student" in generated.security_csv
    print("PASS: an existing, unrelated model's own row is preserved untouched; the new one is added additively")


def test_item173_act_window_shortcut_tag_also_detected():
    """P11 fifth pass item 173: Odoo's own <act_window id="x" res_model="y" .../> shortcut tag is
    functionally identical to a full <record model="ir.actions.act_window"> block (loader-expanded
    into the same record type) but uses XML attributes, not child <field> elements -- a model
    exposed only through this shortcut previously got no baseline access row at all.
    """
    views_xml = (
        '<odoo>\n'
        '    <act_window id="action_school_student" name="Students" res_model="school.student" '
        'view_mode="tree,form"/>\n'
        '    <menuitem id="menu_school_root" name="School" sequence="10"/>\n'
        '    <menuitem id="menu_school_students" name="Students" parent="menu_school_root" '
        'action="action_school_student"/>\n'
        '</odoo>'
    )
    exposed = _menu_exposed_models(views_xml)
    assert exposed == {"school.student"}
    print("PASS item173: the <act_window> shortcut tag is correctly detected, same as a full <record>")


def test_item173_act_window_shortcut_produces_a_real_baseline_row():
    views_xml = (
        '<odoo>\n'
        '    <act_window id="action_school_student" name="Students" res_model="school.student" '
        'view_mode="tree,form"/>\n'
        '    <menuitem id="menu_school_students" name="Students" action="action_school_student"/>\n'
        '</odoo>'
    )
    generated = _make_generated(
        views_xml=views_xml,
        security_csv="id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink\n",
        data=["views/views.xml"],
    )
    _autofix_ensure_baseline_access_row_for_menu_exposed_models(generated)
    assert "model_school_student" in generated.security_csv
    print("PASS item173: the act_window shortcut tag's own exposed model gets a real baseline access row")


if __name__ == "__main__":
    test_menu_exposed_models_detects_the_real_res_model()
    test_menu_exposed_models_empty_when_no_menu_exists()
    test_adds_baseline_row_when_csv_is_header_only()
    test_adds_baseline_row_when_csv_is_empty_string()
    test_never_adds_a_duplicate_row_when_one_already_covers_the_model()
    test_never_fires_when_no_menu_exposes_any_model()
    test_never_touches_an_unrelated_models_own_row()
    test_item173_act_window_shortcut_tag_also_detected()
    test_item173_act_window_shortcut_produces_a_real_baseline_row()
    print("\nALL BASELINE-ACCESS-ROW TESTS PASSED")
