"""Phase 30 business-logic reasoning gate (docs/planning/
PHASE30_CONTENT_QUALITY_REASONING_ANALYSIS_2026-08-06.md +
PHASE30_REASONING_CORRECTNESS_RESEARCH_2026-08-06.md): unit + prompt-assembly regression tests
for the multi-piece complexity gate and its shape-matched worked examples.

Real bug this targets: task 004 (2026-08-06 fix-pass session) -- goal "total of all line
prices shown automatically in the header" produced a view field ('amount_total') that was never
defined as a model field, identically across 2 rounds (round 1 full generation, round 2 scoped-
edit retry) -- the UI half of the feature was written, the compute half wasn't. Confirmed via
direct redis trace inspection: Build's own generated models.py for round 1 defined ONLY
`_inherit = 'project.fieldjob'` with no field at all in one variant, and in another live capture
defined the field correctly but Code-Review still hallucinated it as missing -- the reasoning-
analysis doc's own §1 category A ("UI/scaffold written, backend logic never written") names this
exact shape as the single most common real failure across the 36-task fail set.

Same test style as tests/test_build_prompt_assembly.py (call_structured patched to capture the
assembled prompt without a network call).
"""
import asyncio
import uuid
from unittest.mock import patch

import specialists.build.specialist as specialist_module
import tools_odoo.odoo_schema_client as schema_client_module
from contracts.schema import AutonomyTier, CapabilityClass, SpecialistType, TaskContract
from specialists.build.specialist import (
    BuildSpecialist,
    _detect_business_logic_shape,
    _is_multi_piece_business_logic_goal,
    _multi_piece_business_logic_prompt_block,
)

_DB = "odoo16_dev"


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
# _detect_business_logic_shape() / _is_multi_piece_business_logic_goal() -- unit level
# ---------------------------------------------------------------------------

def test_detects_computed_field_displayed_in_view_shape():
    goal = (
        "On the fieldjob record, I want to see the total of all line prices shown "
        "automatically in the header. It should update whenever I add or change a line."
    )
    shape = _detect_business_logic_shape(goal)
    assert shape is not None
    assert shape[0] == "computed field displayed in a view"
    print("PASS: task 004's own real goal text is detected as the computed-field shape")


def test_detects_smart_button_shape():
    goal = "Add a smart button to the fieldjob record showing the count of related tasks."
    shape = _detect_business_logic_shape(goal)
    assert shape is not None and shape[0] == "smart button with a live count"
    print("PASS: a smart-button goal is detected")


def test_detects_state_transition_trigger_shape():
    goal = "After the invoice is sent to the customer, the record should automatically change status to 'sent'."
    shape = _detect_business_logic_shape(goal)
    assert shape is not None and shape[0] == "state transition triggered by an action"
    print("PASS: a state-transition-trigger goal is detected")


def test_detects_onchange_autofill_shape():
    goal = "When the customer is selected on the order, auto-fill the delivery address from the customer's default address."
    shape = _detect_business_logic_shape(goal)
    assert shape is not None and shape[0] == "onchange auto-fill from a related field"
    print("PASS: an onchange/auto-fill goal is detected")


def test_detects_invoice_line_construction_shape():
    goal = "Batch create invoice lines for all approved fieldjob records, generating the correct invoice lines with tax and account."
    shape = _detect_business_logic_shape(goal)
    assert shape is not None and shape[0] == "invoice line construction with tax/account"
    print("PASS: an invoice-line-construction goal is detected")


def test_detects_state_transition_shape_with_accepted_wording():
    """Real bug this fixes (2026-08-06, fix-pass task 020): task 020's own real goal ("When a
    fieldjob is marked as accepted, automatically send a confirmation email...") went completely
    undetected -- the original trigger-word list only covered sent/approved/confirmed/paid/
    completed/submitted, missing "accepted"/"marked as X" phrasing entirely.
    """
    goal = (
        "When a fieldjob is marked as accepted, automatically send a confirmation email to the "
        "customer in their own language. Include the fieldjob reference, total amount, and "
        "expected finish date."
    )
    shape = _detect_business_logic_shape(goal)
    assert shape is not None
    assert shape[0] == "state transition triggered by an action"
    print("PASS: task 020's own real 'marked as accepted' phrasing is detected as the "
          "state-transition-trigger shape")


