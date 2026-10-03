"""Phase 30 §26 (Phase V -- Build Prompt-Assembly Correctness), docs/planning/
PHASE30_OMA_SEAM_COVERAGE_AND_COMBINATORIAL_HARDENING_2026-07-29.md -- regression tests for
items 1-5: live schema injection (curated, never a full dump), verbatim prior-round failure
feedback, and prompt section ordering/delimiting.

No real network/DB calls -- tools_odoo.odoo_schema_client's boundary functions
(_read_real_field_rows / get_relation_fields_fast / is_fast_path_eligible) are patched with
realistic fixture data matching real Odoo ir.model.fields row shape, same "mock exactly the
external boundary" discipline as tests/test_oma_debris_sweep.py. call_structured (the real LLM
call) is patched to capture the assembled prompt without making a network call, matching
tests/test_build_internal_loop.py's own convention for exercising BuildSpecialist methods.
"""
import asyncio
import uuid
from unittest.mock import patch

import specialists.build.specialist as specialist_module
import tools_odoo.odoo_schema_client as schema_client_module
from contracts.schema import AutonomyTier, CapabilityClass, SpecialistType, TaskContract
from specialists.build.specialist import (
    BuildSpecialist,
    resolve_current_schema_block,
    resolve_current_view_arch_block,
    resolve_current_views_block,
)

_DB = "odoo16_dev"  # the one real fast-path-eligible db this codebase's own allow-list names

_FIELDJOB_FIELD_ROWS = [
    {"name": "name", "ttype": "char", "relation": None, "required": True},
    {"name": "project_id", "ttype": "many2one", "relation": "project.project", "required": True},
    {"name": "state", "ttype": "selection", "relation": None, "required": False},
]
_PROJECT_FIELD_ROWS = [
    {"name": "name", "ttype": "char", "relation": None, "required": True},
    {"name": "partner_id", "ttype": "many2one", "relation": "res.partner", "required": False},
]


def _make_contract(**overrides) -> TaskContract:
    base = dict(
        task_id=uuid.uuid4(),
        specialist_type=SpecialistType.bug_fix,
        capability_class=CapabilityClass.module_development,
        tier=AutonomyTier.tier_2_notify_after,
        goal="On the project.fieldjob record, add a field for the total.",
        inputs=["some input"],
        rules=[],
        deliverables=["a module"],
        compensating_actions=[],
        validation_by="testing_qa",
        pause_if=[],
        turn_budget=15,
    )
    base.update(overrides)
    return TaskContract(**base)


def _make_specialist() -> BuildSpecialist:
    return BuildSpecialist(client=None, db=_DB)


# ---------------------------------------------------------------------------
# resolve_current_schema_block() -- items 1/2, unit level
# ---------------------------------------------------------------------------

def test_schema_block_includes_target_model_fields():
    contract = _make_contract(module_identity="project.fieldjob")
    with patch.object(schema_client_module, "is_fast_path_eligible", return_value=True), \
         patch.object(schema_client_module, "_read_real_field_rows", return_value=_FIELDJOB_FIELD_ROWS), \
         patch.object(schema_client_module, "get_relation_fields_fast", return_value={}):
        block = resolve_current_schema_block(contract, _DB)
    assert "project.fieldjob:" in block
    assert "project_id: many2one -> project.project, required" in block
    assert "state: selection" in block


def test_schema_block_curated_only_includes_related_model_named_in_goal():
    """§26.6's own most important non-goal: never a full ORM dump. A relation field's target
    model must appear ONLY if the goal text itself names it -- not unconditionally for every
    relation the target model happens to have."""
    contract = _make_contract(
        module_identity="project.fieldjob",
        goal="On the project.fieldjob record, add a field referencing project.project.",
    )
    with patch.object(schema_client_module, "is_fast_path_eligible", return_value=True), \
         patch.object(schema_client_module, "_read_real_field_rows", side_effect=lambda model, db, login: {
             "project.fieldjob": _FIELDJOB_FIELD_ROWS, "res.partner": _PROJECT_FIELD_ROWS,
         }.get(model)), \
         patch.object(schema_client_module, "get_relation_fields_fast", return_value={
             "project_id": "project.project", "partner_id": "res.partner",
         }):
        block = resolve_current_schema_block(contract, _DB)
    # project.project IS named in the goal text -- but no fixture was given for it (returns
    # None), so it must be silently skipped, never crash and never appear as a phantom block.
    assert "res.partner:" not in block, "res.partner was never named in the goal -- must not appear"


