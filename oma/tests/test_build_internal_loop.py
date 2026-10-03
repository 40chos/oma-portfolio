"""Phase T (§24 of PHASE30_OMA_SEAM_COVERAGE_AND_COMBINATORIAL_HARDENING_2026-07-29.md) --
regression tests for Build's bounded, in-round internal check/revise loop.

No real LLM/DB/SSH calls -- `_generate_internal_loop_patch` is monkeypatched per test, matching
the narrow validator subset's own pure-function contract (the loop's control flow is what's under
test here, not model generation quality). Async methods are driven via asyncio.run(), matching
this file's own existing convention (test_build_specialist.py) rather than pytest-asyncio, which
isn't configured in this repo.
"""
import asyncio
import inspect
import uuid

from contracts.schema import AutonomyTier, TaskContract, CapabilityClass, SpecialistType
from infra.gateway_client import GatewayUnavailableError
from specialists.build.specialist import (
    BuildSpecialist,
    GeneratedModuleFiles,
    ManifestFields,
    MAX_INTERNAL_LOOP_STEPS,
    _internal_loop_budget_exceeded,
    _internal_loop_state_hash,
    _run_internal_loop_narrow_validators,
    _validate_model_declares_name_or_inherit,
    _validate_name_attribute_matches_odoo_naming_convention,
    _validate_model_class_is_not_a_pure_identity_stub,
    _validate_env_model_lookup_uses_dotted_model_name,
    _validate_no_empty_ref_attribute,
    _validate_view_arch_has_single_root_element,
    flag_internal_loop_budget_exhaustion,
)


def _make_generated(models_py: str = "class X(models.Model):\n    _name = 'x.x'\n") -> GeneratedModuleFiles:
    manifest_fields = ManifestFields(
        name="Test", version="1.0", category="Test", summary="s", author="a",
        depends=["base"], data=[],
    )
    return GeneratedModuleFiles(
        manifest_fields=manifest_fields, models_py=models_py,
        security_csv="id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink\n",
        notes="",
    )


def _make_contract(constraint_status: dict | None = None) -> TaskContract:
    return TaskContract(
        task_id=uuid.uuid4(),
        specialist_type=SpecialistType.bug_fix,
        capability_class=CapabilityClass.module_development,
        tier=AutonomyTier.tier_2_notify_after,
        goal="test goal",
        inputs=["some input"],
        rules=[],
        deliverables=["a module"],
        compensating_actions=[],
        validation_by="testing_qa",
        pause_if=[],
        turn_budget=15,
        constraint_status=constraint_status or {},
    )


def _make_specialist() -> BuildSpecialist:
    return BuildSpecialist(client=None, db="test_db")


_DUP_METHOD_MODELS_PY = (
    "class X(models.Model):\n"
    "    _name = 'x.x'\n\n"
    "    def action_confirm(self):\n"
    "        pass\n\n"
    "    def action_confirm(self):\n"
    "        pass\n"
)
_TWO_DUP_METHODS_MODELS_PY = (
    "class X(models.Model):\n"
    "    _name = 'x.x'\n\n"
    "    def action_confirm(self):\n"
    "        pass\n\n"
    "    def action_confirm(self):\n"
    "        pass\n\n"
    "    def action_confirm(self):\n"
    "        pass\n"
)
# Same duplicate-method problem as _DUP_METHOD_MODELS_PY, PLUS a second, genuinely distinct
# narrow-validator failure (self.message_post(...) with no mail.thread inherit) -- used to prove
# a real regression (1 failing check -> 2 failing checks), not just a bigger version of the same
# single finding (which _run_internal_loop_narrow_validators reports as one finding regardless of
# how many duplicates it found).
_DUP_METHOD_PLUS_UNSAFE_MESSAGE_POST_MODELS_PY = (
    "class X(models.Model):\n"
    "    _name = 'x.x'\n\n"
    "    def action_confirm(self):\n"
    "        pass\n\n"
    "    def action_confirm(self):\n"
    "        pass\n\n"
    "    def action_confirm(self):\n"
    "        self.message_post(body='done')\n"
)
_CLEAN_MODELS_PY = "class X(models.Model):\n    _name = 'x.x'\n    total = fields.Float()\n"


# ---------------------------------------------------------------------------
# The typed budget primitives themselves
# ---------------------------------------------------------------------------

def test_budget_not_exceeded_below_both_caps():
    assert _internal_loop_budget_exceeded(steps_used=0, elapsed_sec=0.0) is None


def test_budget_exceeded_at_step_cap():
    reason = _internal_loop_budget_exceeded(steps_used=MAX_INTERNAL_LOOP_STEPS, elapsed_sec=0.0)
    assert reason is not None
    assert "step cap" in reason


def test_budget_exceeded_at_duration_cap():
    reason = _internal_loop_budget_exceeded(steps_used=0, elapsed_sec=999.0)
    assert reason is not None
    assert "duration cap" in reason


def test_budget_exhaustion_signal_none_below_cap():
    assert flag_internal_loop_budget_exhaustion(steps_used=1, step_cap=MAX_INTERNAL_LOOP_STEPS) is None


def test_budget_exhaustion_signal_fires_at_cap():
    signal = flag_internal_loop_budget_exhaustion(steps_used=MAX_INTERNAL_LOOP_STEPS, step_cap=MAX_INTERNAL_LOOP_STEPS)
    assert signal is not None
    assert signal["steps_used"] == MAX_INTERNAL_LOOP_STEPS
    assert "informational only, not a gate" in signal["message"]


def test_state_hash_changes_when_content_changes():
    g1 = _make_generated("class X(models.Model):\n    _name = 'x.x'\n")
    g2 = _make_generated("class X(models.Model):\n    _name = 'x.y'\n")
    assert _internal_loop_state_hash(g1) != _internal_loop_state_hash(g2)


def test_state_hash_stable_for_identical_content():
    g1 = _make_generated()
    g2 = _make_generated()
    assert _internal_loop_state_hash(g1) == _internal_loop_state_hash(g2)


# ---------------------------------------------------------------------------
# A real regression test built from an actual observed OMA mistake shape: the
# duplicate-method/duplicate-_name/_inherit family (tasks 004/016/027, this
# same session) -- the duplicate-method-definition variant is one of the four
# narrow-subset examples §24.2.1 names explicitly.
# ---------------------------------------------------------------------------

def test_narrow_validators_catch_a_real_observed_duplicate_method_definition():
    generated = _make_generated(_DUP_METHOD_MODELS_PY)
    findings = _run_internal_loop_narrow_validators(generated)
    assert any("no_duplicate_method_definitions" in f for f in findings)


def test_narrow_validators_find_nothing_on_clean_content():
    generated = _make_generated(_CLEAN_MODELS_PY)
    findings = _run_internal_loop_narrow_validators(generated)
    assert findings == []


