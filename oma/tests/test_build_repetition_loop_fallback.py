"""Real bug found live (2026-07-24, task 020 resume, real SITE
benchmark): the scoped-edit retry path (specialists/build/specialist.py
BuildSpecialist._generate_code()) asks the model to reproduce a `target`
span character-for-character inside a JSON-escaped string under
grammar-constrained decoding -- for a large enough span this is a
genuinely harder generation shape than ordinary free-form writing.
Confirmed live: 7 straight resume attempts against the real coder
backend all failed with LLMRepetitionLoopError, including 4 AFTER
infra.gateway_client's own temperature-escalation fix (see
tests/test_gateway_client.py's _run_repetition_loop_temperature_
escalation_test) was deployed and confirmed working -- this is not
noise a retry can fix, it's a structural difficulty with the scoped-
edit generation shape itself for this specific content.

The fix: when the scoped-edit call exhausts its retries specifically
because of a repetition loop (never any other failure -- a genuine
gateway outage must still propagate and pause the task exactly as
before), _generate_code() falls back ONCE to the full-generation path,
passing the current committed content as `fallback_rewrite_context` so
the rewrite can preserve everything unrelated to this round's fix
instead of regenerating from a blank slate.

This test exercises ONLY the routing decision (does a repetition-loop
failure trigger the fallback, does any other GatewayUnavailableError
propagate unchanged) against a fully mocked BuildSpecialist -- no live
GPU host, no real Odoo container, runs in well under a second. The
prompt content the fallback actually sends is NOT re-validated here
(the existing, real end-to-end tests in test_build_specialist.py cover
prompt/schema correctness); this test's only job is proving the
control flow around a real, confirmed live incident.
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from contracts.schema import AutonomyTier, CapabilityClass, SpecialistType, TaskContract
from infra.gateway_client import GatewayUnavailableError, LLMRepetitionLoopError, ModelGatewayClient
from specialists.build.specialist import BuildSpecialist, GeneratedModuleFiles, ManifestFields


def _make_contract() -> TaskContract:
    return TaskContract(
        task_id=uuid.uuid4(),
        specialist_type=SpecialistType.bug_fix,
        capability_class=CapabilityClass.module_development,
        tier=AutonomyTier.tier_2_notify_after,
        goal="send a confirmation email when accepted",
        inputs=[],
        rules=[],
        deliverables=[],
        compensating_actions=[],
        validation_by="testing_qa",
        pause_if=[],
        turn_budget=15,
    )


def _fake_generated() -> GeneratedModuleFiles:
    return GeneratedModuleFiles(
        manifest_fields=ManifestFields(
            name="x", version="1.0", category="Uncategorized", summary="", author="",
            depends=["base"], data=[],
        ),
        models_py="from odoo import models\n",
        security_csv="id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink\n",
        notes="",
    )


async def _run_repetition_loop_triggers_fallback_test():
    specialist = BuildSpecialist(client=ModelGatewayClient.__new__(ModelGatewayClient), db="unused")
    contract = _make_contract()
    prior_files = {"models/models.py": "from odoo import models\n"}

    async def _fake_scoped_edits(*args, **kwargs):
        raise GatewayUnavailableError("generate() (streaming) failed after retries") \
            from LLMRepetitionLoopError("repeated the same span 6+ times")

    fallback_calls = []

    async def _fake_full_gen(contract_arg, constitution_text, skill_text, module_name,
                              depends_on_module=None, target_module_files=None,
                              prior_files=None, temperature=0.0, fallback_rewrite_context=None):
        fallback_calls.append(fallback_rewrite_context)
        return _fake_generated()

    specialist._generate_scoped_edits = _fake_scoped_edits
    original_generate_code_raw = BuildSpecialist._generate_code_raw.__get__(specialist)

    # Real, confirmed staleness fix (2026-08-09): this test previously patched
    # `specialist._generate_code`, expecting the real method's own catch-and-fallback logic to
    # recursively call THAT patched attribute -- but a since-landed refactor moved the retry/
    # fallback routing (the code this test actually exercises) into `_generate_code_raw()`,
    # which recurses into ITSELF (`self._generate_code_raw(..., fallback_rewrite_context=...)`),
    # never `self._generate_code`. The old patch target was silently never hit, and the real
    # recursive call fell through to a genuine, unmocked network call -- confirmed live via a
    # real `AttributeError` crash deep inside ModelGatewayClient.generate() the moment this
    # drifted-but-never-noticed staleness became reachable again. Patching the correct,
    # current recursion target restores real coverage of this incident's own fix.
    specialist._generate_code_raw = _fake_full_gen

    result = await original_generate_code_raw(
        contract, "constitution", "skill", "module", None, None,
        prior_files=prior_files, temperature=0.0,
    )

    assert result.models_py == "from odoo import models\n", (
        f"expected the fallback's fake generation result, got {result!r}"
    )
    assert len(fallback_calls) == 1, (
        f"expected the fallback to be invoked exactly once, got {len(fallback_calls)}"
    )
    assert fallback_calls[0] == prior_files, (
        "fallback must receive the exact prior_files as fallback_rewrite_context, "
        f"so it can preserve currently-committed content -- got {fallback_calls[0]!r}"
    )
    print("PASS: a repetition-loop failure on the scoped-edit path triggers the "
          "context-aware full-rewrite fallback exactly once, with the real prior content passed through")


async def _run_non_repetition_gateway_error_propagates_test():
    """A genuine gateway outage (not a repetition loop) must NOT trigger
    the fallback -- it has to propagate unchanged so the task still
    pauses honestly via the normal gateway-outage path, exactly as
    before this fix.
    """
    specialist = BuildSpecialist(client=ModelGatewayClient.__new__(ModelGatewayClient), db="unused")
    contract = _make_contract()
    prior_files = {"models/models.py": "from odoo import models\n"}

    async def _fake_scoped_edits(*args, **kwargs):
        raise GatewayUnavailableError("connection refused") from ConnectionError("refused")

    specialist._generate_scoped_edits = _fake_scoped_edits

    try:
        await BuildSpecialist._generate_code_raw(
            specialist, contract, "constitution", "skill", "module", None, None,
            prior_files=prior_files, temperature=0.0,
        )
        raise AssertionError("expected GatewayUnavailableError to propagate, it was swallowed instead")
    except GatewayUnavailableError as exc:
        assert not isinstance(exc.__cause__, LLMRepetitionLoopError)
        print("PASS: a non-repetition-loop GatewayUnavailableError propagates unchanged, "
              "never silently redirected into the fallback")


async def _run_scoped_edit_application_error_triggers_fallback_test():
    """Real, general fix (2026-07-24, 50-task sequential re-run, task
    001): a scoped-edit `target` string that doesn't literally appear
    in the prior validated content (ScopedEditApplicationError) is the
    same underlying "scoped-edit is a structurally harder generation
    shape" problem as LLMRepetitionLoopError, just a different failure
    signature -- confirmed live to recur across many different,
    unrelated tasks this session, every occurrence previously burning
    a full round for nothing since nothing routed it to the fallback.
    """
    from specialists.build.specialist import ScopedEditApplicationError

    specialist = BuildSpecialist(client=ModelGatewayClient.__new__(ModelGatewayClient), db="unused")
    contract = _make_contract()
    prior_files = {"models/models.py": "from odoo import models\n"}

    async def _fake_scoped_edits(*args, **kwargs):
        raise ScopedEditApplicationError(
            "scoped edit search_replace target 'xyz' was not found in the prior validated content"
        )

    fallback_calls = []

    async def _fake_full_gen(contract_arg, constitution_text, skill_text, module_name,
                              depends_on_module=None, target_module_files=None,
                              prior_files=None, temperature=0.0, fallback_rewrite_context=None):
        fallback_calls.append(fallback_rewrite_context)
        return _fake_generated()

    specialist._generate_scoped_edits = _fake_scoped_edits
    original_generate_code_raw = BuildSpecialist._generate_code_raw.__get__(specialist)
    specialist._generate_code_raw = _fake_full_gen

    result = await original_generate_code_raw(
        contract, "constitution", "skill", "module", None, None,
        prior_files=prior_files, temperature=0.0,
    )

    assert result.models_py == "from odoo import models\n", (
        f"expected the fallback's fake generation result, got {result!r}"
    )
    assert len(fallback_calls) == 1, (
        f"expected the fallback to be invoked exactly once, got {len(fallback_calls)}"
    )
    assert fallback_calls[0] == prior_files, (
        "fallback must receive the exact prior_files as fallback_rewrite_context -- "
        f"got {fallback_calls[0]!r}"
    )
    print("PASS: a ScopedEditApplicationError (target-not-found/ambiguous/missing-files) "
          "triggers the same context-aware full-rewrite fallback as a repetition loop")


if __name__ == "__main__":
    asyncio.run(_run_repetition_loop_triggers_fallback_test())
    asyncio.run(_run_non_repetition_gateway_error_propagates_test())
    asyncio.run(_run_scoped_edit_application_error_triggers_fallback_test())
    print("\nALL BUILD REPETITION-LOOP FALLBACK TESTS PASSED")
