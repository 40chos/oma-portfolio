"""2026-08-15: unit tests for `_autofix_natural_language_quick_filter_missing()` --
the real fix for the natural-language generalization gap
`_autofix_goal_named_search_filters_missing()`'s own module-level comment describes
(confirmed live, 3x reproduced, including 2x at temperature=0.0 -- a genuine
content-understanding gap the corrective retry loop alone never overcame; see the
overnight certification report, 2026-08-13/14/15).

REVISION (same day): the first version of this fix reused the round's own
(mistaken) inherit_id/model straight out of its wrong-content views_xml, assuming
the round always correctly targeted a search view. Live-proof testing immediately
disproved this -- a real round's decoration-* mistake was applied to crm.lead's
own FORM view, and reusing that inherit_id produced a real sandbox install crash
(ParseError: xpath '//search' not found in a form view). Fixed to resolve the
model and its real base search view independently and live (the same mechanism
Phase 34's sibling build_deterministic_search_view_xml() already uses) instead of
ever trusting the round's own targeting. These tests cover the corrected design.

Pure logic against monkeypatched call_structured/resolve_module_identity/
get_primary_search_view_xmlid_fast (no live LLM call, no live DB), matching this
project's established async-test pattern.
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import contracts.module_identity as module_identity_module
import specialists.build.specialist as specialist_module
import tools_odoo.odoo_schema_client as odoo_schema_client_module
from specialists.build.specialist import (
    GeneratedModuleFiles,
    ManifestFields,
    _autofix_natural_language_quick_filter_missing,
    _NaturalLanguageQuickFilterExtraction,
    _NaturalLanguageQuickFilterSpec,
)

_MANIFEST = ManifestFields(
    name="x", version="1.0.0", category="Tools", summary="x", author="x", depends=["base"], data=[],
)

_GOAL = (
    "In the crm.lead list, I want a quick filter button called 'Accepted' that shows only "
    "accepted records."
)

# A real, live-reproduced shape (2026-08-15): the mistaken round targeted crm.lead's own FORM
# view, not a search view -- this is exactly the case the first version of the fix got wrong.
_WRONG_VIEWS_XML_WRONG_VIEW_TYPE = """\
<odoo>
    <record id="crm_lead_view_form_oma_filters" model="ir.ui.view">
        <field name="name">crm_lead_view_form.oma.filters</field>
        <field name="model">crm.lead</field>
        <field name="inherit_id" ref="crm.crm_lead_view_form"/>
        <field name="arch" type="xml">
            <xpath expr="//header" position="inside">
                <attribute name="decoration-success">stage_id.is_won == True</attribute>
            </xpath>
        </field>
    </record>
