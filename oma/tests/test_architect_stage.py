"""Phase 31 §1: tests for the architect-stage upgrade --
decompose_into_constraints_with_artifacts_biased(), critique_and_merge_constraint_drafts(), and
the orchestration entry point maybe_upgrade_decomposition_with_architect_stage(). No real model
gateway calls -- call_structured (and maybe_escalate_to_cloud where relevant) are mocked, same
discipline as tests/test_decompose_constraint_cap.py.
"""

import asyncio
import os
import sys
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from manager.replanning import (
    _ARCHITECT_STAGE_BIASES,
    _ConstraintArtifacts,
    _ConstraintDecomposition,
    critique_and_merge_constraint_drafts,
    decompose_into_constraints_with_artifacts_biased,
    maybe_upgrade_decomposition_with_architect_stage,
)


def _artifacts(*labels):
    return [_ConstraintArtifacts(label=label) for label in labels]


def test_biased_draft_rejects_unknown_bias():
    async def run():
        await decompose_into_constraints_with_artifacts_biased(
            "goal", client=None, model="x", bias="not_a_real_bias",
        )

    try:
        asyncio.run(run())
        assert False, "an unknown bias must raise, never silently no-op"
    except ValueError:
        pass
    print("PASS: an unknown architect-stage bias raises ValueError, never silently accepted")


def test_biased_draft_never_escalates_to_cloud():
    """Real, deliberate cost-bound: Stage A's drafts must stay local-only so the architect
    upgrade adds at most 2 extra local calls + 1 local critic call, never multiplying the one
    existing cloud-eligible baseline call site by 3.
    """
    async def run():
        with patch(
            "manager.replanning.call_structured",
            new=AsyncMock(return_value=_ConstraintDecomposition(constraints=_artifacts("a", "b"))),
        ), patch(
            "infra.cloud_escalation.maybe_escalate_to_cloud", new=AsyncMock(side_effect=AssertionError(
                "cloud escalation must never be attempted from a biased Stage A draft"
            )),
        ):
            return await decompose_into_constraints_with_artifacts_biased(
                "goal", client=None, model="x", bias="parallelism_precision",
            )

    labels = [item.label for item in asyncio.run(run())]
    assert labels == ["a", "b"]
    print("PASS: a biased Stage A draft never attempts cloud escalation")


def test_architect_orchestration_runs_both_biases_concurrently_and_uses_critic_result():
    call_labels_seen = []

    async def fake_call_structured(**kwargs):
        call_labels_seen.append(kwargs["call_label"])
        if "bias=parallelism_precision" in kwargs["call_label"]:
            return _ConstraintDecomposition(constraints=_artifacts("split_a", "split_b"))
        if "bias=dependency_conservatism" in kwargs["call_label"]:
            return _ConstraintDecomposition(constraints=_artifacts("combined_ab"))
        if "Critiquing and merging" in kwargs["call_label"]:
            return _ConstraintDecomposition(constraints=_artifacts("final_a", "final_b"))
        raise AssertionError(f"unexpected call_label: {kwargs['call_label']}")

    async def run():
        with patch("manager.replanning.call_structured", new=AsyncMock(side_effect=fake_call_structured)):
            return await maybe_upgrade_decomposition_with_architect_stage(
                "a goal with two real independent pieces",
                _artifacts("baseline_a", "baseline_b", "baseline_c"),
                client=None, model="x", task_id="t1",
            )

    result = asyncio.run(run())
    assert [item.label for item in result] == ["final_a", "final_b"], (
        f"the orchestrator must return the CRITIC's own choice, not a raw draft: got {result}"
    )
    assert any("bias=parallelism_precision" in c for c in call_labels_seen)
    assert any("bias=dependency_conservatism" in c for c in call_labels_seen)
    assert any("Critiquing and merging" in c for c in call_labels_seen)
    assert len(call_labels_seen) == 3, (
        f"exactly 2 biased drafts + 1 critic call, no more, no fewer: got {call_labels_seen}"
    )
    print("PASS: architect orchestration runs both biases (concurrently, via asyncio.gather) plus one critic call, and returns the critic's own merged result")


def test_critic_falls_back_to_baseline_draft_when_its_own_output_is_unusable():
    async def run():
        with patch(
            "manager.replanning.call_structured",
            # Empty labels -> _run_decomposition_with_prompt's own generic fallback shape.
            new=AsyncMock(return_value=_ConstraintDecomposition(constraints=[_ConstraintArtifacts(label="")])),
        ):
            return await critique_and_merge_constraint_drafts(
                "goal",
                [_artifacts("real_baseline_a", "real_baseline_b"), _artifacts("draft2_a")],
                client=None, model="x",
            )

    result = asyncio.run(run())
    assert [item.label for item in result] == ["real_baseline_a", "real_baseline_b"], (
        f"an unusable critic output must fall back to the real baseline draft, not a generic placeholder: got {result}"
    )
    print("PASS: critique_and_merge_constraint_drafts() falls back to the real baseline draft, never a generic placeholder, when its own output is unusable")


def test_architect_stage_biases_are_exactly_two_and_structurally_distinct():
    assert len(_ARCHITECT_STAGE_BIASES) == 2, _ARCHITECT_STAGE_BIASES
    assert len(set(_ARCHITECT_STAGE_BIASES)) == 2, "biases must be distinct, never duplicated"
    print("PASS: exactly 2 structurally-distinct biases are configured (Co-Scientist-style, not N-identical-prompts)")


if __name__ == "__main__":
    test_biased_draft_rejects_unknown_bias()
    test_biased_draft_never_escalates_to_cloud()
    test_architect_orchestration_runs_both_biases_concurrently_and_uses_critic_result()
    test_critic_falls_back_to_baseline_draft_when_its_own_output_is_unusable()
    test_architect_stage_biases_are_exactly_two_and_structurally_distinct()
    print("\nALL ARCHITECT-STAGE TESTS PASSED")
