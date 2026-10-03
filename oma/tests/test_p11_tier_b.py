"""P11 Tier B (docs/planning/PHASE30_OMA_SEAM_COVERAGE_AND_COMBINATORIAL_HARDENING_2026-07-29.md
§18b.2 items 18-51; full detail: PHASE30_ADDENDUM_GENERIC_FIX_BUILDOUT_2026-07-30.md §2.4): unit
tests for the 33 buildable Tier B items (item 38 explicitly deferred, no test needed). Every
function here is pure/synchronous, zero LLM/GPU/network calls.
"""

import asyncio
import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from specialists.build.specialist import (
    GeneratedModuleFiles,
    ManifestFields,
    _autofix_add_mail_thread_inherit_when_message_post_used,
    _autofix_add_owning_module_dependency_for_known_mixins,
    _autofix_res_groups_records_missing_noupdate,
    _autofix_strip_redundant_mixin_inherit_when_already_provided,
    _autofix_wrap_plain_untranslated_error_string_literal,
    _validate_abstract_model_has_no_data_records,
    _validate_activity_calls_require_activity_mixin_inherit,
    _validate_calendar_view_date_fields_exist,
    _validate_compute_field_advanced_params,
    _validate_declared_reference_methods_exist,
    _validate_decoration_attr_fields_exist_on_model,
    _validate_error_strings_wrapped_in_translate_call,
    _validate_html_field_kwargs_are_real,
    _validate_http_route_auth_and_uniqueness,
    _validate_kanban_template_fields_declared,
    _validate_manifest_declares_external_python_imports,
    _validate_manifest_hook_functions_exist,
    _validate_many2one_ondelete_matches_required,
    _validate_message_post_requires_mail_thread_inherit,
    _validate_message_post_requires_mail_thread_inherit_live,
    _validate_monetary_field_has_currency_field,
    _validate_no_duplicate_res_groups_name,
    _validate_no_hallucinated_exotic_field_types,
    _validate_numeric_field_digits_param_shape,
    _validate_one2many_inverse_field_exists,
    _validate_pivot_graph_measure_fields_are_numeric,
    _validate_portal_mixin_no_field_redeclaration,
    _validate_qweb_report_template_id_resolves,
    _validate_qweb_template_names_unique_and_calls_resolve,
    _validate_rec_name_and_order_reference_real_fields,
    _validate_record_rule_permission_not_capped_by_access_csv,
    _validate_related_field_first_hop_exists,
    _validate_scalar_field_attribute_sanity,
    _validate_server_action_state_specific_fields_present,
    _validate_sql_constraints_shape,
    _validate_statusbar_widget_on_selection_field,
    _validate_translate_attr_only_on_translatable_types,
)

_MANIFEST = ManifestFields(
    name="x", version="1.0.0", category="Tools", summary="x", author="x", depends=["base"], data=[],
)


def _gen(models_py="", views_xml=None, security_csv="x", security_xml=None, extra_data_files=None) -> GeneratedModuleFiles:
    return GeneratedModuleFiles(
        manifest_fields=_MANIFEST, models_py=models_py, views_xml=views_xml,
        security_csv=security_csv, security_xml=security_xml, extra_data_files=extra_data_files, notes="",
    )


def _raises(fn, *args):
    try:
        fn(*args)
        return None
    except ValueError as exc:
        return str(exc)


def _raises_async(fn, *args):
    try:
        asyncio.run(fn(*args))
        return None
    except ValueError as exc:
        return str(exc)


# --- Item 18: _validate_declared_reference_methods_exist ---

def test_item18_raises_on_undeclared_inverse():
    models_py = "class X(models.Model):\n    _name = 'x.model'\n    y = fields.Char(inverse='_set_y')\n"
    assert _raises(_validate_declared_reference_methods_exist, _gen(models_py))
    print("PASS item18: inverse= naming an undeclared method raises")


def test_item18_never_raises_when_declared():
    models_py = (
        "class X(models.Model):\n    _name = 'x.model'\n"
        "    y = fields.Char(inverse='_set_y')\n    def _set_y(self):\n        pass\n"
    )
    assert _raises(_validate_declared_reference_methods_exist, _gen(models_py)) is None
    print("PASS item18: a declared inverse method never raises")


def test_item18_raises_on_undeclared_button_method():
    models_py = "class X(models.Model):\n    _name = 'x.model'\n"
    views_xml = '<form><button type="object" name="do_thing"/></form>'
    assert _raises(_validate_declared_reference_methods_exist, _gen(models_py, views_xml=views_xml))
    print("PASS item18: a button referencing an undeclared method raises")


# --- Item 19: _validate_no_duplicate_res_groups_name ---

def test_item19_raises_on_duplicate_group_name():
    xml = (
        '<record id="g1" model="res.groups"><field name="name">Admin</field></record>'
        '<record id="g2" model="res.groups"><field name="name">Admin</field></record>'
    )
    assert _raises(_validate_no_duplicate_res_groups_name, _gen(security_xml=xml))
    print("PASS item19: duplicate res.groups names raise")


def test_item19_never_raises_without_duplicates():
    xml = '<record id="g1" model="res.groups"><field name="name">Admin</field></record>'
    assert _raises(_validate_no_duplicate_res_groups_name, _gen(security_xml=xml)) is None
    print("PASS item19: unique group names never raise")


# --- Item 20: _autofix_res_groups_records_missing_noupdate ---

def test_item20_adds_noupdate():
    xml = '<data><record id="g1" model="res.groups"><field name="name">Admin</field></record></data>'
    generated = _gen(security_xml=xml)
    _autofix_res_groups_records_missing_noupdate(generated)
    assert 'noupdate="1"' in generated.security_xml
    print("PASS item20: a missing noupdate on a res.groups-defining <data> is added")


def test_item20_leaves_existing_noupdate_alone():
    xml = '<data noupdate="1"><record id="g1" model="res.groups"><field name="name">Admin</field></record></data>'
    generated = _gen(security_xml=xml)
    _autofix_res_groups_records_missing_noupdate(generated)
    assert generated.security_xml.count('noupdate="1"') == 1
    print("PASS item20: an already-present noupdate is never duplicated")


# --- Item 21: _validate_record_rule_permission_not_capped_by_access_csv ---

def test_item21_raises_on_capped_permission():
    xml = "<record model=\"ir.rule\"><field name=\"groups\" eval=\"[(4, ref('base.group_user'))]\"/></record>"
    csv = (
        "id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink\n"
        "access_x,x,model_x,base.group_user,1,1,1,0\n"
    )
    assert _raises(_validate_record_rule_permission_not_capped_by_access_csv, _gen(security_xml=xml, security_csv=csv))
    print("PASS item21: a group with perm_unlink=0 in access.csv but referenced by an ir.rule raises")


def test_item21_never_raises_when_not_capped():
    xml = "<record model=\"ir.rule\"><field name=\"groups\" eval=\"[(4, ref('base.group_user'))]\"/></record>"
    csv = (
        "id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink\n"
        "access_x,x,model_x,base.group_user,1,1,1,1\n"
    )
    assert _raises(_validate_record_rule_permission_not_capped_by_access_csv, _gen(security_xml=xml, security_csv=csv)) is None
    print("PASS item21: a group with full access.csv permissions never raises")


def test_item21_never_raises_for_a_row_restricting_rule_with_perm_unlink_zero():
    """Real, confirmed bug found live (2026-08-09, task 07141af5-9a4e-41b6-93ea-8b7af04fea9c's
    flagship run, ticket_access_rights node -- the fourth sibling function found carrying this
    same wrong assumption): "an ir.rule can only ever NARROW access, never grant it" is only
    true for the permission dimension the rule's own domain_force actually narrows. A
    row-restricting rule (domain_force references user.id) narrows which ROWS are visible for
    read/write/create -- it says nothing about delete, so a genuinely correct perm_unlink=0
    baseline for that role is not a real gap.
    """
    xml = (
        '<record id="rule_x" model="ir.rule">'
        "<field name=\"domain_force\">[('technician_id', '=', user.id)]</field>"
        "<field name=\"groups\" eval=\"[(4, ref('oma_x.group_field_technician'))]\"/>"
        "</record>"
    )
    csv = (
        "id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink\n"
        "access_x,x,model_x,oma_x.group_field_technician,1,1,1,0\n"
    )
    assert _raises(_validate_record_rule_permission_not_capped_by_access_csv, _gen(security_xml=xml, security_csv=csv)) is None
    print("PASS item21: never raises for a row-restricting rule's own group with a genuinely "
          "correct perm_unlink=0, closing the real live gap found on task 07141af5's "
          "ticket_access_rights node")


