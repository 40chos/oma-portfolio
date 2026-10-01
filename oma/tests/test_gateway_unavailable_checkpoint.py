"""P13 item 12c (docs/planning/PHASE30_OMA_SEAM_COVERAGE_AND_COMBINATORIAL_HARDENING_2026-07-29.md
§22.2): real, live-confirmed bug -- GatewayOutagePause's own message promised "I'll resume
automatically once the gateway is back," but no round_checkpoint memory event was ever written at
any gateway-outage pause site, so resume_task_after_checkpoint() always failed with "No
round_checkpoint found" (confirmed live, task_id 9aa73af4-..., OMA_LIVE_MODEL_AB_TEST_2026-07-31.md).

Tests manager/loop.py's new _write_gateway_unavailable_checkpoint() helper directly -- pure,
mocks append_project_memory(), zero LLM/GPU calls (the whole point of this fix is that it must NOT
require a fresh model call, since the gateway is, by definition, unavailable when this runs).
"""

import os
import sys
import uuid
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from contracts.schema import AutonomyTier, CapabilityClass, ReplanRound, SpecialistType, TaskContract, VerificationResult
from manager.compensations import TaskCutOffPause
from manager.gateway_orchestration import GatewayOutagePause
from manager.loop import _write_gateway_unavailable_checkpoint, _write_task_cut_off_checkpoint


def _make_contract() -> TaskContract:
    return TaskContract(
        task_id=uuid.uuid4(), specialist_type=SpecialistType.bug_fix,
        capability_class=CapabilityClass.module_development, tier=AutonomyTier.tier_2_notify_after,
        goal="Add field X", inputs=[], rules=[], deliverables=[],
        compensating_actions=[], validation_by="testing_qa", pause_if=[], turn_budget=10,
    )


def _make_round() -> ReplanRound:
    contract = _make_contract()
    verification = VerificationResult(
        task_id=uuid.uuid4(), passed=False, reproduction_confirmed=False,
        uncovered_paths=[], coverage_diff="", spot_check_mismatch=False,
        notes="a real prior round failure", regressed_constraints=[], root_cause=None,
    )
    return ReplanRound(
        round_number=1, previous_contract_task_id=str(contract.task_id),
        verification_result=verification, revision_reasoning="test round", new_contract=contract,
    )


def test_writes_a_real_round_checkpoint_event_without_any_llm_call():
    contract = _make_contract()
    pause = GatewayOutagePause(str(contract.task_id), "mod_lock", "gateway is unreachable")
    captured = {}

    def fake_append_project_memory(**kwargs):
        captured.update(kwargs)

    with patch("manager.loop.append_project_memory", side_effect=fake_append_project_memory):
        _write_gateway_unavailable_checkpoint(
            str(contract.task_id), "my_module", contract, [_make_round()], pause,
        )

    assert captured["event_type"] == "round_checkpoint"
    assert captured["tags"] == ["round_checkpoint"]
    assert captured["module"] == "my_module"
    detail = captured["detail"]
    assert detail["escalation_reason"] == "gateway_unavailable"
    assert detail["round_count"] == 1
    assert "human_summary" in detail and detail["human_summary"]
    assert detail["last_contract"]["task_id"] == str(contract.task_id)
    print("PASS: a real round_checkpoint event is written, with no LLM call needed to produce it")


def test_human_summary_is_plain_deterministic_text_not_an_llm_call():
    contract = _make_contract()
    pause = GatewayOutagePause(str(contract.task_id), "mod_lock", "gateway is unreachable")
    captured = {}

    def fake_append_project_memory(**kwargs):
        captured.update(kwargs)

    with patch("manager.loop.append_project_memory", side_effect=fake_append_project_memory):
        _write_gateway_unavailable_checkpoint(str(contract.task_id), None, contract, [], pause)

    human_summary = captured["detail"]["human_summary"]
    assert "0 round" in human_summary
    assert "resume" in human_summary.lower()
    print("PASS: human_summary is deterministic plain text, safe to build with zero rounds and no gateway call")


def test_task_cut_off_also_writes_a_real_round_checkpoint_event():
    """Real, confirmed gap found live (2026-08-17, immediately after the gateway-unavailable
    fix above was deployed): the SAME LLMRepetitionLoopExhaustedError can ALSO surface via
    TaskCutOffPause (after at least one real step already executed, compensations already run)
    -- this pause site never wrote a round_checkpoint either, identical bug, different pause
    reason. Confirms the sibling fix, _write_task_cut_off_checkpoint(), closes it the same way.
    """
    contract = _make_contract()
    pause = TaskCutOffPause(
        str(contract.task_id),
        "This task was cut off mid-sequence after 1 real step(s) had already executed. I ran "
        "the recorded compensating (undo) actions for all of them: 1 succeeded.",
        compensation_results=[],
    )
    captured = {}

    def fake_append_project_memory(**kwargs):
        captured.update(kwargs)

    with patch("manager.loop.append_project_memory", side_effect=fake_append_project_memory):
        _write_task_cut_off_checkpoint(
            str(contract.task_id), "my_module", contract, [_make_round()], pause,
        )

    assert captured["event_type"] == "round_checkpoint"
    assert captured["tags"] == ["round_checkpoint"]
    assert captured["module"] == "my_module"
    detail = captured["detail"]
    assert detail["escalation_reason"] == "task_cut_off"
    assert detail["round_count"] == 1
    assert "human_summary" in detail and detail["human_summary"]
    assert pause.message in detail["human_summary"], "the real compensation outcome must appear verbatim"
    assert detail["last_contract"]["task_id"] == str(contract.task_id)
    print("PASS: a real round_checkpoint event is written for a TaskCutOffPause too, closing the same class of bug")


if __name__ == "__main__":
    test_writes_a_real_round_checkpoint_event_without_any_llm_call()
    test_human_summary_is_plain_deterministic_text_not_an_llm_call()
    test_task_cut_off_also_writes_a_real_round_checkpoint_event()
    print("\nALL GATEWAY-UNAVAILABLE-CHECKPOINT TESTS PASSED")
