"""P11 Tier A (docs/planning/PHASE30_OMA_SEAM_COVERAGE_AND_COMBINATORIAL_HARDENING_2026-07-29.md
§18b.2 items 3-17; full detail: PHASE30_ADDENDUM_GENERIC_FIX_BUILDOUT_2026-07-30.md §2.3): unit
tests for all 15 Tier A validators/autofixes. Every function here is pure/synchronous, zero LLM/
GPU/network calls -- except item 15's test, which mocks the one live-query call it extends.
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import specialists.build.specialist as specialist_module
from specialists.build.specialist import (
    GeneratedModuleFiles,
    ManifestFields,
    _ALREADY_SATISFIED_BY_COLLISION_MARKER,
    _autofix_boolean_field_string_default,
    _autofix_goal_named_computed_field_missing_declaration,
    _autofix_strip_premature_out_of_scope_content_generic,
    _validate_api_constrains_fields_exist,
    _validate_compute_field_body_only_reads_declared_fields,
    _validate_credential_suffix_field_lacks_view_groups_restriction,
    _validate_goal_named_action_method_is_stub_only,
    _validate_model_identity_choice_matches_goal_intent,
    _validate_models_py_has_real_non_comment_content,
    _validate_models_py_touches_goal_target_model,
    _validate_no_direct_inherit_extension_of_res_groups,
    _validate_no_stdlib_datetime_now_for_date_comparisons,
    _validate_no_duplicate_field_declarations_in_same_class,
    _validate_no_duplicate_top_level_model_class_blocks,
    _validate_generated_python_files_are_valid_syntax,
    _validate_no_bare_import_of_odoo_names,
    _validate_no_duplicate_security_csv_ids,
    _validate_no_new_field_collides_with_real_target_field,
    _validate_no_orphaned_compute_methods,
    _validate_security_csv_not_empty,
)

_MANIFEST = ManifestFields(
    name="x", version="1.0.0", category="Tools", summary="x", author="x", depends=["base"], data=[],
)


def _gen(models_py="", views_xml=None, security_csv="x", security_xml=None) -> GeneratedModuleFiles:
    return GeneratedModuleFiles(
        manifest_fields=_MANIFEST, models_py=models_py, views_xml=views_xml,
        security_csv=security_csv, security_xml=security_xml, notes="",
    )


def _raises(fn, *args):
    try:
        fn(*args)
        return None
    except ValueError as exc:
        return str(exc)


# --- Item 3: _validate_models_py_has_real_non_comment_content ---

def test_item3_raises_on_empty_class_body():
    models_py = "class X(models.Model):\n    _name = 'x.model'\n    # nothing else\n"
    assert _raises(_validate_models_py_has_real_non_comment_content, _gen(models_py))
    print("PASS item3: an empty class body (comments only) raises")


def test_item3_never_raises_with_real_field():
    models_py = "class X(models.Model):\n    _name = 'x.model'\n    amount = fields.Integer()\n"
    assert _raises(_validate_models_py_has_real_non_comment_content, _gen(models_py)) is None
    print("PASS item3: a class with a real field declaration never raises")


def test_item3_raises_when_models_py_has_no_class_at_all():
    """Real, confirmed gap found live overnight (2026-08-14 direction-certification run):
    _empty_model_class_blocks() only iterates over MATCHED class blocks -- if models_py
    contains zero `class X(models.Y):` declarations at all (not even an empty one), the
    loop never runs and returns [], so the ORIGINAL check (test_item3_raises_on_empty_class_body
    above) never fires. Confirmed live: a real task ("add a field to crm.lead in a brand
    new module") produced a models.py containing only `from odoo import fields, models` --
    zero classes -- that was still marked installed/passed end to end, exactly the class of
    silent failure this whole validator exists to prevent.
    """
    models_py = "from odoo import fields, models\n"
    result = _raises(_validate_models_py_has_real_non_comment_content, _gen(models_py))
    assert result is not None, "a models.py with zero model classes at all must raise, not silently pass"
    assert "no model class at all" in result
    print("PASS item3: a models.py with zero classes at all (not just an empty one) now raises")


def test_item3_still_exempts_the_legitimate_security_only_shape_with_no_class():
    """The fix above must not break the real, documented exception this function already has:
    a goal restricting visibility via a real groups-attribute edit needs no model class at all,
    and that's the CORRECT answer, not a bug -- confirm the new check still respects it.
    """
    models_py = "from odoo import fields, models\n"
    generated = _gen(
        models_py,
        views_xml=(
            '<record id="x" model="ir.ui.view">'
            '<field name="arch" type="xml">'
            '<xpath expr="//field[@name=\'x\']" position="attributes">'
            '<attribute name="groups">base.group_system</attribute>'
            "</xpath></field></record>"
        ),
    )
    assert _raises(_validate_models_py_has_real_non_comment_content, generated) is None
    print("PASS item3: a genuine security-only goal with a real groups-attribute edit still needs no class")


# --- Item 3 + real-field-collision interaction (2026-08-02 fix) ---
#
# Real, confirmed bug found live during the full-30-task sweep: 6 of 7 real "empty class body"
# failures traced to the exact same root cause -- a single-field-add task whose one field already
# exists for real on the target model (leftover from an earlier real attempt at this SAME goal on
# this SAME shared, persistent odoo16_dev database -- never reset between benchmark runs).
# `_validate_no_new_field_collides_with_real_target_field`'s own autofix correctly removes the
# colliding field, but for a plain (non-computed) field that leaves NOTHING else in the class --
# which `_validate_models_py_has_real_non_comment_content` (a later, unrelated step in the same
# chain) then wrongly rejected as broken, even though the round is genuinely already satisfied.

def test_collision_that_empties_the_class_marks_it_already_satisfied_not_broken(monkeypatch):
    import tools_odoo.odoo_schema_client as _schema_client

    def fake_get_model_fields_fast(target, db):
        return {"special_instructions": {}}
    monkeypatch.setattr(_schema_client, "get_model_fields_fast", fake_get_model_fields_fast)
    monkeypatch.setattr(specialist_module, "is_fast_path_eligible", lambda db: True)
    old_models_py = "class Lead(models.Model):\n    _inherit = 'crm.lead'\n"
    new_models_py = (
        "class Lead(models.Model):\n"
        "    _inherit = 'crm.lead'\n"
        "    special_instructions = fields.Text(string='Special instructions')\n"
    )
    generated = _gen(new_models_py)
    asyncio.run(_validate_no_new_field_collides_with_real_target_field(
        generated, old_models_py, "odoo16_dev", task_id="test",
    ))
    assert "special_instructions" not in generated.models_py, "the colliding field must still be removed"
    assert _ALREADY_SATISFIED_BY_COLLISION_MARKER in generated.notes, (
        "the collision-emptied-the-class case must be marked so the content validator can tell "
        "it apart from a genuinely broken empty class"
    )
    # The real, decisive proof: item 3's own validator must now stay silent for this exact case.
    assert _raises(_validate_models_py_has_real_non_comment_content, generated) is None
    print("PASS: a real field collision that empties the class is marked already-satisfied, "
          "not wrongly rejected as a broken empty round")


def test_genuinely_empty_class_with_no_collision_still_raises():
    # Regression guard: the marker must never leak into an ordinary empty-class failure that has
    # nothing to do with a real collision -- a genuine LLM miss must still be caught exactly as
    # before this fix.
    models_py = "class X(models.Model):\n    _name = 'x.model'\n    # nothing else\n"
    generated = _gen(models_py)
    assert _ALREADY_SATISFIED_BY_COLLISION_MARKER not in (generated.notes or "")
    assert _raises(_validate_models_py_has_real_non_comment_content, generated), (
        "a genuinely empty class with no collision involved at all must still be rejected"
    )
    print("PASS: a genuinely empty class body with no real collision is still correctly rejected")


# --- Item 3 + visibility-only (groups=) edits (2026-08-03 fix, task008) ---
# Real, confirmed bug found live: a goal restricting WHO can see a button/field ("only System
# Administrators should see this button") is correctly solved entirely in views_xml (a real
# <attribute name="groups">...</attribute> edit) -- genuinely no model field is needed, and an
# empty models.py is the CORRECT answer, not a broken one.

def test_visibility_groups_attribute_edit_exempts_an_empty_class():
    models_py = (
        "class Meerwerk(models.Model):\n"
        "    _inherit = 'meerwerk'\n"
        "    # No new fields defined here as per the constraint\n"
        "    # The button visibility is controlled via security rules\n"
    )
    views_xml = (
        '<odoo><record id="view_meerwerk_form_inherit" model="ir.ui.view">'
        '<field name="arch" type="xml">'
        '<button name="send_to_customer" position="attributes">'
        '<attribute name="groups">base.group_system</attribute>'
        "</button></field></record></odoo>"
    )
    generated = _gen(models_py, views_xml=views_xml)
    assert _raises(_validate_models_py_has_real_non_comment_content, generated) is None
    print("PASS: task008's real button-visibility-only shape (groups= edit in views_xml) is never wrongly rejected")


def test_genuinely_empty_class_with_no_groups_edit_still_raises():
    models_py = "class X(models.Model):\n    _name = 'x.model'\n    # nothing else\n"
    generated = _gen(models_py, views_xml="<odoo><record><field name='x'/></record></odoo>")
    assert _raises(_validate_models_py_has_real_non_comment_content, generated), (
        "an empty class with no real groups= visibility edit anywhere must still be rejected"
    )
    print("PASS: an empty class with no real groups= edit is still correctly rejected")


# --- Item 3 + new menu visibility restriction (2026-08-05 fix, task014) ---
# Real, confirmed bug found live: "add a menu entry... only managers should see it" is correctly
# solved entirely via a brand new <menuitem groups="..."/> (or the equivalent ir.ui.menu record
# form) -- genuinely no model field is needed, same shape as task008's fix above but for a NEW
# menu entry rather than an edit to an existing one. Confirmed live: all 3 of task014's own real
# rounds were pre-write rejected by this validator for this exact reason.

def test_new_menuitem_shorthand_with_groups_attribute_exempts_an_empty_class():
    models_py = (
        "class ProjectMenu(models.Model):\n"
        "    _name = 'project.menu'\n"
        "    _description = 'Project Menu Entry'\n"
    )
    views_xml = (
        '<odoo><menuitem id="menu_extra_work" name="Extra Work" '
        'parent="project.menu_main_pm" groups="group_extra_work_manager"/></odoo>'
    )
    generated = _gen(models_py, views_xml=views_xml)
    assert _raises(_validate_models_py_has_real_non_comment_content, generated) is None
    print("PASS: task014's real new-menuitem-with-groups shape is never wrongly rejected")


def test_new_ir_ui_menu_record_form_with_groups_id_field_exempts_an_empty_class():
    """The equivalent long-form <record model="ir.ui.menu"> shape must be recognized too, not
    just the <menuitem> shorthand tag."""
    models_py = "class ProjectMenu(models.Model):\n    _name = 'project.menu'\n"
    views_xml = (
        '<odoo><record id="menu_extra_work" model="ir.ui.menu">'
        '<field name="name">Extra Work</field>'
        '<field name="groups_id" eval="[(4, ref(\'group_extra_work_manager\'))]"/>'
        "</record></odoo>"
    )
    generated = _gen(models_py, views_xml=views_xml)
    assert _raises(_validate_models_py_has_real_non_comment_content, generated) is None
    print("PASS: the long-form ir.ui.menu record with a groups_id field is also recognized")


def test_menuitem_with_no_groups_restriction_still_raises():
    """A plain menuitem with no group restriction at all is a genuinely different, real defect
    -- this exception must never swallow it."""
    models_py = "class ProjectMenu(models.Model):\n    _name = 'project.menu'\n"
    views_xml = '<odoo><menuitem id="menu_extra_work" name="Extra Work" parent="project.menu_main_pm"/></odoo>'
    generated = _gen(models_py, views_xml=views_xml)
    assert _raises(_validate_models_py_has_real_non_comment_content, generated), (
        "a menuitem with no real groups= restriction at all must still be rejected"
    )
    print("PASS: a menuitem with no groups restriction at all is still correctly rejected")


def test_groups_id_field_on_an_unrelated_record_does_not_exempt():
    """Scoped precisely to ir.ui.menu -- a groups_id-shaped field on a totally unrelated record
    model must never be misread as a menu visibility restriction."""
    models_py = "class X(models.Model):\n    _name = 'x.model'\n    # nothing else\n"
    views_xml = (
        '<odoo><record id="some_user" model="res.users">'
        '<field name="groups_id" eval="[(4, ref(\'base.group_user\'))]"/>'
        "</record></odoo>"
    )
    generated = _gen(models_py, views_xml=views_xml)
    assert _raises(_validate_models_py_has_real_non_comment_content, generated), (
        "groups_id on a non-menu record must never exempt an unrelated empty class"
    )
    print("PASS: groups_id on a non-ir.ui.menu record does not falsely exempt")


def test_collision_that_leaves_other_real_content_still_gets_marked():
    # Real, confirmed follow-on gap found live (2026-08-03, re-testing this fix on task021):
    # the marker used to only fire when collision-stripping left the class TOTALLY empty -- but
    # a round can have real, genuinely new content (task021's own real `create()` override + 2
    # action methods) while STILL having one or more individual fields stripped by this same
    # collision. views_xml (built earlier) still references those exact stripped field names,
    # and the disposable sandbox pre-flight has no memory of the real target's own state -- it
    # crashes on them regardless of whether the class as a whole is empty. The marker's real
    # condition was never "is the class now empty," it's "were any fields removed by a confirmed
    # real collision at all" -- so it must fire here too, even with real content surviving.
    import tools_odoo.odoo_schema_client as _schema_client

    def fake_get_model_fields_fast(target, db):
        return {"special_instructions": {}}
    orig = _schema_client.get_model_fields_fast
    orig_fast_path = specialist_module.is_fast_path_eligible
    _schema_client.get_model_fields_fast = fake_get_model_fields_fast
    specialist_module.is_fast_path_eligible = lambda db: True
    try:
        old_models_py = "class Lead(models.Model):\n    _inherit = 'crm.lead'\n"
        new_models_py = (
            "class Lead(models.Model):\n"
            "    _inherit = 'crm.lead'\n"
            "    special_instructions = fields.Text(string='Special instructions')\n"
            "    another_new_field = fields.Char(string='Another')\n"
        )
        generated = _gen(new_models_py)
        asyncio.run(_validate_no_new_field_collides_with_real_target_field(
            generated, old_models_py, "odoo16_dev", task_id="test",
        ))
        assert "special_instructions" not in generated.models_py
        assert "another_new_field" in generated.models_py, "the non-colliding field must survive"
        assert _ALREADY_SATISFIED_BY_COLLISION_MARKER in (generated.notes or ""), (
            "a real collision was still resolved here -- the sandbox-skip signal must fire even "
            "though other real content survives, since the view still references the stripped field"
        )
    finally:
        _schema_client.get_model_fields_fast = orig
        specialist_module.is_fast_path_eligible = orig_fast_path
    print("PASS: a collision that leaves other real content behind still gets marked -- the "
          "sandbox-skip signal fires on any confirmed real collision, not just a fully-emptied class")


# --- Item 4: _validate_models_py_touches_goal_target_model ---

def test_item4_raises_when_goal_model_never_declared():
    goal = "Model: x.model\nAdd a field."
    models_py = "class Y(models.Model):\n    _name = 'y.model'\n"
    assert _raises(_validate_models_py_touches_goal_target_model, _gen(models_py), goal, None)
    print("PASS item4: models_py touching the wrong model raises")


def test_item4_never_raises_when_model_matches():
    goal = "Model: x.model\nAdd a field."
    models_py = "class X(models.Model):\n    _name = 'x.model'\n"
    assert _raises(_validate_models_py_touches_goal_target_model, _gen(models_py), goal, None) is None
    print("PASS item4: models_py touching the right model never raises")


def test_item4_skips_when_goal_names_no_model():
    assert _raises(_validate_models_py_touches_goal_target_model, _gen("class X: pass"), "Add a field.", None) is None
    print("PASS item4: no Model: line at all -- skipped, never guessed")


def test_item4_skips_genuinely_views_only_quick_filter_round():
    """Real, confirmed false-positive found live (2026-08-12, reinstall sample sweep, task043 --
    an on-screen 'Report Preview' tab, the same views-only shape as the already-proven task022):
    this validator never shared the sibling item-3 validator's own genuinely-views-only exemptions,
    so a round that correctly leaves models_py untouched (a quick filter, a groups= visibility
    edit, etc.) was wrongly rejected for not declaring the goal's own Model: line target."""
    goal = "Model: project.meerwerk\nAdd a quick filter button to the list."
    generated = _gen("class X: pass")
    generated.views_xml = (
        "<odoo><record id=\"x\" model=\"ir.ui.view\">"
        "<field name=\"arch\" type=\"xml\"><xpath expr=\"//search\" position=\"inside\">"
        "<filter name=\"accepted\" string=\"Accepted\" domain=\"[('state','=','accepted')]\"/>"
        "</xpath></field></record></odoo>"
    )
    assert _raises(_validate_models_py_touches_goal_target_model, generated, goal, None) is None
    print("PASS item4: a genuinely views-only round (quick filter) is not forced to also "
          "declare the goal's Model: line target in models_py")


def test_item4_still_raises_when_field_addition_genuinely_forgotten():
    """The fix above must stay narrow: a goal needing a real field/method addition, where
    models_py never touches the target model AND no views-only exemption signal is present,
    is still a genuine miss and must still raise."""
    goal = "Model: project.meerwerk\nAdd a many2one field linking to the invoice."
    generated = _gen("class X: pass")
    generated.views_xml = "<odoo></odoo>"
    assert _raises(_validate_models_py_touches_goal_target_model, generated, goal, None)
    print("PASS item4: a genuine forgotten-field miss (no views-only signal) still raises")


# --- Item 5: _validate_model_identity_choice_matches_goal_intent ---

def test_item5_raises_when_new_model_intent_but_no_name():
    goal = "Create a new model to track widgets."
    models_py = "class X(models.Model):\n    _inherit = 'res.partner'\n"
    assert _raises(_validate_model_identity_choice_matches_goal_intent, _gen(models_py), goal, None)
    print("PASS item5: unambiguous new-model intent with only _inherit raises")


def test_item5_raises_when_extend_intent_but_new_name_no_inherit():
    goal = "Extend the existing res.partner model with a new field."
    models_py = "class X(models.Model):\n    _name = 'x.new.model'\n"
    assert _raises(_validate_model_identity_choice_matches_goal_intent, _gen(models_py), goal, None)
    print("PASS item5: unambiguous extend intent with only _name raises")


def test_item5_skips_when_intent_ambiguous():
    goal = "Add a field to track widgets."  # neither pattern matches
    models_py = "class X(models.Model):\n    _name = 'x.model'\n"
    assert _raises(_validate_model_identity_choice_matches_goal_intent, _gen(models_py), goal, None) is None
    print("PASS item5: ambiguous/unstated intent is skipped, never guessed")


def test_item5_never_raises_when_correct():
    goal = "Extend the existing res.partner model with a new field."
    models_py = "class X(models.Model):\n    _inherit = 'res.partner'\n"
    assert _raises(_validate_model_identity_choice_matches_goal_intent, _gen(models_py), goal, None) is None
    print("PASS item5: correct extend usage never raises")


def test_item5_recognizes_custom_model_phrasing_not_just_new():
    """Real, confirmed gap found live (2026-08-12, reinstall sample sweep, task034): "Create a
    custom payment term model called payment.term.cust" is unambiguous new-model intent in plain
    manager-request phrasing, but the original regex only matched "new model", not "custom
    model", so Build's real _inherit-instead-of-_name mistake went uncaught here."""
    goal = "Create a custom payment term model called payment.term.cust with installment lines."
    models_py = "class X(models.Model):\n    _inherit = 'payment.term.cust'\n"
    assert _raises(_validate_model_identity_choice_matches_goal_intent, _gen(models_py), goal, None)
    print("PASS item5: 'create a custom ... model' phrasing is recognized as new-model intent")


def test_inherit_res_groups_raises():
    """Real, confirmed general bug found live (2026-08-12, day-to-day directions sweep, Batch C):
    Build repeatedly wrote _inherit = 'res.groups' to add a field instead of a real res.groups
    XML record, confirmed on two unrelated menu/group-restriction scenarios."""
    models_py = (
        "from odoo import models, fields\n\n"
        "class ResGroups(models.Model):\n    _inherit = 'res.groups'\n\n"
        "    is_fleet_contract_viewer = fields.Boolean()\n"
    )
    assert _raises(_validate_no_direct_inherit_extension_of_res_groups, _gen(models_py))
    print("PASS: _inherit = 'res.groups' raises")


def test_inherit_res_groups_never_raises_when_absent():
    models_py = "from odoo import models, fields\n\nclass X(models.Model):\n    _inherit = 'project.project'\n"
    assert _raises(_validate_no_direct_inherit_extension_of_res_groups, _gen(models_py)) is None
    print("PASS: extending an unrelated model never raises")


def test_datetime_now_raises():
    """Real, confirmed bug found live (2026-08-12, reinstall sample sweep, task018): Code-Review
    independently confirmed datetime.now() used in a cron method comparing against a fields.Date
    column -- ignores Odoo's own timezone/DST handling."""
    models_py = (
        "from odoo import models, fields, api\nimport datetime\n\n"
        "class X(models.Model):\n    _inherit = 'project.meerwerk'\n\n"
        "    def _auto_reject_old(self):\n        cutoff = datetime.datetime.now()\n"
    )
    assert _raises(_validate_no_stdlib_datetime_now_for_date_comparisons, _gen(models_py))
    print("PASS: datetime.now() usage raises")


def test_datetime_utcnow_raises():
    models_py = "class X(models.Model):\n    def f(self):\n        x = datetime.utcnow()\n"
    assert _raises(_validate_no_stdlib_datetime_now_for_date_comparisons, _gen(models_py))
    print("PASS: datetime.utcnow() usage raises")


def test_datetime_now_never_raises_when_absent():
    models_py = (
        "from odoo import models, fields, api\nfrom dateutil.relativedelta import relativedelta\n\n"
        "class X(models.Model):\n    _inherit = 'project.meerwerk'\n\n"
        "    def _auto_reject_old(self):\n        cutoff = fields.Date.today() - relativedelta(days=30)\n"
    )
    assert _raises(_validate_no_stdlib_datetime_now_for_date_comparisons, _gen(models_py)) is None
    print("PASS: correct fields.Date.today() usage never raises")


# --- Item 6: _validate_no_orphaned_compute_methods ---

def test_item6_raises_on_orphaned_compute_method():
    models_py = (
        "class X(models.Model):\n"
        "    _name = 'x.model'\n"
        "    def _compute_total(self):\n        pass\n"
    )
    assert _raises(_validate_no_orphaned_compute_methods, _gen(models_py))
    print("PASS item6: a _compute_* method with no field referencing it via compute= raises")


def test_item6_never_raises_when_wired():
    models_py = (
        "class X(models.Model):\n"
        "    _name = 'x.model'\n"
        "    total = fields.Integer(compute='_compute_total')\n"
        "    def _compute_total(self):\n        pass\n"
    )
    assert _raises(_validate_no_orphaned_compute_methods, _gen(models_py)) is None
    print("PASS item6: a compute method referenced by a field's compute= never raises")


# --- Item 7: _validate_compute_field_body_only_reads_declared_fields ---

def test_item7_raises_on_invented_field_read():
    models_py = (
        "class X(models.Model):\n"
        "    _name = 'x.model'\n"
        "    total = fields.Integer(compute='_compute_total')\n"
        "    def _compute_total(self):\n"
        "        self.total = self.nonexistent_field\n"
    )
    assert _raises(_validate_compute_field_body_only_reads_declared_fields, _gen(models_py))
    print("PASS item7: reading an undeclared field inside a compute method raises")


def test_item7_skips_on_inherit():
    models_py = (
        "class X(models.Model):\n"
        "    _inherit = 'res.partner'\n"
        "    def _compute_total(self):\n"
        "        self.total = self.some_base_field\n"
    )
    assert _raises(_validate_compute_field_body_only_reads_declared_fields, _gen(models_py)) is None
    print("PASS item7: an _inherit class is skipped entirely -- can't see base fields without a live query")


def test_item7_never_raises_on_declared_field():
    models_py = (
        "class X(models.Model):\n"
        "    _name = 'x.model'\n"
        "    amount = fields.Integer()\n"
        "    total = fields.Integer(compute='_compute_total')\n"
        "    def _compute_total(self):\n"
        "        self.total = self.amount\n"
    )
    assert _raises(_validate_compute_field_body_only_reads_declared_fields, _gen(models_py)) is None
    print("PASS item7: reading a real declared field never raises")


# --- Item 8: default= vs compute= extension inside the existing autofix ---

def test_item8_raises_when_goal_facts_says_computed_but_declared_with_default():
    goal = "Field name: total\n"
    models_py = (
        "class X(models.Model):\n"
        "    _name = 'x.model'\n"
        "    total = fields.Integer(default=0)\n"
    )
    goal_facts = {"field_name": "total", "is_computed": True}
    try:
        _autofix_goal_named_computed_field_missing_declaration(_gen(models_py), goal, {}, goal_facts)
        assert False, "expected ValueError"
    except ValueError as exc:
        assert "total" in str(exc) and "default" in str(exc)
    print("PASS item8: a goal_facts-confirmed computed field declared with default= raises")


def test_item8_never_raises_when_correctly_wired():
    goal = "Field name: total\n"
    models_py = (
        "class X(models.Model):\n"
        "    _name = 'x.model'\n"
        "    total = fields.Integer(compute='_compute_total')\n"
    )
    goal_facts = {"field_name": "total", "is_computed": True}
    _autofix_goal_named_computed_field_missing_declaration(_gen(models_py), goal, {}, goal_facts)  # must not raise
    print("PASS item8: a correctly compute=-wired field never raises")


# --- Item 9: _autofix_strip_premature_out_of_scope_content_generic ---

def test_item9_strips_premature_field_matching_scope_keyword():
    goal = "Add the amount field. NOT yet in scope for this round: ['discount_rate']"
    models_py = (
        "class X(models.Model):\n"
        "    _name = 'x.model'\n"
        "    amount = fields.Integer()\n"
        "    discount_rate = fields.Float()\n"
    )
    generated = _gen(models_py)
    _autofix_strip_premature_out_of_scope_content_generic(generated, goal, {})
    assert "discount_rate" not in generated.models_py
    assert "amount = fields.Integer()" in generated.models_py
    print("PASS item9: a premature field matching the round's own not-yet-in-scope keyword is stripped")


def test_item9_never_fires_on_short_keyword():
    goal = "Add the amount field. NOT yet in scope for this round: ['abc']"
    models_py = "class X(models.Model):\n    _name = 'x.model'\n    abc = fields.Integer()\n"
    generated = _gen(models_py)
    _autofix_strip_premature_out_of_scope_content_generic(generated, goal, {})
    assert "abc = fields.Integer()" in generated.models_py
    print("PASS item9: a keyword shorter than the minimum specificity guard never fires")


def test_item9_noop_when_no_not_yet_in_scope_line():
    models_py = "class X(models.Model):\n    _name = 'x.model'\n    amount = fields.Integer()\n"
    generated = _gen(models_py)
    _autofix_strip_premature_out_of_scope_content_generic(generated, "Add the amount field.", {})
    assert generated.models_py == models_py
    print("PASS item9: no not-yet-in-scope line at all -- no-op")


# --- Item 10: _validate_goal_named_action_method_is_stub_only ---

def test_item10_raises_on_stub_body():
    goal = "Action method: do_confirm\n"
    models_py = (
        "class X(models.Model):\n"
        "    _name = 'x.model'\n"
        "    def do_confirm(self):\n"
        "        pass\n"
    )
    assert _raises(_validate_goal_named_action_method_is_stub_only, _gen(models_py), goal, None)
    print("PASS item10: a goal-named action method with a pass-only body raises")


def test_item10_never_raises_with_real_body():
    goal = "Action method: do_confirm\n"
    models_py = (
        "class X(models.Model):\n"
        "    _name = 'x.model'\n"
        "    def do_confirm(self):\n"
        "        self.state = 'confirmed'\n"
    )
    assert _raises(_validate_goal_named_action_method_is_stub_only, _gen(models_py), goal, None) is None
    print("PASS item10: a real, non-stub method body never raises")


def test_item10_skips_when_method_not_declared():
    goal = "Action method: do_confirm\n"
    assert _raises(_validate_goal_named_action_method_is_stub_only, _gen("class X: pass"), goal, None) is None
    print("PASS item10: method not present at all -- a different check's job, skipped here")


# --- Item 11: _validate_credential_suffix_field_lacks_view_groups_restriction ---

def test_item11_raises_on_unrestricted_credential_field():
    models_py = "class X(models.Model):\n    _name = 'x.model'\n    access_token = fields.Char()\n"
    views_xml = '<form><field name="access_token"/></form>'
    assert _raises(
        _validate_credential_suffix_field_lacks_view_groups_restriction, _gen(models_py, views_xml=views_xml),
    )
    print("PASS item11: a credential-suffix field rendered without groups= raises")


def test_item11_never_raises_on_restricted_field():
    models_py = "class X(models.Model):\n    _name = 'x.model'\n    access_token = fields.Char()\n"
    views_xml = '<form><field name="access_token" groups="base.group_system"/></form>'
    assert _raises(
        _validate_credential_suffix_field_lacks_view_groups_restriction, _gen(models_py, views_xml=views_xml),
    ) is None
    print("PASS item11: a groups=-restricted credential field never raises")


def test_item11_never_false_positives_on_description_suffix():
    # api_key_description ends '_description', never 'token'/'secret'/'password'/'passwd' --
    # must never match, the exact false-positive class an earlier draft was rejected for.
    models_py = "class X(models.Model):\n    _name = 'x.model'\n    api_key_description = fields.Char()\n"
    views_xml = '<form><field name="api_key_description"/></form>'
    assert _raises(
        _validate_credential_suffix_field_lacks_view_groups_restriction, _gen(models_py, views_xml=views_xml),
    ) is None
    print("PASS item11: api_key_description never false-positives (trailing segment is 'description', not a match)")


def test_item11_never_fires_on_bare_key_suffix():
    models_py = "class X(models.Model):\n    _name = 'x.model'\n    primary_key = fields.Char()\n"
    views_xml = '<form><field name="primary_key"/></form>'
    assert _raises(
        _validate_credential_suffix_field_lacks_view_groups_restriction, _gen(models_py, views_xml=views_xml),
    ) is None
    print("PASS item11: bare 'key' suffix is deliberately excluded, never matches")


# --- Item 12: _validate_no_duplicate_security_csv_ids ---

def test_item12_raises_on_duplicate_id():
    csv = (
        "id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink\n"
        "access_x,x,model_x,base.group_user,1,1,1,0\n"
        "access_x,x2,model_x,base.group_system,1,1,1,1\n"
    )
    assert _raises(_validate_no_duplicate_security_csv_ids, _gen(security_csv=csv))
    print("PASS item12: a repeated CSV id raises")


def test_item12_never_raises_without_duplicates():
    csv = (
        "id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink\n"
        "access_x,x,model_x,base.group_user,1,1,1,0\n"
    )
    assert _raises(_validate_no_duplicate_security_csv_ids, _gen(security_csv=csv)) is None
    print("PASS item12: unique CSV ids never raise")


# --- Item 13: _autofix_boolean_field_string_default ---

def test_item13_fixes_string_true_to_bare_true():
    models_py = "class X(models.Model):\n    _name = 'x.model'\n    active2 = fields.Boolean(default='True')\n"
    generated = _gen(models_py)
    _autofix_boolean_field_string_default(generated)
    assert "default=True" in generated.models_py
    assert "default='True'" not in generated.models_py
    print("PASS item13: a string 'True' default is rewritten to the bare boolean")


def test_item13_fixes_string_false_and_leaves_bare_bool_alone():
    models_py = (
        "class X(models.Model):\n"
        "    _name = 'x.model'\n"
        "    a = fields.Boolean(default='False')\n"
        "    b = fields.Boolean(default=True)\n"
    )
    generated = _gen(models_py)
    _autofix_boolean_field_string_default(generated)
    assert "a = fields.Boolean(default=False)" in generated.models_py
    assert "b = fields.Boolean(default=True)" in generated.models_py
    print("PASS item13: string False is fixed, an already-bare bool default is left untouched")


# --- Item 14: _validate_api_constrains_fields_exist ---

def test_item14_raises_on_undeclared_constrains_field():
    models_py = (
        "class X(models.Model):\n"
        "    _name = 'x.model'\n"
        "    amount = fields.Integer()\n"
        "    @api.constrains('amount', 'nonexistent_field')\n"
        "    def _check_amount(self):\n        pass\n"
    )
    assert _raises(_validate_api_constrains_fields_exist, _gen(models_py))
    print("PASS item14: @api.constrains naming an undeclared field raises")


def test_item14_never_raises_when_fields_real():
    models_py = (
        "class X(models.Model):\n"
        "    _name = 'x.model'\n"
        "    amount = fields.Integer()\n"
        "    @api.constrains('amount')\n"
        "    def _check_amount(self):\n        pass\n"
    )
    assert _raises(_validate_api_constrains_fields_exist, _gen(models_py)) is None
    print("PASS item14: @api.constrains naming only real declared fields never raises")


def test_item14_skips_on_inherit():
    models_py = (
        "class X(models.Model):\n"
        "    _inherit = 'res.partner'\n"
        "    @api.constrains('some_base_field')\n"
        "    def _check(self):\n        pass\n"
    )
    assert _raises(_validate_api_constrains_fields_exist, _gen(models_py)) is None
    print("PASS item14: an _inherit class is skipped -- can't see base fields without a live query")


# --- Item 15: image.mixin field-name collision extension ---

def test_item15_autofixes_image_mixin_field_collision_even_when_live_query_returns_none(monkeypatch):
    # The live query returning None (uncertain target) must NOT suppress the static
    # image.mixin-name collision -- that's the whole point of item 15's own extension. The
    # existing collision mechanism autofixes cleanly here (removes the colliding declaration)
    # rather than raising, matching its own pre-existing behavior for any other real collision.
    def fake_get_model_fields(target, db):
        return None
    monkeypatch.setattr(specialist_module, "get_model_fields", fake_get_model_fields)
    monkeypatch.setattr(specialist_module, "is_fast_path_eligible", lambda db: False)
    old_models_py = "class X(models.Model):\n    _inherit = 'some.model'\n"
    new_models_py = (
        "class X(models.Model):\n"
        "    _inherit = 'some.model'\n"
        "    image_1920 = fields.Image()\n"
    )
    generated = _gen(new_models_py)
    asyncio.run(_validate_no_new_field_collides_with_real_target_field(generated, old_models_py, "test_db"))
    assert "image_1920" not in generated.models_py
    print("PASS item15: a freshly-declared image_1920 on an _inherit extension is autofixed away even when the live query can't confirm it")


def test_item15_never_raises_on_ordinary_new_field_when_query_returns_none(monkeypatch):
    def fake_get_model_fields(target, db):
        return None
    monkeypatch.setattr(specialist_module, "get_model_fields", fake_get_model_fields)
    monkeypatch.setattr(specialist_module, "is_fast_path_eligible", lambda db: False)
    old_models_py = "class X(models.Model):\n    _inherit = 'some.model'\n"
    new_models_py = (
        "class X(models.Model):\n"
        "    _inherit = 'some.model'\n"
        "    my_new_field = fields.Char()\n"
    )
    generated = _gen(new_models_py)
    asyncio.run(_validate_no_new_field_collides_with_real_target_field(generated, old_models_py, "test_db"))  # must not raise
    print("PASS item15: an ordinary, non-image-mixin new field never raises just because the live query returned None")


# --- Second-read redundancy against the exact real incident shape (2026-08-05, task019) ---
#
# Real, confirmed bug found live: a genuine, from-scratch _inherit extension (old_models_py=""
# -- round 1, nothing committed yet, so this was never a round-diffing gap) redeclared
# `customer_grouping_rule`, a field that WAS real on the live target at generation time.
# `get_model_fields_fast()`'s own live query silently returned None at that exact moment
# (this codebase's own `_read_real_field_rows()` collapses ANY exception, including a transient
# timeout under heavy concurrent XML-RPC load, to None) -- so the check found nothing to flag, and
# the collision reached Code-Review instead, which caught it via the SAME underlying
# `_read_real_field_rows()` function (through `resolve_current_schema_block()`'s own separate call).
# The fix: a second, independent call to that exact same function gives this check its own real
# second chance, rather than depending on a single query attempt succeeding.

def test_second_schema_read_catches_collision_the_primary_fast_query_missed(monkeypatch):
    import tools_odoo.odoo_schema_client as _schema_client

    def fake_get_model_fields_fast(target, db):
        return None  # simulates the exact real incident: the primary query silently failed
    def fake_read_real_field_rows(model_name, db, login):
        # the second, independent read succeeds -- same shape resolve_current_schema_block() uses
        return [{"name": "customer_grouping_rule", "ttype": "boolean"}]
    monkeypatch.setattr(_schema_client, "get_model_fields_fast", fake_get_model_fields_fast)
    monkeypatch.setattr(_schema_client, "_read_real_field_rows", fake_read_real_field_rows)
    monkeypatch.setattr(specialist_module, "is_fast_path_eligible", lambda db: True)
    old_models_py = ""  # genuine round 1 -- nothing committed yet, exactly like the real incident
    new_models_py = (
        "class ProjectMeerwerk(models.Model):\n"
        "    _inherit = 'project.meerwerk'\n"
        "    customer_grouping_rule = fields.Boolean(string='Customer Grouping Rule')\n"
    )
    generated = _gen(new_models_py)
    asyncio.run(_validate_no_new_field_collides_with_real_target_field(
        generated, old_models_py, "odoo16_dev", task_id="test",
    ))
    assert "customer_grouping_rule" not in generated.models_py, (
        "the second, independent schema read must catch and remove the collision even though the "
        "primary fast-path query returned None -- this is the exact real gap found live on task019"
    )
    print("PASS: a collision missed by the primary live query is still caught by the second, "
          "independent read against the same function <current_schema> is built from")


def test_second_schema_read_never_raises_when_both_reads_agree_nothing_real():
    import tools_odoo.odoo_schema_client as _schema_client
    old_models_py = ""
    new_models_py = (
        "class ProjectMeerwerk(models.Model):\n"
        "    _inherit = 'project.meerwerk'\n"
        "    genuinely_new_field = fields.Boolean(string='Genuinely New')\n"
    )
    generated = _gen(new_models_py)
    # No monkeypatching -- real (or realistically-absent) live calls; a genuinely new field name
    # must never be flagged just because a live query ran. Uses is_fast_path_eligible's own real
    # default (False for "test_db"), matching the ordinary slow-path fallback other tests here use.
    asyncio.run(_validate_no_new_field_collides_with_real_target_field(
        generated, old_models_py, "test_db", task_id="test",
    ))
    assert "genuinely_new_field" in generated.models_py, (
        "a genuinely new field name must never be stripped -- got: " + generated.models_py
    )
    print("PASS: a genuinely new field is never flagged by the second independent read either")


# --- Item 16: _validate_no_duplicate_field_declarations_in_same_class ---

def test_item16_raises_on_duplicate_field():
    models_py = (
        "class X(models.Model):\n"
        "    _name = 'x.model'\n"
        "    status = fields.Selection([('a', 'A')])\n"
        "    status = fields.Selection([('b', 'B')])\n"
    )
    assert _raises(_validate_no_duplicate_field_declarations_in_same_class, _gen(models_py))
    print("PASS item16: a field declared twice in the same class raises")


def test_item16_never_raises_without_duplicates():
    models_py = "class X(models.Model):\n    _name = 'x.model'\n    amount = fields.Integer()\n"
    assert _raises(_validate_no_duplicate_field_declarations_in_same_class, _gen(models_py)) is None
    print("PASS item16: no duplicate field declarations never raises")


def test_item16_never_raises_on_repeated_method_body_local_variable_matching_field_shape():
    """Real, confirmed bug found live (2026-08-09, task 07141af5's flagship run,
    project_ticket_counts node): `_MODEL_FIELD_DEF_RE` (this validator's own detection core)
    used to match ANY line assigning to something starting with `fields.` -- including a plain
    method-body local variable like `cutoff = fields.Datetime.now() - ...` (a completely common,
    legitimate Odoo idiom for reading the current date/time), indistinguishable from a real field
    declaration. Two different methods each legitimately using their own local `cutoff = fields.
    Datetime.now() - ...` variable was rejected as declaring the field 'cutoff' twice, when
    neither line is a field declaration at all -- confirmed live via a manager replan explicitly
    flagged `root_cause='pattern_worth_a_rule'` (a real, general defect, not one-off noise).
    """
    models_py = (
        "class X(models.Model):\n"
        "    _name = 'x.model'\n"
        "    amount = fields.Integer()\n\n"
        "    def _compute_a(self):\n"
        "        cutoff = fields.Datetime.now() - timedelta(days=1)\n"
        "        return cutoff\n\n"
        "    def action_b(self):\n"
        "        cutoff = fields.Datetime.now() - timedelta(days=3)\n"
        "        return cutoff\n"
    )
    assert _raises(_validate_no_duplicate_field_declarations_in_same_class, _gen(models_py)) is None
    print("PASS item16: a repeated method-body local variable shaped like a field assignment never raises")


# --- _validate_no_duplicate_top_level_model_class_blocks ---

def test_no_duplicate_top_level_model_class_blocks_raises_on_colliding_members():
    """Real, confirmed bug found live (2026-08-09, task 07141af5's flagship run,
    project_ticket_counts node): models.py ended up with TWO entirely separate top-level
    'class ProjectProject(models.Model): _inherit = "project.project": ...' blocks, byte-for-byte
    identical, confirmed via direct inspection of the real committed content -- almost certainly a
    "shared file gets wholesale-duplicated instead of additively edited" artifact. Correctly
    flagged live by Code-Review as a real risk: whichever block Odoo's registry processes last
    silently wins for any member declared in both, discarding the other block's own declaration.
    """
    models_py = (
        "class ProjectProject(models.Model):\n"
        "    _inherit = 'project.project'\n"
        "    open_ticket_count = fields.Integer(compute='_compute_ticket_counts')\n\n"
        "    def _compute_ticket_counts(self):\n"
        "        pass\n\n\n"
        "class ProjectProject(models.Model):\n"
        "    _inherit = 'project.project'\n"
        "    open_ticket_count = fields.Integer(compute='_compute_ticket_counts')\n\n"
        "    def _compute_ticket_counts(self):\n"
        "        pass\n"
    )
    assert _raises(_validate_no_duplicate_top_level_model_class_blocks, _gen(models_py)) is not None
    print("PASS: two top-level class blocks for the same model with colliding members raises")


def test_no_duplicate_top_level_model_class_blocks_never_raises_on_legitimate_split():
    """Odoo genuinely supports multiple `_inherit` blocks extending the same model (a legitimate,
    common pattern for splitting concerns) -- as long as no field/method name repeats across the
    blocks, Odoo's registry cleanly merges them. This must never be flagged.
    """
    models_py = (
        "class ProjectProject(models.Model):\n"
        "    _inherit = 'project.project'\n"
        "    open_ticket_count = fields.Integer(compute='_compute_open')\n\n"
        "    def _compute_open(self):\n"
        "        pass\n\n\n"
        "class ProjectProject(models.Model):\n"
        "    _inherit = 'project.project'\n"
        "    overdue_ticket_count = fields.Integer(compute='_compute_overdue')\n\n"
        "    def _compute_overdue(self):\n"
        "        pass\n"
    )
    assert _raises(_validate_no_duplicate_top_level_model_class_blocks, _gen(models_py)) is None
    print("PASS: two legitimate _inherit blocks for the same model with no colliding members never raises")


def test_no_duplicate_top_level_model_class_blocks_never_raises_on_a_single_block():
    models_py = (
        "class ProjectProject(models.Model):\n"
        "    _inherit = 'project.project'\n"
        "    open_ticket_count = fields.Integer(compute='_compute_ticket_counts')\n\n"
        "    def _compute_ticket_counts(self):\n"
        "        pass\n"
    )
    assert _raises(_validate_no_duplicate_top_level_model_class_blocks, _gen(models_py)) is None
    print("PASS: a single class block for a model never raises")


# --- _validate_generated_python_files_are_valid_syntax ---

def test_generated_python_files_are_valid_syntax_catches_tests_py_indentation_error():
    """Real, confirmed bug found live (2026-08-09, task 07141af5's flagship run,
    project_ticket_counts node): a scoped-edit round produced `tests/test_project_ticket_counts.py`
    with a plain Python `IndentationError` on its own class statement -- nothing in the validator
    chain checked this before a REAL, expensive sandbox install attempt crashed deep inside
    Odoo's own module loader instead of failing fast, pre-write.
    """
    bad_tests_py = "class TestProjectTicketCounts(TransactionCase):\n        def test_x(self):\n    pass\n"
    generated = GeneratedModuleFiles(
        manifest_fields=_MANIFEST, models_py="class X(models.Model):\n    _name = 'x.model'\n",
        security_csv="x", notes="",
        tests_py={"tests/test_project_ticket_counts.py": bad_tests_py},
    )
    err = _raises(_validate_generated_python_files_are_valid_syntax, generated)
    assert err is not None, "a real IndentationError in tests_py must be caught"
    assert "tests/test_project_ticket_counts.py" in err
    print("PASS: a syntax error in a tests_py file is caught before a real sandbox install")


def test_generated_python_files_are_valid_syntax_catches_models_py_error():
    generated = _gen(models_py="class X(models.Model):\n    _name = 'x.model\n")
    err = _raises(_validate_generated_python_files_are_valid_syntax, generated)
    assert err is not None and "models.py" in err
    print("PASS: a syntax error in models.py is caught")


def test_generated_python_files_are_valid_syntax_never_raises_on_valid_python():
    generated = GeneratedModuleFiles(
        manifest_fields=_MANIFEST,
        models_py="class X(models.Model):\n    _name = 'x.model'\n    amount = fields.Integer()\n",
        security_csv="x", notes="",
        tests_py={"tests/test_x.py": "class TestX(TransactionCase):\n    def test_a(self):\n        pass\n"},
    )
    assert _raises(_validate_generated_python_files_are_valid_syntax, generated) is None
    print("PASS: valid Python in both models.py and tests_py never raises")


# --- _validate_no_bare_import_of_odoo_names ---

def test_no_bare_import_of_odoo_names_catches_combined_import():
    """Real, confirmed bug found live (2026-08-09, task 07141af5's flagship run,
    project_ticket_counts node): a scoped-edit round asked to add an 'import datetime' line
    instead wrote 'import datetime, fields, models' -- fields/models/api are never real,
    standalone top-level Python modules in an Odoo addon, crashing the entire module install
    with ModuleNotFoundError on syntactically-VALID Python a plain ast.parse check cannot catch.
    """
    models_py = "import datetime, fields, models\nfrom odoo import models, fields, api\n"
    err = _raises(_validate_no_bare_import_of_odoo_names, _gen(models_py))
    assert err is not None and "fields" in err
    print("PASS: a combined 'import datetime, fields, models' line is caught")


def test_no_bare_import_of_odoo_names_never_raises_on_correct_imports():
    models_py = "import datetime\nfrom odoo import models, fields, api\n"
    assert _raises(_validate_no_bare_import_of_odoo_names, _gen(models_py)) is None
    print("PASS: correct, separate imports never raise")


# --- Item 17: _validate_security_csv_not_empty row-count extension ---

def test_item17_raises_on_header_only_csv_when_a_new_model_is_declared():
    csv = "id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink\n"
    models_py = "class X(models.Model):\n    _name = 'x.model'\n    amount = fields.Integer()\n"
    assert _raises(_validate_security_csv_not_empty, _gen(models_py, security_csv=csv))
    print("PASS item17: a header-only CSV raises when this module declares a real new model")


def test_item17_never_raises_with_a_real_row():
    csv = (
        "id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink\n"
        "access_x,x,model_x,base.group_user,1,1,1,0\n"
    )
    models_py = "class X(models.Model):\n    _name = 'x.model'\n"
    assert _raises(_validate_security_csv_not_empty, _gen(models_py, security_csv=csv)) is None
    print("PASS item17: a CSV with at least one real data row never raises")


def test_item17_never_raises_on_header_only_csv_when_no_new_model_declared():
    # Real regression found live, 2026-08-02 (P7 Tier 3 pair 23): a pure _inherit-only field
    # addition legitimately produces a header-only security_csv (nothing new to grant access to)
    # -- this must never raise, matching the deterministic builder's own "no new models" path.
    csv = "id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink\n"
    models_py = "class X(models.Model):\n    _inherit = 'res.partner'\n    internal_reference_code = fields.Char()\n"
    assert _raises(_validate_security_csv_not_empty, _gen(models_py, security_csv=csv)) is None
    print("PASS item17: a header-only CSV on a pure _inherit-only module (no new model) never raises")


# --- Fourth exception (2026-08-06, fix-pass task 010): a genuine ir.rule domain_force
# restriction exempts an empty models_py class, same as the button/menu visibility-group
# exceptions above but for row-level record-rule security instead of UI visibility. ---

def test_record_rule_domain_restriction_exempts_an_empty_class():
    """The exact real task 010 shape: 'Mechanics should only see the meerwerk records they
    created themselves' is correctly, completely solved by a res.groups + ir.rule pair in
    security_xml alone -- models_py legitimately needs zero real content.
    """
    models_py = "class ProjectMeerwerk(models.Model):\n    _inherit = 'project.meerwerk'\n\n    pass\n"
    security_xml = (
        '<?xml version="1.0" encoding="utf-8"?><odoo>'
        '<record id="group_mechanics" model="res.groups">'
        '<field name="name">Mechanics</field>'
        '</record>'
        '<record id="rule_project_meerwerk_own" model="ir.rule">'
        '<field name="name">Mechanics: own records only</field>'
        '<field name="model_id" ref="model_project_meerwerk"/>'
        "<field name=\"domain_force\">[('create_uid','=',user.id)]</field>"
        '<field name="groups" eval="[(4, ref(\'oma_xyz.group_mechanics\'))]"/>'
        "</record></odoo>"
    )
    generated = _gen(models_py, security_xml=security_xml)
    assert _raises(_validate_models_py_has_real_non_comment_content, generated) is None
    print("PASS: a genuine ir.rule domain_force restriction exempts an empty models_py class, "
          "task 010's own real correct shape")


def test_ir_rule_with_empty_domain_force_does_not_exempt():
    """An ir.rule record present but with no real domain_force content (blank/whitespace-only)
    is not a genuine restriction -- must never falsely exempt an empty class.
    """
    models_py = "class ProjectMeerwerk(models.Model):\n    _inherit = 'project.meerwerk'\n\n    pass\n"
    security_xml = (
        '<?xml version="1.0" encoding="utf-8"?><odoo>'
        '<record id="rule_project_meerwerk_own" model="ir.rule">'
        '<field name="name">Mechanics: own records only</field>'
        '<field name="model_id" ref="model_project_meerwerk"/>'
        '<field name="domain_force"></field>'
        "</record></odoo>"
    )
    generated = _gen(models_py, security_xml=security_xml)
    assert _raises(_validate_models_py_has_real_non_comment_content, generated), (
        "an ir.rule with no real domain_force content must never exempt an empty class"
    )
    print("PASS: an ir.rule with an empty domain_force does not falsely exempt")


def test_domain_force_on_an_unrelated_model_does_not_exempt():
    """Scoped precisely to ir.rule -- a domain_force-shaped field on a totally unrelated record
    model must never be misread as a real record-rule restriction."""
    models_py = "class X(models.Model):\n    _name = 'x.model'\n    # nothing else\n"
    security_xml = (
        '<?xml version="1.0" encoding="utf-8"?><odoo>'
        '<record id="some_config" model="ir.config_parameter">'
        '<field name="domain_force">not a real rule</field>'
        "</record></odoo>"
    )
    generated = _gen(models_py, security_xml=security_xml)
    assert _raises(_validate_models_py_has_real_non_comment_content, generated), (
        "domain_force on a non-ir.rule record must never exempt an unrelated empty class"
    )
    print("PASS: domain_force on a non-ir.rule record does not falsely exempt")


def test_genuinely_empty_class_with_no_record_rule_still_raises():
    """No security_xml at all, genuinely empty class -- the original, unexempted defect must
    still be caught."""
    models_py = "class ProjectMeerwerk(models.Model):\n    _inherit = 'project.meerwerk'\n\n    pass\n"
    generated = _gen(models_py, security_xml=None)
    assert _raises(_validate_models_py_has_real_non_comment_content, generated), (
        "a genuinely empty class with no security_xml at all must still be rejected"
    )
    print("PASS: a genuinely empty class with no record-rule content at all is still correctly rejected")


# --- Fifth exception (2026-08-15): a multi-round decomposition can split a
# "new security group + record rule" goal into a round whose OWN scope is only the group
# creation, deferring the ir.rule itself to a later round -- confirmed live twice independently
# (record_rule_row_level_security's own certification runs, stock.picking and
# fleet.vehicle.log.services) before this fix. ---

def test_bare_new_security_group_exempts_an_empty_class_when_goal_signals_record_rule_intent():
    """Real, confirmed live twice (2026-08-15): a round scoped to ONLY creating the new
    res.groups record (the record rule deferred to a later round) is legitimately model-less,
    same as the fourth exception above, but with no ir.rule/domain_force present YET.
    """
    models_py = "from odoo import fields, models\n"
    security_xml = (
        '<?xml version="1.0" encoding="utf-8"?><odoo>'
        '<record id="group_inventory_clerk" model="res.groups">'
        '<field name="name">Inventory Clerk</field>'
        "</record></odoo>"
    )
    goal = (
        "Add a new security group named 'Inventory Clerk' scoped to the stock.picking model, "
        "with a real ir.model.access.csv row granting the group read, write, and create "
        "access. Add a record rule that applies ONLY to members of the 'Inventory Clerk' "
        "group, restricting them to see only stock pickings where they are the responsible "
        "user."
    )
    generated = _gen(models_py, security_xml=security_xml)
    assert _raises(_validate_models_py_has_real_non_comment_content, generated, goal) is None
    print("PASS: a round scoped to only creating the new security group is correctly exempted "
          "when the full goal signals record-rule intent")


def test_bare_new_security_group_does_not_exempt_when_goal_has_no_record_rule_intent():
    """The fix above must be narrow -- a bare res.groups record with an empty class must still
    raise when the goal doesn't signal any security-group/record-rule intent at all (a
    genuinely unrelated round that just happens to define an unused group)."""
    models_py = "class X(models.Model):\n    _name = 'x.model'\n    # nothing else\n"
    security_xml = (
        '<?xml version="1.0" encoding="utf-8"?><odoo>'
        '<record id="group_unrelated" model="res.groups">'
        '<field name="name">Unrelated</field>'
        "</record></odoo>"
    )
    generated = _gen(models_py, security_xml=security_xml)
    assert _raises(_validate_models_py_has_real_non_comment_content, generated, "Add a field named foo to x.model."), (
        "a bare security group must not exempt an empty class when the goal has no "
        "security-group/record-rule intent at all"
    )
    print("PASS: a bare security group does not falsely exempt when the goal signals no such intent")


if __name__ == "__main__":
    fns = [v for k, v in list(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
    print(f"\nALL {len(fns)} P11 TIER A TESTS PASSED")
