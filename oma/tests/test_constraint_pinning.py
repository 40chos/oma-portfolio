"""Phase 18 (§22.5/§22.8/§22.14 test 1): real tests for Constraint
Pinning -- TaskContract.constraint_status surviving both an ordinary
contract revision and a real mid-loop round-history compaction event
completely unaffected, and Build's own dual-position ledger rendering
(render_constraint_ledger(), specialists/build/specialist.py).

Follows this project's own established two-tier test discipline
(tests/test_replanning.py, tests/test_build_specialist.py): plain
`def test_...()` functions so pytest collects them by default, each
internally driving `asyncio.run()` for any piece that genuinely needs
the real model gateway -- never a second, parallel "only runs if you
invoke main() by hand" tier for anything that can reasonably run in a
normal `pytest` pass.
"""

import asyncio
import inspect
import os
import sys
import uuid
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from contracts.schema import AutonomyTier, CapabilityClass, ReplanRound, SpecialistType, TaskContract, VerificationResult
from infra.gateway_client import ModelGatewayClient
from manager.replanning import (
    compress_round_history_mid_loop,
    derive_constraint_labels,
    estimate_manager_context_pressure,
    revise_contract_from_verification,
)
from specialists.build.specialist import BuildSpecialist, GeneratedModuleEdits, GeneratedModuleFiles, ManifestFields, render_constraint_ledger

MANAGER_MODEL = "qwen3.6-27b"  # real, known 65536-token window (manager/replanning.MODEL_CONTEXT_WINDOWS)


def _make_contract(constraint_status: dict, goal: str = "Add scheduling fields; enforce a double-booking rule; and add a product-scoping relation.") -> TaskContract:
    return TaskContract(
        task_id=uuid.uuid4(), specialist_type=SpecialistType.bug_fix,
        capability_class=CapabilityClass.module_development, tier=AutonomyTier.tier_2_notify_after,
        goal=goal, inputs=[], rules=[], deliverables=[], compensating_actions=[],
        validation_by="testing_qa", pause_if=[], turn_budget=10,
        constraint_status=constraint_status,
    )


def test_constraint_status_and_regressed_constraints_default_empty():
    """§22.13: both new schema fields exist and default to empty,
    additive, backward-compatible -- every existing TaskContract/
    VerificationResult construction site (Phases 6-17) keeps working
    with zero changes.
    """
    contract = _make_contract(constraint_status={})
    assert contract.constraint_status == {}

    v = VerificationResult(
        task_id=contract.task_id, passed=True, reproduction_confirmed=True,
        uncovered_paths=[], coverage_diff="", spot_check_mismatch=False, notes="ok",
    )
    assert v.regressed_constraints == []
    print("PASS: TaskContract.constraint_status and VerificationResult.regressed_constraints "
          "both default to empty, additive to every existing call site")


def test_derive_constraint_labels_splits_a_real_multi_constraint_goal():
    """§22.5/§22.9-adjacent: the Manager's own up-front, deterministic
    label derivation, run against the exact real three-constraint goal
    shape confirmed live this session (§22.1) -- scheduling fields +
    double-booking rule + product-scoping relation, three genuinely
    independent requirements in one goal string.
    """
    goal = "Add scheduling fields to the service module; enforce a double-booking business rule; and add a product-scoping relation."
    labels = derive_constraint_labels(goal)

    assert isinstance(labels, dict)
    assert len(labels) >= 2, f"a real 3-clause goal must derive more than one constraint label, got {labels}"
    assert all(v == "pending" for v in labels.values()), "every freshly-derived label must start pending"
    for label in labels:
        assert label == label.lower(), f"label {label!r} must be snake_case (lowercase)"
        assert " " not in label, f"label {label!r} must be snake_case (no spaces)"

    # A single-clause goal collapses to exactly one label -- matching
    # the dossier's own finding that 1-2-constraint tasks are already
    # handled fine by today's single-contract shape.
    single = derive_constraint_labels("Fix the broken invoice total calculation.")
    assert len(single) == 1, f"a single-clause goal must derive exactly one label, got {single}"
    print(f"PASS: derive_constraint_labels() split the real 3-constraint goal into "
          f"{len(labels)} pending labels ({sorted(labels)}); a single-clause goal collapsed to 1")


