"""P12 Tier B/C item 26 (docs/planning/PHASE30_ARCH_SYNTHESIS_CRITIQUE_FINAL_2026-07-30.md §4):
tests for extract_ambiguity_report() and AmbiguityReport's own defaults -- real, confirmed gap:
OMA had no pre-build "is this goal genuinely well-specified enough to build" check; ambiguity
was only ever discovered the expensive way, via failed rounds and reactive escalation. Mocks
call_structured() directly, zero live LLM/GPU calls.
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from unittest.mock import patch

import contracts.ambiguity_check as ambiguity_check_module
from contracts.ambiguity_check import AmbiguityReport, extract_ambiguity_report


def test_default_report_has_no_blocking_ambiguity():
    report = AmbiguityReport()
    assert report.has_blocking_ambiguity is False
    assert report.blocking_questions == []
    print("PASS: the default AmbiguityReport (nothing extracted) has no blocking ambiguity -- fail-open shape")


def test_extract_returns_the_real_structured_result_on_success():
    fake_report = AmbiguityReport(
        has_blocking_ambiguity=True, blocking_questions=["Which team should tickets route to?"],
        reasoning="The routing target is undefined and is the whole point of the task.",
    )

    async def fake_call_structured(**kwargs):
        return fake_report

    async def run():
        with patch.object(ambiguity_check_module, "call_structured", new=fake_call_structured):
            return await extract_ambiguity_report("Route tickets to the right team.", client=None, model="x")

    result = asyncio.run(run())
    assert result.has_blocking_ambiguity is True
    assert result.blocking_questions == ["Which team should tickets route to?"]
    print("PASS: extract_ambiguity_report() returns the real structured result on success")


def test_extract_fails_open_on_any_exception():
    async def fake_call_structured_raises(**kwargs):
        raise RuntimeError("gateway error")

    async def run():
        with patch.object(ambiguity_check_module, "call_structured", new=fake_call_structured_raises):
            return await extract_ambiguity_report("Any goal.", client=None, model="x")

    result = asyncio.run(run())
    assert result.has_blocking_ambiguity is False
    print("PASS: a genuine extraction failure fails open to has_blocking_ambiguity=False, never raises")


if __name__ == "__main__":
    test_default_report_has_no_blocking_ambiguity()
    test_extract_returns_the_real_structured_result_on_success()
    test_extract_fails_open_on_any_exception()
    print("\nALL AMBIGUITY-CHECK TESTS PASSED")
