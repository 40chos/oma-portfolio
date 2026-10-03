"""Real bug found live (2026-08-08, site_50 benchmark task 007, "In the fieldjob list, I want a
quick filter button called 'Accepted' that shows only accepted records..."):
_extract_all_reproduction_targets()'s own final-round-widening extraction call hallucinated a
literal field requirement named 'accepted' from the FILTER BUTTON's own quoted UI label -- no
such field was ever meant to exist (the correct implementation filters by an EXISTING state
field's value, e.g. domain=[('state','=','accepted')]). None of _autocorrect_hallucinated_
reproduction_target()'s tiers could recover from this (there is no real 'accepted'-stem field to
correct to), so the regression check reported a false "an earlier constraint's own claimed field
no longer exists," escalating a real, working implementation as broken.

Fixed by adding an explicit anti-hallucination instruction to the extraction prompt,
distinguishing a UI control's own label (button/filter/menu text) from a genuine field
requirement. This test proves the fix is actually present in the prompt sent to the model --
mocks call_structured (no live LLM call), matching this test file's established sibling
(test_testing_qa_constitution_access.py's captured-prompt pattern).
"""

import os
import sys
import uuid
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import specialists.testing_qa.specialist as testing_qa_module
from contracts.schema import AutonomyTier, CapabilityClass, SpecialistType, TaskContract
from infra.gateway_client import ModelGatewayClient


def _contract(goal: str) -> TaskContract:
    return TaskContract(
        task_id=uuid.uuid4(), specialist_type=SpecialistType.bug_fix,
        capability_class=CapabilityClass.module_development, tier=AutonomyTier.tier_1_readonly,
        goal=goal, original_goal=goal, inputs=[], rules=[], deliverables=[],
        compensating_actions=[], validation_by="testing_qa", pause_if=[], turn_budget=10,
    )


def test_prompt_warns_against_treating_a_ui_control_label_as_a_field_claim():
    goal = (
        "In the fieldjob list, I want a quick filter button called 'Accepted' that shows only "
        "accepted records, and another called 'My records' that shows only records assigned to me."
    )
    contract = _contract(goal)
    specialist = testing_qa_module.TestingQASpecialist(client=ModelGatewayClient())

    captured = {}

    async def fake_call_structured(**kwargs):
        captured["prompt"] = kwargs["prompt"]
        return testing_qa_module.ReproductionTargetList(targets=[])

    with patch.object(testing_qa_module, "read_module_files", return_value={"models/models.py": "class X: pass"}):
        with patch.object(testing_qa_module, "call_structured", new=fake_call_structured):
            import asyncio
            asyncio.run(specialist._extract_all_reproduction_targets(contract, "oma_x"))

    prompt = captured["prompt"]
    assert "'Accepted'" in prompt or "Accepted" in prompt  # the real goal text made it into the prompt at all
    assert "BUTTON, FILTER, or MENU ITEM" in prompt, (
        "the anti-hallucination guidance distinguishing a UI control's own label from a real "
        "field claim must actually be present in the prompt sent to the model"
    )
    assert "is NOT a field claim" in prompt
    print("PASS: the extraction prompt explicitly warns against mistaking a filter/button/menu label for a field claim")


if __name__ == "__main__":
    test_prompt_warns_against_treating_a_ui_control_label_as_a_field_claim()
    print("\nALL UI-LABEL-GUARD TESTS PASSED")
