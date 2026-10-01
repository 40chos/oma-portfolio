"""Phase 28C (2026-07-28): unit tests for
specialists/build/specialist.py's
_autofix_strip_premature_views_content_on_decomposed_round() -- pure
logic, no live SSH/DB/gateway needed.

Real bug this fixes: the `school_student` task's own first live run
(the actual Operator-priority deliverable Phase 28 exists to ship) burned
its entire 5-round retry budget on an identical non-progress loop --
Build kept generating a real, populated `views/views.xml` and
referencing it in the manifest despite the round's own goal explicitly
saying 'student_views' is NOT yet in scope this round. Code-Review
correctly rejected it every round; nothing ever stopped Build from
generating the same premature content again -- the exact same failure
class `_autofix_strip_premature_security_content_on_decomposed_round()`
(see test_build_specialist_premature_security_strip.py) already exists
to close for security content, just never built for views.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from specialists.build.specialist import (
    GeneratedModuleFiles,
    ManifestFields,
    _any_view_related_constraint_already_satisfied,
    _autofix_restore_dropped_views_content_when_already_satisfied,
    _autofix_strip_premature_computed_field_on_decomposed_round,
    _autofix_strip_premature_views_content_on_decomposed_round,
    _this_rounds_focus_is_views_related,
)

_REAL_SCHOOL_STUDENT_GOAL = (
    "Build a complete, clean, and simple custom module called school_student. "
    "This round's own NEW focus is ONLY: 'student_model_fields'. Add ONLY the code this "
    "one constraint strictly requires. The following constraints are NOT yet in scope for "
    "this round and must NOT be implemented even partially: ['student_views', "
    "'menu_structure', 'security_groups', 'record_rules', 'computed_age_field', "
    "'demo_data', 'automated_tests']."
)

_VIEWS_FOCUS_GOAL = (
    "Build a complete, clean, and simple custom module called school_student. "
    "This round's own NEW focus is ONLY: 'student_views'. The following constraints are "
    "NOT yet in scope for this round and must NOT be implemented even partially: []."
)

_PLAIN_GOAL = "Add a single new field 'preferred_language' to res.partner."

_REAL_VIEWS_XML = (
    '<odoo><record id="view_school_student_form" model="ir.ui.view">'
    '<field name="name">school.student.form</field>'
    '<field name="model">school.student</field>'
    '<field name="arch" type="xml"><form><field name="name"/></form></field>'
    "</record></odoo>"
)


def _make_generated(views_xml: str | None) -> GeneratedModuleFiles:
    manifest_fields = ManifestFields(
        name="oma_test", version="0.1", category="Uncategorized", summary="", author="",
        depends=["base"],
        data=(["views/views.xml"] if views_xml else []),
    )
    return GeneratedModuleFiles(
        manifest_fields=manifest_fields,
        models_py="from odoo import models, fields\n\nclass SchoolStudent(models.Model):\n    _name = 'school.student'\n",
        security_csv="id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink",
        views_xml=views_xml,
        notes="",
    )


def test_focus_is_views_related_false_for_model_round():
    assert _this_rounds_focus_is_views_related(_REAL_SCHOOL_STUDENT_GOAL) is False
    print("PASS: 'student_model_fields' focus is correctly NOT flagged as views-related")


def test_focus_is_views_related_true_for_views_round():
    assert _this_rounds_focus_is_views_related(_VIEWS_FOCUS_GOAL) is True
    print("PASS: 'student_views' focus is correctly flagged as views-related")


def test_strips_premature_views_xml_and_its_manifest_reference():
    generated = _make_generated(_REAL_VIEWS_XML)
    assert "views/views.xml" in generated.manifest_py
    _autofix_strip_premature_views_content_on_decomposed_round(generated, _REAL_SCHOOL_STUDENT_GOAL)
    assert generated.views_xml == ""
    assert "views/views.xml" not in generated.manifest_py
    print("PASS: premature views_xml content and its manifest reference are both stripped, "
          "closing the real 5-round non-convergent loop found live on school_student")


def test_does_not_strip_on_the_rounds_own_views_focused_round():
    generated = _make_generated(_REAL_VIEWS_XML)
    _autofix_strip_premature_views_content_on_decomposed_round(generated, _VIEWS_FOCUS_GOAL)
    assert generated.views_xml == _REAL_VIEWS_XML
    assert "views/views.xml" in generated.manifest_py
    print("PASS: a round whose OWN focus is the views work is never stripped")


def test_does_not_strip_on_a_plain_non_decomposed_task():
    generated = _make_generated(_REAL_VIEWS_XML)
    _autofix_strip_premature_views_content_on_decomposed_round(generated, _PLAIN_GOAL)
    assert generated.views_xml == _REAL_VIEWS_XML
    print("PASS: a plain, non-decomposed task's real views content is never touched")


def test_no_op_when_there_is_no_views_xml_at_all():
    generated = _make_generated(None)
    _autofix_strip_premature_views_content_on_decomposed_round(generated, _REAL_SCHOOL_STUDENT_GOAL)
    assert not generated.views_xml
    print("PASS: a round with no views_xml at all is a clean no-op")


_SECURITY_GROUPS_FOCUS_GOAL = (
    "Build a complete, clean, and simple custom module called school_student. "
    "This round's own NEW focus is ONLY: 'security_groups'. The following constraints are "
    "NOT yet in scope for this round and must NOT be implemented even partially: "
    "['record_rules', 'computed_age_field', 'demo_data', 'automated_tests']."
)


def test_any_view_related_constraint_already_satisfied_true_for_menu_structure():
    assert _any_view_related_constraint_already_satisfied({
        "student_model_fields": "satisfied", "student_views": "satisfied",
        "menu_structure": "satisfied", "security_groups": "pending",
    }) is True
    print("PASS: menu_structure=satisfied is correctly recognized as views-related content "
          "that must be preserved")


def test_any_view_related_constraint_already_satisfied_false_when_none_satisfied_yet():
    assert _any_view_related_constraint_already_satisfied({
        "student_model_fields": "satisfied", "student_views": "pending", "menu_structure": "pending",
    }) is False
    print("PASS: no false positive when no views/menu-shaped constraint has been satisfied yet")


def test_does_not_regress_already_satisfied_menu_structure_on_a_later_round():
    """Real, confirmed bug found live (2026-07-29, Phase 28C,
    school_student task, security_groups round): with menu_structure
    already satisfied, the very next round (security_groups) correctly
    carried the existing menu/action content forward in its own
    regenerated views_xml -- which this stripper then wiped to empty
    AND dropped the manifest reference for, regressing a constraint
    that had already genuinely passed. Confirmed live via Code-Review's
    own real complaint: "File is empty, regressing the already-
    satisfied menu_structure constraint by removing the required
    menu_school_root/menu_school_students/action_school_student
    content."
    """
    generated = _make_generated(_REAL_VIEWS_XML)
    constraint_status = {
        "student_model_fields": "satisfied", "student_views": "satisfied",
        "menu_structure": "satisfied", "security_groups": "pending",
        "record_rules": "pending", "computed_age_field": "pending",
        "demo_data": "pending", "automated_tests": "pending",
    }
    _autofix_strip_premature_views_content_on_decomposed_round(
        generated, _SECURITY_GROUPS_FOCUS_GOAL, constraint_status,
    )
    assert generated.views_xml == _REAL_VIEWS_XML, "already-earned menu/view content must survive"
    assert "views/views.xml" in generated.manifest_py
    print("PASS: already-satisfied menu_structure content is preserved on a later, unrelated "
          "round, closing the real live regression")


def test_restores_dropped_views_content_when_build_never_regenerated_it():
    """Real, confirmed bug found live (2026-07-29, Phase 28C,
    school_student task, security_groups round): distinct from the
    stripping bug above -- here Build's OWN regeneration emitted an
    empty views_xml directly (nothing for the stripper to strip),
    still correctly rejected by Code-Review for regressing
    menu_structure. "Don't destroy existing content" doesn't help when
    the content was never regenerated in the first place; this needs
    an actual RESTORE from the prior, already-committed round's real
    content.
    """
    generated = _make_generated(None)  # Build wrote nothing this round
    constraint_status = {
        "student_model_fields": "satisfied", "student_views": "satisfied",
        "menu_structure": "satisfied", "security_groups": "pending",
    }
    _autofix_restore_dropped_views_content_when_already_satisfied(
        generated, constraint_status, _REAL_VIEWS_XML,
    )
    assert generated.views_xml == _REAL_VIEWS_XML
    assert "views/views.xml" in generated.manifest_py
    print("PASS: already-satisfied views content Build simply never regenerated is restored "
          "from the prior committed round's own real content")


def test_never_overwrites_content_build_genuinely_did_write():
    generated = _make_generated("<odoo><record id='new_thing' model='ir.ui.view'/></odoo>")
    constraint_status = {"menu_structure": "satisfied"}
    _autofix_restore_dropped_views_content_when_already_satisfied(
        generated, constraint_status, _REAL_VIEWS_XML,
    )
    assert generated.views_xml == "<odoo><record id='new_thing' model='ir.ui.view'/></odoo>"
    print("PASS: never overwrites content Build genuinely did write this round")


def test_never_restores_when_nothing_views_related_is_satisfied_yet():
    generated = _make_generated(None)
    constraint_status = {"student_model_fields": "satisfied", "menu_structure": "pending"}
    _autofix_restore_dropped_views_content_when_already_satisfied(
        generated, constraint_status, _REAL_VIEWS_XML,
    )
    assert not generated.views_xml
    print("PASS: never restores when no views-related constraint has genuinely been satisfied yet")


def test_never_restores_when_no_prior_content_exists():
    generated = _make_generated(None)
    constraint_status = {"menu_structure": "satisfied"}
    _autofix_restore_dropped_views_content_when_already_satisfied(generated, constraint_status, "")
    assert not generated.views_xml
    print("PASS: a no-op when there's no prior committed content to restore from")


def test_still_strips_genuinely_premature_views_when_nothing_views_related_is_satisfied():
    generated = _make_generated(_REAL_VIEWS_XML)
    constraint_status = {"student_model_fields": "satisfied", "student_views": "pending"}
    _autofix_strip_premature_views_content_on_decomposed_round(
        generated, _REAL_SCHOOL_STUDENT_GOAL, constraint_status,
    )
    assert generated.views_xml == ""
    print("PASS: the original fix still works -- genuinely premature content (nothing views-"
          "related satisfied yet) is still correctly stripped")


_REAL_SCHOOL_STUDENT_MODELS_PY = (
    "from odoo import models, fields, api\n\n"
    "class SchoolStudent(models.Model):\n"
    "    _name = 'school.student'\n\n"
    "    name = fields.Char(required=True)\n"
    "    date_of_birth = fields.Date()\n"
    "    age = fields.Integer(compute='_compute_age', store=True)\n\n"
    "    @api.depends('date_of_birth')\n"
    "    def _compute_age(self):\n"
    "        for rec in self:\n"
    "            rec.age = 0\n"
)


def test_strips_a_premature_computed_field_named_by_a_not_yet_in_scope_label():
    """Real, confirmed bug found live (2026-07-28, Phase 28C,
    `school_student` task's own first live end-to-end run): a round
    scoped to ONLY `student_model_fields`, with `computed_age_field`
    explicitly excluded, still generated the `age` computed field and
    its `_compute_age` method every round, correctly rejected by
    Code-Review every time, never converging.
    """
    generated = _make_generated(None)
    generated.models_py = _REAL_SCHOOL_STUDENT_MODELS_PY
    _autofix_strip_premature_computed_field_on_decomposed_round(generated, _REAL_SCHOOL_STUDENT_GOAL)
    assert "age" not in generated.models_py, generated.models_py
    assert "_compute_age" not in generated.models_py
    assert "name = fields.Char" in generated.models_py, "unrelated, in-scope fields must never be touched"
    print("PASS: a premature computed field named by a not-yet-in-scope label is stripped, "
          "unrelated in-scope fields untouched")


def test_does_not_strip_computed_field_on_the_rounds_own_focused_round():
    computed_focus_goal = (
        "Build school_student. This round's own NEW focus is ONLY: 'computed_age_field'. "
        "The following constraints are NOT yet in scope for this round and must NOT be "
        "implemented even partially: []."
    )
    generated = _make_generated(None)
    generated.models_py = _REAL_SCHOOL_STUDENT_MODELS_PY
    _autofix_strip_premature_computed_field_on_decomposed_round(generated, computed_focus_goal)
    assert "age = fields.Integer" in generated.models_py
    assert "_compute_age" in generated.models_py
    print("PASS: a round whose OWN focus is the computed field itself is never stripped")


if __name__ == "__main__":
    test_focus_is_views_related_false_for_model_round()
    test_focus_is_views_related_true_for_views_round()
    test_strips_premature_views_xml_and_its_manifest_reference()
    test_does_not_strip_on_the_rounds_own_views_focused_round()
    test_does_not_strip_on_a_plain_non_decomposed_task()
    test_no_op_when_there_is_no_views_xml_at_all()
    test_any_view_related_constraint_already_satisfied_true_for_menu_structure()
    test_any_view_related_constraint_already_satisfied_false_when_none_satisfied_yet()
    test_does_not_regress_already_satisfied_menu_structure_on_a_later_round()
    test_restores_dropped_views_content_when_build_never_regenerated_it()
    test_never_overwrites_content_build_genuinely_did_write()
    test_never_restores_when_nothing_views_related_is_satisfied_yet()
    test_never_restores_when_no_prior_content_exists()
    test_still_strips_genuinely_premature_views_when_nothing_views_related_is_satisfied()
    test_strips_a_premature_computed_field_named_by_a_not_yet_in_scope_label()
    test_does_not_strip_computed_field_on_the_rounds_own_focused_round()
    print("\nALL BUILD PREMATURE-VIEWS-STRIP TESTS PASSED")
