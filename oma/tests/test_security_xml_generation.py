"""Phase 20 Area 2 (build plan §24.14.8, 2026-07-15): unit tests for the
new security_xml support in specialists/build/specialist.py -- pure
logic, no DB/infra dependency (mirrors GeneratedModuleFiles objects
directly), same style as the existing manifest-reference autofix tests
this module's own docstrings describe but which live inside
test_build_specialist.py's DB-heavy fixture file. Kept separate here so
these run cleanly even when that file's duplicate-DB fixture is stale.

Real bug this fixes: GeneratedModuleFiles previously had no field for a
res.groups/ir.rule definition file at all, so Build could never
generate a custom security group -- confirmed live on the first task
of the Area 2 batch (5 identical round failures, manifest referencing
'security/security.xml' with no content ever written for it).
"""

import ast
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

import specialists.build.specialist as specialist_module
from specialists.build.specialist import (
    GeneratedModuleFiles,
    ManifestFields,
    _autofix_bare_menu_restriction_id,
    _autofix_manifest_missing_dependency_for_cross_module_record_id,
    _autofix_manifest_missing_dependency_for_csv_model_refs,
    _autofix_manifest_hallucinated_custom_module_dependency,
    _autofix_manifest_missing_security_xml_reference,
    _autofix_manifest_security_csv_reference,
    _autofix_manifest_security_xml_reference,
    _autofix_security_csv_drops_inherit_only_rows,
    _autofix_models_py_missing_odoo_submodule_import,
    _autofix_xml_bare_model_ref_qualifies_owning_module,
    _files_from_generated,
    _autofix_xml_record_definition_order,
    _validate_ir_rule_domain_fields_exist,
    _validate_self_referenced_xmlids_are_defined,
    _validate_manifest_security_xml_references,
    _validate_new_model_name_not_already_real,
    _validate_xml_refs_resolve,
)


def _base_files(**overrides):
    """Phase 22 (2026-07-23): GeneratedModuleFiles.manifest_fields is
    now a typed ManifestFields object, not a raw manifest_py string --
    this helper keeps every existing test's own manifest_py="{...}"
    override (each encoding a real, specific test scenario -- a
    particular depends list, a particular data list) working unchanged
    by parsing the literal string into ManifestFields internally,
    rather than touching 20+ individual call sites throughout this
    file for a purely mechanical schema-shape change.
    """
    defaults = dict(
        manifest_py="{'name': 'x', 'version': '1.0', 'license': 'LGPL-3', 'depends': ['base'], "
                     "'data': ['security/ir.model.access.csv']}",
        models_py="from odoo import models, fields\n\nclass X(models.Model):\n    _inherit = 'res.partner'\n",
        security_csv="id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink",
        notes="",
    )
    defaults.update(overrides)
    manifest_py = defaults.pop("manifest_py")
    parsed = ast.literal_eval(manifest_py)
    parsed.setdefault("category", "Uncategorized")
    parsed.setdefault("summary", "")
    parsed.setdefault("author", "")
    parsed.setdefault("data", [])
    return GeneratedModuleFiles(manifest_fields=ManifestFields(**parsed), **defaults)


def test_files_from_generated_includes_security_xml_when_present():
    g = _base_files(security_xml="<odoo><record id='g1' model='res.groups'/></odoo>")
    files = _files_from_generated(g)
    assert files["security/security.xml"] == g.security_xml
    print("PASS: _files_from_generated() includes security/security.xml when set")


def test_files_from_generated_omits_security_xml_when_absent():
    g = _base_files()
    files = _files_from_generated(g)
    assert "security/security.xml" not in files
    print("PASS: _files_from_generated() omits security/security.xml when unset (plain field-add tasks unaffected)")


def test_autofix_rewrites_wrong_security_xml_filename():
    g = _base_files(
        manifest_py="{'name': 'x', 'version': '1.0', 'license': 'LGPL-3', 'depends': ['base'], "
                     "'data': ['security/ir.model.access.csv', 'security/groups.xml']}",
        security_xml="<odoo><record id='g1' model='res.groups'/></odoo>",
    )
    _autofix_manifest_security_xml_reference(g)
    assert "security/security.xml" in g.manifest_py
    assert "security/groups.xml" not in g.manifest_py
    print("PASS: wrong security XML filename ('security/groups.xml') rewritten to the real path")


def test_autofix_adds_missing_security_xml_reference():
    g = _base_files(
        manifest_py="{'name': 'x', 'version': '1.0', 'license': 'LGPL-3', 'depends': ['base'], "
                     "'data': ['security/ir.model.access.csv']}",
        security_xml="<odoo><record id='g1' model='res.groups'/></odoo>",
    )
    _autofix_manifest_missing_security_xml_reference(g)
    assert "security/security.xml" in g.manifest_py
    print("PASS: real security_xml content with no manifest reference gets the reference added")


def test_validator_rejects_reference_to_unwritten_file():
    g = _base_files(
        manifest_py="{'name': 'x', 'version': '1.0', 'license': 'LGPL-3', 'depends': ['base'], "
                     "'data': ['security/ir.model.access.csv', 'security/security.xml']}",
        security_xml=None,
    )
    with pytest.raises(ValueError, match="security_xml content was returned"):
        _validate_manifest_security_xml_references(g)
    print("PASS: manifest referencing security/security.xml with no real content raises")


def test_validator_passes_when_reference_and_content_match():
    g = _base_files(
        manifest_py="{'name': 'x', 'version': '1.0', 'license': 'LGPL-3', 'depends': ['base'], "
                     "'data': ['security/ir.model.access.csv', 'security/security.xml']}",
        security_xml="<odoo><record id='g1' model='res.groups'/></odoo>",
    )
    _validate_manifest_security_xml_references(g)  # must not raise
    print("PASS: matching reference + real content passes cleanly")


def test_plain_field_add_task_unaffected():
    """The overwhelming majority of real tasks (plain field adds, no
    custom group) never set security_xml at all -- every new
    validator/autofix here must be a true no-op for that shape,
    exactly like the views_xml equivalents already are.
    """
    g = _base_files()
    _autofix_manifest_missing_security_xml_reference(g)
    _autofix_manifest_security_xml_reference(g)
    _validate_manifest_security_xml_references(g)  # must not raise
    assert g.security_xml is None
    assert "security/security.xml" not in g.manifest_py
    print("PASS: plain field-add task (no security_xml) passes through every new check untouched")