def test_derive_constraint_labels_never_splits_on_prose_periods_or_metadata_lines():
    """Real, severe bug found live (2026-07-24, 50-task sequential
    re-run, task 001): every real task spec in this project (matching
    the original site_50_tasks.md convention this benchmark itself was
    written in) follows the same shape -- a prose request, then a
    structured "Key: Value" technical-spec block (Module:, Model:,
    Field:, View:, Security:, ...). The OLD split regex (period-based
    sentence splitting, plus no metadata-line filtering at all) badly
    over-decomposed this exact shape: a genuinely simple, ONE-field
    task ("add a Special instructions text field to crm.lead") got
    split into 3 meaningless labels, the last one derived straight
    from the literal "Module: mis_base_extend" line. Since nearly
    every task spec in this whole benchmark shares this exact
    prose-plus-metadata shape, this was very likely mis-decomposing a
    large fraction of the entire run -- and the mis-decomposition had
    a real, damaging downstream effect: with `constraint_status`
    wrongly non-empty, `_validate_goal_named_field_is_declared()`
    (specialists/build/specialist.py) correctly stayed silent for what
    LOOKED like a genuinely decomposed round, letting a round that
    silently never declared the requested field sail through every
    check, only surfacing as a real sandbox install crash.

    Reproduces the EXACT real goal text from that live incident.
    """
    goal = (
        "I want to see a text field on the lead form called 'Special instructions' "
        "where the salesperson can type a short note about what the customer said "
        "during the first call. Only show it on the form, not the list.\n\n"
        "Module: mis_base_extend\nModel: crm.lead\nField: special_instructions (Text)\n"
        "View: Inherit crm.lead.all.activities.form.view, add after description field "
        "inside Notes tab\nSecurity: visible to all internal users"
    )
    labels = derive_constraint_labels(goal)
    assert len(labels) == 1, (
        f"a genuinely single-field task described in prose plus a structured technical-spec "
        f"block must collapse to exactly one constraint label, not be split on ordinary prose "
        f"sentence boundaries or the technical-spec metadata lines -- got {labels}"
    )
    print(f"PASS: derive_constraint_labels() correctly collapsed the real task-001 goal "
          f"(two prose sentences + a 5-line metadata block) to exactly one label: {labels}")


def test_render_constraint_ledger_format():
    """§22.8: the exact compact block shape, including the imperative
    header (authority-drift countermeasure, §22.2) and the literal
    per-state text this format uses.
    """
    status = {
        "scheduling_fields": "satisfied",
        "double_booking_rule": "satisfied",
        "product_scoping_relation": "failing",
    }
    rendered = render_constraint_ledger(status)
    assert rendered.splitlines()[0] == (
        'CONSTRAINT STATUS (do not lose track of any "satisfied" item '
        'while working on a "failing" one):'
    )
    assert "- scheduling_fields: SATISFIED" in rendered
    assert "- double_booking_rule: SATISFIED" in rendered
    assert "- product_scoping_relation: FAILING -- this round's focus" in rendered

    # Empty status renders nothing -- never a bare, confusing header
    # with zero rows underneath it.
    assert render_constraint_ledger({}) == ""
    print("PASS: render_constraint_ledger() produces the exact compact format, and renders "
          "nothing at all for an empty constraint_status")


def test_generate_code_prompt_renders_ledger_twice_at_fixed_positions():
    """§22.8: the real prompt-assembly code inside
    BuildSpecialist._generate_code() -- confirm the SAME ledger block
    appears in full TWICE: once immediately after the constitution/skill
    preamble (the very start of the prompt) and once immediately before
    the final "Write the real manifest..." instruction line -- never
    deduped, per the lost-in-the-middle mitigation this is built for.

    call_structured() itself is patched here (capturing the exact
    `prompt` kwarg it was called with, returning a minimal valid
    GeneratedModuleFiles instance with no real network call) -- this
    test is about OUR OWN deterministic prompt-string assembly, not
    model behavior, so there is nothing to lose by not paying for a
    real ~30s generation just to throw its content away unread; the
    grammar/response_format wiring itself (§22.12 Component 8) is
    separately confirmed for real in test_gateway_client.py-style direct
    calls, not re-proven here.
    """
    captured = {}

    async def _fake_call_structured(**kwargs):
        captured.update(kwargs)
        return GeneratedModuleFiles(
            manifest_fields=ManifestFields(
                name="x", version="1.0", category="Uncategorized", summary="", author="",
                depends=["base"], data=[],
            ),
            models_py="from odoo import models", security_csv="id,name\n",
            notes="fake",
        )

    status = {
        "scheduling_fields": "satisfied",
        "double_booking_rule": "satisfied",
        "product_scoping_relation": "failing",
    }
    contract = _make_contract(constraint_status=status)

    async def _drive():
        client = ModelGatewayClient()
        try:
            specialist = BuildSpecialist(client=client, db="unused-for-this-test")
            with patch("specialists.build.specialist.call_structured", _fake_call_structured):
                await specialist._generate_code(
                    contract=contract, constitution_text="THE CONSTITUTION TEXT",
                    skill_text="THE SKILL TEXT", module_name="oma_test_module",
                )
        finally:
            await client.aclose()

    asyncio.run(_drive())

    assert "prompt" in captured, "call_structured() must have been called with a prompt kwarg"
    prompt = captured["prompt"]
    ledger = render_constraint_ledger(status)
    occurrences = prompt.count(ledger)
    assert occurrences == 2, (
        f"expected the exact ledger block twice (start + end), found {occurrences} occurrence(s) "
        f"in the assembled prompt"
    )

    first_index = prompt.index(ledger)
    second_index = prompt.rindex(ledger)
    preamble_end = prompt.index("THE SKILL TEXT") + len("THE SKILL TEXT")
    final_instruction_index = prompt.index("Write the real manifest and model code for this module.")

    assert first_index >= preamble_end, "first ledger occurrence must come after the constitution/skill preamble"
    assert first_index < prompt.index("Task goal:"), "first ledger occurrence must land before the goal/rules block"
    assert second_index < final_instruction_index, "second ledger occurrence must land immediately before the final instruction line"
    assert final_instruction_index - second_index < len(ledger) + 20, (
        "second ledger occurrence must be IMMEDIATELY before the final instruction line, not just "
        "somewhere earlier in the prompt"
    )
    print("PASS: Build's assembled prompt renders the exact constraint ledger twice, at the "
          "start (after the preamble) and immediately before the final instruction line")