def test_schema_block_empty_when_module_identity_unresolved():
    contract = _make_contract(module_identity=None)
    block = resolve_current_schema_block(contract, _DB)
    assert block == ""


def test_schema_block_empty_when_model_does_not_exist_live():
    contract = _make_contract(module_identity="project.nonexistent_model_xyz")
    with patch.object(schema_client_module, "is_fast_path_eligible", return_value=True), \
         patch.object(schema_client_module, "_read_real_field_rows", return_value=None):
        block = resolve_current_schema_block(contract, _DB)
    assert block == ""


def test_schema_block_empty_when_db_not_fast_path_eligible():
    contract = _make_contract(module_identity="project.fieldjob")
    block = resolve_current_schema_block(contract, "some_fresh_sandbox_db")
    assert block == ""


# ---------------------------------------------------------------------------
# resolve_current_views_block() -- task038 fix-pass finding, unit level
# ---------------------------------------------------------------------------

def test_views_block_lists_real_views_for_target_model():
    contract = _make_contract(module_identity="sale.room")
    with patch.object(schema_client_module, "is_fast_path_eligible", return_value=True), \
         patch.object(schema_client_module, "list_model_view_xmlids_fast", return_value=[
             ("sale_room_management.view_sale_room_form", "form"),
             ("sale_room_management.view_sale_room_new_form", "form"),
             ("sale_room_management.view_sale_room_tree", "tree"),
         ]):
        block = resolve_current_views_block(contract, _DB)
    assert "sale_room_management.view_sale_room_form (form)" in block
    assert "sale_room_management.view_sale_room_tree (tree)" in block
    assert "never invent a plausible-sounding id" in block


def test_views_block_empty_when_module_identity_unresolved():
    contract = _make_contract(module_identity=None)
    block = resolve_current_views_block(contract, _DB)
    assert block == ""


def test_views_block_empty_when_model_has_no_real_views():
    contract = _make_contract(module_identity="project.nonexistent_model_xyz")
    with patch.object(schema_client_module, "is_fast_path_eligible", return_value=True), \
         patch.object(schema_client_module, "list_model_view_xmlids_fast", return_value=None):
        block = resolve_current_views_block(contract, _DB)
    assert block == ""


def test_views_block_empty_when_db_not_fast_path_eligible():
    contract = _make_contract(module_identity="sale.room")
    block = resolve_current_views_block(contract, "some_fresh_sandbox_db")
    assert block == ""


# ---------------------------------------------------------------------------
# resolve_current_view_arch_block() -- 2026-08-17 overnight workflow_with_custom_
# buttons_or_cron root-cause finding, unit level.
# ---------------------------------------------------------------------------

_PARTNER_FORM_ARCH_NO_HEADER = (
    "<form><sheet><group><field name='name'/><field name='email'/></group></sheet></form>"
)
_PARTNER_FORM_ARCH_WITH_HEADER_AND_BUTTONS = (
    "<form><header>"
    "<button name='action_confirm' string='Confirm' type='object'/>"
    "<button name='action_cancel' string='Cancel' type='object'/>"
    "</header><sheet><group><field name='name'/></group></sheet></form>"
)


def test_view_arch_block_lists_header_and_existing_buttons():
    contract = _make_contract(module_identity="res.partner")
    with patch.object(schema_client_module, "is_fast_path_eligible", return_value=True), \
         patch.object(
             schema_client_module, "get_model_form_view_arch_fast",
             return_value=_PARTNER_FORM_ARCH_WITH_HEADER_AND_BUTTONS,
         ):
        block = resolve_current_view_arch_block(contract, _DB)
    assert "<header> element: present" in block
    assert "'action_confirm'" in block and "string='Confirm'" in block
    assert "'action_cancel'" in block and "string='Cancel'" in block


