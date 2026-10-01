"""Phase 5 tests: the TaskContract/VerificationResult/SpecialistOutput
schemas, and -- the structurally important piece -- the registry
mechanism itself. Registers a trivial FAKE specialist (no real
specialist exists until Phases 9-11) and proves delegation can route to
it purely through registry.get(...), with zero changes to any other
code -- the concrete proof that "add a fourth specialist later" is a
real, working promise and not just an intention.
"""

import asyncio
import os
import sys
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from contracts.schema import (
    AutonomyTier,
    CapabilityClass,
    CompensatingAction,
    SpecialistOutput,
    SpecialistType,
    TaskContract,
    VerificationResult,
)
from infra.gateway_client import ModelGatewayClient
from manager.charter import check_sensitive_paths
from manager.classify import classify_capability_class
from specialists import registry
from specialists.base import Specialist

CLASSIFIER_MODEL = os.environ.get("OMA_MODEL_CLASSIFIER", "qwen3.6-27b")


def test_schema_round_trip_matches_the_plans_own_example():
    """The exact filled-in TaskContract example from §0.5.7, round-tripped
    through the real Pydantic model to confirm the schema as actually
    written accepts it byte-for-byte in shape.
    """
    contract = TaskContract(
        task_id=uuid.uuid4(),
        specialist_type=SpecialistType.bug_fix,
        capability_class=CapabilityClass.module_development,
        tier=AutonomyTier.tier_2_notify_after,
        goal="Add a 'preferred_language' field to res.partner (contacts), "
             "visible on the contact form view.",
        inputs=["res.partner model definition", "contact form view XML"],
        rules=["No changes to core Odoo modules", "No schema changes beyond "
               "the one new field", "Follow the odoo-module-scaffolding skill"],
        deliverables=["A new or extended module adding the field",
                      "The field visible and editable on the contact form"],
        compensating_actions=[
            CompensatingAction(
                forward_step="Install the module containing the new field",
                undo_action="Uninstall the module via the same install "
                            "mechanism, never a manual database edit",
            ),
        ],
        validation_by="testing_qa",
        pause_if=["the fix would touch anything on sensitive_paths",
                  "ambiguity about which form view should show the field"],
        turn_budget=15,
        retry_sub_budget=3,
    )
    assert contract.specialist_type == SpecialistType.bug_fix
    assert contract.tier == AutonomyTier.tier_2_notify_after
    assert contract.validation_by == "testing_qa"

    # Round-trip through JSON, as it would be stored in agent_memory_events'
    # JSONB detail column.
    dumped = contract.model_dump_json()
    reloaded = TaskContract.model_validate_json(dumped)
    assert reloaded == contract
    print("PASS: TaskContract matches §0.5.7's exact example and round-trips through JSON")


def test_verification_result_and_specialist_output_shapes():
    output = SpecialistOutput(
        task_id=uuid.uuid4(),
        specialist_type=SpecialistType.bug_fix,
        summary="Added the field and view change",
        detail={"module_written": "oma_contact_language"},
        claims_complete=True,
        artifacts=["oma_contact_language/models/res_partner.py"],
    )
    assert output.claims_complete is True

    result = VerificationResult(
        task_id=output.task_id,
        passed=True,
        reproduction_confirmed=True,
        uncovered_paths=[],
        coverage_diff="100% of the new field's read/write paths covered",
        spot_check_mismatch=False,
        notes="Confirmed field present and editable via a fresh XML-RPC read",
    )
    assert result.root_cause is None  # only set when passed=False
    print("PASS: SpecialistOutput and VerificationResult both construct correctly, "
          "distinct types (specialist never self-issues a VerificationResult)")


def test_registry_starts_empty_and_get_on_unregistered_type_raises_clearly():
    registry.clear()
    assert registry.registered_types() == []
    try:
        registry.get(SpecialistType.bug_fix)
        raised = False
    except registry.SpecialistNotAvailableError as exc:
        raised = True
        assert "bug_fix" in str(exc)
    assert raised, "expected a clear SpecialistNotAvailableError, not a silent stub"
    print("PASS: an unregistered SpecialistType raises a clear, loud error -- "
          "no hollow placeholder pretending to work")