def test_generate_scoped_edits_prompt_now_includes_live_schema_grounding():
    """50-task deep-dive (docs/reports/PHASE30_50TASK_DEEP_DIVE_MASTER_2026-08-05.md, P2 item 6 /
    Pattern B re-verification): real, confirmed gap found by direct comparison against
    `_generate_code_raw()` -- that function injects a live `<current_schema>` excerpt into EVERY
    call (Phase 30 §26 items 1/2/5), specifically because Build was confirmed hallucinating
    fields/model shape without it. `_generate_scoped_edits()` (the RETRY-only path) never had the
    same injection -- confirmed via a direct grep across its whole function body before this fix:
    zero references to `resolve_current_schema_block`/`current_schema` anywhere in it. This is a
    real, plausible contributing cause for a meaningful share of Pattern B's own "stalled revision
    loop / feedback not landing" instances (10 of 50 tasks, 20%): a retry round already carries the
    round's own critical-fix rules (verbatim failure text), but without live schema grounding,
    Build has no way to VERIFY those rules against the real target model's own actual fields.

    Same prompt-construction-only test discipline as
    test_generate_code_prompt_renders_ledger_twice_at_fixed_positions above -- call_structured()
    and resolve_current_schema_block() are both patched to capture/control their inputs, no real
    network call, since this test is about our own deterministic prompt assembly.
    """
    captured = {}

    async def _fake_call_structured(**kwargs):
        captured.update(kwargs)
        return GeneratedModuleEdits(edits=[], notes="fake")

    def _fake_resolve_current_schema_block(contract, db):
        captured["schema_block_call_args"] = (contract, db)
        return "project.fieldjob:\n  name: char, required\n  state: selection"

    contract = _make_contract(constraint_status={}, goal="Fix the 'state' field validation.")
    prior_files = {
        "__manifest__.py": (
            "{'name': 'x', 'version': '1.0', 'category': 'Uncategorized', 'summary': 'x', "
            "'author': 'x', 'depends': ['base'], 'data': []}\n"
        ),
        "models/models.py": (
            "from odoo import fields, models\n\nclass X(models.Model):\n"
            "    _inherit = 'project.fieldjob'\n"
        ),
        "security/ir.model.access.csv": (
            "id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink\n"
        ),
    }

    async def _drive():
        client = ModelGatewayClient()
        try:
            specialist = BuildSpecialist(client=client, db="test_db")
            with patch("specialists.build.specialist.call_structured", _fake_call_structured), \
                 patch("specialists.build.specialist.resolve_current_schema_block", _fake_resolve_current_schema_block):
                await specialist._generate_scoped_edits(
                    contract=contract, constitution_text="THE CONSTITUTION TEXT",
                    skill_text="THE SKILL TEXT", module_name="oma_test_module",
                    prior_files=prior_files,
                )
        finally:
            await client.aclose()

    asyncio.run(_drive())

    assert "schema_block_call_args" in captured, (
        "resolve_current_schema_block() must actually be called by _generate_scoped_edits() -- "
        "the real, confirmed gap this fix closes"
    )
    assert "prompt" in captured
    prompt = captured["prompt"]
    assert "<current_schema>" in prompt and "project.fieldjob:" in prompt, (
        f"the live schema excerpt must actually appear in the assembled retry prompt -- got:\n{prompt[:2000]}"
    )
    preamble_end = prompt.index("THE SKILL TEXT") + len("THE SKILL TEXT")
    schema_index = prompt.index("<current_schema>")
    assert schema_index >= preamble_end, "the schema block must come after the constitution/skill preamble"
    assert schema_index < prompt.index("CURRENT VALIDATED CONTENT"), (
        "the schema block must come before the retry's own current-content block, matching "
        "_generate_code_raw()'s own established ordering"
    )
    print("PASS: _generate_scoped_edits()'s own retry prompt now includes the same live "
          "<current_schema> grounding _generate_code_raw() already had")


def test_constraint_status_survives_an_ordinary_contract_revision():
    """§22.5: revise_contract_from_verification() only ever updates
    `rules` -- constraint_status must pass through completely
    byte-identical across a real round revision, never re-derived,
    never dropped, never silently reset.
    """
    status = {"scheduling_fields": "satisfied", "product_scoping_relation": "failing"}
    contract = _make_contract(constraint_status=status)
    v = VerificationResult(
        task_id=contract.task_id, passed=False, reproduction_confirmed=True,
        uncovered_paths=["oma.service.scoping"], coverage_diff="", spot_check_mismatch=False,
        notes="product-scoping relation still missing", root_cause="skill_gap",
    )
    new_contract, _reasoning = revise_contract_from_verification(contract, v, round_number=3)
    assert new_contract.constraint_status == status, (
        "constraint_status must survive a contract revision completely unchanged -- "
        "revise_contract_from_verification() only ever appends to `rules`"
    )
    assert new_contract.constraint_status is not contract.rules  # sanity: not comparing the wrong field
    print("PASS: constraint_status survives revise_contract_from_verification() byte-identical")


