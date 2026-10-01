"""Phase 35 §17.2.1: tests for manager/intake_grounding.py -- the task-intake existence/dedup
check. Mocks call_structured() and get_model_existence() directly, zero live LLM/graph calls.
"""

import asyncio
import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import manager.intake_grounding as intake_module
from manager.intake_grounding import (
    IntakeCandidateEntities,
    IntakeGroundingVerdict,
    check_intake_grounding,
    deterministic_technical_name_scan,
    extract_intake_candidates,
)


def test_deterministic_scan_finds_dotted_technical_names():
    goal = "Add a field to crm.lead, and also touch oma.badge.scan.log while you're at it."
    hits = deterministic_technical_name_scan(goal)
    assert "crm.lead" in hits
    assert "oma.badge.scan.log" in hits


def test_deterministic_scan_ignores_prose_with_no_dotted_names():
    goal = "Create a new module for tracking office plants."
    assert deterministic_technical_name_scan(goal) == []


def test_extract_returns_real_result_on_success():
    fake = IntakeCandidateEntities(candidate_models=["crm.lead"], confidence=0.9)

    async def fake_call_structured(**kwargs):
        return fake

    async def run():
        with patch.object(intake_module, "call_structured", new=fake_call_structured):
            return await extract_intake_candidates("Add a field to crm.lead", client=None, model="x")

    result = asyncio.run(run())
    assert result.candidate_models == ["crm.lead"]
    assert result.confidence == 0.9


def test_extract_fails_open_on_exception():
    async def fake_raises(**kwargs):
        raise RuntimeError("gateway error")

    async def run():
        with patch.object(intake_module, "call_structured", new=fake_raises):
            return await extract_intake_candidates("Any goal", client=None, model="x")

    result = asyncio.run(run())
    assert result.candidate_models == []
    assert result.confidence == 0.0


def _mock_extraction(candidate_models=None, confidence=0.0):
    fake = IntakeCandidateEntities(candidate_models=candidate_models or [], confidence=confidence)

    async def fake_call_structured(**kwargs):
        return fake
    return fake_call_structured


def test_exists_verbatim_when_graph_confirms_a_match():
    def fake_get_model_existence(driver, technical_name):
        if technical_name == "oma.badge.scan.log":
            return False, {"technical_name": technical_name, "defining_modules": ["oma_badges"], "field_count": 3}
        return False, None

    async def run():
        with patch.object(intake_module, "call_structured", new=_mock_extraction(["oma.badge.scan.log"], 0.9)), \
             patch.object(intake_module, "get_model_existence", side_effect=fake_get_model_existence):
            return await check_intake_grounding(
                "Add a field to oma.badge.scan.log", driver=object(), client=None, model="x",
            )

    result = asyncio.run(run())
    assert result.verdict == IntakeGroundingVerdict.EXISTS_VERBATIM
    assert result.matched_models == ["oma.badge.scan.log"]


def test_no_signal_when_candidate_checked_but_not_found():
    async def run():
        with patch.object(intake_module, "call_structured", new=_mock_extraction(["oma.new.thing"], 0.9)), \
             patch.object(intake_module, "get_model_existence", return_value=(False, None)):
            return await check_intake_grounding(
                "Create a new model oma.new.thing", driver=object(), client=None, model="x",
            )

    result = asyncio.run(run())
    assert result.verdict == IntakeGroundingVerdict.NO_SIGNAL
    assert result.matched_models == []


def test_ungrounded_requires_both_low_confidence_and_no_deterministic_hits():
    async def run():
        with patch.object(intake_module, "call_structured", new=_mock_extraction([], 0.1)):
            return await check_intake_grounding(
                "Please make things better.", driver=object(), client=None, model="x",
            )

    result = asyncio.run(run())
    assert result.verdict == IntakeGroundingVerdict.UNGROUNDED


def test_deterministic_hit_overrides_ungrounded_even_at_low_llm_confidence():
    # §17.2.1.1 item 2: a deterministic scan hit means it's NOT ungrounded, even if the LLM's
    # own confidence was low -- the two signals are independent, not just LLM-gated.
    async def run():
        with patch.object(intake_module, "call_structured", new=_mock_extraction([], 0.1)), \
             patch.object(intake_module, "get_model_existence", return_value=(False, None)):
            return await check_intake_grounding(
                "Do something with crm.lead please.", driver=object(), client=None, model="x",
            )

    result = asyncio.run(run())
    assert result.verdict != IntakeGroundingVerdict.UNGROUNDED
    assert "crm.lead" in result.candidates_checked


def test_graph_query_failure_never_reported_as_a_confirmed_non_match():
    def raising_get_model_existence(driver, technical_name):
        raise RuntimeError("neo4j connection error")

    async def run():
        with patch.object(intake_module, "call_structured", new=_mock_extraction(["oma.thing"], 0.9)), \
             patch.object(intake_module, "get_model_existence", side_effect=raising_get_model_existence):
            return await check_intake_grounding(
                "Add a field to oma.thing", driver=object(), client=None, model="x",
            )

    result = asyncio.run(run())
    assert result.verdict == IntakeGroundingVerdict.GRAPH_QUERY_FAILED


def test_import_in_progress_on_one_candidate_does_not_hide_a_real_match_on_another():
    def fake_get_model_existence(driver, technical_name):
        if technical_name == "oma.stale":
            return True, None
        if technical_name == "oma.real":
            return False, {"technical_name": "oma.real", "defining_modules": ["m"], "field_count": 1}
        return False, None

    async def run():
        with patch.object(intake_module, "call_structured", new=_mock_extraction(["oma.stale", "oma.real"], 0.9)), \
             patch.object(intake_module, "get_model_existence", side_effect=fake_get_model_existence):
            return await check_intake_grounding(
                "Touch oma.stale and oma.real", driver=object(), client=None, model="x",
            )

    result = asyncio.run(run())
    assert result.verdict == IntakeGroundingVerdict.EXISTS_VERBATIM
    assert result.matched_models == ["oma.real"]