async def _run_fake_specialist_delegation_test():
    """The concrete proof: a trivial fake specialist, registered purely
    through the registry, with the Manager's own code (charter.py,
    classify.py) completely untouched -- delegation happens by calling
    registry.get(...).run(contract) exactly the way Phase 6's real
    delegate_to_specialist() will, once it exists.
    """
    registry.clear()

    class EchoFakeSpecialist:
        """Just echoes its input back as a fake report -- proves the
        interface/registry mechanism, nothing about real specialist logic.
        """
        async def run(self, contract: TaskContract) -> SpecialistOutput:
            return SpecialistOutput(
                task_id=contract.task_id,
                specialist_type=contract.specialist_type,
                summary=f"[FAKE] echoed goal: {contract.goal}",
                detail={"echoed_inputs": contract.inputs},
                claims_complete=True,
                artifacts=[],
            )

    fake = EchoFakeSpecialist()
    assert isinstance(fake, Specialist), "EchoFakeSpecialist must satisfy the Specialist protocol"
    registry.register(SpecialistType.bug_fix, fake)
    assert registry.registered_types() == [SpecialistType.bug_fix]

    # Build a realistic contract using Phase 4's already-hardened cascade
    # (classify_capability_class already applies strip_think_block() +
    # the </think>-completeness check internally -- reused here rather
    # than re-derived, exactly the discipline established in Phases 3-4).
    client = ModelGatewayClient()
    try:
        goal_text = "Add a 'preferred_language' field to contacts, visible on the contact form."
        capability_class_label = await classify_capability_class(goal_text, client, CLASSIFIER_MODEL)
        assert capability_class_label == CapabilityClass.module_development.value

        tier = check_sensitive_paths(is_write=True)  # no sensitive-path hit for a field add
        assert tier == AutonomyTier.tier_2_notify_after.value

        contract = TaskContract(
            task_id=uuid.uuid4(),
            specialist_type=SpecialistType.bug_fix,
            capability_class=CapabilityClass(capability_class_label),
            tier=AutonomyTier(tier),
            goal=goal_text,
            inputs=["res.partner model definition"],
            rules=["Follow the odoo-module-scaffolding skill"],
            deliverables=["The field visible on the contact form"],
            compensating_actions=[
                CompensatingAction(forward_step="Install the module", undo_action="Uninstall it")
            ],
            validation_by="testing_qa",
            pause_if=[],
            turn_budget=15,
        )
    finally:
        await client.aclose()

    # This is the actual delegation call -- exactly what Phase 6's
    # delegate_to_specialist() tool will do internally, proven here
    # with zero changes needed to charter.py, classify.py, schema.py,
    # or registry.py itself.
    specialist = registry.get(contract.specialist_type)
    output = await specialist.run(contract)

    assert output.task_id == contract.task_id
    assert output.claims_complete is True
    assert "preferred_language" in output.summary
    print(f"PASS: delegation via registry.get(...).run(contract) worked end to end -- "
          f"contract built from real Phase 4 classification (capability_class="
          f"{capability_class_label!r}, tier={tier}), routed to a fake specialist "
          f"purely through the registry, zero other code touched: {output.summary!r}")

    registry.clear()


def test_import_linter_contract_is_configured_and_currently_kept():
    """Confirms the import-linter config exists and the contract is
    actually defined -- the real enforcement check itself is run via
    `./.venv/bin/lint-imports` (see README), not re-implemented in
    Python here, since import-linter's own CLI is the real mechanism.
    This test just guards against the config file silently disappearing.
    """
    pyproject = os.path.join(os.path.dirname(__file__), "..", "pyproject.toml")
    with open(pyproject) as f:
        content = f.read()
    assert "[tool.importlinter]" in content
    assert 'source_modules = ["manager"]' in content
    assert 'forbidden_modules = ["specialists"]' in content
    print("PASS: import-linter contract is configured in pyproject.toml "
          "(run `./.venv/bin/lint-imports` for the real, live check)")


if __name__ == "__main__":
    test_schema_round_trip_matches_the_plans_own_example()
    test_verification_result_and_specialist_output_shapes()
    test_registry_starts_empty_and_get_on_unregistered_type_raises_clearly()
    asyncio.run(_run_fake_specialist_delegation_test())
    test_import_linter_contract_is_configured_and_currently_kept()
    print("\nALL SPECIALISTS REGISTRY TESTS PASSED")