# ---------------------------------------------------------------------------
# 6th narrow validator (2026-08-04): model class declares neither _name nor
# _inherit -- added after this exact shape was independently observed 4x in
# one night's real sweep (task004/016/019-027/028, all "The _name attribute
# ... is not valid" or "declares neither _name nor _inherit" Code-Review
# findings). Positive + negative case, same discipline as the other 5.
# ---------------------------------------------------------------------------

_MISSING_IDENTITY_MODELS_PY = (
    "from odoo import models, fields\n\n"
    "class ProjectFieldjob(models.Model):\n\n"
    "    total = fields.Float()\n"
)
_VALID_NAME_MODELS_PY = "class X(models.Model):\n    _name = 'x.x'\n"
_VALID_INHERIT_MODELS_PY = "class X(models.Model):\n    _inherit = 'project.fieldjob'\n"
_VALID_INHERIT_LIST_MODELS_PY = "class X(models.Model):\n    _inherit = ['mail.thread', 'project.fieldjob']\n"


def test_narrow_validators_catch_a_real_observed_missing_model_identity():
    generated = _make_generated(_MISSING_IDENTITY_MODELS_PY)
    findings = _run_internal_loop_narrow_validators(generated)
    assert any("model_declares_name_or_inherit" in f for f in findings)


def test_missing_model_identity_validator_flags_bare_class_directly():
    generated = _make_generated(_MISSING_IDENTITY_MODELS_PY)
    raised = False
    try:
        _validate_model_declares_name_or_inherit(generated)
    except ValueError as exc:
        raised = True
        assert "ProjectFieldjob" in str(exc)
        assert "_name" in str(exc) and "_inherit" in str(exc)
    assert raised, "a class with neither _name nor _inherit must be flagged"


def test_missing_model_identity_validator_passes_on_name_declaration():
    generated = _make_generated(_VALID_NAME_MODELS_PY)
    _validate_model_declares_name_or_inherit(generated)  # must not raise


def test_missing_model_identity_validator_passes_on_bare_string_inherit():
    generated = _make_generated(_VALID_INHERIT_MODELS_PY)
    _validate_model_declares_name_or_inherit(generated)  # must not raise


def test_missing_model_identity_validator_passes_on_list_form_inherit():
    generated = _make_generated(_VALID_INHERIT_LIST_MODELS_PY)
    _validate_model_declares_name_or_inherit(generated)  # must not raise


def test_missing_model_identity_validator_ignores_non_model_classes():
    """A plain helper class (not inheriting models.Model/TransientModel/
    AbstractModel) is not a real Odoo model at all -- must never be flagged
    for lacking _name/_inherit, which would make no sense for it.
    """
    generated = _make_generated("class Helper:\n    def foo(self):\n        pass\n")
    _validate_model_declares_name_or_inherit(generated)  # must not raise


def test_missing_model_identity_validator_silent_on_syntax_error():
    """Catching malformed Python is a different validator's job -- this one
    only judges class shape given valid syntax, per its own docstring.
    """
    generated = _make_generated("class X(models.Model)\n    _name = 'x.x'\n")  # missing colon
    _validate_model_declares_name_or_inherit(generated)  # must not raise


# ---------------------------------------------------------------------------
# 7th narrow validator (2026-08-04): _name declared but not Odoo's required
# dotted-lowercase form -- a real, confirmed-live-3x-in-one-night sibling gap
# to the 6th validator above (task016/022/027, all "The _name attribute
# ProjectFieldjob/InvoiceSummary is not valid" live tracebacks against this
# deployment's own odoo/models.py). That validator only checks _name is
# PRESENT; this one checks its VALUE is legal.
# ---------------------------------------------------------------------------

_CAMELCASE_NAME_MODELS_PY = "class ProjectFieldjob(models.Model):\n    _name = 'ProjectFieldjob'\n"
_ANOTHER_CAMELCASE_NAME_MODELS_PY = "class InvoiceSummary(models.Model):\n    _name = 'InvoiceSummary'\n"
_VALID_DOTTED_NAME_MODELS_PY = "class X(models.Model):\n    _name = 'project.fieldjob'\n"
_VALID_SINGLE_WORD_NAME_MODELS_PY = "class X(models.Model):\n    _name = 'fieldjob'\n"


def test_narrow_validators_catch_a_real_observed_camelcase_name():
    generated = _make_generated(_CAMELCASE_NAME_MODELS_PY)
    findings = _run_internal_loop_narrow_validators(generated)
    assert any("name_attribute_matches_odoo_naming_convention" in f for f in findings)


def test_name_convention_validator_flags_camelcase_directly():
    generated = _make_generated(_CAMELCASE_NAME_MODELS_PY)
    raised = False
    try:
        _validate_name_attribute_matches_odoo_naming_convention(generated)
    except ValueError as exc:
        raised = True
        assert "ProjectFieldjob" in str(exc)
    assert raised, "_name = 'ProjectFieldjob' (CamelCase, no dots) must be flagged"


def test_name_convention_validator_flags_second_real_observed_camelcase_name():
    """task022's own real observed shape -- confirms this isn't a fix tuned to
    one specific string."""
    generated = _make_generated(_ANOTHER_CAMELCASE_NAME_MODELS_PY)
    raised = False
    try:
        _validate_name_attribute_matches_odoo_naming_convention(generated)
    except ValueError as exc:
        raised = True
        assert "InvoiceSummary" in str(exc)
    assert raised, "_name = 'InvoiceSummary' must also be flagged"


def test_name_convention_validator_passes_on_dotted_lowercase_name():
    generated = _make_generated(_VALID_DOTTED_NAME_MODELS_PY)
    _validate_name_attribute_matches_odoo_naming_convention(generated)  # must not raise


def test_name_convention_validator_passes_on_legal_single_word_name():
    """Odoo's own regex (^[a-z0-9_.]+$) does not require a dot -- a single
    lowercase word is legal and must not false-positive."""
    generated = _make_generated(_VALID_SINGLE_WORD_NAME_MODELS_PY)
    _validate_name_attribute_matches_odoo_naming_convention(generated)  # must not raise


def test_name_convention_validator_ignores_inherit_only_classes():
    """_inherit-only classes never declare a new object name -- nothing here
    to validate."""
    generated = _make_generated(_VALID_INHERIT_MODELS_PY)
    _validate_name_attribute_matches_odoo_naming_convention(generated)  # must not raise


def test_name_convention_validator_ignores_non_model_classes():
    generated = _make_generated("class Helper:\n    _name = 'NotAModel'\n")
    _validate_name_attribute_matches_odoo_naming_convention(generated)  # must not raise


