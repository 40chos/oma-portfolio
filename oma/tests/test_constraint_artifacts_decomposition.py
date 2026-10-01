"""P13 item 4/10(a) (docs/planning/PHASE30_OMA_SEAM_COVERAGE_AND_COMBINATORIAL_HARDENING_
2026-07-29.md §22.2): tests for manager/replanning.py's decompose_into_constraints_with_artifacts()
and build_constraint_nodes(). Mocks call_structured() directly for the async extraction, pure
functions otherwise -- zero live LLM/GPU calls.
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from unittest.mock import AsyncMock, patch

from manager.replanning import (
    _ConstraintArtifacts,
    _ConstraintDecomposition,
    build_constraint_nodes,
    decompose_into_constraints_with_artifacts,
)


def test_decompose_with_artifacts_returns_real_creates_requires():
    """Real, reproducible bug found live (2026-08-07): decompose_into_constraints_with_artifacts()
    is the one existing cloud-eligible call site (allow_cloud_escalation=True in
    _run_decomposition_with_prompt()) -- when OMA_CLOUD_ESCALATION_ENABLED and a real API key are
    present in the environment (as they genuinely are in this project's own .env), the REAL
    infra.cloud_escalation.maybe_escalate_to_cloud() fires and answers with its own real cloud
    decomposition, silently bypassing the mocked call_structured() below entirely -- this test
    passed 'by luck' whenever cloud escalation happened to be disabled in whatever shell ran it,
    and failed deterministically (3/3 reruns, confirmed independently) whenever it wasn't.
    Mocking maybe_escalate_to_cloud to return None (the real 'declined/unavailable' shape,
    forcing fallthrough to the local call_structured() mock) makes this test hermetic regardless
    of environment -- this is exactly the discipline every OTHER cloud-eligible call site's own
    tests already need and some already have (see tests/test_manager_classify.py).
    """
    fake_result = _ConstraintDecomposition(constraints=[
        _ConstraintArtifacts(label="satisfaction_model", creates=["project.satisfaction"]),
        _ConstraintArtifacts(
            label="project_score_fields", creates=["project.project.score"],
            requires=["project.satisfaction"],
        ),
    ])

    async def run():
        with patch(
            "manager.replanning.call_structured", new=AsyncMock(return_value=fake_result),
        ), patch(
            "infra.cloud_escalation.maybe_escalate_to_cloud", new=AsyncMock(return_value=None),
        ):
            return await decompose_into_constraints_with_artifacts(
                "Add a satisfaction model and a score field.", client=None, model="x",
            )

    items = asyncio.run(run())
    assert [i.label for i in items] == ["satisfaction_model", "project_score_fields"]
    assert items[1].requires == ["project.satisfaction"]
    print("PASS: decompose_into_constraints_with_artifacts() returns real per-constraint creates/requires, hermetic regardless of real cloud-escalation env state")


def test_decompose_with_artifacts_never_returns_empty_list():
    fake_result = _ConstraintDecomposition(constraints=[])

    async def run():
        with patch(
            "manager.replanning.call_structured", new=AsyncMock(return_value=fake_result),
        ), patch(
            "infra.cloud_escalation.maybe_escalate_to_cloud", new=AsyncMock(return_value=None),
        ):
            return await decompose_into_constraints_with_artifacts("x", client=None, model="x")

    items = asyncio.run(run())
    assert len(items) == 1 and items[0].label == "primary_requirement"
    print("PASS: an empty extraction falls back to a single primary_requirement, never an empty list")


def test_decompose_with_artifacts_real_cloud_escalation_path_is_exercised_and_still_returns_real_shape():
    """The flip side of the bug above, tested directly rather than left implicit: when cloud
    escalation genuinely IS enabled and DOES fire, its own real JSON response still has to
    correctly override the local mock -- proving this call site's cloud path itself works,
    not just that tests can turn it off.
    """
    cloud_json = (
        '{"constraints": [{"label": "cloud_model", "creates": ["x.y"], "requires": []}]}'
    )

    async def run():
        with patch(
            "manager.replanning.call_structured",
            new=AsyncMock(side_effect=AssertionError("the local model must never be called once cloud escalation succeeds")),
        ), patch(
            "infra.cloud_escalation.maybe_escalate_to_cloud", new=AsyncMock(return_value=cloud_json),
        ):
            return await decompose_into_constraints_with_artifacts("goal", client=None, model="x")

    items = asyncio.run(run())
    assert [i.label for i in items] == ["cloud_model"]
    print("PASS: when cloud escalation genuinely fires, its own real response is used and the local model is never called")


def test_build_constraint_nodes_uses_real_overlap_when_available():
    items = [
        _ConstraintArtifacts(label="satisfaction_model", creates=["project.satisfaction"]),
        _ConstraintArtifacts(
            label="project_score_fields", creates=["project.project.score"],
            requires=["project.satisfaction"],
        ),
    ]
    nodes = build_constraint_nodes(items)
    assert nodes["project_score_fields"].predecessor_labels == ["satisfaction_model"]
    assert nodes["satisfaction_model"].predecessor_labels == []
    print("PASS: build_constraint_nodes() uses real creates/requires overlap when both nodes have signal")


def test_build_constraint_nodes_falls_back_to_tier_order_when_no_signal():
    # "scheduling_fields" and "product_scoping_relation" both carry no creates/requires --
    # must fall back to _dependency_tier_for_constraint_label()'s bucket order, not stay empty.
    items = [
        _ConstraintArtifacts(label="scheduling_fields"),
        _ConstraintArtifacts(label="product_scoping_relation"),
    ]
    nodes = build_constraint_nodes(items)
    # Both have empty creates/requires -- fallback compares real tier numbers.
    from manager.replanning import _dependency_tier_for_constraint_label
    tier_a = _dependency_tier_for_constraint_label("scheduling_fields")
    tier_b = _dependency_tier_for_constraint_label("product_scoping_relation")
    if tier_a < tier_b:
        assert nodes["product_scoping_relation"].predecessor_labels == ["scheduling_fields"]
    elif tier_b < tier_a:
        assert nodes["scheduling_fields"].predecessor_labels == ["product_scoping_relation"]
    else:
        assert nodes["scheduling_fields"].predecessor_labels == []
        assert nodes["product_scoping_relation"].predecessor_labels == []
    print("PASS: build_constraint_nodes() falls back to real tier-number precedence when no creates/requires exist")


def test_build_constraint_nodes_never_worse_than_tier_only_never_self_predecessor():
    items = [_ConstraintArtifacts(label="solo_constraint")]
    nodes = build_constraint_nodes(items)
    assert nodes["solo_constraint"].predecessor_labels == []
    print("PASS: a single constraint never lists itself as its own predecessor")


if __name__ == "__main__":
    test_decompose_with_artifacts_returns_real_creates_requires()
    test_decompose_with_artifacts_never_returns_empty_list()
    test_decompose_with_artifacts_real_cloud_escalation_path_is_exercised_and_still_returns_real_shape()
    test_build_constraint_nodes_uses_real_overlap_when_available()
    test_build_constraint_nodes_falls_back_to_tier_order_when_no_signal()
    test_build_constraint_nodes_never_worse_than_tier_only_never_self_predecessor()
    print("\nALL CONSTRAINT-ARTIFACTS DECOMPOSITION TESTS PASSED")