def test_detects_migration_script_must_actually_run_shape():
    """Real bug this locks in: task 032 (2026-08-06 fix-pass session) -- goal "Write a Python
    migration script to populate a new field on existing records" took 3 full rounds and still
    escalated, each round fixing the PREVIOUS round's own named issue while a different one of
    3 simultaneously-required pieces (real logic; bounded batching; actual invocation) stayed
    broken -- the reasoning-analysis doc's own clearest documented "whack-a-mole" case.
    """
    goal = "Write a Python migration script to populate a new field on existing records"
    shape = _detect_business_logic_shape(goal)
    assert shape is not None
    assert shape[0] == "one-time migration script that must actually run"
    assert "migrate(cr, version)" in shape[1]
    print("PASS: task 032's own real goal text is detected as the migration-script shape")


def test_smart_button_worked_example_never_names_a_real_live_model():
    """Real, confirmed regression found live (2026-08-06, fix-pass task 026): the original smart-
    button worked example illustrated the shape using 'project.fieldjob' -- a REAL, live model in
    this deployment (also the real target of tasks 004/009/etc.) -- and Build, given task 026's
    own genuinely different goal ('Container count' smart button on project.project, counting
    waste.container records), copied the example nearly verbatim: wrong model (_inherit =
    'project.fieldjob' instead of 'project.project'), wrong relation field, wrong domain,
    wrong res_model ('project.fieldjob.line' instead of 'waste.container'). Fixed by using a
    clearly fictional example domain ('library.loan') that can never be mistaken for a real
    target in any deployment, plus an explicit warning against literal reuse.
    """
    from specialists.build.specialist import _detect_business_logic_shape
    shape = _detect_business_logic_shape(
        "I want to add a 'Container count' smart button to the project form, just like the "
        "fieldjob button. Show the number of containers and clicking it opens the container "
        "list for that project."
    )
    assert shape is not None
    assert shape[0] == "smart button with a live count"
    assert "project.fieldjob" not in shape[1], (
        "the smart-button worked example must never name a real, live model in this "
        "deployment -- it invites literal copying onto unrelated tasks"
    )
    assert "library.loan" in shape[1]
    assert "FICTIONAL EXAMPLE DOMAIN" in shape[1]
    print("PASS: the smart-button worked example uses a clearly fictional domain, never a real "
          "live model that could be mistaken for the actual target")


def test_no_worked_example_names_a_real_live_deployment_model():
    """General regression guard: none of the 6 worked examples may reference 'project.fieldjob'
    (or any other real, live model in this specific deployment) as their illustrative model --
    only genuinely generic, standard Odoo models (res.partner, account.move.line) or the
    deliberately fictional 'library.loan' placeholder are safe to use as example domains.
    """
    from specialists.build.specialist import _BUSINESS_LOGIC_SHAPE_EXAMPLES
    for _pattern, name, example in _BUSINESS_LOGIC_SHAPE_EXAMPLES:
        assert "project.fieldjob" not in example, (
            f"worked example {name!r} names a real, live deployment model -- must use a "
            f"fictional or genuinely generic standard-Odoo placeholder instead"
        )
    print("PASS: no worked example names a real, live deployment model")


def test_detects_smart_button_goal_phrased_as_how_many_instead_of_count():
    """Real bug this fixes (2026-08-06, fix-pass task 009): task 009's own real goal ("a button
    in the top-right corner that shows how many fieldjob records exist for this project") is
    exactly the reasoning-analysis doc's own category-A smart-button example, but the original
    regex required the literal word "count" near "button" -- "how many ... exist" never matched
    at all, so this shape went completely undetected for its own real, natural phrasing.
    """
    goal = (
        "On the project form, I want to see a button in the top-right corner that shows how "
        "many fieldjob (extra work) records exist for this project. Clicking it should open "
        "that list filtered to this project."
    )
    shape = _detect_business_logic_shape(goal)
    assert shape is not None
    assert shape[0] == "smart button with a live count"
    print("PASS: task 009's own real 'how many ... exist' phrasing is detected as the "
          "smart-button shape")


