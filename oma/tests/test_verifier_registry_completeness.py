"""Phase 25E (2026-07-26): verifier systematization.

Cross-checks contracts/verifier_registry.py's HALLUCINATION_FILTER_
REGISTRY against the REAL function definitions in code_review/
specialist.py and testing_qa/specialist.py, by introspecting the
modules directly (not by re-reading source text) -- so this test fails
the moment a new `_filter_hallucinated_*_findings` or `_exempt_*_from_
coverage_gap` function is added to either file without a matching
registry entry, closing exactly the gap this project's own history
kept re-creating (9 filters + 3 exemptions, each found live, one at a
time, none tracked anywhere as a set, until this phase).

Also unit-tests the reproduction-shape classifier/gap-flag with real
goal text shapes from this session's own tasks (verified shapes:
field/button-group; unverified shapes: onchange/selection/ir.cron),
confirming the flag is purely informational (never flips passed).
"""

import inspect
import os
import re
import sys
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import specialists.code_review.specialist as code_review_module
import specialists.testing_qa.specialist as testing_qa_module
from contracts.schema import AutonomyTier, CapabilityClass, SpecialistType, TaskContract
from contracts.verifier_registry import (
    HALLUCINATION_FILTER_REGISTRY,
    REPRODUCTION_SHAPE_REGISTRY,
    classify_targeted_shapes,
    known_shape_ids,
    registered_filter_ids,
    unverified_shapes_targeted,
)

_FILTER_NAME_RE = re.compile(r"^_filter_hallucinated_.*_findings$")
_EXEMPT_NAME_RE = re.compile(r"^_exempt_verified_.*_from_coverage_gap$")


def _real_function_names(module, name_re) -> set[str]:
    return {
        name
        for name, obj in inspect.getmembers(module)
        if inspect.isfunction(obj) and name_re.match(name)
    }


def test_every_real_hallucination_filter_function_is_registered():
    real_filters = _real_function_names(code_review_module, _FILTER_NAME_RE)
    registered_functions = {
        entry.function_name for entry in HALLUCINATION_FILTER_REGISTRY
        if entry.module == "specialists.code_review.specialist"
    }
    missing = real_filters - registered_functions
    assert not missing, (
        f"found _filter_hallucinated_*_findings function(s) in code_review/specialist.py "
        f"with NO entry in HALLUCINATION_FILTER_REGISTRY: {missing} -- register them in "
        f"contracts/verifier_registry.py"
    )
    stale = registered_functions - real_filters
    assert not stale, (
        f"HALLUCINATION_FILTER_REGISTRY references function(s) that no longer exist in "
        f"code_review/specialist.py: {stale}"
    )
    print(f"PASS: all {len(real_filters)} real code-review hallucination filters are registered")


def test_every_real_coverage_exemption_function_is_registered():
    real_exemptions = _real_function_names(testing_qa_module, _EXEMPT_NAME_RE)
    registered_functions = {
        entry.function_name for entry in HALLUCINATION_FILTER_REGISTRY
        if entry.module == "specialists.testing_qa.specialist"
    }
    missing = real_exemptions - registered_functions
    assert not missing, (
        f"found _exempt_verified_*_from_coverage_gap function(s) in testing_qa/specialist.py "
        f"with NO entry in HALLUCINATION_FILTER_REGISTRY: {missing} -- register them in "
        f"contracts/verifier_registry.py"
    )
    stale = registered_functions - real_exemptions
    assert not stale, (
        f"HALLUCINATION_FILTER_REGISTRY references function(s) that no longer exist in "
        f"testing_qa/specialist.py: {stale}"
    )
    print(f"PASS: all {len(real_exemptions)} real coverage-gap exemptions are registered")


def test_registry_ids_are_unique():
    ids = [entry.filter_id for entry in HALLUCINATION_FILTER_REGISTRY]
    assert len(ids) == len(set(ids)), f"duplicate filter_id(s) in HALLUCINATION_FILTER_REGISTRY: {ids}"
    shape_ids = [entry.shape_id for entry in REPRODUCTION_SHAPE_REGISTRY]
    assert len(shape_ids) == len(set(shape_ids)), f"duplicate shape_id(s) in REPRODUCTION_SHAPE_REGISTRY: {shape_ids}"
    print("PASS: registry ids are unique")


def _make_contract(goal: str) -> TaskContract:
    return TaskContract(
        task_id=uuid.uuid4(),
        specialist_type=SpecialistType.bug_fix,
        capability_class=CapabilityClass.readonly_investigation,
        tier=AutonomyTier.tier_1_readonly,
        goal=goal,
        inputs=[],
        rules=[],
        deliverables=[],
        compensating_actions=[],
        validation_by="testing_qa",
        pause_if=[],
        turn_budget=10,
    )


def test_classifier_detects_a_verified_shape_field_existence():
    contract = _make_contract("I want to see a text field on the lead form called 'Notes'.\n\nModule: crm\nModel: crm.lead\nField: notes (Text)\n")
    shapes = classify_targeted_shapes(contract)
    assert "orm_field_existence" in shapes
    assert unverified_shapes_targeted(contract) == [], "a plain field-add goal should have no unverified-shape flags"
    print("PASS: field-existence goal classified as verified, no gap flag")


def test_classifier_flags_onchange_as_unverified():
    contract = _make_contract("When I select a customer, auto-fill the salesperson field via @api.onchange.\n\nModule: sale\nModel: sale.order\n")
    unverified = unverified_shapes_targeted(contract)
    assert "onchange_behavior" in unverified, f"expected onchange_behavior to be flagged unverified, got {unverified}"
    print(f"PASS: onchange goal flagged unverified: {unverified}")


def test_classifier_flags_ir_cron_as_unverified():
    contract = _make_contract("Add a scheduled action (ir.cron) that runs nightly to archive old leads.\n\nModule: crm\n")
    unverified = unverified_shapes_targeted(contract)
    assert "ir_cron_schedule" in unverified, f"expected ir_cron_schedule to be flagged unverified, got {unverified}"
    print(f"PASS: ir.cron goal flagged unverified: {unverified}")


def test_classifier_treats_button_group_restriction_as_verified():
    contract = _make_contract("Restrict the 'Send to customer' button to users in the group base.group_system.\n\nModule: sale\n")
    shapes = classify_targeted_shapes(contract)
    assert "button_group_restriction" in shapes
    assert "button_group_restriction" not in unverified_shapes_targeted(contract)
    print("PASS: button-group-restriction goal (fix 40's own shape) classified as verified")


def test_known_shape_ids_covers_registry():
    assert known_shape_ids() == {entry.shape_id for entry in REPRODUCTION_SHAPE_REGISTRY}
    print("PASS: known_shape_ids() matches the registry")


if __name__ == "__main__":
    test_every_real_hallucination_filter_function_is_registered()
    test_every_real_coverage_exemption_function_is_registered()
    test_registry_ids_are_unique()
    test_classifier_detects_a_verified_shape_field_existence()
    test_classifier_flags_onchange_as_unverified()
    test_classifier_flags_ir_cron_as_unverified()
    test_classifier_treats_button_group_restriction_as_verified()
    test_known_shape_ids_covers_registry()
    print("\nALL VERIFIER REGISTRY TESTS PASSED")
