"""Phase 11: real tests for the Testing/QA specialist, against the
actual odoo16-dev container, a real fresh database, and the real model
gateway -- no mocks. Covers the build plan's own two required tests: a
deliberately broken fix (claims success but leaves a bug) must be
caught by reproduction_confirmed=False, and a deliberately incomplete
self-report (claims full coverage but leaves something untested) must
be caught by the deterministic spot-check, not this specialist's own
say-so.
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from contracts.schema import AutonomyTier, CapabilityClass, SpecialistType, TaskContract
from infra.gateway_client import ModelGatewayClient
from manager.tools import await_verification
from specialists import registry
from specialists.testing_qa.specialist import ReproductionTarget, SelfReportedCoverage, TestingQASpecialist
from tools_odoo.module_dev.toolchain import (
    _MODULE_DEV_ADDONS_DIR,
    _run_in_container,
    install_module,
    scaffold_module,
    write_module_file,
)

FRESH_DB = "odoo16_dev_fresh_20260708_061614"  # created for the Phase 16 QA pass reset (the prior fresh db was dropped in the state cleanup)

BROKEN_FIX_MODULE = "oma_broken_fix_test"
INCOMPLETE_COVERAGE_MODULE = "oma_incomplete_coverage_test"
GOOD_FIX_MODULE = "oma_good_fix_e2e_test"


def _cleanup_module_dir(module_name: str) -> None:
    _run_in_container(f"rm -rf {_MODULE_DEV_ADDONS_DIR}/{module_name}")
    _run_in_container(f"rm -f /tmp/oma_coverage_{module_name}.json")


def _make_contract(goal: str, deliverables: list[str], inputs: list[str]) -> TaskContract:
    import uuid

    return TaskContract(
        task_id=uuid.uuid4(),
        specialist_type=SpecialistType.testing_qa,
        capability_class=CapabilityClass.module_development,
        tier=AutonomyTier.tier_2_notify_after,
        goal=goal,
        inputs=inputs,
        rules=[],
        deliverables=deliverables,
        compensating_actions=[],
        validation_by="testing_qa",
        pause_if=[],
        turn_budget=10,
        retry_sub_budget=3,
    )


def test_deliberately_broken_fix_is_caught_by_reproduction_check():
    """A module that CLAIMS to add 'should_not_exist_field' but never
    actually defines it -- the deliberately broken fix the build plan's
    own step 3 asks for. Must be caught by a real reproduction_confirmed=False,
    never assumed true just because the install itself didn't error.
    """
    _cleanup_module_dir(BROKEN_FIX_MODULE)
    try:
        scaffold_module(BROKEN_FIX_MODULE)
        write_module_file(
            BROKEN_FIX_MODULE,
            "__manifest__.py",
            '{\n    "name": "oma_broken_fix_test",\n    "license": "LGPL-3",\n'
            '    "depends": ["base"],\n    "data": [],\n}\n',
        )
        # Deliberately does NOT define should_not_exist_field -- a real
        # bug where the claimed fix never actually happened.
        write_module_file(
            BROKEN_FIX_MODULE,
            "models/models.py",
            'from odoo import fields, models\n\n\n'
            'class BrokenFixPartner(models.Model):\n'
            '    _inherit = "res.partner"\n\n'
            '    a_completely_different_field = fields.Char(string="Not What Was Asked For")\n',
        )
        install_result = install_module(BROKEN_FIX_MODULE, FRESH_DB)
        assert install_result.success, f"setup: install itself should succeed cleanly: {install_result.message}"

        async def _drive():
            client = ModelGatewayClient()
            try:
                specialist = TestingQASpecialist(client=client)
                contract = _make_contract(
                    "Add a 'should_not_exist_field' field to res.partner.",
                    ["A 'should_not_exist_field' Char field on res.partner"],
                    [f"verify_module:{BROKEN_FIX_MODULE}:{FRESH_DB}"],
                )
                return await specialist.run(contract)
            finally:
                await client.aclose()

        output = asyncio.run(_drive())

        assert output.detail["reproduction_confirmed"] is False, (
            "the claimed field was never actually added -- reproduction MUST fail"
        )
        assert output.claims_complete is False
        print("PASS: a deliberately broken fix (claimed field never actually added) is correctly "
              "caught -- reproduction_confirmed=False, claims_complete=False, not assumed true "
              "just because install itself didn't error")
    finally:
        _cleanup_module_dir(BROKEN_FIX_MODULE)


class _AlwaysClaimsFullyCoveredSpecialist(TestingQASpecialist):
    """Test-only subclass: forces the self-report step to deliberately
    claim full coverage, regardless of what the model would actually
    say -- standing in for 'a fix whose author claims full coverage but
    actually left something untested,' per the build plan's own step 3.
    The point is proving the DETERMINISTIC spot-check catches this,
    independent of whether an LLM would naturally make this claim.
    """

    async def _self_report_coverage(self, contract, module_name) -> SelfReportedCoverage:
        return SelfReportedCoverage(
            believed_fully_covered=True,
            claimed_uncovered_paths=[],  # deliberately, falsely claims nothing is untested
            reasoning="(test-forced claim) everything is fully covered",
        )


def test_incomplete_self_report_is_caught_by_deterministic_spot_check():
    """A module with a real method containing a branch that's never
    executed by a plain install (confirmed working mechanism during
    this phase's own experimentation: coverage.py genuinely shows the
    method body as uncovered). The self-report is forced to claim full
    coverage anyway -- the deterministic, non-LLM spot-check must catch
    this discrepancy on its own, not trust the self-report.
    """
    _cleanup_module_dir(INCOMPLETE_COVERAGE_MODULE)
    try:
        scaffold_module(INCOMPLETE_COVERAGE_MODULE)
        write_module_file(
            INCOMPLETE_COVERAGE_MODULE,
            "__manifest__.py",
            '{\n    "name": "oma_incomplete_coverage_test",\n    "license": "LGPL-3",\n'
            '    "depends": ["base"],\n    "data": [],\n}\n',
        )
        write_module_file(
            INCOMPLETE_COVERAGE_MODULE,
            "models/models.py",
            'from odoo import fields, models\n\n\n'
            'class IncompleteCoveragePartner(models.Model):\n'
            '    _inherit = "res.partner"\n\n'
            '    incomplete_coverage_field = fields.Char(string="Incomplete Coverage Field")\n\n'
            '    def compute_something_untested(self):\n'
            '        if self.incomplete_coverage_field == "urgent":\n'
            '            return "URGENT_PATH"\n'
            '        else:\n'
            '            return "NORMAL_PATH"\n',
        )

        async def _drive():
            client = ModelGatewayClient()
            try:
                specialist = _AlwaysClaimsFullyCoveredSpecialist(client=client)
                contract = _make_contract(
                    "Add an 'incomplete_coverage_field' field to res.partner.",
                    ["An 'incomplete_coverage_field' Char field on res.partner"],
                    [f"verify_module:{INCOMPLETE_COVERAGE_MODULE}:{FRESH_DB}"],
                )
                return await specialist.run(contract)
            finally:
                await client.aclose()

        output = asyncio.run(_drive())

        assert output.detail["claimed_uncovered_paths"] == []
        assert len(output.detail["uncovered_paths"]) > 0, (
            "the real coverage.py run must show the method body's branches as genuinely uncovered"
        )
        assert output.detail["spot_check_mismatch"] is True, (
            "the deterministic spot-check must catch the discrepancy between the (forced) false "
            "claim of full coverage and the real, measured uncovered lines"
        )
        assert output.claims_complete is False, (
            "a real, unclaimed coverage gap must prevent claims_complete, even if reproduction "
            "itself passed"
        )
        print(f"PASS: the deterministic spot-check caught a real, unclaimed coverage gap "
              f"({output.detail['uncovered_paths']}) despite a forced false claim of full coverage -- "
              f"spot_check_mismatch=True, claims_complete correctly False")
    finally:
        _cleanup_module_dir(INCOMPLETE_COVERAGE_MODULE)


def test_await_verification_reads_real_spot_check_not_hardcoded_false():
    """Confirms the Phase 6 flagged limitation is genuinely fixed:
    manager.tools.await_verification() must read the REAL
    reproduction_confirmed/spot_check_mismatch from a real
    TestingQASpecialist's own detail dict, not the old hardcoded
    spot_check_mismatch=False. Uses a genuinely correct module (the
    claimed field really is added, really is covered by nothing more
    than its own declaration) -- the good-path case, complementing the
    two failure-path tests above.
    """
    _cleanup_module_dir(GOOD_FIX_MODULE)
    registry.clear()
    try:
        scaffold_module(GOOD_FIX_MODULE)
        write_module_file(
            GOOD_FIX_MODULE,
            "__manifest__.py",
            '{\n    "name": "oma_good_fix_e2e_test",\n    "license": "LGPL-3",\n'
            '    "depends": ["base"],\n    "data": [],\n}\n',
        )
        write_module_file(
            GOOD_FIX_MODULE,
            "models/models.py",
            'from odoo import fields, models\n\n\n'
            'class GoodFixPartner(models.Model):\n'
            '    _inherit = "res.partner"\n\n'
            '    good_fix_field = fields.Char(string="Good Fix Field")\n',
        )
        install_result = install_module(GOOD_FIX_MODULE, FRESH_DB)
        assert install_result.success, f"setup: install must succeed: {install_result.message}"

        async def _drive():
            client = ModelGatewayClient()
            try:
                registry.register(SpecialistType.testing_qa, TestingQASpecialist(client=client))
                contract = _make_contract(
                    "Add a 'good_fix_field' field to res.partner.",
                    ["A 'good_fix_field' Char field on res.partner"],
                    [f"verify_module:{GOOD_FIX_MODULE}:{FRESH_DB}"],
                )
                from contracts.schema import SpecialistOutput

                fake_build_output = SpecialistOutput(
                    task_id=contract.task_id,
                    specialist_type=SpecialistType.bug_fix,
                    summary="fake build output, not used by await_verification's own logic",
                    detail={},
                    claims_complete=True,
                )
                return await await_verification(contract, fake_build_output, client, "qwen3.6-27b")
            finally:
                await client.aclose()

        result = asyncio.run(_drive())

        assert result.reproduction_confirmed is True
        # A plain field declaration has no method body -- fully covered
        # by its own class-level declaration, so no real gap exists.
        assert result.spot_check_mismatch is False
        assert result.passed is True
        print("PASS: await_verification() reads REAL reproduction_confirmed/spot_check_mismatch "
              "from the real TestingQASpecialist -- the Phase 6 hardcoded-False limitation is "
              "genuinely fixed, confirmed end to end")
    finally:
        registry.clear()
        _cleanup_module_dir(GOOD_FIX_MODULE)


FALLBACK_MODULE = "oma_fallback_verify_test"


def test_await_verification_falls_back_to_last_known_module_when_this_round_lacks_one():
    """Real regression test for a live bug found in the Phase 16 QA
    pass: when a retry round is routed through code_review instead of
    bug_fix (manager.replanning.select_specialist_for_retry()'s own
    theme-recurrence branch), THAT round's build_output is
    CodeReviewSpecialist's own output -- {mode, audited_path,
    files_read, findings}, never module_name/db, since Code-Review
    reviews an existing diff rather than scaffolding one. Before the
    fix, testing_qa validation then had nothing to inject a
    verify_module: entry from and failed instantly with "contract.inputs
    contains no 'verify_module:' entry" -- confirmed live: a real
    service.record task's round 4 failed exactly this way, right after
    two rounds of genuine reproduction work, wasting a round.

    Confirms manager.tools.await_verification()'s fallback_module_name/
    fallback_db parameters (populated by manager.loop.run_plan()'s own
    running memory of the last round that WAS Build) let a real
    TestingQASpecialist reproduction check proceed even when THIS
    round's own build_output carries no module info at all.
    """
    _cleanup_module_dir(FALLBACK_MODULE)
    registry.clear()
    try:
        scaffold_module(FALLBACK_MODULE)
        write_module_file(
            FALLBACK_MODULE,
            "__manifest__.py",
            '{\n    "name": "oma_fallback_verify_test",\n    "license": "LGPL-3",\n'
            '    "depends": ["base"],\n    "data": [],\n}\n',
        )
        write_module_file(
            FALLBACK_MODULE,
            "models/models.py",
            'from odoo import fields, models\n\n\n'
            'class FallbackVerifyPartner(models.Model):\n'
            '    _inherit = "res.partner"\n\n'
            '    fallback_verify_field = fields.Char(string="Fallback Verify Field")\n',
        )
        install_result = install_module(FALLBACK_MODULE, FRESH_DB)
        assert install_result.success, f"setup: install must succeed: {install_result.message}"

        async def _drive():
            client = ModelGatewayClient()
            try:
                registry.register(SpecialistType.testing_qa, TestingQASpecialist(client=client))
                contract = _make_contract(
                    "Add a 'fallback_verify_field' field to res.partner.",
                    ["A 'fallback_verify_field' Char field on res.partner"],
                    [],  # deliberately empty -- no verify_module: entry, matching the real bug
                )
                from contracts.schema import SpecialistOutput

                # This round's own output is shaped exactly like a real
                # CodeReviewSpecialist result -- no module_name/db at all.
                code_review_shaped_output = SpecialistOutput(
                    task_id=contract.task_id,
                    specialist_type=SpecialistType.code_review,
                    summary="Reviewed the existing diff, no new blocking findings.",
                    detail={"mode": "diff_review", "findings": []},
                    claims_complete=True,
                )
                return await await_verification(
                    contract, code_review_shaped_output, client, "qwen3.6-27b",
                    fallback_module_name=FALLBACK_MODULE, fallback_db=FRESH_DB,
                )
            finally:
                await client.aclose()

        result = asyncio.run(_drive())

        assert result.reproduction_confirmed is True, (
            f"expected the fallback module/db to let real reproduction succeed, got: {result.notes}"
        )
        assert result.passed is True
        assert "contains no" not in result.notes, (
            "the degenerate 'contract.inputs contains no verify_module:' failure must not occur "
            "when a real fallback is available"
        )
        print("PASS: await_verification() genuinely falls back to the last known module/db when "
              "this round's own build_output (e.g. a code_review-routed round) carries none -- "
              "the real live bug is fixed and confirmed end to end")
    finally:
        registry.clear()
        _cleanup_module_dir(FALLBACK_MODULE)


def test_await_verification_uses_build_summary_when_module_name_present_but_db_missing():
    """Real, confirmed bug found live (2026-08-08, task 07141af5-9a4e-41b6-93ea-8b7af04fea9c's
    flagship run, service_ticket_model node, rounds 1 and 3 both): the original guard only
    checked for "module_name" missing entirely -- but specialists/build/specialist.py's own
    sandbox_failed early-return genuinely includes a real "module_name" (the sandbox write
    happened) while NEVER including "db" (the real target db was never touched, since the
    sandbox failed before getting there). On the very first round of a brand-new constraint
    node, no fallback_db exists yet either, so `db` resolved to None, no verify_module: entry
    was ever added, and Build's own real, specific sandbox error (visible the whole time in
    build_output.summary) was discarded in favor of the useless generic "nothing concrete to
    verify" message -- escalating to a human decision for a problem whose real cause was never
    even shown. Confirmed live via the exact real shape reproduced here.
    """
    async def _drive():
        client = ModelGatewayClient()
        try:
            registry.register(SpecialistType.testing_qa, TestingQASpecialist(client=client))
            contract = _make_contract("Add a field to res.partner.", ["A field"], [])
            from contracts.schema import SpecialistOutput

            sandbox_failed_shaped_output = SpecialistOutput(
                task_id=contract.task_id,
                specialist_type=SpecialistType.bug_fix,
                summary="Sandbox pre-flight: install failed -- ParseError: real, specific sandbox error detail.",
                detail={
                    "module_name": "oma_some_real_module",
                    "sandbox_failed": True,
                    "sandbox_log_tail": "real log tail",
                },
                claims_complete=False,
            )
            return await await_verification(contract, sandbox_failed_shaped_output, client, "qwen3.6-27b")
        finally:
            await client.aclose()

    try:
        result = asyncio.run(_drive())
        assert result.passed is False
        assert "real, specific sandbox error detail" in result.notes, (
            f"expected Build's own real sandbox failure reason to be used directly -- got: "
            f"{result.notes!r}"
        )
        assert "contains no" not in result.notes, (
            "must never fall through to testing_qa's generic 'nothing concrete to verify' "
            "message when Build's own real failure reason is already known"
        )
        print("PASS: a sandbox-failed round (module_name present, db genuinely absent, no "
              "fallback) uses Build's own real failure reason directly, closing the real live "
              "gap found on task 07141af5's service_ticket_model node")
    finally:
        registry.clear()


def test_await_verification_still_fails_honestly_with_no_fallback_available():
    """Regression guard for the original, still-correct behavior: with
    NO module_name/db anywhere (this round's own output nor any
    fallback), testing_qa must still fail honestly and clearly --
    never silently invent a target or crash.
    """
    async def _drive():
        client = ModelGatewayClient()
        try:
            registry.register(SpecialistType.testing_qa, TestingQASpecialist(client=client))
            contract = _make_contract("Add a field to res.partner.", ["A field"], [])
            from contracts.schema import SpecialistOutput

            empty_output = SpecialistOutput(
                task_id=contract.task_id,
                specialist_type=SpecialistType.code_review,
                summary="Reviewed.",
                detail={"mode": "diff_review", "findings": []},
                claims_complete=True,
            )
            return await await_verification(contract, empty_output, client, "qwen3.6-27b")
        finally:
            await client.aclose()

    try:
        result = asyncio.run(_drive())
        assert result.passed is False
        assert "contains no" in result.notes
        print("PASS: with genuinely no fallback available, await_verification() still fails "
              "honestly with a clear message, never a crash or a fabricated pass")
    finally:
        registry.clear()


if __name__ == "__main__":
    test_deliberately_broken_fix_is_caught_by_reproduction_check()
    test_incomplete_self_report_is_caught_by_deterministic_spot_check()
    test_await_verification_reads_real_spot_check_not_hardcoded_false()
    test_await_verification_falls_back_to_last_known_module_when_this_round_lacks_one()
    test_await_verification_uses_build_summary_when_module_name_present_but_db_missing()
    test_await_verification_still_fails_honestly_with_no_fallback_available()
    print("\nALL TESTING/QA SPECIALIST TESTS PASSED")
