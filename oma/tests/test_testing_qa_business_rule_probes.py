"""P13 item 12a (docs/planning/PHASE30_OMA_SEAM_COVERAGE_AND_COMBINATORIAL_HARDENING_2026-07-29.md
§22.2): unit tests for TestingQASpecialist's new `_run_business_rule_probes()` -- the business-rule
sibling of the existing behavioral-probe orchestration (test_testing_qa_behavioral_probe_
orchestration.py), same mocked call_structured/run_behavioral_probe discipline, zero live SSH/DB/
gateway needed.
"""

import asyncio
import os
import sys
import uuid
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from contracts.schema import AutonomyTier, CapabilityClass, SpecialistType, TaskContract
import specialists.testing_qa.specialist as tq_module
from specialists.testing_qa.specialist import (
    BehavioralProbeCreate,
    BehavioralProbeSpec,
    BehavioralProbeVerdict,
    TestingQASpecialist,
)


def _make_contract(goal: str = "x", interpreted_business_rules=None) -> TaskContract:
    return TaskContract(
        task_id=uuid.uuid4(), specialist_type=SpecialistType.testing_qa,
        capability_class=CapabilityClass.module_development, tier=AutonomyTier.tier_2_notify_after,
        goal=goal, inputs=[], rules=[], deliverables=[], compensating_actions=[],
        validation_by="testing_qa", pause_if=[], turn_budget=10, retry_sub_budget=3,
        interpreted_business_rules=interpreted_business_rules or [],
    )


def test_no_op_when_no_business_rules_extracted():
    contract = _make_contract(interpreted_business_rules=[])

    async def run():
        specialist = TestingQASpecialist(client=None)
        return await specialist._run_business_rule_probes(contract, "oma_x", "db_x")

    confirmed, notes = asyncio.run(run())
    assert confirmed is True
    assert notes == ""
    print("PASS: empty interpreted_business_rules is a real no-op, no calls made")


def test_probe_confirms_a_genuinely_correct_interpretation():
    contract = _make_contract(interpreted_business_rules=["the order total includes tax"])

    async def fake_call_structured(**kwargs):
        if kwargs["schema"] is BehavioralProbeSpec:
            return BehavioralProbeSpec(
                applicable=True,
                creates=[BehavioralProbeCreate(model="sale.order", values={"tax_rate": 0.21, "price_unit": 100.0})],
                target_index=0, check_fields=["amount_total"],
                expected_effect="amount_total should be 121.0 (100 + 21% tax)",
            )
        assert kwargs["schema"] is BehavioralProbeVerdict
        return BehavioralProbeVerdict(behavior_confirmed=True, reasoning="real amount_total was 121.0 as predicted")

    async def run():
        with patch("specialists.testing_qa.specialist.call_structured", new=AsyncMock(side_effect=fake_call_structured)):
            with patch.object(tq_module, "run_behavioral_probe", return_value={"amount_total": 121.0}):
                with patch.object(tq_module, "read_module_files", return_value={"models/models.py": "x"}):
                    specialist = TestingQASpecialist(client=None)
                    return await specialist._run_business_rule_probes(contract, "oma_x", "db_x")

    confirmed, notes = asyncio.run(run())
    assert confirmed is True
    assert notes == ""
    print("PASS: a genuinely correct business-rule interpretation confirms, no failure notes")


def test_probe_catches_a_genuine_business_rule_mismatch():
    contract = _make_contract(interpreted_business_rules=["the order total includes tax"])

    async def fake_call_structured(**kwargs):
        if kwargs["schema"] is BehavioralProbeSpec:
            return BehavioralProbeSpec(
                applicable=True,
                creates=[BehavioralProbeCreate(model="sale.order", values={"tax_rate": 0.21, "price_unit": 100.0})],
                target_index=0, check_fields=["amount_total"],
                expected_effect="amount_total should be 121.0 (100 + 21% tax)",
            )
        assert kwargs["schema"] is BehavioralProbeVerdict
        return BehavioralProbeVerdict(behavior_confirmed=False, reasoning="the observed total excludes tax entirely")

    async def run():
        with patch("specialists.testing_qa.specialist.call_structured", new=AsyncMock(side_effect=fake_call_structured)):
            with patch.object(tq_module, "run_behavioral_probe", return_value={"amount_total": 100.0}):
                with patch.object(tq_module, "read_module_files", return_value={"models/models.py": "x"}):
                    specialist = TestingQASpecialist(client=None)
                    return await specialist._run_business_rule_probes(contract, "oma_x", "db_x")

    confirmed, notes = asyncio.run(run())
    assert confirmed is False
    assert "Business rule check FAILED: 'the order total includes tax'" in notes
    assert "amount_total" in notes
    print("PASS: a genuine business-rule mismatch fails with the correctly-formatted marker text")


