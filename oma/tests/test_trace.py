"""Phase 16 (§20.7 test 3): confirm a Pub/Sub publish failure never
raises out of the caller, AND (now that the project owner's granted the real
PUBLISH/SUBSCRIBE ACL on oma:* channels) confirm a real publish
genuinely succeeds and is actually deliverable. Real Redis throughout
-- no mocks.
"""

import os
import sys
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import redis

from infra.redis_client import get_redis_client
from manager.trace import channel_name, publish_trace_event


def test_publish_trace_event_genuinely_delivers_now_that_acl_is_granted():
    """Real, live evidence: the ACL grant is genuinely in effect --
    a real publish_trace_event() call is actually received by a real
    subscriber, not just "didn't raise."
    """
    task_id = str(uuid.uuid4())
    r = get_redis_client()
    pubsub = r.pubsub()
    pubsub.subscribe(channel_name(task_id))
    pubsub.get_message(timeout=2)  # the subscribe confirmation itself

    publish_trace_event(task_id, {"level": "manager", "actor": "manager", "phase": "test", "message": "x", "status": "running"}, client=r)

    msg = pubsub.get_message(timeout=3)
    assert msg is not None and msg["type"] == "message", f"expected a real delivered message, got: {msg}"
    assert '"message": "x"' in msg["data"]
    print("PASS: publish_trace_event() genuinely delivers a real message to a real subscriber -- ACL grant confirmed working")


def test_publish_trace_event_never_raises_on_dead_connection():
    """The §20.7-specified case: a genuinely unreachable Redis host."""
    dead_client = redis.Redis(host="10.255.255.1", port=6379, socket_connect_timeout=1, socket_timeout=1)
    publish_trace_event(str(uuid.uuid4()), {"level": "manager", "message": "x"}, client=dead_client)
    print("PASS: publish_trace_event() did not raise against a genuinely unreachable Redis host")


def test_channel_name_format():
    assert channel_name("abc-123") == "oma:trace:abc-123"
    print("PASS: channel_name() produces the real oma:trace:{task_id} convention")


if __name__ == "__main__":
    test_publish_trace_event_genuinely_delivers_now_that_acl_is_granted()
    test_publish_trace_event_never_raises_on_dead_connection()
    test_channel_name_format()
    print("\nALL TRACE TESTS PASSED")
