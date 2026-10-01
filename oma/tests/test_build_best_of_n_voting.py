"""Phase 30 §26 item 12 -- regression tests for execution-fingerprint clustering in
_generate_best_of_n(), extending (never replacing) the prior fewest-regressions-wins-only
scoring. No real LLM/DB/SSH calls -- _generate_code()/run_build_internal_loop()/
_validate_generated_module() and the deterministic-override module functions are all
monkeypatched, matching tests/test_build_internal_loop.py's own established convention for
exercising _generate_best_of_n().
"""
import asyncio
import uuid
from unittest.mock import patch

import specialists.build.specialist as specialist_module
from contracts.schema import AutonomyTier, CapabilityClass, SpecialistType, TaskContract
from specialists.build.specialist import (
    BuildSpecialist,
    GeneratedModuleFiles,
    InternalLoopOutcome,
    ManifestFields,
    _candidate_execution_fingerprint,
)


def _make_generated(models_py: str, notes: str = "") -> GeneratedModuleFiles:
    manifest_fields = ManifestFields(
        name="Test", version="1.0", category="Test", summary="s", author="a",
        depends=["base"], data=[],
    )
    return GeneratedModuleFiles(
        manifest_fields=manifest_fields, models_py=models_py,
        security_csv="id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink\n",
        notes=notes,
    )


def _make_contract() -> TaskContract:
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
    )


def _make_specialist() -> BuildSpecialist:
    return BuildSpecialist(client=None, db="test_db")


# ---------------------------------------------------------------------------
# _candidate_execution_fingerprint() itself
# ---------------------------------------------------------------------------

_VARIANT_A = (
    "class X(models.Model):\n"
    "    _name = 'x.x'\n"
    "    total = fields.Float()\n\n"
    "    def action_confirm(self):\n"
    "        pass\n"
)
_VARIANT_A_REWORDED = (
    "# a differently-worded, differently-formatted candidate with the identical real shape\n"
    "class X(models.Model):\n"
    "    _name = 'x.x'\n\n"
    "    total = fields.Float()\n"
    "\n"
    "    def action_confirm(self):\n"
    "        # different comment, different notes -- same structural content\n"
    "        pass\n"
)
_VARIANT_B_DIFFERENT_FIELD = (
    "class X(models.Model):\n"
    "    _name = 'x.x'\n"
    "    amount = fields.Float()\n\n"
    "    def action_confirm(self):\n"
    "        pass\n"
)


def test_fingerprint_identical_for_structurally_same_candidates_with_different_wording():
    fp_a = _candidate_execution_fingerprint(_make_generated(_VARIANT_A, notes="candidate A notes"))
    fp_a_reworded = _candidate_execution_fingerprint(_make_generated(_VARIANT_A_REWORDED, notes="totally different notes"))
    assert fp_a == fp_a_reworded


def test_fingerprint_differs_for_different_field_sets():
    fp_a = _candidate_execution_fingerprint(_make_generated(_VARIANT_A))
    fp_b = _candidate_execution_fingerprint(_make_generated(_VARIANT_B_DIFFERENT_FIELD))
    assert fp_a != fp_b


def test_fingerprint_unparseable_content_never_collapses_with_anything():
    broken1 = _make_generated("class X(models.Model)\n    _name = 'x.x'\n")  # missing colon
    broken2 = _make_generated("class X(models.Model)\n    _name = 'x.x'\n")  # identical broken text
    fp1 = _candidate_execution_fingerprint(broken1)
    fp2 = _candidate_execution_fingerprint(broken2)
    fp_valid = _candidate_execution_fingerprint(_make_generated(_VARIANT_A))
    assert fp1 == fp2, "identical unparseable text is still allowed to match itself"
    assert fp1 != fp_valid


# ---------------------------------------------------------------------------
# _generate_best_of_n()'s real selection behavior
# ---------------------------------------------------------------------------

