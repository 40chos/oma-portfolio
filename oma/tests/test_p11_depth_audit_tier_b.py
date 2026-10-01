"""P11 depth-audit Tier B (docs/planning/PHASE30_DEPTH_AUDIT_FINAL_CONSOLIDATED_2026-07-30.md
§2.2, items 90-132): unit tests. Items 109/111/128 confirmed already-covered/duplicates, no tests
needed. Item 110 blocked on its own unbuilt prerequisite, no test needed. Item 104 is a documented
no-op (no type-aware live-field-lookup tool exists) -- one test confirms it never raises. Zero
live LLM/GPU calls -- async items use monkeypatched DB functions.
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import specialists.build.specialist as build_specialist
from specialists.build.specialist import (
    GeneratedModuleFiles,
    ManifestFields,
    _validate_abstract_model_not_granted_security_csv_access,
    _validate_action_res_model_is_real_or_declared,
    _validate_api_constrains_method_has_a_raise,
    _validate_api_depends_fields_exist,
    _validate_boolean_required_is_meaningless_noop,
    _validate_common_widget_field_type_compatibility,
    _validate_declared_reference_methods_exist,
    _validate_decoration_class_is_real_bootstrap_context,
    _validate_decoration_condition_value_matches_selection_options,
    _validate_gantt_grid_map_view_declares_owning_module_dependency,
    _validate_kanban_view_has_required_template_scaffold,
    _validate_many2many_comodel_exists_via_live_registry,
    _validate_many2one_comodel_target_exists,
    _validate_many2one_domain_kwarg_fields_exist,
    _validate_many2one_ondelete_value_is_valid_enum,
    _validate_many2one_reference_has_model_field_kwarg,
    _validate_no_base_automation_model_reference,
    _validate_numeric_field_digits_param_shape,
    _validate_numeric_group_operator_is_valid,
    _validate_one2many_comodel_and_inverse_exist_via_live_registry,
    _validate_persisted_model_declares_name_or_inherit,
    _validate_precompute_not_combined_with_related,
    _validate_qweb_report_field_references_exist_on_model,
    _validate_reference_field_has_selection_kwarg,
    _validate_related_field_type_matches_target_field_type,
    _validate_security_csv_model_id_refers_to_known_model,
    _validate_security_csv_perm_columns_are_binary,
    _validate_selection_field_default_is_valid_option,
    _validate_server_action_code_does_not_reference_undefined_self,
    _validate_smart_button_statinfo_field_is_numeric,
    _validate_sql_constraint_referenced_fields_exist,
    _validate_stat_button_uses_button_box_placement,
    _validate_statusbar_visible_values_are_real_selection_options,
    _validate_test_classes_have_tagged_decorator,
    _validate_test_methods_contain_at_least_one_assertion,
    _validate_xpath_by_field_name_target_exists_on_model,
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


def _araises(coro):
    try:
        asyncio.run(coro)
        return None
    except ValueError as exc:
        return str(exc)


# --- Item 90 ---
def test_item90_raises_on_boolean_required():
    assert _raises(_validate_boolean_required_is_meaningless_noop, _gen("class X:\n    x = fields.Boolean(required=True)\n"))
    print("PASS item90")


def test_item90_never_raises_without_required():
    assert _raises(_validate_boolean_required_is_meaningless_noop, _gen("class X:\n    x = fields.Boolean()\n")) is None
    print("PASS item90 negative")


# --- Item 91 ---
def test_item91_raises_on_invalid_group_operator():
    assert _raises(_validate_numeric_group_operator_is_valid, _gen("class X:\n    x = fields.Integer(group_operator='total')\n"))
    print("PASS item91")


def test_item91_never_raises_on_valid_group_operator():
    assert _raises(_validate_numeric_group_operator_is_valid, _gen("class X:\n    x = fields.Integer(group_operator='sum')\n")) is None
    print("PASS item91 negative")


# --- Item 92 (extends item 23) ---
def test_item92_raises_on_float_string_default():
    assert _raises(_validate_numeric_field_digits_param_shape, _gen("class X:\n    x = fields.Float(default='0.0')\n"))
    print("PASS item92")


def test_item92_never_raises_on_real_float_default():
    assert _raises(_validate_numeric_field_digits_param_shape, _gen("class X:\n    x = fields.Float(default=0.0)\n")) is None
    print("PASS item92 negative")


# --- Item 93 ---
def test_item93_raises_on_invalid_selection_default():
    models_py = "class X:\n    x = fields.Selection([('a', 'A'), ('b', 'B')], default='c')\n"
    assert _raises(_validate_selection_field_default_is_valid_option, _gen(models_py))
    print("PASS item93")


def test_item93_never_raises_on_valid_selection_default():
    models_py = "class X:\n    x = fields.Selection([('a', 'A'), ('b', 'B')], default='a')\n"
    assert _raises(_validate_selection_field_default_is_valid_option, _gen(models_py)) is None
    print("PASS item93 negative")


# --- Item 94 (extends item 18) ---
def test_item94_raises_on_undeclared_callable_selection():
    models_py = "class X:\n    _name = 'x.model'\n    x = fields.Selection(selection='_get_options')\n"
    assert _raises(_validate_declared_reference_methods_exist, _gen(models_py))
    print("PASS item94")


def test_item94_never_raises_on_declared_callable_selection():
    models_py = "class X:\n    _name = 'x.model'\n    x = fields.Selection(selection='_get_options')\n    def _get_options(self):\n        pass\n"
    assert _raises(_validate_declared_reference_methods_exist, _gen(models_py)) is None
    print("PASS item94 negative")


# --- Item 95 (async) ---
def test_item95_raises_when_comodel_does_not_exist(monkeypatch):
    monkeypatch.setattr(build_specialist, "is_fast_path_eligible", lambda db: False)
    monkeypatch.setattr(build_specialist, "get_model_fields", lambda model, db: [])
    models_py = "class X:\n    partner_id = fields.Many2one('some.fake.model')\n"
    assert _araises(_validate_many2one_comodel_target_exists(_gen(models_py), [], "odoo16_dev"))
    print("PASS item95")


def test_item95_never_raises_when_comodel_exists(monkeypatch):
    monkeypatch.setattr(build_specialist, "is_fast_path_eligible", lambda db: False)
    monkeypatch.setattr(build_specialist, "get_model_fields", lambda model, db: ["id", "name"])
    models_py = "class X:\n    partner_id = fields.Many2one('res.partner')\n"
    assert _araises(_validate_many2one_comodel_target_exists(_gen(models_py), [], "odoo16_dev")) is None
    print("PASS item95 negative")


# --- Item 96 (async) ---
def test_item96_raises_on_undeclared_domain_field(monkeypatch):
    monkeypatch.setattr(build_specialist, "is_fast_path_eligible", lambda db: False)
    monkeypatch.setattr(build_specialist, "get_model_fields", lambda model, db: ["id", "name"])
    models_py = "class X:\n    partner_id = fields.Many2one('res.partner', domain=[('missing_field', '=', True)])\n"
    assert _araises(_validate_many2one_domain_kwarg_fields_exist(_gen(models_py), "odoo16_dev"))
    print("PASS item96")


def test_item96_never_raises_on_declared_domain_field(monkeypatch):
    monkeypatch.setattr(build_specialist, "is_fast_path_eligible", lambda db: False)
    monkeypatch.setattr(build_specialist, "get_model_fields", lambda model, db: ["id", "name"])
    models_py = "class X:\n    partner_id = fields.Many2one('res.partner', domain=[('name', '=', 'x')])\n"
    assert _araises(_validate_many2one_domain_kwarg_fields_exist(_gen(models_py), "odoo16_dev")) is None
    print("PASS item96 negative")


# --- Item 97 ---
def test_item97_raises_on_invalid_ondelete_value():
    assert _raises(_validate_many2one_ondelete_value_is_valid_enum, _gen("class X:\n    x = fields.Many2one('res.partner', ondelete='delete')\n"))
    print("PASS item97")


def test_item97_never_raises_on_valid_ondelete_value():
    assert _raises(_validate_many2one_ondelete_value_is_valid_enum, _gen("class X:\n    x = fields.Many2one('res.partner', ondelete='cascade')\n")) is None
    print("PASS item97 negative")


# --- Item 98 (async) ---
def test_item98_raises_on_missing_inverse_field_external(monkeypatch):
    # comodel deliberately NOT res./ir.-prefixed -- those are treated as well-known/skipped by design.
    monkeypatch.setattr(build_specialist, "is_fast_path_eligible", lambda db: False)
    monkeypatch.setattr(build_specialist, "get_model_fields", lambda model, db: ["id"])
    models_py = "class X:\n    line_ids = fields.One2many('crm.lead', 'missing_inverse')\n"
    assert _araises(_validate_one2many_comodel_and_inverse_exist_via_live_registry(_gen(models_py), [], "odoo16_dev"))
    print("PASS item98")


def test_item98_never_raises_when_inverse_exists(monkeypatch):
    monkeypatch.setattr(build_specialist, "is_fast_path_eligible", lambda db: False)
    monkeypatch.setattr(build_specialist, "get_model_fields", lambda model, db: ["id", "parent_id"])
    models_py = "class X:\n    line_ids = fields.One2many('crm.lead', 'parent_id')\n"
    assert _araises(_validate_one2many_comodel_and_inverse_exist_via_live_registry(_gen(models_py), [], "odoo16_dev")) is None
    print("PASS item98 negative")


def test_item98_skips_res_ir_prefixed_comodel_by_design(monkeypatch):
    monkeypatch.setattr(build_specialist, "is_fast_path_eligible", lambda db: False)
    monkeypatch.setattr(build_specialist, "get_model_fields", lambda model, db: ["id"])
    models_py = "class X:\n    line_ids = fields.One2many('res.partner', 'missing_inverse')\n"
    assert _araises(_validate_one2many_comodel_and_inverse_exist_via_live_registry(_gen(models_py), [], "odoo16_dev")) is None
    print("PASS item98: res./ir.-prefixed comodels are deliberately skipped, treated as well-known")


# --- Item 99 (async) ---
def test_item99_raises_when_comodel_does_not_exist(monkeypatch):
    monkeypatch.setattr(build_specialist, "is_fast_path_eligible", lambda db: False)
    monkeypatch.setattr(build_specialist, "get_model_fields", lambda model, db: [])
    models_py = "class X:\n    tag_ids = fields.Many2many('some.fake.model')\n"
    assert _araises(_validate_many2many_comodel_exists_via_live_registry(_gen(models_py), [], "odoo16_dev"))
    print("PASS item99")


# --- Item 100 ---
def test_item100_raises_without_selection_kwarg():
    assert _raises(_validate_reference_field_has_selection_kwarg, _gen("class X:\n    x = fields.Reference()\n"))
    print("PASS item100")


def test_item100_never_raises_with_selection_kwarg():
    assert _raises(_validate_reference_field_has_selection_kwarg, _gen("class X:\n    x = fields.Reference(selection=[('res.partner', 'Partner')])\n")) is None
    print("PASS item100 negative")


# --- Item 101 ---
def test_item101_raises_without_model_field_kwarg():
    assert _raises(_validate_many2one_reference_has_model_field_kwarg, _gen("class X:\n    x = fields.Many2oneReference()\n"))
    print("PASS item101")


# --- Item 102 (extends api_depends) ---
def test_item102_raises_on_undeclared_bare_field(monkeypatch):
    models_py = (
        "class X:\n    _name = 'x.model'\n    amount = fields.Integer()\n"
        "    @api.depends('missing_field')\n    def _compute(self):\n        pass\n"
    )
    assert _araises(_validate_api_depends_fields_exist(_gen(models_py), [], "odoo16_dev"))
    print("PASS item102")


def test_item102_never_raises_on_declared_bare_field():
    models_py = (
        "class X:\n    _name = 'x.model'\n    amount = fields.Integer()\n"
        "    @api.depends('amount')\n    def _compute(self):\n        pass\n"
    )
    assert _araises(_validate_api_depends_fields_exist(_gen(models_py), [], "odoo16_dev")) is None
    print("PASS item102 negative")


# --- Item 103 ---
def test_item103_raises_on_precompute_with_related():
    assert _raises(_validate_precompute_not_combined_with_related, _gen("class X:\n    x = fields.Integer(precompute=True, related='y.z')\n"))
    print("PASS item103")


def test_item103_never_raises_without_related():
    assert _raises(_validate_precompute_not_combined_with_related, _gen("class X:\n    x = fields.Integer(precompute=True, compute='_c', store=True)\n")) is None
    print("PASS item103 negative")


# --- Item 104 (documented no-op) ---
def test_item104_is_a_documented_noop():
    models_py = "class X:\n    partner_id = fields.Many2one('res.partner')\n    x = fields.Char(related='partner_id.name')\n"
    assert _araises(_validate_related_field_type_matches_target_field_type(_gen(models_py), "odoo16_dev")) is None
    print("PASS item104: documented no-op, no type-aware live-field-lookup tool exists")


# --- Item 105 ---
def test_item105_raises_on_undeclared_sql_constraint_field():
    models_py = "class X:\n    _name = 'x.model'\n    _sql_constraints = [('u', 'unique(missing_field)', 'msg')]\n"
    assert _raises(_validate_sql_constraint_referenced_fields_exist, _gen(models_py))
    print("PASS item105")


def test_item105_never_raises_on_declared_field():
    models_py = "class X:\n    _name = 'x.model'\n    code = fields.Char()\n    _sql_constraints = [('u', 'unique(code)', 'msg')]\n"
    assert _raises(_validate_sql_constraint_referenced_fields_exist, _gen(models_py)) is None
    print("PASS item105 negative")


def test_item180_raises_on_many2many_field_with_no_real_column():
    models_py = (
        "class X:\n    _name = 'x.model'\n"
        "    tag_ids = fields.Many2many('x.tag')\n"
        "    _sql_constraints = [('u', 'unique(tag_ids)', 'msg')]\n"
    )
    assert _raises(_validate_sql_constraint_referenced_fields_exist, _gen(models_py))
    print("PASS item180 many2many")


def test_item180_raises_on_compute_field_without_store():
    models_py = (
        "class X:\n    _name = 'x.model'\n"
        "    total = fields.Float(compute='_compute_total')\n"
        "    _sql_constraints = [('u', 'unique(total)', 'msg')]\n"
    )
    assert _raises(_validate_sql_constraint_referenced_fields_exist, _gen(models_py))
    print("PASS item180 compute-no-store")


def test_item180_never_raises_on_stored_compute_field():
    models_py = (
        "class X:\n    _name = 'x.model'\n"
        "    total = fields.Float(compute='_compute_total', store=True)\n"
        "    _sql_constraints = [('u', 'unique(total)', 'msg')]\n"
    )
    assert _raises(_validate_sql_constraint_referenced_fields_exist, _gen(models_py)) is None
    print("PASS item180 stored-compute negative")


def test_item180_never_raises_on_ordinary_scalar_field():
    models_py = (
        "class X:\n    _name = 'x.model'\n"
        "    code = fields.Char()\n"
        "    _sql_constraints = [('u', 'unique(code)', 'msg')]\n"
    )
    assert _raises(_validate_sql_constraint_referenced_fields_exist, _gen(models_py)) is None
    print("PASS item180 ordinary scalar field negative")


# --- Item 106 ---
def test_item106_raises_on_constrains_method_with_no_raise():
    models_py = "class X:\n    @api.constrains('x')\n    def _check(self):\n        pass\n"
    assert _raises(_validate_api_constrains_method_has_a_raise, _gen(models_py))
    print("PASS item106")


def test_item106_never_raises_when_raise_present():
    models_py = "class X:\n    @api.constrains('x')\n    def _check(self):\n        if not self.x:\n            raise ValidationError('bad')\n"
    assert _raises(_validate_api_constrains_method_has_a_raise, _gen(models_py)) is None
    print("PASS item106 negative")


def test_item106_never_raises_when_the_raise_is_the_files_own_final_line_with_no_trailing_newline():
    """Real, confirmed bug found live (2026-08-03, task012's own real re-test -- 3 identical
    false positives in a row on real production traffic): the body-capture group required every
    line, including the method's own final line, to end in a real newline. `generated.models_py`
    genuinely has no trailing newline after its own last line (confirmed via a direct repr() of
    the real content) -- whenever the `raise` happened to be the method's own (and the whole
    file's own) final line, it silently dropped out of the captured body, and this validator
    falsely reported a method that DOES raise as having "no raise anywhere in its body". Using
    task012's own real captured content (docs/reports/PHASE30_50TASK_SWEEP_RUN3_FINAL_2026-08-03.md),
    byte-for-byte, deliberately with NO trailing newline at the very end.
    """
    models_py = (
        "from odoo import api, fields, models\n"
        "from odoo.tools.translate import _\n"
        "from odoo.exceptions import ValidationError\n\n"
        "class ProjectMeerwerk(models.Model):\n"
        "    _inherit = 'project.meerwerk'\n\n\n"
        "    @api.constrains('date', 'date_finish')\n"
        "    def _check_date_order(self):\n"
        "        for record in self:\n"
        "            if record.date and record.date_finish:\n"
        "                if record.date_finish < record.date:\n"
        '                    raise ValidationError(_("The finish date cannot be before the start date."))'
    )
    assert not models_py.endswith("\n"), "the real bug only reproduces when the file has no trailing newline"
    assert _raises(_validate_api_constrains_method_has_a_raise, _gen(models_py)) is None, (
        "task012's own real, genuinely-raising constraint method must not be falsely rejected"
    )
    print("PASS item106: a real raise on the method's own (and the file's own) final line, with "
          "no trailing newline, is correctly recognized -- not a false 'no raise' positive")


# --- Item 107 ---
def test_item107_raises_on_neither_name_nor_inherit():
    assert _raises(_validate_persisted_model_declares_name_or_inherit, _gen("class X(models.Model):\n    x = fields.Char()\n"))
    print("PASS item107")


def test_item107_never_raises_with_name():
    assert _raises(_validate_persisted_model_declares_name_or_inherit, _gen("class X(models.Model):\n    _name = 'x.model'\n")) is None
    print("PASS item107 negative")


# --- Item 108 ---
def test_item108_raises_on_abstract_model_csv_access():
    models_py = "class X(models.AbstractModel):\n    _name = 'x.abstract'\n"
    csv = "id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink\naccess_x,x,model_x_abstract,base.group_user,1,1,1,0\n"
    assert _raises(_validate_abstract_model_not_granted_security_csv_access, _gen(models_py, security_csv=csv))
    print("PASS item108")


# --- Item 112 ---
def test_item112_raises_on_missing_kanban_scaffold():
    views_xml = "<kanban><field name='x'/></kanban>"
    assert _raises(_validate_kanban_view_has_required_template_scaffold, _gen(views_xml=views_xml))
    print("PASS item112")


def test_item112_never_raises_with_scaffold():
    views_xml = '<kanban><templates><t t-name="kanban-box"/></templates></kanban>'
    assert _raises(_validate_kanban_view_has_required_template_scaffold, _gen(views_xml=views_xml)) is None
    print("PASS item112 negative")


# --- Item 113 ---
def test_item113_raises_on_gantt_without_dependency():
    generated = _gen(views_xml="<gantt/>")
    assert _raises(_validate_gantt_grid_map_view_declares_owning_module_dependency, generated)
    print("PASS item113")


def test_item113_never_raises_with_dependency():
    manifest = ManifestFields(name="x", version="1.0.0", category="Tools", summary="x", author="x", depends=["base", "web_gantt"], data=[])
    generated = GeneratedModuleFiles(manifest_fields=manifest, models_py="", security_csv="x", views_xml="<gantt/>", notes="")
    assert _raises(_validate_gantt_grid_map_view_declares_owning_module_dependency, generated) is None
    print("PASS item113 negative")


# --- Item 114 ---
def test_item114_raises_on_undeclared_xpath_field():
    models_py = "class X:\n    _name = 'x.model'\n"
    views_xml = "<xpath expr=\"//field[@name='missing_field']\"/>"
    assert _raises(_validate_xpath_by_field_name_target_exists_on_model, _gen(models_py, views_xml=views_xml))
    print("PASS item114")


# --- Item 115 ---
def test_item115_raises_on_non_numeric_statinfo():
    models_py = "class X:\n    _name = 'x.model'\n    x = fields.Char()\n"
    views_xml = '<field name="x" widget="statinfo"/>'
    assert _raises(_validate_smart_button_statinfo_field_is_numeric, _gen(models_py, views_xml=views_xml))
    print("PASS item115")


# --- Item 116 ---
def test_item116_raises_on_statinfo_outside_button_box():
    views_xml = '<form><button widget="statinfo" name="x"/></form>'
    assert _raises(_validate_stat_button_uses_button_box_placement, _gen(views_xml=views_xml))
    print("PASS item116")


def test_item116_never_raises_inside_button_box():
    views_xml = '<form><div class="oe_button_box"><button widget="statinfo" name="x"/></div></form>'
    assert _raises(_validate_stat_button_uses_button_box_placement, _gen(views_xml=views_xml)) is None
    print("PASS item116 negative")


# --- Item 117 ---
def test_item117_raises_on_invalid_statusbar_visible_value():
    models_py = "class X:\n    _name = 'x.model'\n    state = fields.Selection([('a', 'A'), ('b', 'B')])\n"
    views_xml = '<field name="state" statusbar_visible="a,c"/>'
    assert _raises(_validate_statusbar_visible_values_are_real_selection_options, _gen(models_py, views_xml=views_xml))
    print("PASS item117")


# --- Item 118 ---
def test_item118_raises_on_widget_type_mismatch():
    models_py = "class X:\n    _name = 'x.model'\n    x = fields.Char()\n"
    views_xml = '<field name="x" widget="boolean_toggle"/>'
    assert _raises(_validate_common_widget_field_type_compatibility, _gen(models_py, views_xml=views_xml))
    print("PASS item118")


def test_item118_never_raises_on_widget_type_match():
    models_py = "class X:\n    _name = 'x.model'\n    x = fields.Boolean()\n"
    views_xml = '<field name="x" widget="boolean_toggle"/>'
    assert _raises(_validate_common_widget_field_type_compatibility, _gen(models_py, views_xml=views_xml)) is None
    print("PASS item118 negative")


# --- Item 119 ---
def test_item119_raises_on_invalid_decoration_class():
    views_xml = '<tree decoration-fancy="x &lt; 0"/>'
    assert _raises(_validate_decoration_class_is_real_bootstrap_context, _gen(views_xml=views_xml))
    print("PASS item119")


def test_item119_never_raises_on_real_decoration_class():
    views_xml = '<tree decoration-danger="x &lt; 0"/>'
    assert _raises(_validate_decoration_class_is_real_bootstrap_context, _gen(views_xml=views_xml)) is None
    print("PASS item119 negative")


# --- Item 120 ---
def test_item120_raises_on_invalid_decoration_selection_literal():
    models_py = "class X:\n    _name = 'x.model'\n    state = fields.Selection([('a', 'A'), ('b', 'B')])\n"
    views_xml = "<tree decoration-danger=\"state == 'c'\"/>"
    assert _raises(_validate_decoration_condition_value_matches_selection_options, _gen(models_py, views_xml=views_xml))
    print("PASS item120")


# --- Item 121 (async) ---
def test_item121_raises_when_res_model_does_not_exist(monkeypatch):
    monkeypatch.setattr(build_specialist, "is_fast_path_eligible", lambda db: False)
    monkeypatch.setattr(build_specialist, "get_model_fields", lambda model, db: [])
    views_xml = '<record model="ir.actions.act_window"><field name="res_model">some.fake.model</field></record>'
    assert _araises(_validate_action_res_model_is_real_or_declared(_gen(views_xml=views_xml), "odoo16_dev"))
    print("PASS item121")


def test_item121_never_raises_on_own_new_model():
    views_xml = '<record model="ir.actions.act_window"><field name="res_model">x.model</field></record>'
    generated = _gen("class X:\n    _name = 'x.model'\n", views_xml=views_xml)
    assert _araises(_validate_action_res_model_is_real_or_declared(generated, "odoo16_dev")) is None
    print("PASS item121 negative")


# --- Item 122 ---
def test_item122_raises_on_undefined_self_in_server_action():
    xml = '<record model="ir.actions.server"><field name="state">code</field><field name="code">self.do_thing()</field></record>'
    assert _raises(_validate_server_action_code_does_not_reference_undefined_self, _gen(security_xml=xml))
    print("PASS item122")


def test_item122_never_raises_with_model_reference():
    xml = '<record model="ir.actions.server"><field name="state">code</field><field name="code">model.do_thing()</field></record>'
    assert _raises(_validate_server_action_code_does_not_reference_undefined_self, _gen(security_xml=xml)) is None
    print("PASS item122 negative")


# --- Item 124 ---
def test_item124_raises_on_base_automation_reference():
    xml = '<record model="base.automation"><field name="name">x</field></record>'
    assert _raises(_validate_no_base_automation_model_reference, _gen(security_xml=xml))
    print("PASS item124")


# --- Item 125 ---
def test_item125_raises_on_test_method_with_no_assertion():
    tests_py = {"test_x.py": "class TestX(TransactionCase):\n    def test_thing(self):\n        x = 1\n"}
    assert _raises(_validate_test_methods_contain_at_least_one_assertion, _gen(tests_py=tests_py))
    print("PASS item125")


def test_item125_never_raises_with_assertion():
    tests_py = {"test_x.py": "class TestX(TransactionCase):\n    def test_thing(self):\n        self.assertEqual(1, 1)\n"}
    assert _raises(_validate_test_methods_contain_at_least_one_assertion, _gen(tests_py=tests_py)) is None
    print("PASS item125 negative")


# --- Item 126 ---
def test_item126_raises_on_missing_tagged_decorator():
    tests_py = {"test_x.py": "class TestX(TransactionCase):\n    pass\n"}
    assert _raises(_validate_test_classes_have_tagged_decorator, _gen(tests_py=tests_py))
    print("PASS item126")


def test_item126_never_raises_with_tagged_decorator():
    tests_py = {"test_x.py": "@tagged('post_install')\nclass TestX(TransactionCase):\n    pass\n"}
    assert _raises(_validate_test_classes_have_tagged_decorator, _gen(tests_py=tests_py)) is None
    print("PASS item126 negative")


# --- Item 127 ---
def test_item127_raises_on_non_binary_perm_column():
    csv = "id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink\naccess_x,x,model_x,base.group_user,yes,1,1,0\n"
    assert _raises(_validate_security_csv_perm_columns_are_binary, _gen(security_csv=csv))
    print("PASS item127")


def test_item127_never_raises_on_binary_perm_columns():
    csv = "id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink\naccess_x,x,model_x,base.group_user,1,1,1,0\n"
    assert _raises(_validate_security_csv_perm_columns_are_binary, _gen(security_csv=csv)) is None
    print("PASS item127 negative")


# --- Item 129 (async) ---
def test_item129_raises_when_model_id_does_not_resolve(monkeypatch):
    monkeypatch.setattr(build_specialist, "is_fast_path_eligible", lambda db: False)
    monkeypatch.setattr(build_specialist, "resolve_xmlids_exist", lambda refs, db: {r: False for r in refs})
    csv = "id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink\naccess_x,x,fake_module.model_fake,base.group_user,1,1,1,0\n"
    assert _araises(_validate_security_csv_model_id_refers_to_known_model(_gen(security_csv=csv), "odoo16_dev"))
    print("PASS item129")


def test_item129_skips_own_new_model():
    models_py = "class X:\n    _name = 'x.model'\n"
    csv = "id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink\naccess_x,x,model_x_model,base.group_user,1,1,1,0\n"
    assert _araises(_validate_security_csv_model_id_refers_to_known_model(_gen(models_py, security_csv=csv), "odoo16_dev")) is None
    print("PASS item129 negative")


# --- Item 131 ---
def test_item131_raises_on_undeclared_doc_field():
    models_py = "class X:\n    _name = 'x.model'\n"
    xml = '<template id="t1"><span t-esc="doc.missing_field"/></template>'
    assert _raises(_validate_qweb_report_field_references_exist_on_model, _gen(models_py, security_xml=xml))
    print("PASS item131")


def test_item131_never_raises_on_declared_doc_field():
    models_py = "class X:\n    _name = 'x.model'\n    amount = fields.Integer()\n"
    xml = '<template id="t1"><span t-esc="doc.amount"/></template>'
    assert _raises(_validate_qweb_report_field_references_exist_on_model, _gen(models_py, security_xml=xml)) is None
    print("PASS item131 negative")


def test_item131_never_false_positives_on_o_or_company_prefix():
    models_py = "class X:\n    _name = 'x.model'\n"
    xml = '<template id="t1"><span t-esc="o.missing_field"/><span t-esc="company.missing_field2"/></template>'
    assert _raises(_validate_qweb_report_field_references_exist_on_model, _gen(models_py, security_xml=xml)) is None
    print("PASS item131: only the doc. prefix is checked, o./company. never false-positive")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
    print(f"\nALL {len(fns)} P11 DEPTH-AUDIT TIER B TESTS PASSED")