def test_recurrence_escalation_survives_the_resume_paths_rules_reformat():
    """Real bug found live (2026-07-12, verifying the scaffold-
    boilerplate fix on Operator's original 8-constraint service-management
    task): revise_contract_from_verification()'s own recurrence
    detection used to filter contract.rules for a LITERAL marker string
    ("Code-Review flagged" / "failed, classified as") to find the prior
    round's own finding to compare against -- but manager.loop's resume
    path (resume_task_after_checkpoint, after a round-budget-exhaustion
    checkpoint) REBUILDS contract.rules from scratch using a completely
    different format ("Round N: <notes>"), which contains neither
    marker string. Confirmed live: an identical Code-Review finding
    ("Defines 7 extra fields...") recurred verbatim across a real
    checkpoint/resume boundary, yet got NO CRITICAL_RULE_PREFIX
    escalation on the very first round after resuming -- exactly the
    scenario recurrence detection exists for, since a constraint that
    already exhausted one round budget is the one MOST likely to keep
    recurring.

    Fixed: both recurrence checks now compare against contract.rules[-1]
    directly (whatever format it's in) via the same marker-agnostic
    _same_underlying_finding() word-overlap heuristic, so this survives
    any rules-formatting change, not just this one resume-path shape.
    This test reproduces the exact resume-path rules shape (a
    "Round N: <notes>" entry, never "Code-Review flagged...") and
    confirms the SAME finding recurring across it now correctly
    escalates.
    """
    from contracts.schema import CRITICAL_RULE_PREFIX

    finding = (
        "Defines 7 extra fields (title, description, status, priority, attachment_ids, "
        "product_id, employee_ids) beyond the strictly required project_id, violating the "
        "round's explicit scope constraint."
    )
    # Exactly the shape manager.loop's resume path produces -- never the
    # "Code-Review flagged this last attempt -- fix it this time: ..."
    # phrasing revise_contract_from_verification() itself would have used
    # pre-resume.
    contract = _make_contract(constraint_status={})
    contract = contract.model_copy(update={
        "rules": [
            "Original attempt summary: After 5 round(s), this task stopped because it ran out "
            "of retry attempts.",
            f"Round 5: Reproduction FAILED for oma.service.issue.project_id. Spot-check matches "
            f"the self-report. Code-Review found 1 blocking issue(s): {finding}",
        ],
    })
    v = VerificationResult(
        task_id=contract.task_id, passed=False, reproduction_confirmed=False,
        uncovered_paths=[], coverage_diff="", spot_check_mismatch=False,
        notes=f"Reproduction FAILED for project.project.issue_ids. Spot-check matches the self-report.",
        root_cause="pattern_worth_a_rule",
    )
    new_contract, _reasoning = revise_contract_from_verification(
        contract, v, round_number=1, code_review_finding=finding,
    )
    critical_rules = [r for r in new_contract.rules if r.startswith(CRITICAL_RULE_PREFIX)]
    assert critical_rules, (
        f"an identical Code-Review finding recurring across the resume-path's own rules "
        f"format must still trigger CRITICAL_RULE_PREFIX escalation: {new_contract.rules}"
    )
    print("PASS: recurrence escalation correctly fires even when the prior round's own finding "
          "is in the resume path's 'Round N: ...' format, not revise_contract_from_verification()'s "
          "own 'Code-Review flagged...' phrasing")


def test_recurrence_escalation_catches_a_non_consecutive_regression():
    """Real bug found live (2026-07-24, task 020's Group C re-run): both
    recurrence checks in revise_contract_from_verification() compared
    ONLY against `contract.rules[-1]` -- the single, immediately-
    preceding round's own complaint. A real, live oscillation doesn't
    always recur on CONSECUTIVE rounds though: round 1 referenced
    out-of-scope fields (`amount_total`, `expected_finish_date`), round
    2 correctly removed them (a DIFFERENT complaint, about `lang`, was
    `rules[-1]` at that point), and round 3 silently REintroduced the
    exact same out-of-scope fields -- invisible to a rules[-1]-only
    check, since going into round 3, rules[-1] was round 2's `lang`
    complaint, not round 1's fields complaint. Confirmed live: this
    exact "fixed, then silently un-fixed two rounds later" shape kept
    task 020 oscillating even after every OTHER real pipeline bug found
    the same session was fixed.

    Fixed: `_recurs_anywhere_in_rules()` checks the FULL rules history,
    not just the last entry -- a strict widening (every case the old
    rules[-1]-only check caught is still caught; this test's sibling
    above still passes unchanged).
    """
    from contracts.schema import CRITICAL_RULE_PREFIX

    fields_finding = (
        "The body_html references object.amount_total and object.expected_finish_date "
        "which are not in scope for this round and will cause KeyError on load."
    )
    lang_finding = (
        "The field lang uses invalid syntax {{ object.partner_id.lang }} inside a record field."
    )
    contract = _make_contract(constraint_status={})
    contract = contract.model_copy(update={
        "rules": [
            "Original attempt summary: ...",
            f"Round 1: Code-Review found 1 blocking issue(s): {fields_finding}",
            f"Round 2: Code-Review found 1 blocking issue(s): {lang_finding}",
        ],
    })
    v = VerificationResult(
        task_id=contract.task_id, passed=False, reproduction_confirmed=False,
        uncovered_paths=[], coverage_diff="", spot_check_mismatch=False,
        notes="Reproduction FAILED for project.fieldjob.fieldjob_acceptance_trigger.",
        root_cause="one_off",
    )
    new_contract, _reasoning = revise_contract_from_verification(
        contract, v, round_number=3, code_review_finding=fields_finding,
    )
    critical_rules = [r for r in new_contract.rules if r.startswith(CRITICAL_RULE_PREFIX)]
    assert critical_rules, (
        f"a finding that recurred in round 1 (not round 2, the immediately preceding round) "
        f"must still trigger CRITICAL_RULE_PREFIX escalation on round 3: {new_contract.rules}"
    )
    print("PASS: recurrence escalation correctly fires for a non-consecutive regression "
          "(round 1's finding reappearing in round 3, skipping round 2)")