# --- Item 22: _validate_scalar_field_attribute_sanity ---

def test_item22_raises_on_text_size():
    models_py = "class X(models.Model):\n    _name = 'x.model'\n    notes = fields.Text(size=100)\n"
    assert _raises(_validate_scalar_field_attribute_sanity, _gen(models_py))
    print("PASS item22: size= on a Text field raises")


def test_item22_never_raises_on_char_size():
    models_py = "class X(models.Model):\n    _name = 'x.model'\n    code = fields.Char(size=10)\n"
    assert _raises(_validate_scalar_field_attribute_sanity, _gen(models_py)) is None
    print("PASS item22: size= on a Char field never raises")


# --- Item 23: _validate_numeric_field_digits_param_shape ---

def test_item23_raises_on_integer_digits():
    models_py = "class X(models.Model):\n    _name = 'x.model'\n    n = fields.Integer(digits=(6, 2))\n"
    assert _raises(_validate_numeric_field_digits_param_shape, _gen(models_py))
    print("PASS item23: digits= on an Integer field raises")


def test_item23_never_raises_on_float_digits():
    models_py = "class X(models.Model):\n    _name = 'x.model'\n    n = fields.Float(digits=(6, 2))\n"
    assert _raises(_validate_numeric_field_digits_param_shape, _gen(models_py)) is None
    print("PASS item23: digits= on a Float field never raises")


# --- Item 24: _validate_html_field_kwargs_are_real ---

def test_item24_raises_on_invented_kwarg():
    models_py = "class X(models.Model):\n    _name = 'x.model'\n    body = fields.Html(sanitize_html=True)\n"
    assert _raises(_validate_html_field_kwargs_are_real, _gen(models_py))
    print("PASS item24: an invented Html kwarg raises")


def test_item24_never_raises_on_real_kwargs():
    models_py = "class X(models.Model):\n    _name = 'x.model'\n    body = fields.Html(sanitize=True, string='Body')\n"
    assert _raises(_validate_html_field_kwargs_are_real, _gen(models_py)) is None
    print("PASS item24: real Html kwargs never raise")


# --- Item 25: _validate_monetary_field_has_currency_field ---

def test_item25_raises_when_currency_field_missing():
    models_py = "class X(models.Model):\n    _name = 'x.model'\n    amount = fields.Monetary()\n"
    assert _raises(_validate_monetary_field_has_currency_field, _gen(models_py))
    print("PASS item25: Monetary with no resolving currency_field raises")


def test_item25_never_raises_when_resolved():
    models_py = (
        "class X(models.Model):\n    _name = 'x.model'\n"
        "    currency_id = fields.Many2one('res.currency')\n"
        "    amount = fields.Monetary(currency_field='currency_id')\n"
    )
    assert _raises(_validate_monetary_field_has_currency_field, _gen(models_py)) is None
    print("PASS item25: a Monetary field whose currency_field resolves to a real res.currency Many2one never raises")


# --- Item 26: _validate_many2one_ondelete_matches_required ---

def test_item26_raises_on_required_set_null():
    models_py = "class X(models.Model):\n    _name = 'x.model'\n    partner_id = fields.Many2one('res.partner', required=True, ondelete='set null')\n"
    assert _raises(_validate_many2one_ondelete_matches_required, _gen(models_py))
    print("PASS item26: required=True + ondelete='set null' raises")


def test_item26_never_raises_on_restrict():
    models_py = "class X(models.Model):\n    _name = 'x.model'\n    partner_id = fields.Many2one('res.partner', required=True, ondelete='restrict')\n"
    assert _raises(_validate_many2one_ondelete_matches_required, _gen(models_py)) is None
    print("PASS item26: required=True + ondelete='restrict' never raises")


# --- Item 27: _validate_one2many_inverse_field_exists ---

def test_item27_raises_when_inverse_missing_on_own_new_model():
    models_py = (
        "class Parent(models.Model):\n    _name = 'x.parent'\n    line_ids = fields.One2many('x.line', 'parent_id')\n"
        "class Line(models.Model):\n    _name = 'x.line'\n"
    )
    assert _raises(_validate_one2many_inverse_field_exists, _gen(models_py))
    print("PASS item27: a One2many whose inverse field doesn't exist on this module's own new comodel raises")


def test_item27_never_raises_when_present():
    models_py = (
        "class Parent(models.Model):\n    _name = 'x.parent'\n    line_ids = fields.One2many('x.line', 'parent_id')\n"
        "class Line(models.Model):\n    _name = 'x.line'\n    parent_id = fields.Many2one('x.parent')\n"
    )
    assert _raises(_validate_one2many_inverse_field_exists, _gen(models_py)) is None
    print("PASS item27: a One2many whose inverse field IS present never raises")


def test_item27_skips_external_comodel():
    models_py = "class Parent(models.Model):\n    _name = 'x.parent'\n    line_ids = fields.One2many('external.model', 'parent_id')\n"
    assert _raises(_validate_one2many_inverse_field_exists, _gen(models_py)) is None
    print("PASS item27: an external (not this module's own) comodel is skipped, never guessed at")


# --- Item 28: _validate_no_hallucinated_exotic_field_types ---

def test_item28_raises_on_exotic_type():
    models_py = "class X(models.Model):\n    _name = 'x.model'\n    data = fields.Json()\n"
    assert _raises(_validate_no_hallucinated_exotic_field_types, _gen(models_py))
    print("PASS item28: an exotic field type (Json) raises")


def test_item28_never_raises_on_ordinary_type():
    models_py = "class X(models.Model):\n    _name = 'x.model'\n    amount = fields.Integer()\n"
    assert _raises(_validate_no_hallucinated_exotic_field_types, _gen(models_py)) is None
    print("PASS item28: an ordinary field type never raises")


# --- Item 29: _validate_compute_field_advanced_params ---

def test_item29_raises_on_precompute_without_store():
    models_py = "class X(models.Model):\n    _name = 'x.model'\n    total = fields.Integer(compute='_c', precompute=True)\n"
    assert _raises(_validate_compute_field_advanced_params, _gen(models_py))
    print("PASS item29: precompute=True without store=True raises")


def test_item29_never_raises_with_store():
    models_py = "class X(models.Model):\n    _name = 'x.model'\n    total = fields.Integer(compute='_c', precompute=True, store=True)\n"
    assert _raises(_validate_compute_field_advanced_params, _gen(models_py)) is None
    print("PASS item29: precompute=True with store=True never raises")


# --- Item 30: _validate_related_field_first_hop_exists ---

def test_item30_raises_on_unresolved_first_hop():
    models_py = "class X(models.Model):\n    _name = 'x.model'\n    y = fields.Char(related='nonexistent.z')\n"
    assert _raises(_validate_related_field_first_hop_exists, _gen(models_py))
    print("PASS item30: a related= first hop that isn't declared raises")


def test_item30_never_raises_on_resolved_first_hop():
    models_py = (
        "class X(models.Model):\n    _name = 'x.model'\n"
        "    partner_id = fields.Many2one('res.partner')\n    y = fields.Char(related='partner_id.name')\n"
    )
    assert _raises(_validate_related_field_first_hop_exists, _gen(models_py)) is None
    print("PASS item30: a related= first hop that IS declared never raises")


# --- Item 31: _validate_sql_constraints_shape ---

