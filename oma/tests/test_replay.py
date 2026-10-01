"""Phase 16: real replay test -- run a real task, replay it via the
real HTTP endpoint, confirm a brand-new task_id runs with the SAME
real goal text, and the original task's own history is untouched.
"""

import os
import sys
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from starlette.testclient import TestClient

from manager.dashboard import get_original_task
from ui.chat.server import app


def test_replay_resubmits_as_a_brand_new_task():
    with TestClient(app) as client:
        marker = uuid.uuid4().hex[:8]
        message = f"Do a full read-only quality audit of the custom Odoo codebase. marker={marker}"
        original = client.post(
            "/api/message",
            json={
                "session_id": "test-replay",
                "message": message,
                "anticipated_scope": {"contract_inputs": ["full_codebase_audit:garazd_product_label"]},
            },
        ).json()["result"]
        assert original["status"] == "completed"
        original_task_id = original["task_id"]

        original_stored = get_original_task(original_task_id)
        assert original_stored["goal"] == message
        assert original_stored["inputs"] == ["full_codebase_audit:garazd_product_label"], (
            "contract.inputs must be stored durably too, not just the goal -- a replay without "
            "them would lose whatever made the original task actually runnable"
        )

        replayed = client.post(f"/api/tasks/{original_task_id}/replay").json()
        assert replayed["replayed_from"] == original_task_id
        new_result = replayed["result"]
        assert new_result["status"] == "completed", f"replay must carry forward real inputs too, not just goal text: {new_result}"
        assert new_result["task_id"] != original_task_id, "replay must mint a brand-new task_id, never resume the old one"

        # The original task's own real goal record must be untouched --
        # replay never mutates or supersedes it.
        assert get_original_task(original_task_id)["goal"] == message
        assert get_original_task(new_result["task_id"])["goal"] == message
        print(f"PASS: replay resubmitted the real original goal as a brand-new task "
              f"({original_task_id} -> {new_result['task_id']}), both with matching real goal text, "
              f"original task's own record untouched")

        unknown = client.post(f"/api/tasks/{uuid.uuid4()}/replay")
        assert unknown.status_code == 404
        print("PASS: replaying an unknown task_id returns a clean 404, not a crash")


if __name__ == "__main__":
    test_replay_resubmits_as_a_brand_new_task()
    print("\nALL REPLAY TESTS PASSED")