def test_new_group_access_row_survives_on_inherit_only_module():
    """The real bug found live (2026-07-15) after the first fix: an
    `_inherit`-only module (no new `_name` model) granting a brand-new
    security group access to res.partner had its own legitimate access
    row silently stripped to header-only by the OLDER Area-1 autofix,
    which only knew about "new model," not "new group." A row for a
    genuinely new group is never a duplicate, regardless of whether the
    model is new.
    """
    g = _base_files(
        manifest_py="{'name': 'x', 'version': '1.0', 'license': 'LGPL-3', 'depends': ['base'], "
                     "'data': ['security/ir.model.access.csv', 'security/security.xml']}",
        models_py="from odoo import models, fields\n\nclass X(models.Model):\n    _inherit = 'res.partner'\n",
        security_xml=(
            "<odoo><record id=\"group_contacts_managers\" model=\"res.groups\">"
            "<field name=\"name\">Contacts Managers</field></record></odoo>"
        ),
        security_csv=(
            "id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink\n"
            "access_res_partner_mgr,res.partner.mgr,model_res_partner,"
            "oma_xyz.group_contacts_managers,1,1,1,1"
        ),
    )
    _autofix_security_csv_drops_inherit_only_rows(g)
    assert "group_contacts_managers" in g.security_csv, (
        f"the new-group access row was wrongly stripped: {g.security_csv!r}"
    )
    print("PASS: a CSV row granting a genuinely new group survives on an _inherit-only module")


def test_row_for_an_unrelated_existing_group_still_stripped():
    """Regression guard: the ORIGINAL Area-1 bug this function fixes
    (a duplicate row against a group that already has real access,
    e.g. base.group_user on an _inherit-only module) must still be
    stripped -- the new-group carve-out must not swallow the original
    fix.
    """
    g = _base_files(
        models_py="from odoo import models, fields\n\nclass X(models.Model):\n    _inherit = 'res.partner'\n",
        security_csv=(
            "id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink\n"
            "access_res_partner_dup,res.partner.dup,model_res_partner,base.group_user,1,1,1,0"
        ),
    )
    _autofix_security_csv_drops_inherit_only_rows(g)
    assert "base.group_user" not in g.security_csv, (
        f"the original Area-1 fix regressed -- duplicate base.group_user row not stripped: {g.security_csv!r}"
    )
    print("PASS: a duplicate row against an unrelated already-existing group is still correctly stripped")


def test_xml_refs_validator_rejects_hallucinated_ref(monkeypatch):
    """The real bug found live (2026-07-15): a generated views_xml
    referenced 'stock.action_stock_inventory_form', a plausible but
    entirely nonexistent external id -- confirmed against the real
    database it does not exist. Odoo's install crashes rc=255 trying
    to resolve it. Mocks resolve_xmlids_exist() (real version needs
    live SSH) to return exactly that real-world answer.
    """
    def fake_resolve(xmlids, db, **_kwargs):
        return {x: (x != "stock.action_stock_inventory_form") for x in xmlids}
    monkeypatch.setattr(specialist_module, "resolve_xmlids_exist", fake_resolve)

    g = _base_files(
        views_xml=(
            "<odoo><record id=\"menu_inventory\" model=\"ir.ui.menu\">"
            "<field name=\"action\" ref=\"stock.action_stock_inventory_form\"/></record></odoo>"
        ),
    )
    with pytest.raises(ValueError, match="stock.action_stock_inventory_form"):
        asyncio.run(_validate_xml_refs_resolve(g, db="odoo16_dev", own_module_name="oma_test"))
    print("PASS: a hallucinated ref=\"...\" external id is rejected before it can crash a real install")


def test_xml_refs_validator_catches_hallucinated_ref_inside_eval_call_syntax(monkeypatch):
    """Real, confirmed gap found live (2026-07-17): a many2many field
    assignment (`groups`/`groups_id`) is written as
    `eval="[(4, ref('module.xmlid'))]"` -- Odoo's `ref(...)` FUNCTION
    CALL syntax, completely different from the `ref="..."` ATTRIBUTE
    syntax the original _XML_REF_ATTR_RE only matched. Every ref-
    scanning validator was silently blind to this entire syntax form
    until _find_all_xml_refs() was built to cover both.
    """
    def fake_resolve(xmlids, db, **_kwargs):
        return {x: (x != "crm.group_totally_fake") for x in xmlids}
    monkeypatch.setattr(specialist_module, "resolve_xmlids_exist", fake_resolve)

    g = _base_files(
        security_xml=(
            '<odoo><record id="rule_x" model="ir.rule">'
            '<field name="model_id" ref="model_crm_lead"/>'
            "<field name=\"domain_force\">[('create_uid','=',user.id)]</field>"
            '<field name="groups" eval="[(4, ref(\'crm.group_totally_fake\'))]"/>'
            '</record></odoo>'
        ),
    )
    with pytest.raises(ValueError, match="crm.group_totally_fake"):
        asyncio.run(_validate_xml_refs_resolve(g, db="odoo16_dev", own_module_name="oma_test"))
    print("PASS: a hallucinated ref('...') inside an eval= attribute is now caught too")


def test_autofix_qualifies_bare_model_ref_in_xml(monkeypatch):
    """Real bug found live (2026-07-16), the XML-ref counterpart to
    the CSV bare-model-id bug: a generated security_xml referenced
    `ref="model_crm_lead"` (bare, no module prefix) -- confirmed via a
    real escalation that _validate_xml_refs_resolve correctly rejected
    it every round, but the model repeated the identical mistake on
    the very next round (non-progress loop) despite explicit feedback.
    This autofix must qualify it to 'crm.model_crm_lead' AND add 'crm'
    to depends, deterministically, before the validator ever runs.
    """
    def fake_resolve_owning_modules(names, db, own_module_name=None, **_kwargs):
        return {n: ("crm" if n == "model_crm_lead" else None) for n in names}
    monkeypatch.setattr(specialist_module, "resolve_owning_modules", fake_resolve_owning_modules)

    g = _base_files(
        manifest_py="{'name': 'x', 'version': '1.0', 'license': 'LGPL-3', 'depends': ['base'], "
                     "'data': ['security/security.xml']}",
        security_xml=(
            '<odoo><record id="access_crm_lead_managers" model="ir.model.access">'
            '<field name="model_id" ref="model_crm_lead"/>'
            '</record></odoo>'
        ),
    )
    asyncio.run(_autofix_xml_bare_model_ref_qualifies_owning_module(g, db="odoo16_dev", own_module_name="oma_test"))
    assert 'ref="crm.model_crm_lead"' in g.security_xml, (
        f"expected the bare ref qualified with the owning module, got: {g.security_xml!r}"
    )
    assert "'crm'" in g.manifest_py
    print("PASS: bare XML ref=\"model_crm_lead\" qualified to 'crm.model_crm_lead' and 'crm' added to depends")


