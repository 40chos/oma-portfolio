"""Phase 20 Area 2 (2026-07-20, UPDATE 20): unit tests for
ui/chat/server.py's _task_has_recent_progress() -- pure logic against
a fake Redis client, no live Redis/DB needed.

Real bug this fixes: task #50 was spuriously marked "orphaned by an
external service restart" by the startup reconciliation sweep while
its own round was still genuinely producing real trace events (a real
replan_round event landed ~2 minutes AFTER the false cancellation).
The exact trigger was never root-caused, so this is a defensive guard
against ANY trigger of this shape: never write an orphan/cancel
decision for a task with recent, real evidence of life.
"""

import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from ui.chat.server import _RECENT_PROGRESS_THRESHOLD_SECONDS, _task_has_recent_progress


class _FakeRedisClient:
    def __init__(self, lindex_return=None, raise_exc=None):
        self._lindex_return = lindex_return
        self._raise_exc = raise_exc

    def lindex(self, key, index):
        if self._raise_exc:
            raise self._raise_exc
        return self._lindex_return


def test_true_for_a_very_recent_event():
    client = _FakeRedisClient(lindex_return=json.dumps({"ts": time.time() - 5}))
    assert _task_has_recent_progress("t1", client) is True
    print("PASS: a trace event from 5 seconds ago counts as recent progress")


def test_false_for_an_old_event():
    client = _FakeRedisClient(lindex_return=json.dumps({"ts": time.time() - (_RECENT_PROGRESS_THRESHOLD_SECONDS + 30)}))
    assert _task_has_recent_progress("t1", client) is False
    print("PASS: a trace event older than the threshold does not count as recent progress")


def test_false_when_no_history_at_all():
    client = _FakeRedisClient(lindex_return=None)
    assert _task_has_recent_progress("t1", client) is False
    print("PASS: a task with no trace history at all falls through to the existing reconcile behavior")


def test_false_on_unparsable_json():
    client = _FakeRedisClient(lindex_return="not json")
    assert _task_has_recent_progress("t1", client) is False
    print("PASS: an unparsable trace entry is treated conservatively as no recent progress")


def test_false_when_ts_field_missing():
    client = _FakeRedisClient(lindex_return=json.dumps({"message": "no ts here"}))
    assert _task_has_recent_progress("t1", client) is False
    print("PASS: an entry missing its own 'ts' field is treated conservatively as no recent progress")


def test_false_when_redis_raises():
    client = _FakeRedisClient(raise_exc=ConnectionError("redis down"))
    assert _task_has_recent_progress("t1", client) is False
    print("PASS: a Redis error never crashes the guard -- falls through to existing reconcile behavior")


if __name__ == "__main__":
    test_true_for_a_very_recent_event()
    test_false_for_an_old_event()
    test_false_when_no_history_at_all()
    test_false_on_unparsable_json()
    test_false_when_ts_field_missing()
    test_false_when_redis_raises()
    print("\nALL RECONCILIATION RECENT-PROGRESS GUARD TESTS PASSED")
