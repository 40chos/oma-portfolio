"""Phase 28C (2026-07-28): unit tests for _validate_no_duplicate_xml_
record_ids() -- the real, confirmed live gap found on the
`school_student` task's own `menu_structure` round. The SAME
underlying mistake as _validate_no_duplicate_method_definitions()
(Build declaring the same name twice), but for XML `<record id="...">`
/`<menuitem id="...">` declarations, which Odoo's own XML loader
raises a hard ParseError for -- confirmed live, Code-Review repeatedly
flagged "Duplicate record id 'menu_school_students' defined twice in
the same file" across multiple identical rounds with no existing
deterministic check to catch it pre-write.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from specialists.build.specialist import (
    GeneratedModuleFiles,
    ManifestFields,
    _autofix_dedupe_duplicate_xml_record_ids,
    _validate_no_duplicate_xml_record_ids,
)


def _make_generated(views_xml: str | None = None, security_xml: str | None = None) -> GeneratedModuleFiles:
    manifest = ManifestFields(
        name="x", version="1.0.0", category="Tools", summary="x", author="x",
        depends=["base"], data=[],
    )
    return GeneratedModuleFiles(
        manifest_fields=manifest, models_py="", security_csv="id,name\n",
        views_xml=views_xml, security_xml=security_xml, notes="",
    )


def test_catches_a_real_duplicate_menuitem_id():
    generated = _make_generated(views_xml=(
        '<odoo>'
        '<menuitem id="menu_school_root" name="School" sequence="10"/>'
        '<menuitem id="menu_school_students" name="Students" parent="menu_school_root" sequence="10"/>'
        '<menuitem id="menu_school_students" name="Students" parent="menu_school_root" sequence="20"/>'
        '</odoo>'
    ))
    raised = False
    try:
        _validate_no_duplicate_xml_record_ids(generated)
    except ValueError as exc:
        raised = True
        assert "menu_school_students" in str(exc)
    assert raised, "a genuine duplicate <menuitem id=...> must be caught before it ever reaches Odoo's own install"
    print("PASS: a real duplicate menuitem id is caught deterministically, closing the real "
          "non-convergent loop found live on school_student")


def test_catches_a_real_duplicate_record_id():
    generated = _make_generated(security_xml=(
        '<odoo>'
        '<record id="group_school_admin" model="res.groups"><field name="name">Admin</field></record>'
        '<record id="group_school_admin" model="res.groups"><field name="name">Administrator</field></record>'
        '</odoo>'
    ))
    raised = False
    try:
        _validate_no_duplicate_xml_record_ids(generated)
    except ValueError as exc:
        raised = True
        assert "group_school_admin" in str(exc)
    assert raised, "a genuine duplicate <record id=...> must be caught before it ever reaches Odoo's own install"
    print("PASS: a real duplicate record id is caught deterministically")


def test_does_not_false_positive_on_distinct_ids():
    generated = _make_generated(views_xml='<odoo><menuitem id="menu_a"/><menuitem id="menu_b"/></odoo>')
    _validate_no_duplicate_xml_record_ids(generated)
    print("PASS: no false positive on genuinely distinct ids")


def test_does_not_false_positive_on_the_same_id_appearing_once_per_file():
    """The same id appearing once in security_xml AND once in views_xml
    is completely normal and legitimate -- Odoo's own xmlid namespace
    has no cross-file uniqueness requirement, only a genuine duplicate
    WITHIN the same file is ever a real bug.
    """
    generated = _make_generated(
        views_xml='<odoo><record id="shared_id" model="ir.ui.view"><field name="name">x</field></record></odoo>',
        security_xml='<odoo><record id="shared_id" model="res.groups"><field name="name">y</field></record></odoo>',
    )
    _validate_no_duplicate_xml_record_ids(generated)
    print("PASS: the same id appearing once per file (not duplicated within either) is never a false positive")


def test_autofix_dedupes_a_real_duplicate_self_closing_menuitem():
    """Real, confirmed bug found live (2026-07-28, Phase 28C,
    school_student task): the validator above correctly, deterministically
    rejected this exact duplicate every round, but the LLM regenerated
    the identical mistake across TWO separate 5-round escalations, even
    after an explicit human note named the duplicate id -- the textbook
    non-convergent self-repair loop. The autofix keeps the FIRST
    occurrence and drops the later one, so the round converges instead
    of exhausting its budget on a mechanically fixable mistake.
    """
    generated = _make_generated(views_xml=(
        '<odoo>\n'
        '    <menuitem id="menu_school_root" name="School" sequence="10"/>\n'
        '    <menuitem id="menu_school_students" name="Students" parent="menu_school_root" sequence="10" action="action_school_student"/>\n'
        '    <menuitem id="menu_school_students" name="Students" parent="menu_school_root" sequence="20" action="action_school_student"/>\n'
        '</odoo>'
    ))
    _autofix_dedupe_duplicate_xml_record_ids(generated)
    assert generated.views_xml.count('id="menu_school_students"') == 1
    assert generated.views_xml.count('id="menu_school_root"') == 1
    # The FIRST occurrence's own content (sequence="10") must survive, not the second's.
    assert 'sequence="10" action="action_school_student"' in generated.views_xml
    _validate_no_duplicate_xml_record_ids(generated)  # must now pass cleanly
    print("PASS: a real duplicate self-closing <menuitem> is deduped, keeping the first occurrence, "
          "and passes the real validator with no further fix needed")


def test_autofix_dedupes_a_real_duplicate_record_block():
    generated = _make_generated(views_xml=(
        '<odoo>\n'
        '    <record id="view_school_student_tree" model="ir.ui.view">\n'
        '        <field name="name">school.student.tree.v1</field>\n'
        '        <field name="model">school.student</field>\n'
        '    </record>\n'
        '    <record id="view_school_student_tree" model="ir.ui.view">\n'
        '        <field name="name">school.student.tree.v2</field>\n'
        '        <field name="model">school.student</field>\n'
        '    </record>\n'
        '</odoo>'
    ))
    _autofix_dedupe_duplicate_xml_record_ids(generated)
    assert generated.views_xml.count('id="view_school_student_tree"') == 1
    assert "school.student.tree.v1" in generated.views_xml
    assert "school.student.tree.v2" not in generated.views_xml
    _validate_no_duplicate_xml_record_ids(generated)
    print("PASS: a real duplicate multi-line <record> block is deduped, keeping the first occurrence")


def test_autofix_never_touches_genuinely_distinct_ids():
    generated = _make_generated(views_xml=(
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
    ))
    before = generated.views_xml
    _autofix_dedupe_duplicate_xml_record_ids(generated)
    assert generated.views_xml == before
    print("PASS: genuinely distinct real content (the actual live school_student shape) is never touched")


def test_autofix_is_a_noop_when_there_is_nothing_to_dedupe():
    generated = _make_generated(views_xml=None, security_xml=None)
    _autofix_dedupe_duplicate_xml_record_ids(generated)  # must not raise
    print("PASS: a no-op when views_xml/security_xml are both empty")


if __name__ == "__main__":
    test_catches_a_real_duplicate_menuitem_id()
    test_catches_a_real_duplicate_record_id()
    test_does_not_false_positive_on_distinct_ids()
    test_does_not_false_positive_on_the_same_id_appearing_once_per_file()
    test_autofix_dedupes_a_real_duplicate_self_closing_menuitem()
    test_autofix_dedupes_a_real_duplicate_record_block()
    test_autofix_never_touches_genuinely_distinct_ids()
    test_autofix_is_a_noop_when_there_is_nothing_to_dedupe()
    print("\nALL DUPLICATE-XML-RECORD-ID TESTS PASSED")