def test_autofix_xml_ref_does_not_add_own_module_to_its_own_depends(monkeypatch):
    """XML-ref counterpart to the CSV-side self-dependency bug (see
    test_autofix_does_not_add_own_module_to_its_own_depends): the same
    "add every resolved owner to depends" step exists here too and had
    the identical bug -- a model_XXX ref resolving to the CALLER'S OWN
    module (fix 9's own exemption) must still qualify the ref, but must
    never add the module's own name to its own depends list.
    """
    def fake_resolve_owning_modules(names, db, own_module_name=None, **_kwargs):
        return {n: own_module_name for n in names}
    monkeypatch.setattr(specialist_module, "resolve_owning_modules", fake_resolve_owning_modules)

    g = _base_files(
        manifest_py="{'name': 'x', 'version': '1.0', 'license': 'LGPL-3', 'depends': ['base'], "
                     "'data': ['security/security.xml']}",
        security_xml=(
            '<odoo><record id="rule_x" model="ir.rule">'
            '<field name="model_id" ref="model_service_ticket"/>'
            '</record></odoo>'
        ),
    )
    asyncio.run(_autofix_xml_bare_model_ref_qualifies_owning_module(
        g, db="odoo16_dev", own_module_name="oma_create_a_small_new_559f773d",
    ))
    assert 'ref="oma_create_a_small_new_559f773d.model_service_ticket"' in g.security_xml
    assert "oma_create_a_small_new_559f773d" not in ast.literal_eval(g.manifest_py)["depends"], (
        f"module must never depend on itself, got depends: {ast.literal_eval(g.manifest_py)['depends']!r}"
    )
    print("PASS: an XML ref qualified with the caller's own module is never added to that module's own depends")


def test_xml_refs_validator_passes_real_ref(monkeypatch):
    def fake_resolve(xmlids, db, **_kwargs):
        return {x: True for x in xmlids}
    monkeypatch.setattr(specialist_module, "resolve_xmlids_exist", fake_resolve)

    g = _base_files(
        views_xml=(
            "<odoo><record id=\"stock.menu_stock_root\" model=\"ir.ui.menu\">"
            "<field name=\"groups_id\" eval=\"[(4, ref('oma_test.group_x'))]\"/></record></odoo>"
        ),
    )
    asyncio.run(_validate_xml_refs_resolve(g, db="odoo16_dev", own_module_name="oma_test"))  # must not raise
    print("PASS: a real, resolvable external id passes cleanly")


def test_xml_refs_validator_skips_self_references():
    """A ref prefixed with this module's OWN name (e.g. a security_csv
    row's group_id:id pointing at a res.groups record this SAME
    generation just defined) must never be checked against the live
    registry -- it genuinely doesn't exist there yet. No mock needed:
    if this didn't skip self-refs, it would call the real
    resolve_xmlids_exist() (live SSH) and hang/fail in this test
    environment, so a clean pass here proves the skip actually fires.
    """
    g = _base_files(
        security_xml=(
            "<odoo><record id=\"group_x\" model=\"res.groups\">"
            "<field name=\"name\">X</field>"
            "<field name=\"category_id\" ref=\"oma_test.some_local_category\"/>"
            "</record></odoo>"
        ),
    )
    asyncio.run(_validate_xml_refs_resolve(g, db="odoo16_dev", own_module_name="oma_test"))  # must not raise/hang
    print("PASS: refs prefixed with this module's own name are correctly skipped, never checked live")


def test_autofix_rewrites_bare_menu_restriction_id_to_real_xmlid():
    """The real, exact bug found live (2026-07-15): round 4 AND round 5
    of a real task both generated `<record id="menu_crm_root"
    model="ir.ui.menu">` -- a bare local id -- instead of the real
    external id 'crm.crm_menu_root', despite an explicit prompt rule.
    """
    g = _base_files(
        views_xml=(
            '<odoo><record id="menu_crm_root" model="ir.ui.menu">'
            '<field name="groups_id" eval="[(4, ref(\'oma_test.group_x\'))]"/>'
            '</record></odoo>'
        ),
    )
    _autofix_bare_menu_restriction_id(g)
    assert 'id="crm.crm_menu_root"' in g.views_xml
    assert 'id="menu_crm_root"' not in g.views_xml
    print("PASS: bare local menu-restriction id rewritten to the real external id")


def test_autofix_leaves_real_external_id_untouched():
    g = _base_files(
        views_xml=(
            '<odoo><record id="crm.crm_menu_root" model="ir.ui.menu">'
            '<field name="groups_id" eval="[(4, ref(\'oma_test.group_x\'))]"/>'
            '</record></odoo>'
        ),
    )
    original = g.views_xml
    _autofix_bare_menu_restriction_id(g)
    assert g.views_xml == original
    print("PASS: an already-correct real external id is left untouched")


def test_autofix_leaves_genuine_new_menu_definition_untouched():
    """A record that defines real new content (action/name), not just a
    restriction, legitimately gets a fresh local id -- must not be
    rewritten, since it isn't targeting an existing record at all.
    """
    g = _base_files(
        views_xml=(
            '<odoo><record id="menu_crm_custom_report" model="ir.ui.menu">'
            '<field name="name">Custom CRM Report</field>'
            '<field name="action" ref="oma_test.action_custom_report"/>'
            '</record></odoo>'
        ),
    )
    original = g.views_xml
    _autofix_bare_menu_restriction_id(g)
    assert g.views_xml == original
    print("PASS: a genuine new-menu-definition record (has name/action) is left untouched")


def test_autofix_adds_missing_dependency_for_cross_module_record_id():
    """The real bug found live (2026-07-15) via a direct, full-traceback
    reproduction: Odoo's own loader raises
    AssertionError('The ID "crm.crm_menu_root" refers to an
    uninstalled module') when a `<record id="crm.crm_menu_root">` is
    used but 'crm' isn't in this module's own manifest `depends` list
    -- confirmed as a hard assertion in Odoo's tools/convert.py, not a
    warning.
    """
    g = _base_files(
        manifest_py="{'name': 'x', 'version': '1.0', 'license': 'LGPL-3', 'depends': ['base'], "
                     "'data': ['security/security.xml']}",
        security_xml=(
            '<odoo><record id="crm.crm_menu_root" model="ir.ui.menu">'
            '<field name="groups_id" eval="[(4, ref(\'oma_test.group_x\'))]"/>'
            '</record></odoo>'
        ),
    )
    _autofix_manifest_missing_dependency_for_cross_module_record_id(g, own_module_name="oma_test")
    assert "'crm'" in g.manifest_py
    print("PASS: 'crm' added to depends when a record references crm.crm_menu_root")


def test_autofix_does_not_add_own_module_as_dependency():
    """A record id prefixed with THIS module's own name (a legitimate
    self-reference, e.g. a security_csv row's group_id:id pointing at
    a group this same generation defines) must never be added to
    depends -- a module cannot depend on itself.
    """
    g = _base_files(
        manifest_py="{'name': 'x', 'version': '1.0', 'license': 'LGPL-3', 'depends': ['base'], "
                     "'data': ['security/security.xml']}",
        security_xml='<odoo><record id="oma_test.group_x" model="res.groups"/></odoo>',
    )
    original = g.manifest_py
    _autofix_manifest_missing_dependency_for_cross_module_record_id(g, own_module_name="oma_test")
    assert g.manifest_py == original
    print("PASS: a self-referencing record id never adds this module as its own dependency")


