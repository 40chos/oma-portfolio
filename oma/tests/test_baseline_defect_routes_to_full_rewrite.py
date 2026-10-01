"""Real, confirmed follow-up fix (2026-08-09, task 07141af5's flagship run, project_ticket_counts
node) -- an independent review of this session's own 5 earlier fixes correctly identified the
actual remaining mechanism: `_generate_scoped_edits()`'s own docstring states plainly that it
"asks for a small, named list of edits ONLY... everything not named in an edit is copied forward
from prior_files byte-for-byte, never touched by the model at all." The baseline-defect note
(an earlier fix this same night) correctly told the model an existing class was broken -- but the
model still had to emit a valid, exact-match search/replace edit to fix it, the SAME "reproduce an
existing span character-for-character" difficulty this codebase already documented as a genuine,
confirmed weak spot for this model (the repetition-loop/ScopedEditApplicationError fallback,
tests/test_build_repetition_loop_fallback.py). No amount of prompting, temperature, or candidate
diversity can fix a structural mismatch between the task shape and what scoped-edit generation can
reliably do.

The fix reuses the SAME already-proven full-rewrite fallback mechanism (never new machinery):
when `_find_empty_inherit_class_defect()` detects a real defect in the committed baseline BEFORE
even attempting a scoped edit, `_generate_code_raw()` routes straight to a full rewrite (complete
freedom to actually remove/replace the broken class) instead of attempting a surgical edit against
content the model may not reproduce reliably -- given the current content as context via
`fallback_rewrite_context` so everything else is still preserved, exactly like the other fallback
branches.
"""
import asyncio
import os
import sys
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from contracts.schema import AutonomyTier, CapabilityClass, SpecialistType, TaskContract
from infra.gateway_client import ModelGatewayClient
from specialists.build.specialist import BuildSpecialist, GeneratedModuleFiles, ManifestFields


def _make_contract() -> TaskContract:
    return TaskContract(
        task_id=uuid.uuid4(),
        specialist_type=SpecialistType.bug_fix,
        capability_class=CapabilityClass.module_development,
        tier=AutonomyTier.tier_2_notify_after,
        goal="add ticket count smart buttons to project.project",
        inputs=[],
        rules=[],
        deliverables=[],
        compensating_actions=[],
        validation_by="testing_qa",
        pause_if=[],
        turn_budget=15,
    )


def _fake_generated() -> GeneratedModuleFiles:
    return GeneratedModuleFiles(
        manifest_fields=ManifestFields(
            name="x", version="1.0", category="Uncategorized", summary="", author="",
            depends=["base"], data=[],
        ),
        models_py="from odoo import models\n",
        security_csv="id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink\n",
        notes="",
    )


_EMPTY_INHERIT_BASELINE = (
    "from odoo import models, fields\n\n\n"
    "class ProjectProject(models.Model):\n"
    "    _inherit = 'project.project'\n"
)
_HEALTHY_BASELINE = (
    "from odoo import models, fields\n\n\n"
    "class ProjectProject(models.Model):\n"
    "    _inherit = 'project.project'\n"
    "    open_ticket_count = fields.Integer()\n"
)


async def _run_baseline_defect_routes_to_full_rewrite_not_scoped_edit_test():
    specialist = BuildSpecialist(client=ModelGatewayClient.__new__(ModelGatewayClient), db="unused")
    contract = _make_contract()
    prior_files = {"models/models.py": _EMPTY_INHERIT_BASELINE}

    scoped_edit_calls = []
    fallback_calls = []

    async def _fake_scoped_edits(*args, **kwargs):
        scoped_edit_calls.append(True)
        return _fake_generated()

    async def _fake_full_gen(contract_arg, constitution_text, skill_text, module_name,
                              depends_on_module=None, target_module_files=None,
                              prior_files=None, temperature=0.0, fallback_rewrite_context=None):
        fallback_calls.append(fallback_rewrite_context)
        return _fake_generated()

    specialist._generate_scoped_edits = _fake_scoped_edits
    original_generate_code_raw = BuildSpecialist._generate_code_raw.__get__(specialist)
    specialist._generate_code_raw = _fake_full_gen

    result = await original_generate_code_raw(
        contract, "constitution", "skill", "module", None, None,
        prior_files=prior_files, temperature=0.0,
    )

    assert scoped_edit_calls == [], (
        "a detected baseline defect must route straight to full rewrite -- scoped-edit must "
        "never even be attempted, since the model demonstrably can't reliably clean up "
        f"existing broken content via an exact-match edit. Got {len(scoped_edit_calls)} scoped-edit call(s)."
    )
    assert len(fallback_calls) == 1, (
        f"expected the full-rewrite fallback to be invoked exactly once, got {len(fallback_calls)}"
    )
    assert fallback_calls[0] == prior_files, (
        "the full-rewrite fallback must receive the exact prior_files as fallback_rewrite_context "
        f"so unrelated content is preserved -- got {fallback_calls[0]!r}"
    )
    assert result.models_py == "from odoo import models\n"
    print("PASS: a real baseline defect (empty _inherit class) routes straight to the full-rewrite "
          "fallback, never attempting a scoped edit against content the model can't reliably fix")


