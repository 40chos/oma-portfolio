"""Phase 35 §17.3.2: tests for manager/loop.py's _log_concurrent_claim_admission() -- the
decomposition-time claim admission wiring. Mocks acquire_task_claim/release_task_claim directly,
zero real Redis calls (the underlying claim primitives themselves are already covered by real
Redis integration tests in tests/test_concurrent_claim.py).
"""

import asyncio
import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import manager.loop as loop_module
from manager.concurrent_claim import ClaimResult
from manager.graph_governance_flags import GateMode


def _run(touched_models, task_id="t1"):
    return asyncio.run(loop_module._log_concurrent_claim_admission(touched_models, task_id))


def test_empty_touched_models_is_a_silent_noop():
    with patch("manager.concurrent_claim.acquire_task_claim") as mock_acquire:
        _run([])
    mock_acquire.assert_not_called()


def test_disabled_gate_never_calls_redis():
    with patch("manager.loop.get_gate_mode", return_value=GateMode.DISABLED), \
         patch("manager.concurrent_claim.acquire_task_claim") as mock_acquire:
        _run(["crm.lead"])
    mock_acquire.assert_not_called()


def test_clean_acquisition_is_silent_and_releases_immediately():
    result = ClaimResult(acquired=True, task_id="t1", claimed_targets=["crm.lead"], colliding_target=None, colliding_task_id=None)
    with patch("manager.loop.get_gate_mode", return_value=GateMode.LOG_ONLY), \
         patch("manager.concurrent_claim.acquire_task_claim", return_value=result), \
         patch("manager.concurrent_claim.release_task_claim") as mock_release, \
         patch.object(loop_module, "publish_trace_event") as mock_publish:
        _run(["crm.lead"])
    mock_release.assert_called_once()
    mock_publish.assert_not_called()


def test_real_collision_is_logged_but_never_blocks():
    result = ClaimResult(
        acquired=False, task_id="t1", claimed_targets=[],
        colliding_target="crm.lead", colliding_task_id="other-task-99",
    )
    with patch("manager.loop.get_gate_mode", return_value=GateMode.LOG_ONLY), \
         patch("manager.concurrent_claim.acquire_task_claim", return_value=result), \
         patch("manager.concurrent_claim.release_task_claim") as mock_release, \
         patch.object(loop_module, "publish_trace_event") as mock_publish:
        _run(["crm.lead"])
    # No claim was actually held (acquisition failed), so nothing to release.
    mock_release.assert_not_called()
    assert mock_publish.called
    event = mock_publish.call_args[0][1]
    assert "other-task-99" in event["message"]
    assert "log_only" in event["message"]


def test_redis_failure_fails_open_and_never_raises():
    with patch("manager.loop.get_gate_mode", return_value=GateMode.LOG_ONLY), \
         patch("manager.concurrent_claim.acquire_task_claim", side_effect=RuntimeError("redis down")), \
         patch.object(loop_module, "publish_trace_event") as mock_publish:
        _run(["crm.lead"])  # must not raise
    assert mock_publish.called
    event = mock_publish.call_args[0][1]
    assert "failed open" in event["message"]