def test_autofix_no_op_when_dependency_already_present():
    g = _base_files(
        manifest_py="{'name': 'x', 'version': '1.0', 'license': 'LGPL-3', 'depends': ['base', 'crm'], "
                     "'data': ['security/security.xml']}",
        security_xml='<odoo><record id="crm.crm_menu_root" model="ir.ui.menu"/></odoo>',
    )
    original = g.manifest_py
    _autofix_manifest_missing_dependency_for_cross_module_record_id(g, own_module_name="oma_test")
    assert g.manifest_py == original
    print("PASS: no-op when the referenced module is already a declared dependency")


def test_autofix_rewrites_security_csv_ref_missing_directory_prefix():
    """The real bug found live (2026-07-15): the manifest's data list
    referenced bare 'ir.model.access.csv' (no 'security/' directory
    prefix at all) -- invisible to the original security/-anchored
    regex, so it sailed through unrewritten and Odoo's install crashed
    trying to load a nonexistent module-root file.
    """
    g = _base_files(
        manifest_py="{'name': 'x', 'version': '1.0', 'license': 'LGPL-3', 'depends': ['base'], "
                     "'data': ['security/security.xml', 'ir.model.access.csv']}",
    )
    _autofix_manifest_security_csv_reference(g)
    assert "'security/ir.model.access.csv'" in g.manifest_py
    assert "'ir.model.access.csv'" not in g.manifest_py.replace("'security/ir.model.access.csv'", "")
    print("PASS: a security-CSV reference missing its directory prefix is corrected")


def test_rejects_new_model_name_that_already_exists(monkeypatch):
    """The real bug found live (2026-07-15): all 5 rounds of a real
    task generated `class ResGroups(models.Model): _name = 'res.groups'`
    -- redefining Odoo's own core model instead of using an XML
    <record model="res.groups"> in security_xml. Mocks get_model_fields
    (real version needs live SSH) to return the real-world answer:
    'res.groups' already exists.
    """
    def fake_get_model_fields(model_name, db):
        return ["id", "name"] if model_name == "res.groups" else None
    monkeypatch.setattr(specialist_module, "get_model_fields", fake_get_model_fields)

    g = _base_files(
        models_py="from odoo import models\n\nclass ResGroups(models.Model):\n    _name = 'res.groups'\n",
    )
    with pytest.raises(ValueError, match="already a real, existing Odoo model"):
        asyncio.run(_validate_new_model_name_not_already_real(g, db="odoo16_dev"))
    print("PASS: defining `_name = 'res.groups'` (an already-real model) is rejected")


def test_allows_genuinely_new_model_name(monkeypatch):
    def fake_get_model_fields(model_name, db):
        return None  # genuinely new, doesn't exist yet
    monkeypatch.setattr(specialist_module, "get_model_fields", fake_get_model_fields)

    g = _base_files(
        models_py="from odoo import models\n\nclass ServiceTicket(models.Model):\n    _name = 'service.ticket'\n",
    )
    asyncio.run(_validate_new_model_name_not_already_real(g, db="odoo16_dev"))  # must not raise
    print("PASS: a genuinely new, not-already-existing model name passes cleanly")


def test_allows_same_task_own_model_name_continuing_across_rounds(monkeypatch):
    """Real bug found live (2026-07-21, Operator demo, task 'warranty.
    claim'): a decomposed task's LATER constraint regenerating
    `_name = 'X'` for a model an EARLIER constraint of the SAME task
    already created was previously rejected here (or, briefly, silently
    rewritten to `_inherit` by a sibling autofix -- confirmed live that
    THAT rewrite itself crashes Odoo's registry, since this module's
    own class is the ONLY place that ever declares the model). Fixed:
    when own_module_name is given and matches the real owning module,
    `_name` is allowed through unchanged -- it's this module's own
    model continuing to exist across rounds, not a real redefinition.
    """
    monkeypatch.setattr(specialist_module, "get_model_fields", lambda name, db: ["id", "name"])
    monkeypatch.setattr(
        specialist_module, "list_custom_models",
        lambda db: [("warranty.claim", "oma_create_a_small_new_6da83730")],
    )
    g = _base_files(
        models_py="from odoo import models\n\nclass WarrantyClaim(models.Model):\n    _name = 'warranty.claim'\n",
    )
    asyncio.run(_validate_new_model_name_not_already_real(
        g, db="odoo16_dev", own_module_name="oma_create_a_small_new_6da83730",
    ))  # must not raise
    print("PASS: a same-task self-continuation of _name is allowed through, never rejected")


def test_still_rejects_when_owned_by_a_different_module(monkeypatch):
    monkeypatch.setattr(specialist_module, "get_model_fields", lambda name, db: ["id", "name"])
    monkeypatch.setattr(
        specialist_module, "list_custom_models",
        lambda db: [("warranty.claim", "oma_create_a_small_new_UNRELATED")],
    )
    g = _base_files(
        models_py="from odoo import models\n\nclass WarrantyClaim(models.Model):\n    _name = 'warranty.claim'\n",
    )
    with pytest.raises(ValueError, match="already a real, existing Odoo model"):
        asyncio.run(_validate_new_model_name_not_already_real(
            g, db="odoo16_dev", own_module_name="oma_create_a_small_new_6da83730",
        ))
    print("PASS: a real collision with a genuinely DIFFERENT module's model is still rejected")


def test_autofix_adds_missing_dependency_for_csv_model_ref(monkeypatch):
    """The real bug found live (2026-07-15), confirmed via a direct
    install reproduction: Odoo's CSV loader raises "No matching record
    found for external id 'model_project_task'" when security_csv
    grants a new group access to project.task but 'project' isn't in
    depends -- a bare CSV model_id:id has no module prefix to read
    directly, unlike an XML record id, so it needs a live lookup.
    """
    def fake_resolve_owning_modules(names, db, own_module_name=None, **_kwargs):
        return {n: ("project" if n == "model_project_task" else None) for n in names}
    monkeypatch.setattr(specialist_module, "resolve_owning_modules", fake_resolve_owning_modules)

    g = _base_files(
        manifest_py="{'name': 'x', 'version': '1.0', 'license': 'LGPL-3', 'depends': ['base'], "
                     "'data': ['security/security.xml', 'security/ir.model.access.csv']}",
        security_csv=(
            "id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink\n"
            "access_project_task,access.project.task,model_project_task,"
            "oma_test.group_tasks_managers,1,1,1,1"
        ),
    )
    asyncio.run(_autofix_manifest_missing_dependency_for_csv_model_refs(g, db="odoo16_dev"))
    assert "'project'" in g.manifest_py
    print("PASS: 'project' added to depends for a security_csv row granting access to project.task")


