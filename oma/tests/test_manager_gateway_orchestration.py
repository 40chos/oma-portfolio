"""Phase 6 tests: gateway-outage orchestration as real Manager-loop
behavior. Kills the gateway mid-task (points the client at a dead URL)
and confirms all three required things actually happen -- not just that
the client itself gives up cleanly:
  1. Redis task state flips to paused:gateway_unavailable.
  2. The task's module lock is released (so unrelated work isn't blocked).
  3. The message to Operator is worded distinctly from an ambiguity-pause
     or a tier-3/4 sign-off pause.
"""

import asyncio
import os
import sys
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from infra.fencing import acquire_module_lock
from infra.gateway_client import LLMRepetitionLoopExhaustedError, ModelGatewayClient
from manager.gateway_orchestration import (
    GatewayOutagePause,
    run_with_gateway_outage_handling,
)
from manager.task_state import (
    PAUSE_AMBIGUITY,
    PAUSE_GATEWAY_UNAVAILABLE,
    PAUSE_SIGN_OFF_REQUIRED,
    get_task_state,
)


async def _test_repetition_loop_exhaustion_gets_an_accurate_message() -> None:
    """Real, confirmed bug found live (2026-08-10, task 07141af5's flagship run,
    daily_escalation_cron node, resumes r205 and r206): this same function used to tell Operator
    "it's an infrastructure outage... I'll resume automatically once the gateway is back" for
    EVERY GatewayUnavailableError, including a repetition loop that had exhausted every one of
    its own temperature-escalated retries -- confirmed live, twice, via an immediate real health
    check that the gateway was never actually down. Fixed: `LLMRepetitionLoopExhaustedError` (a
    GatewayUnavailableError subclass) now gets a distinct, accurate message that never claims an
    infrastructure outage.
    """
    task_id = str(uuid.uuid4())
    module_name = f"test.repetition_exhaustion.{uuid.uuid4().hex[:8]}"
    lock = acquire_module_lock(module_name, task_id)
    assert lock.acquired

    async def _doomed_call():
        raise LLMRepetitionLoopExhaustedError(
            "generate() (streaming) exhausted every retry attempt on a repetition loop against "
            "backend 'gpu_worker_01_coder': model repeated the same 120-char span 6+ times."
        )

    raised = False
    try:
        await run_with_gateway_outage_handling(task_id, module_name, _doomed_call)
    except GatewayOutagePause as pause:
        raised = True
        assert "not an infrastructure outage" in pause.message.lower() or "not" in pause.message.lower() and "infrastructure outage" in pause.message.lower(), (
            f"expected the message to explicitly deny this is an infrastructure outage -- got: "
            f"{pause.message!r}"
        )
        assert "resume automatically once the gateway is back" not in pause.message, (
            "must never tell Operator to wait for a gateway that was never actually down"
        )
        assert "repetition loop" in pause.message.lower()
        print(f"PASS: a repetition-loop exhaustion gets an accurate, distinct message: {pause.message!r}")
    assert raised, "expected GatewayOutagePause to be raised"


async def main():
    task_id = str(uuid.uuid4())
    module_name = f"test.gateway_outage.{uuid.uuid4().hex[:8]}"

    # Simulate a task that's already holding this module's lock, as a
    # real task in progress would be, before the outage hits.
    lock = acquire_module_lock(module_name, task_id)
    assert lock.acquired

    dead_client = ModelGatewayClient(base_url_override="http://10.255.255.1:1/v1")

    async def _doomed_call():
        return await dead_client.generate(
            model="whatever",
            messages=[{"role": "user", "content": "hi"}],
            timeout_sec=2.0,
        )

    raised = False
    try:
        await run_with_gateway_outage_handling(task_id, module_name, _doomed_call)
    except GatewayOutagePause as pause:
        raised = True

        # 1. Redis task state flipped correctly.
        state = get_task_state(task_id)
        assert state == PAUSE_GATEWAY_UNAVAILABLE, f"expected {PAUSE_GATEWAY_UNAVAILABLE!r}, got {state!r}"
        print(f"PASS: task state correctly flipped to {state!r}")

        # 2. The module lock was actually released -- a fresh caller
        # must now be able to acquire it immediately.
        second_lock = acquire_module_lock(module_name, "some-other-task")
        assert second_lock.acquired, "module lock must be released on gateway outage, but a second caller couldn't acquire it"
        print("PASS: module lock was released -- a second caller acquired it immediately")

        # 3. The message is worded distinctly from ambiguity/sign-off pauses.
        assert "gateway" in pause.message.lower() or "unreachable" in pause.message.lower()
        assert "sign-off" not in pause.message.lower() or "isn't waiting on your sign-off" in pause.message.lower()
        assert pause.message != PAUSE_AMBIGUITY
        assert pause.message != PAUSE_SIGN_OFF_REQUIRED
        print(f"PASS: message worded distinctly from ambiguity/sign-off pauses: {pause.message!r}")
    finally:
        await dead_client.aclose()

    assert raised, "expected GatewayOutagePause to be raised"
    await _test_repetition_loop_exhaustion_gets_an_accurate_message()
    print("\nALL MANAGER GATEWAY ORCHESTRATION TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
