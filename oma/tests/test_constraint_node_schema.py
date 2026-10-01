"""P13 item 4 / item 7 (docs/planning/PHASE30_OMA_SEAM_COVERAGE_AND_COMBINATORIAL_HARDENING_2026-07-29.md
§22.2): tests for the additive ConstraintNode / FailureRecord / FailureCategory / ConstraintNodeState
schema on contracts/schema.py, and TaskContract's new constraint_nodes field. Pure pydantic
validation, zero LLM/GPU calls.
"""

import os
import sys
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from contracts.schema import (
    AutonomyTier,
    CapabilityClass,
    ConstraintNode,
    ConstraintNodeState,
    FailureCategory,
    FailureRecord,
    SpecialistType,
    TaskContract,
)


def _make_contract() -> TaskContract:
    return TaskContract(
        task_id=uuid.uuid4(),
        specialist_type=SpecialistType.bug_fix,
        capability_class=CapabilityClass.module_development,
        tier=AutonomyTier.tier_2_notify_after,
        goal="Add field X",
        inputs=[],
        rules=[],
        deliverables=[],
        compensating_actions=[],
        validation_by="testing_qa",
        pause_if=[],
        turn_budget=15,
    )


def test_constraint_node_defaults_are_empty_never_guessed():
    node = ConstraintNode(label="scheduling_fields")
    assert node.creates == []
    assert node.requires == []
    assert node.predecessor_labels == []
    assert node.tier == 0
    assert node.state == ConstraintNodeState.pending
    assert node.round_number == 0
    assert node.failure_records == []
    print("PASS: a bare ConstraintNode defaults every list/optional field to empty")


def test_failure_record_defaults_to_unclassified_and_judge_source():
    record = FailureRecord(round_number=1)
    assert record.failure_category == FailureCategory.unclassified
    assert record.source == "judge"
    assert record.constraint_label is None
    print("PASS: a bare FailureRecord defaults to unclassified/judge, never guesses a category")


def test_business_rule_mismatch_category_exists():
    # 2026-07-31 integration-coherence pass addition -- closes the item 12a routing gap.
    record = FailureRecord(
        round_number=2, failure_category=FailureCategory.business_rule_mismatch,
        location="models.py:88", observed="discount capped at 10% per rule R-4",
        concrete_alternative="clamp discount to 0.10 before write", source="testing_qa",
    )
    assert record.failure_category == FailureCategory.business_rule_mismatch
    print("PASS: FailureCategory.business_rule_mismatch is a real, constructible value")


def test_constraint_node_carries_failure_records():
    record = FailureRecord(round_number=1, failure_category=FailureCategory.scope_violation)
    node = ConstraintNode(label="double_booking_rule", failure_records=[record])
    assert len(node.failure_records) == 1
    assert node.failure_records[0].failure_category == FailureCategory.scope_violation
    print("PASS: ConstraintNode.failure_records carries real FailureRecord objects")


def test_task_contract_constraint_nodes_defaults_empty_and_round_trips():
    contract = _make_contract()
    assert contract.constraint_nodes == {}
    node = ConstraintNode(label="add_field_x", creates=["product.template.field_x"])
    updated = contract.model_copy(update={"constraint_nodes": {"add_field_x": node}})
    assert updated.constraint_nodes["add_field_x"].creates == ["product.template.field_x"]
    # round-trip through the same serialize/validate path a Postgres jsonb checkpoint uses
    dumped = updated.model_dump()
    reloaded = TaskContract.model_validate(dumped)
    assert reloaded.constraint_nodes["add_field_x"].creates == ["product.template.field_x"]
    print("PASS: TaskContract.constraint_nodes defaults empty and round-trips through dump/validate")


def test_round_timings_accepts_the_new_three_level_resume_indexed_shape():
    # Real, confirmed bug found live (2026-08-09, task 07141af5's flagship run): the type
    # annotation was never updated when manager.tools.persist_node_round_timing() added the
    # resume_index nesting level the same day, breaking loading of any task with real
    # round-timing data recorded after that fix shipped.
    node = ConstraintNode(
        label="x", round_timings={"1": {"0": {"qa": 404.5, "build": 200.0, "total": 1269.3}}},
    )
    assert node.round_timings["1"]["0"]["qa"] == 404.5
    print("PASS: round_timings accepts the new {round: {resume_index: {actor: seconds}}} shape")


def test_round_timings_self_heals_legacy_flat_rows_at_load_time():
    # Follow-up real bug found immediately after fixing the above (same live resume):
    # manager.tools.persist_node_round_timing()'s own _migrate() only upgrades a round's legacy
    # flat {actor: seconds} shape the NEXT time that exact round is written to -- rows for other
    # rounds/nodes untouched since the nesting fix shipped stay in the old flat shape, so real
    # production data is a genuine mix of both shapes at once, not a one-time cutover.
    node = ConstraintNode(
        label="x",
        round_timings={
            "1": {"build": 136.4, "round_total": 442.3},  # legacy flat shape
            "2": {"0": {"qa": 404.5, "total": 1269.3}},  # already-migrated new shape
            "3": {},  # empty round, must stay empty, not wrapped
        },
    )
    assert node.round_timings["1"] == {"0": {"build": 136.4, "round_total": 442.3}}
    assert node.round_timings["2"] == {"0": {"qa": 404.5, "total": 1269.3}}
    assert node.round_timings["3"] == {}
    print("PASS: round_timings self-heals a legacy flat row into the new nested shape at load time")


def test_round_timings_flattens_arbitrarily_deep_accumulated_nesting():
    # Real, confirmed follow-up bug found live (2026-08-09, same task, immediately after the
    # first round_timings fix above): some rows accumulated a SECOND extra nesting level over
    # time ("round_timings.1.0.68" -- four real levels), not just the single level the first
    # fix handled. round_timings is a pure UI/display convenience (confirmed: nothing outside
    # serialization reads it for a correctness decision), so this must tolerate arbitrary
    # accumulated drift depth, not just the two shapes seen so far.
    node = ConstraintNode(
        label="x", round_timings={"1": {"0": {"68": {"qa": 42.18, "total": 235.71}}}},
    )
    assert node.round_timings == {"1": {"0.68": {"qa": 42.18, "total": 235.71}}}
    # A truly empty round must stay empty, not get wrapped into a spurious bucket.
    empty_node = ConstraintNode(label="x", round_timings={"3": {}})
    assert empty_node.round_timings == {"3": {}}
    # Real, valid multiple sibling resume_index buckets at the correct depth must pass through
    # completely untouched.
    multi_node = ConstraintNode(
        label="x", round_timings={"1": {"0": {"qa": 1.0}, "65": {"build": 2.0}}},
    )
    assert multi_node.round_timings == {"1": {"0": {"qa": 1.0}, "65": {"build": 2.0}}}
    print("PASS: round_timings flattens arbitrarily deep accumulated nesting drift, "
          "keeps empty rounds empty, and leaves already-correct multi-resume data untouched")


if __name__ == "__main__":
    test_constraint_node_defaults_are_empty_never_guessed()
    test_failure_record_defaults_to_unclassified_and_judge_source()
    test_business_rule_mismatch_category_exists()
    test_constraint_node_carries_failure_records()
    test_task_contract_constraint_nodes_defaults_empty_and_round_trips()
    test_round_timings_accepts_the_new_three_level_resume_indexed_shape()
    test_round_timings_self_heals_legacy_flat_rows_at_load_time()
    test_round_timings_flattens_arbitrarily_deep_accumulated_nesting()
    print("\nALL CONSTRAINT-NODE SCHEMA TESTS PASSED")