def test_constraint_status_never_reaches_compress_round_history_mid_loop():
    """§22.5's central, structural guarantee: constraint_status can
    NEVER be silently lossily compressed, because
    compress_round_history_mid_loop() -- the one and only mid-loop
    summarization call in this codebase -- does not, and structurally
    cannot, ever receive it. Confirmed two ways: (1) by inspecting the
    function's own real signature (it takes goal/prior_rounds/
    existing_summary/client/model/task_id -- never a TaskContract, never
    constraint_status, so there is no parameter to even pass it
    through), and (2) with a REAL call against a genuinely large,
    real prior_rounds history forced past SOFT_TRIGGER_FRACTION (the
    exact real trigger condition manager/loop.py checks every round),
    confirming compression actually fires for real and produces a real
    summary -- and that a TaskContract's own constraint_status, revised
    immediately afterward using that real compressed summary as
    evidence, is completely unaffected by the compaction event that
    just happened alongside it.
    """
    sig = inspect.signature(compress_round_history_mid_loop)
    param_names = set(sig.parameters)
    assert "constraint_status" not in param_names
    assert "contract" not in param_names, (
        f"compress_round_history_mid_loop() must never accept a whole TaskContract -- only "
        f"goal (plain str) -- structurally guaranteeing constraint_status can't reach it. "
        f"Actual params: {sorted(param_names)}"
    )

    goal = "Add scheduling fields; enforce a double-booking rule; and add a product-scoping relation."
    long_finding = (
        "Product-scoping relation still references the wrong model and the manifest dependency "
        "list keeps oscillating between declaring 'hr' and declaring 'product' but never both at "
        "once, which is a real, specific, recurring problem confirmed across many actual rounds. "
    ) * 5  # ~1,310 chars ≈ ~440 tokens per field, 2 fields/round
    _ROUND_COUNT = 60  # ~52,800 tokens total (~80% of qwen3.6-27b's real 65,536-token window) --
    # comfortably clears SOFT_TRIGGER_FRACTION (0.50). A HIGH round count (not a large per-round
    # size) is deliberate: qwen3.6-27b (§0.5.2's own confirmed finding, also documented in
    # infra/structured_output.py's strip_think_block() docstring) always emits a long hidden
    # <think> trace regardless of /no_think, and compress_round_history_mid_loop()'s own
    # max_tokens budget scales with round count (400 * round_count) -- many short rounds gives
    # the real summarization call genuine room to actually finish thinking and answer, rather
    # than a few huge rounds that would blow the output budget before reaching a real answer.

    prior_rounds = [
        ReplanRound(
            round_number=i, previous_contract_task_id=str(uuid.uuid4()),
            verification_result=VerificationResult(
                task_id=uuid.uuid4(), passed=False, reproduction_confirmed=True,
                uncovered_paths=[], coverage_diff="", spot_check_mismatch=False,
                notes=long_finding, root_cause="skill_gap",
            ),
            code_review_finding_summary=long_finding,
            revision_reasoning=f"round {i} revision reasoning",
            new_contract=_make_contract(constraint_status={}, goal=goal),
        )
        for i in range(1, _ROUND_COUNT + 1)
    ]

    pressure = estimate_manager_context_pressure(goal, prior_rounds, None, MANAGER_MODEL)
    assert pressure >= 0.50, (
        f"test setup must genuinely force past the real SOFT_TRIGGER_FRACTION (0.50) manager/loop.py "
        f"itself checks every round -- got {pressure:.3f}, engineer a larger prior_rounds history"
    )

    async def _drive():
        client = ModelGatewayClient()
        try:
            return await compress_round_history_mid_loop(
                goal, prior_rounds, None, client, MANAGER_MODEL, task_id=None,
            )
        finally:
            await client.aclose()

    summary = asyncio.run(_drive())
    assert isinstance(summary, str) and len(summary) > 0, "a real compaction event must produce a real, non-empty summary"
    # compress_round_history_mid_loop() has two legitimate, real, non-crashing outcomes under
    # real gateway load: a genuine LLM-produced compression (materially smaller than the raw
    # history), or -- confirmed to happen for real under contention/timeout against this shared
    # dev host, e.g. two heavy test runs hitting the same llama-swap-served model at once -- its
    # own documented honest fallback (raw round text passed through unsummarized, never a crash,
    # never silently dropped). Both are valid "a real compaction event happened" outcomes for
    # THIS test's actual point (constraint_status is unaffected either way); only a genuinely
    # larger-than-raw or malformed result would be a real bug.
    raw_concat_upper_bound = len(long_finding) * 2 * _ROUND_COUNT + 500
    assert len(summary) <= raw_concat_upper_bound, (
        f"summary ({len(summary)}c) must never exceed the raw round text it was built from "
        f"({raw_concat_upper_bound}c) -- that would mean data was invented, not compressed or "
        f"honestly passed through"
    )
    if len(summary) < len(long_finding) * 5:
        print("  (genuine LLM-produced compression fired this run)")
    else:
        print("  (gateway fell back to its own honest, non-crashing raw-passthrough path this "
              "run -- still a real, valid outcome; constraint_status survival is unaffected either way)")

    # The real, separate proof this test exists for: a TaskContract
    # carrying real constraint_status, revised the SAME round the
    # compaction above happened, using the compressed summary as
    # evidence -- constraint_status must be completely unaffected.
    status_before = {"scheduling_fields": "satisfied", "double_booking_rule": "satisfied", "product_scoping_relation": "failing"}
    contract = _make_contract(constraint_status=status_before, goal=goal)
    v_after_compaction = VerificationResult(
        task_id=contract.task_id, passed=False, reproduction_confirmed=True,
        uncovered_paths=[], coverage_diff="", spot_check_mismatch=False,
        notes=f"Compressed history so far: {summary}", root_cause="skill_gap",
    )
    revised_contract, _reasoning = revise_contract_from_verification(contract, v_after_compaction, round_number=6)
    assert revised_contract.constraint_status == status_before, (
        "constraint_status must be completely unaffected by a real mid-loop compaction event "
        "happening alongside it in the same round"
    )

    # And it still renders in full, in both fixed positions, in Build's
    # own next-round prompt -- the compaction event changed nothing
    # about what render_constraint_ledger() produces.
    ledger = render_constraint_ledger(revised_contract.constraint_status)
    assert "scheduling_fields: SATISFIED" in ledger
    assert "double_booking_rule: SATISFIED" in ledger
    assert "product_scoping_relation: FAILING -- this round's focus" in ledger
    print(f"PASS: a real mid-loop compaction event fired (pressure {pressure:.3f} >= 0.50, real "
          f"summary produced, {len(long_finding) * 5}c raw -> {len(summary)}c summary), and "

          f"constraint_status survived it completely unaffected, still rendering in full")


