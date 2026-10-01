"""P11 depth-audit Tier A (docs/planning/PHASE30_DEPTH_AUDIT_FINAL_CONSOLIDATED_2026-07-30.md
§2.1, items 76-89): unit tests. Items 83/84 are genuine duplicates of Tier B item 36, not built,
no tests needed for them. Zero LLM/GPU calls -- items 76/77 need one live-registry-mocked test
each (they feed into the existing async `_validate_xml_refs_resolve`), everything else is pure.
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import specialists.build.specialist as build_specialist
from specialists.build.specialist import (
    GeneratedModuleFiles,
    ManifestFields,
    _autofix_add_mail_thread_inherit_when_tracking_kwarg_used,
    _autofix_missing_translation_underscore_import,
    _find_all_xml_refs,
    _validate_action_view_mode_tokens_are_valid_enum,
    _validate_field_tracking_kwarg_requires_mail_thread_inherit,
    _validate_ir_cron_interval_type_is_valid_enum,
    _validate_mail_thread_no_field_redeclaration,
    _validate_no_duplicate_new_model_names_in_same_generation,
    _validate_test_file_field_refs_exist_on_model,
    _validate_view_attrs_domain_fields_exist_on_model,
    _validate_xml_refs_resolve,
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


# --- Item 76: groups= attribute extraction ---

def test_item76_find_all_xml_refs_extracts_groups_attr():
    content = '<field name="x" groups="base.group_system,!base.group_public"/>'
    refs = _find_all_xml_refs(content)
    assert "base.group_system" in refs
    assert "base.group_public" in refs  # negation stripped
    print("PASS item76: groups= xmlids are extracted, negation prefix stripped")


def test_item76_and_77_validate_xml_refs_resolve_catches_bad_group_in_extra_data_files(monkeypatch):
    # Also proves item 77: the bad ref lives in extra_data_files, not views_xml/security_xml.
    def fake_resolve_xmlids_exist(refs, db):
        return {r: False for r in refs}

    monkeypatch.setattr(build_specialist, "is_fast_path_eligible", lambda db: False)
    monkeypatch.setattr(build_specialist, "resolve_xmlids_exist", fake_resolve_xmlids_exist)

    generated = _gen(
        extra_data_files={"data/cron_data.xml": '<record model="ir.cron"><field name="code_field" groups="base.group_nonexistent_xyz"/></record>'},
    )

    async def run():
        await _validate_xml_refs_resolve(generated, "odoo16_dev", "oma_test_module", task_id="t1")

    try:
        asyncio.run(run())
        assert False, "expected ValueError"
    except ValueError as exc:
        assert "base.group_nonexistent_xyz" in str(exc)
    print("PASS items 76+77: a bad groups= xmlid living in extra_data_files is caught -- proves "
          "both the groups= extraction AND the extra_data_files scan widening")


# --- Item 78: ir.cron interval_type enum ---

def test_item78_raises_on_invalid_interval_type():
    xml = '<record model="ir.cron"><field name="interval_type">seconds</field></record>'
    assert _raises(_validate_ir_cron_interval_type_is_valid_enum, _gen(security_xml=xml))
    print("PASS item78: a hallucinated interval_type raises")


def test_item78_never_raises_on_valid_interval_type():
    xml = '<record model="ir.cron"><field name="interval_type">days</field></record>'
    assert _raises(_validate_ir_cron_interval_type_is_valid_enum, _gen(security_xml=xml)) is None
    print("PASS item78: a real interval_type never raises")


# --- Item 79: action view_mode tokens ---

def test_item79_raises_on_invalid_view_mode_token():
    xml = '<record model="ir.actions.act_window"><field name="view_mode">tree,invalid_mode</field></record>'
    assert _raises(_validate_action_view_mode_tokens_are_valid_enum, _gen(security_xml=xml))
    print("PASS item79: an invalid view_mode token raises")


def test_item79_raises_on_gantt_without_dependency():
    xml = '<record model="ir.actions.act_window"><field name="view_mode">gantt</field></record>'
    assert _raises(_validate_action_view_mode_tokens_are_valid_enum, _gen(security_xml=xml))
    print("PASS item79: gantt without web_gantt dependency raises")


def test_item79_never_raises_on_gantt_with_dependency():
    manifest = ManifestFields(name="x", version="1.0.0", category="Tools", summary="x", author="x", depends=["base", "web_gantt"], data=[])
    generated = GeneratedModuleFiles(
        manifest_fields=manifest, models_py="", security_csv="x",
        security_xml='<record model="ir.actions.act_window"><field name="view_mode">gantt</field></record>',
        notes="",
    )
    assert _raises(_validate_action_view_mode_tokens_are_valid_enum, generated) is None
    print("PASS item79: gantt with web_gantt declared never raises")


def test_item79_never_raises_on_ordinary_tokens():
    xml = '<record model="ir.actions.act_window"><field name="view_mode">tree,form</field></record>'
    assert _raises(_validate_action_view_mode_tokens_are_valid_enum, _gen(security_xml=xml)) is None
    print("PASS item79: ordinary tokens never raise")


# --- Items 80/81/82: attrs/domain/context group_by/filter domain field existence ---

def test_item80_raises_on_undeclared_attrs_field():
    models_py = "class X(models.Model):\n    _name = 'x.model'\n"
    views_xml = '<field name="y" attrs="{\'invisible\': [(\'missing_field\', \'=\', True)]}"/>'
    assert _raises(_validate_view_attrs_domain_fields_exist_on_model, _gen(models_py, views_xml=views_xml))
    print("PASS item80: attrs= referencing an undeclared field raises")


def test_item81_raises_on_undeclared_groupby_field():
    models_py = "class X(models.Model):\n    _name = 'x.model'\n"
    views_xml = "<search><filter name=\"f\" context=\"{'group_by': 'missing_field'}\"/></search>"
    assert _raises(_validate_view_attrs_domain_fields_exist_on_model, _gen(models_py, views_xml=views_xml))
    print("PASS item81: context group_by referencing an undeclared field raises")


def test_item82_raises_on_undeclared_filter_domain_field():
    models_py = "class X(models.Model):\n    _name = 'x.model'\n"
    views_xml = "<filter name=\"f\" domain=\"[('missing_field', '=', True)]\"/>"
    assert _raises(_validate_view_attrs_domain_fields_exist_on_model, _gen(models_py, views_xml=views_xml))
    print("PASS item82: a <filter domain=> referencing an undeclared field raises")


def test_items_80_81_82_never_raise_on_declared_fields():
    models_py = "class X(models.Model):\n    _name = 'x.model'\n    amount = fields.Integer()\n"
    views_xml = (
        '<field name="y" attrs="{\'invisible\': [(\'amount\', \'=\', 0)]}"/>'
        "<filter name=\"f\" domain=\"[('amount', '>', 0)]\"/>"
    )
    assert _raises(_validate_view_attrs_domain_fields_exist_on_model, _gen(models_py, views_xml=views_xml)) is None
    print("PASS items 80/81/82: declared fields referenced in attrs/domain never raise")


def test_items_80_81_82_skip_on_inherit():
    models_py = "class X(models.Model):\n    _inherit = 'res.partner'\n"
    views_xml = '<field name="y" attrs="{\'invisible\': [(\'missing_field\', \'=\', True)]}"/>'
    assert _raises(_validate_view_attrs_domain_fields_exist_on_model, _gen(models_py, views_xml=views_xml)) is None
    print("PASS items 80/81/82: _inherit classes are skipped -- can't see base fields without a live query")


def test_items_80_81_82_skip_on_view_only_round_with_no_new_model_declared():
    """Real, confirmed false positive found live (2026-08-15, quick_filter_search_construction):
    a pure view-only round (e.g. a quick-filter search-view override) adds no Python at all --
    models_py is left as whatever scaffold boilerplate was already there, containing neither a
    real `_name =` (a new model) nor `_inherit =` (extending an existing one). The old guard only
    checked for the ABSENCE of "_inherit", so this case fell through and got treated as "the new
    model's complete field list is empty", making every real domain field reference on the
    round's actual target model (e.g. sale.order.user_id, product.template.qty_available -- both
    confirmed live to genuinely exist) look "missing". This check's own stated scope is "new-model
    case only" -- it must require the POSITIVE signal of a real new model being declared, not just
    the absence of _inherit.
    """
    models_py = "# scaffold boilerplate, no real model declared this round\n"
    views_xml = "<filter name=\"f\" domain=\"[('user_id', '=', False)]\"/>"
    assert _raises(_validate_view_attrs_domain_fields_exist_on_model, _gen(models_py, views_xml=views_xml)) is None
    print("PASS items 80/81/82: a view-only round declaring no new model in Python is skipped, not falsely rejected")


# --- Item 85: test file field refs ---

def test_item85_raises_on_undeclared_test_field():
    models_py = "class X(models.Model):\n    _name = 'x.model'\n"
    tests_py = {"test_x.py": "self.env['x.model'].create({'missing_field': 1})\n"}
    assert _raises(_validate_test_file_field_refs_exist_on_model, _gen(models_py, tests_py=tests_py))
    print("PASS item85: a .create({...}) key not declared on the model raises")


def test_item85_never_raises_on_declared_test_field():
    models_py = "class X(models.Model):\n    _name = 'x.model'\n    amount = fields.Integer()\n"
    tests_py = {"test_x.py": "self.env['x.model'].create({'amount': 1})\n"}
    assert _raises(_validate_test_file_field_refs_exist_on_model, _gen(models_py, tests_py=tests_py)) is None
    print("PASS item85: a declared field in .create({...}) never raises")


# --- Item 86: duplicate new model names ---

def test_item86_raises_on_duplicate_name_across_classes():
    models_py = (
        "class A(models.Model):\n    _name = 'x.model'\n"
        "class B(models.Model):\n    _name = 'x.model'\n"
    )
    assert _raises(_validate_no_duplicate_new_model_names_in_same_generation, _gen(models_py))
    print("PASS item86: the same _name declared twice in one generation raises")


def test_item86_never_raises_on_unique_names():
    models_py = "class A(models.Model):\n    _name = 'x.model'\n"
    assert _raises(_validate_no_duplicate_new_model_names_in_same_generation, _gen(models_py)) is None
    print("PASS item86: a single _name never raises")


# --- Item 87: mail.thread field redeclaration ---

def test_item87_raises_on_redeclared_mail_thread_field():
    models_py = "class X(models.Model):\n    _inherit = ['x.model', 'mail.thread']\n    message_ids = fields.One2many('mail.message', 'res_id')\n"
    assert _raises(_validate_mail_thread_no_field_redeclaration, _gen(models_py))
    print("PASS item87: redeclaring message_ids on a mail.thread model raises")


def test_item87_never_raises_without_redeclaration():
    models_py = "class X(models.Model):\n    _inherit = ['x.model', 'mail.thread']\n    amount = fields.Integer()\n"
    assert _raises(_validate_mail_thread_no_field_redeclaration, _gen(models_py)) is None
    print("PASS item87: an unrelated field on a mail.thread model never raises")


# --- Item 88: tracking= requires mail.thread ---

def test_item88_raises_on_tracking_without_mail_thread():
    models_py = "class X(models.Model):\n    _name = 'x.model'\n    state = fields.Selection([('a', 'A')], tracking=True)\n"
    assert _raises(_validate_field_tracking_kwarg_requires_mail_thread_inherit, _gen(models_py))
    print("PASS item88: tracking=True without mail.thread inherit raises")


def test_item88_never_raises_with_mail_thread():
    models_py = "class X(models.Model):\n    _inherit = ['x.model', 'mail.thread']\n    state = fields.Selection([('a', 'A')], tracking=True)\n"
    assert _raises(_validate_field_tracking_kwarg_requires_mail_thread_inherit, _gen(models_py)) is None
    print("PASS item88: tracking=True with mail.thread inherit never raises")


# --- Item 89: missing translation import autofix ---

def test_item89_adds_missing_translate_import():
    models_py = "from odoo import models, fields\n\nclass X(models.Model):\n    def go(self):\n        raise ValidationError(_('Bad value'))\n"
    generated = _gen(models_py)
    _autofix_missing_translation_underscore_import(generated)
    assert "from odoo.tools.translate import _" in generated.models_py
    print("PASS item89: a missing _() import is added when _(...) is used")


def test_item89_never_fires_on_throwaway_underscore_variable():
    models_py = "from odoo import models, fields\n\nclass X(models.Model):\n    def go(self):\n        for _ in range(3):\n            pass\n"
    generated = _gen(models_py)
    original = generated.models_py
    _autofix_missing_translation_underscore_import(generated)
    assert generated.models_py == original
    print("PASS item89: Python's own throwaway `_` variable idiom never triggers a false-positive import")


def test_item89_never_duplicates_existing_import():
    models_py = "from odoo import models, fields, _\n\nclass X(models.Model):\n    def go(self):\n        raise ValidationError(_('Bad'))\n"
    generated = _gen(models_py)
    original = generated.models_py
    _autofix_missing_translation_underscore_import(generated)
    assert generated.models_py == original
    print("PASS item89: an already-present _ import is never duplicated")


# --- Autofix for item 88 (real bug found live, 2026-08-08, task e89150c8's
# ticket_chatter_logging node: the validator alone escalated a purely mechanical, single-answer
# fix -- add mail.thread -- to a human decision) ---

def test_autofix_adds_mail_thread_inherit_for_a_brand_new_model_closing_the_live_gap():
    """Real bug found live (2026-08-08, task e89150c8-69de-4cad-8450-69eda822cfcb's flagship
    run, ticket_chatter_logging node): the goal explicitly required chatter logging ("every state
    change should be logged to the ticket's own chatter"), the LLM correctly used tracking=True
    to get it, but never added the one real, required _inherit = 'mail.thread' -- a brand-new
    `_name`-only model, the exact shape this autofix's sibling (triggered by message_post()
    instead) deliberately leaves untouched.
    """
    models_py = (
        "from odoo import models, fields\n\n"
        "class ServiceTicket(models.Model):\n"
        "    _name = 'oma.service.ticket'\n"
        "    _description = 'Service Ticket'\n\n"
        "    state = fields.Selection([('new', 'New')], tracking=True)\n"
    )
    generated = _gen(models_py)
    asyncio.run(_autofix_add_mail_thread_inherit_when_tracking_kwarg_used(generated, "odoo16_dev", None))
    assert "_inherit = ['mail.thread']" in generated.models_py, (
        f"expected a real mail.thread inherit to be added -- got {generated.models_py!r}"
    )
    assert _raises(_validate_field_tracking_kwarg_requires_mail_thread_inherit, generated) is None, (
        "the validator this autofix runs ahead of must now be satisfied"
    )
    print("PASS: mail.thread is added to a brand-new tracking=True model, closing the real live "
          "gap found on task e89150c8's ticket_chatter_logging node -- no more escalating a "
          "single-answer fix to a human decision")


def test_autofix_extends_an_existing_inherit_list_rather_than_a_new_name_model():
    models_py = (
        "class X(models.Model):\n"
        "    _inherit = 'project.task'\n"
        "    state = fields.Selection([('a', 'A')], tracking=True)\n"
    )
    generated = _gen(models_py)
    asyncio.run(_autofix_add_mail_thread_inherit_when_tracking_kwarg_used(generated, "odoo16_dev", None))
    assert "_inherit = ['project.task', 'mail.thread']" in generated.models_py
    print("PASS: an existing bare _inherit string target is correctly widened into a list "
          "including mail.thread")


def test_autofix_is_a_noop_when_mail_thread_already_inherited():
    models_py = "class X(models.Model):\n    _inherit = ['x.model', 'mail.thread']\n    state = fields.Selection([('a', 'A')], tracking=True)\n"
    generated = _gen(models_py)
    original = generated.models_py
    asyncio.run(_autofix_add_mail_thread_inherit_when_tracking_kwarg_used(generated, "odoo16_dev", None))
    assert generated.models_py == original
    print("PASS: a no-op when mail.thread is already genuinely inherited")


def test_autofix_is_a_noop_when_tracking_kwarg_is_absent():
    models_py = "class X(models.Model):\n    _name = 'x.model'\n    state = fields.Selection([('a', 'A')])\n"
    generated = _gen(models_py)
    original = generated.models_py
    asyncio.run(_autofix_add_mail_thread_inherit_when_tracking_kwarg_used(generated, "odoo16_dev", None))
    assert generated.models_py == original
    print("PASS: a no-op when no field uses tracking= at all")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
    print(f"\nALL {len(fns)} P11 DEPTH-AUDIT TIER A TESTS PASSED")
