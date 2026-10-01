"""Phase 15 (§19.9): tests 1, 2, 3, and 7 -- the reflect-and-retry
inner loop. Real Manager loop, real Postgres/Redis, real model gateway
where an LLM call is genuinely involved (root-cause classification);
specialists are synthetic/fake here specifically because these tests
need to engineer PRECISE, controlled failure sequences (fail once then
pass; fail forever) -- the real specialists are already tested against
real infra in their own dedicated test files (Phases 9-11, 13).
"""

import asyncio
import os
import sys
import time
import uuid
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from contracts.schema import AutonomyTier, CapabilityClass, ReplanRound, SpecialistOutput, SpecialistType, TaskContract, VerificationResult
from infra.gateway_client import ModelGatewayClient
from manager.learning import check_repeated_failures, check_repeated_failures_across_modules
from manager.loop import run_turn
from manager.memory import read_project_memory
from manager.replanning import select_specialist_for_retry
from manager.tools import append_project_memory
from specialists import registry

CLASSIFIER_MODEL = os.environ.get("OMA_MODEL_CLASSIFIER", "qwen3.6-27b")


class FailsNTimesThenPasses:
    """A fake specialist engineered to fail for a genuinely fixable
    reason (its own fixed number of times), then pass -- lets a test
    control EXACTLY how many rounds occur, unlike a real specialist.
    """
    def __init__(self, fail_count: int):
        self.fail_count = fail_count
        self.calls = 0
        self.seen_rules: list[list[str]] = []

    async def run(self, contract: TaskContract) -> SpecialistOutput:
        self.calls += 1
        self.seen_rules.append(list(contract.rules))
        ok = self.calls > self.fail_count
        return SpecialistOutput(
            task_id=contract.task_id, specialist_type=contract.specialist_type,
            summary=f"[FAKE call {self.calls}] {contract.goal}",
            detail={"uncovered_paths": [], "coverage_diff": "N/A"},
            claims_complete=ok, artifacts=[],
        )


class AlwaysFails:
    async def run(self, contract: TaskContract) -> SpecialistOutput:
        return SpecialistOutput(
            task_id=contract.task_id, specialist_type=contract.specialist_type,
            summary=f"[FAKE ALWAYS FAILS] {contract.goal}",
            detail={"uncovered_paths": [], "coverage_diff": "N/A"},
            claims_complete=False, artifacts=[],
        )


async def _run_test1_measurably_different_second_round_succeeds(client):
    """Test 1: a task engineered to fail once for a genuinely fixable
    reason, then succeed on round 2 -- confirm round 2's contract is
    measurably different (not a byte-for-byte copy) and that it
    succeeds, with a real ReplanRound row written and readable back.
    """
    registry.clear()
    specialist = FailsNTimesThenPasses(fail_count=1)
    registry.register(SpecialistType.code_review, specialist)

    message = "Do a full read-only quality audit of the custom Odoo codebase."
    result = await run_turn(message, client, CLASSIFIER_MODEL, anticipated_scope={"planning_round_budget": 3})

    assert result["status"] == "completed"
    assert result["passed"] is True
    assert result["rounds_taken"] == 2, f"expected exactly 2 rounds, got {result['rounds_taken']}"
    assert specialist.calls == 2

    # The contract actually seen on round 2 must be measurably
    # different from round 1's -- never a byte-for-byte copy.
    assert specialist.seen_rules[0] != specialist.seen_rules[1]
    assert len(specialist.seen_rules[1]) > len(specialist.seen_rules[0]), (
        "round 2's contract must carry NEW rules describing round 1's specific failure"
    )

    rows = _read_replan_rounds(result["task_id"])
    assert len(rows) == 1, f"expected exactly 1 replan_round row (for the 1 failed round), got {len(rows)}"
    print(f"PASS: round 2's revised contract is measurably different from round 1's "
          f"({len(specialist.seen_rules[0])} -> {len(specialist.seen_rules[1])} rules), it "
          f"succeeded, and a real ReplanRound row is readable back (row #{rows[0]['id']})")
    registry.clear()