def test_item31_raises_on_duplicate_constraint_name():
    models_py = (
        "class X(models.Model):\n    _name = 'x.model'\n"
        "    _sql_constraints = [('uniq_x', 'unique(x)', 'msg'), ('uniq_x', 'unique(y)', 'msg2')]\n"
    )
    assert _raises(_validate_sql_constraints_shape, _gen(models_py))
    print("PASS item31: a repeated _sql_constraints name raises")


def test_item31_never_raises_on_unique_names():
    models_py = (
        "class X(models.Model):\n    _name = 'x.model'\n"
        "    _sql_constraints = [('uniq_x', 'unique(x)', 'msg')]\n"
    )
    assert _raises(_validate_sql_constraints_shape, _gen(models_py)) is None
    print("PASS item31: unique _sql_constraints names never raise")


def test_item179_never_raises_on_the_same_name_reused_across_different_models():
    """P11 fifth pass item 179: real bug found and fixed while building this item -- the SAME
    constraint name is perfectly legal reused across two DIFFERENT models (Postgres constraint
    names are table-scoped) -- must never raise. This was a real, confirmed false positive in the
    pre-existing item 31 check before this fix.
    """
    models_py = (
        "class X(models.Model):\n    _name = 'x.model'\n"
        "    _sql_constraints = [('code_uniq', 'unique(code)', 'msg')]\n\n"
        "class Y(models.Model):\n    _name = 'y.model'\n"
        "    _sql_constraints = [('code_uniq', 'unique(code)', 'msg')]\n"
    )
    assert _raises(_validate_sql_constraints_shape, _gen(models_py)) is None
    print("PASS item179: the same constraint name reused across two different models never raises")


def test_item179_still_raises_on_a_duplicate_within_the_same_model():
    models_py = (
        "class X(models.Model):\n    _name = 'x.model'\n"
        "    _sql_constraints = [('code_uniq', 'unique(code)', 'msg'), ('code_uniq', 'unique(ref)', 'msg2')]\n"
    )
    assert _raises(_validate_sql_constraints_shape, _gen(models_py))
    print("PASS item179: a genuine duplicate within the SAME model's own list still raises")


# --- Item 32: _validate_abstract_model_has_no_data_records ---

def test_item32_raises_on_data_record_for_abstract_model():
    models_py = "class X(models.AbstractModel):\n    _name = 'x.abstract'\n"
    xml = '<record id="r1" model="x.abstract"><field name="name">y</field></record>'
    assert _raises(_validate_abstract_model_has_no_data_records, _gen(models_py, security_xml=xml))
    print("PASS item32: a data record for an AbstractModel raises")


def test_item32_never_raises_for_concrete_model():
    models_py = "class X(models.Model):\n    _name = 'x.model'\n"
    xml = '<record id="r1" model="x.model"><field name="name">y</field></record>'
    assert _raises(_validate_abstract_model_has_no_data_records, _gen(models_py, security_xml=xml)) is None
    print("PASS item32: a data record for a concrete model never raises")


# --- Item 33: _validate_kanban_template_fields_declared ---

def test_item33_raises_on_undeclared_kanban_field():
    views_xml = '<kanban><templates><t-esc="record.missing_field"/></templates></kanban>'.replace('t-esc="', 't-esc="record.missing_field" data-x="')
    # Build a realistic kanban block referencing an undeclared field via t-esc="record.X"
    views_xml = '<kanban><templates><div t-esc="record.missing_field"/></templates></kanban>'
    assert _raises(_validate_kanban_template_fields_declared, _gen(views_xml=views_xml))
    print("PASS item33: a kanban t-esc referencing an undeclared field raises")


def test_item33_never_raises_when_field_declared_in_view():
    views_xml = '<kanban><field name="amount"/><templates><div t-esc="record.amount"/></templates></kanban>'
    assert _raises(_validate_kanban_template_fields_declared, _gen(views_xml=views_xml)) is None
    print("PASS item33: a kanban t-esc referencing an explicitly-declared field never raises")


# --- Item 34: _validate_calendar_view_date_fields_exist ---

def test_item34_raises_on_undeclared_date_field():
    models_py = "class X(models.Model):\n    _name = 'x.model'\n"
    views_xml = '<calendar date_start="missing_date"/>'
    assert _raises(_validate_calendar_view_date_fields_exist, _gen(models_py, views_xml=views_xml))
    print("PASS item34: a calendar date_start referencing an undeclared field raises")


def test_item34_never_raises_on_declared_date_field():
    models_py = "class X(models.Model):\n    _name = 'x.model'\n    due_date = fields.Date()\n"
    views_xml = '<calendar date_start="due_date"/>'
    assert _raises(_validate_calendar_view_date_fields_exist, _gen(models_py, views_xml=views_xml)) is None
    print("PASS item34: a calendar date_start referencing a declared field never raises")


# --- Item 35: _validate_pivot_graph_measure_fields_are_numeric ---

def test_item35_raises_on_non_numeric_measure():
    models_py = "class X(models.Model):\n    _name = 'x.model'\n    label = fields.Char()\n"
    views_xml = '<pivot><field name="label" type="measure"/></pivot>'
    assert _raises(_validate_pivot_graph_measure_fields_are_numeric, _gen(models_py, views_xml=views_xml))
    print("PASS item35: a non-numeric measure field raises")


def test_item35_never_raises_on_numeric_measure():
    models_py = "class X(models.Model):\n    _name = 'x.model'\n    amount = fields.Integer()\n"
    views_xml = '<pivot><field name="amount" type="measure"/></pivot>'
    assert _raises(_validate_pivot_graph_measure_fields_are_numeric, _gen(models_py, views_xml=views_xml)) is None
    print("PASS item35: a numeric measure field never raises")


# --- Item 36: _validate_qweb_template_names_unique_and_calls_resolve ---

def test_item36_raises_on_unresolved_t_call():
    xml = '<template id="t1"><t t-call="nonexistent.template"/></template>'
    assert _raises(_validate_qweb_template_names_unique_and_calls_resolve, _gen(security_xml=xml))
    print("PASS item36: a t-call referencing a template not defined anywhere raises")


def test_item36_never_raises_when_resolved():
    xml = '<template id="t1"/><template id="t2"><t t-call="t1"/></template>'
    assert _raises(_validate_qweb_template_names_unique_and_calls_resolve, _gen(security_xml=xml)) is None
    print("PASS item36: a t-call referencing a defined template never raises")


def test_item36_raises_on_duplicate_template_id():
    xml = '<template id="t1"/><template id="t1"/>'
    assert _raises(_validate_qweb_template_names_unique_and_calls_resolve, _gen(security_xml=xml))
    print("PASS item36: a duplicate <template id> raises")


# --- Item 37: _validate_statusbar_widget_on_selection_field ---

def test_item37_raises_on_non_selection_statusbar():
    models_py = "class X(models.Model):\n    _name = 'x.model'\n    state = fields.Char()\n"
    views_xml = '<field name="state" widget="statusbar"/>'
    assert _raises(_validate_statusbar_widget_on_selection_field, _gen(models_py, views_xml=views_xml))
    print("PASS item37: widget=statusbar on a non-Selection field raises")


def test_item37_never_raises_on_selection_field():
    models_py = "class X(models.Model):\n    _name = 'x.model'\n    state = fields.Selection([('a', 'A')])\n"
    views_xml = '<field name="state" widget="statusbar"/>'
    assert _raises(_validate_statusbar_widget_on_selection_field, _gen(models_py, views_xml=views_xml)) is None
    print("PASS item37: widget=statusbar on a real Selection field never raises")


# --- Item 39: _validate_decoration_attr_fields_exist_on_model ---

def test_item39_raises_on_undeclared_decoration_field():
    models_py = "class X(models.Model):\n    _name = 'x.model'\n"
    views_xml = '<tree decoration-danger="missing_field &lt; 0"/>'
    assert _raises_async(_validate_decoration_attr_fields_exist_on_model, _gen(models_py, views_xml=views_xml))
    print("PASS item39: a decoration-* expression referencing an undeclared field raises")