def test_autofix_qualifies_bare_csv_model_ref_with_owning_module(monkeypatch):
    """Real, second bug found live (2026-07-16), via a direct controlled
    A/B install reproduction: adding the owning module to `depends`
    alone is NOT sufficient -- Odoo's ir.model.access.csv loader only
    resolves a bare `model_id:id` value against models THIS module
    itself defines, never falling back to other installed modules'
    xmlids even when correctly declared as a dependency. Installing the
    exact same module twice, changing only this cell, proved it:
    bare 'model_project_task' failed every time with "No matching
    record found for external id 'model_project_task'"; the fully-
    qualified 'project.model_project_task' installed cleanly. This
    autofix must rewrite the CSV cell itself, not just fix depends.
    """
    def fake_resolve_owning_modules(names, db, own_module_name=None, **_kwargs):
        return {n: ("project" if n == "model_project_task" else None) for n in names}
    monkeypatch.setattr(specialist_module, "resolve_owning_modules", fake_resolve_owning_modules)

    g = _base_files(
        manifest_py="{'name': 'x', 'version': '1.0', 'license': 'LGPL-3', 'depends': ['base'], "
                     "'data': ['security/security.xml', 'security/ir.model.access.csv']}",
        security_csv=(
            "id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink\n"
            "access_project_task,access.project.task,model_project_task,"
            "oma_test.group_tasks_managers,1,1,1,1"
        ),
    )
    asyncio.run(_autofix_manifest_missing_dependency_for_csv_model_refs(g, db="odoo16_dev"))
    assert "'project'" in g.manifest_py
    assert "project.model_project_task" in g.security_csv, (
        f"expected the bare model_id:id cell rewritten to the fully-qualified form, "
        f"got: {g.security_csv!r}"
    )
    assert "\nmodel_project_task," not in ("\n" + g.security_csv), (
        "the bare, unqualified form must not remain in the rewritten CSV"
    )
    print("PASS: bare CSV model_id:id cell rewritten to the fully-qualified 'project.model_project_task' form")


def test_autofix_does_not_add_own_module_to_its_own_depends(monkeypatch):
    """Real bug found live (2026-07-20, #43, Phase 20 Area 2 pass 13):
    a direct, confirmed side effect of fix 9 (resolve_owning_modules()'s
    own-module exemption) -- once a model_XXX ref resolves to the
    CALLER'S OWN current module (the whole point of fix 9: a decomposed
    task's own earlier constraint), this autofix's "add every resolved
    owner to depends" step blindly added the module's OWN name to its
    OWN depends list (`'depends': ['base', 'oma_create_a_small_new_
    559f773d']` -- confirmed via a direct diff on the exact generated
    manifest), an invalid self-dependency that crashed Odoo's install
    with a generic, unhelpful "Install failed (rc=255)" 3 rounds
    running. A module must never be told to depend on itself.
    """
    def fake_resolve_owning_modules(names, db, own_module_name=None, **_kwargs):
        return {n: own_module_name for n in names}
    monkeypatch.setattr(specialist_module, "resolve_owning_modules", fake_resolve_owning_modules)

    g = _base_files(
        manifest_py="{'name': 'x', 'version': '1.0', 'license': 'LGPL-3', 'depends': ['base'], "
                     "'data': ['security/security.xml', 'security/ir.model.access.csv']}",
        security_csv=(
            "id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink\n"
            "access_x,access.x,model_service_ticket,oma_create_a_small_new_559f773d.group_x,1,1,1,1"
        ),
    )
    asyncio.run(_autofix_manifest_missing_dependency_for_csv_model_refs(
        g, db="odoo16_dev", own_module_name="oma_create_a_small_new_559f773d",
    ))
    assert "oma_create_a_small_new_559f773d" not in ast.literal_eval(g.manifest_py)["depends"], (
        f"module must never depend on itself, got depends: {ast.literal_eval(g.manifest_py)['depends']!r}"
    )
    print("PASS: a model owned by the caller's own module is never added to that module's own depends list")


def test_autofix_skips_csv_ref_to_this_modules_own_new_model():
    """A model_id:id referencing THIS module's own new `_name` model
    genuinely has no owning module yet (not installed) -- must not
    attempt a live lookup or add anything for it. No mock needed: if
    this didn't skip it, it would call the real resolve_owning_modules
    (live SSH) and hang/fail in this test environment.
    """
    g = _base_files(
        manifest_py="{'name': 'x', 'version': '1.0', 'license': 'LGPL-3', 'depends': ['base'], "
                     "'data': ['security/ir.model.access.csv']}",
        models_py="from odoo import models, fields\n\nclass X(models.Model):\n    _name = 'service.ticket'\n",
        security_csv=(
            "id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink\n"
            "access_service_ticket,access.service.ticket,model_service_ticket,base.group_user,1,1,1,0"
        ),
    )
    original = g.manifest_py
    asyncio.run(_autofix_manifest_missing_dependency_for_csv_model_refs(g, db="odoo16_dev"))
    assert g.manifest_py == original
    print("PASS: a CSV ref to this module's own new model is skipped, never triggers a live lookup")


def test_autofix_strips_hallucinated_dependency_on_unrelated_generated_module():
    """The real bug found live (2026-07-15): a task with NO
    depends_on_module set (confirmed directly against the real LLM
    prompt actually sent -- the string never appeared in it) still
    generated 'depends': ['base', 'oma_create_a_small_new_477b3067']
    -- a real, unrelated other task's own module, referenced for no
    legitimate reason. Dangerous because the referenced module IS
    real, so Odoo installs cleanly while silently doing something
    never asked for -- confirmed live: Testing/QA reported "passed"
    even though the real access group was never created. Originally a
    raising validator; converted to a silent autofix after confirming
    live the model repeats the exact same hallucination on retry (a
    non-progress loop).
    """
    g = _base_files(
        manifest_py="{'name': 'oma_this_task', 'version': '1.0', 'license': 'LGPL-3', "
                     "'depends': ['base', 'oma_create_a_small_new_477b3067']}",
    )
    _autofix_manifest_hallucinated_custom_module_dependency(
        g, own_module_name="oma_this_task", depends_on_module=None,
    )
    assert "oma_create_a_small_new_477b3067" not in g.manifest_py
    assert "'base'" in g.manifest_py
    print("PASS: a hallucinated dependency on an unrelated other generated module is stripped, base kept")