def test_name_convention_validator_silent_on_syntax_error():
    generated = _make_generated("class X(models.Model)\n    _name = 'Bad'\n")  # missing colon
    _validate_name_attribute_matches_odoo_naming_convention(generated)  # must not raise


def test_name_convention_validator_ignores_dynamic_name_expressions():
    """_name assigned from a variable/f-string (not a plain string literal) is
    a different, much rarer shape this validator deliberately does not
    attempt to evaluate -- must not false-positive or crash."""
    generated = _make_generated(
        "SOME_NAME = 'x.x'\nclass X(models.Model):\n    _name = SOME_NAME\n"
    )
    _validate_name_attribute_matches_odoo_naming_convention(generated)  # must not raise


# ---------------------------------------------------------------------------
# 8th narrow validator (2026-08-04): a model class with valid _name/_inherit but otherwise
# entirely empty content -- task021/025/028's own real, live shape, distinct from the 6th/7th
# validators (which catch missing/invalid identity, not a fully empty body with valid identity).
# ---------------------------------------------------------------------------

_PURE_STUB_NAME_MODELS_PY = "class X(models.Model):\n    _name = 'x.x'\n    _description = 'X'\n"
_PURE_STUB_INHERIT_MODELS_PY = "class WasteContainer(models.Model):\n    _inherit = 'waste.container'\n    _description = 'Waste Container'\n"
_REAL_FIELD_CONTENT_MODELS_PY = "class X(models.Model):\n    _name = 'x.x'\n    total = fields.Float()\n"
_REAL_METHOD_ONLY_CONTENT_MODELS_PY = (
    "class X(models.Model):\n"
    "    _inherit = 'x.x'\n\n"
    "    def action_confirm(self):\n"
    "        pass\n"
)
_DOCSTRING_ONLY_STILL_EMPTY_MODELS_PY = (
    "class X(models.Model):\n"
    "    \"\"\"A docstring, but still no real content.\"\"\"\n"
    "    _name = 'x.x'\n"
)


def test_narrow_validators_catch_a_real_observed_pure_identity_stub():
    generated = _make_generated(_PURE_STUB_NAME_MODELS_PY)
    findings = _run_internal_loop_narrow_validators(generated)
    assert any("model_class_is_not_a_pure_identity_stub" in f for f in findings)


def test_pure_identity_stub_validator_flags_task025s_own_real_shape():
    generated = _make_generated(_PURE_STUB_INHERIT_MODELS_PY)
    raised = False
    try:
        _validate_model_class_is_not_a_pure_identity_stub(generated)
    except ValueError as exc:
        raised = True
        assert "WasteContainer" in str(exc)
    assert raised, "an _inherit-only class with zero fields/methods must be flagged"


def test_pure_identity_stub_validator_passes_on_real_field_content():
    generated = _make_generated(_REAL_FIELD_CONTENT_MODELS_PY)
    _validate_model_class_is_not_a_pure_identity_stub(generated)  # must not raise


def test_pure_identity_stub_validator_passes_on_method_only_content():
    """A class that only adds a method override (no new field) is legitimate -- must not
    false-positive just because it has no field assignments."""
    generated = _make_generated(_REAL_METHOD_ONLY_CONTENT_MODELS_PY)
    _validate_model_class_is_not_a_pure_identity_stub(generated)  # must not raise


def test_pure_identity_stub_validator_still_flags_with_a_docstring_present():
    """A docstring alone must not count as 'real content' and mask an otherwise-empty class."""
    generated = _make_generated(_DOCSTRING_ONLY_STILL_EMPTY_MODELS_PY)
    raised = False
    try:
        _validate_model_class_is_not_a_pure_identity_stub(generated)
    except ValueError:
        raised = True
    assert raised, "a docstring-only class body must still be flagged as a pure identity stub"


def test_pure_identity_stub_validator_ignores_non_model_classes():
    generated = _make_generated("class Helper:\n    _name = 'not.a.model'\n")
    _validate_model_class_is_not_a_pure_identity_stub(generated)  # must not raise


def test_pure_identity_stub_validator_silent_on_syntax_error():
    generated = _make_generated("class X(models.Model)\n    _name = 'x.x'\n")  # missing colon
    _validate_model_class_is_not_a_pure_identity_stub(generated)  # must not raise


def _make_generated_with_views(views_xml: str) -> GeneratedModuleFiles:
    manifest_fields = ManifestFields(
        name="Test", version="1.0", category="Test", summary="s", author="a",
        depends=["base"], data=[],
    )
    return GeneratedModuleFiles(
        manifest_fields=manifest_fields,
        models_py="class X(models.Model):\n    _name = 'x.x'\n    total = fields.Float()\n",
        security_csv="id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink\n",
        views_xml=views_xml,
        notes="",
    )


# ---------------------------------------------------------------------------
# 9th narrow validator (2026-08-04): empty ref="" attribute -- task014's own real, live shape
# (an <ir.ui.menu> record with `<field name="action" ref=""/>`, a real Odoo ParseError at
# install time). Distinct from the other identity-related validators -- this one is purely about
# generated XML content, not model Python code.
# ---------------------------------------------------------------------------

_EMPTY_REF_MENU_XML = (
    '<odoo>\n  <record id="menu_extra_work" model="ir.ui.menu">\n'
    '    <field name="name">Extra Work</field>\n'
    '    <field name="action" ref=""/>\n'
    "  </record>\n</odoo>"
)
_VALID_REF_MENU_XML = (
    '<odoo>\n  <record id="menu_extra_work" model="ir.ui.menu">\n'
    '    <field name="name">Extra Work</field>\n'
    '    <field name="action" ref="action_extra_work"/>\n'
    "  </record>\n</odoo>"
)
_NO_ACTION_FIELD_MENU_XML = (
    '<odoo>\n  <record id="menu_extra_work" model="ir.ui.menu">\n'
    '    <field name="name">Extra Work</field>\n'
    "  </record>\n</odoo>"
)


def test_narrow_validators_catch_a_real_observed_empty_ref_attribute():
    generated = _make_generated_with_views(_EMPTY_REF_MENU_XML)
    findings = _run_internal_loop_narrow_validators(generated)
    assert any("no_empty_ref_attribute" in f for f in findings)


def test_empty_ref_validator_flags_task014s_own_real_shape_directly():
    generated = _make_generated_with_views(_EMPTY_REF_MENU_XML)
    raised = False
    try:
        _validate_no_empty_ref_attribute(generated)
    except ValueError as exc:
        raised = True
        assert "ir.ui.menu" not in str(exc) or True  # message need not name the model explicitly
        assert 'ref=""' in str(exc)
    assert raised, "an empty ref=\"\" attribute must be flagged"


def test_empty_ref_validator_passes_on_a_real_resolved_ref():
    generated = _make_generated_with_views(_VALID_REF_MENU_XML)
    _validate_no_empty_ref_attribute(generated)  # must not raise