def test_multiple_rules_only_fail_on_the_genuinely_wrong_one():
    contract = _make_contract(interpreted_business_rules=["rule A holds", "rule B holds"])

    async def fake_call_structured(**kwargs):
        if kwargs["schema"] is BehavioralProbeSpec:
            return BehavioralProbeSpec(
                applicable=True, creates=[BehavioralProbeCreate(model="x.model", values={"a": 1})],
                target_index=0, check_fields=["value"], expected_effect="x",
            )
        # rule A confirmed, rule B fails -- distinguish via the reasoning text passed to judge
        confirmed = "rule A" in kwargs["prompt"]
        return BehavioralProbeVerdict(behavior_confirmed=confirmed, reasoning="checked")

    async def run():
        with patch("specialists.testing_qa.specialist.call_structured", new=AsyncMock(side_effect=fake_call_structured)):
            with patch.object(tq_module, "run_behavioral_probe", return_value={"value": 1}):
                with patch.object(tq_module, "read_module_files", return_value={"models/models.py": "x"}):
                    specialist = TestingQASpecialist(client=None)
                    return await specialist._run_business_rule_probes(contract, "oma_x", "db_x")

    confirmed, notes = asyncio.run(run())
    assert confirmed is False
    assert "rule A" not in notes
    assert "rule B holds" in notes
    print("PASS: only the genuinely-wrong rule appears in the failure notes, the confirmed one doesn't")


def test_returns_true_when_probe_synthesis_is_not_applicable():
    contract = _make_contract(interpreted_business_rules=["a rule with no checkable effect"])

    async def fake_call_structured(**kwargs):
        return BehavioralProbeSpec(applicable=False)

    async def run():
        with patch("specialists.testing_qa.specialist.call_structured", new=AsyncMock(side_effect=fake_call_structured)):
            with patch.object(tq_module, "read_module_files", return_value={"models/models.py": "x"}):
                specialist = TestingQASpecialist(client=None)
                return await specialist._run_business_rule_probes(contract, "oma_x", "db_x")

    confirmed, notes = asyncio.run(run())
    assert confirmed is True
    assert notes == ""
    print("PASS: a non-applicable probe is skipped, never counted as a failure")


def test_returns_true_on_real_probe_execution_failure_genuine_uncertainty():
    contract = _make_contract(interpreted_business_rules=["the order total includes tax"])

    async def fake_call_structured(**kwargs):
        return BehavioralProbeSpec(
            applicable=True, creates=[BehavioralProbeCreate(model="x.model", values={"a": 1})],
            target_index=0, check_fields=["value"], expected_effect="x",
        )

    async def run():
        with patch("specialists.testing_qa.specialist.call_structured", new=AsyncMock(side_effect=fake_call_structured)):
            with patch.object(tq_module, "run_behavioral_probe", return_value=None):
                with patch.object(tq_module, "read_module_files", return_value={"models/models.py": "x"}):
                    specialist = TestingQASpecialist(client=None)
                    return await specialist._run_business_rule_probes(contract, "oma_x", "db_x")

    confirmed, notes = asyncio.run(run())
    assert confirmed is True
    assert notes == ""
    print("PASS: a real probe-execution failure (genuine infra uncertainty) never counts as a mismatch")


if __name__ == "__main__":
    test_no_op_when_no_business_rules_extracted()
    test_probe_confirms_a_genuinely_correct_interpretation()
    test_probe_catches_a_genuine_business_rule_mismatch()
    test_multiple_rules_only_fail_on_the_genuinely_wrong_one()
    test_returns_true_when_probe_synthesis_is_not_applicable()
    test_returns_true_on_real_probe_execution_failure_genuine_uncertainty()
    print("\nALL TESTING/QA BUSINESS-RULE PROBE TESTS PASSED")
