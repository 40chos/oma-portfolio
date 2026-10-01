"""Phase 33 follow-up (2026-08-11): unit test for
specialists/testing_qa/specialist.py's _extract_security_access_claim() --
real, confirmed live gap: for a plain-role-phrase ACL restriction goal ("only
Administrators should see X"), the LLM extraction step invented a fabricated
xmlid ("oma.group_admin") never stated anywhere in the goal, causing a FALSE
mismatch failure against Build's own now-correct, deterministically-resolved
real code. Fixed by trying the SAME deterministic extractor Build itself uses
FIRST, never letting an LLM guess a specific group identifier this pipeline
already has a real way to resolve. This test asserts the LLM path is never
even reached for this shape -- a client whose call would raise proves it.
"""

import asyncio
import os
import sys
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from contracts.schema import AutonomyTier, CapabilityClass, SpecialistType, TaskContract
from specialists.testing_qa.specialist import TestingQASpecialist


class _RaisingClient:
    """Any real LLM call through this client fails the test -- proving the
    deterministic short-circuit never falls through to the LLM extraction."""

    def pop_call_stats(self, task_id):
        return {"count": 0, "duration_sec": 0.0}


def _make_contract(goal: str) -> TaskContract:
    return TaskContract(
        task_id=uuid.uuid4(),
        specialist_type=SpecialistType.testing_qa,
        capability_class=CapabilityClass.module_development,
        tier=AutonomyTier.tier_2_notify_after,
        goal=goal,
        inputs=[],
        rules=[],
        deliverables=[],
        compensating_actions=[],
        validation_by="testing_qa",
        pause_if=[],
        turn_budget=10,
        retry_sub_budget=3,
    )


def test_field_shape_never_calls_the_llm_and_uses_the_real_external_id(monkeypatch):
    import tools_odoo.odoo_schema_client as schema_client_module
    monkeypatch.setattr(schema_client_module, "check_group_exists_fast", lambda db, name: name == "Settings")
    monkeypatch.setattr(schema_client_module, "resolve_group_external_id_fast", lambda db, name: "base.group_system")

    goal = (
        "On the punchlist form, only Administrators should see the internal escalation note "
        "field.\n\nModule: oma_add_a_project_punchlist_c3a43045\nModel: project.punchlist (inherit)\n"
        "Field: internal_escalation_note (Text)\n"
    )
    contract = _make_contract(goal)
    specialist = TestingQASpecialist(client=_RaisingClient())

    claim = asyncio.run(specialist._extract_security_access_claim(contract, "odoo16_dev"))

    assert claim.applicable is True
    assert claim.model == "project.punchlist"
    assert claim.restricted_field_name == "internal_escalation_note"
    assert claim.restricted_group_xmlid == "base.group_system"
    print("PASS: the field-shape ACL claim is resolved deterministically, real external ID, no LLM call")


if __name__ == "__main__":
    print("(this file requires pytest's monkeypatch fixture; run via pytest)")