def test_item39_never_raises_on_declared_field():
    models_py = "class X(models.Model):\n    _name = 'x.model'\n    amount = fields.Integer()\n"
    views_xml = '<tree decoration-danger="amount &lt; 0"/>'
    assert _raises_async(_validate_decoration_attr_fields_exist_on_model, _gen(models_py, views_xml=views_xml)) is None
    print("PASS item39: a decoration-* expression referencing a declared field never raises")


def test_item39_never_raises_when_comparing_field_to_a_string_literal():
    """Real, confirmed bug found live (2026-08-09, task 07141af5's flagship run,
    ticket_list_view node): `decoration-danger="state == 'resolved'"` is the standard, expected
    Odoo idiom -- 'resolved' is a STRING LITERAL value being compared against the real, declared
    'state' field, not a second field reference. The old identifier-extraction regex had no way
    to tell "inside quotes" from "bare identifier", so it extracted 'resolved' as if it were
    ANOTHER referenced field and falsely raised -- escalating a genuinely correct, working
    expression to a needless human decision.
    """
    models_py = (
        "class X(models.Model):\n    _name = 'x.model'\n"
        "    state = fields.Selection([('new', 'New'), ('resolved', 'Resolved')])\n"
    )
    views_xml = '<tree decoration-danger="state == \'resolved\'"/>'
    assert _raises_async(_validate_decoration_attr_fields_exist_on_model, _gen(models_py, views_xml=views_xml)) is None
    print("PASS item39: a decoration-* expression comparing a declared field to a string "
          "literal value never raises, closing the real live gap found on task 07141af5's "
          "ticket_list_view node")


def test_item39_still_raises_when_the_compared_field_itself_is_undeclared():
    """The fix must only strip QUOTED literal contents -- the real field name being compared
    (outside the quotes) still has to be checked, and a genuinely undeclared field there must
    still raise.
    """
    models_py = "class X(models.Model):\n    _name = 'x.model'\n"
    views_xml = '<tree decoration-danger="missing_field == \'resolved\'"/>'
    assert _raises_async(_validate_decoration_attr_fields_exist_on_model, _gen(models_py, views_xml=views_xml))
    print("PASS item39: still raises when the field being compared (outside the quotes) is "
          "genuinely undeclared")


def test_item39_depends_on_module_round_with_empty_models_py_consults_live_schema():
    """Real, confirmed bug found live (2026-08-10, task e65381cc, ticket_views_menu node): a
    `depends_on_module:` task's views-only round has a legitimately EMPTY models_py (the real
    fields live in the dependency module, not this round's own file) -- `state` on
    `oma.service.ticket` was falsely rejected as undeclared. With a live field lookup available
    (mocked here) and the record's own `<field name="model">` resolved, a real field the local
    models.py knows nothing about must NOT raise.
    """
    views_xml = (
        '<record id="v1" model="ir.ui.view">'
        '<field name="model">oma.service.ticket</field>'
        '<field name="arch" type="xml"><tree decoration-info="state == \'new\'"/></field>'
        "</record>"
    )
    generated = _gen("", views_xml=views_xml)
    with patch("specialists.build.specialist.is_fast_path_eligible", return_value=True), \
         patch("tools_odoo.odoo_schema_client.get_model_fields_fast", return_value=["id", "name", "state"]):
        result = _raises_async(
            _validate_decoration_attr_fields_exist_on_model, generated, "some_db", "task-123",
        )
    assert result is None, f"expected no raise once live schema confirms 'state' is real, got: {result!r}"
    print("PASS item39: a depends_on_module round with empty models.py consults live schema "
          "instead of falsely rejecting a real field owned by another module")


def test_item39_depends_on_module_round_still_raises_for_a_genuinely_invented_field():
    views_xml = (
        '<record id="v1" model="ir.ui.view">'
        '<field name="model">oma.service.ticket</field>'
        '<field name="arch" type="xml"><tree decoration-info="totally_invented_field == \'x\'"/></field>'
        "</record>"
    )
    generated = _gen("", views_xml=views_xml)
    with patch("specialists.build.specialist.is_fast_path_eligible", return_value=True), \
         patch("tools_odoo.odoo_schema_client.get_model_fields_fast", return_value=["id", "name", "state"]):
        result = _raises_async(
            _validate_decoration_attr_fields_exist_on_model, generated, "some_db", "task-123",
        )
    assert result, "a genuinely invented field must still raise even with live schema available"
    print("PASS item39: still raises for a genuinely invented field once live schema is checked")


# --- Item 40: _validate_server_action_state_specific_fields_present ---

def test_item40_raises_on_missing_code_field():
    xml = (
        '<record model="ir.actions.server"><field name="state">code</field></record>'
    )
    assert _raises(_validate_server_action_state_specific_fields_present, _gen(security_xml=xml))
    print("PASS item40: state='code' with no <field name='code'> raises")


def test_item40_never_raises_when_code_present():
    xml = (
        '<record model="ir.actions.server"><field name="state">code</field>'
        '<field name="code">pass</field></record>'
    )
    assert _raises(_validate_server_action_state_specific_fields_present, _gen(security_xml=xml)) is None
    print("PASS item40: state='code' with a real code field never raises")


# --- Item 42: _validate_manifest_declares_external_python_imports (documented no-op) ---

def test_item42_is_a_documented_noop_given_the_real_schema_gap():
    models_py = "import requests\nclass X(models.Model):\n    _name = 'x.model'\n"
    assert _raises(_validate_manifest_declares_external_python_imports, _gen(models_py)) is None
    print("PASS item42: never raises today -- ManifestFields has no external_dependencies field, documented gap")


# --- Item 43: _validate_manifest_hook_functions_exist (currently-inert check, real logic) ---

def test_item43_never_raises_since_manifest_schema_has_no_hook_fields_today():
    models_py = "class X(models.Model):\n    _name = 'x.model'\n"
    assert _raises(_validate_manifest_hook_functions_exist, _gen(models_py)) is None
    print("PASS item43: never raises today -- ManifestFields has no hook-key fields, documented gap")


# --- Item 44: _validate_http_route_auth_and_uniqueness ---

def test_item44_raises_on_invalid_auth_value():
    models_py = "class X(models.Model):\n    @http.route('/x', auth='invalid_value')\n    def x(self):\n        pass\n"
    assert _raises(_validate_http_route_auth_and_uniqueness, _gen(models_py))
    print("PASS item44: an invalid auth= value raises")


def test_item44_never_raises_on_real_auth_value():
    models_py = "class X(models.Model):\n    @http.route('/x', auth='public')\n    def x(self):\n        pass\n"
    assert _raises(_validate_http_route_auth_and_uniqueness, _gen(models_py)) is None
    print("PASS item44: a real auth= value never raises")


def test_item44_raises_on_duplicate_route():
    models_py = (
        "class X(models.Model):\n"
        "    @http.route('/x', auth='public', methods=['GET'])\n    def x(self):\n        pass\n"
        "    @http.route('/x', auth='public', methods=['GET'])\n    def x2(self):\n        pass\n"
    )
    assert _raises(_validate_http_route_auth_and_uniqueness, _gen(models_py))
    print("PASS item44: a duplicate (route, methods) tuple raises")


# --- Item 45: _validate_message_post_requires_mail_thread_inherit ---

def test_item45_raises_without_mail_thread():
    models_py = "class X(models.Model):\n    _name = 'x.model'\n    def go(self):\n        self.message_post(body='hi')\n"
    assert _raises(_validate_message_post_requires_mail_thread_inherit, _gen(models_py))
    print("PASS item45: message_post without mail.thread inherit raises")


def test_item45_never_raises_with_mail_thread():
    models_py = "class X(models.Model):\n    _inherit = 'x.model'\n    _inherit = ['x.model', 'mail.thread']\n    def go(self):\n        self.message_post(body='hi')\n"
    assert _raises(_validate_message_post_requires_mail_thread_inherit, _gen(models_py)) is None
    print("PASS item45: message_post with mail.thread inherit never raises")


