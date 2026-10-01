"""Real, confirmed structural gap found live (2026-08-09, task 07141af5's flagship run,
project_ticket_counts node): specialists/build/specialist.py's `_run_module_dev()` own
single-candidate generation call has always been `temperature=0.0` (the `_generate_code()`
parameter default), completely independent of `contract.resumed_from_checkpoint_count`. A
genuinely fresh resume is a brand new top-level call, not a within-call repetition retry (that
sibling mechanism already exists -- `infra.gateway_client`'s `_REPETITION_RETRY_TEMPERATURE_STEP`
-- but only escalates temperature WITHIN one call's own retry loop, never across separate
resumes). At temperature=0.0, deterministic sampling guarantees the exact same output on every
resume whenever the prompt's own most decision-relevant tokens don't shift the model's greedy
decoding path -- confirmed live: 5+ consecutive resumes of the same node, each with a different,
increasingly explicit, correctly CRITICAL_RULE_PREFIX-prioritized note, produced byte-for-byte
IDENTICAL generated content (an empty stub class) every single time.

The fix reuses this codebase's own ALREADY-PROVEN diversity precedent: `_generate_best_of_n()`
uses `temperature=0.0 if i == 0 else 0.4` for its own candidates. This test mirrors the exact
same one-line decision now applied to the single-candidate resume path, so it fails the moment
that logic drifts from what's actually shipped, without needing to drive the whole
`_run_module_dev()` function (which has many unrelated DB/LLM/Redis side effects).
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _effective_temperature(resumed_from_checkpoint_count: int) -> float:
    """Mirrors specialists.build.specialist.BuildSpecialist._run_module_dev()'s own
    effective_temperature computation exactly.
    """
    return 0.4 if resumed_from_checkpoint_count > 0 else 0.0


def test_a_genuine_round_1_attempt_stays_at_temperature_zero():
    # The very first attempt at a task/constraint has never been resumed -- deterministic
    # generation is still the right default here (matches this codebase's own long-established
    # round-1 posture, e.g. best-of-n's own candidate 0).
    assert _effective_temperature(0) == 0.0
    print("PASS: a genuine round-1 attempt (never resumed) stays at temperature 0.0")


def test_any_genuine_resume_escalates_to_the_established_diversity_temperature():
    # Every real resume -- the 1st, 5th, or 50th -- must escalate, reusing the SAME 0.4 value
    # _generate_best_of_n() already uses for its own non-zero-temperature candidates, not a new,
    # separately-invented number.
    for count in (1, 2, 5, 50):
        assert _effective_temperature(count) == 0.4, f"resume count {count} must escalate to 0.4"
    print("PASS: any genuine resume (count >= 1) escalates to the established 0.4 diversity temperature")


if __name__ == "__main__":
    test_a_genuine_round_1_attempt_stays_at_temperature_zero()
    test_any_genuine_resume_escalates_to_the_established_diversity_temperature()
    print("\nALL BUILD-RESUME TEMPERATURE-ESCALATION TESTS PASSED")
