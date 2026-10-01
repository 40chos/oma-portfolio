"""Real, confirmed gap found live (2026-08-17, overnight `record_rule_row_level_security`
certification run): every GatewayOutagePause message promises "I'll resume automatically once
the gateway is back," but nothing in this codebase actually did that -- see
manager/gateway_auto_resume.py's own module docstring for the full incident. Real Redis, same
convention as tests/test_fencing.py -- no mocking of the storage layer itself, only of
resume_task_after_checkpoint() (a real round-loop call, genuinely expensive/network-bound).
"""

import json
import os
import sys
import time
import uuid
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import manager.gateway_auto_resume as gar
from infra.redis_client import get_redis_client
from manager.task_state import (
    PAUSE_AMBIGUITY,
    PAUSE_GATEWAY_UNAVAILABLE,
    PAUSE_TASK_CUT_OFF,
    STATE_RUNNING,
    clear_task_state,
    request_cancel,
    set_task_state,
)


def _cleanup(task_id: str, client) -> None:
    client.delete(gar._TRACKING_KEY_TEMPLATE.format(task_id=task_id))
    clear_task_state(task_id, client=client)
    client.delete(f"oma:task:{task_id}:cancel_requested")


def test_record_gateway_pause_schedules_a_fresh_entry():
    client = get_redis_client()
    task_id = f"test-gar-{uuid.uuid4()}"
    _cleanup(task_id, client)
    try:
        gar.record_gateway_pause(task_id, "some.module", client=client)
        raw = client.get(gar._TRACKING_KEY_TEMPLATE.format(task_id=task_id))
        assert raw is not None
        tracking = json.loads(raw)
        assert tracking["attempts"] == 0
        assert tracking["module_name"] == "some.module"
        assert tracking["next_retry_at"] > time.time()
        print("PASS: a fresh pause schedules a tracking entry at attempts=0")
    finally:
        _cleanup(task_id, client)


def test_record_gateway_pause_preserves_existing_attempt_count():
    """The real correctness requirement found during this fix's own review: a re-pause for the
    same reason must NOT reset the attempt counter, or the bounded-retry cap could never
    actually trigger (see record_gateway_pause()'s own docstring)."""
    client = get_redis_client()
    task_id = f"test-gar-{uuid.uuid4()}"
    _cleanup(task_id, client)
    try:
        client.set(
            gar._TRACKING_KEY_TEMPLATE.format(task_id=task_id),
            json.dumps({"attempts": 3, "next_retry_at": 0, "module_name": "x"}),
        )
        gar.record_gateway_pause(task_id, "x", client=client)
        tracking = json.loads(client.get(gar._TRACKING_KEY_TEMPLATE.format(task_id=task_id)))
        assert tracking["attempts"] == 3, "a re-pause must preserve, never reset, the existing attempt count"
        print("PASS: a re-pause preserves the existing attempt count instead of resetting it")
    finally:
        _cleanup(task_id, client)


def test_clear_gateway_auto_resume_tracking_removes_the_entry():
    client = get_redis_client()
    task_id = f"test-gar-{uuid.uuid4()}"
    _cleanup(task_id, client)
    try:
        gar.record_gateway_pause(task_id, "x", client=client)
        assert client.get(gar._TRACKING_KEY_TEMPLATE.format(task_id=task_id)) is not None
        gar.clear_gateway_auto_resume_tracking(task_id, client=client)
        assert client.get(gar._TRACKING_KEY_TEMPLATE.format(task_id=task_id)) is None
        print("PASS: clearing removes the tracking entry")
    finally:
        _cleanup(task_id, client)