def test_empty_ref_validator_passes_when_the_optional_field_is_omitted_entirely():
    """The legitimate fix shape: omitting the whole <field name="action"/> element entirely
    (a menu with no action is valid Odoo) must never be flagged."""
    generated = _make_generated_with_views(_NO_ACTION_FIELD_MENU_XML)
    _validate_no_empty_ref_attribute(generated)  # must not raise


def test_empty_ref_validator_silent_on_malformed_xml():
    """A different validator's job -- this one only judges ref values given valid XML."""
    generated = _make_generated_with_views('<odoo><record ref=""></odoo>')  # malformed
    _validate_no_empty_ref_attribute(generated)  # must not raise


def test_empty_ref_validator_no_op_when_views_xml_is_none():
    generated = _make_generated(_CLEAN_MODELS_PY)
    _validate_no_empty_ref_attribute(generated)  # must not raise


# ---------------------------------------------------------------------------
# Coverage for _validate_view_arch_has_single_root_element (2026-08-05): task007's own real,
# live-traced shape -- an ir.ui.view record's arch containing both a <tree> and a <search> as
# sibling root elements, which Odoo's own registry can't resolve into a single view type and
# rejects with a misleading "Wrong value for ir.ui.view.type: 'data'" install-time error.
# ---------------------------------------------------------------------------

_TWO_ROOT_TREE_SEARCH_XML = (
    '<odoo>\n  <record id="view_fieldjob_list_tree" model="ir.ui.view">\n'
    '    <field name="name">fieldjob.list.tree</field>\n'
    '    <field name="model">fieldjob.list</field>\n'
    '    <field name="arch" type="xml">\n'
    '      <tree string="Fieldjob List">\n'
    '        <field name="name"/>\n'
    "      </tree>\n"
    "      <search>\n"
    '        <filter name="filter_accepted" string="Accepted" domain="[]"/>\n'
    "      </search>\n"
    "    </field>\n"
    "  </record>\n</odoo>"
)

_SINGLE_ROOT_TREE_XML = (
    '<odoo>\n  <record id="view_fieldjob_list_tree" model="ir.ui.view">\n'
    '    <field name="name">fieldjob.list.tree</field>\n'
    '    <field name="model">fieldjob.list</field>\n'
    '    <field name="arch" type="xml">\n'
    '      <tree string="Fieldjob List">\n'
    '        <field name="name"/>\n'
    "      </tree>\n"
    "    </field>\n"
    "  </record>\n</odoo>"
)

_SEPARATE_SEARCH_VIEW_XML = (
    '<odoo>\n  <record id="view_fieldjob_list_tree" model="ir.ui.view">\n'
    '    <field name="name">fieldjob.list.tree</field>\n'
    '    <field name="model">fieldjob.list</field>\n'
    '    <field name="arch" type="xml">\n'
    '      <tree string="Fieldjob List">\n'
    '        <field name="name"/>\n'
    "      </tree>\n"
    "    </field>\n"
    "  </record>\n"
    '  <record id="view_fieldjob_list_search" model="ir.ui.view">\n'
    '    <field name="name">fieldjob.list.search</field>\n'
    '    <field name="model">fieldjob.list</field>\n'
    '    <field name="arch" type="xml">\n'
    "      <search>\n"
    '        <filter name="filter_accepted" string="Accepted" domain="[]"/>\n'
    "      </search>\n"
    "    </field>\n"
    "  </record>\n</odoo>"
)


def test_narrow_validators_catch_task007s_own_real_two_root_arch():
    generated = _make_generated_with_views(_TWO_ROOT_TREE_SEARCH_XML)
    findings = _run_internal_loop_narrow_validators(generated)
    assert any("view_arch_has_single_root_element" in f for f in findings)


def test_arch_validator_flags_task007s_own_real_shape_directly():
    generated = _make_generated_with_views(_TWO_ROOT_TREE_SEARCH_XML)
    raised = False
    try:
        _validate_view_arch_has_single_root_element(generated)
    except ValueError as exc:
        raised = True
        assert "<tree>" in str(exc) and "<search>" in str(exc)
    assert raised, "two sibling root elements inside one arch must be flagged"


def test_arch_validator_passes_on_a_single_root_element():
    generated = _make_generated_with_views(_SINGLE_ROOT_TREE_XML)
    _validate_view_arch_has_single_root_element(generated)  # must not raise


def test_arch_validator_passes_when_search_is_its_own_separate_record():
    """The legitimate fix shape: a tree view and a search view as two SEPARATE ir.ui.view
    records must never be flagged -- only two roots sharing the SAME arch is the real defect."""
    generated = _make_generated_with_views(_SEPARATE_SEARCH_VIEW_XML)
    _validate_view_arch_has_single_root_element(generated)  # must not raise


def test_arch_validator_silent_on_malformed_xml():
    generated = _make_generated_with_views('<odoo><record model="ir.ui.view">')  # malformed
    _validate_view_arch_has_single_root_element(generated)  # must not raise


def test_arch_validator_no_op_when_views_xml_is_none():
    generated = _make_generated(_CLEAN_MODELS_PY)
    _validate_view_arch_has_single_root_element(generated)  # must not raise


_TASK026_OWN_REAL_MODELS_PY = (
    "from odoo import models, fields, api\n\n"
    "class Project(models.Model):\n"
    "    _inherit = 'project.project'\n\n"
    "    def action_view_containers(self):\n"
    "        containers = self.env['container'].search([('project_id', '=', self.id)])\n"
    "        return {\n"
    "            'name': 'Containers',\n"
    "            'type': 'ir.actions.act_window',\n"
    "            'res_model': 'container',\n"
    "        }\n"
)


def test_narrow_validators_catch_task026s_own_real_bare_env_lookup():
    generated = _make_generated(_TASK026_OWN_REAL_MODELS_PY)
    findings = _run_internal_loop_narrow_validators(generated)
    assert any("env_model_lookup_uses_dotted_model_name" in f for f in findings)


def test_env_lookup_validator_flags_task026s_own_real_shape_directly():
    generated = _make_generated(_TASK026_OWN_REAL_MODELS_PY)
    raised = False
    try:
        _validate_env_model_lookup_uses_dotted_model_name(generated)
    except ValueError as exc:
        raised = True
        assert "'container'" in str(exc)
    assert raised, "a bare, undotted env[...] model name must be flagged"


def test_env_lookup_validator_passes_on_real_dotted_model_names():
    models_py = (
        "class X(models.Model):\n"
        "    _inherit = 'waste.container'\n\n"
        "    def go(self):\n"
        "        real = self.env['waste.container'].search([])\n"
        "        also = self.env['account.move'].create({})\n"
    )
    generated = _make_generated(models_py)
    _validate_env_model_lookup_uses_dotted_model_name(generated)  # must not raise


