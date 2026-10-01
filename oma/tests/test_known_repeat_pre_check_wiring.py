"""P13 item 8 (docs/planning/PHASE30_OMA_SEAM_COVERAGE_AND_COMBINATORIAL_HARDENING_2026-07-29.md
§22.2), widened 2026-08-02 (Sonnet 5 A/B follow-up, task005,
docs/reports/PHASE30_P14_ITEM3_SONNET5_FOLLOWUP_2026-08-02.md): regression guard confirming
is_known_repeat() is genuinely wired into manager/loop.py's round loop as a real escalation
trigger, immediately after revise_contract_from_verification() (item 7) builds this round's own
FailureRecord, before the next round's Build call would dispatch.

Real, confirmed gap closed by this widening: the ORIGINAL wiring only ever ran when
current_contract.current_constraint_label was set, which is exclusively true for a DECOMPOSED
(3+-constraint) task -- a plain, single-requirement task (the more common real shape) never set
it, so the whole check silently never ran for that entire population regardless of how many
rounds repeated the identical finding. Confirmed live on task005 (a single "add an onchange" goal)
hitting the same real Code-Review finding in rounds 4 and 5, verbatim-adjacent, invisible to this
gate and every other oscillation detector. Fixed by falling back to the contract's own flat
failure_records history whenever no constraint-node history exists -- the SAME is_known_repeat()
detection logic either way, never a second, competing check.

This check lives deep inside _execute_contract()'s own round loop (heavy real dependencies --
module locks, Redis, real specialists, asyncio.gather() over Code-Review/Testing-QA), making a
full live reproduction impractical here -- same reasoning already established for items 16/22/29's
own source-level regression guards. Zero LLM/GPU calls -- pure source inspection.
"""

import inspect
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import manager.loop as loop_module


def test_is_known_repeat_is_wired_immediately_after_revise_contract_from_verification():
    source = inspect.getsource(loop_module)
    revise_call_index = source.index("new_contract, reasoning = revise_contract_from_verification(")
    # The next real escalation-relevant statement after the revise call must be this gate --
    # search only in the characters immediately following the revise call, not the whole file, so
    # an unrelated later use of is_known_repeat() elsewhere wouldn't false-pass this.
    window = source[revise_call_index:revise_call_index + 3400]
    assert "is_known_repeat(newest_failure_record, repeat_history)" in window, (
        "is_known_repeat() must be called immediately after revise_contract_from_verification() "
        "builds this round's own FailureRecord, before the round loop moves on to routing/dispatch"
    )
    assert 'escalation_reason = "known_repeat_pre_check"' in window
    print("PASS: is_known_repeat() is wired immediately after revise_contract_from_verification()")


def test_the_gate_never_overrides_an_already_decided_escalation_reason():
    source = inspect.getsource(loop_module)
    match = re.search(
        r'if not escalation_reason:\n'
        r'(.*?)\n\s*# P13 item 12b',
        source, re.DOTALL,
    )
    assert match, "could not locate the item 8 pre-check gate block by its own guard condition"
    print("PASS: the gate is guarded by 'not escalation_reason', never overriding an already-decided escalation")


def test_the_gate_compares_against_the_pre_revision_contract_not_the_new_one():
    source = inspect.getsource(loop_module)
    revise_call_index = source.index("new_contract, reasoning = revise_contract_from_verification(")
    window = source[revise_call_index:revise_call_index + 3400]
    assert "current_contract.constraint_nodes.get(current_contract.current_constraint_label)" in window, (
        "prior_node must be read from current_contract (pre-revision), never new_contract "
        "(which already has this round's own record appended) -- otherwise the candidate would "
        "always match itself trivially"
    )
    print("PASS: the gate reads prior_node from current_contract (pre-revision), never new_contract")


def test_the_gate_falls_back_to_the_flat_failure_records_for_non_decomposed_tasks():
    """The real 2026-08-02 widening -- confirms the fallback exists in source, at the correct
    location, reading from current_contract (pre-revision) rather than new_contract.
    """
    source = inspect.getsource(loop_module)
    revise_call_index = source.index("new_contract, reasoning = revise_contract_from_verification(")
    window = source[revise_call_index:revise_call_index + 3400]
    assert "prior_node.failure_records if prior_node else current_contract.failure_records" in window, (
        "when no constraint-node history exists (a plain, non-decomposed task), the gate must "
        "fall back to the contract's own flat failure_records list -- otherwise this whole check "
        "stays structurally blind to the most common real task shape"
    )
    print("PASS: the gate falls back to current_contract.failure_records when no node history exists")


if __name__ == "__main__":
    test_is_known_repeat_is_wired_immediately_after_revise_contract_from_verification()
    test_the_gate_never_overrides_an_already_decided_escalation_reason()
    test_the_gate_compares_against_the_pre_revision_contract_not_the_new_one()
    test_the_gate_falls_back_to_the_flat_failure_records_for_non_decomposed_tasks()
    print("\nALL KNOWN-REPEAT PRE-CHECK WIRING TESTS PASSED")