def _read_replan_rounds(task_id: str) -> list[dict]:
    import psycopg2
    import psycopg2.extras
    from infra.settings import load_postgres_settings

    s = load_postgres_settings()
    conn = psycopg2.connect(host=s.host, port=s.port, dbname=s.db, user=s.user, password=s.password)
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            "SELECT * FROM agent_memory_events WHERE event_type='replan_round' AND task_id=%s ORDER BY id",
            (task_id,),
        )
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


async def _run_test2_select_specialist_for_retry_switches_on_recurring_finding(client):
    """Test 2: the SAME underlying Code-Review finding recurring across
    two rounds unaddressed -- confirm select_specialist_for_retry()
    changes strategy (routes to code_review itself) rather than blindly
    retrying Build the same way a third time.

    Real, structural fix verified here, not a synthetic exact-match
    alone: a real Phase 15 verification pass found that real Code-Review
    output rephrases its complaints every round even for a persistent
    issue, so exact string equality never actually fired. This test
    exercises the real fix (classify_findings_same_theme(), a real
    cascade that can genuinely call the real gateway) against REPHRASED
    findings describing the same underlying problem, not just identical
    strings.
    """
    contract = TaskContract(
        task_id=uuid.uuid4(), specialist_type=SpecialistType.bug_fix, capability_class=CapabilityClass.module_development,
        tier=AutonomyTier.tier_1_readonly, goal="x", inputs=[], rules=[], deliverables=[], compensating_actions=[],
        validation_by="testing_qa", pause_if=[], turn_budget=10,
    )
    v = VerificationResult(task_id=contract.task_id, passed=False, reproduction_confirmed=True,
                            uncovered_paths=[], coverage_diff="", spot_check_mismatch=False, notes="x")

    # No recurrence yet -- defaults to the same specialist.
    no_recurrence = await select_specialist_for_retry(contract, v, [], client, CLASSIFIER_MODEL)
    assert no_recurrence == SpecialistType.bug_fix

    # Identical strings -- the old exact-match case, still must work via the fast path.
    same_finding = "hardcoded credential in models.py"
    r1 = ReplanRound(round_number=1, previous_contract_task_id=str(contract.task_id), verification_result=v,
                      code_review_finding_summary=same_finding, revision_reasoning="r1", new_contract=contract)
    r2 = ReplanRound(round_number=2, previous_contract_task_id=str(contract.task_id), verification_result=v,
                      code_review_finding_summary=same_finding, revision_reasoning="r2", new_contract=contract)
    switched = await select_specialist_for_retry(contract, v, [r1, r2], client, CLASSIFIER_MODEL)
    assert switched == SpecialistType.code_review, (
        f"identical findings recurring across 2 rounds must switch strategy, got {switched}"
    )

    # REPHRASED findings describing the SAME real problem -- the actual
    # gap found live (Phase 15's real 4-round escalation: "Missing
    # 'project' and 'product' dependencies required by model fields and
    # views" vs "the required project and product module dependencies
    # are still absent from the manifest") -- must still switch.
    r1_rephrased = ReplanRound(
        round_number=1, previous_contract_task_id=str(contract.task_id), verification_result=v,
        code_review_finding_summary=(
            "Missing 'project' and 'product' dependencies required by model fields and views."
        ),
        revision_reasoning="r1", new_contract=contract,
    )
    r2_rephrased = ReplanRound(
        round_number=2, previous_contract_task_id=str(contract.task_id), verification_result=v,
        code_review_finding_summary=(
            "The manifest still does not declare the required project and product module "
            "dependencies needed by the fields defined on this model."
        ),
        revision_reasoning="r2", new_contract=contract,
    )
    switched_rephrased = await select_specialist_for_retry(
        contract, v, [r1_rephrased, r2_rephrased], client, CLASSIFIER_MODEL
    )
    assert switched_rephrased == SpecialistType.code_review, (
        f"two REPHRASED findings describing the same real underlying problem must still switch "
        f"strategy (this is the actual real-world gap found live), got {switched_rephrased}"
    )

    # A genuinely DIFFERENT finding each round must NOT trigger the switch.
    r2_different = ReplanRound(round_number=2, previous_contract_task_id=str(contract.task_id), verification_result=v,
                                code_review_finding_summary="The QWeb template uses inline JavaScript that violates CSP.",
                                revision_reasoning="r2", new_contract=contract)
    not_switched = await select_specialist_for_retry(contract, v, [r1, r2_different], client, CLASSIFIER_MODEL)
    assert not_switched == SpecialistType.bug_fix, (
        "two genuinely DIFFERENT findings across rounds must not trigger the switch -- only a real recurrence"
    )
    print("PASS: select_specialist_for_retry() switches strategy when the SAME underlying "
          "Code-Review finding recurs across 2 rounds -- including when REPHRASED, not just "
          "identical strings -- and never switches on two genuinely different findings")