def test_view_arch_block_reports_absent_header_so_build_never_xpaths_into_it():
    """The exact real failure shape this fix closes: a goal-driven xpath into `//header` on a
    parent view that genuinely has none (confirmed live, 2026-08-17,
    hr_recruitment.hr_applicant_view_form)."""
    contract = _make_contract(module_identity="res.partner")
    with patch.object(schema_client_module, "is_fast_path_eligible", return_value=True), \
         patch.object(
             schema_client_module, "get_model_form_view_arch_fast",
             return_value=_PARTNER_FORM_ARCH_NO_HEADER,
         ):
        block = resolve_current_view_arch_block(contract, _DB)
    assert "ABSENT -- do not xpath into //header, it does not exist" in block
    assert "existing <button> elements already in this view: none" in block


def test_view_arch_block_empty_when_module_identity_unresolved():
    contract = _make_contract(module_identity=None)
    block = resolve_current_view_arch_block(contract, _DB)
    assert block == ""


def test_view_arch_block_empty_when_no_arch_could_be_fetched():
    contract = _make_contract(module_identity="project.nonexistent_model_xyz")
    with patch.object(schema_client_module, "is_fast_path_eligible", return_value=True), \
         patch.object(schema_client_module, "get_model_form_view_arch_fast", return_value=None):
        block = resolve_current_view_arch_block(contract, _DB)
    assert block == ""


def test_view_arch_block_empty_when_db_not_fast_path_eligible():
    contract = _make_contract(module_identity="res.partner")
    block = resolve_current_view_arch_block(contract, "some_fresh_sandbox_db")
    assert block == ""


# ---------------------------------------------------------------------------
# _generate_code()'s full prompt assembly -- items 1/3/4/5, integration level.
# call_structured is patched to capture the real prompt without a network call.
# ---------------------------------------------------------------------------

def _capture_prompt(specialist, contract, **kwargs):
    captured = {}

    async def _fake_call_structured(**call_kwargs):
        captured["prompt"] = call_kwargs["prompt"]
        from specialists.build.specialist import GeneratedModuleFiles, ManifestFields
        return GeneratedModuleFiles(
            manifest_fields=ManifestFields(name="t", version="1.0", category="t", summary="t", author="t", depends=["base"], data=[]),
            models_py="class X(models.Model):\n    _name = 'x.x'\n    total = fields.Float()\n",
            security_csv="id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink\n",
            notes="",
        )

    with patch.object(specialist_module, "is_fast_path_eligible", return_value=True), \
         patch.object(schema_client_module, "is_fast_path_eligible", return_value=True), \
         patch.object(schema_client_module, "_read_real_field_rows", return_value=_FIELDJOB_FIELD_ROWS), \
         patch.object(schema_client_module, "get_relation_fields_fast", return_value={}), \
         patch.object(specialist_module, "call_structured", _fake_call_structured):
        asyncio.run(specialist._generate_code(
            contract, "CONSTITUTION_TEXT", "SKILL_TEXT", "test_module", **kwargs,
        ))
    return captured["prompt"]


def test_schema_block_injected_without_depends_on_module():
    """§26.7 DoD (a): a task extending an existing model without depends_on_module set now
    receives that model's real current field list in the prompt -- §26.1's own confirmed gap."""
    specialist = _make_specialist()
    contract = _make_contract(module_identity="project.fieldjob")
    prompt = _capture_prompt(specialist, contract, depends_on_module=None, target_module_files=None)
    assert "<current_schema>" in prompt
    assert "project.fieldjob:" in prompt
    assert "project_id: many2one -> project.project, required" in prompt