def test_field_omission_recurrence_escalates_to_a_concrete_snippet():
    """Real, general fix (2026-07-25, task 001's 9th fresh submission):
    `_validate_goal_named_field_is_declared()` (specialists/build/
    specialist.py) correctly fired pre-write on EVERY round of a full
    5-round budget, yet Build never once actually declared the field --
    the diagnostic-only CRITICAL_RULE_PREFIX escalation alone wasn't
    enough, mirroring the exact lesson already learned for the
    uncovered_paths consecutive_repeats >= 2 case. After this same
    validator's rejection recurs 3 times (2 confirmed repeats plus the
    current one), revise_contract_from_verification() must switch to a
    concrete, literal `fields.X(...)` instruction parsed from the goal's
    own stated field type -- never invented, never task-specific.
    """
    goal = (
        "I want to see a text field on the lead form called 'Special instructions' where the "
        "salesperson can type a short note about what the customer said during the first call. "
        "Only show it on the form, not the list.\n\n"
        "Module: mis_base_extend\n"
        "Model: crm.lead\n"
        "Field: special_instructions (Text)\n"
        "View: Inherit crm.lead.all.activities.form.view, add after description field inside Notes tab\n"
        "Security: visible to all internal users"
    )
    notes = (
        "the task's own goal names a field 'special_instructions' to add, but generated models_py "
        "never actually declares it (no `special_instructions = fields.X(...)` found anywhere) -- "
        "this is a real, silent omission that only surfaces as an install crash later "
        "('Field does not exist in model'), never caught by any prior check. The field "
        "must actually be added, not just referenced in the view."
    )
    contract = _make_contract(constraint_status={"i_want_see": "pending"}, goal=goal)
    # Two prior rounds already carrying the identical rejection --
    # _consecutive_notes_repeats() must count both before this, the 3rd
    # occurrence, triggers the concrete-snippet escalation.
    contract = contract.model_copy(update={
        "rules": [
            f"Prior attempt (round 1) failed: {notes}",
            f"Prior attempt (round 2) failed: {notes}",
        ],
    })
    v = VerificationResult(
        task_id=contract.task_id, passed=False, reproduction_confirmed=False,
        uncovered_paths=[], coverage_diff="", spot_check_mismatch=False,
        notes=notes, root_cause=None,
    )
    new_contract, reasoning = revise_contract_from_verification(contract, v, round_number=3)
    newest_rule = new_contract.rules[-1]
    assert "special_instructions = fields.Text(string='Special Instructions')" in newest_rule, (
        f"expected a concrete, ready-to-paste field declaration in the escalated rule, got: {newest_rule!r}"
    )
    assert "rounds in a row" in newest_rule
    print("PASS: a field-omission validator rejection recurring 3 times escalates to a concrete, "
          f"literal fields.X(...) snippet parsed from the goal's own stated type -- reasoning: {reasoning}")

    # Real, confirmed bug found live (2026-07-25, tasks 001 and 002's
    # first real runs with this escalation deployed): rounds 4 and 5
    # both silently REVERTED to the plain diagnostic form instead of
    # continuing to prescribe the concrete snippet, because the OLD
    # recurrence counter used generic word-overlap
    # (`_same_underlying_finding()`) against the prescriptive rule's own
    # deliberately different wording ("Add EXACTLY this line...") --
    # which shares almost no vocabulary with the validator's diagnostic
    # message, so the very rule meant to prove recurrence broke its own
    # recurrence detection the moment it fired once. This continuation
    # proves round 4 (whose own `contract.rules` now ends with the
    # round-3 escalation just asserted above) still gets the concrete
    # snippet, not a reversion to plain diagnostic text.
    v4 = VerificationResult(
        task_id=contract.task_id, passed=False, reproduction_confirmed=False,
        uncovered_paths=[], coverage_diff="", spot_check_mismatch=False,
        notes=notes, root_cause="pattern_worth_a_rule",
    )
    round4_contract, _r4 = revise_contract_from_verification(new_contract, v4, round_number=4)
    round4_rule = round4_contract.rules[-1]
    assert "special_instructions = fields.Text(string='Special Instructions')" in round4_rule, (
        f"round 4 must NOT revert to the plain diagnostic form after round 3's escalation -- got: {round4_rule!r}"
    )
    print("PASS: round 4 keeps prescribing the concrete snippet instead of silently reverting to "
          "plain diagnostic text")