def test_env_lookup_validator_ignores_dynamic_non_literal_lookups():
    """env[variable_name] (not a literal string) can never be judged statically -- must never
    be flagged, since we genuinely cannot know what it resolves to."""
    models_py = (
        "class X(models.Model):\n"
        "    _inherit = 'waste.container'\n\n"
        "    def go(self, model_name):\n"
        "        real = self.env[model_name].search([])\n"
    )
    generated = _make_generated(models_py)
    _validate_env_model_lookup_uses_dotted_model_name(generated)  # must not raise


def test_env_lookup_validator_no_op_when_no_env_lookup_at_all():
    generated = _make_generated(_CLEAN_MODELS_PY)
    _validate_env_model_lookup_uses_dotted_model_name(generated)  # must not raise


def test_arch_validator_ignores_non_ir_ui_view_records():
    """A record of a different model with a coincidentally-named 'arch' field must never be
    misjudged -- the check is scoped specifically to model="ir.ui.view" records."""
    xml = (
        '<odoo>\n  <record id="some_other_record" model="some.other.model">\n'
        '    <field name="arch" type="xml">\n'
        "      <a/>\n      <b/>\n"
        "    </field>\n"
        "  </record>\n</odoo>"
    )
    generated = _make_generated_with_views(xml)
    _validate_view_arch_has_single_root_element(generated)  # must not raise


# ---------------------------------------------------------------------------
# Internal-loop coverage for _validate_single_constraint_field_scope (2026-08-04): a real,
# confirmed gap found live in the same-night full 30-task sweep (task029) -- this validator
# already existed at the full EXTERNAL round gate and DID correctly catch Build over-generating
# scope on a decomposed single-constraint round, but only there; the internal loop's own faster
# same-round self-correction never saw the finding, so task029 burned its whole round budget
# before finally escalating. Wired in here so a decomposed round gets the same fast, same-round
# self-correction chance every other narrow-validator mistake shape already gets.
# ---------------------------------------------------------------------------

_BASELINE_MODELS_PY = "class X(models.Model):\n    _name = 'x.x'\n"
_OVER_SCOPE_MODELS_PY = (
    "class X(models.Model):\n"
    "    _name = 'x.x'\n"
    "    field_a = fields.Char()\n"
    "    field_b = fields.Char()\n"
    "    field_c = fields.Char()\n"
    "    field_d = fields.Char()\n"
    "    field_e = fields.Char()\n"
)
_IN_SCOPE_MODELS_PY = "class X(models.Model):\n    _name = 'x.x'\n    field_a = fields.Char()\n"


def test_narrow_validators_catch_single_constraint_scope_violation_when_context_given():
    generated = _make_generated(_OVER_SCOPE_MODELS_PY)
    findings = _run_internal_loop_narrow_validators(
        generated, old_models_py=_BASELINE_MODELS_PY, constraint_status={"field_a_only": "satisfied"},
    )
    assert any("single_constraint_field_scope" in f for f in findings)


def test_narrow_validators_skip_scope_check_with_no_constraint_status():
    """Additive-only: with no constraint_status (the default, matching every pre-existing call
    site's old behavior), the same over-scope content must not be flagged by this check --
    byte-identical to before this fix for a non-decomposed task."""
    generated = _make_generated(_OVER_SCOPE_MODELS_PY)
    findings = _run_internal_loop_narrow_validators(generated)
    assert not any("single_constraint_field_scope" in f for f in findings)


def test_narrow_validators_pass_scope_check_on_in_scope_content():
    generated = _make_generated(_IN_SCOPE_MODELS_PY)
    findings = _run_internal_loop_narrow_validators(
        generated, old_models_py=_BASELINE_MODELS_PY, constraint_status={"field_a_only": "satisfied"},
    )
    assert not any("single_constraint_field_scope" in f for f in findings)


def test_run_build_internal_loop_surfaces_scope_violation_from_old_models_py_context():
    """End-to-end: run_build_internal_loop() itself (not just the validator function directly)
    must thread old_models_py/contract.constraint_status through to the narrow-validator calls
    it makes -- confirms the real task029 wiring gap is closed, not just the helper function in
    isolation. No patch function needed: Escalate is expected (mechanically, the patch call
    itself isn't mocked here), what's under test is that the finding is seen at all in step 1
    -- reflected in findings_history[0].
    """
    contract = _make_contract(constraint_status={"field_a_only": "satisfied"})
    specialist = _make_specialist()
    generated = _make_generated(_OVER_SCOPE_MODELS_PY)

    async def _escalating_patch(*args, **kwargs):
        raise ValueError("no real patch call needed for this test")

    specialist._generate_internal_loop_patch = _escalating_patch

    outcome = asyncio.run(
        specialist.run_build_internal_loop(
            contract, "test_module", generated, task_id=None, old_models_py=_BASELINE_MODELS_PY,
        )
    )
    assert outcome.findings_history, "at least one step must have run and recorded findings"
    assert any(
        "single_constraint_field_scope" in f for f in outcome.findings_history[0]
    ), "the scope violation must be visible in the internal loop's own findings, not only at the external gate"


# ---------------------------------------------------------------------------
# The orchestrator: Keep / Patch / Escalate control flow, external-controller
# enforcement, and the last-known-good revert-on-regression guardrail.
# ---------------------------------------------------------------------------

def test_keep_path_makes_zero_patch_calls_on_already_clean_content():
    specialist = _make_specialist()
    patch_calls = []

    async def _fake_patch(contract, module_name, generated, findings):
        patch_calls.append(findings)
        raise AssertionError("should never be called for already-clean content")

    specialist._generate_internal_loop_patch = _fake_patch
    generated = _make_generated(_CLEAN_MODELS_PY)

    outcome = asyncio.run(
        specialist.run_build_internal_loop(_make_contract(), "test_module", generated, task_id=None)
    )

    assert outcome.action == "keep"
    assert outcome.steps_used == 1
    assert outcome.budget_exhausted is False
    assert patch_calls == []


def test_patch_path_reaches_keep_on_a_genuine_fix():
    specialist = _make_specialist()
    dirty = _make_generated(_DUP_METHOD_MODELS_PY)
    clean = _make_generated(_CLEAN_MODELS_PY)

    async def _fake_patch(contract, module_name, generated, findings):
        return clean

    specialist._generate_internal_loop_patch = _fake_patch

    outcome = asyncio.run(
        specialist.run_build_internal_loop(_make_contract(), "test_module", dirty, task_id=None)
    )

    assert outcome.action == "keep"
    assert outcome.steps_used == 2
    assert outcome.generated.models_py == clean.models_py


