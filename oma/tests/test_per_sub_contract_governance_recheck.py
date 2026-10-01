"""P12 Tier A item 23 (docs/planning/PHASE30_ARCH_SYNTHESIS_CRITIQUE_FINAL_2026-07-30.md §4):
tests for the new per-sub-contract re-check of check_hard_governance_gates() inside
_run_constraint_labels_from() -- real, confirmed gap: this hard, deterministic pre-flight gate
only ever ran once, at run_turn()'s own intake, against the whole original goal text. A later
sub-contract's own narrowed goal_text is genuinely different text that could independently
match a governance rule's predicate even when the original goal never did. Mocks
check_hard_governance_gates() directly (patched into manager.loop's own imported name) rather
than depending on whatever real governance rules currently exist in Postgres -- deterministic,
zero LLM/GPU calls, and _execute_contract() is also mocked to detect whether Build would have
been wrongly reached.
"""

import asyncio
import os
import sys
import uuid
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from contracts.schema import AutonomyTier, CapabilityClass, SpecialistType, TaskContract
from manager.loop import _run_constraint_labels_from


def _make_decomposable_contract() -> TaskContract:
    return TaskContract(
        task_id=uuid.uuid4(), specialist_type=SpecialistType.bug_fix,
        capability_class=CapabilityClass.module_development, tier=AutonomyTier.tier_2_notify_after,
        goal="Add field A; add field B.", inputs=[], rules=[], deliverables=[],
        compensating_actions=[], validation_by="testing_qa", pause_if=[], turn_budget=10,
        constraint_status={},
    )


def test_a_sub_contract_matching_a_governance_rule_is_blocked_before_reaching_build():
    contract = _make_decomposable_contract()
    labels = ["constraint_a", "constraint_b"]
    satisfied = {"constraint_a": "pending", "constraint_b": "pending"}

    fake_block = {
        "status": "paused", "reason": "governance_rule_blocked",
        "message": "This matches a known, active governance rule.",
        "matched_rule_id": 42,
    }

    build_reached = []

    async def fake_execute_contract(sub_contract, *args, **kwargs):
        build_reached.append(sub_contract.current_constraint_label)
        return {"status": "completed", "passed": True, "rounds_taken": 1, "task_id": str(sub_contract.task_id)}

    async def run():
        with patch("manager.loop.check_hard_governance_gates", return_value=fake_block):
            with patch("manager.loop._execute_contract", new=AsyncMock(side_effect=fake_execute_contract)):
                with patch("manager.loop.clear_task_state"):
                    return await _run_constraint_labels_from(
                        contract, labels, satisfied, 0,
                        "module_lock", client=None, classifier_model="x", correction_result={},
                        module_for_repeat_check=None, memory_block="", constitution_text="",
                        sensitive_paths_raw=[],
                    )

    result = asyncio.run(run())
    assert result["status"] == "paused"
    assert result["reason"] == "governance_rule_blocked"
    assert result["matched_rule_id"] == 42
    assert build_reached == [], "Build must never be reached at all once the governance gate blocks a sub-contract"
    print("PASS: a sub-contract whose own narrowed goal_text matches a governance rule is blocked BEFORE Build is ever reached")


def test_a_clean_sub_contract_proceeds_normally():
    contract = _make_decomposable_contract()
    labels = ["constraint_a", "constraint_b"]
    satisfied = {"constraint_a": "pending", "constraint_b": "pending"}
    build_reached = []

    async def fake_execute_contract(sub_contract, *args, **kwargs):
        build_reached.append(sub_contract.current_constraint_label)
        return {"status": "completed", "passed": True, "rounds_taken": 1, "task_id": str(sub_contract.task_id)}

    async def run():
        with patch("manager.loop.check_hard_governance_gates", return_value=None):
            with patch("manager.loop._execute_contract", new=AsyncMock(side_effect=fake_execute_contract)):
                with patch("manager.loop.clear_task_state"):
                    return await _run_constraint_labels_from(
                        contract, labels, satisfied, 0,
                        "module_lock", client=None, classifier_model="x", correction_result={},
                        module_for_repeat_check=None, memory_block="", constitution_text="",
                        sensitive_paths_raw=[],
                    )

    result = asyncio.run(run())
    assert result["passed"] is True
    assert build_reached == ["constraint_a", "constraint_b"]
    print("PASS: a clean sub-contract (no governance match) proceeds through both constraints exactly as before")


def test_first_constraint_blocked_never_touches_the_second():
    contract = _make_decomposable_contract()
    labels = ["constraint_a", "constraint_b"]
    satisfied = {"constraint_a": "pending", "constraint_b": "pending"}
    build_reached = []
    call_count = {"n": 0}

    def fake_governance_check(goal_text):
        call_count["n"] += 1
        return {"status": "paused", "reason": "governance_rule_blocked", "message": "blocked", "matched_rule_id": 1}

    async def fake_execute_contract(sub_contract, *args, **kwargs):
        build_reached.append(sub_contract.current_constraint_label)
        return {"status": "completed", "passed": True, "rounds_taken": 1, "task_id": str(sub_contract.task_id)}

    async def run():
        with patch("manager.loop.check_hard_governance_gates", side_effect=fake_governance_check):
            with patch("manager.loop._execute_contract", new=AsyncMock(side_effect=fake_execute_contract)):
                with patch("manager.loop.clear_task_state"):
                    return await _run_constraint_labels_from(
                        contract, labels, satisfied, 0,
                        "module_lock", client=None, classifier_model="x", correction_result={},
                        module_for_repeat_check=None, memory_block="", constitution_text="",
                        sensitive_paths_raw=[],
                    )

    result = asyncio.run(run())
    assert call_count["n"] == 1, "the loop must stop entirely on the first blocked sub-contract, never even check the second"
    assert build_reached == []
    print("PASS: the loop stops entirely on the first governance-blocked sub-contract, never checks or builds the next one")


