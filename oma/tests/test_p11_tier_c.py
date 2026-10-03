"""P11 Tier C (docs/planning/PHASE30_OMA_SEAM_COVERAGE_AND_COMBINATORIAL_HARDENING_2026-07-29.md
§18b.2 items 52-73; full detail: PHASE30_ADDENDUM_GENERIC_FIX_BUILDOUT_2026-07-30.md §2.5): unit
tests for all 22 Tier C items. Every function here is pure/synchronous, zero LLM/GPU/network calls.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from specialists.build.specialist import (
    GeneratedModuleFiles,
    ManifestFields,
    _autofix_copy_vals_list_before_batch_create_mutation,
    _autofix_field_type_missing_call_parens,
    _autofix_rewrite_list_view_arch_tag_to_tree_for_odoo16,
    _validate_action_view_mode_has_corresponding_view,
    _validate_binary_field_has_filename_companion,
    _validate_client_action_tag_and_params_shape,
    _validate_config_parameter_key_is_module_prefixed,
    _validate_date_datetime_default_helper_matches_field_type,
    _validate_form_helper_model_reference_exists,
    _validate_gantt_map_required_fields_exist,
    _validate_generated_test_file_is_valid_odoo_test_class,
    _validate_goal_named_button_group_restriction_is_applied,
    _validate_goal_named_record_ownership_restriction_is_implemented,
    _validate_image_field_kwargs_are_real,
    _validate_mail_alias_mixin_no_field_redeclaration,
    _validate_mail_template_lang_field_has_no_jinja,
    _validate_manifest_fields_name_category_author_summary_non_empty,
    _validate_many2many_relation_table_disambiguation,
    _validate_no_conflicting_duplicate_sequence_records,
    _validate_no_duplicate_class_names_across_test_files,
    _validate_no_in_place_vals_mutation_in_batch_create,
    _validate_paperformat_fields_are_numeric,
    _validate_report_action_references_real_template,
    _validate_url_action_shape,
)

_MANIFEST = ManifestFields(
    name="x", version="1.0.0", category="Tools", summary="x", author="x", depends=["base"], data=[],
)


def _gen(models_py="", views_xml=None, security_csv="x", security_xml=None, extra_data_files=None, tests_py=None) -> GeneratedModuleFiles:
    return GeneratedModuleFiles(
        manifest_fields=_MANIFEST, models_py=models_py, views_xml=views_xml,
        security_csv=security_csv, security_xml=security_xml, extra_data_files=extra_data_files,
        tests_py=tests_py, notes="",
    )


def _raises(fn, *args):
    try:
        fn(*args)
        return None
    except ValueError as exc:
        return str(exc)


# --- Item 52: _autofix_field_type_missing_call_parens ---

def test_item52_appends_missing_parens():
    generated = _gen("class X(models.Model):\n    _name = 'x.model'\n    amount = fields.Integer\n")
    _autofix_field_type_missing_call_parens(generated)
    assert "fields.Integer()" in generated.models_py
    print("PASS item52: a bare fields.Integer class reference gets () appended")


def test_item52_leaves_correct_call_alone():
    models_py = "class X(models.Model):\n    _name = 'x.model'\n    amount = fields.Integer()\n"
    generated = _gen(models_py)
    _autofix_field_type_missing_call_parens(generated)
    assert generated.models_py == models_py
    print("PASS item52: an already-correct fields.Integer() call is never touched")


# --- Item 53: _validate_generated_test_file_is_valid_odoo_test_class ---

def test_item53_raises_on_wrong_base_class():
    tests_py = {"test_x.py": "class TestX(unittest.TestCase):\n    pass\n"}
    assert _raises(_validate_generated_test_file_is_valid_odoo_test_class, _gen(tests_py=tests_py))
    print("PASS item53: a test class not inheriting a real Odoo test base class raises")


def test_item53_never_raises_on_transaction_case():
    tests_py = {"test_x.py": "class TestX(TransactionCase):\n    pass\n"}
    assert _raises(_validate_generated_test_file_is_valid_odoo_test_class, _gen(tests_py=tests_py)) is None
    print("PASS item53: TransactionCase never raises")


# --- _autofix_rewrite_list_view_arch_tag_to_tree_for_odoo16 (real task025 content) ---
# task025's real captured view arch used <list string="...">...</list> (Odoo 17+ terminology) as
# a view's root arch element -- this project's real, fixed target is Odoo 16 (OMA_ODOO_DB=
# odoo16_dev), which has no <list> tag in view arch at all; this crashed the real sandbox install
# with rc=255 ("Install failed"), never caught by any prior static validator.

def test_autofix_rewrites_list_tag_to_tree_on_task025_real_shape():
    views_xml = (
        '<odoo><record id="view_waste_container_list" model="ir.ui.view">'
        '<field name="name">waste.container.list</field>'
        '<field name="model">waste.container</field>'
        '<field name="arch" type="xml">'
        '<list string="Waste Containers">'
        '<field name="project_id"/><field name="supplier_id"/>'
        '</list>'
        '</field></record></odoo>'
    )
    gen = _gen(views_xml=views_xml)
    _autofix_rewrite_list_view_arch_tag_to_tree_for_odoo16(gen)
    assert "<list" not in gen.views_xml
    assert "</list>" not in gen.views_xml
    assert '<tree string="Waste Containers">' in gen.views_xml
    assert "</tree>" in gen.views_xml
    import xml.etree.ElementTree as ET
    ET.fromstring(gen.views_xml)
    print("PASS: task025's real <list> arch root is rewritten to <tree>, XML stays well-formed")


def test_autofix_is_a_no_op_when_no_list_tag_present():
    views_xml = '<odoo><tree string="X"><field name="y"/></tree></odoo>'
    gen = _gen(views_xml=views_xml)
    _autofix_rewrite_list_view_arch_tag_to_tree_for_odoo16(gen)
    assert gen.views_xml == views_xml
    print("PASS: views_xml with no <list> tag at all is a pure no-op")


def test_autofix_leaves_attributes_and_content_untouched():
    views_xml = '<odoo><list string="Waste Containers" default_order="name"><field name="x"/></list></odoo>'
    gen = _gen(views_xml=views_xml)
    _autofix_rewrite_list_view_arch_tag_to_tree_for_odoo16(gen)
    assert '<tree string="Waste Containers" default_order="name">' in gen.views_xml
    assert '<field name="x"/>' in gen.views_xml
    print("PASS: only the tag name changes, attributes and inner content are untouched")


# --- Item 54: _validate_action_view_mode_has_corresponding_view ---

def test_item54_raises_on_missing_view_type():
    views_xml = (
        '<record model="ir.actions.act_window"><field name="view_mode">tree,kanban</field></record>'
        '<tree/>'
    )
    assert _raises(_validate_action_view_mode_has_corresponding_view, _gen(views_xml=views_xml))
    print("PASS item54: view_mode names a type with no matching view raises")


def test_item54_never_raises_when_all_present():
    views_xml = (
        '<record model="ir.actions.act_window"><field name="view_mode">tree,form</field></record>'
        '<tree/><form/>'
    )
    assert _raises(_validate_action_view_mode_has_corresponding_view, _gen(views_xml=views_xml)) is None
    print("PASS item54: every view_mode type having a matching view never raises")


# --- Item 55: _validate_mail_template_lang_field_has_no_jinja ---

def test_item55_raises_on_jinja_in_lang():
    xml = '<field name="lang">{{ object.partner_id.lang }}</field>'
    assert _raises(_validate_mail_template_lang_field_has_no_jinja, _gen(security_xml=xml))
    print("PASS item55: literal {{ }} inside <field name='lang'> raises")


def test_item55_never_raises_on_plain_lang():
    xml = '<field name="lang">${object.partner_id.lang}</field>'
    assert _raises(_validate_mail_template_lang_field_has_no_jinja, _gen(security_xml=xml)) is None
    print("PASS item55: a real ${...} lang expression never raises")


# --- Item 56: _validate_no_conflicting_duplicate_sequence_records ---

def test_item56_raises_on_conflicting_config():
    xml = (
        '<record model="ir.sequence"><field name="code">x.seq</field><field name="prefix">A</field></record>'
        '<record model="ir.sequence"><field name="code">x.seq</field><field name="prefix">B</field></record>'
    )
    assert _raises(_validate_no_conflicting_duplicate_sequence_records, _gen(security_xml=xml))
    print("PASS item56: two ir.sequence records sharing a code but differing config raise")


def test_item56_never_raises_on_matching_config():
    xml = (
        '<record model="ir.sequence"><field name="code">x.seq</field><field name="prefix">A</field></record>'
    )
    assert _raises(_validate_no_conflicting_duplicate_sequence_records, _gen(security_xml=xml)) is None
    print("PASS item56: a single sequence record never raises")


# --- Item 57: _validate_no_in_place_vals_mutation_in_batch_create ---

def test_item57_raises_on_unrebound_vals_mutation():
    models_py = (
        "class X(models.Model):\n"
        "    @api.model_create_multi\n"
        "    def create(self, vals_list):\n"
        "        for vals in vals_list:\n"
        "            vals['x'] = 1\n"
        "        return super().create(vals_list)\n"
    )
    assert _raises(_validate_no_in_place_vals_mutation_in_batch_create, _gen(models_py))
    print("PASS item57: mutating vals[...] without first rebinding a local copy raises")


def test_item57_never_raises_when_rebound():
    models_py = (
        "class X(models.Model):\n"
        "    @api.model_create_multi\n"
        "    def create(self, vals_list):\n"
        "        for vals in vals_list:\n"
        "            vals = dict(vals)\n"
        "            vals['x'] = 1\n"
        "        return super().create(vals_list)\n"
    )
    assert _raises(_validate_no_in_place_vals_mutation_in_batch_create, _gen(models_py)) is None
    print("PASS item57: rebinding vals = dict(vals) first never raises")


# --- _autofix_copy_vals_list_before_batch_create_mutation (real task006 content) ---
# task006's real captured create() unconditionally set vals['create_uid'] = self.env.user.id
# inside 'for vals in vals_list:' with no rebind -- exactly what item 57 flags. A naive per-item
# `vals = dict(vals)` rebind inside the loop can't be autofixed safely (it would stop the mutation
# from ever reaching the dicts actually passed to super().create(), silently dropping the field).
# The safe fix copies the whole list once, before the loop, preserving the loop's own mutation
# semantics while breaking aliasing to the caller's original dicts.

def test_autofix_copies_vals_list_before_loop_on_task006_real_shape():
    models_py = (
        "from odoo import api, fields, models\n\n"
        "class ProjectFieldjob(models.Model):\n"
        "    _inherit = 'project.fieldjob'\n\n"
        "    create_uid = fields.Many2one('res.users', string='Created by', readonly=True)\n\n"
        "    @api.model_create_multi\n"
        "    def create(self, vals_list):\n"
        "        for vals in vals_list:\n"
        "            if 'create_uid' not in vals:\n"
        "                vals['create_uid'] = self.env.user.id\n"
        "        return super().create(vals_list)\n"
    )
    gen = _gen(models_py)
    _autofix_copy_vals_list_before_batch_create_mutation(gen)
    assert "vals_list = [dict(v) for v in vals_list]" in gen.models_py
    import ast
    ast.parse(gen.models_py)
    assert _raises(_validate_no_in_place_vals_mutation_in_batch_create, gen) is None
    print("PASS: task006's real unrebound-mutation shape is autofixed and the validator no longer raises")


def test_autofix_is_a_no_op_when_no_batch_create_present():
    models_py = "class X(models.Model):\n    _name = 'x.model'\n"
    gen = _gen(models_py)
    _autofix_copy_vals_list_before_batch_create_mutation(gen)
    assert gen.models_py == models_py
    print("PASS: no @api.model_create_multi create() present is a pure no-op")


def test_autofix_is_a_no_op_when_already_safely_rebound():
    models_py = (
        "class X(models.Model):\n"
        "    @api.model_create_multi\n"
        "    def create(self, vals_list):\n"
        "        for vals in vals_list:\n"
        "            vals = dict(vals)\n"
        "            vals['x'] = 1\n"
        "        return super().create(vals_list)\n"
    )
    gen = _gen(models_py)
    _autofix_copy_vals_list_before_batch_create_mutation(gen)
    assert gen.models_py == models_py
    print("PASS: an already-safely-rebound loop is left untouched")


def test_autofix_preserves_functional_behavior_not_just_syntax():
    """Direct regression for the exact risk the docstring warns about: the fix must not just
    silence the validator, it must genuinely still deliver the mutated field to super().create().
    """
    calls = []

    class FakeUser:
        id = 99

    class FakeEnv:
        user = FakeUser()

    class FakeModel:
        env = FakeEnv()

        def create(self, vals_list):
            vals_list = [dict(v) for v in vals_list]
            for vals in vals_list:
                if "create_uid" not in vals:
                    vals["create_uid"] = self.env.user.id
            calls.append(vals_list)
            return vals_list

    original = [{"name": "a"}, {"name": "b"}]
    original_snapshot = [dict(d) for d in original]
    result = FakeModel().create(original)
    assert all("create_uid" in r for r in result), "fixed shape must still deliver the field"
    assert original == original_snapshot, "caller's original dicts must be left untouched"
    print("PASS: fixed shape still functionally sets the field while leaving the caller's dicts untouched")


# --- Item 58: _validate_binary_field_has_filename_companion ---

def test_item58_raises_without_filename():
    models_py = "class X(models.Model):\n    _name = 'x.model'\n    attachment = fields.Binary()\n"
    assert _raises(_validate_binary_field_has_filename_companion, _gen(models_py))
    print("PASS item58: a Binary field with no filename= companion raises")


def test_item58_never_raises_with_filename():
    models_py = "class X(models.Model):\n    _name = 'x.model'\n    attachment = fields.Binary(filename='attachment_filename')\n"
    assert _raises(_validate_binary_field_has_filename_companion, _gen(models_py)) is None
    print("PASS item58: a Binary field with filename= never raises")


# --- Item 59: _validate_image_field_kwargs_are_real ---

def test_item59_raises_on_invented_kwarg():
    models_py = "class X(models.Model):\n    _name = 'x.model'\n    photo = fields.Image(quality=80)\n"
    assert _raises(_validate_image_field_kwargs_are_real, _gen(models_py))
    print("PASS item59: an invented Image kwarg raises")


def test_item59_never_raises_on_real_kwargs():
    models_py = "class X(models.Model):\n    _name = 'x.model'\n    photo = fields.Image(max_width=1024)\n"
    assert _raises(_validate_image_field_kwargs_are_real, _gen(models_py)) is None
    print("PASS item59: real Image kwargs never raise")


# --- Item 60: _validate_date_datetime_default_helper_matches_field_type ---

def test_item60_raises_on_date_with_datetime_default():
    models_py = "class X(models.Model):\n    _name = 'x.model'\n    d = fields.Date(default=fields.Datetime.now)\n"
    assert _raises(_validate_date_datetime_default_helper_matches_field_type, _gen(models_py))
    print("PASS item60: fields.Date(default=fields.Datetime.now) raises")


def test_item60_never_raises_on_correct_helper():
    models_py = "class X(models.Model):\n    _name = 'x.model'\n    d = fields.Date(default=fields.Date.today)\n"
    assert _raises(_validate_date_datetime_default_helper_matches_field_type, _gen(models_py)) is None
    print("PASS item60: fields.Date(default=fields.Date.today) never raises")


# --- Item 61: _validate_many2many_relation_table_disambiguation ---

def test_item61_raises_on_colliding_many2many():
    models_py = (
        "class X(models.Model):\n    _name = 'x.model'\n"
        "    a_ids = fields.Many2many('res.partner')\n    b_ids = fields.Many2many('res.partner')\n"
    )
    assert _raises(_validate_many2many_relation_table_disambiguation, _gen(models_py))
    print("PASS item61: two Many2many fields to the same comodel with no relation= raise")


def test_item61_never_raises_with_explicit_relation():
    models_py = (
        "class X(models.Model):\n    _name = 'x.model'\n"
        "    a_ids = fields.Many2many('res.partner', relation='x_a_rel')\n"
        "    b_ids = fields.Many2many('res.partner', relation='x_b_rel')\n"
    )
    assert _raises(_validate_many2many_relation_table_disambiguation, _gen(models_py)) is None
    print("PASS item61: explicit relation= tables never raise")


# --- Item 62: _validate_gantt_map_required_fields_exist ---

def test_item62_raises_on_undeclared_gantt_field():
    models_py = "class X(models.Model):\n    _name = 'x.model'\n"
    views_xml = '<gantt date_start="missing"/>'
    assert _raises(_validate_gantt_map_required_fields_exist, _gen(models_py, views_xml=views_xml))
    print("PASS item62: a gantt date_start referencing an undeclared field raises")


def test_item62_never_raises_on_declared_field():
    models_py = "class X(models.Model):\n    _name = 'x.model'\n    start = fields.Datetime()\n"
    views_xml = '<gantt date_start="start"/>'
    assert _raises(_validate_gantt_map_required_fields_exist, _gen(models_py, views_xml=views_xml)) is None
    print("PASS item62: a gantt date_start referencing a declared field never raises")


# --- Item 63: _validate_client_action_tag_and_params_shape ---

def test_item63_raises_on_missing_tag():
    xml = '<record model="ir.actions.client"><field name="name">X</field></record>'
    assert _raises(_validate_client_action_tag_and_params_shape, _gen(security_xml=xml))
    print("PASS item63: an ir.actions.client with no tag raises")


def test_item63_never_raises_with_tag():
    xml = '<record model="ir.actions.client"><field name="tag">my_client_action</field></record>'
    assert _raises(_validate_client_action_tag_and_params_shape, _gen(security_xml=xml)) is None
    print("PASS item63: an ir.actions.client with a real tag never raises")


# --- Item 64: _validate_url_action_shape ---

def test_item64_raises_on_missing_url():
    xml = '<record model="ir.actions.act_url"><field name="name">X</field></record>'
    assert _raises(_validate_url_action_shape, _gen(security_xml=xml))
    print("PASS item64: an ir.actions.act_url with no url raises")


def test_item64_never_raises_with_url():
    xml = '<record model="ir.actions.act_url"><field name="url">https://example.com</field></record>'
    assert _raises(_validate_url_action_shape, _gen(security_xml=xml)) is None
    print("PASS item64: an ir.actions.act_url with a real url never raises")


# --- Item 65: _validate_report_action_references_real_template (thin alias, inert until prerequisite) ---

def test_item65_shares_item48_logic():
    xml = (
        '<record model="ir.actions.report"><field name="report_name">module.missing</field></record>'
    )
    assert _raises(_validate_report_action_references_real_template, _gen(security_xml=xml))
    print("PASS item65: shares item 48's real resolution logic, raises identically")


# --- Item 66: _validate_form_helper_model_reference_exists (documented no-op) ---

def test_item66_is_a_documented_noop():
    tests_py = {"test_x.py": "Form(self.env['x.new.model'])\n"}
    assert _raises(_validate_form_helper_model_reference_exists, _gen(tests_py=tests_py)) is None
    print("PASS item66: never raises today, per its own documented conservative-scoping no-op")


# --- Item 67: _validate_mail_alias_mixin_no_field_redeclaration ---

def test_item67_raises_on_redeclared_alias_id():
    models_py = "class X(models.Model):\n    _inherit = ['x.model', 'mail.alias.mixin']\n    alias_id = fields.Many2one('mail.alias')\n"
    assert _raises(_validate_mail_alias_mixin_no_field_redeclaration, _gen(models_py))
    print("PASS item67: redeclaring alias_id on a mail.alias.mixin model raises")


def test_item67_never_raises_without_redeclaration():
    models_py = "class X(models.Model):\n    _inherit = ['x.model', 'mail.alias.mixin']\n    amount = fields.Integer()\n"
    assert _raises(_validate_mail_alias_mixin_no_field_redeclaration, _gen(models_py)) is None
    print("PASS item67: an unrelated field on a mail.alias.mixin model never raises")


# --- Item 68: _validate_paperformat_fields_are_numeric ---

def test_item68_raises_on_non_numeric_margin():
    xml = '<record model="report.paperformat"><field name="margin_top">not_a_number</field></record>'
    assert _raises(_validate_paperformat_fields_are_numeric, _gen(security_xml=xml))
    print("PASS item68: a non-numeric paperformat field raises")


def test_item68_never_raises_on_numeric_margin():
    xml = '<record model="report.paperformat"><field name="margin_top">10</field></record>'
    assert _raises(_validate_paperformat_fields_are_numeric, _gen(security_xml=xml)) is None
    print("PASS item68: a real numeric paperformat field never raises")


# --- Item 69: _validate_no_duplicate_class_names_across_test_files ---

def test_item69_raises_on_duplicate_test_class_across_files():
    tests_py = {
        "test_a.py": "class TestX(TransactionCase):\n    pass\n",
        "test_b.py": "class TestX(TransactionCase):\n    pass\n",
    }
    assert _raises(_validate_no_duplicate_class_names_across_test_files, _gen(tests_py=tests_py))
    print("PASS item69: the same test class name in two different files raises")


def test_item69_never_raises_with_unique_names():
    tests_py = {
        "test_a.py": "class TestX(TransactionCase):\n    pass\n",
        "test_b.py": "class TestY(TransactionCase):\n    pass\n",
    }
    assert _raises(_validate_no_duplicate_class_names_across_test_files, _gen(tests_py=tests_py)) is None
    print("PASS item69: unique test class names across files never raise")


# --- Item 70: _validate_config_parameter_key_is_module_prefixed ---

def test_item70_raises_on_unprefixed_declared_key():
    xml = '<record model="ir.config_parameter"><field name="key">some_setting</field></record>'
    assert _raises(_validate_config_parameter_key_is_module_prefixed, _gen(security_xml=xml))
    print("PASS item70: a newly-declared, unprefixed config_parameter key raises")


def test_item70_never_raises_on_prefixed_declared_key():
    xml = '<record model="ir.config_parameter"><field name="key">my_module.some_setting</field></record>'
    assert _raises(_validate_config_parameter_key_is_module_prefixed, _gen(security_xml=xml)) is None
    print("PASS item70: a properly module-prefixed declared key never raises")


def test_item70_never_raises_on_a_plain_read():
    # get_param() READS a config parameter -- must never be flagged the same way as declaring one.
    models_py = "class X(models.Model):\n    def go(self):\n        self.env['ir.config_parameter'].sudo().get_param('web.base.url')\n"
    assert _raises(_validate_config_parameter_key_is_module_prefixed, _gen(models_py)) is None
    print("PASS item70: reading an existing config parameter via get_param() is never flagged")


# --- Item 71: _validate_manifest_fields_name_category_author_summary_non_empty ---

def test_item71_raises_on_empty_manifest_name():
    manifest = ManifestFields(name="", version="1.0.0", category="Tools", summary="x", author="x", depends=["base"], data=[])
    generated = GeneratedModuleFiles(manifest_fields=manifest, models_py="", security_csv="x", notes="")
    assert _raises(_validate_manifest_fields_name_category_author_summary_non_empty, generated)
    print("PASS item71: an empty manifest name raises")


def test_item71_never_raises_when_all_present():
    assert _raises(_validate_manifest_fields_name_category_author_summary_non_empty, _gen()) is None
    print("PASS item71: a fully-populated manifest never raises")


# --- Item 72: _validate_goal_named_record_ownership_restriction_is_implemented ---

def test_item72_raises_when_goal_requests_ownership_restriction_but_no_rule():
    goal = "Users should only see records they created."
    assert _raises(_validate_goal_named_record_ownership_restriction_is_implemented, _gen(), goal, None)
    print("PASS item72: an ownership-restriction goal with no ir.rule at all raises")


def test_item72_raises_when_rule_exists_but_not_scoped_by_create_uid():
    goal = "Users should only see records they created."
    xml = '<record model="ir.rule"><field name="domain_force">[(\'state\', \'=\', \'done\')]</field></record>'
    assert _raises(_validate_goal_named_record_ownership_restriction_is_implemented, _gen(security_xml=xml), goal, None)
    print("PASS item72: an ir.rule that exists but doesn't scope by create_uid raises")


def test_item72_never_raises_when_correctly_implemented():
    goal = "Users should only see records they created."
    xml = '<record model="ir.rule"><field name="domain_force">[(\'create_uid\', \'=\', user.id)]</field></record>'
    assert _raises(_validate_goal_named_record_ownership_restriction_is_implemented, _gen(security_xml=xml), goal, None) is None
    print("PASS item72: a create_uid-scoped ir.rule never raises")


def test_item72_skips_when_goal_never_requests_ownership_restriction():
    assert _raises(_validate_goal_named_record_ownership_restriction_is_implemented, _gen(), "Add a field.", None) is None
    print("PASS item72: a goal that never requests ownership restriction is skipped, never guessed")


def test_item72_accepts_assignment_field_when_goal_says_their_own_not_created():
    """Real, confirmed general bug found live (2026-08-12, day-to-day directions sweep, Batch B):
    "employees only see their own leave requests" was being force-corrected toward create_uid by
    this validator, even though the record's real owner here is whoever it's assigned to
    (employee_id), never who happened to click Create. Confirmed on two unrelated models
    (hr.leave, fleet.vehicle). This proves a non-create_uid, user-referencing field is now
    accepted for "their own" phrasing that never says "created"."""
    goal = "Employees should only see their own leave requests."
    xml = '<record model="ir.rule"><field name="domain_force">[(\'employee_id.user_id\', \'=\', user.id)]</field></record>'
    assert _raises(_validate_goal_named_record_ownership_restriction_is_implemented, _gen(security_xml=xml), goal, None) is None
    print("PASS item72: an employee_id-scoped rule satisfies 'their own' phrasing without create_uid")


def test_item72_still_requires_create_uid_when_goal_literally_says_created():
    """The fix above must stay narrow: a goal that literally says 'they created' still
    unambiguously means create_uid, and a rule scoped by a different field must still raise."""
    goal = "Users should only see records they created."
    xml = '<record model="ir.rule"><field name="domain_force">[(\'employee_id.user_id\', \'=\', user.id)]</field></record>'
    assert _raises(_validate_goal_named_record_ownership_restriction_is_implemented, _gen(security_xml=xml), goal, None)
    print("PASS item72: 'they created' phrasing still requires create_uid specifically, even with a different real field present")


def test_item72_raises_when_their_own_rule_never_references_current_user_at_all():
    """The general-ownership branch must still catch a rule that references NO user field at
    all -- only the specific create_uid requirement was loosened, not the check itself."""
    goal = "Employees should only see their own leave requests."
    xml = '<record model="ir.rule"><field name="domain_force">[(\'state\', \'=\', \'done\')]</field></record>'
    assert _raises(_validate_goal_named_record_ownership_restriction_is_implemented, _gen(security_xml=xml), goal, None)
    print("PASS item72: a rule that references no user field at all still raises for 'their own' phrasing")


def test_item72_raises_when_their_own_rule_uses_create_uid_only():
    """Real, confirmed gap found live (2026-08-12, day-to-day directions sweep, hr.expense
    re-verification): create_uid still contains the literal substring 'user.id' in its own
    domain, so the earlier version of the general branch wrongly accepted it for 'their own'
    phrasing -- the exact same wrong-field mistake this validator exists to catch, just
    smuggled through under different wording. Confirmed live on two unrelated models
    (purchase.order, hr.expense)."""
    goal = "Employees should only see their own leave requests."
    xml = '<record model="ir.rule"><field name="domain_force">[(\'create_uid\', \'=\', user.id)]</field></record>'
    assert _raises(_validate_goal_named_record_ownership_restriction_is_implemented, _gen(security_xml=xml), goal, None)
    print("PASS item72: create_uid alone no longer satisfies 'their own' phrasing")


def test_item72_accepts_create_uid_alongside_a_real_assignment_field():
    """A rule with BOTH create_uid and a real assignment field somewhere (e.g. a companion
    manager-override rule) must not be penalized just because create_uid also appears."""
    goal = "Employees should only see their own leave requests."
    xml = (
        '<record model="ir.rule"><field name="domain_force">'
        "[('employee_id.user_id', '=', user.id)]</field></record>"
        '<record model="ir.rule"><field name="domain_force">'
        "[('create_uid', '=', user.id)]</field></record>"
    )
    assert _raises(_validate_goal_named_record_ownership_restriction_is_implemented, _gen(security_xml=xml), goal, None) is None
    print("PASS item72: a real assignment field elsewhere is accepted even if create_uid also appears")


# --- Item 73: _validate_goal_named_button_group_restriction_is_applied ---

def test_item73_raises_when_button_not_restricted():
    goal = "Restrict the Send button to base.group_system."
    views_xml = '<button type="object" name="send_action"/>'
    assert _raises(_validate_goal_named_button_group_restriction_is_applied, _gen(views_xml=views_xml), goal, None)
    print("PASS item73: a goal-named button-group restriction with no groups= on any button raises")


def test_item73_never_raises_when_restricted():
    goal = "Restrict the Send button to base.group_system."
    views_xml = '<button type="object" name="send_action" groups="base.group_system"/>'
    assert _raises(_validate_goal_named_button_group_restriction_is_applied, _gen(views_xml=views_xml), goal, None) is None
    print("PASS item73: a button correctly carrying the requested groups= never raises")


def test_item73_skips_when_goal_never_requests_it():
    assert _raises(_validate_goal_named_button_group_restriction_is_applied, _gen(), "Add a field.", None) is None
    print("PASS item73: a goal that never requests a button-group restriction is skipped")


# --- Item 73 extension (50-task deep-dive, docs/reports/PHASE30_50TASK_DEEP_DIVE_MASTER_2026-08-05.md,
# P1 item 3): Task 008's real goal shape ("X button... only visible to Y", a human role name, not
# "restrict the X button to Y" with a technical token) never matched the original regex at all --
# and a bare regex widen alone would false-positive-block genuinely correct code, since a human
# role phrase ("System Administrators") never literally overlaps with the real technical group
# xmlid Odoo's own code correctly uses (`groups="base.group_system"`). ---

_TASK_008_GOAL = (
    "The 'Send to customer' button on the fieldjob form should only be visible to System "
    "Administrators. Normal users and managers should not see it."
)


def test_item73_task008_shape_never_raises_on_correctly_resolved_group():
    # (a) the direct regression test: correctly-fixed code using the real group xmlid must not
    # be false-positive-blocked just because "System Administrators" never literally appears in it.
    views_xml = '<button type="object" name="send_to_customer" groups="base.group_system"/>'
    assert _raises(
        _validate_goal_named_button_group_restriction_is_applied, _gen(views_xml=views_xml),
        _TASK_008_GOAL, None,
    ) is None
    print("PASS item73 extension: a human role phrase ('System Administrators') correctly "
          "resolves to base.group_system and never false-positives on genuinely correct code")


def test_item73_original_restrict_the_x_button_to_y_phrasing_still_works():
    # (b) no regression on the pre-existing, already-passing "restrict the X button to Y" shape.
    goal = "Restrict the Send button to base.group_system."
    views_xml_missing = '<button type="object" name="send_action"/>'
    views_xml_present = '<button type="object" name="send_action" groups="base.group_system"/>'
    assert _raises(
        _validate_goal_named_button_group_restriction_is_applied, _gen(views_xml=views_xml_missing),
        goal, None,
    )
    assert _raises(
        _validate_goal_named_button_group_restriction_is_applied, _gen(views_xml=views_xml_present),
        goal, None,
    ) is None
    print("PASS item73 extension: the original 'restrict the X button to Y' phrasing is unaffected")


def test_item73_task008_shape_raises_on_the_original_real_defect():
    # (c) Task 008's real original defect: a send_to_customer_visible computed field that's
    # always False and never wired to the button's own groups= attribute at all.
    views_xml = '<button type="object" name="send_to_customer" invisible="not send_to_customer_visible"/>'
    assert _raises(
        _validate_goal_named_button_group_restriction_is_applied, _gen(views_xml=views_xml),
        _TASK_008_GOAL, None,
    ), "the original Task 008 defect (no groups= at all, just an always-False computed field) must still raise"
    print("PASS item73 extension: Task 008's real original defect (no groups= restriction at all) still raises")


def test_item73_unmapped_role_name_fails_conservatively_not_silently_or_falsely():
    # (d) a role with no known mapping (a project-specific custom role) -- must neither silently
    # no-op nor false-positive on correct-but-unresolvable code; the conservative fallback
    # requires SOME real groups= restriction to exist, without asserting a specific group value.
    goal = "The 'Approve Discount' button should only be visible to Regional Sales Directors."
    views_xml_no_restriction = '<button type="object" name="approve_discount"/>'
    views_xml_some_restriction = (
        '<button type="object" name="approve_discount" groups="sales_team.group_sale_manager"/>'
    )
    assert _raises(
        _validate_goal_named_button_group_restriction_is_applied, _gen(views_xml=views_xml_no_restriction),
        goal, None,
    ), "an unmapped custom role with NO groups= restriction applied at all must still raise"
    assert _raises(
        _validate_goal_named_button_group_restriction_is_applied, _gen(views_xml=views_xml_some_restriction),
        goal, None,
    ) is None, "an unmapped custom role with SOME real groups= restriction applied must not false-positive"
    print("PASS item73 extension: an unmapped custom role fails conservatively (requires some real "
          "restriction) rather than silently no-op'ing or false-positiving on an unresolvable value")


# --- 50-task deep-dive P2 item 5: migration-vs-ir.cron misclassification --------------------
# docs/reports/PHASE30_50TASK_DEEP_DIVE_MASTER_2026-08-05.md -- real, confirmed gap on Task 032
# ("Write a Python migration script to populate a new field on existing records"): Code-Review
# repeatedly, correctly rejected ir.cron for a one-time migration across 2 rounds, and no existing
# validator distinguishes a migration-shaped goal from a legitimately recurring cron goal.

_TASK_032_GOAL = "Write a Python migration script to populate a new field on existing records"
_TASK_018_GOAL = (
    "Every night at midnight, automatically change all fieldjob records that have been in "
    "'sent' state for more than 30 days to 'rejected'. We don't want to chase customers forever."
)


def test_migration_goal_raises_task032_real_shape_using_ir_cron():
    # (a) reproduce Task 032's exact goal with generated code using ir.cron -- must raise.
    from specialists.build.specialist import _validate_migration_goal_does_not_use_ir_cron

    extra_data_files = {
        "data/cron_data.xml": (
            '<odoo><record id="ir_cron_populate_tax_exempt" model="ir.cron">'
            '<field name="name">Populate tax_exempt</field>'
            '<field name="model_id" ref="model_res_partner"/>'
            '<field name="interval_number">1</field>'
            '<field name="interval_type">days</field>'
            "</record></odoo>"
        ),
    }
    assert _raises(
        _validate_migration_goal_does_not_use_ir_cron,
        _gen(extra_data_files=extra_data_files), _TASK_032_GOAL,
    ), "Task 032's real shape (migration goal implemented via ir.cron) must raise"
    print("PASS: Task 032's real ir.cron-for-a-one-time-migration defect raises")


def test_migration_goal_never_raises_with_correct_migration_hook_pattern():
    # (b) the same goal with a correct migrations/<version>/post-*.py-shaped file instead -- must
    # not raise (no ir.cron record anywhere in the generated data).
    from specialists.build.specialist import _validate_migration_goal_does_not_use_ir_cron

    extra_data_files = {
        "migrations/1.1.0/post-populate-tax-exempt.py": (
            "def migrate(cr, version):\n"
            "    cr.execute(\"UPDATE res_partner SET tax_exempt = false WHERE tax_exempt IS NULL\")\n"
        ),
    }
    assert _raises(
        _validate_migration_goal_does_not_use_ir_cron,
        _gen(extra_data_files=extra_data_files), _TASK_032_GOAL,
    ) is None
    print("PASS: a correct migrations/<version>/post-*.py hook never raises")


def test_migration_goal_never_raises_on_task018_real_legitimately_recurring_shape():
    # (c) the explicit regression test for the risk called out in the report: Task 018's real,
    # legitimately-recurring cron goal must never be affected by this new check.
    from specialists.build.specialist import _validate_migration_goal_does_not_use_ir_cron

    extra_data_files = {
        "data/cron_data.xml": (
            '<odoo><record id="ir_cron_reject_stale_fieldjob" model="ir.cron">'
            '<field name="name">Reject stale fieldjob records</field>'
            '<field name="model_id" ref="model_project_fieldjob"/>'
            '<field name="interval_number">1</field>'
            '<field name="interval_type">days</field>'
            "</record></odoo>"
        ),
    }
    assert _raises(
        _validate_migration_goal_does_not_use_ir_cron,
        _gen(extra_data_files=extra_data_files), _TASK_018_GOAL,
    ) is None, (
        "Task 018's real, legitimately recurring 'every night at midnight' cron goal must never "
        "be false-triggered by the migration-vs-cron check"
    )
    print("PASS: Task 018's real legitimately-recurring cron goal is never affected by this check")


def test_migration_goal_unbounded_search_raises_task032_round2_real_shape():
    # (d) the second, distinct lint from Task 032's own round-2 finding: unbounded search([]) on
    # a migration-shaped goal.
    from specialists.build.specialist import _validate_migration_goal_does_not_use_unbounded_search

    models_py = (
        "class ResPartner(models.Model):\n"
        "    _inherit = 'res.partner'\n\n"
        "    def _migrate_tax_exempt(self):\n"
        "        partners = self.env['res.partner'].search([])\n"
        "        for p in partners:\n"
        "            p.tax_exempt = False\n"
    )
    assert _raises(
        _validate_migration_goal_does_not_use_unbounded_search, _gen(models_py), _TASK_032_GOAL,
    ), "an unbounded search([]) on a migration-shaped goal must raise"
    print("PASS: Task 032's real unbounded search([]) defect raises")


def test_migration_goal_bounded_search_never_raises():
    from specialists.build.specialist import _validate_migration_goal_does_not_use_unbounded_search

    models_py = (
        "class ResPartner(models.Model):\n"
        "    _inherit = 'res.partner'\n\n"
        "    def _migrate_tax_exempt(self):\n"
        "        partners = self.env['res.partner'].search([('tax_exempt', '=', False)], limit=1000)\n"
        "        for p in partners:\n"
        "            p.tax_exempt = False\n"
    )
    assert _raises(
        _validate_migration_goal_does_not_use_unbounded_search, _gen(models_py), _TASK_032_GOAL,
    ) is None
    print("PASS: a bounded/domained search never raises")


def test_migration_goal_checks_skip_entirely_on_a_non_migration_goal():
    from specialists.build.specialist import (
        _validate_migration_goal_does_not_use_ir_cron,
        _validate_migration_goal_does_not_use_unbounded_search,
    )

    extra_data_files = {
        "data/cron_data.xml": (
            '<odoo><record id="x" model="ir.cron"><field name="name">X</field></record></odoo>'
        ),
    }
    models_py = "class X(models.Model):\n    _name = 'x'\n\n    def go(self):\n        self.env['x'].search([])\n"
    assert _raises(
        _validate_migration_goal_does_not_use_ir_cron,
        _gen(models_py, extra_data_files=extra_data_files), "Add a Kanban view for fieldjob.",
    ) is None
    assert _raises(
        _validate_migration_goal_does_not_use_unbounded_search,
        _gen(models_py), "Add a Kanban view for fieldjob.",
    ) is None
    print("PASS: both migration-shaped checks are complete no-ops on an ordinary, non-migration goal")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
    print(f"\nALL {len(fns)} P11 TIER C TESTS PASSED")
