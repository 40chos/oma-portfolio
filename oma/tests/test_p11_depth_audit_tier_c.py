"""P11 depth-audit Tier C (docs/planning/PHASE30_DEPTH_AUDIT_FINAL_CONSOLIDATED_2026-07-30.md
§2.3, items 133-167): unit tests. Items 138/153 blocked on real, confirmed missing infra
(type-aware live-field-lookup / xmlid-to-model-name resolution, neither exists in tools_odoo/),
item 161 explicitly deferred by the source doc's own text, item 149 folded into an extension of
Tier B item 34 (tested there, no separate test needed here). Items 144/159 are honest documented
no-ops (same missing-infra category) -- one test each confirms they never raise. Zero live LLM/
GPU calls -- async items use monkeypatched DB functions.
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import specialists.build.specialist as build_specialist
from specialists.build.specialist import (
    GeneratedModuleFiles,
    ManifestFields,
    _autofix_manifest_removes_self_dependency,
    _autofix_manifest_version_prefixed_with_odoo_series,
    _autofix_security_csv_perm_unlink_defaults_to_zero,
    _validate_activity_view_template_fields_exist_on_model,
    _validate_binary_field_attachment_kwarg_type,
    _validate_calendar_mode_and_color_attrs_are_valid,
    _validate_custom_report_model_get_report_values_shape,
    _validate_goal_named_company_restriction_is_implemented,
    _validate_integer_id_suffix_field_likely_should_be_relational,
    _validate_inverse_function_signature_takes_only_self,
    _validate_ir_cron_numeric_fields_are_numeric_literals,
    _validate_kanban_progressbar_colors_match_selection_options,
    _validate_manifest_version_matches_odoo_series,
    _validate_many2many_explicit_relation_table_name_is_valid_identifier,
    _validate_monetary_field_has_currency_field_inherited,
    _validate_no_duplicate_onchange_registration_for_same_field_set,
    _validate_onchange_decorator_field_args_exist,
    _validate_paperformat_custom_format_requires_dimensions,
    _validate_pivot_graph_field_type_attrs_are_valid_enum,
    _validate_priority_widget_selection_keys_are_numeric_strings,
    _validate_properties_field_has_definition_record_pair,
    _validate_related_field_not_combined_with_own_default,
    _validate_report_action_report_type_is_valid_enum,
    _validate_report_print_report_name_is_valid_python_expression,
    _validate_search_panel_field_exists_on_model,
    _validate_security_csv_perm_unlink_matches_goal_intent,
    _validate_selection_field_no_duplicate_keys,
    _validate_server_action_object_write_fields_reference_real_target_fields,
    _validate_statusbar_no_deprecated_options_dict_syntax,
    _validate_sum_compute_field_type_is_numeric,
    _validate_transient_model_not_targeted_by_persisted_relation,
    _validate_tree_editable_attr_value,
    _validate_view_required_field_not_readonly_only,
    _validate_xml_eval_attributes_are_valid_python_literals,
)

_MANIFEST = ManifestFields(
    name="x", version="16.0.1.0.0", category="Tools", summary="x", author="x", depends=["base"], data=[],
)


def _gen(models_py="", views_xml=None, security_csv="x", security_xml=None, extra_data_files=None, manifest=None) -> GeneratedModuleFiles:
    return GeneratedModuleFiles(
        manifest_fields=manifest or _MANIFEST, models_py=models_py, views_xml=views_xml,
        security_csv=security_csv, security_xml=security_xml, extra_data_files=extra_data_files, notes="",
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


# --- Item 133 ---
def test_item133_raises_on_suspicious_integer_id_field():
    assert _raises(_validate_integer_id_suffix_field_likely_should_be_relational, _gen("class X:\n    partner_id = fields.Integer()\n"))
    print("PASS item133")


def test_item133_never_raises_on_allowlisted_name():
    assert _raises(_validate_integer_id_suffix_field_likely_should_be_relational, _gen("class X:\n    legacy_id = fields.Integer()\n")) is None
    print("PASS item133 negative")


def test_item133_never_raises_on_polymorphic_model_reference_pair():
    """Real bug found live (2026-08-06, task046 of the SITE 50-task fix-pass, confirmed twice --
    round 1's `record_id`+`model_name`, round 2's `tracked_record_id`+its own sibling model
    field): a false positive that burned both of task046's rounds and escalated the whole task on
    zero other blocking findings. An Integer *_id field with a sibling model/res_model/model_name
    Char field (the standard Odoo polymorphic-reference pattern, e.g. mail.activity's own real
    res_id/res_model pair) must never be flagged -- it structurally cannot be a Many2one."""
    assert _raises(
        _validate_integer_id_suffix_field_likely_should_be_relational,
        _gen("class X:\n    record_id = fields.Integer()\n    model_name = fields.Char()\n"),
    ) is None
    assert _raises(
        _validate_integer_id_suffix_field_likely_should_be_relational,
        _gen("class X:\n    tracked_record_id = fields.Integer()\n    res_model = fields.Char()\n"),
    ) is None
    assert _raises(
        _validate_integer_id_suffix_field_likely_should_be_relational,
        _gen("class X:\n    res_id = fields.Integer()\n    model = fields.Char()\n"),
    ) is None
    print("PASS item133 polymorphic-reference-pair negative")


def test_item133_still_raises_when_no_model_reference_sibling_present():
    """Regression guard: the exemption above must not swallow a genuine, isolated suspicious
    Integer *_id field with no model-naming sibling field at all."""
    assert _raises(
        _validate_integer_id_suffix_field_likely_should_be_relational,
        _gen("class X:\n    partner_id = fields.Integer()\n    name = fields.Char()\n"),
    ) is not None
    print("PASS item133 still raises without a model-reference sibling")


# --- Item 134 ---
def test_item134_raises_on_string_attachment_kwarg():
    assert _raises(_validate_binary_field_attachment_kwarg_type, _gen("class X:\n    x = fields.Binary(attachment='True')\n"))
    print("PASS item134")


# --- Item 135 (async) ---
def test_item135_raises_when_inherited_currency_field_unresolved(monkeypatch):
    monkeypatch.setattr(build_specialist, "is_fast_path_eligible", lambda db: False)
    monkeypatch.setattr(build_specialist, "get_model_fields", lambda model, db: ["id", "name"])
    models_py = "class X:\n    _inherit = 'res.partner'\n    amount = fields.Monetary()\n"
    assert _araises(_validate_monetary_field_has_currency_field_inherited(_gen(models_py), "odoo16_dev"))
    print("PASS item135")


def test_item135_never_raises_when_resolved(monkeypatch):
    monkeypatch.setattr(build_specialist, "is_fast_path_eligible", lambda db: False)
    monkeypatch.setattr(build_specialist, "get_model_fields", lambda model, db: ["id", "currency_id"])
    models_py = "class X:\n    _inherit = 'res.partner'\n    amount = fields.Monetary()\n"
    assert _araises(_validate_monetary_field_has_currency_field_inherited(_gen(models_py), "odoo16_dev")) is None
    print("PASS item135 negative")


# --- Item 136 ---
def test_item136_raises_on_duplicate_selection_key():
    models_py = "class X:\n    x = fields.Selection([('a', 'A'), ('a', 'B')])\n"
    assert _raises(_validate_selection_field_no_duplicate_keys, _gen(models_py))
    print("PASS item136")


# --- Item 137 ---
def test_item137_raises_without_both_kwargs():
    assert _raises(_validate_properties_field_has_definition_record_pair, _gen("class X:\n    x = fields.Properties(definition_record='y')\n"))
    print("PASS item137")


def test_item137_never_raises_with_both_kwargs():
    models_py = "class X:\n    x = fields.Properties(definition_record='y', definition_record_field='z')\n"
    assert _raises(_validate_properties_field_has_definition_record_pair, _gen(models_py)) is None
    print("PASS item137 negative")


# --- Item 139 ---
def test_item139_raises_on_invalid_relation_identifier():
    models_py = "class X:\n    x = fields.Many2many('res.partner', relation='Bad-Name!')\n"
    assert _raises(_validate_many2many_explicit_relation_table_name_is_valid_identifier, _gen(models_py))
    print("PASS item139")


def test_item139_never_raises_on_valid_identifier():
    models_py = "class X:\n    x = fields.Many2many('res.partner', relation='x_y_rel')\n"
    assert _raises(_validate_many2many_explicit_relation_table_name_is_valid_identifier, _gen(models_py)) is None
    print("PASS item139 negative")


# --- Item 140 ---
def test_item140_raises_on_sum_compute_non_numeric_type():
    models_py = "class X:\n    x = fields.Char(compute='_compute_x')\n    def _compute_x(self):\n        self.x = sum(self.line_ids.mapped('amount'))\n"
    assert _raises(_validate_sum_compute_field_type_is_numeric, _gen(models_py))
    print("PASS item140")


def test_item140_never_raises_on_numeric_type():
    models_py = "class X:\n    x = fields.Float(compute='_compute_x')\n    def _compute_x(self):\n        self.x = sum(self.line_ids.mapped('amount'))\n"
    assert _raises(_validate_sum_compute_field_type_is_numeric, _gen(models_py)) is None
    print("PASS item140 negative")


# --- Item 141 ---
def test_item141_raises_on_related_with_default():
    models_py = "class X:\n    x = fields.Char(related='y.z', default='foo')\n"
    assert _raises(_validate_related_field_not_combined_with_own_default, _gen(models_py))
    print("PASS item141")


# --- Item 142 ---
def test_item142_raises_on_extra_required_param():
    models_py = "class X:\n    x = fields.Char(inverse='_set_x')\n    def _set_x(self, extra):\n        pass\n"
    assert _raises(_validate_inverse_function_signature_takes_only_self, _gen(models_py))
    print("PASS item142")


def test_item142_never_raises_with_only_self():
    models_py = "class X:\n    x = fields.Char(inverse='_set_x')\n    def _set_x(self):\n        pass\n"
    assert _raises(_validate_inverse_function_signature_takes_only_self, _gen(models_py)) is None
    print("PASS item142 negative")


# --- Item 143 ---
def test_item143_raises_on_duplicate_onchange_registration():
    models_py = (
        "class X:\n    def _onchange_a(self):\n        pass\n"
        "    @api.onchange('x', 'y')\n    def _onchange_a(self):\n        pass\n"
        "    @api.onchange('y', 'x')\n    def _onchange_b(self):\n        pass\n"
    )
    assert _raises(_validate_no_duplicate_onchange_registration_for_same_field_set, _gen(models_py))
    print("PASS item143")


# --- Item 144 (documented no-op) ---
def test_item144_is_a_documented_noop():
    models_py = "class X(models.Model):\n    partner_id = fields.Many2one('some.wizard.model')\n"
    assert _araises(_validate_transient_model_not_targeted_by_persisted_relation(_gen(models_py), "odoo16_dev")) is None
    print("PASS item144: documented no-op, no model-kind-aware live lookup tool exists")


# --- Item 145 (async) ---
def test_item145_raises_on_undeclared_onchange_field():
    models_py = "class X:\n    _name = 'x.model'\n    amount = fields.Integer()\n    @api.onchange('missing_field')\n    def _onchange(self):\n        pass\n"
    assert _araises(_validate_onchange_decorator_field_args_exist(_gen(models_py), "odoo16_dev"))
    print("PASS item145")


def test_item145_never_raises_on_declared_field():
    models_py = "class X:\n    _name = 'x.model'\n    amount = fields.Integer()\n    @api.onchange('amount')\n    def _onchange(self):\n        pass\n"
    assert _araises(_validate_onchange_decorator_field_args_exist(_gen(models_py), "odoo16_dev")) is None
    print("PASS item145 negative")


# --- Item 146 ---
def test_item146_raises_on_dead_end_required_readonly():
    models_py = "class X:\n    _name = 'x.model'\n    x = fields.Char(readonly=True)\n"
    views_xml = '<field name="x" required="1"/>'
    assert _raises(_validate_view_required_field_not_readonly_only, _gen(models_py, views_xml=views_xml))
    print("PASS item146")


def test_item146_never_raises_with_attrs_override():
    models_py = "class X:\n    _name = 'x.model'\n    x = fields.Char(readonly=True)\n"
    views_xml = "<field name=\"x\" required=\"1\" attrs=\"{'readonly': [('state', '=', 'done')]}\"/>"
    assert _raises(_validate_view_required_field_not_readonly_only, _gen(models_py, views_xml=views_xml)) is None
    print("PASS item146 negative")


# --- Item 147 ---
def test_item147_raises_on_invalid_editable_value():
    assert _raises(_validate_tree_editable_attr_value, _gen(views_xml='<tree editable="middle"/>'))
    print("PASS item147")


def test_item147_never_raises_on_valid_editable_value():
    assert _raises(_validate_tree_editable_attr_value, _gen(views_xml='<tree editable="bottom"/>')) is None
    print("PASS item147 negative")


# --- Item 148 ---
def test_item148_raises_on_progressbar_color_key_mismatch():
    models_py = "class X:\n    _name = 'x.model'\n    state = fields.Selection([('a', 'A'), ('b', 'B')])\n"
    views_xml = '<progressbar field="state" colors="{\'c\': \'danger\'}"/>'
    assert _raises(_validate_kanban_progressbar_colors_match_selection_options, _gen(models_py, views_xml=views_xml))
    print("PASS item148")


# --- Item 149 (extends item 34) ---
def test_item149_raises_on_invalid_calendar_mode():
    assert _raises(_validate_calendar_mode_and_color_attrs_are_valid, _gen(views_xml='<calendar mode="fortnight"/>'))
    print("PASS item149")


def test_item149_never_raises_on_valid_calendar_mode():
    assert _raises(_validate_calendar_mode_and_color_attrs_are_valid, _gen(views_xml='<calendar mode="week"/>')) is None
    print("PASS item149 negative")


# --- Item 150 ---
def test_item150_raises_on_invalid_graph_type():
    assert _raises(_validate_pivot_graph_field_type_attrs_are_valid_enum, _gen(views_xml='<graph type="donut"/>'))
    print("PASS item150")


def test_item150_never_raises_on_valid_graph_type():
    assert _raises(_validate_pivot_graph_field_type_attrs_are_valid_enum, _gen(views_xml='<graph type="bar"/>')) is None
    print("PASS item150 negative")


# --- Item 151 ---
def test_item151_raises_on_undeclared_searchpanel_field():
    models_py = "class X:\n    _name = 'x.model'\n"
    views_xml = '<searchpanel><field name="missing_field"/></searchpanel>'
    assert _raises(_validate_search_panel_field_exists_on_model, _gen(models_py, views_xml=views_xml))
    print("PASS item151")


# --- Item 152 ---
def test_item152_raises_on_undeclared_activity_template_field():
    views_xml = '<activity><div t-esc="record.missing_field"/></activity>'
    assert _raises(_validate_activity_view_template_fields_exist_on_model, _gen(views_xml=views_xml))
    print("PASS item152")


def test_item152_never_raises_on_declared_activity_field():
    views_xml = '<activity><field name="amount"/><div t-esc="record.amount"/></activity>'
    assert _raises(_validate_activity_view_template_fields_exist_on_model, _gen(views_xml=views_xml)) is None
    print("PASS item152 negative")


# --- Item 154 ---
def test_item154_raises_on_deprecated_clickable_options():
    views_xml = '<field name="state" widget="statusbar" options="{\'clickable\': \'1\'}"/>'
    assert _raises(_validate_statusbar_no_deprecated_options_dict_syntax, _gen(views_xml=views_xml))
    print("PASS item154")


# --- Item 155 ---
def test_item155_raises_on_non_numeric_priority_keys():
    models_py = "class X:\n    _name = 'x.model'\n    state = fields.Selection([('low', 'Low'), ('high', 'High')])\n"
    views_xml = '<field name="state" widget="priority"/>'
    assert _raises(_validate_priority_widget_selection_keys_are_numeric_strings, _gen(models_py, views_xml=views_xml))
    print("PASS item155")


def test_item155_never_raises_on_numeric_priority_keys():
    models_py = "class X:\n    _name = 'x.model'\n    state = fields.Selection([('0', 'Low'), ('1', 'High')])\n"
    views_xml = '<field name="state" widget="priority"/>'
    assert _raises(_validate_priority_widget_selection_keys_are_numeric_strings, _gen(models_py, views_xml=views_xml)) is None
    print("PASS item155 negative")


# --- Item 156 ---
def test_item156_raises_on_invalid_eval_syntax():
    views_xml = '<field name="groups" eval="[(4, ref(&apos;base.group_user&apos;)"/>'
    assert _raises(_validate_xml_eval_attributes_are_valid_python_literals, _gen(views_xml=views_xml))
    print("PASS item156")


def test_item156_never_raises_on_valid_eval_syntax():
    views_xml = "<field name=\"groups\" eval=\"[(4, ref('base.group_user'))]\"/>"
    assert _raises(_validate_xml_eval_attributes_are_valid_python_literals, _gen(views_xml=views_xml)) is None
    print("PASS item156 negative")


# --- Item 157 ---
def test_item157_raises_on_invalid_report_type():
    xml = '<record model="ir.actions.report"><field name="report_type">qweb-json</field></record>'
    assert _raises(_validate_report_action_report_type_is_valid_enum, _gen(security_xml=xml))
    print("PASS item157")


def test_item157_never_raises_on_valid_report_type():
    xml = '<record model="ir.actions.report"><field name="report_type">qweb-pdf</field></record>'
    assert _raises(_validate_report_action_report_type_is_valid_enum, _gen(security_xml=xml)) is None
    print("PASS item157 negative")


# --- Item 158 ---
def test_item158_raises_on_invalid_print_report_name():
    xml = '<record model="ir.actions.report"><field name="print_report_name">object.name +</field></record>'
    assert _raises(_validate_report_print_report_name_is_valid_python_expression, _gen(security_xml=xml))
    print("PASS item158")


def test_item158_never_raises_on_valid_print_report_name():
    xml = "<record model=\"ir.actions.report\"><field name=\"print_report_name\">'Report - %s' % object.name</field></record>"
    assert _raises(_validate_report_print_report_name_is_valid_python_expression, _gen(security_xml=xml)) is None
    print("PASS item158 negative")


# --- Item 159 (documented no-op) ---
def test_item159_is_a_documented_noop():
    xml = '<record model="ir.actions.server"><field name="state">object_write</field><field name="model_id" ref="base.model_res_partner"/></record>'
    assert _araises(_validate_server_action_object_write_fields_reference_real_target_fields(_gen(security_xml=xml), "odoo16_dev")) is None
    print("PASS item159: documented no-op, no xmlid-to-model-name resolution tool exists")


# --- Item 160 ---
def test_item160_raises_on_non_integer_interval_number():
    xml = '<record model="ir.cron"><field name="interval_number">daily</field></record>'
    assert _raises(_validate_ir_cron_numeric_fields_are_numeric_literals, _gen(security_xml=xml))
    print("PASS item160")


def test_item160_never_raises_on_negative_one_numbercall():
    xml = '<record model="ir.cron"><field name="numbercall">-1</field></record>'
    assert _raises(_validate_ir_cron_numeric_fields_are_numeric_literals, _gen(security_xml=xml)) is None
    print("PASS item160: -1 (run forever) is a valid special case, never flagged")


# --- Item 162 ---
def test_item162_raises_when_goal_never_mentions_delete():
    csv = "id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink\naccess_x,x,model_x,base.group_user,1,1,1,1\n"
    assert _raises(_validate_security_csv_perm_unlink_matches_goal_intent, _gen(security_csv=csv), "Add a field to track status.")
    print("PASS item162")


def test_item162_never_raises_when_goal_mentions_delete():
    csv = "id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink\naccess_x,x,model_x,base.group_user,1,1,1,1\n"
    assert _raises(_validate_security_csv_perm_unlink_matches_goal_intent, _gen(security_csv=csv), "Allow admins to delete records.") is None
    print("PASS item162 negative")


# --- _autofix_security_csv_perm_unlink_defaults_to_zero (real task010 content) ---
# Real, confirmed bug found live (2026-08-03, task010's own repeated real stall): item 162's
# validator above only ever flagged perm_unlink=1-without-delete-intent as an advisory ValueError,
# burning a full round to ask Build to fix something this project's own deterministic CSV builder
# already has an established default answer for (perm_unlink=0 unless the goal explicitly asks for
# delete). This autofix applies that same established default automatically.

def test_autofix_downgrades_perm_unlink_to_zero_on_task010_real_shape():
    csv = (
        "id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink\n"
        "access_project_fieldjob_group_mechanics,project.fieldjob,"
        "project_fieldjob.model_project_fieldjob,oma_x.group_mechanics,1,1,1,1"
    )
    gen = _gen(security_csv=csv)
    goal = "Mechanics should only see the fieldjob records they created themselves."
    _autofix_security_csv_perm_unlink_defaults_to_zero(gen, goal)
    assert gen.security_csv.endswith(",1,1,1,0")
    assert _raises(_validate_security_csv_perm_unlink_matches_goal_intent, gen, goal) is None
    print("PASS: task010's real perm_unlink=1 row is downgraded to 0, validator no longer raises")


def test_autofix_is_a_no_op_when_goal_explicitly_requests_delete():
    csv = (
        "id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink\n"
        "access_x,x,model_x,base.group_user,1,1,1,1"
    )
    gen = _gen(security_csv=csv)
    goal = "Allow admins to delete records."
    _autofix_security_csv_perm_unlink_defaults_to_zero(gen, goal)
    assert gen.security_csv == csv
    print("PASS: an explicit delete-intent goal is left untouched")


def test_autofix_is_a_no_op_when_perm_unlink_already_zero():
    csv = (
        "id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink\n"
        "access_x,x,model_x,base.group_user,1,1,1,0"
    )
    gen = _gen(security_csv=csv)
    _autofix_security_csv_perm_unlink_defaults_to_zero(gen, "Add a field to track status.")
    assert gen.security_csv == csv
    print("PASS: already-zero perm_unlink is left untouched")


# --- Item 163 ---
def test_item163_raises_when_no_ir_rule_at_all():
    goal = "Restrict visibility so each company only sees its own records (multi-company)."
    assert _raises(_validate_goal_named_company_restriction_is_implemented, _gen(), goal, None)
    print("PASS item163")


def test_item163_never_raises_when_correctly_implemented():
    goal = "Restrict visibility so each company only sees its own records (multi-company)."
    xml = "<record model=\"ir.rule\"><field name=\"domain_force\">[('company_id', '=', user.company_id.id)]</field></record>"
    assert _raises(_validate_goal_named_company_restriction_is_implemented, _gen(security_xml=xml), goal, None) is None
    print("PASS item163 negative")


# --- Item 164 ---
def test_item164_raises_on_wrong_series():
    manifest = ManifestFields(name="x", version="17.0.1.0.0", category="Tools", summary="x", author="x", depends=["base"], data=[])
    assert _raises(_validate_manifest_version_matches_odoo_series, _gen(manifest=manifest))
    print("PASS item164")


def test_item164_never_raises_on_correct_series():
    assert _raises(_validate_manifest_version_matches_odoo_series, _gen()) is None
    print("PASS item164 negative")


def test_item164_autofix_prefixes_bare_semver_with_odoo_series():
    manifest = ManifestFields(name="x", version="1.0.0", category="Tools", summary="x", author="x", depends=["base"], data=[])
    generated = _gen(manifest=manifest)
    _autofix_manifest_version_prefixed_with_odoo_series(generated)
    assert generated.manifest_fields.version == "16.0.1.0.0"
    assert _raises(_validate_manifest_version_matches_odoo_series, generated) is None
    print("PASS item164 autofix prefixes bare semver, validator clean afterward")


def test_item164_autofix_never_touches_an_already_correct_version():
    generated = _gen()
    before = generated.manifest_fields.version
    _autofix_manifest_version_prefixed_with_odoo_series(generated)
    assert generated.manifest_fields.version == before
    print("PASS item164 autofix no-op on already-correct series")


# --- Item 165 ---
def test_item165_removes_self_dependency():
    manifest = ManifestFields(name="x", version="16.0.1.0.0", category="Tools", summary="x", author="x", depends=["base", "oma_self_module"], data=[])
    generated = _gen(manifest=manifest)
    _autofix_manifest_removes_self_dependency(generated, "oma_self_module")
    assert "oma_self_module" not in generated.manifest_fields.depends
    assert "base" in generated.manifest_fields.depends
    print("PASS item165")


# --- Item 166 ---
def test_item166_raises_without_super_call():
    models_py = "class X:\n    def _get_report_values(self, docids, data=None):\n        return {'doc_ids': docids}\n"
    assert _raises(_validate_custom_report_model_get_report_values_shape, _gen(models_py))
    print("PASS item166")


def test_item166_never_raises_with_super_call():
    models_py = "class X:\n    def _get_report_values(self, docids, data=None):\n        values = super()._get_report_values(docids, data)\n        return values\n"
    assert _raises(_validate_custom_report_model_get_report_values_shape, _gen(models_py)) is None
    print("PASS item166 negative")


# --- Item 167 (extends item 68) ---
def test_item167_raises_on_custom_format_without_dimensions():
    xml = '<record model="report.paperformat"><field name="format">custom</field></record>'
    assert _raises(_validate_paperformat_custom_format_requires_dimensions, _gen(security_xml=xml))
    print("PASS item167")


def test_item167_never_raises_with_dimensions():
    xml = (
        '<record model="report.paperformat"><field name="format">custom</field>'
        '<field name="page_width">210</field><field name="page_height">297</field></record>'
    )
    assert _raises(_validate_paperformat_custom_format_requires_dimensions, _gen(security_xml=xml)) is None
    print("PASS item167 negative")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
    print(f"\nALL {len(fns)} P11 DEPTH-AUDIT TIER C TESTS PASSED")