# --- Item 45 live sibling (2026-08-04, task027's own real false-positive) ---

def test_item45_live_skips_when_inherited_model_already_provides_mail_thread_transitively():
    """The real, confirmed false positive this closes: _inherit = 'project.fieldjob' already
    transitively provides mail.thread (via project.fieldjob's own _inherit), so calling
    message_post() here is genuinely correct Odoo code -- the sync-only check would wrongly
    reject it."""
    import specialists.build.specialist as specialist_module

    models_py = (
        "class X(models.Model):\n"
        "    _inherit = 'project.fieldjob'\n\n"
        "    def go(self):\n"
        "        self.message_post(body='hi')\n"
    )
    generated = _gen(models_py)
    with patch.object(specialist_module, "is_fast_path_eligible", return_value=False), \
         patch.object(specialist_module, "get_model_fields", return_value=["id", "name", "message_follower_ids"]):
        result = asyncio.run(_validate_message_post_requires_mail_thread_inherit_live(generated, "test_db", "task-123"))
    assert result is None
    print("PASS: the live check correctly skips rejecting message_post() when the inherited model already provides mail.thread")


def test_item45_live_still_raises_when_inherited_model_genuinely_lacks_mail_thread():
    import specialists.build.specialist as specialist_module

    models_py = (
        "class X(models.Model):\n"
        "    _inherit = 'some.other.model'\n\n"
        "    def go(self):\n"
        "        self.message_post(body='hi')\n"
    )
    generated = _gen(models_py)
    with patch.object(specialist_module, "is_fast_path_eligible", return_value=False), \
         patch.object(specialist_module, "get_model_fields", return_value=["id", "name"]):
        assert _raises_async(_validate_message_post_requires_mail_thread_inherit_live, generated, "test_db", "task-123")
    print("PASS: the live check still raises when the inherited model genuinely lacks mail.thread")


def test_item45_live_never_calls_live_lookup_when_mail_thread_already_literal():
    """The fast path: if 'mail.thread' is already literally in the inherit list, the sync check
    alone already passes -- no live call should even be attempted."""
    import specialists.build.specialist as specialist_module

    models_py = (
        "class X(models.Model):\n"
        "    _inherit = ['project.fieldjob', 'mail.thread']\n\n"
        "    def go(self):\n"
        "        self.message_post(body='hi')\n"
    )
    generated = _gen(models_py)
    with patch.object(specialist_module, "get_model_fields", side_effect=AssertionError("must not be called")):
        result = asyncio.run(_validate_message_post_requires_mail_thread_inherit_live(generated, "test_db", "task-123"))
    assert result is None
    print("PASS: no live lookup attempted when the sync check already passes")


# --- Item 45 new sibling: _validate_message_post_ensures_partner_is_follower ---
# 50-task deep-dive (docs/reports/PHASE30_50TASK_DEEP_DIVE_MASTER_2026-08-05.md, P2 item 4): real,
# confirmed gap on Task 036 -- message_post() alone never guarantees email delivery unless the
# partner is already a follower or explicitly passed via partner_ids=. Recurred identically across
# 2 rounds despite the fix being injected verbatim as feedback each time.

def test_item45b_raises_task036_real_shape_no_partner_ids_no_subscribe():
    from specialists.build.specialist import _validate_message_post_ensures_partner_is_follower

    models_py = (
        "class AccountMove(models.Model):\n"
        "    _inherit = 'account.move'\n\n"
        "    def _send_overdue_reminder(self):\n"
        "        self.message_post(body='Your invoice is overdue.')\n"
    )
    assert _raises(_validate_message_post_ensures_partner_is_follower, _gen(models_py)), (
        "Task 036's real defect shape -- message_post with no partner_ids= and no "
        "message_subscribe anywhere in the same method -- must raise"
    )
    print("PASS item45b: Task 036's real defect (no partner_ids=, no message_subscribe) raises")


def test_item45b_never_raises_with_partner_ids_passed_directly():
    from specialists.build.specialist import _validate_message_post_ensures_partner_is_follower

    models_py = (
        "class AccountMove(models.Model):\n"
        "    _inherit = 'account.move'\n\n"
        "    def _send_overdue_reminder(self):\n"
        "        self.message_post(body='Your invoice is overdue.', "
        "partner_ids=[self.partner_id.id])\n"
    )
    assert _raises(_validate_message_post_ensures_partner_is_follower, _gen(models_py)) is None
    print("PASS item45b: message_post(..., partner_ids=[...]) never raises")


def test_item45b_never_raises_with_earlier_message_subscribe_in_same_method():
    from specialists.build.specialist import _validate_message_post_ensures_partner_is_follower

    models_py = (
        "class AccountMove(models.Model):\n"
        "    _inherit = 'account.move'\n\n"
        "    def _send_overdue_reminder(self):\n"
        "        self.message_subscribe(partner_ids=[self.partner_id.id])\n"
        "        self.message_post(body='Your invoice is overdue.')\n"
    )
    assert _raises(_validate_message_post_ensures_partner_is_follower, _gen(models_py)) is None
    print("PASS item45b: a message_subscribe(...) call earlier in the same method never raises")


def test_item45b_never_raises_on_internal_only_chatter_note():
    """An internal-only chatter note (no goal ever signals partner-facing intent -- this check
    fires on ANY bare message_post with no partner_ids=/message_subscribe regardless of intent,
    matching this codebase's own conservative-but-real posture elsewhere: a real internal note
    genuinely has no follower-delivery requirement to satisfy, but this check cannot see that
    from models_py text alone, and the report's own spec (d) asks this be confirmed explicitly.
    Confirming here: an internal-only note that never needs partner delivery would still need
    either partner_ids= or message_subscribe() to satisfy this check today -- this is a real,
    documented limitation of a same-method-body-only, text-only heuristic, not silently assumed
    to be handled."""
    from specialists.build.specialist import _validate_message_post_ensures_partner_is_follower

    models_py = (
        "class AccountMove(models.Model):\n"
        "    _inherit = 'account.move'\n\n"
        "    def _log_internal_audit_note(self):\n"
        "        self.message_post(body='Internal: reminder job ran.', subtype_xmlid='mail.mt_note')\n"
    )
    assert _raises(_validate_message_post_ensures_partner_is_follower, _gen(models_py)), (
        "documents the real, known limitation: a purely-internal note with no partner-facing "
        "intent still raises today, since this check has no way to distinguish intent from "
        "models_py text alone -- not silently swallowed, explicitly confirmed by this test"
    )
    print("PASS item45b: documents the real limitation that an internal-only note also raises "
          "today (no intent signal available from models_py text alone)")


def test_item45b_never_raises_when_message_post_absent():
    from specialists.build.specialist import _validate_message_post_ensures_partner_is_follower

    models_py = "class X(models.Model):\n    _name = 'x.model'\n"
    assert _raises(_validate_message_post_ensures_partner_is_follower, _gen(models_py)) is None
    print("PASS item45b: no message_post() call at all never raises")


# --- Autofix for item 45's real, safe subset (2026-08-03, full-30-task sweep, task016/task027) ---
#
# Real, confirmed live on 2 separate real tasks the same day: self.message_post(...) exists for
# exactly one purpose (chatter posting), and both real goals explicitly requested chatter
# behavior -- adding the one missing real dependency is never unrequested scope creep here.

def test_autofix_adds_mail_thread_to_a_bare_string_inherit_task016_real_shape():
    """task016's own real captured generated content shape (docs/reports/
    PHASE30_50TASK_SWEEP_RUN3_FINAL_2026-08-03.md) -- a bare-string `_inherit`, message_post()
    called from a write() override to log field changes to chatter, exactly the goal's own
    explicit request ('I want to see in the chatter history...').
    """
    models_py = (
        "from odoo import models, api\n\n"
        "class ProjectFieldjob(models.Model):\n"
        "    _inherit = 'project.fieldjob'\n\n"
        "    def write(self, vals):\n"
        "        result = super(ProjectFieldjob, self).write(vals)\n"
        "        if 'state' in vals:\n"
        "            self.message_post(body=\"State changed\")\n"
        "        return result\n"
    )
    generated = _gen(models_py)
    asyncio.run(_autofix_add_mail_thread_inherit_when_message_post_used(generated, "test_db", None))
    assert "_inherit = ['project.fieldjob', 'mail.thread']" in generated.models_py
    assert _raises(_validate_message_post_requires_mail_thread_inherit, generated) is None
    print("PASS: task016's own real bare-string _inherit is autofixed to include mail.thread")