async def _run_test2b_code_review_is_always_a_one_round_detour(client):
    """Real regression test for a GENERAL, structural bug found live
    (Phase 16 QA pass, not specific to any one task): code_review was
    always meant to be a one-round diagnostic detour -- this function's
    own original docstring already said "before the next Build
    attempt" -- but the code never actually routed back to bug_fix
    afterward. Once the theme-recurrence branch picked code_review
    once, the old fallback (`return contract.specialist_type`) kept
    returning code_review forever, since contract.specialist_type was
    already code_review from that point on.

    Confirmed live: a real scheduling+product-scoping task got stuck on
    code_review for two rounds straight (rounds 4 and 5), never
    re-running Build to actually fix the real AttributeError
    Code-Review itself had already identified, and escalated having
    made zero further progress after round 3. This is a GENERAL defect
    -- any task that ever hits the theme-recurrence trigger would have
    the same fate, not just this one. Fixed: whatever ran as
    code_review unconditionally routes back to bug_fix the very next
    round, regardless of theme recurrence or any other signal.
    """
    contract_after_code_review = TaskContract(
        task_id=uuid.uuid4(), specialist_type=SpecialistType.code_review,
        capability_class=CapabilityClass.module_development,
        tier=AutonomyTier.tier_1_readonly, goal="x", inputs=[], rules=[], deliverables=[],
        compensating_actions=[], validation_by="testing_qa", pause_if=[], turn_budget=10,
    )
    v = VerificationResult(task_id=contract_after_code_review.task_id, passed=False, reproduction_confirmed=True,
                            uncovered_paths=[], coverage_diff="", spot_check_mismatch=False, notes="x")

    # No prior rounds at all -- still must not stay on code_review.
    result_no_history = await select_specialist_for_retry(contract_after_code_review, v, [], client, CLASSIFIER_MODEL)
    assert result_no_history == SpecialistType.bug_fix, (
        f"a round that just ran as code_review must always route back to bug_fix next, got {result_no_history}"
    )

    # Even with a STILL-recurring same-themed finding (the exact real
    # scenario that got a real task stuck) -- code_review must not win
    # again immediately; it gets exactly one round, then back to Build.
    same_finding = "AttributeError: 'project.project' object has no attribute 'allowed_product_ids'"
    r1 = ReplanRound(round_number=3, previous_contract_task_id=str(contract_after_code_review.task_id), verification_result=v,
                      code_review_finding_summary=same_finding, revision_reasoning="r3", new_contract=contract_after_code_review)
    r2 = ReplanRound(round_number=4, previous_contract_task_id=str(contract_after_code_review.task_id), verification_result=v,
                      code_review_finding_summary=same_finding, revision_reasoning="r4", new_contract=contract_after_code_review)
    result_with_recurrence = await select_specialist_for_retry(
        contract_after_code_review, v, [r1, r2], client, CLASSIFIER_MODEL
    )
    assert result_with_recurrence == SpecialistType.bug_fix, (
        f"code_review must never be picked two rounds in a row, even with a recurring finding "
        f"that would otherwise trigger the switch -- got {result_with_recurrence}"
    )
    print("PASS: select_specialist_for_retry() never lets code_review run two rounds in a row -- "
          "it's genuinely a one-round detour, always routing back to bug_fix next, exactly as the "
          "function's own docstring always claimed but the code never actually did")


