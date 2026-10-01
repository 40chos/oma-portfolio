"""P12 Tier B/C item 29 (docs/planning/PHASE30_ARCH_SYNTHESIS_CRITIQUE_FINAL_2026-07-30.md §4,
A Finding 26): regression guard confirming the short-circuit VerificationResult construction
in manager/loop.py (sandbox_failed/lock_rejected/real_install_genuinely_failed/validation_by
is None) now copies regressed_constraints through, for parity with the normal path
(manager/tools.py's await_verification()). Real, confirmed gap: this branch silently defaulted
to [] before, blinding detect_oscillation()'s own remediation note in the narrow overlap case
where a round both regressed a constraint AND failed via one of these short-circuit reasons.

This construction lives deep inside run_turn()'s own round loop (heavy real dependencies --
module locks, Redis, real specialists), making a full live reproduction impractical here (same
reasoning already established for items 16/22's own source-level regression guards). Confirms
the fix is genuinely present in source, at the correct construction site. Zero LLM/GPU calls --
pure source inspection.
"""

import inspect
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import manager.loop as loop_module


def test_short_circuit_verification_result_copies_regressed_constraints():
    source = inspect.getsource(loop_module)
    # Locate the short-circuit VerificationResult construction specifically (the one keyed off
    # root_cause="one_off" if build_output.detail.get("lock_rejected") -- a marker unique to
    # this exact branch, not the normal await_verification() path).
    match = re.search(
        r'root_cause="one_off" if build_output\.detail\.get\("lock_rejected"\) else None,\s*\n(.*?)\n\s*\)',
        source, re.DOTALL,
    )
    assert match, "could not locate the short-circuit VerificationResult construction's own tail (root_cause= line onward)"
    tail = match.group(1)
    assert 'regressed_constraints=list(build_output.detail.get("regressed_constraints", []))' in tail, (
        "the short-circuit VerificationResult construction must copy regressed_constraints "
        "through from build_output.detail, for parity with the normal await_verification() path"
    )
    print("PASS: the short-circuit VerificationResult construction genuinely copies regressed_constraints through")


if __name__ == "__main__":
    test_short_circuit_verification_result_copies_regressed_constraints()
    print("\nALL SHORT-CIRCUIT REGRESSED-CONSTRAINTS COPY-THROUGH TESTS PASSED")
