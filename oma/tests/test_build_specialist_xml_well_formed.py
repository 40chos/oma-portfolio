"""Phase 28C (2026-07-28): unit tests for _validate_xml_is_well_formed()
-- the real, confirmed live gap found on the school_student task's own
security_groups round.

Root cause: every other XML check in build/specialist.py is a regex
heuristic against ONE specific known shape (duplicate id, unresolved
ref, wrong order, ...). None of them ever actually PARSE the document,
so none could catch Build emitting a real, complete <menuitem> tag
immediately followed by a stray, truncated fragment on its own line
(`" action="action_school_student"/>`, no `<menuitem`/`<record` prefix
at all -- almost certainly a cut-off duplicate-generation artifact).
The malformed file passed Build's ENTIRE validation chain, scaffolded,
was written, and only failed at the real Odoo install step with a
genuine odoo.tools.convert.ParseError -- confirmed live via directly
reading the exact broken file off the real container's disk.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from specialists.build.specialist import (
    GeneratedModuleFiles,
    ManifestFields,
    _autofix_strip_stray_xml_attribute_fragment_lines,
    _validate_xml_is_well_formed,
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


# The EXACT real broken content pulled live off odoo-dev.int's own disk
# (/mnt/extra-addons/oma_simple_custom_module_task_595ad7bc/views/views.xml)
# after it caused a genuine odoo.tools.convert.ParseError at install time.
_REAL_BROKEN_CONTENT = (
    '<?xml version="1.0" encoding="utf-8"?>\n'
    '<odoo>\n'
    '    <record id="action_school_student" model="ir.actions.act_window">\n'
    '        <field name="name">School Student</field>\n'
    '        <field name="res_model">school.student</field>\n'
    '        <field name="view_mode">tree,form</field>\n'
    '    </record>\n'
    '    <menuitem id="menu_school_root" name="School" sequence="10"/>\n'
    '    <menuitem id="menu_school_students" name="Students" parent="menu_school_root" '
    'sequence="10" action="action_school_student"/>\n'
    '    " action="action_school_student"/>\n'
    '    <record id="view_school_student_tree" model="ir.ui.view">\n'
    '        <field name="name">school.student.tree</field>\n'
    '    </record>\n'
    '</odoo>'
)


def test_catches_the_real_live_truncated_fragment():
    generated = _make_generated(views_xml=_REAL_BROKEN_CONTENT)
    raised = False
    try:
        _validate_xml_is_well_formed(generated)
    except ValueError as exc:
        raised = True
        assert "stray" in str(exc)
    assert raised, (
        "a genuinely malformed XML document (real content pulled from the live failure) must be "
        "caught deterministically before it ever reaches Odoo's own install"
    )
    print("PASS: the exact real truncated-fragment content that reached a live Odoo ParseError "
          "is now caught pre-write, closing the real gap found on school_student's security_groups round")


def test_does_not_false_positive_on_genuinely_well_formed_xml():
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
    _validate_xml_is_well_formed(generated)  # must not raise
    print("PASS: no false positive on genuinely well-formed, real content")


def test_catches_malformed_security_xml_too():
    generated = _make_generated(security_xml=(
        '<odoo>\n'
        '    <record id="group_school_admin" model="res.groups">\n'
        '        <field name="name">Admin</field>\n'
        '    </record\n'  # missing closing '>'
        '</odoo>'
    ))
    raised = False
    try:
        _validate_xml_is_well_formed(generated)
    except ValueError:
        raised = True
    assert raised, "malformed security_xml must be caught the same way as malformed views_xml"
    print("PASS: security_xml gets the exact same real well-formedness check as views_xml")


def test_is_a_noop_when_both_files_are_empty():
    generated = _make_generated(views_xml=None, security_xml=None)
    _validate_xml_is_well_formed(generated)  # must not raise
    print("PASS: a no-op when there's no XML content to check at all")


def test_autofix_strips_the_real_live_fragment_and_then_passes_validation():
    """Real, confirmed bug found live (2026-07-28, Phase 28C,
    school_student task, security_groups round): the well-formedness
    validator above correctly, deterministically caught this exact
    fragment, but the LLM regenerated the byte-for-byte identical
    mistake on the very next round -- a non-convergent loop. The
    autofix strips it automatically so the round converges instead.
    """
    generated = _make_generated(views_xml=_REAL_BROKEN_CONTENT)
    _autofix_strip_stray_xml_attribute_fragment_lines(generated)
    stray_lines = [line for line in generated.views_xml.splitlines() if line.strip().startswith('"')]
    assert not stray_lines, f"the stray bare-quote line must be gone, found: {stray_lines!r}"
    assert 'id="menu_school_students"' in generated.views_xml  # the real, legitimate menuitem survives
    _validate_xml_is_well_formed(generated)  # must now pass cleanly
    print("PASS: the exact real stray fragment is stripped automatically, and the result passes "
          "the real well-formedness validator with no further fix needed")


def test_autofix_never_touches_a_legitimate_quoted_field_value():
    generated = _make_generated(views_xml=(
        '<odoo>\n'
        '    <record id="view_x" model="ir.ui.view">\n'
        '        <field name="name">"a genuinely quoted label"</field>\n'
        '    </record>\n'
        '</odoo>'
    ))
    before = generated.views_xml
    _autofix_strip_stray_xml_attribute_fragment_lines(generated)
    assert generated.views_xml == before
    print("PASS: a legitimate <field> line containing a quoted string is never touched -- only a "
          "bare-quote-starting line with no '<' anywhere is ever stripped")


if __name__ == "__main__":
    test_catches_the_real_live_truncated_fragment()
    test_does_not_false_positive_on_genuinely_well_formed_xml()
    test_catches_malformed_security_xml_too()
    test_is_a_noop_when_both_files_are_empty()
    test_autofix_strips_the_real_live_fragment_and_then_passes_validation()
    test_autofix_never_touches_a_legitimate_quoted_field_value()
    print("\nALL XML-WELL-FORMED TESTS PASSED")