@pytest.mark.asyncio
async def test_auto_resume_loop_also_resumes_a_task_cut_off_pause():
    """Real, confirmed gap found live (2026-08-17, immediately after the first version of this
    fix was deployed): the SAME LLMRepetitionLoopExhaustedError can ALSO surface via
    PAUSE_TASK_CUT_OFF (manager/compensations.py's TaskCutOffPause, after at least one real
    step already executed), not just PAUSE_GATEWAY_UNAVAILABLE. Confirms the poller treats both
    pause reasons identically."""
    client = get_redis_client()
    task_id = f"test-gar-{uuid.uuid4()}"
    _cleanup(task_id, client)
    try:
        set_task_state(task_id, PAUSE_TASK_CUT_OFF, client=client)
        client.set(
            gar._TRACKING_KEY_TEMPLATE.format(task_id=task_id),
            json.dumps({"attempts": 0, "next_retry_at": time.time() - 1, "module_name": "x"}),
        )

        async def _fake_resume(*args, **kwargs):
            set_task_state(task_id, STATE_RUNNING, client=client)
            return {"status": "completed"}

        with patch("manager.loop.resume_task_after_checkpoint", new=AsyncMock(side_effect=_fake_resume)):
            import asyncio

            loop_task = asyncio.create_task(gar.run_auto_resume_loop(
                client=object(), classifier_model="x", manager_model="y", poll_interval_sec=0.05,
            ))
            for _ in range(40):
                await asyncio.sleep(0.05)
                if client.get(gar._TRACKING_KEY_TEMPLATE.format(task_id=task_id)) is None:
                    break
            loop_task.cancel()
            try:
                await loop_task
            except asyncio.CancelledError:
                pass

        assert client.get(gar._TRACKING_KEY_TEMPLATE.format(task_id=task_id)) is None
        print("PASS: a PAUSE_TASK_CUT_OFF task is auto-resumed the same way as a gateway-unavailable one")
    finally:
        _cleanup(task_id, client)


def test_backoff_is_exponential_and_capped():
    assert gar._backoff_seconds(0) == gar._BASE_BACKOFF_SEC
    assert gar._backoff_seconds(1) == gar._BASE_BACKOFF_SEC * 2
    assert gar._backoff_seconds(2) == gar._BASE_BACKOFF_SEC * 4
    assert gar._backoff_seconds(20) == gar._MAX_BACKOFF_SEC, "must cap, never grow unbounded"
    print("PASS: backoff grows exponentially and is capped")


@pytest.mark.asyncio
async def test_auto_resume_loop_resumes_a_due_paused_task_and_clears_tracking():
    """The real, end-to-end happy path: a task genuinely due for retry, still paused for the
    tracked reason, gets a real resume_task_after_checkpoint() call, and -- once it's no longer
    paused for that reason -- its tracking entry is cleared."""
    client = get_redis_client()
    task_id = f"test-gar-{uuid.uuid4()}"
    _cleanup(task_id, client)
    try:
        set_task_state(task_id, PAUSE_GATEWAY_UNAVAILABLE, client=client)
        client.set(
            gar._TRACKING_KEY_TEMPLATE.format(task_id=task_id),
            json.dumps({"attempts": 0, "next_retry_at": time.time() - 1, "module_name": "x"}),
        )

        async def _fake_resume(*args, **kwargs):
            # Simulate a successful resume: the task is no longer gateway-paused afterward.
            set_task_state(task_id, STATE_RUNNING, client=client)
            return {"status": "completed"}

        with patch("manager.loop.resume_task_after_checkpoint", new=AsyncMock(side_effect=_fake_resume)):
            loop_task = None
            import asyncio

            loop_task = asyncio.create_task(gar.run_auto_resume_loop(
                client=object(), classifier_model="x", manager_model="y", poll_interval_sec=0.05,
            ))
            for _ in range(40):
                await asyncio.sleep(0.05)
                if client.get(gar._TRACKING_KEY_TEMPLATE.format(task_id=task_id)) is None:
                    break
            loop_task.cancel()
            try:
                await loop_task
            except asyncio.CancelledError:
                pass

        assert client.get(gar._TRACKING_KEY_TEMPLATE.format(task_id=task_id)) is None, (
            "tracking entry must be cleared once the task is no longer gateway-paused"
        )
        print("PASS: a due, still-paused task is resumed and its tracking entry is cleared on success")
    finally:
        _cleanup(task_id, client)


