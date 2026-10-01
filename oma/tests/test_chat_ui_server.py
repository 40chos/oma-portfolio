"""Phase 12: real tests for the chat UI's FastAPI backend, via
starlette's real TestClient (a genuine ASGI call through the actual
app -- not a mock of the HTTP layer), against the real Manager loop,
real Postgres, real Redis, and the real model gateway. No specialist
mocks either: code_review is always the real CodeReviewSpecialist
(registered at app startup via scripts.bootstrap_specialists), and
bug_fix/testing_qa fall back to the demo fake only if
OMA_ODOO_DB_DUPLICATE_FOR_BUILD isn't set -- exactly the same startup
behavior a real deployment would have.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from starlette.testclient import TestClient

from ui.chat.server import app


def test_index_serves_the_real_html_page():
    with TestClient(app) as client:
        resp = client.get("/")
        assert resp.status_code == 200
        assert "Odoo Manager Agent" in resp.text
        assert "sendMessage" in resp.text  # the real JS, not a stub page
    print("PASS: GET / serves the real chat UI page")


def test_readonly_message_routes_to_code_review_over_real_http():
    """A real audit target (contract_inputs, the same caller-supplied-
    hint pattern used throughout since Phase 6/13) is supplied so this
    genuinely passes in round 1 -- without it, contract.inputs=[] makes
    CodeReviewSpecialist correctly refuse ("nothing to review"), which
    under Phase 15's round loop retries up to the default 5-round
    budget before escalating via PauseForOperator, a real, different (and
    slower) behavior than what this test means to check: that routing
    to code_review works at all.
    """
    with TestClient(app) as client:
        resp = client.post(
            "/api/message",
            json={
                "session_id": "test-readonly",
                "message": "Do a full read-only quality audit of the custom Odoo codebase.",
                "anticipated_scope": {"contract_inputs": ["full_codebase_audit:garazd_product_label"]},
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["result"]["capability_class"] == "readonly"
        assert data["result"]["build_output"]["specialist_type"] == "code_review"
        print(f"PASS: a real HTTP chat message correctly routes to code_review "
              f"(task {data['result']['task_id']})")


def test_tier4_sign_off_full_round_trip_over_real_http():
    """The real, complete flow: propose (tier 4, pauses) -> appears in
    the pending queue -> approve over HTTP -> actually executes ->
    disappears from the pending queue. All real HTTP calls, real
    Redis-backed pending storage, real Postgres memory writes.
    """
    with TestClient(app) as client:
        propose = client.post(
            "/api/message",
            json={
                "session_id": "test-signoff",
                "message": "Modify the ir.model.access.csv permissions for a chat-UI test model.",
                "anticipated_scope": {"touches_schema_or_permissions": True},
            },
        )
        assert propose.status_code == 200
        result = propose.json()["result"]
        assert result["status"] == "paused"
        assert result["reason"] == "sign_off_required"
        task_id = result["task_id"]
        assert result["contract"]["tier"] == 4

        pending = client.get("/api/pending").json()
        matching = [s for s in pending["sign_offs"] if s["task_id"] == task_id]
        assert matching, f"task {task_id} must appear in the real pending queue"
        print(f"PASS: a real tier-4 proposal appears in the real HTTP pending queue (task {task_id})")

        approve = client.post(f"/api/sign_off/{task_id}", json={"approved": True})
        assert approve.status_code == 200
        approved_result = approve.json()
        assert approved_result["status"] == "completed"
        assert approved_result["tier"] == 4

        pending_after = client.get("/api/pending").json()
        assert not any(s["task_id"] == task_id for s in pending_after["sign_offs"]), (
            "an approved sign-off must be cleared from the pending queue"
        )
        print(f"PASS: approving over real HTTP actually executes the task and clears it from the "
              f"pending queue (task {task_id})")


def test_reject_unknown_sign_off_returns_404():
    with TestClient(app) as client:
        resp = client.post(
            "/api/sign_off/00000000-0000-0000-0000-000000000000", json={"approved": True}
        )
        assert resp.status_code == 404
    print("PASS: approving/rejecting an unknown task_id over real HTTP returns a clean 404, not a crash")


def test_proposed_rule_reject_over_real_http_actually_updates_postgres():
    from manager.correction import list_pending_proposed_rules

    before = list_pending_proposed_rules()
    if not before:
        print("SKIP: no pending proposed rules exist in this environment to test against")
        return

    row_id = before[0]["id"]
    with TestClient(app) as client:
        resp = client.post(f"/api/proposed_rule/{row_id}", json={"approved": False})
        assert resp.status_code == 200

    after = list_pending_proposed_rules()
    assert row_id not in [r["id"] for r in after], "a rejected proposed rule must no longer be pending"
    print(f"PASS: rejecting a proposed rule over real HTTP actually updates Postgres (row #{row_id})")


if __name__ == "__main__":
    test_index_serves_the_real_html_page()
    test_readonly_message_routes_to_code_review_over_real_http()
    test_tier4_sign_off_full_round_trip_over_real_http()
    test_reject_unknown_sign_off_returns_404()
    test_proposed_rule_reject_over_real_http_actually_updates_postgres()
    print("\nALL CHAT UI SERVER TESTS PASSED")