def test_autofix_keeps_legitimate_depends_on_module():
    g = _base_files(
        manifest_py="{'name': 'oma_this_task', 'version': '1.0', 'license': 'LGPL-3', "
                     "'depends': ['base', 'oma_prior_task_abc123']}",
    )
    _autofix_manifest_hallucinated_custom_module_dependency(
        g, own_module_name="oma_this_task", depends_on_module="oma_prior_task_abc123",
    )
    assert "oma_prior_task_abc123" in g.manifest_py
    print("PASS: a legitimately-passed depends_on_module is kept, not stripped")


def test_autofix_leaves_standard_odoo_modules_in_depends():
    g = _base_files(
        manifest_py="{'name': 'oma_this_task', 'version': '1.0', 'license': 'LGPL-3', "
                     "'depends': ['base', 'crm', 'project']}",
    )
    original = g.manifest_py
    _autofix_manifest_hallucinated_custom_module_dependency(
        g, own_module_name="oma_this_task", depends_on_module=None,
    )
    assert g.manifest_py == original
    print("PASS: standard, non-oma_-prefixed Odoo module dependencies are never touched")


def test_autofix_adds_missing_fields_import():
    """Real bug found live (2026-07-16, Phase 20 Area 2), via direct
    reproduction of task #22's real escalation (5/5 identical rounds,
    all a generic 'Sandbox install failed rc=255' with no specific
    validator ever catching it): models_py used `fields.Char(...)` with
    only `from odoo import models` -- confirmed via a direct install
    reproduction to crash with `NameError: name 'fields' is not
    defined` at Python import time.
    """
    g = _base_files(
        models_py=(
            "from odoo import models\n\n"
            "class Model(models.Model):\n"
            "    _name = 'oma_test.model'\n"
            "    name = fields.Char(string='Name')\n"
        ),
    )
    _autofix_models_py_missing_odoo_submodule_import(g)
    assert "from odoo import models, fields" in g.models_py or "from odoo import fields, models" in g.models_py
    print("PASS: missing 'fields' import added when models_py uses fields.X without importing it")


def test_autofix_leaves_correct_import_untouched():
    g = _base_files(
        models_py=(
            "from odoo import models, fields\n\n"
            "class Model(models.Model):\n"
            "    _name = 'oma_test.model'\n"
            "    name = fields.Char(string='Name')\n"
        ),
    )
    original = g.models_py
    _autofix_models_py_missing_odoo_submodule_import(g)
    assert g.models_py == original
    print("PASS: models_py with a correct existing 'fields' import is left untouched")


def test_autofix_prepends_import_when_none_exists():
    g = _base_files(
        models_py=(
            "class Model(models.Model):\n"
            "    _name = 'oma_test.model'\n"
            "    name = fields.Char(string='Name')\n"
        ),
    )
    _autofix_models_py_missing_odoo_submodule_import(g)
    assert g.models_py.startswith("from odoo import")
    assert "fields" in g.models_py.splitlines()[0]
    assert "models" in g.models_py.splitlines()[0]
    print("PASS: a fresh 'from odoo import ...' line is prepended when none exists at all")


def test_autofix_adds_missing_api_import():
    g = _base_files(
        models_py=(
            "from odoo import models, fields\n\n"
            "class Model(models.Model):\n"
            "    _name = 'oma_test.model'\n"
            "    name = fields.Char(string='Name')\n\n"
            "    @api.depends('name')\n"
            "    def _compute_x(self):\n"
            "        pass\n"
        ),
    )
    _autofix_models_py_missing_odoo_submodule_import(g)
    first_line = g.models_py.splitlines()[0]
    assert "api" in first_line and "fields" in first_line and "models" in first_line
    print("PASS: missing 'api' import added when models_py uses api.X without importing it")


def test_autofix_adds_data_key_when_missing_entirely_views():
    """Real bug found live (2026-07-17): the missing-views-reference
    autofix used to bail out silently whenever the manifest had NO
    'data' key at all (only handled an existing list missing this one
    entry) -- leaving real views_xml content written to disk but never
    loaded by Odoo, with nothing else in the chain catching the gap.
    """
    from specialists.build.specialist import _autofix_manifest_missing_views_reference
    g = _base_files(
        manifest_py="{'name': 'x', 'version': '1.0', 'license': 'LGPL-3', 'depends': ['base']}",
        views_xml="<odoo><record id='v1' model='ir.ui.view'/></odoo>",
    )
    _autofix_manifest_missing_views_reference(g)
    assert "'views/views.xml'" in g.manifest_py
    print("PASS: 'data' key created from scratch when missing entirely, views.xml added")


def test_autofix_adds_data_key_when_missing_entirely_security():
    g = _base_files(
        manifest_py="{'name': 'x', 'version': '1.0', 'license': 'LGPL-3', 'depends': ['base']}",
        security_xml="<odoo><record id='g1' model='res.groups'/></odoo>",
    )
    _autofix_manifest_missing_security_xml_reference(g)
    assert "'security/security.xml'" in g.manifest_py
    print("PASS: 'data' key created from scratch when missing entirely, security.xml added")


def test_autofix_reorders_forward_referenced_record():
    """Real bug found live (2026-07-17), via direct install
    reproduction: a task correctly defined BOTH a new res.groups
    record and an ir.rule referencing it -- exactly the right shape --
    but the ir.rule (referencer) was written BEFORE the res.groups
    record (referenced), and Odoo's XML loader resolves ref() calls
    strictly in file order, so it crashed with
    "External ID not found in the system: '<module>.group_x'" even
    though the record genuinely existed later in the same file.
    """
    g = _base_files(
        security_xml=(
            '<odoo>\n'
            '  <record id="rule_x" model="ir.rule">\n'
            '    <field name="model_id" ref="project.model_project_task"/>\n'
            "    <field name=\"domain_force\">[('create_uid','=',user.id)]</field>\n"
            '    <field name="groups" eval="[(4, ref(\'oma_test.group_x\'))]"/>\n'
            '  </record>\n'
            '  <record id="group_x" model="res.groups">\n'
            '    <field name="name">Group X</field>\n'
            '  </record>\n'
            '</odoo>'
        ),
    )
    _autofix_xml_record_definition_order(g, own_module_name="oma_test")
    group_pos = g.security_xml.index('id="group_x"')
    rule_pos = g.security_xml.index('id="rule_x"')
    assert group_pos < rule_pos, (
        f"expected group_x reordered before rule_x, got: {g.security_xml!r}"
    )
    # Real content preserved, not dropped -- both records still present.
    assert 'model="res.groups"' in g.security_xml
    assert 'model="ir.rule"' in g.security_xml
    print("PASS: a forward-referenced record is reordered to come before its referencer")