def test_onchange_goal_with_form_and_automatically_does_not_false_positive_as_computed_field():
    """Real bug this fixes (2026-08-06, fix-pass task 005): task 005's real goal ("When I select
    a project on the fieldjob form, I want the 'Assigned to' field to automatically fill with the
    project manager") has zero total/sum/computation semantics, but the ORIGINAL computed-field
    regex's right-hand alternative listed bare "automatically" as a standalone trigger alongside
    "total/sum/calculate/compute" -- so "form"..."automatically" alone matched it, shadowing the
    correct, more specific onchange-autofill shape and showing the wrong worked example (a
    Monetary compute method) for a task that actually needs @api.onchange.
    """
    goal = (
        "When I select a project on the fieldjob form, I want the 'Assigned to' field to "
        "automatically fill with the project manager. I can still change it manually after."
    )
    shape = _detect_business_logic_shape(goal)
    assert shape is not None
    assert shape[0] == "onchange auto-fill from a related field", (
        f"expected the onchange shape, got {shape[0]!r} -- the computed-field regex is "
        f"over-matching on 'automatically' alone again"
    )
    print("PASS: task 005's own real goal text correctly matches the onchange shape, not the "
          "computed-field shape it used to false-positive on")


def test_single_piece_goal_does_not_match_any_shape():
    """The gate's whole point is to stay OFF for simple, single-piece tasks -- per the research
    doc's own confirmed finding that blanket CoT/scaffolding hurts simple tasks.
    """
    goal = "Add a single new field 'preferred_language' (Char) to res.partner."
    assert _detect_business_logic_shape(goal) is None
    contract = _make_contract(goal=goal, goal_facts={})
    assert _is_multi_piece_business_logic_goal(contract) is False
    print("PASS: a plain single-field task matches no shape and the gate stays off")


def test_goal_facts_is_computed_alone_triggers_the_gate_even_without_a_textual_shape_match():
    """The upstream goal_facts extraction (contracts/goal_facts.py) is a second, independent
    signal for computed-field goals -- must trigger the gate even when the goal's own free text
    happens not to match the regex (e.g. an unusual phrasing the regex doesn't cover).
    """
    contract = _make_contract(
        goal="Track a running balance on the wallet record.",
        goal_facts={"is_computed": True},
    )
    assert _is_multi_piece_business_logic_goal(contract) is True
    print("PASS: goal_facts.is_computed alone is sufficient to trigger the gate")


def test_multi_piece_prompt_block_includes_the_matched_shape_example_only():
    contract = _make_contract(
        goal="Add a smart button to the fieldjob record showing the count of related tasks.",
        goal_facts={},
    )
    block = _multi_piece_business_logic_prompt_block(contract)
    assert "smart button with a live count" in block
    assert "state transition triggered by an action" not in block
    assert "onchange auto-fill" not in block
    print("PASS: the prompt block includes only the ONE matched shape's example, not the others")


def test_multi_piece_prompt_block_empty_for_non_qualifying_goal():
    contract = _make_contract(
        goal="Add a single new field 'preferred_language' (Char) to res.partner.",
        goal_facts={},
    )
    assert _multi_piece_business_logic_prompt_block(contract) == ""
    print("PASS: the prompt block is empty for a goal that doesn't qualify")


def test_is_computed_only_case_still_gets_the_general_instruction_with_no_example():
    """goal_facts.is_computed=True with no textual shape match: the gate fires (per the test
    above), and the general decomposition instruction must still be injected even though no
    specific worked example applies -- never silently empty just because no shape regex matched.
    """
    contract = _make_contract(
        goal="Track a running balance on the wallet record.",
        goal_facts={"is_computed": True},
    )
    block = _multi_piece_business_logic_prompt_block(contract)
    assert block != ""
    assert "state each" not in block  # sanity: not asserting exact wording, just non-empty
    assert "dependency order" in block
    print("PASS: the general decomposition instruction still fires on goal_facts.is_computed alone")


# ---------------------------------------------------------------------------
# Prompt-assembly integration -- both the from-scratch and scoped-edit (retry) paths.
# call_structured is patched to capture the real prompt, matching
# tests/test_build_prompt_assembly.py's own convention.
# ---------------------------------------------------------------------------

def _capture_generate_code_raw_prompt(specialist, contract, **kwargs):
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
         patch.object(schema_client_module, "_read_real_field_rows", return_value=[]), \
         patch.object(schema_client_module, "get_relation_fields_fast", return_value={}), \
         patch.object(specialist_module, "call_structured", _fake_call_structured):
        asyncio.run(specialist._generate_code(
            contract, "CONSTITUTION_TEXT", "SKILL_TEXT", "test_module", **kwargs,
        ))
    return captured["prompt"]