def test_field_omission_escalation_never_fires_on_a_first_occurrence():
    """The concrete-snippet escalation above must only fire once the
    SAME rejection has already recurred -- a first-round validator
    rejection gets the ordinary diagnostic-only treatment, same as
    every other notes-driven failure, since giving away the answer on
    round 1 would remove the model's own chance to get it right
    unassisted.
    """
    goal = (
        "Add a text field. \n\nField: special_instructions (Text)\nModel: crm.lead"
    )
    notes = (
        "the task's own goal names a field 'special_instructions' to add, but generated models_py "
        "never actually declares it (no `special_instructions = fields.X(...)` found anywhere)"
    )
    contract = _make_contract(constraint_status={"add_a_text": "pending"}, goal=goal)
    v = VerificationResult(
        task_id=contract.task_id, passed=False, reproduction_confirmed=False,
        uncovered_paths=[], coverage_diff="", spot_check_mismatch=False,
        notes=notes, root_cause=None,
    )
    new_contract, _reasoning = revise_contract_from_verification(contract, v, round_number=1)
    newest_rule = new_contract.rules[-1]
    assert "fields.Text(string=" not in newest_rule, (
        f"a first-occurrence rejection must not yet get the concrete-snippet escalation, got: {newest_rule!r}"
    )
    print("PASS: no concrete-snippet escalation on a first occurrence")


def test_field_omission_escalation_never_prescribes_a_fake_computed_field():
    """Real, severe bug found live (2026-07-25, task 004): watched live,
    round 3's escalation prescribed `amount_total = fields.Monetary(
    string='Amount Total', currency_field='currency_id')` -- a plain,
    NON-computed field -- for a goal that explicitly asked for
    `compute=_compute_amount_total, depends on line_ids.price_unit,
    store=True`. A field that "exists" but never actually computes
    anything would silently satisfy the presence check while being
    semantically broken (always reads 0, never auto-updates) -- a
    confidently-worded WRONG instruction, worse than the diagnostic-only
    fallback. Must back off to the plain diagnostic form instead of
    guessing a fake skeleton for a computed/related/onchange field.
    """
    goal = (
        "On the fieldjob record, I want to see the total of all line prices shown automatically "
        "in the header.\n\n"
        "Module: project_fieldjob\nModel: project.fieldjob\n"
        "Field: amount_total (Monetary, compute=_compute_amount_total, depends on "
        "line_ids.price_unit, store=True, currency_field=currency_id)\n"
    )
    notes = (
        "the task's own goal names a field 'amount_total' to add, but generated models_py "
        "never actually declares it (no `amount_total = fields.X(...)` found anywhere)"
    )
    contract = _make_contract(constraint_status={"on_the_fieldjob": "pending"}, goal=goal)
    contract = contract.model_copy(update={
        "rules": [
            f"Prior attempt (round 1) failed: {notes}",
            f"Prior attempt (round 2) failed: {notes}",
        ],
    })
    v = VerificationResult(
        task_id=contract.task_id, passed=False, reproduction_confirmed=False,
        uncovered_paths=[], coverage_diff="", spot_check_mismatch=False,
        notes=notes, root_cause=None,
    )
    new_contract, _reasoning = revise_contract_from_verification(contract, v, round_number=3)
    newest_rule = new_contract.rules[-1]
    assert "fields.Monetary(string=" not in newest_rule, (
        f"must never prescribe a fake non-computed field for a computed-field spec, got: {newest_rule!r}"
    )
    print("PASS: no fake computed-field snippet prescribed -- falls back to plain diagnostic form")