def test_autofix_leaves_already_correct_order_untouched():
    g = _base_files(
        security_xml=(
            '<odoo>\n'
            '  <record id="group_x" model="res.groups">\n'
            '    <field name="name">Group X</field>\n'
            '  </record>\n'
            '  <record id="rule_x" model="ir.rule">\n'
            '    <field name="model_id" ref="project.model_project_task"/>\n'
            "    <field name=\"domain_force\">[('create_uid','=',user.id)]</field>\n"
            '    <field name="groups" eval="[(4, ref(\'oma_test.group_x\'))]"/>\n'
            '  </record>\n'
            '</odoo>'
        ),
    )
    original = g.security_xml
    _autofix_xml_record_definition_order(g, own_module_name="oma_test")
    assert g.security_xml == original
    print("PASS: an already-correctly-ordered security_xml is left untouched")


def test_self_referenced_xmlid_validator_rejects_undefined_group(monkeypatch):
    """Real bug found live (2026-07-17): a real record-rule task's
    ir.rule `groups` field referenced 'oma_xyz.group_project_task_
    restricted' but never actually defined a <record model="res.groups">
    for it anywhere in security_xml -- _validate_xml_refs_resolve
    deliberately skips self-prefixed refs assuming they're always
    legitimate, which this case disproves. Confirmed via direct
    reproduction: an unresolvable local xmlid crashes Odoo's install.
    """
    g = _base_files(
        security_xml=(
            '<odoo><record id="rule_x" model="ir.rule">'
            '<field name="model_id" ref="project.model_project_task"/>'
            "<field name=\"domain_force\">[('create_uid','=',user.id)]</field>"
            '<field name="groups" eval="[(4, ref(\'oma_test.group_never_defined\'))]"/>'
            '</record></odoo>'
        ),
    )
    with pytest.raises(ValueError, match="group_never_defined"):
        _validate_self_referenced_xmlids_are_defined(g, own_module_name="oma_test")
    print("PASS: a self-referenced group xmlid with no matching <record> is rejected")


def test_self_referenced_xmlid_validator_passes_when_defined():
    g = _base_files(
        security_xml=(
            '<odoo>'
            '<record id="group_x" model="res.groups"><field name="name">X</field></record>'
            '<record id="rule_x" model="ir.rule">'
            '<field name="model_id" ref="project.model_project_task"/>'
            "<field name=\"domain_force\">[('create_uid','=',user.id)]</field>"
            '<field name="groups" eval="[(4, ref(\'oma_test.group_x\'))]"/>'
            '</record></odoo>'
        ),
    )
    _validate_self_referenced_xmlids_are_defined(g, own_module_name="oma_test")
    print("PASS: a self-referenced group xmlid with a matching <record> passes cleanly")


def test_self_referenced_xmlid_validator_exempts_model_xxx_shaped_refs():
    """Real bug found live (2026-07-20, #43, Phase 20 Area 2 pass 12):
    a self-prefixed `<own_module>.model_service_ticket` ref -- a model
    an EARLIER constraint of the SAME decomposed task already created
    -- was wrongly rejected as "no matching <record id=...>" even
    though a model_XXX xmlid is NEVER hand-written as a <record>, it's
    auto-generated by Odoo when the model is created via Python `_name`.
    Confirmed as the direct cause of #43 failing constraint 2 even
    after fix 9 correctly qualified the ref.
    """
    g = _base_files(
        security_xml=(
            '<odoo><record id="rule_x" model="ir.rule">'
            '<field name="model_id" ref="oma_test.model_service_ticket"/>'
            "<field name=\"domain_force\">[('create_uid','=',user.id)]</field>"
            '</record></odoo>'
        ),
    )
    # Must not raise -- no <record id="model_service_ticket"> is expected to exist
    _validate_self_referenced_xmlids_are_defined(g, own_module_name="oma_test")
    print("PASS: a self-prefixed model_XXX ref is exempted from the local-record-definition check")


def test_ir_rule_domain_field_validator_rejects_invented_field(monkeypatch):
    """Real bug found live (2026-07-16/17): a real task ('restrict
    res.partner... records they created or are responsible for') failed
    all 5 rounds identically inventing a `responsible_id` field on
    res.partner, which has no such field -- confirmed via direct
    reproduction. This validator must catch it before the sandbox ever
    sees it, the same discipline as _validate_no_invented_related_fields
    but for domain_force strings instead of Python code.
    """
    def fake_resolve_model_names(names, db, **_kwargs):
        return {n: ("res.partner" if n == "model_res_partner" else None) for n in names}
    def fake_get_model_fields(model, db):
        return ["id", "name", "create_uid", "write_uid", "user_id", "company_id"]
    monkeypatch.setattr(specialist_module, "resolve_model_names_from_xmlids", fake_resolve_model_names)
    monkeypatch.setattr(specialist_module, "get_model_fields", fake_get_model_fields)

    g = _base_files(
        security_xml=(
            '<odoo><record id="rule_own" model="ir.rule">'
            '<field name="model_id" ref="model_res_partner"/>'
            "<field name=\"domain_force\">[('create_uid','=',user.id)|('responsible_id','=',user.id)]</field>"
            '</record></odoo>'
        ),
    )
    with pytest.raises(ValueError, match="responsible_id"):
        asyncio.run(_validate_ir_rule_domain_fields_exist(g, db="odoo16_dev"))
    print("PASS: an ir.rule domain_force referencing an invented field is rejected")


def test_ir_rule_domain_field_validator_surfaces_relevant_field_not_just_alphabetical(monkeypatch):
    """Real bug found live (2026-07-17): the error message used to show
    an arbitrary ALPHABETICAL prefix of the real field list -- for a
    large model (project.task has 197 fields), the one relevant field
    ('user_ids', real, vs. the invented 'user_id') never appeared in a
    naive first-20-alphabetical slice, and the SAME task re-invented
    the identical field 4 rounds straight despite this validator
    correctly rejecting it every round. This test builds a model with
    30 fields, none starting with 'a' through 't', so a naive
    alphabetical-prefix bug would never surface 'user_ids' -- the fix
    must rank by relevance (shared substring with the invented name)
    first.
    """
    def fake_resolve_model_names(names, db, **_kwargs):
        return {n: ("project.task" if n == "model_project_task" else None) for n in names}
    padding_fields = [f"zzz_padding_field_{i}" for i in range(30)]  # all sort after 'user_ids'
    def fake_get_model_fields(model, db):
        return ["id", "name", "user_ids", *padding_fields]
    monkeypatch.setattr(specialist_module, "resolve_model_names_from_xmlids", fake_resolve_model_names)
    monkeypatch.setattr(specialist_module, "get_model_fields", fake_get_model_fields)

    g = _base_files(
        security_xml=(
            '<odoo><record id="rule_own" model="ir.rule">'
            '<field name="model_id" ref="model_project_task"/>'
            "<field name=\"domain_force\">[('create_uid','=',user.id)|('user_id','=',user.id)]</field>"
            '</record></odoo>'
        ),
    )
    with pytest.raises(ValueError, match="user_ids") as exc_info:
        asyncio.run(_validate_ir_rule_domain_fields_exist(g, db="odoo16_dev"))
    assert "user_ids" in str(exc_info.value)
    print("PASS: the relevant real field ('user_ids') is surfaced even though it would never "
          "appear in a naive alphabetical-prefix truncation")