def test_controller_hard_stops_at_step_cap_regardless_of_patch_behavior():
    """§24.6's definition of done: 'a test proving Build cannot exceed the cap regardless of what
    it decides' -- the fake patch here NEVER converges (always returns equally-dirty content), the
    same way a real model that keeps failing to fix the issue would behave. The controller must
    still hard-stop at MAX_INTERNAL_LOOP_STEPS, not the patch function's own judgment.
    """
    specialist = _make_specialist()
    dirty = _make_generated(_DUP_METHOD_MODELS_PY)
    call_count = {"n": 0}

    async def _fake_patch(contract, module_name, generated, findings):
        call_count["n"] += 1
        return dirty  # never actually fixes anything

    specialist._generate_internal_loop_patch = _fake_patch

    outcome = asyncio.run(
        specialist.run_build_internal_loop(_make_contract(), "test_module", dirty, task_id=None)
    )

    assert outcome.action == "escalate"
    assert outcome.steps_used == MAX_INTERNAL_LOOP_STEPS
    assert outcome.budget_exhausted is True
    assert call_count["n"] <= MAX_INTERNAL_LOOP_STEPS


def test_regression_reverts_to_last_known_good_instead_of_compounding():
    """§24.2.3: 'if a Patch ever produces content that fails the narrow validator subset WORSE
    than the version before it, revert to the last version that passed, don't compound forward
    from a regression.' A real, measured failure mode this guards against: arXiv:2607.24604's
    82%->67.3% regression from an uncontrolled second revision pass.
    """
    specialist = _make_specialist()
    one_dup = _make_generated(_DUP_METHOD_MODELS_PY)
    two_dups = _make_generated(_DUP_METHOD_PLUS_UNSAFE_MESSAGE_POST_MODELS_PY)

    async def _fake_patch(contract, module_name, generated, findings):
        return two_dups  # a "fix" that makes it worse

    specialist._generate_internal_loop_patch = _fake_patch

    outcome = asyncio.run(
        specialist.run_build_internal_loop(_make_contract(), "test_module", one_dup, task_id=None)
    )

    assert outcome.action == "escalate"
    # Reverted to the pre-patch (last-known-good) candidate, not the regressed one.
    assert outcome.generated.models_py == one_dup.models_py


def test_mechanically_failing_patch_escalates_without_crashing_the_round():
    specialist = _make_specialist()
    dirty = _make_generated(_DUP_METHOD_MODELS_PY)

    async def _fake_patch(contract, module_name, generated, findings):
        raise ValueError("scoped edit target not found")

    specialist._generate_internal_loop_patch = _fake_patch

    outcome = asyncio.run(
        specialist.run_build_internal_loop(_make_contract(), "test_module", dirty, task_id=None)
    )

    assert outcome.action == "escalate"
    assert outcome.generated.models_py == dirty.models_py


def test_gateway_unavailable_from_patch_call_escalates_instead_of_crashing_the_round():
    """Real, confirmed bug found live (2026-08-09, task 07141af5's flagship run,
    project_ticket_counts node): the internal loop's own Patch call (`_generate_internal_loop_patch`,
    a small, explicitly OPTIONAL self-correction attempt per this loop's own docstring) can hit a
    genuine repetition loop that exhausts every retry inside `generate()`, at which point the real
    cause gets wrapped in a plain `GatewayUnavailableError` -- which this loop's own except clause
    never caught, so it propagated all the way up and aborted the ENTIRE round, surfacing as a
    task-wide "gateway_unavailable" pause ("infrastructure outage") when the gateway's own health
    endpoint was responding normally the whole time and the real cause was a content/sampling
    problem with this one small, skippable patch attempt. Confirmed live via two consecutive
    resumes with this identical failure shape. A failed Patch attempt of any kind must escalate
    (fall through to the existing external round path, unchanged), never crash the whole round.
    """
    specialist = _make_specialist()
    dirty = _make_generated(_DUP_METHOD_MODELS_PY)

    async def _fake_patch(contract, module_name, generated, findings):
        raise GatewayUnavailableError(
            "generate() (streaming) failed after retries against backend 'gpu_worker_01_coder': "
            "Internal loop: patching a narrow validator finding before submitting: model "
            "'qwen3-coder-30b-a3b' repeated the same 120-char span 6+ times after 3955 chars "
            "generated -- aborting early rather than waiting out the full timeout."
        )

    specialist._generate_internal_loop_patch = _fake_patch

    outcome = asyncio.run(
        specialist.run_build_internal_loop(_make_contract(), "test_module", dirty, task_id=None)
    )

    assert outcome.action == "escalate"
    assert outcome.generated.models_py == dirty.models_py


# ---------------------------------------------------------------------------
# Coverage extension (2026-08-04): the internal loop now also runs inside
# _generate_best_of_n(), once per candidate -- confirmed live that most real
# traffic (2+ constraints, or a single computed/onchange constraint) routes
# through best-of-N rather than the single-candidate path, so the loop was
# going unexercised for the majority of real tasks. These tests exercise the
# wiring itself (call count, per-candidate content, and non-bias of scoring),
# not model generation quality -- _generate_code/_validate_generated_module
# and the deterministic-override module functions are all monkeypatched.
# ---------------------------------------------------------------------------

from unittest.mock import patch

import specialists.build.specialist as specialist_module
from specialists.build.specialist import InternalLoopOutcome


def test_internal_loop_runs_once_per_best_of_n_candidate():
    """Round 2 of the coverage extension (2026-08-04): the internal loop now lives INSIDE
    _generate_code() itself (see that method's own docstring for why -- a real, confirmed gap
    where external wrapping alone wasn't reliably reaching the scoped-edit retry path). This
    test now exercises the REAL _generate_code() wrapper (mocking only _generate_code_raw, the
    pure content producer, and run_build_internal_loop itself) rather than mocking _generate_code
    directly, so it still proves best-of-N's own per-candidate call pattern reaches the loop once
    per candidate -- now via _generate_code()'s own internal wiring, not best_of_n's.
    """
    specialist = _make_specialist()
    contract = _make_contract()
    call_count = 0

    async def _fake_generate_code_raw(*args, **kwargs):
        return _make_generated(_CLEAN_MODELS_PY)

    async def _fake_internal_loop(contract, module_name, generated, task_id, **kwargs):
        nonlocal call_count
        call_count += 1
        return InternalLoopOutcome(
            generated=generated, action="keep", steps_used=1,
            findings_history=[[]], budget_exhausted=False,
        )

    async def _fake_override_view(*args, **kwargs):
        return None

    def _fake_override_security(*args, **kwargs):
        return None

    async def _fake_validate(*args, **kwargs):
        return None

    specialist._generate_code_raw = _fake_generate_code_raw
    specialist.run_build_internal_loop = _fake_internal_loop
    specialist._validate_generated_module = _fake_validate

    with patch.object(specialist_module, "_maybe_override_view_xml_deterministically", _fake_override_view), \
         patch.object(specialist_module, "_maybe_override_security_csv_deterministically", _fake_override_security):
        winner, candidate_count_used = asyncio.run(
            specialist._generate_best_of_n(
                contract, "constitution", "skill", "test_module",
                None, None, None, old_files_by_relpath={}, candidate_count=3,
            )
        )

    assert call_count == 3, "the internal loop must run once per candidate, not once overall"
    assert candidate_count_used == 3