def test_generate_code_raw_injects_shape_example_for_task004_shaped_goal():
    specialist = _make_specialist()
    contract = _make_contract(
        goal=(
            "On the fieldjob record, I want to see the total of all line prices shown "
            "automatically in the header. It should update whenever I add or change a line."
        ),
        module_identity="project.fieldjob",
    )
    prompt = _capture_generate_code_raw_prompt(specialist, contract, depends_on_module=None, target_module_files=None)
    assert "coordinated pieces to work together" in prompt
    assert "computed field displayed in a view" in prompt
    assert "dependency order" in prompt
    print("PASS: the from-scratch prompt for task 004's own real goal includes the decomposition "
          "instruction and the matched computed-field worked example")


def test_generate_code_raw_omits_block_for_simple_single_field_goal():
    specialist = _make_specialist()
    contract = _make_contract(
        goal="Add a single new field 'preferred_language' (Char) to res.partner.",
        module_identity="res.partner",
    )
    prompt = _capture_generate_code_raw_prompt(specialist, contract, depends_on_module=None, target_module_files=None)
    assert "coordinated pieces to work together" not in prompt
    print("PASS: a simple single-field goal's prompt has no decomposition scaffold at all")


def _capture_scoped_edits_prompt(specialist, contract, prior_files):
    captured = {}

    async def _fake_call_structured(**call_kwargs):
        captured["prompt"] = call_kwargs["prompt"]
        from specialists.build.specialist import GeneratedModuleEdits
        return GeneratedModuleEdits(edits=[], notes="")

    with patch.object(specialist_module, "is_fast_path_eligible", return_value=True), \
         patch.object(schema_client_module, "is_fast_path_eligible", return_value=True), \
         patch.object(schema_client_module, "_read_real_field_rows", return_value=[]), \
         patch.object(schema_client_module, "get_relation_fields_fast", return_value={}), \
         patch.object(specialist_module, "call_structured", _fake_call_structured):
        asyncio.run(specialist._generate_scoped_edits(
            contract, "CONSTITUTION_TEXT", "SKILL_TEXT", "test_module", prior_files,
        ))
    return captured["prompt"]


def test_scoped_edits_retry_path_also_injects_the_shape_example():
    """Real bug this locks in: task 004's round 2 was a scoped-edit RETRY round, and it repeated
    the exact same missing-compute-method mistake round 1 made -- the gate must fire on retries
    too, not just round-1 full generation.
    """
    specialist = _make_specialist()
    contract = _make_contract(
        goal=(
            "On the fieldjob record, I want to see the total of all line prices shown "
            "automatically in the header. It should update whenever I add or change a line."
        ),
        module_identity="project.fieldjob",
    )
    prior_files = {
        "__manifest__.py": (
            "{'name': 't', 'version': '1.0', 'category': 't', 'summary': 't', 'author': 't', "
            "'depends': ['base'], 'data': []}"
        ),
        "models/models.py": "class ProjectFieldjob(models.Model):\n    _inherit = 'project.fieldjob'\n",
        "security/ir.model.access.csv": "id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink\n",
    }
    prompt = _capture_scoped_edits_prompt(specialist, contract, prior_files)
    assert "coordinated pieces to work together" in prompt
    assert "computed field displayed in a view" in prompt
    print("PASS: the scoped-edit retry path also injects the decomposition instruction and "
          "matched worked example, not just the round-1 full-generation path")


if __name__ == "__main__":
    test_detects_computed_field_displayed_in_view_shape()
    test_detects_smart_button_shape()
    test_detects_state_transition_trigger_shape()
    test_detects_onchange_autofill_shape()
    test_detects_invoice_line_construction_shape()
    test_detects_migration_script_must_actually_run_shape()
    test_single_piece_goal_does_not_match_any_shape()
    test_goal_facts_is_computed_alone_triggers_the_gate_even_without_a_textual_shape_match()
    test_multi_piece_prompt_block_includes_the_matched_shape_example_only()
    test_multi_piece_prompt_block_empty_for_non_qualifying_goal()
    test_is_computed_only_case_still_gets_the_general_instruction_with_no_example()
    test_generate_code_raw_injects_shape_example_for_task004_shaped_goal()
    test_generate_code_raw_omits_block_for_simple_single_field_goal()
    test_scoped_edits_retry_path_also_injects_the_shape_example()
    print("\nALL BUSINESS-LOGIC REASONING GATE TESTS PASSED")