</odoo>
"""


def _generated(views_xml: str) -> GeneratedModuleFiles:
    return GeneratedModuleFiles(
        manifest_fields=_MANIFEST, models_py="class X: pass", views_xml=views_xml,
        security_csv="x", notes="",
    )


def _run(coro):
    return asyncio.run(coro)


def _patch_resolve(monkeypatch, model_name):
    monkeypatch.setattr(module_identity_module, "resolve_module_identity", lambda goal, hint=None: model_name)


def _patch_base_view(monkeypatch, xmlid):
    monkeypatch.setattr(odoo_schema_client_module, "get_primary_search_view_xmlid_fast", lambda model, db, login="Admin": xmlid)


def _patch_model_fields(monkeypatch, fields):
    monkeypatch.setattr(odoo_schema_client_module, "get_model_fields_fast", lambda model, db, login="Admin": fields)


def test_fixes_the_real_decoration_vs_filter_mistake_end_to_end(monkeypatch):
    async def fake_call_structured(**kwargs):
        return _NaturalLanguageQuickFilterExtraction(
            filters=[_NaturalLanguageQuickFilterSpec(
                name="accepted", string="Accepted", domain="[('stage_id.is_won', '=', True)]",
            )],
            confidence=0.9,
        )

    monkeypatch.setattr(specialist_module, "call_structured", fake_call_structured)
    _patch_resolve(monkeypatch, "crm.lead")
    _patch_base_view(monkeypatch, "crm.crm_lead_view_search")
    _patch_model_fields(monkeypatch, ["stage_id", "name", "partner_id"])
    generated = _generated(_WRONG_VIEWS_XML_WRONG_VIEW_TYPE)
    _run(_autofix_natural_language_quick_filter_missing(
        generated, _GOAL, client=None, model="x", db="odoo16_dev", task_id=None,
    ))
    assert "<filter" in generated.views_xml
    assert 'name="accepted"' in generated.views_xml
    assert 'string="Accepted"' in generated.views_xml
    # the REAL, live-resolved search view -- never the round's own wrong form-view target
    assert "crm.crm_lead_view_search" in generated.views_xml
    assert "crm_lead_view_form" not in generated.views_xml
    print("PASS: fixes the real decoration-vs-filter mistake using the live-resolved real search view")


def test_leaves_untouched_when_a_real_filter_already_exists(monkeypatch):
    async def fake_call_structured(**kwargs):
        raise AssertionError("must never be called when a real <filter> already exists")

    monkeypatch.setattr(specialist_module, "call_structured", fake_call_structured)
    already_correct = _WRONG_VIEWS_XML_WRONG_VIEW_TYPE.replace(
        "<attribute name=\"decoration-success\">stage_id.is_won == True</attribute>",
        "<filter name=\"accepted\" string=\"Accepted\" domain=\"[('stage_id.is_won','=',True)]\"/>",
    )
    generated = _generated(already_correct)
    _run(_autofix_natural_language_quick_filter_missing(
        generated, _GOAL, client=None, model="x", db="odoo16_dev", task_id=None,
    ))
    assert generated.views_xml == already_correct
    print("PASS: never calls anything when a real <filter> is already present")


def test_leaves_untouched_when_goal_does_not_mention_quick_filter(monkeypatch):
    async def fake_call_structured(**kwargs):
        raise AssertionError("must never be called for an unrelated goal")

    monkeypatch.setattr(specialist_module, "call_structured", fake_call_structured)
    generated = _generated(_WRONG_VIEWS_XML_WRONG_VIEW_TYPE)
    _run(_autofix_natural_language_quick_filter_missing(
        generated, "add a field named foo to crm.lead", client=None, model="x", db="odoo16_dev", task_id=None,
    ))
    assert generated.views_xml == _WRONG_VIEWS_XML_WRONG_VIEW_TYPE
    print("PASS: a goal with no quick-filter intent is left completely untouched")


def test_leaves_untouched_when_model_cannot_be_resolved(monkeypatch):
    async def fake_call_structured(**kwargs):
        raise AssertionError("must never be called when the model can't be resolved")

    monkeypatch.setattr(specialist_module, "call_structured", fake_call_structured)
    _patch_resolve(monkeypatch, None)
    generated = _generated(_WRONG_VIEWS_XML_WRONG_VIEW_TYPE)
    _run(_autofix_natural_language_quick_filter_missing(
        generated, _GOAL, client=None, model="x", db="odoo16_dev", task_id=None,
    ))
    assert "<filter" not in (generated.views_xml or "")
    print("PASS: bails safely when the target model can't be resolved from the goal")


def test_leaves_untouched_when_no_real_base_search_view_found_live(monkeypatch):
    async def fake_call_structured(**kwargs):
        raise AssertionError("must never be called when no real base search view is found")

    monkeypatch.setattr(specialist_module, "call_structured", fake_call_structured)
    _patch_resolve(monkeypatch, "crm.lead")
    _patch_base_view(monkeypatch, None)
    generated = _generated(_WRONG_VIEWS_XML_WRONG_VIEW_TYPE)
    _run(_autofix_natural_language_quick_filter_missing(
        generated, _GOAL, client=None, model="x", db="odoo16_dev", task_id=None,
    ))
    assert "<filter" not in (generated.views_xml or "")
    print("PASS: bails safely when the live registry has no real base search view for this model")


def test_leaves_untouched_on_low_confidence_extraction(monkeypatch):
    async def fake_call_structured(**kwargs):
        return _NaturalLanguageQuickFilterExtraction(
            filters=[_NaturalLanguageQuickFilterSpec(name="accepted", string="Accepted", domain="[]")],
            confidence=0.2,
        )

    monkeypatch.setattr(specialist_module, "call_structured", fake_call_structured)
    _patch_resolve(monkeypatch, "crm.lead")
    _patch_base_view(monkeypatch, "crm.crm_lead_view_search")
    _patch_model_fields(monkeypatch, ["state"])
    generated = _generated(_WRONG_VIEWS_XML_WRONG_VIEW_TYPE)
    _run(_autofix_natural_language_quick_filter_missing(
        generated, _GOAL, client=None, model="x", db="odoo16_dev", task_id=None,
    ))
    assert generated.views_xml == _WRONG_VIEWS_XML_WRONG_VIEW_TYPE
    print("PASS: a low-confidence extraction is never trusted enough to rewrite views_xml")


def test_leaves_untouched_when_domain_is_not_a_valid_python_literal(monkeypatch):
    async def fake_call_structured(**kwargs):
        return _NaturalLanguageQuickFilterExtraction(
            filters=[_NaturalLanguageQuickFilterSpec(
                name="accepted", string="Accepted", domain="not valid python {{{",
            )],
            confidence=0.9,
        )

    monkeypatch.setattr(specialist_module, "call_structured", fake_call_structured)
    _patch_resolve(monkeypatch, "crm.lead")
    _patch_base_view(monkeypatch, "crm.crm_lead_view_search")
    _patch_model_fields(monkeypatch, ["state"])
    generated = _generated(_WRONG_VIEWS_XML_WRONG_VIEW_TYPE)
    _run(_autofix_natural_language_quick_filter_missing(
        generated, _GOAL, client=None, model="x", db="odoo16_dev", task_id=None,
    ))
    assert generated.views_xml == _WRONG_VIEWS_XML_WRONG_VIEW_TYPE
    print("PASS: an unsafe/unparsable domain literal is never rendered into XML")


def test_leaves_untouched_when_domain_is_not_a_list(monkeypatch):
    async def fake_call_structured(**kwargs):
        return _NaturalLanguageQuickFilterExtraction(
            filters=[_NaturalLanguageQuickFilterSpec(name="accepted", string="Accepted", domain="42")],
            confidence=0.9,
        )

    monkeypatch.setattr(specialist_module, "call_structured", fake_call_structured)
    _patch_resolve(monkeypatch, "crm.lead")
    _patch_base_view(monkeypatch, "crm.crm_lead_view_search")
    _patch_model_fields(monkeypatch, ["state"])
    generated = _generated(_WRONG_VIEWS_XML_WRONG_VIEW_TYPE)
    _run(_autofix_natural_language_quick_filter_missing(
        generated, _GOAL, client=None, model="x", db="odoo16_dev", task_id=None,
    ))
    assert generated.views_xml == _WRONG_VIEWS_XML_WRONG_VIEW_TYPE
    print("PASS: a domain that parses but isn't a list is rejected, never rendered")


def test_leaves_untouched_when_call_structured_raises(monkeypatch):
    async def fake_call_structured(**kwargs):
        raise RuntimeError("model gateway unavailable")

    monkeypatch.setattr(specialist_module, "call_structured", fake_call_structured)
    _patch_resolve(monkeypatch, "crm.lead")
    _patch_base_view(monkeypatch, "crm.crm_lead_view_search")
    _patch_model_fields(monkeypatch, ["state"])
    generated = _generated(_WRONG_VIEWS_XML_WRONG_VIEW_TYPE)
    _run(_autofix_natural_language_quick_filter_missing(
        generated, _GOAL, client=None, model="x", db="odoo16_dev", task_id=None,
    ))
    assert generated.views_xml == _WRONG_VIEWS_XML_WRONG_VIEW_TYPE
    print("PASS: a call_structured failure fails open, never crashes the round")


def test_bails_on_the_whole_extraction_if_any_one_filter_name_is_unsafe(monkeypatch):
    async def fake_call_structured(**kwargs):
        return _NaturalLanguageQuickFilterExtraction(
            filters=[
                _NaturalLanguageQuickFilterSpec(name="accepted", string="Accepted", domain="[('state','=','accepted')]"),
                _NaturalLanguageQuickFilterSpec(name="NOT SAFE!!", string="Bad", domain="[]"),
            ],
            confidence=0.9,
        )

    monkeypatch.setattr(specialist_module, "call_structured", fake_call_structured)
    _patch_resolve(monkeypatch, "crm.lead")
    _patch_base_view(monkeypatch, "crm.crm_lead_view_search")
    _patch_model_fields(monkeypatch, ["state"])
    generated = _generated(_WRONG_VIEWS_XML_WRONG_VIEW_TYPE)
    _run(_autofix_natural_language_quick_filter_missing(
        generated, _GOAL, client=None, model="x", db="odoo16_dev", task_id=None,
    ))
    assert generated.views_xml == _WRONG_VIEWS_XML_WRONG_VIEW_TYPE
    print("PASS: one malformed filter spec voids the whole extraction rather than half-fixing")


def test_leaves_untouched_when_schema_fetch_fails(monkeypatch):
    async def fake_call_structured(**kwargs):
        raise AssertionError("must never be called when the live schema can't be fetched")

    monkeypatch.setattr(specialist_module, "call_structured", fake_call_structured)
    _patch_resolve(monkeypatch, "crm.lead")
    _patch_base_view(monkeypatch, "crm.crm_lead_view_search")
    _patch_model_fields(monkeypatch, None)
    generated = _generated(_WRONG_VIEWS_XML_WRONG_VIEW_TYPE)
    _run(_autofix_natural_language_quick_filter_missing(
        generated, _GOAL, client=None, model="x", db="odoo16_dev", task_id=None,
    ))
    assert generated.views_xml == _WRONG_VIEWS_XML_WRONG_VIEW_TYPE
    print("PASS: bails safely when the live schema itself can't be fetched")


def test_leaves_untouched_when_domain_references_a_field_not_on_the_model(monkeypatch):
    """Real, live-reproduced bug (2026-08-15, task 46462d51-...): the extraction guessed a
    field ('description') that doesn't exist on product.template at all -- caught downstream
    by Testing/QA's own field-existence validator, but only after a wasted round and a real
    bake-in-certification qualifying failure. This is the fix: catch it here, before ever
    rendering it."""
    async def fake_call_structured(**kwargs):
        return _NaturalLanguageQuickFilterExtraction(
            filters=[_NaturalLanguageQuickFilterSpec(
                name="no_description", string="No Description", domain="[('description', '=', False)]",
            )],
            confidence=0.9,
        )

    monkeypatch.setattr(specialist_module, "call_structured", fake_call_structured)
    _patch_resolve(monkeypatch, "product.template")
    _patch_base_view(monkeypatch, "product.product_template_search_view")
    _patch_model_fields(monkeypatch, ["name", "list_price", "description_sale"])  # no "description"
    generated = _generated(_WRONG_VIEWS_XML_WRONG_VIEW_TYPE)
    _run(_autofix_natural_language_quick_filter_missing(
        generated, _GOAL, client=None, model="x", db="odoo16_dev", task_id=None,
    ))
    assert "<filter" not in (generated.views_xml or "")
    print("PASS: a domain referencing a field that doesn't exist on the real model is never rendered")


if __name__ == "__main__":
    class _MonkeyPatch:
        def setattr(self, obj, name, value):
            setattr(obj, name, value)

    mp = _MonkeyPatch()
    test_fixes_the_real_decoration_vs_filter_mistake_end_to_end(mp)
    test_leaves_untouched_when_a_real_filter_already_exists(mp)
    test_leaves_untouched_when_goal_does_not_mention_quick_filter(mp)
    test_leaves_untouched_when_model_cannot_be_resolved(mp)
    test_leaves_untouched_when_no_real_base_search_view_found_live(mp)
    test_leaves_untouched_on_low_confidence_extraction(mp)
    test_leaves_untouched_when_domain_is_not_a_valid_python_literal(mp)
    test_leaves_untouched_when_domain_is_not_a_list(mp)
    test_leaves_untouched_when_call_structured_raises(mp)
    test_bails_on_the_whole_extraction_if_any_one_filter_name_is_unsafe(mp)
    test_leaves_untouched_when_schema_fetch_fails(mp)
    test_leaves_untouched_when_domain_references_a_field_not_on_the_model(mp)
    print("\nALL NATURAL-LANGUAGE QUICK-FILTER AUTOFIX TESTS PASSED")