def _run_best_of_n(specialist, contract, candidates_and_regressions, candidate_count):
    """candidates_and_regressions: list of (models_py, regressed_constraint_labels) per candidate
    index. Patches _generate_code to return each in order, and compute_regressed_constraints to
    report exactly the given regression count for that candidate."""
    call_index = {"n": 0}

    async def _fake_generate_code(contract_arg, constitution_text, skill_text, module_name, depends_on_module, target_module_files, prior_files=None, temperature=0.0):
        i = call_index["n"]
        call_index["n"] += 1
        models_py, _ = candidates_and_regressions[i]
        return _make_generated(models_py)

    async def _fake_internal_loop(contract_arg, module_name, generated, task_id, **kwargs):
        return InternalLoopOutcome(generated=generated, action="keep", steps_used=1, findings_history=[[]], budget_exhausted=False)

    async def _fake_override_view(*args, **kwargs):
        return None

    def _fake_override_security(*args, **kwargs):
        return None

    async def _fake_validate(*args, **kwargs):
        return None

    def _fake_compute_regressed(old_files, candidate_files, constraint_status, constraint_nodes=None):
        # Identify which candidate this is by matching its models_py content.
        for models_py, regressed_labels in candidates_and_regressions:
            if candidate_files.get("models/models.py") == models_py:
                return regressed_labels
        return []

    specialist._generate_code = _fake_generate_code
    specialist.run_build_internal_loop = _fake_internal_loop
    specialist._validate_generated_module = _fake_validate

    with patch.object(specialist_module, "_maybe_override_view_xml_deterministically", _fake_override_view), \
         patch.object(specialist_module, "_maybe_override_security_csv_deterministically", _fake_override_security), \
         patch.object(specialist_module, "compute_regressed_constraints", _fake_compute_regressed):
        winner, count_used = asyncio.run(specialist._generate_best_of_n(
            contract, "constitution", "skill", "test_module",
            None, None, None, old_files_by_relpath={}, candidate_count=candidate_count,
        ))
    return winner, count_used


def test_best_of_n_prefers_largest_consensus_cluster_over_lower_regression_singleton():
    """Two structurally-identical candidates (regression counts 2 and 1) form a real consensus
    cluster; a third, structurally-distinct candidate has FEWER regressions (0) but is a lone
    outlier. The winner must come from the size-2 consensus cluster, not the singleton --
    exactly item 12's own point: 2+ independently-generated candidates agreeing structurally is
    a stronger signal than one candidate's lower regression count alone."""
    specialist = _make_specialist()
    contract = _make_contract()
    candidates = [
        (_VARIANT_A, ["c1", "c2"]),           # candidate 0: consensus cluster, 2 regressions
        (_VARIANT_A_REWORDED, ["c1"]),        # candidate 1: consensus cluster, 1 regression (best in-cluster)
        (_VARIANT_B_DIFFERENT_FIELD, []),     # candidate 2: singleton, 0 regressions (best overall, but no consensus)
    ]
    winner, count_used = _run_best_of_n(specialist, contract, candidates, candidate_count=3)
    assert winner.models_py == _VARIANT_A_REWORDED, (
        "winner must be the best candidate WITHIN the largest consensus cluster, not the "
        "lowest-regression singleton outside it"
    )
    assert count_used == 3


def test_best_of_n_falls_back_to_regression_count_when_no_consensus():
    """No two candidates share a structural fingerprint -- unchanged prior behavior: plain
    fewest-regressions-wins across all attempts."""
    specialist = _make_specialist()
    contract = _make_contract()
    variant_c = (
        "class X(models.Model):\n    _name = 'x.x'\n    qty = fields.Integer()\n\n"
        "    def action_done(self):\n        pass\n"
    )
    candidates = [
        (_VARIANT_A, ["c1", "c2"]),
        (_VARIANT_B_DIFFERENT_FIELD, ["c1"]),
        (variant_c, []),
    ]
    winner, count_used = _run_best_of_n(specialist, contract, candidates, candidate_count=3)
    assert winner.models_py == variant_c, "with no consensus cluster, fewest-regressions-wins must still apply"
    assert count_used == 3
