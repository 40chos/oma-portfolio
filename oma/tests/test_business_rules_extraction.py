"""P13 item 12a (docs/planning/PHASE30_OMA_SEAM_COVERAGE_AND_COMBINATORIAL_HARDENING_2026-07-29.md
§22.2): tests for contracts/business_rules.py -- the deterministic trigger check and the
keyword-gated LLM extraction. Mocks call_structured() directly, zero live LLM/GPU calls.
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from unittest.mock import patch

import contracts.business_rules as business_rules_module
from contracts.business_rules import (
    InterpretedBusinessRules,
    extract_interpreted_business_rules,
    goal_has_business_rule_ambiguity_signal,
)


def test_trigger_fires_on_total():
    assert goal_has_business_rule_ambiguity_signal("Show the order total on the header.") is True
    print("PASS: 'total' fires the trigger")


def test_trigger_fires_on_overdue_threshold():
    assert goal_has_business_rule_ambiguity_signal("Mark records overdue after a threshold of 30 days.") is True
    print("PASS: 'overdue'/'threshold' fires the trigger")


def test_trigger_does_not_fire_on_plain_field_goal():
    assert goal_has_business_rule_ambiguity_signal("Add a text field to the lead form.") is False
    print("PASS: an ordinary field-only goal does not fire the trigger")


def test_extract_skips_the_llm_call_entirely_when_no_trigger():
    calls = []

    async def fake_call_structured(**kwargs):
        calls.append(kwargs)
        return InterpretedBusinessRules(interpreted_business_rules=["should never be reached"])

    async def run():
        with patch.object(business_rules_module, "call_structured", new=fake_call_structured):
            return await extract_interpreted_business_rules("Add a boolean field.", client=None, model="x")

    result = asyncio.run(run())
    assert result == []
    assert calls == []
    print("PASS: no trigger keyword -> zero LLM calls made, empty list returned")


def test_extract_returns_real_result_when_triggered():
    fake_result = InterpretedBusinessRules(
        interpreted_business_rules=["the order total includes tax"],
    )

    async def fake_call_structured(**kwargs):
        return fake_result

    async def run():
        with patch.object(business_rules_module, "call_structured", new=fake_call_structured):
            return await extract_interpreted_business_rules(
                "Show the order total, including tax.", client=None, model="x",
            )

    result = asyncio.run(run())
    assert result == ["the order total includes tax"]
    print("PASS: extract_interpreted_business_rules() returns the real structured result on success")


def test_extract_fails_open_on_any_exception():
    async def fake_call_structured_raises(**kwargs):
        raise RuntimeError("gateway error")

    async def run():
        with patch.object(business_rules_module, "call_structured", new=fake_call_structured_raises):
            return await extract_interpreted_business_rules("Compute the total.", client=None, model="x")

    result = asyncio.run(run())
    assert result == []
    print("PASS: a genuine extraction failure fails open to [], never raises")


def test_extract_strips_blank_entries():
    fake_result = InterpretedBusinessRules(interpreted_business_rules=["a real rule", "  ", ""])

    async def fake_call_structured(**kwargs):
        return fake_result

    async def run():
        with patch.object(business_rules_module, "call_structured", new=fake_call_structured):
            return await extract_interpreted_business_rules("Compute the total.", client=None, model="x")

    result = asyncio.run(run())
    assert result == ["a real rule"]
    print("PASS: blank/whitespace-only entries are stripped")


if __name__ == "__main__":
    test_trigger_fires_on_total()
    test_trigger_fires_on_overdue_threshold()
    test_trigger_does_not_fire_on_plain_field_goal()
    test_extract_skips_the_llm_call_entirely_when_no_trigger()
    test_extract_returns_real_result_when_triggered()
    test_extract_fails_open_on_any_exception()
    test_extract_strips_blank_entries()
    print("\nALL BUSINESS-RULE EXTRACTION TESTS PASSED")