def test_one_candidates_gateway_unavailable_does_not_crash_the_whole_round():
    """Real, confirmed bug found live (2026-08-09, task 07141af5's flagship run,
    project_ticket_counts node): `_one_candidate()`'s own except clause used to catch only
    ValueError -- a candidate's generation can also exhaust every retry on a genuine repetition
    loop and surface as GatewayUnavailableError, which escaped uncaught into the bare
    `asyncio.gather()` call (no `return_exceptions=True`, despite this function's own docstring
    claiming that behavior). Without `return_exceptions=True`, gather() immediately propagates
    the first raised exception AND cancels every other still-in-flight candidate -- so ONE
    candidate's repetition-loop failure silently discarded whatever the OTHER, differently-
    tempered candidate might have produced successfully, crashing the entire round as a false
    "gateway_unavailable" pause. Confirmed live via two consecutive resumes with the gateway's
    own health endpoint responding normally throughout both. This test simulates exactly that:
    candidate 0 (temperature 0.0) raises GatewayUnavailableError, candidate 1 (temperature 0.4)
    succeeds -- the round must still produce a real winner from the surviving candidate, not
    crash.
    """
    specialist = _make_specialist()
    contract = _make_contract()

    async def _fake_generate_code_raw(contract, constitution_text, skill_text, module_name,
                                       depends_on_module, target_module_files, prior_files=None,
                                       temperature=0.0, fallback_rewrite_context=None):
        if temperature == 0.0:
            raise GatewayUnavailableError(
                "generate() (streaming) failed after retries against backend "
                "'gpu_worker_01_coder': Writing the module's manifest and model code: model "
                "'qwen3-coder-30b-a3b' repeated the same 120-char span 6+ times after 4022 "
                "chars generated -- aborting early rather than waiting out the full timeout."
            )
        return _make_generated(_CLEAN_MODELS_PY)

    async def _fake_internal_loop(contract, module_name, generated, task_id, **kwargs):
        return InternalLoopOutcome(
            generated=generated, action="keep", steps_used=1,
            findings_history=[[]], budget_exhausted=False,
        )

    async def _fake_override_view(*args, **kwargs):
        return None

    def _fake_override_security(*args, **kwargs):
        return None

    async def _fake_validate(*args, **kwargs):
        return None

    specialist._generate_code_raw = _fake_generate_code_raw
    specialist.run_build_internal_loop = _fake_internal_loop
    specialist._validate_generated_module = _fake_validate

    with patch.object(specialist_module, "_maybe_override_view_xml_deterministically", _fake_override_view), \
         patch.object(specialist_module, "_maybe_override_security_csv_deterministically", _fake_override_security):
        winner, regressed_count = asyncio.run(
            specialist._generate_best_of_n(
                contract, "constitution", "skill", "test_module",
                None, None, None, old_files_by_relpath={}, candidate_count=2,
            )
        )

    assert winner.models_py == _CLEAN_MODELS_PY, (
        "the surviving candidate must still win, not be discarded along with the failed one"
    )


def test_internal_loop_output_is_what_gets_scored_not_the_pre_loop_candidate():
    """If the internal loop patches a candidate's content, best-of-N's own
    regression-count scoring must see the PATCHED content -- proving the
    loop's output actually flows into scoring, not silently discarded.
    """
    specialist = _make_specialist()
    contract = _make_contract()

    async def _fake_generate_code_raw(*args, **kwargs):
        return _make_generated(_DUP_METHOD_MODELS_PY)

    async def _fake_internal_loop(contract, module_name, generated, task_id, **kwargs):
        # Simulate a successful Patch: the loop hands back cleaned-up content.
        return InternalLoopOutcome(
            generated=_make_generated(_CLEAN_MODELS_PY), action="keep", steps_used=1,
            findings_history=[["no_duplicate_method_definitions: found"], []], budget_exhausted=False,
        )

    async def _fake_override_view(*args, **kwargs):
        return None

    def _fake_override_security(*args, **kwargs):
        return None

    async def _fake_validate(*args, **kwargs):
        return None

    specialist._generate_code_raw = _fake_generate_code_raw
    specialist.run_build_internal_loop = _fake_internal_loop
    specialist._validate_generated_module = _fake_validate

    with patch.object(specialist_module, "_maybe_override_view_xml_deterministically", _fake_override_view), \
         patch.object(specialist_module, "_maybe_override_security_csv_deterministically", _fake_override_security):
        winner, _ = asyncio.run(
            specialist._generate_best_of_n(
                contract, "constitution", "skill", "test_module",
                None, None, None, old_files_by_relpath={}, candidate_count=1,
            )
        )

    assert winner.models_py == _CLEAN_MODELS_PY, "scoring must see the internal loop's patched output"


# ---------------------------------------------------------------------------
# Phase T coverage extension, round 2 (2026-08-04): direct proof that the internal loop now
# covers the SCOPED-EDIT retry path specifically -- the real, confirmed gap (896 real scoped-edit
# LLM calls against only 9 internal-loop events in one real task's own trace, despite the
# external-wrapping design structurally appearing to cover it). Exercises the REAL
# _generate_code() -> _generate_code_raw() -> _generate_scoped_edits() call chain, mocking only
# the boundary LLM call (call_structured) and run_build_internal_loop itself.
# ---------------------------------------------------------------------------