async def _run_clean_baseline_still_uses_scoped_edit_test():
    specialist = BuildSpecialist(client=ModelGatewayClient.__new__(ModelGatewayClient), db="unused")
    contract = _make_contract()
    prior_files = {"models/models.py": _HEALTHY_BASELINE}

    scoped_edit_calls = []
    fallback_calls = []

    async def _fake_scoped_edits(*args, **kwargs):
        scoped_edit_calls.append(True)
        return _fake_generated()

    async def _fake_full_gen(*args, **kwargs):
        fallback_calls.append(True)
        return _fake_generated()

    specialist._generate_scoped_edits = _fake_scoped_edits
    original_generate_code_raw = BuildSpecialist._generate_code_raw.__get__(specialist)
    specialist._generate_code_raw = _fake_full_gen

    result = await original_generate_code_raw(
        contract, "constitution", "skill", "module", None, None,
        prior_files=prior_files, temperature=0.0,
    )

    assert len(scoped_edit_calls) == 1, (
        f"a healthy baseline (no defect) must still use scoped-edit, unchanged from before this "
        f"fix -- got {len(scoped_edit_calls)} scoped-edit call(s)"
    )
    assert fallback_calls == [], (
        "the full-rewrite fallback must never fire when the baseline has no real defect -- "
        f"got {len(fallback_calls)} fallback call(s)"
    )
    assert result.models_py == "from odoo import models\n"
    print("PASS: a clean, healthy baseline still uses the ordinary scoped-edit path, exactly as before this fix")


async def _run_no_prior_files_never_triggers_baseline_check_test():
    # Round 1 of any task always has prior_files=None -- the baseline-defect check must be a
    # complete no-op there (nothing to check yet), never crash on a missing baseline.
    specialist = BuildSpecialist(client=ModelGatewayClient.__new__(ModelGatewayClient), db="unused")
    contract = _make_contract()

    scoped_edit_calls = []

    async def _fake_scoped_edits(*args, **kwargs):
        scoped_edit_calls.append(True)
        return _fake_generated()

    specialist._generate_scoped_edits = _fake_scoped_edits

    # prior_files=None skips straight past the `if prior_files:` block entirely (both the
    # baseline check and scoped-edit) -- confirmed by the real, unmodified full-generation path
    # below actually attempting a real (mocked-away-by-absence) LLM call; asserting scoped_edit
    # was never called is the real, relevant assertion here.
    import specialists.build.specialist as specialist_module

    async def _fake_call_structured(**kwargs):
        return _fake_generated()

    original_call_structured = specialist_module.call_structured
    specialist_module.call_structured = _fake_call_structured
    try:
        await BuildSpecialist._generate_code_raw(
            specialist, contract, "constitution", "skill", "module", None, None,
            prior_files=None, temperature=0.0,
        )
    finally:
        specialist_module.call_structured = original_call_structured

    assert scoped_edit_calls == [], "prior_files=None must never attempt a scoped edit at all"
    print("PASS: prior_files=None (round 1) never triggers the baseline-defect check or scoped-edit")


if __name__ == "__main__":
    asyncio.run(_run_baseline_defect_routes_to_full_rewrite_not_scoped_edit_test())
    asyncio.run(_run_clean_baseline_still_uses_scoped_edit_test())
    asyncio.run(_run_no_prior_files_never_triggers_baseline_check_test())
    print("\nALL BASELINE-DEFECT-ROUTES-TO-FULL-REWRITE TESTS PASSED")