@pytest.mark.asyncio
async def test_auto_resume_loop_respects_cancel_requested():
    client = get_redis_client()
    task_id = f"test-gar-{uuid.uuid4()}"
    _cleanup(task_id, client)
    try:
        set_task_state(task_id, PAUSE_GATEWAY_UNAVAILABLE, client=client)
        client.set(
            gar._TRACKING_KEY_TEMPLATE.format(task_id=task_id),
            json.dumps({"attempts": 0, "next_retry_at": time.time() - 1, "module_name": "x"}),
        )
        request_cancel(task_id, client=client)

        called = False

        async def _fake_resume(*args, **kwargs):
            nonlocal called
            called = True
            return {"status": "completed"}

        with patch("manager.loop.resume_task_after_checkpoint", new=AsyncMock(side_effect=_fake_resume)):
            import asyncio

            loop_task = asyncio.create_task(gar.run_auto_resume_loop(
                client=object(), classifier_model="x", manager_model="y", poll_interval_sec=0.05,
            ))
            await asyncio.sleep(0.3)
            loop_task.cancel()
            try:
                await loop_task
            except asyncio.CancelledError:
                pass

        assert not called, "a cancelled task must never be auto-resumed"
        assert client.get(gar._TRACKING_KEY_TEMPLATE.format(task_id=task_id)) is None
        print("PASS: a task with a pending cancel request is never auto-resumed")
    finally:
        _cleanup(task_id, client)


@pytest.mark.asyncio
async def test_auto_resume_loop_gives_up_after_max_attempts():
    client = get_redis_client()
    task_id = f"test-gar-{uuid.uuid4()}"
    _cleanup(task_id, client)
    try:
        set_task_state(task_id, PAUSE_GATEWAY_UNAVAILABLE, client=client)
        client.set(
            gar._TRACKING_KEY_TEMPLATE.format(task_id=task_id),
            json.dumps({
                "attempts": gar._MAX_AUTO_RESUME_ATTEMPTS, "next_retry_at": time.time() - 1,
                "module_name": "x",
            }),
        )

        called = False

        async def _fake_resume(*args, **kwargs):
            nonlocal called
            called = True
            return {"status": "completed"}

        with patch("manager.loop.resume_task_after_checkpoint", new=AsyncMock(side_effect=_fake_resume)):
            import asyncio

            loop_task = asyncio.create_task(gar.run_auto_resume_loop(
                client=object(), classifier_model="x", manager_model="y", poll_interval_sec=0.05,
            ))
            await asyncio.sleep(0.3)
            loop_task.cancel()
            try:
                await loop_task
            except asyncio.CancelledError:
                pass

        assert not called, "must never attempt again once the cap is reached"
        assert client.get(gar._TRACKING_KEY_TEMPLATE.format(task_id=task_id)) is None, (
            "the tracking entry must be cleared once given up on, leaving the task genuinely "
            "paused for a human -- matching every other escalation-cap convention"
        )
        print("PASS: automatic resume gives up after the attempt cap, leaving the task for a human")
    finally:
        _cleanup(task_id, client)


@pytest.mark.asyncio
async def test_auto_resume_loop_ignores_tasks_paused_for_a_different_reason():
    client = get_redis_client()
    task_id = f"test-gar-{uuid.uuid4()}"
    _cleanup(task_id, client)
    try:
        set_task_state(task_id, PAUSE_AMBIGUITY, client=client)
        client.set(
            gar._TRACKING_KEY_TEMPLATE.format(task_id=task_id),
            json.dumps({"attempts": 0, "next_retry_at": time.time() - 1, "module_name": "x"}),
        )

        called = False

        async def _fake_resume(*args, **kwargs):
            nonlocal called
            called = True
            return {"status": "completed"}

        with patch("manager.loop.resume_task_after_checkpoint", new=AsyncMock(side_effect=_fake_resume)):
            import asyncio

            loop_task = asyncio.create_task(gar.run_auto_resume_loop(
                client=object(), classifier_model="x", manager_model="y", poll_interval_sec=0.05,
            ))
            await asyncio.sleep(0.3)
            loop_task.cancel()
            try:
                await loop_task
            except asyncio.CancelledError:
                pass

        assert not called, "a task now paused for a DIFFERENT reason must never be auto-resumed"
        assert client.get(gar._TRACKING_KEY_TEMPLATE.format(task_id=task_id)) is None, (
            "the stale tracking entry (from a prior gateway pause) must be cleaned up"
        )
        print("PASS: a task that moved to a different pause reason is left alone and cleaned up")
    finally:
        _cleanup(task_id, client)