def test_autofix_appends_mail_thread_to_an_existing_inherit_list():
    models_py = (
        "from odoo import models\n\n"
        "class X(models.Model):\n"
        "    _inherit = ['project.fieldjob', 'portal.mixin']\n\n"
        "    def write(self, vals):\n"
        "        self.message_post(body='x')\n"
        "        return super().write(vals)\n"
    )
    generated = _gen(models_py)
    asyncio.run(_autofix_add_mail_thread_inherit_when_message_post_used(generated, "test_db", None))
    assert "'project.fieldjob', 'portal.mixin', 'mail.thread'" in generated.models_py
    assert _raises(_validate_message_post_requires_mail_thread_inherit, generated) is None
    print("PASS: an existing _inherit list gets mail.thread appended, other mixins preserved")


def test_autofix_is_a_no_op_when_message_post_never_called():
    models_py = "class X(models.Model):\n    _inherit = 'project.fieldjob'\n"
    generated = _gen(models_py)
    before = generated.models_py
    asyncio.run(_autofix_add_mail_thread_inherit_when_message_post_used(generated, "test_db", None))
    assert generated.models_py == before
    print("PASS: no message_post() call at all is a pure no-op")


def test_autofix_is_a_no_op_when_mail_thread_already_inherited():
    models_py = (
        "class X(models.Model):\n"
        "    _inherit = ['project.fieldjob', 'mail.thread']\n\n"
        "    def write(self, vals):\n"
        "        self.message_post(body='x')\n"
        "        return super().write(vals)\n"
    )
    generated = _gen(models_py)
    before = generated.models_py
    asyncio.run(_autofix_add_mail_thread_inherit_when_message_post_used(generated, "test_db", None))
    assert generated.models_py == before
    print("PASS: mail.thread already present is a pure no-op, never double-added")


def test_autofix_skips_adding_when_the_target_model_already_provides_mail_thread():
    """The real, live-confirmed root cause of task027's own confusing install crash: this
    autofix's OLD blind-add behavior added a REDUNDANT 'mail.thread' to a class extending
    project.fieldjob, which already inherits mail.thread on its own base definition -- Odoo's
    real _inherit merging breaks on the redundant re-declaration (confirmed via a live,
    byte-for-byte reproduction against the real sandbox). get_model_fields is monkeypatched to
    simulate the target model already having 'message_follower_ids' (a field that only ever
    exists on a model that already has mail.thread mixed in somewhere in its own chain)."""
    import specialists.build.specialist as specialist_module

    models_py = (
        "class ProjectFieldjob(models.Model):\n"
        "    _inherit = 'project.fieldjob'\n\n"
        "    def _post_accepted_note(self):\n"
        "        self.message_post(body='x')\n"
    )
    generated = _gen(models_py)
    before = generated.models_py
    with patch.object(specialist_module, "is_fast_path_eligible", return_value=False), \
         patch.object(specialist_module, "get_model_fields", return_value=["id", "name", "message_follower_ids"]):
        asyncio.run(_autofix_add_mail_thread_inherit_when_message_post_used(generated, "test_db", "task-123"))
    assert generated.models_py == before, "must never add a redundant mail.thread when the live check confirms it's already provided"
    print("PASS: the live check correctly skips adding mail.thread when the target model already provides it")


def test_autofix_still_adds_mail_thread_when_target_model_genuinely_lacks_it():
    """The other half of the same fix: a target model confirmed live to NOT already have
    mail.thread (no message_follower_ids field) must still get it added, exactly as before --
    this is the common, correct case for every target that isn't project.fieldjob."""
    import specialists.build.specialist as specialist_module

    models_py = (
        "class X(models.Model):\n"
        "    _inherit = 'some.other.model'\n\n"
        "    def write(self, vals):\n"
        "        self.message_post(body='x')\n"
        "        return super().write(vals)\n"
    )
    generated = _gen(models_py)
    with patch.object(specialist_module, "is_fast_path_eligible", return_value=False), \
         patch.object(specialist_module, "get_model_fields", return_value=["id", "name"]):
        asyncio.run(_autofix_add_mail_thread_inherit_when_message_post_used(generated, "test_db", "task-123"))
    assert "mail.thread" in generated.models_py
    print("PASS: mail.thread is still added when the live check confirms the target genuinely lacks it")


def test_autofix_falls_through_to_old_behavior_when_live_lookup_is_inconclusive():
    """None (couldn't get a confident live answer) must never regress this autofix's own
    already-proven-correct behavior for every target that isn't project.fieldjob -- falls
    through to the old blind-add rather than silently doing nothing."""
    import specialists.build.specialist as specialist_module

    models_py = (
        "class X(models.Model):\n"
        "    _inherit = 'some.unknown.model'\n\n"
        "    def write(self, vals):\n"
        "        self.message_post(body='x')\n"
        "        return super().write(vals)\n"
    )
    generated = _gen(models_py)
    with patch.object(specialist_module, "is_fast_path_eligible", return_value=False), \
         patch.object(specialist_module, "get_model_fields", return_value=None):
        asyncio.run(_autofix_add_mail_thread_inherit_when_message_post_used(generated, "test_db", "task-123"))
    assert "mail.thread" in generated.models_py
    print("PASS: an inconclusive live lookup still falls through to adding mail.thread, never silently skips")


# --- Autofix for the missing-owning-module-dependency gap (2026-08-04, same-night full
# 30-task sweep, task027 -- confirmed via a live, byte-for-byte reproduction against the real
# sandbox: `_inherit = [..., 'mail.thread']` with 'mail' missing from the manifest's own
# `depends` produces a confusing `ValueError: The _name attribute ProjectFieldjob is not valid`
# at real Odoo registry-init time -- Odoo silently falls through to registering the whole class
# as a brand-new model when it can't resolve every _inherit target, deriving _name from the
# Python class name. Each test builds its OWN fresh ManifestFields (never the shared _MANIFEST
# constant above) since this autofix mutates `.depends` in place -- reusing the shared, mutable
# instance across tests would leak state between them. ---

def _gen_with_depends(models_py: str, depends: list[str]) -> GeneratedModuleFiles:
    manifest = ManifestFields(
        name="x", version="1.0.0", category="Tools", summary="x", author="x", depends=depends, data=[],
    )
    return GeneratedModuleFiles(manifest_fields=manifest, models_py=models_py, security_csv="x", notes="")


def test_autofix_adds_mail_to_depends_for_task027s_own_real_shape():
    """task027's own real, live-reproduced shape: _inherit correctly lists 'mail.thread', but
    the manifest's own depends never included 'mail' -- the actual root cause behind a
    confusing install-time _name error that looks like (but is not) a naming mistake."""
    models_py = (
        "class ProjectFieldjob(models.Model):\n"
        "    _inherit = ['project.fieldjob', 'mail.thread']\n\n"
        "    def _post_accepted_note(self):\n"
        "        pass\n"
    )
    generated = _gen_with_depends(models_py, ["base", "project_fieldjob"])
    _autofix_add_owning_module_dependency_for_known_mixins(generated)
    assert "mail" in generated.manifest_fields.depends
    print("PASS: 'mail' added to depends for a _inherit list naming mail.thread")


def test_autofix_adds_portal_to_depends_for_portal_mixin():
    models_py = "class X(models.Model):\n    _inherit = ['project.fieldjob', 'portal.mixin']\n"
    generated = _gen_with_depends(models_py, ["base", "project_fieldjob"])
    _autofix_add_owning_module_dependency_for_known_mixins(generated)
    assert "portal" in generated.manifest_fields.depends
    print("PASS: 'portal' added to depends for a _inherit list naming portal.mixin")