def test_ir_rule_domain_field_validator_passes_real_fields(monkeypatch):
    def fake_resolve_model_names(names, db, **_kwargs):
        return {n: ("res.partner" if n == "model_res_partner" else None) for n in names}
    def fake_get_model_fields(model, db):
        return ["id", "name", "create_uid", "write_uid", "user_id", "company_id"]
    monkeypatch.setattr(specialist_module, "resolve_model_names_from_xmlids", fake_resolve_model_names)
    monkeypatch.setattr(specialist_module, "get_model_fields", fake_get_model_fields)

    g = _base_files(
        security_xml=(
            '<odoo><record id="rule_own" model="ir.rule">'
            '<field name="model_id" ref="model_res_partner"/>'
            "<field name=\"domain_force\">[('create_uid','=',user.id)|('user_id','=',user.id)]</field>"
            '</record></odoo>'
        ),
    )
    asyncio.run(_validate_ir_rule_domain_fields_exist(g, db="odoo16_dev"))
    print("PASS: an ir.rule domain_force referencing only real fields passes cleanly")


def test_stripper_running_after_adder_still_produces_clean_depends():
    """Real bug found live (2026-07-16), root-caused via object-identity
    instrumentation after static analysis alone couldn't explain it: the
    stripper (_autofix_manifest_hallucinated_custom_module_dependency)
    used to run FIRST in _validate_generated_module's chain, correctly
    removing a hallucinated 'oma_create_a_small_new_XXXXXXXX' dependency
    -- but a LATER autofix in that same chain,
    _autofix_manifest_missing_dependency_for_cross_module_record_id (an
    ADDER that scans this candidate's OWN security_xml content for
    `<record id="module.name">` cross-references and blindly adds
    whatever module prefix it finds), re-added the exact same
    hallucinated dependency because the candidate's OWN security_xml
    content ITSELF contained a hallucinated cross-module record
    reference into that same nonexistent module -- the adder has no way
    to know that prefix was a hallucination, not a real cross-module
    reference. Confirmed live via a second, independently-reproduced
    case, not just the original one. This test proves the fix: running
    the adder first (as _validate_generated_module's real order now
    puts it) and the stripper LAST still yields a clean depends list,
    because the stripper has final say regardless of what re-added the
    bad entry.
    """
    g = _base_files(
        manifest_py="{'name': 'oma_this_task', 'version': '1.0', 'license': 'LGPL-3', "
                     "'depends': ['base'], 'data': ['security/security.xml']}",
        security_xml=(
            '<odoo><record id="oma_create_a_small_new_477b3067.group_x" model="res.groups">'
            '<field name="name">Tasks Managers</field>'
            '</record></odoo>'
        ),
    )
    # Adder runs first, exactly like the real chain: it blindly trusts the
    # hallucinated cross-module record reference and re-adds the module.
    _autofix_manifest_missing_dependency_for_cross_module_record_id(g, own_module_name="oma_this_task")
    assert "oma_create_a_small_new_477b3067" in g.manifest_py, (
        "sanity check: the adder should have added the hallucinated module first, "
        "otherwise this test isn't reproducing the real ordering bug"
    )
    # Stripper runs LAST -- must have final, authoritative say.
    _autofix_manifest_hallucinated_custom_module_dependency(
        g, own_module_name="oma_this_task", depends_on_module=None,
    )
    assert "oma_create_a_small_new_477b3067" not in g.manifest_py, (
        "stripper running after the adder must still remove the hallucinated dependency"
    )
    assert "'base'" in g.manifest_py
    print("PASS: stripper running last still produces a clean depends list even when an earlier "
          "adder re-introduced the hallucinated dependency from the candidate's own security_xml content")


if __name__ == "__main__":
    test_files_from_generated_includes_security_xml_when_present()
    test_files_from_generated_omits_security_xml_when_absent()
    test_autofix_rewrites_wrong_security_xml_filename()
    test_autofix_adds_missing_security_xml_reference()
    test_validator_rejects_reference_to_unwritten_file()
    test_validator_passes_when_reference_and_content_match()
    test_plain_field_add_task_unaffected()
    test_new_group_access_row_survives_on_inherit_only_module()
    test_row_for_an_unrelated_existing_group_still_stripped()
    test_xml_refs_validator_rejects_hallucinated_ref()
    test_xml_refs_validator_catches_hallucinated_ref_inside_eval_call_syntax()
    test_autofix_qualifies_bare_model_ref_in_xml()
    test_xml_refs_validator_passes_real_ref()
    test_xml_refs_validator_skips_self_references()
    test_autofix_rewrites_bare_menu_restriction_id_to_real_xmlid()
    test_autofix_leaves_real_external_id_untouched()
    test_autofix_leaves_genuine_new_menu_definition_untouched()
    test_autofix_adds_missing_dependency_for_cross_module_record_id()
    test_autofix_does_not_add_own_module_as_dependency()
    test_autofix_no_op_when_dependency_already_present()
    test_autofix_rewrites_security_csv_ref_missing_directory_prefix()
    test_rejects_new_model_name_that_already_exists()
    test_allows_genuinely_new_model_name()
    test_autofix_adds_missing_dependency_for_csv_model_ref()
    test_autofix_qualifies_bare_csv_model_ref_with_owning_module()
    test_autofix_skips_csv_ref_to_this_modules_own_new_model()
    test_autofix_strips_hallucinated_dependency_on_unrelated_generated_module()
    test_autofix_keeps_legitimate_depends_on_module()
    test_autofix_leaves_standard_odoo_modules_in_depends()
    test_autofix_adds_missing_fields_import()
    test_autofix_leaves_correct_import_untouched()
    test_autofix_prepends_import_when_none_exists()
    test_autofix_adds_missing_api_import()
    test_autofix_adds_data_key_when_missing_entirely_views()
    test_autofix_adds_data_key_when_missing_entirely_security()
    test_autofix_reorders_forward_referenced_record()
    test_autofix_leaves_already_correct_order_untouched()
    test_self_referenced_xmlid_validator_rejects_undefined_group()
    test_self_referenced_xmlid_validator_passes_when_defined()
    test_ir_rule_domain_field_validator_rejects_invented_field()
    test_ir_rule_domain_field_validator_surfaces_relevant_field_not_just_alphabetical()
    test_ir_rule_domain_field_validator_passes_real_fields()
    test_stripper_running_after_adder_still_produces_clean_depends()
    print("\nALL SECURITY_XML GENERATION TESTS PASSED")
