"""Phase 30, P1c (Phase I, §12): unit tests for TestingQASpecialist's
new behavioral-probe orchestration (_run_behavioral_probe_check) --
mocked call_structured/run_behavioral_probe, no live SSH/DB/gateway
needed. A real, live-verified run against the actual gateway and dev
database is documented separately in
docs/reports/PHASE30_P1C_BEHAVIORAL_VERIFICATION_2026-07-30.md.
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
    ReproductionTarget,
    TestingQASpecialist,
)


def _make_contract(goal: str = "x", goal_facts=None) -> TaskContract:
    return TaskContract(
        task_id=uuid.uuid4(), specialist_type=SpecialistType.testing_qa,
        capability_class=CapabilityClass.module_development, tier=AutonomyTier.tier_2_notify_after,
        goal=goal, inputs=[], rules=[], deliverables=[], compensating_actions=[],
        validation_by="testing_qa", pause_if=[], turn_budget=10, retry_sub_budget=3,
        goal_facts=goal_facts or {},
    )


def test_probe_confirms_a_genuinely_correct_compute_field(monkeypatch):
    """The real, positive case: a probe creates a record, the real
    observed result matches what the goal claims, and the verdict is
    confirmed=True -- grounded in the REAL observed value, not the
    pre-probe prediction.
    """
    contract = _make_contract("A sale order's amount_total should sum its lines.")
    target = ReproductionTarget(model="sale.order", field_name="amount_total")

    call_count = {"n": 0}

    async def fake_call_structured(**kwargs):
        call_count["n"] += 1
        if kwargs["schema"] is BehavioralProbeSpec:
            return BehavioralProbeSpec(
                applicable=True,
                creates=[
                    BehavioralProbeCreate(model="sale.order", values={"partner_id": 14}),
                    BehavioralProbeCreate(model="sale.order.line", values={"order_id": "$0", "price_unit": 150.0}),
                ],
                target_index=0,
                check_fields=["amount_total"],
                expected_effect="amount_total should be 300",
            )
        assert kwargs["schema"] is BehavioralProbeVerdict
        return BehavioralProbeVerdict(behavior_confirmed=True, reasoning="real amount_total was 300 as predicted")

    async def run():
        with patch("specialists.testing_qa.specialist.call_structured", new=AsyncMock(side_effect=fake_call_structured)):
            with patch.object(tq_module, "run_behavioral_probe", return_value={"amount_total": 300.0}):
                with patch.object(tq_module, "read_module_files", return_value={"models/models.py": "x"}):
                    specialist = TestingQASpecialist(client=None)
                    return await specialist._run_behavioral_probe_check(contract, target, "oma_x", "db_x")

    result = asyncio.run(run())
    assert result is not None
    confirmed, message = result
    assert confirmed is True
    assert "300.0" in message
    assert call_count["n"] == 2, "must call synthesis then judgment as two separate calls"
    print("PASS: a genuinely correct compute field is confirmed, grounded in the real observed value")


def test_probe_catches_a_genuinely_wrong_compute_field(monkeypatch):
    """The real, negative case this whole priority exists for: the field
    EXISTS (would pass the old existence-only check) but its real
    computed value is wrong -- must be caught.
    """
    contract = _make_contract("A sale order's amount_total should sum its lines.")
    target = ReproductionTarget(model="sale.order", field_name="amount_total")

    async def fake_call_structured(**kwargs):
        if kwargs["schema"] is BehavioralProbeSpec:
            return BehavioralProbeSpec(
                applicable=True,
                creates=[BehavioralProbeCreate(model="sale.order", values={"partner_id": 14})],
                target_index=0, check_fields=["amount_total"], expected_effect="should reflect real lines",
            )
        return BehavioralProbeVerdict(
            behavior_confirmed=False, reasoning="real amount_total was 0, never actually computed from lines",
        )

    async def run():
        with patch("specialists.testing_qa.specialist.call_structured", new=AsyncMock(side_effect=fake_call_structured)):
            with patch.object(tq_module, "run_behavioral_probe", return_value={"amount_total": 0.0}):
                with patch.object(tq_module, "read_module_files", return_value={"models/models.py": "x"}):
                    specialist = TestingQASpecialist(client=None)
                    return await specialist._run_behavioral_probe_check(contract, target, "oma_x", "db_x")

    confirmed, message = asyncio.run(run())
    assert confirmed is False
    assert "never actually computed" in message
    print("PASS: a real, genuinely-wrong computed value is caught, not silently passed")


def test_returns_none_when_probe_is_not_applicable(monkeypatch):
    contract = _make_contract("x")
    target = ReproductionTarget(model="x.y", field_name="z")

    async def fake_call_structured(**kwargs):
        return BehavioralProbeSpec(applicable=False)

    async def run():
        with patch("specialists.testing_qa.specialist.call_structured", new=AsyncMock(side_effect=fake_call_structured)):
            with patch.object(tq_module, "run_behavioral_probe") as mock_run:
                with patch.object(tq_module, "read_module_files", return_value={}):
                    specialist = TestingQASpecialist(client=None)
                    result = await specialist._run_behavioral_probe_check(contract, target, "oma_x", "db_x")
                    mock_run.assert_not_called()
                    return result

    assert asyncio.run(run()) is None
    print("PASS: returns None (falls back to existence-only) when the probe isn't applicable, never fabricates a verdict")


def test_returns_none_when_the_real_probe_execution_fails(monkeypatch):
    """A real create/read failure (SSH hiccup, timeout, anything
    genuinely uncertain) must degrade to None -- never guessed as either
    pass or fail.
    """
    contract = _make_contract("x")
    target = ReproductionTarget(model="x.y", field_name="z")

    async def fake_call_structured(**kwargs):
        return BehavioralProbeSpec(
            applicable=True, creates=[BehavioralProbeCreate(model="x.y", values={})],
            target_index=0, check_fields=["z"], expected_effect="x",
        )

    async def run():
        with patch("specialists.testing_qa.specialist.call_structured", new=AsyncMock(side_effect=fake_call_structured)):
            with patch.object(tq_module, "run_behavioral_probe", return_value=None):
                with patch.object(tq_module, "read_module_files", return_value={}):
                    specialist = TestingQASpecialist(client=None)
                    return await specialist._run_behavioral_probe_check(contract, target, "oma_x", "db_x")

    assert asyncio.run(run()) is None
    print("PASS: a real execution failure degrades to None, never guessed as pass or fail")


if __name__ == "__main__":
    print("(this file requires pytest's monkeypatch/patch fixtures; run via pytest)")