async def _run_test3_round_budget_stops_the_loop(client):
    """Test 3a: a genuinely unfixable task -- confirm the round budget
    stops the loop and ask_operator() is actually called, with the real
    round trace attached.
    """
    registry.clear()
    registry.register(SpecialistType.code_review, AlwaysFails())

    with patch("manager.loop.ask_operator") as mock_ask_operator:
        result = await run_turn(
            "Do a full read-only quality audit of the custom Odoo codebase.",
            client, CLASSIFIER_MODEL,
            anticipated_scope={"planning_round_budget": 2, "round_wall_clock_cap_seconds": 999999},
        )
        assert mock_ask_operator.called, "ask_operator() must actually be called when the round budget is exhausted"

    assert result["status"] == "paused"
    assert result["reason"] == "ask_operator"
    assert len(result["prior_rounds"]) == 1, (
        f"with planning_round_budget=2, exactly 1 round must complete (and be logged) before "
        f"escalating on round 2, got {len(result['prior_rounds'])}"
    )
    print(f"PASS: the round budget (2) independently stops the loop, ask_operator() was actually "
          f"called, and the real round trace ({len(result['prior_rounds'])} round(s)) is attached")
    registry.clear()


async def _run_test3_wall_clock_cap_stops_the_loop(client):
    """Test 3b: the wall-clock cap independently stops the loop, tested
    separately from the round budget -- a generous round budget (100)
    but a near-zero wall-clock cap must still escalate quickly.
    """
    registry.clear()
    registry.register(SpecialistType.code_review, AlwaysFails())

    with patch("manager.loop.ask_operator") as mock_ask_operator:
        result = await run_turn(
            "Do a full read-only quality audit of the custom Odoo codebase.",
            client, CLASSIFIER_MODEL,
            anticipated_scope={"planning_round_budget": 100, "round_wall_clock_cap_seconds": 0},
        )
        assert mock_ask_operator.called

    assert result["status"] == "paused"
    assert result["reason"] == "ask_operator"
    print(f"PASS: the wall-clock cap (0s) independently stops the loop even with a generous "
          f"round budget (100) -- ask_operator() was actually called")
    registry.clear()


def test_cross_module_repeated_failure_reproduced_from_phase13():
    """Test 7: the real cross-module repeated-failure case from Phase
    13's own report, reproduced directly -- two different synthetic
    modules failing for the same underlying root_cause classification.
    Confirm check_repeated_failures_across_modules() catches it AND
    confirm the plain per-module check_repeated_failures() genuinely
    would not have (assert both directly, not just the new function in
    isolation) -- this is the real gap Phase 13 found, not a
    hypothetical one.
    """
    sig = f"test.phase15.repro.{uuid.uuid4().hex[:8]}"
    module_a = f"module_a_{uuid.uuid4().hex[:8]}"
    module_b = f"module_b_{uuid.uuid4().hex[:8]}"

    append_project_memory(event_type="outcome", actor="testing_qa", task_id=str(uuid.uuid4()), module=module_a,
                           summary=f"{sig}: model reference mismatch", tags=["failed"], detail={"root_cause": "skill_gap"}, verified=True)
    append_project_memory(event_type="outcome", actor="testing_qa", task_id=str(uuid.uuid4()), module=module_b,
                           summary=f"{sig}: model reference mismatch", tags=["failed"], detail={"root_cause": "skill_gap"}, verified=True)

    per_module_a = check_repeated_failures(module_a)
    per_module_b = check_repeated_failures(module_b)
    across = check_repeated_failures_across_modules(sig)

    assert per_module_a["should_pause"] is False, "only 1 failure on module_a -- per-module check must NOT catch this"
    assert per_module_b["should_pause"] is False, "only 1 failure on module_b -- per-module check must NOT catch this"
    assert across["should_pause"] is True, "the cross-module check MUST catch this real, recurring pattern"
    print("PASS: reproduced Phase 13's real cross-module gap directly -- check_repeated_failures() "
          "genuinely misses it (asserted on both modules), check_repeated_failures_across_modules() "
          "genuinely catches it")


