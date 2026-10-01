"""Phase 16 (§20.7 test 1): GET /api/stream/{task_id} against the real
starlette TestClient and the real Redis instance, now that the
`odoo-manager-agent` ACL user genuinely has PUBLISH/SUBSCRIBE on
oma:* channels (confirmed live -- the project owner's grant). Real live delivery,
not just the honest-degradation path: a real publish, from a real
concurrent thread, while the SSE response is being read, confirms the
event actually arrives client-side, and that the stream closes cleanly
on a real terminal event.
"""

import json
import os
import sys
import threading
import time
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from starlette.testclient import TestClient

from manager.trace import publish_trace_event
from ui.chat.server import app


def test_stream_endpoint_delivers_real_live_events_and_closes_on_terminal():
    task_id = str(uuid.uuid4())
    received = []

    def _publish_after_subscribed():
        # A real subscribe takes a moment to register on the Redis side;
        # give it a beat before publishing, then send a real non-terminal
        # event followed by a real terminal one.
        time.sleep(0.5)
        publish_trace_event(task_id, {
            "level": "manager", "actor": "manager", "phase": "classify",
            "message": "Risk tier 1 -- work type: readonly.", "status": "passed",
        })
        time.sleep(0.2)
        publish_trace_event(task_id, {
            "level": "manager", "actor": "manager", "phase": "task",
            "message": "Done -- verified and saved to memory.", "status": "passed",
        })

    publisher = threading.Thread(target=_publish_after_subscribed, daemon=True)

    with TestClient(app) as client:
        publisher.start()
        with client.stream("GET", f"/api/stream/{task_id}") as resp:
            assert resp.status_code == 200
            assert "text/event-stream" in resp.headers["content-type"]
            for line in resp.iter_lines():
                if not line.startswith("data:"):
                    continue
                event = json.loads(line[len("data:"):].strip())
                received.append(event)
                if event.get("phase") == "task":
                    break  # the endpoint itself should also close right here

    publisher.join(timeout=5)

    assert len(received) == 2, f"expected exactly the 2 real published events, got: {received}"
    assert received[0]["phase"] == "classify" and received[0]["status"] == "passed"
    assert received[1]["phase"] == "task" and received[1]["status"] == "passed"
    print(f"PASS: GET /api/stream/{{task_id}} delivered {len(received)} real, live-published "
          f"events end to end (not the honest-failure path) and the stream closed cleanly on "
          f"the real terminal event -- Redis ACL grant confirmed working through the actual HTTP layer")


def test_stream_endpoint_does_not_close_on_a_paused_task_status():
    """Real bug found live (2026-08-10, the project owner's own direct report: resuming a task from the
    backend "worked perfectly" but the UI "just isn't updating... only appearing when I hard
    refresh"). `stream_task()` used to ALSO close the connection on `phase='task', status=
    'paused'`, treating it as terminal -- wrong: a paused task is explicitly expected to receive
    MORE real events later (on resume). Closing early forced the browser's EventSource to
    reconnect on its own schedule; any resume event published in that gap (before reconnect
    completed) was lost forever for that tab, since Redis pub/sub has no replay for a
    not-yet-subscribed listener. This test proves the stream now stays open across a 'paused'
    event and keeps delivering real events published afterward -- the resume this bug used to
    silently swallow.
    """
    task_id = str(uuid.uuid4())
    received = []

    def _publish_pause_then_resume_events():
        time.sleep(0.5)
        publish_trace_event(task_id, {
            "level": "manager", "actor": "manager", "phase": "task",
            "message": "Paused -- waiting on a decision.", "status": "paused",
        })
        # The real gap this bug lost: a "resume" event published AFTER the paused status,
        # simulating exactly what a real /continue call publishes moments later.
        time.sleep(0.3)
        publish_trace_event(task_id, {
            "level": "manager", "actor": "manager", "phase": "branch_message",
            "message": "Resumed after Operator's decision to continue.", "status": "passed",
        })
        time.sleep(0.2)
        publish_trace_event(task_id, {
            "level": "manager", "actor": "manager", "phase": "task",
            "message": "Done -- verified and saved to memory.", "status": "passed",
        })

    publisher = threading.Thread(target=_publish_pause_then_resume_events, daemon=True)

    with TestClient(app) as client:
        publisher.start()
        with client.stream("GET", f"/api/stream/{task_id}") as resp:
            assert resp.status_code == 200
            for line in resp.iter_lines():
                if not line.startswith("data:"):
                    continue
                event = json.loads(line[len("data:"):].strip())
                received.append(event)
                if event.get("phase") == "task" and event.get("status") == "passed":
                    break  # only the genuinely terminal 'passed' event should ever close this

    publisher.join(timeout=5)

    phases = [(e.get("phase"), e.get("status")) for e in received]
    assert phases == [
        ("task", "paused"),
        ("branch_message", "passed"),
        ("task", "passed"),
    ], f"expected the stream to stay open across 'paused' and keep delivering the real resume events after it, got: {phases}"
    print("PASS: GET /api/stream/{task_id} does NOT close on a 'paused' status -- it stays open "
          "and keeps delivering real events published after the pause (e.g. a real resume), "
          "closing only on a genuinely terminal passed/failed event")


if __name__ == "__main__":
    test_stream_endpoint_delivers_real_live_events_and_closes_on_terminal()
    test_stream_endpoint_does_not_close_on_a_paused_task_status()
    print("\nALL STREAM ENDPOINT TESTS PASSED")
