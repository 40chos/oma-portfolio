"""P13 item 4 / item 10(a): tests for contracts/constraint_graph.py's pure, zero-LLM
creates/requires overlap -> predecessor_labels derivation, and the tier-fallback detector.
No mocks needed -- deterministic pure functions.

P13 item 11, Unit 2 (added 2026-08-01): tests for not_yet_built_artifacts()/
is_expected_incomplete_finding() -- the join-suppression fix's own pure, zero-LLM half.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from contracts.constraint_graph import (
    collapse_cycle_to_tier_chain,
    derive_predecessor_labels_from_overlap,
    detect_cycle,
    is_expected_incomplete_finding,
    labels_needing_tier_fallback,
    not_yet_built_artifacts,
)
from contracts.schema import ConstraintNode


def test_no_overlap_means_no_predecessors():
    nodes = {
        "add_field_a": ConstraintNode(label="add_field_a", creates=["model.field_a"]),
        "add_field_b": ConstraintNode(label="add_field_b", creates=["model.field_b"]),
    }
    result = derive_predecessor_labels_from_overlap(nodes)
    assert result["add_field_a"] == []
    assert result["add_field_b"] == []
    print("PASS: two constraints with disjoint creates/requires have no predecessors")


def test_real_requires_creates_overlap_produces_a_predecessor_edge():
    # satisfaction_model / project_score_fields -- the exact real example item 4 names.
    nodes = {
        "satisfaction_model": ConstraintNode(
            label="satisfaction_model", creates=["project.satisfaction"],
        ),
        "project_score_fields": ConstraintNode(
            label="project_score_fields",
            creates=["project.project.score"],
            requires=["project.satisfaction"],
        ),
    }
    result = derive_predecessor_labels_from_overlap(nodes)
    assert result["project_score_fields"] == ["satisfaction_model"]
    assert result["satisfaction_model"] == []
    print("PASS: a real requires/creates overlap produces a real predecessor edge, not a tier guess")


def test_empty_requires_never_guesses_a_predecessor():
    nodes = {
        "a": ConstraintNode(label="a", creates=["x"]),
        "b": ConstraintNode(label="b", creates=["y"]),  # requires deliberately empty
    }
    result = derive_predecessor_labels_from_overlap(nodes)
    assert result["b"] == []
    print("PASS: a node with empty requires gets an empty predecessor list, never guessed")


def test_labels_needing_tier_fallback_only_flags_fully_empty_nodes():
    nodes = {
        "has_creates": ConstraintNode(label="has_creates", creates=["x"]),
        "has_requires": ConstraintNode(label="has_requires", requires=["x"]),
        "fully_empty": ConstraintNode(label="fully_empty"),
    }
    fallback = labels_needing_tier_fallback(nodes)
    assert fallback == ["fully_empty"]
    print("PASS: only nodes with BOTH creates and requires empty are flagged for tier fallback")


def test_self_reference_is_never_its_own_predecessor():
    nodes = {
        "a": ConstraintNode(label="a", creates=["x"], requires=["x"]),
    }
    result = derive_predecessor_labels_from_overlap(nodes)
    assert result["a"] == []
    print("PASS: a node never lists itself as its own predecessor even if creates/requires overlap")


def test_not_yet_built_artifacts_excludes_only_satisfied_labels():
    # The real 0689823f-... worked example's own shape: access_rights (constraint 4 of 6) hasn't
    # run yet, its creates includes the access-rule artifact for project.satisfaction.
    nodes = {
        "satisfaction_model": ConstraintNode(label="satisfaction_model", creates=["project.satisfaction"]),
        "access_rights": ConstraintNode(label="access_rights", creates=["project.satisfaction.access"]),
    }
    status = {"satisfaction_model": "satisfied", "access_rights": "pending"}
    result = not_yet_built_artifacts(nodes, status)
    assert result == {"project.satisfaction.access"}
    print("PASS: only the not-yet-satisfied label's own creates artifacts are considered not-yet-built")


def test_not_yet_built_artifacts_treats_absent_status_as_unresolved():
    nodes = {"x": ConstraintNode(label="x", creates=["m.field"])}
    result = not_yet_built_artifacts(nodes, {})  # never attempted, not in constraint_status at all
    assert result == {"m.field"}
    print("PASS: a label absent from constraint_status entirely is treated as not-yet-built, never as satisfied")


def test_not_yet_built_artifacts_empty_when_everything_satisfied():
    nodes = {"x": ConstraintNode(label="x", creates=["m.field"])}
    result = not_yet_built_artifacts(nodes, {"x": "satisfied"})
    assert result == set()
    print("PASS: a fully-satisfied graph produces an empty not-yet-built set")


def test_is_expected_incomplete_finding_true_when_fully_covered():
    not_yet_built = {"project.satisfaction"}
    assert is_expected_incomplete_finding(["project.satisfaction"], not_yet_built) is True
    print("PASS: a finding whose sole artifact is claimed by an unresolved sibling is expected-incomplete")


def test_is_expected_incomplete_finding_false_when_partially_covered():
    # Real, deliberately conservative behavior: even ONE artifact not explained by an unresolved
    # sibling keeps the whole finding a genuine, blocking failure -- never partial suppression.
    not_yet_built = {"project.satisfaction"}
    assert is_expected_incomplete_finding(["project.satisfaction", "some.other.model"], not_yet_built) is False
    print("PASS: a finding naming even one artifact NOT claimed by any unresolved sibling stays blocking")


def test_is_expected_incomplete_finding_false_for_empty_artifact_list():
    # A finding with no parsed artifact names at all must never be suppressed -- there is nothing
    # concrete to justify downgrading it, matching the "never guessed" discipline throughout.
    assert is_expected_incomplete_finding([], {"anything"}) is False
    print("PASS: a finding with no named artifacts is never suppressed, regardless of what's unresolved")


def test_is_expected_incomplete_finding_false_when_nothing_unresolved():
    assert is_expected_incomplete_finding(["project.satisfaction"], set()) is False
    print("PASS: with an empty not-yet-built set (e.g. no constraint_nodes at all), nothing is ever suppressed")


def test_detect_cycle_returns_empty_for_a_dag():
    nodes = {
        "a": ConstraintNode(label="a", predecessor_labels=[]),
        "b": ConstraintNode(label="b", predecessor_labels=["a"]),
        "c": ConstraintNode(label="c", predecessor_labels=["a", "b"]),
    }
    assert detect_cycle(nodes) == []
    print("PASS: a real DAG produces no cycles")


def test_detect_cycle_finds_a_real_three_node_cycle():
    nodes = {
        "a": ConstraintNode(label="a", predecessor_labels=["c"]),
        "b": ConstraintNode(label="b", predecessor_labels=["a"]),
        "c": ConstraintNode(label="c", predecessor_labels=["b"]),
    }
    sccs = detect_cycle(nodes)
    assert len(sccs) == 1
    assert set(sccs[0]) == {"a", "b", "c"}
    print("PASS: a real a->b->c->a cycle is found as one SCC")


def test_detect_cycle_finds_all_sccs_in_one_pass():
    # Two independent cycles plus one unrelated acyclic node -- both cycles must surface from a
    # single detect_cycle() call, not require a loop-until-acyclic wrapper.
    nodes = {
        "a": ConstraintNode(label="a", predecessor_labels=["b"]),
        "b": ConstraintNode(label="b", predecessor_labels=["a"]),
        "x": ConstraintNode(label="x", predecessor_labels=["y"]),
        "y": ConstraintNode(label="y", predecessor_labels=["x"]),
        "z": ConstraintNode(label="z", predecessor_labels=[]),
    }
    sccs = detect_cycle(nodes)
    assert len(sccs) == 2
    scc_sets = {frozenset(scc) for scc in sccs}
    assert frozenset({"a", "b"}) in scc_sets
    assert frozenset({"x", "y"}) in scc_sets
    print("PASS: two independent cycles both surface from one detect_cycle() call")


def test_detect_cycle_ignores_a_stale_predecessor_label_not_in_nodes():
    nodes = {
        "a": ConstraintNode(label="a", predecessor_labels=["does_not_exist"]),
    }
    assert detect_cycle(nodes) == []
    print("PASS: a predecessor_labels entry with no matching node is silently skipped, never guessed")


def test_collapse_cycle_to_tier_chain_makes_the_graph_acyclic():
    nodes = {
        "a": ConstraintNode(label="a", predecessor_labels=["c"]),
        "b": ConstraintNode(label="b", predecessor_labels=["a"]),
        "c": ConstraintNode(label="c", predecessor_labels=["b"]),
    }
    cyclic_labels = detect_cycle(nodes)[0]
    collapse_cycle_to_tier_chain(nodes, cyclic_labels)
    assert detect_cycle(nodes) == [], "collapse must leave a real DAG behind"
    print("PASS: collapse_cycle_to_tier_chain() breaks a real cycle into an acyclic chain")


def test_collapse_cycle_to_tier_chain_leaves_edges_outside_the_cycle_untouched():
    nodes = {
        "a": ConstraintNode(label="a", predecessor_labels=["c", "external"]),
        "b": ConstraintNode(label="b", predecessor_labels=["a"]),
        "c": ConstraintNode(label="c", predecessor_labels=["b"]),
        "external": ConstraintNode(label="external", predecessor_labels=[]),
    }
    cyclic_labels = detect_cycle(nodes)[0]
    collapse_cycle_to_tier_chain(nodes, cyclic_labels)
    assert "external" in nodes["a"].predecessor_labels, (
        "an edge to a label outside the cycle must survive collapse untouched"
    )
    print("PASS: collapse_cycle_to_tier_chain() only strips intra-cycle edges, never edges to outside labels")


if __name__ == "__main__":
    test_no_overlap_means_no_predecessors()
    test_real_requires_creates_overlap_produces_a_predecessor_edge()
    test_empty_requires_never_guesses_a_predecessor()
    test_labels_needing_tier_fallback_only_flags_fully_empty_nodes()
    test_self_reference_is_never_its_own_predecessor()
    test_not_yet_built_artifacts_excludes_only_satisfied_labels()
    test_not_yet_built_artifacts_treats_absent_status_as_unresolved()
    test_not_yet_built_artifacts_empty_when_everything_satisfied()
    test_is_expected_incomplete_finding_true_when_fully_covered()
    test_is_expected_incomplete_finding_false_when_partially_covered()
    test_is_expected_incomplete_finding_false_for_empty_artifact_list()
    test_is_expected_incomplete_finding_false_when_nothing_unresolved()
    test_detect_cycle_returns_empty_for_a_dag()
    test_detect_cycle_finds_a_real_three_node_cycle()
    test_detect_cycle_finds_all_sccs_in_one_pass()
    test_detect_cycle_ignores_a_stale_predecessor_label_not_in_nodes()
    test_collapse_cycle_to_tier_chain_makes_the_graph_acyclic()
    test_collapse_cycle_to_tier_chain_leaves_edges_outside_the_cycle_untouched()
    print("\nALL CONSTRAINT-GRAPH DERIVATION TESTS PASSED")