def test_views_block_injected_alongside_schema_block_in_full_prompt():
    """task038 fix-pass finding: the new <current_views> block must actually reach the
    assembled prompt via the real _generate_code() path, using the same _capture_prompt
    integration harness every sibling injection test in this file already uses."""
    specialist = _make_specialist()
    contract = _make_contract(module_identity="project.fieldjob")
    with patch.object(schema_client_module, "list_model_view_xmlids_fast", return_value=[
        ("project_fieldjob.view_project_fieldjob_form", "form"),
    ]):
        prompt = _capture_prompt(specialist, contract, depends_on_module=None, target_module_files=None)
    assert "<current_views>" in prompt
    assert "project_fieldjob.view_project_fieldjob_form (form)" in prompt
    assert "never invent a plausible-sounding id" in prompt


def test_view_arch_block_injected_alongside_views_block_in_full_prompt():
    """2026-08-17 root-cause finding: the new <current_view_arch> block must actually reach the
    assembled prompt via the real _generate_code() path, using the same _capture_prompt
    integration harness every sibling injection test in this file already uses."""
    specialist = _make_specialist()
    contract = _make_contract(module_identity="project.fieldjob")
    with patch.object(schema_client_module, "list_model_view_xmlids_fast", return_value=[
        ("project_fieldjob.view_project_fieldjob_form", "form"),
    ]), patch.object(
        schema_client_module, "get_model_form_view_arch_fast",
        return_value=_PARTNER_FORM_ARCH_WITH_HEADER_AND_BUTTONS,
    ):
        prompt = _capture_prompt(specialist, contract, depends_on_module=None, target_module_files=None)
    assert "<current_view_arch>" in prompt
    assert "<header> element: present" in prompt
    assert "'action_confirm'" in prompt


def test_previous_round_raw_failure_text_appears_verbatim():
    """§26.7 DoD (b): a failing round's exact validator/traceback text appears byte-for-byte in
    the next round's prompt, not a paraphrase."""
    specialist = _make_specialist()
    raw_text = (
        "Sandbox install failed: ValueError: The _name attribute ProjectFieldjob is not valid.\n"
        "  File \"models.py\", line 3, in <module>\n    raise ValueError(...)"
    )
    contract = _make_contract(previous_round_raw_failure_text=raw_text)
    prompt = _capture_prompt(specialist, contract, depends_on_module=None, target_module_files=None)
    assert raw_text in prompt, "the raw failure text must appear byte-for-byte, unparaphrased"
    assert "<previous_attempt_errors>" in prompt


def test_previous_attempt_errors_block_absent_on_round_one():
    specialist = _make_specialist()
    contract = _make_contract(previous_round_raw_failure_text=None)
    prompt = _capture_prompt(specialist, contract, depends_on_module=None, target_module_files=None)
    assert "<previous_attempt_errors>" not in prompt


def test_prompt_section_order_matches_item4():
    """§26.7 DoD (c): the prompt's rendered section order matches item 4's specified shape --
    stable constitution/skill first, schema excerpt, capped rules history, previous-attempt-
    errors, task/goal, output-format contract last."""
    specialist = _make_specialist()
    contract = _make_contract(
        module_identity="project.fieldjob",
        rules=["some prior rule"],
        previous_round_raw_failure_text="literal prior failure text",
    )
    prompt = _capture_prompt(specialist, contract, depends_on_module=None, target_module_files=None)

    idx_constitution = prompt.index("CONSTITUTION_TEXT")
    idx_skill = prompt.index("SKILL_TEXT")
    idx_schema = prompt.index("<current_schema>")
    idx_rules = prompt.index("Rules:")
    idx_prev_errors = prompt.index("<previous_attempt_errors>")
    idx_task = prompt.index("<task>")
    idx_output_contract = prompt.index("<output_contract>")

    assert idx_constitution < idx_skill < idx_schema < idx_rules < idx_prev_errors < idx_task < idx_output_contract, (
        "prompt sections must appear in item 4's specified order: stable content first, "
        "volatile goal/output-contract last"
    )


def test_output_contract_tag_closed_at_true_end_of_prompt():
    specialist = _make_specialist()
    contract = _make_contract(module_identity="project.fieldjob")
    prompt = _capture_prompt(specialist, contract, depends_on_module=None, target_module_files=None)
    assert prompt.rstrip().endswith("</output_contract>")
