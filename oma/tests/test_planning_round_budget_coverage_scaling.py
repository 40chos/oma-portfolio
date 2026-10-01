"""P10 (docs/planning/PHASE30_OMA_SEAM_COVERAGE_AND_COMBINATORIAL_HARDENING_2026-07-29.md §18,
Phase O, item 2): regression guard confirming manager/loop.py's _execute_contract() widens
planning_round_budget when the goal's own field_type has real, evidence-backed low
combination/generation confidence -- and never overrides an explicit caller-supplied budget.

This wiring lives immediately after extract_goal_field_facts() inside _execute_contract()'s own
large, heavily-dependent body (real gateway calls, module locks, round loop) -- a full live
reproduction is impractical here, same reasoning already established for items 16/22/29/8's own
source-level regression guards. Zero LLM/GPU calls -- pure source inspection.
"""

import inspect
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import manager.loop as loop_module


def test_budget_widening_is_wired_immediately_after_goal_facts_extraction():
    source = inspect.getsource(loop_module)
    goal_facts_index = source.index('contract = contract.model_copy(update={"goal_facts": goal_facts.model_dump()})')
    current_contract_index = source.index("current_contract = contract", goal_facts_index)
    window = source[goal_facts_index:current_contract_index]
    assert "field_type_has_low_coverage_confidence(goal_facts.field_type)" in window
    assert '"planning_round_budget": 8' in window
    print("PASS: planning_round_budget widening is wired immediately after goal_facts extraction, before the round loop starts")


def test_widening_only_fires_when_budget_is_still_the_schema_default():
    source = inspect.getsource(loop_module)
    goal_facts_index = source.index('contract = contract.model_copy(update={"goal_facts": goal_facts.model_dump()})')
    current_contract_index = source.index("current_contract = contract", goal_facts_index)
    window = source[goal_facts_index:current_contract_index]
    assert "contract.planning_round_budget == 5" in window, (
        "the widening must be guarded by 'still exactly the schema default' -- an explicit "
        "caller-supplied override must never be silently overwritten"
    )
    print("PASS: the widening is guarded so an explicit caller-supplied planning_round_budget is never silently overridden")


if __name__ == "__main__":
    test_budget_widening_is_wired_immediately_after_goal_facts_extraction()
    test_widening_only_fires_when_budget_is_still_the_schema_default()
    print("\nALL PLANNING-ROUND-BUDGET COVERAGE-SCALING TESTS PASSED")