class AlwaysLockRejected:
    """Mimics BuildSpecialist's own real lock-rejection SpecialistOutput
    shape (specialists/build/specialist.py, Phase 26B follow-up) --
    never actually touches Redis/a real module directory, purely to
    drive manager/loop.py's own downstream handling of this shape in
    isolation.
    """
    async def run(self, contract: TaskContract) -> SpecialistOutput:
        return SpecialistOutput(
            task_id=contract.task_id, specialist_type=contract.specialist_type,
            summary=f"Model {contract.module_identity!r} is currently locked by another task -- refusing to proceed concurrently.",
            detail={"module_name": f"oma_never_actually_written_{contract.task_id}", "lock_rejected": True},
            claims_complete=False, artifacts=[],
        )


async def _run_test9_lock_rejection_is_handled_cleanly_with_backoff(client):
    """Phase 26B follow-up (2026-07-27) live verification: real, live-
    reproduced bug found the moment the lock-rejection path became
    genuinely reachable for the first time (module_name's own per-task
    hash suffix meant two tasks could never collide before this whole
    phase's fix) -- manager/loop.py's own should_run_code_review /
    verification_will_be_skipped checks only ever excluded a SANDBOX
    failure, never a lock rejection (which happens even earlier, before
    any file is ever written), so Code-Review got invoked to diff a
    module that was never scaffolded, producing a confusing "no files
    found" finding instead of the real, simple "another task is editing
    this model" reason. Confirms: (1) the round's own failure message is
    the CLEAN lock-rejection summary, never a files-not-found confusion;
    (2) root_cause is set directly to "one_off" (no wasted LLM
    classification call for an already-known, transient reason); (3) a
    short, bounded backoff actually happens between the rejected round
    and the next retry.
    """
    registry.clear()
    registry.register(SpecialistType.bug_fix, AlwaysLockRejected())

    goal = (
        "Add a field to a synthetic test model.\n\n"
        f"Module: mis_base_extend\nModel: oma_phase26b_lock_reject_test_{uuid.uuid4().hex[:8]}.synthetic\n"
        "Field: some_field (Char)\n"
    )
    sleep_calls = []
    real_sleep = asyncio.sleep

    async def _tracking_sleep(seconds):
        sleep_calls.append(seconds)
        # Don't actually wait 15s in a test -- confirm the call happened
        # with the right, deliberately-short duration instead.

    with patch("manager.loop.ask_operator"), patch("manager.loop.asyncio.sleep", _tracking_sleep):
        result = await run_turn(
            goal, client, CLASSIFIER_MODEL,
            anticipated_scope={"planning_round_budget": 2, "round_wall_clock_cap_seconds": 999999},
        )

    assert result["status"] == "paused" and result["reason"] == "ask_operator"
    assert len(result["prior_rounds"]) >= 1
    first_round = result["prior_rounds"][0]
    notes = first_round.verification_result.notes
    assert "locked by another task" in notes, (
        f"the round's own failure message must be the clean lock-rejection summary -- got {notes!r}"
    )
    assert "no files found" not in notes.lower() and "does not exist" not in notes.lower(), (
        f"must never surface a confusing files-not-found message for a lock rejection -- got {notes!r}"
    )
    assert first_round.verification_result.root_cause == "one_off", (
        f"root_cause must be set directly to 'one_off' for a lock rejection, no LLM classification "
        f"call needed -- got {first_round.verification_result.root_cause!r}"
    )
    # asyncio.sleep(0) is used elsewhere in this codebase as a plain
    # cooperative yield point, unrelated to this backoff -- only the
    # real, deliberate backoff calls (> 0) are this test's own concern.
    backoff_calls = [s for s in sleep_calls if s > 0]
    assert backoff_calls, "a real backoff must occur between a lock-rejected round and the next retry"
    assert all(s <= 30 for s in backoff_calls), (
        f"backoff must be short and bounded, never an unbounded/guessed wait -- got {backoff_calls!r}"
    )
    print(f"PASS: a lock-rejected round is handled cleanly -- the round's own message is the real "
          f"lock-rejection summary (never a confusing files-not-found finding), root_cause is set "
          f"directly to 'one_off', and a real, bounded backoff ({sleep_calls}) occurred before retrying")
    registry.clear()


