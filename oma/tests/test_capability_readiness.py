"""P12 Tier S item 1 (docs/planning/PHASE30_ARCH_SYNTHESIS_CRITIQUE_FINAL_2026-07-30.md §4):
tests for manager/capability_readiness.py -- the "disable the route until it's real" gate for
capability classes with no real specialist handler (data_change, a permanent documented stub in
BuildSpecialist._run_data_change()). Pure, deterministic, zero live calls -- matches this
project's own established pattern for testing pre-flight gates directly (no DB, no LLM).
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from manager.capability_readiness import (
    CAPABILITY_READY,
    build_capability_not_ready_block,
    capability_class_is_ready,
)


def test_module_dev_is_ready():
    assert capability_class_is_ready("module_dev") is True
    print("PASS: module_dev is a real, ready capability class")


def test_readonly_is_ready():
    assert capability_class_is_ready("readonly") is True
    print("PASS: readonly is a real, ready capability class")


def test_data_change_is_not_ready():
    assert capability_class_is_ready("data_change") is False
    print("PASS: data_change is correctly flagged not-ready (permanent stub)")


def test_unrecognized_label_fails_open():
    assert capability_class_is_ready("some_future_capability_class") is True
    print("PASS: an unrecognized label fails open (never blocks on uncertainty)")


def test_registry_matches_real_capability_class_values():
    # Real values confirmed directly against contracts/schema.py's CapabilityClass enum --
    # module_development.value == "module_dev", readonly_investigation.value == "readonly",
    # data_change.value == "data_change". A mismatch here would silently make this whole gate
    # inert (the fast-path dict lookup would always miss, "fail open" every time).
    assert set(CAPABILITY_READY.keys()) == {"module_dev", "readonly", "data_change"}
    print("PASS: registry keys match the real CapabilityClass enum values exactly")


def test_block_shape_matches_governance_gate_convention():
    block = build_capability_not_ready_block("data_change")
    assert block["status"] == "paused"
    assert block["reason"] == "capability_not_ready"
    assert "data_change" in block["message"]
    assert block["capability_class_label"] == "data_change"
    # Same shape check_hard_governance_gates() returns -- task_id/correction_result are added by
    # the caller (manager/loop.py), not this function, matching that gate's own convention.
    assert "task_id" not in block
    print("PASS: pause block matches the established governance-gate shape/convention")


if __name__ == "__main__":
    test_module_dev_is_ready()
    test_readonly_is_ready()
    test_data_change_is_not_ready()
    test_unrecognized_label_fails_open()
    test_registry_matches_real_capability_class_values()
    test_block_shape_matches_governance_gate_convention()
    print("\nALL CAPABILITY-READINESS TESTS PASSED")