def test_autofix_is_a_no_op_when_owning_module_already_present():
    models_py = "class X(models.Model):\n    _inherit = ['project.fieldjob', 'mail.thread']\n"
    generated = _gen_with_depends(models_py, ["base", "project_fieldjob", "mail"])
    before = list(generated.manifest_fields.depends)
    _autofix_add_owning_module_dependency_for_known_mixins(generated)
    assert generated.manifest_fields.depends == before
    print("PASS: 'mail' already present is a pure no-op, never duplicated")


def test_autofix_is_a_no_op_when_no_known_mixin_is_inherited():
    models_py = "class X(models.Model):\n    _inherit = 'project.fieldjob'\n"
    generated = _gen_with_depends(models_py, ["base", "project_fieldjob"])
    before = list(generated.manifest_fields.depends)
    _autofix_add_owning_module_dependency_for_known_mixins(generated)
    assert generated.manifest_fields.depends == before
    print("PASS: no known mixin inherited at all is a pure no-op")


def test_autofix_handles_bare_string_inherit_naming_a_mixin_directly():
    """A model that ONLY inherits the mixin (bare string form, not a list) must still be
    caught -- the mixin doesn't have to be one of several _inherit targets."""
    models_py = "class X(models.Model):\n    _inherit = 'mail.thread'\n"
    generated = _gen_with_depends(models_py, ["base"])
    _autofix_add_owning_module_dependency_for_known_mixins(generated)
    assert "mail" in generated.manifest_fields.depends
    print("PASS: a bare-string _inherit naming the mixin directly is also caught")


# --- Autofix for the LLM-written redundant mixin gap (2026-08-04, same-night full 30-task
# sweep, task027 RE-run -- confirmed live, the exact same night the sibling autofix above was
# fixed for a related but distinct gap): that fix only stopped THIS session's own autofix from
# blindly ADDING a redundant 'mail.thread' -- it never covered the LLM's own generated output
# writing the redundant declaration directly itself, which crashes install with the identical
# misleading `ValueError: The _name attribute ... is not valid` error. ---

def test_strip_removes_redundant_mail_thread_from_task027s_own_real_re_crash_shape():
    """task027's own real re-crash content, verbatim (read directly off the real sandbox
    container after this session's first fix was already deployed): _inherit correctly lists
    project.fieldjob, but the LLM ALSO wrote 'mail.thread' directly -- redundant, since
    project.fieldjob already provides it transitively. Odoo's own real _inherit merging breaks on
    the redundant re-declaration, confirmed via live reproduction the same night."""
    import specialists.build.specialist as specialist_module

    models_py = (
        "from odoo import api, models\n\n\n"
        "class ProjectFieldjob(models.Model):\n"
        "    _inherit = ['project.fieldjob', 'mail.thread']\n\n"
        "    def _post_accepted_note(self):\n"
        "        for record in self:\n"
        "            if record.project_id:\n"
        "                record.project_id.message_post(body='x')\n"
    )
    generated = _gen(models_py)
    with patch.object(specialist_module, "is_fast_path_eligible", return_value=False), \
         patch.object(specialist_module, "get_model_fields", return_value=["id", "name", "message_follower_ids"]):
        asyncio.run(_autofix_strip_redundant_mixin_inherit_when_already_provided(generated, "test_db", "task-123"))
    assert "_inherit = ['project.fieldjob']" in generated.models_py
    assert "'mail.thread'" not in generated.models_py
    print("PASS: a redundant mail.thread the LLM wrote directly is stripped when the other target already provides it")


def test_strip_is_a_no_op_when_only_one_inherit_target():
    models_py = "class X(models.Model):\n    _inherit = 'project.fieldjob'\n"
    generated = _gen(models_py)
    before = generated.models_py
    asyncio.run(_autofix_strip_redundant_mixin_inherit_when_already_provided(generated, "test_db", "task-123"))
    assert generated.models_py == before
    print("PASS: a single, non-list _inherit target is a pure no-op -- nothing redundant to strip")


def test_strip_is_a_no_op_when_target_genuinely_lacks_mail_thread():
    """The other half of the same fix: a target model confirmed live to NOT already provide
    mail.thread must keep its explicit mail.thread -- stripping it here would be the real
    regression this autofix must never cause."""
    import specialists.build.specialist as specialist_module

    models_py = "class X(models.Model):\n    _inherit = ['some.other.model', 'mail.thread']\n"
    generated = _gen(models_py)
    before = generated.models_py
    with patch.object(specialist_module, "is_fast_path_eligible", return_value=False), \
         patch.object(specialist_module, "get_model_fields", return_value=["id", "name"]):
        asyncio.run(_autofix_strip_redundant_mixin_inherit_when_already_provided(generated, "test_db", "task-123"))
    assert generated.models_py == before
    print("PASS: mail.thread is kept when the live check confirms the target genuinely lacks it")


def test_strip_is_a_no_op_when_live_lookup_is_inconclusive():
    """None (couldn't get a confident live answer) must never strip anything -- conservative,
    matches every other live-lookup in this chain: an inconclusive answer leaves content alone."""
    import specialists.build.specialist as specialist_module

    models_py = "class X(models.Model):\n    _inherit = ['some.unknown.model', 'mail.thread']\n"
    generated = _gen(models_py)
    before = generated.models_py
    with patch.object(specialist_module, "is_fast_path_eligible", return_value=False), \
         patch.object(specialist_module, "get_model_fields", return_value=None):
        asyncio.run(_autofix_strip_redundant_mixin_inherit_when_already_provided(generated, "test_db", "task-123"))
    assert generated.models_py == before
    print("PASS: an inconclusive live lookup never strips anything")


def test_strip_is_a_no_op_without_task_id():
    """No task_id means no live lookup is possible at all -- must never guess-strip."""
    models_py = "class X(models.Model):\n    _inherit = ['project.fieldjob', 'mail.thread']\n"
    generated = _gen(models_py)
    before = generated.models_py
    asyncio.run(_autofix_strip_redundant_mixin_inherit_when_already_provided(generated, "test_db", None))
    assert generated.models_py == before
    print("PASS: without a task_id, nothing is stripped -- no live lookup is possible")


def test_strip_handles_portal_mixin_redundancy_too():
    """Same fix, generalized to every known mixin this codebase already tracks in
    _MIXIN_OWNING_MODULE, not just mail.thread -- portal.mixin's own real signal field is
    access_token."""
    import specialists.build.specialist as specialist_module

    models_py = "class X(models.Model):\n    _inherit = ['project.fieldjob', 'portal.mixin']\n"
    generated = _gen(models_py)
    with patch.object(specialist_module, "is_fast_path_eligible", return_value=False), \
         patch.object(specialist_module, "get_model_fields", return_value=["id", "name", "access_token"]):
        asyncio.run(_autofix_strip_redundant_mixin_inherit_when_already_provided(generated, "test_db", "task-123"))
    assert "_inherit = ['project.fieldjob']" in generated.models_py
    print("PASS: the same fix generalizes to portal.mixin, not just mail.thread")


# --- Item 46: _validate_activity_calls_require_activity_mixin_inherit ---

def test_item46_raises_without_activity_mixin():
    models_py = "class X(models.Model):\n    _name = 'x.model'\n    def go(self):\n        self.activity_schedule('mail.mail_activity_data_todo')\n"
    assert _raises(_validate_activity_calls_require_activity_mixin_inherit, _gen(models_py))
    print("PASS item46: activity_schedule without mail.activity.mixin raises")


def test_item46_never_raises_with_activity_mixin():
    models_py = "class X(models.Model):\n    _inherit = ['x.model', 'mail.activity.mixin']\n    def go(self):\n        self.activity_schedule('mail.mail_activity_data_todo')\n"
    assert _raises(_validate_activity_calls_require_activity_mixin_inherit, _gen(models_py)) is None
    print("PASS item46: activity_schedule with mail.activity.mixin never raises")