async def _run_test8_escalation_writes_a_real_reachable_failed_outcome_row(client):
    """Phase 26B live verification (2026-07-27, audit Finding #2's own
    §26B.5 "escalation test"): real, independently-confirmed bug this
    closes -- separate from, and compounding, the module-identity
    mismatch itself. Before this fix, NO code path anywhere in
    manager/loop.py ever wrote a genuine `event_type="outcome"` row
    tagged "failed" on a real escalation (confirmed via exhaustive grep
    -- a failed round wrote `event_type="replan_round"`, a final
    escalation wrote `event_type="round_checkpoint"`; neither shape
    check_repeated_failures()'s own query has ever matched). This test
    runs a REAL run_turn() escalation (via a genuinely-unfixable fake
    specialist, ask_operator mocked so no real human notification fires)
    against a goal naming a real Model: line, TWICE, and confirms:
    (1) each escalation now genuinely writes a real, reachable "outcome"
    row tagged "failed", correctly keyed by the goal's own resolved
    module identity; (2) check_repeated_failures() now genuinely detects
    2+ of them on a THIRD, fresh submission targeting the same model --
    the actual end-to-end proof this mechanism is no longer structurally
    dead in production.
    """
    registry.clear()
    registry.register(SpecialistType.bug_fix, AlwaysFails())

    synthetic_model = f"oma_phase26b_escalation_test_{uuid.uuid4().hex[:8]}.synthetic"
    goal = (
        "Add a field to track something on this synthetic test model.\n\n"
        f"Module: mis_base_extend\nModel: {synthetic_model}\nField: some_field (Char)\n"
    )

    for attempt in range(2):
        with patch("manager.loop.ask_operator"):
            result = await run_turn(
                goal, client, CLASSIFIER_MODEL,
                anticipated_scope={"planning_round_budget": 2, "round_wall_clock_cap_seconds": 999999},
            )
        assert result["status"] == "paused" and result["reason"] == "ask_operator", (
            f"attempt {attempt + 1}: expected a real escalation, got {result!r}"
        )

    rows = read_project_memory(tags=None, limit=50)
    matching = [
        r for r in rows
        if r.event_type == "outcome" and r.module == synthetic_model and "failed" in (r.tags or [])
    ]
    assert len(matching) >= 2, (
        f"expected 2 real 'outcome'/'failed' rows written by the two real escalations above, "
        f"keyed by {synthetic_model!r} -- got {len(matching)}. Before this fix, this list was "
        f"ALWAYS empty -- no code path ever wrote this shape at all."
    )

    repeat_check = check_repeated_failures(synthetic_model)
    assert repeat_check["should_pause"] is True, (
        f"check_repeated_failures() must now genuinely detect the 2 real prior escalations for a "
        f"fresh submission on the same model -- got {repeat_check!r}"
    )
    print(f"PASS: 2 real run_turn() escalations each wrote a genuinely reachable 'outcome'/'failed' "
          f"row keyed by {synthetic_model!r}, and check_repeated_failures() now correctly detects "
          f"them -- this mechanism was structurally dead in production before this fix")
    registry.clear()


async def main():
    client = ModelGatewayClient()
    try:
        await _run_test1_measurably_different_second_round_succeeds(client)
        await _run_test3_round_budget_stops_the_loop(client)
        await _run_test3_wall_clock_cap_stops_the_loop(client)
        await _run_test2_select_specialist_for_retry_switches_on_recurring_finding(client)
        await _run_test2b_code_review_is_always_a_one_round_detour(client)
        await _run_test8_escalation_writes_a_real_reachable_failed_outcome_row(client)
        await _run_test9_lock_rejection_is_handled_cleanly_with_backoff(client)
    finally:
        await client.aclose()
    test_cross_module_repeated_failure_reproduced_from_phase13()
    print("\nALL REPLANNING TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
