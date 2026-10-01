"""P14 item 1 (docs/planning/PHASE30_OMA_SEAM_COVERAGE_AND_COMBINATORIAL_HARDENING_2026-07-29.md
§18c.4b): tests for contracts/data_change_extraction.py. Mocks call_structured() directly, zero
live LLM/GPU calls.
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from unittest.mock import patch

import contracts.data_change_extraction as extraction_module
from contracts.data_change_extraction import DataChangeOperation, extract_data_change_operation, is_complete


def test_a_complete_real_extraction_round_trips():
    fake_result = DataChangeOperation(
        model="res.partner", record_search_field="email", record_search_value="x@example.com",
        field="phone", new_value="555-1234",
    )

    async def fake_call_structured(**kwargs):
        return fake_result

    async def run():
        with patch.object(extraction_module, "call_structured", new=fake_call_structured):
            return await extract_data_change_operation(
                "Update the phone number to 555-1234 on the contact whose email is x@example.com.",
                client=None, model="x",
            )

    result = asyncio.run(run())
    assert result == fake_result
    assert is_complete(result) is True
    print("PASS: a complete, concrete extraction round-trips and is_complete() confirms it")


def test_a_failed_extraction_returns_all_none_never_raises():
    async def failing_call_structured(**kwargs):
        raise RuntimeError("gateway exploded")

    async def run():
        with patch.object(extraction_module, "call_structured", new=failing_call_structured):
            return await extract_data_change_operation("some goal", client=None, model="x")

    result = asyncio.run(run())
    assert result == DataChangeOperation()
    assert is_complete(result) is False
    print("PASS: a genuine extraction failure returns an all-None operation, never raises")


def test_is_complete_requires_every_field():
    partial = DataChangeOperation(model="res.partner", field="phone")
    assert is_complete(partial) is False
    print("PASS: a partial extraction (missing record_search_field/value or new_value) is never treated as complete")


def test_is_complete_accepts_a_falsy_but_real_new_value():
    # new_value="False" (a real, concrete string the goal states) must count as present --
    # only None (never extracted) means "not stated."
    op = DataChangeOperation(
        model="res.partner", record_search_field="email", record_search_value="x@example.com",
        field="active", new_value="False",
    )
    assert is_complete(op) is True
    print("PASS: a real, concrete (but falsy-looking) new_value string is correctly treated as present")


if __name__ == "__main__":
    test_a_complete_real_extraction_round_trips()
    test_a_failed_extraction_returns_all_none_never_raises()
    test_is_complete_requires_every_field()
    test_is_complete_accepts_a_falsy_but_real_new_value()
    print("\nALL DATA-CHANGE-EXTRACTION TESTS PASSED")