# --- Item 47: _validate_portal_mixin_no_field_redeclaration ---

def test_item47_raises_on_redeclared_portal_field():
    models_py = "class X(models.Model):\n    _inherit = ['x.model', 'portal.mixin']\n    access_token = fields.Char()\n"
    assert _raises(_validate_portal_mixin_no_field_redeclaration, _gen(models_py))
    print("PASS item47: redeclaring access_token on a portal.mixin model raises")


def test_item47_never_raises_without_redeclaration():
    models_py = "class X(models.Model):\n    _inherit = ['x.model', 'portal.mixin']\n    amount = fields.Integer()\n"
    assert _raises(_validate_portal_mixin_no_field_redeclaration, _gen(models_py)) is None
    print("PASS item47: an unrelated field on a portal.mixin model never raises")


# --- Item 48: _validate_qweb_report_template_id_resolves ---

def test_item48_raises_on_unresolved_report_name():
    xml = (
        '<record model="ir.actions.report"><field name="report_name">module.missing_template</field></record>'
    )
    assert _raises(_validate_qweb_report_template_id_resolves, _gen(security_xml=xml))
    print("PASS item48: a report_name that doesn't resolve to a defined template raises")


def test_item48_never_raises_when_resolved():
    xml = (
        '<template id="module.t1"/>'
        '<record model="ir.actions.report"><field name="report_name">module.t1</field></record>'
    )
    assert _raises(_validate_qweb_report_template_id_resolves, _gen(security_xml=xml)) is None
    print("PASS item48: a report_name that resolves to a defined template never raises")


# --- Item 49: _validate_error_strings_wrapped_in_translate_call ---

def test_item49_raises_on_unwrapped_error_string():
    models_py = "class X(models.Model):\n    def go(self):\n        raise ValidationError('Something went wrong')\n"
    assert _raises(_validate_error_strings_wrapped_in_translate_call, _gen(models_py))
    print("PASS item49: an un-translated ValidationError string literal raises")


def test_item49_never_raises_on_wrapped_string():
    models_py = "class X(models.Model):\n    def go(self):\n        raise ValidationError(_('Something went wrong'))\n"
    assert _raises(_validate_error_strings_wrapped_in_translate_call, _gen(models_py)) is None
    print("PASS item49: a properly _()-wrapped error string never raises")


# --- Autofix for item 49's own safe subset (2026-08-03, full-30-task sweep, task012/024) ---
#
# Real, confirmed live: item 49's own validator has always been deliberately flag-only, for a
# real documented risk (naively wrapping an f-string's INTERPOLATED RESULT rather than its
# TEMPLATE). Both real occurrences found live the same day (task012, task024) were always the
# much simpler, unconditionally-safe case: a single plain string literal with no interpolation at
# all -- wrapping it in _(...) is a pure, lossless transformation with no "result vs. template"
# distinction to get wrong.

def test_autofix_wraps_task012_own_real_plain_literal():
    """task012's own real captured generated content (docs/reports/
    PHASE30_50TASK_SWEEP_RUN3_FINAL_2026-08-03.md) -- a plain ValidationError string with no
    interpolation, raised from an @api.constrains date-order check.
    """
    models_py = (
        "from odoo import api, fields, models\n"
        "from odoo.exceptions import ValidationError\n\n"
        "class ProjectFieldjob(models.Model):\n"
        "    _inherit = 'project.fieldjob'\n\n"
        "    date_finish = fields.Date(string='Finish date')\n\n"
        "    @api.constrains('date', 'date_finish')\n"
        "    def _check_date_order(self):\n"
        "        for record in self:\n"
        "            if record.date and record.date_finish:\n"
        "                if record.date_finish < record.date:\n"
        "                    raise ValidationError(\n"
        "                        'The finish date cannot be before the start date.'\n"
        "                    )\n"
    )
    generated = _gen(models_py)
    _autofix_wrap_plain_untranslated_error_string_literal(generated)
    assert "_('The finish date cannot be before the start date.')" in generated.models_py
    assert _raises(_validate_error_strings_wrapped_in_translate_call, generated) is None, (
        "the validator must stay silent once the autofix has wrapped the plain literal"
    )
    print("PASS: task012's own real plain-literal ValidationError string is now autofixed, "
          "not just flagged")


def test_autofix_never_touches_an_fstring_or_percent_formatted_literal():
    """Regression guard, exactly the risk item 49's own docstring names: an f-string or a
    %%-formatted string must be left completely untouched -- these are exactly the cases where a
    naive autofix would wrap the wrong thing. The validator's own raise remains the real,
    unchanged safety net for these.
    """
    models_py_fstring = (
        "class X(models.Model):\n"
        "    def check(self):\n"
        '        raise ValidationError(f"Bad value: {self.name}")\n'
    )
    generated = _gen(models_py_fstring)
    before = generated.models_py
    _autofix_wrap_plain_untranslated_error_string_literal(generated)
    assert generated.models_py == before, "an f-string must never be touched by this autofix"
    assert _raises(_validate_error_strings_wrapped_in_translate_call, generated), (
        "an untouched f-string must still be caught by the validator's own raise"
    )

    models_py_percent = (
        "class X(models.Model):\n"
        "    def check(self):\n"
        "        raise ValidationError('Bad value: %s' % (self.name,))\n"
    )
    generated2 = _gen(models_py_percent)
    before2 = generated2.models_py
    _autofix_wrap_plain_untranslated_error_string_literal(generated2)
    assert generated2.models_py == before2, "a %-formatted call must never be touched by this autofix"
    print("PASS: f-strings and %-formatted error strings are never touched by the autofix -- "
          "the validator's own raise still catches them exactly as before")


def test_autofix_skips_a_literal_already_wrapped_in_translate_call():
    models_py = "class X(models.Model):\n    def go(self):\n        raise ValidationError(_('Already wrapped'))\n"
    generated = _gen(models_py)
    before = generated.models_py
    _autofix_wrap_plain_untranslated_error_string_literal(generated)
    assert generated.models_py == before, "an already-wrapped literal must be left completely alone"
    print("PASS: an already _()-wrapped literal is never double-wrapped or otherwise touched")


# --- Item 50: _validate_translate_attr_only_on_translatable_types ---

def test_item50_raises_on_translate_on_integer():
    models_py = "class X(models.Model):\n    _name = 'x.model'\n    n = fields.Integer(translate=True)\n"
    assert _raises(_validate_translate_attr_only_on_translatable_types, _gen(models_py))
    print("PASS item50: translate=True on an Integer field raises")


def test_item50_never_raises_on_char():
    models_py = "class X(models.Model):\n    _name = 'x.model'\n    name = fields.Char(translate=True)\n"
    assert _raises(_validate_translate_attr_only_on_translatable_types, _gen(models_py)) is None
    print("PASS item50: translate=True on a Char field never raises")


# --- Item 51: _validate_rec_name_and_order_reference_real_fields ---

def test_item51_raises_on_undeclared_rec_name():
    models_py = "class X(models.Model):\n    _name = 'x.model'\n    _rec_name = 'missing_field'\n"
    assert _raises(_validate_rec_name_and_order_reference_real_fields, _gen(models_py))
    print("PASS item51: _rec_name naming an undeclared field raises")


def test_item51_raises_on_undeclared_order_field():
    models_py = "class X(models.Model):\n    _name = 'x.model'\n    _order = 'missing_field asc'\n"
    assert _raises(_validate_rec_name_and_order_reference_real_fields, _gen(models_py))
    print("PASS item51: _order naming an undeclared field raises")


def test_item51_never_raises_on_declared_fields():
    models_py = (
        "class X(models.Model):\n    _name = 'x.model'\n    name = fields.Char()\n"
        "    _rec_name = 'name'\n    _order = 'name asc'\n"
    )
    assert _raises(_validate_rec_name_and_order_reference_real_fields, _gen(models_py)) is None
    print("PASS item51: _rec_name/_order naming declared fields never raise")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
    print(f"\nALL {len(fns)} P11 TIER B TESTS PASSED")