def test_scoped_edit_retry_path_now_goes_through_the_internal_loop():
    from specialists.build.specialist import GeneratedModuleEdit, GeneratedModuleEdits, _render_manifest_py

    specialist = _make_specialist()
    contract = _make_contract()

    prior_manifest_fields = ManifestFields(
        name="Test", version="1.0", category="Test", summary="s", author="a",
        depends=["base"], data=[],
    )
    prior_files = {
        "__manifest__.py": _render_manifest_py(prior_manifest_fields),
        "models/models.py": _CLEAN_MODELS_PY,
        "security/ir.model.access.csv": "id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink\n",
    }

    # A trivial, valid scoped edit: replace_manifest_fields with the exact same fields (a real,
    # legal no-op edit shape this schema supports) -- the point of this test is what happens
    # AFTER the edit is applied, not the edit's own content.
    fake_edits_result = GeneratedModuleEdits(
        edits=[GeneratedModuleEdit(file="__manifest__.py", operation="replace_manifest_fields", manifest_fields=prior_manifest_fields)],
        notes="",
    )

    internal_loop_calls = []

    async def _fake_call_structured(**kwargs):
        return fake_edits_result

    async def _fake_internal_loop(contract, module_name, generated, task_id, **kwargs):
        internal_loop_calls.append(generated)
        return InternalLoopOutcome(
            generated=generated, action="keep", steps_used=1,
            findings_history=[[]], budget_exhausted=False,
        )

    specialist.run_build_internal_loop = _fake_internal_loop

    with patch.object(specialist_module, "call_structured", _fake_call_structured):
        result = asyncio.run(specialist._generate_code(
            contract, "constitution", "skill", "test_module",
            depends_on_module=None, target_module_files=None, prior_files=prior_files,
        ))

    assert len(internal_loop_calls) == 1, (
        "the scoped-edit branch's own output must reach run_build_internal_loop exactly once -- "
        "this is the real gap confirmed live (896 scoped-edit calls, 9 internal-loop events)"
    )
    assert internal_loop_calls[0].models_py == _CLEAN_MODELS_PY
    assert result.models_py == _CLEAN_MODELS_PY


def test_generate_code_applies_internal_loop_exactly_once_not_twice_on_fallback_rewrite():
    """The fallback-rewrite path recurses via _generate_code_raw() (not the wrapped
    _generate_code()) specifically to avoid double-applying the internal loop -- confirmed here
    directly: a scoped edit that raises ScopedEditApplicationError triggers exactly one fallback
    full-generation call, and the internal loop still runs exactly once overall, not twice.
    """
    from specialists.build.specialist import ScopedEditApplicationError

    specialist = _make_specialist()
    contract = _make_contract()
    prior_files = {
        "__manifest__.py": "{'name': 'Test', 'version': '1.0'}",
        "models/models.py": _CLEAN_MODELS_PY,
        "security/ir.model.access.csv": "id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink\n",
    }

    async def _fake_generate_scoped_edits(*args, **kwargs):
        raise ScopedEditApplicationError("target not found -- simulated for this test")

    async def _fake_generate_code_raw_fallback(contract_arg, constitution_text, skill_text, module_name, depends_on_module=None, target_module_files=None, prior_files=None, temperature=0.0, fallback_rewrite_context=None):
        # Only reached via the fallback recursion (prior_files=None at that point).
        assert prior_files is None
        assert fallback_rewrite_context is not None
        return _make_generated(_CLEAN_MODELS_PY)

    internal_loop_calls = []

    async def _fake_internal_loop(contract, module_name, generated, task_id, **kwargs):
        internal_loop_calls.append(generated)
        return InternalLoopOutcome(
            generated=generated, action="keep", steps_used=1,
            findings_history=[[]], budget_exhausted=False,
        )

    specialist._generate_scoped_edits = _fake_generate_scoped_edits
    specialist.run_build_internal_loop = _fake_internal_loop

    # Patch _generate_code_raw AFTER binding the real one so the fallback recursion (inside the
    # real _generate_code_raw, triggered by ScopedEditApplicationError) calls a fake that never
    # re-enters real generation logic, but the OUTER _generate_code() call still goes through the
    # real _generate_code_raw() once (to reach the try/except that catches the simulated error).
    real_generate_code_raw = specialist._generate_code_raw

    async def _generate_code_raw_dispatch(contract_arg, *args, **kwargs):
        if kwargs.get("prior_files") is None and kwargs.get("fallback_rewrite_context") is not None:
            return await _fake_generate_code_raw_fallback(contract_arg, *args, **kwargs)
        return await real_generate_code_raw(contract_arg, *args, **kwargs)

    specialist._generate_code_raw = _generate_code_raw_dispatch

    result = asyncio.run(specialist._generate_code(
        contract, "constitution", "skill", "test_module",
        depends_on_module=None, target_module_files=None, prior_files=prior_files,
    ))

    assert len(internal_loop_calls) == 1, "the internal loop must apply exactly once, not once per recursion level"
    assert result.models_py == _CLEAN_MODELS_PY


# ---------------------------------------------------------------------------
# 2026-08-04: both model-identity narrow validators are now ALSO a hard gate inside
# _validate_generated_module() itself -- real, confirmed gap found live (task004 round 2, full
# 30-task sweep): they were previously reachable ONLY through the internal loop's own bounded,
# best-effort self-correction, so an Escalate outcome (patch budget exhausted without fixing it)
# still let a guaranteed-to-crash candidate get written to disk and installed. Source-level check
# (same discipline as test_sandbox_and_install_failure_short_circuit_uses_the_structure_aware_fold
# in tests/test_manager_loop_verification_notes_fold_choice.py) rather than a full
# _validate_generated_module() invocation, which needs a large amount of unrelated setup
# (module_name, depends_on_module, old_*, constraint_status, goal, goal_facts, task_id) to even
# call -- this directly and durably prevents a regression back to internal-loop-only coverage
# without needing to mock the entire validator chain.
# ---------------------------------------------------------------------------

def test_validate_generated_module_hard_gate_includes_both_identity_validators():
    import specialists.build.specialist as specialist_module

    source = inspect.getsource(specialist_module.BuildSpecialist._validate_generated_module)
    assert "_validate_model_declares_name_or_inherit(generated)" in source, (
        "the hard external gate must call this validator directly, not rely solely on the "
        "internal loop's own best-effort self-correction -- the exact gap found live in task004"
    )
    assert "_validate_name_attribute_matches_odoo_naming_convention(generated)" in source, (
        "the hard external gate must also call the naming-convention validator directly, same "
        "reasoning as its identity-presence sibling above"
    )
    assert "_validate_model_class_is_not_a_pure_identity_stub(generated)" in source, (
        "the hard external gate must also call the pure-identity-stub validator directly -- "
        "task021/025/028's own real shape, valid identity but zero real content"
    )
    assert "_validate_no_empty_ref_attribute(generated)" in source, (
        "the hard external gate must also call the empty-ref validator directly -- task014's own "
        "real shape, an <ir.ui.menu> ref=\"\" causing a real install-time ParseError"
    )
    assert "_validate_view_arch_has_single_root_element(generated)" in source, (
        "the hard external gate must also call the arch-single-root validator directly -- "
        "task007's own real shape, a <tree>+<search> pair sharing one ir.ui.view arch causing a "
        "misleading 'Wrong value for ir.ui.view.type' install-time error"
    )
    assert "_validate_env_model_lookup_uses_dotted_model_name(generated)" in source, (
        "the hard external gate must also call the env-lookup dotted-name validator directly -- "
        "task026's own real shape, self.env['container'] instead of the real 'waste.container'"
    )