def test_known_risk_hint_noise_never_reaches_the_governance_check():
    """Real, live-confirmed bug (2026-08-08, Phase 31 §9 Phase B concurrency experiment):
    contract.known_risk_hint carries a VERBATIM prior-round failure summary, which can itself
    contain arbitrary, unrelated diagnostic noise (a real install log tail quoting Odoo's own
    internal warnings about a completely different module, e.g. "account.bank.statement.line:
    inconsistent compute_sudo..."). Confirmed live: this exact substring, inherited from an
    earlier, unrelated attempt's own install-failure log, tripped the "never touch the
    accounting module" governance rule for a task that never once mentioned accounting. Fixed:
    the risk-hint contribution is stripped back out of the text handed to
    check_hard_governance_gates(), so its own incidental content can never trigger a false block.
    """
    contract = _make_decomposable_contract()
    contract = contract.model_copy(update={
        "known_risk_hint": (
            "Known risk for this module: a prior round's own install failed with a log tail "
            "mentioning account.bank.statement.line: inconsistent compute_sudo for computed fields."
        ),
    })
    labels = ["constraint_a"]
    satisfied = {"constraint_a": "pending"}

    seen_governance_check_texts = []

    def real_shaped_governance_check(goal_text):
        seen_governance_check_texts.append(goal_text)
        import re
        if re.search(r"\baccount\.[a-z_]+\b", goal_text, re.IGNORECASE):
            return {
                "status": "paused", "reason": "governance_rule_blocked",
                "message": "This matches a known, active governance rule.", "matched_rule_id": 1159,
            }
        return None

    async def fake_execute_contract(sub_contract, *args, **kwargs):
        return {"status": "completed", "passed": True, "rounds_taken": 1, "task_id": str(sub_contract.task_id)}

    async def run():
        with patch("manager.loop.check_hard_governance_gates", side_effect=real_shaped_governance_check):
            with patch("manager.loop._execute_contract", new=AsyncMock(side_effect=fake_execute_contract)):
                with patch("manager.loop.clear_task_state"):
                    return await _run_constraint_labels_from(
                        contract, labels, satisfied, 0,
                        "module_lock", client=None, classifier_model="x", correction_result={},
                        module_for_repeat_check=None, memory_block="", constitution_text="",
                        sensitive_paths_raw=[],
                    )

    result = asyncio.run(run())
    assert result["passed"] is True, f"the risk-hint's own incidental 'account.*' text must never block this round: {result}"
    assert seen_governance_check_texts, "the governance check must still have been called"
    for text in seen_governance_check_texts:
        assert "account.bank.statement.line" not in text, (
            f"the known_risk_hint's own content must never reach check_hard_governance_gates(): {text!r}"
        )
    print("PASS: known_risk_hint's own incidental diagnostic noise is stripped before the governance re-check, never causing a false block")


def test_a_genuine_match_in_the_narrowed_goal_text_itself_still_blocks():
    """The other half of the same fix: stripping the risk-hint contribution must not weaken the
    real check -- a genuine governance match anywhere else in the narrowed goal_text (the focus
    label, the original goal text, etc.) must still block exactly as before.
    """
    contract = _make_decomposable_contract()
    contract = contract.model_copy(update={
        "goal": "Add an account.move integration field; add field B.",
        "known_risk_hint": "Known risk for this module: an unrelated prior warning about product.template.",
    })
    labels = ["constraint_a"]
    satisfied = {"constraint_a": "pending"}

    def real_shaped_governance_check(goal_text):
        import re
        if re.search(r"\baccount\.[a-z_]+\b", goal_text, re.IGNORECASE):
            return {
                "status": "paused", "reason": "governance_rule_blocked",
                "message": "This matches a known, active governance rule.", "matched_rule_id": 1159,
            }
        return None

    build_reached = []

    async def fake_execute_contract(sub_contract, *args, **kwargs):
        build_reached.append(sub_contract.current_constraint_label)
        return {"status": "completed", "passed": True, "rounds_taken": 1, "task_id": str(sub_contract.task_id)}

    async def run():
        with patch("manager.loop.check_hard_governance_gates", side_effect=real_shaped_governance_check):
            with patch("manager.loop._execute_contract", new=AsyncMock(side_effect=fake_execute_contract)):
                with patch("manager.loop.clear_task_state"):
                    return await _run_constraint_labels_from(
                        contract, labels, satisfied, 0,
                        "module_lock", client=None, classifier_model="x", correction_result={},
                        module_for_repeat_check=None, memory_block="", constitution_text="",
                        sensitive_paths_raw=[],
                    )

    result = asyncio.run(run())
    assert result["status"] == "paused"
    assert result["reason"] == "governance_rule_blocked"
    assert build_reached == [], "a genuine match in the real goal text must still block Build, exactly as before this fix"
    print("PASS: a genuine governance match in the real (non-risk-hint) goal text still blocks exactly as before")


if __name__ == "__main__":
    test_a_sub_contract_matching_a_governance_rule_is_blocked_before_reaching_build()
    test_a_clean_sub_contract_proceeds_normally()
    test_first_constraint_blocked_never_touches_the_second()
    test_known_risk_hint_noise_never_reaches_the_governance_check()
    test_a_genuine_match_in_the_narrowed_goal_text_itself_still_blocks()
    print("\nALL PER-SUB-CONTRACT GOVERNANCE-RECHECK TESTS PASSED")
