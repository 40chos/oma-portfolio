"""P12 Tier A item 24 (docs/planning/PHASE30_ARCH_SYNTHESIS_CRITIQUE_FINAL_2026-07-30.md §4):
tests for _load_constitution_text_or_none() and its wiring into _self_report_coverage()'s
prompt -- real, confirmed gap: Code-Review was the only specialist reading the Odoo Development
Agent Constitution; Testing/QA's own self-report judgment had no access to the same real
standard. Mocks load_odoo_development_constitution() directly (no dependency on the real file's
current on-disk presence/content), zero LLM calls.
"""

import asyncio
import os
import sys
import uuid
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import specialists.testing_qa.specialist as testing_qa_module
from contracts.schema import AutonomyTier, CapabilityClass, SpecialistType, TaskContract
from specialists.constitution import ConstitutionNotFoundError


def _contract() -> TaskContract:
    return TaskContract(
        task_id=uuid.uuid4(), specialist_type=SpecialistType.bug_fix,
        capability_class=CapabilityClass.module_development, tier=AutonomyTier.tier_1_readonly,
        goal="x", inputs=[], rules=[], deliverables=[], compensating_actions=[],
        validation_by="testing_qa", pause_if=[], turn_budget=10,
    )


def test_returns_the_real_constitution_text_when_present():
    with patch.object(testing_qa_module, "load_odoo_development_constitution", return_value="REAL CONSTITUTION TEXT"):
        result = testing_qa_module._load_constitution_text_or_none()
    assert result == "REAL CONSTITUTION TEXT"
    print("PASS: returns the real constitution text when the file is present")


def test_gracefully_degrades_to_none_when_the_file_is_missing():
    def raise_not_found():
        raise ConstitutionNotFoundError("not present")

    with patch.object(testing_qa_module, "load_odoo_development_constitution", side_effect=raise_not_found):
        result = testing_qa_module._load_constitution_text_or_none()
    assert result is None
    print("PASS: gracefully degrades to None on ConstitutionNotFoundError, never raises/crashes")


async def _run_self_report(specialist, contract):
    captured_prompts = []

    async def fake_call_structured(**kwargs):
        captured_prompts.append(kwargs["prompt"])
        return testing_qa_module.SelfReportedCoverage(believed_fully_covered=True, claimed_uncovered_paths=[], reasoning="x")

    with patch.object(testing_qa_module, "call_structured", new=fake_call_structured):
        with patch.object(testing_qa_module._SKILL_PATH.__class__, "read_text", return_value="skill text"):
            await specialist._self_report_coverage(contract, "fake_module")
    return captured_prompts


def test_self_report_coverage_prompt_includes_constitution_text_when_present():
    specialist = testing_qa_module.TestingQASpecialist(client=None, routine_model="fake-model")
    with patch.object(testing_qa_module, "load_odoo_development_constitution", return_value="REAL CONSTITUTION MARKER TEXT"):
        prompts = asyncio.run(_run_self_report(specialist, _contract()))
    assert prompts and "REAL CONSTITUTION MARKER TEXT" in prompts[0]
    print("PASS: _self_report_coverage()'s real prompt genuinely includes the constitution text when present")


def test_self_report_coverage_prompt_omits_constitution_block_when_absent():
    specialist = testing_qa_module.TestingQASpecialist(client=None, routine_model="fake-model")

    def raise_not_found():
        raise ConstitutionNotFoundError("not present")

    with patch.object(testing_qa_module, "load_odoo_development_constitution", side_effect=raise_not_found):
        prompts = asyncio.run(_run_self_report(specialist, _contract()))
    assert prompts and "Odoo Development Agent Constitution" not in prompts[0]
    print("PASS: when the constitution is absent, the prompt omits the block entirely rather than a broken/empty reference")


if __name__ == "__main__":
    test_returns_the_real_constitution_text_when_present()
    test_gracefully_degrades_to_none_when_the_file_is_missing()
    test_self_report_coverage_prompt_includes_constitution_text_when_present()
    test_self_report_coverage_prompt_omits_constitution_block_when_absent()
    print("\nALL TESTING-QA CONSTITUTION-ACCESS TESTS PASSED")