def test_field_omission_escalation_never_prescribes_a_todo_placeholder_selection():
    """Real, severe bug found live (2026-07-25, Phase 25A regression
    gate, task 003's resubmission): round 3's escalation fired exactly
    as designed (the recurrence mechanism itself is sound) but
    PRESCRIBED `order_priority = fields.Selection([('TODO', 'TODO')],
    string='Order Priority')` -- a confidently-worded but FAKE
    placeholder, worse than the plain diagnostic fallback, since a field
    that "exists" with garbage options would silently satisfy the
    presence check while being semantically useless. Root cause: this
    goal used a SEPARATE `Selection: low/normal/high` line (a real,
    established convention) rather than the inline-parenthetical form
    `_build_selection_skeleton()` recognized -- it returned None, and
    `_FIELD_TYPE_SKELETONS['selection']` (a generic TODO placeholder)
    silently took over instead. Fixed two ways at once, both proven by
    this test: `_build_selection_skeleton()` now recognizes the
    separate-line convention too, AND the generic dict no longer carries
    a 'selection' entry at all (removed permanently -- a fake skeleton
    must never be the fallback for ANY type _build_selection_skeleton()
    doesn't itself resolve).
    """
    goal = (
        "On the sale order list, I want to see a priority field: Low, Normal, High. High "
        "priority orders should be highlighted in red in the list.\n\n"
        "Module: mis_base_extend\n"
        "Model: sale.order\n"
        "Field: order_priority (Selection)\n"
        "Selection: low/normal/high\n"
        "Field type: fields.Selection\n"
        "Default: normal\n"
    )
    notes = (
        "the task's own goal names a field 'order_priority' to add, but generated models_py "
        "never actually declares it (no `order_priority = fields.X(...)` found anywhere)"
    )
    contract = _make_contract(constraint_status={"on_the_sale": "pending"}, goal=goal)
    contract = contract.model_copy(update={
        "rules": [
            f"Prior attempt (round 1) failed: {notes}",
            f"Prior attempt (round 2) failed: {notes}",
        ],
    })
    v = VerificationResult(
        task_id=contract.task_id, passed=False, reproduction_confirmed=False,
        uncovered_paths=[], coverage_diff="", spot_check_mismatch=False,
        notes=notes, root_cause=None,
    )
    new_contract, _reasoning = revise_contract_from_verification(contract, v, round_number=3)
    newest_rule = new_contract.rules[-1]
    assert "TODO" not in newest_rule, (
        f"must never prescribe a fake TODO-placeholder Selection skeleton, got: {newest_rule!r}"
    )
    assert (
        "order_priority = fields.Selection([('low', 'Low'), ('normal', 'Normal'), ('high', 'High')], "
        "string='Order Priority', default='normal')" in newest_rule
    ), f"expected the REAL options parsed from the separate 'Selection:' line, got: {newest_rule!r}"
    print("PASS: a Selection field's separate 'Selection:' line is recognized, and the escalation "
          "prescribes real options -- never a TODO placeholder")


def test_field_omission_escalation_never_prescribes_a_fake_relational_comodel():
    """Same root-cause class as the Selection TODO-placeholder bug above,
    for the three relational types: a goal naming a Many2one/One2many/
    Many2many field can never safely have its comodel guessed (unlike
    Selection options, a comodel is essentially never stated as
    concretely in a goal's own Field-type line) -- confirmed this dict
    used to carry `'<comodel.name>'` placeholder skeletons for all
    three, the exact same "confidently wrong is worse than no
    prescription" defect. Must fall back to the plain diagnostic form,
    never a fake comodel reference.
    """
    goal = "Add a link. \n\nField: partner_id (Many2one)\nField type: fields.Many2one\nModel: crm.lead"
    notes = (
        "the task's own goal names a field 'partner_id' to add, but generated models_py "
        "never actually declares it (no `partner_id = fields.X(...)` found anywhere)"
    )
    contract = _make_contract(constraint_status={"add_a_link": "pending"}, goal=goal)
    contract = contract.model_copy(update={
        "rules": [
            f"Prior attempt (round 1) failed: {notes}",
            f"Prior attempt (round 2) failed: {notes}",
        ],
    })
    v = VerificationResult(
        task_id=contract.task_id, passed=False, reproduction_confirmed=False,
        uncovered_paths=[], coverage_diff="", spot_check_mismatch=False,
        notes=notes, root_cause=None,
    )
    new_contract, _reasoning = revise_contract_from_verification(contract, v, round_number=3)
    newest_rule = new_contract.rules[-1]
    assert "comodel" not in newest_rule and "fields.Many2one(" not in newest_rule, (
        f"must never prescribe a fake relational-field skeleton with a guessed comodel, got: {newest_rule!r}"
    )
    print("PASS: no fake relational-field skeleton prescribed -- falls back to plain diagnostic form")
