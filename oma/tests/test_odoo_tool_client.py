"""Phase 7 (rebuilt): OdooToolClient, tested for real against the
duplicate Odoo database. Covers the same required behaviors as before
(the __last_update concurrency test) PLUS the new allowlist layer that
replaces mcp-server-odoo's dropped whitelist feature: a call for a
model/operation not on odoo_tool_allowlist.yaml must be refused before
it ever reaches Odoo.
"""

import os
import sys
import uuid
import xmlrpc.client

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from infra.odoo_jit_apikey import create_task_api_key, revoke_task_api_key
from tools_odoo.odoo_tool_client import (
    AllowlistViolationError,
    ConcurrencyConflictError,
    OdooToolClient,
    load_allowlist,
)

DUPLICATE_DB = "odoo16_dev_dup_20260707"
DEDICATED_USER = "oma_agent"
DEDICATED_UID = 450
ODOO_URL = "http://127.0.0.1:18071"


def _make_client_and_key(task_suffix: str):
    row_id, api_key = create_task_api_key(DUPLICATE_DB, DEDICATED_USER, f"phase7-toolclient-{task_suffix}")
    client = OdooToolClient(uid=DEDICATED_UID, api_key=api_key, db_override=DUPLICATE_DB)
    return client, row_id, api_key


def _pick_a_real_partner_id(client: OdooToolClient) -> int:
    ids = client._execute_kw("res.partner", "search", [[("active", "=", True)]], {"limit": 1})
    assert ids, "expected at least one active res.partner to exist in the duplicate"
    return ids[0]


def test_allowlist_loads_and_contains_res_partner():
    rules = load_allowlist()
    assert any(r.get("model") == "res.partner" for r in rules)
    print(f"PASS: odoo_tool_allowlist.yaml loads {len(rules)} real rule(s), including res.partner")


def test_search_read_and_read_always_include_last_update():
    client, row_id, _ = _make_client_and_key("read")
    try:
        partner_id = _pick_a_real_partner_id(client)
        record = client.read("res.partner", partner_id, ["name"])
        assert "__last_update" in record
        assert "name" in record
        print(f"PASS: read() on res.partner #{partner_id} always includes __last_update "
              f"(value: {record['__last_update']!r})")

        rows = client.search_read("res.partner", [("id", "=", partner_id)], ["name"], limit=1)
        assert len(rows) == 1
        print("PASS: search_read() (allowlisted operation) works correctly")
    finally:
        revoke_task_api_key(DUPLICATE_DB, row_id)


def test_write_with_correct_last_update_succeeds():
    client, row_id, _ = _make_client_and_key("write-ok")
    try:
        partner_id = _pick_a_real_partner_id(client)
        record = client.read("res.partner", partner_id, ["comment"])
        marker = f"oma-test-{uuid.uuid4().hex[:8]}"
        client.write("res.partner", partner_id, {"comment": marker}, record["__last_update"])

        reread = client.read("res.partner", partner_id, ["comment"])
        # `comment` is an HTML field -- Odoo's sanitizer wraps plain
        # text in <span>...</span>, so check containment, not equality.
        assert marker in reread["comment"]
        print(f"PASS: write() with the correct, freshly-read __last_update succeeds "
              f"and the write is confirmed present (partner #{partner_id})")
    finally:
        revoke_task_api_key(DUPLICATE_DB, row_id)


def test_concurrency_conflict_is_rejected_not_silently_overwritten():
    """THE required test: a second, independent client modifies the
    record between our read and our write. Our write must be rejected.
    """
    client, row_id, api_key = _make_client_and_key("concurrency")
    try:
        partner_id = _pick_a_real_partner_id(client)

        our_read = client.read("res.partner", partner_id, ["comment"])
        stale_last_update = our_read["__last_update"]
        original_comment = our_read["comment"]

        # A SECOND, independent client (its own raw XML-RPC connection,
        # simulating a human editing the record in the Odoo web UI at
        # that same moment) modifies the record.
        second_client_models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object")
        concurrent_value = f"concurrent-edit-{uuid.uuid4().hex[:8]}"
        second_client_models.execute_kw(
            DUPLICATE_DB, DEDICATED_UID, api_key,
            "res.partner", "write", [[partner_id], {"comment": concurrent_value}],
        )

        our_value = f"our-write-{uuid.uuid4().hex[:8]}"
        raised = False
        try:
            client.write("res.partner", partner_id, {"comment": our_value}, stale_last_update)
        except ConcurrencyConflictError:
            raised = True

        assert raised, (
            "our write used a stale __last_update after a concurrent modification -- "
            "it MUST be rejected, but no ConcurrencyConflictError was raised"
        )
        print("PASS: our write with a stale __last_update was correctly rejected "
              "(ConcurrencyConflictError raised)")

        final = client.read("res.partner", partner_id, ["comment"])
        assert concurrent_value in final["comment"], (
            f"expected the record to still hold the concurrent edit ({concurrent_value!r}), "
            f"got {final['comment']!r} -- our rejected write must not have silently applied anyway"
        )
        print(f"PASS: the record correctly still holds the concurrent client's value "
              f"({concurrent_value!r}), confirming our rejected write never actually applied")

        # Cleanup: restore the original value.
        fresh = client.read("res.partner", partner_id, ["comment"])
        client.write("res.partner", partner_id, {"comment": original_comment}, fresh["__last_update"])
    finally:
        revoke_task_api_key(DUPLICATE_DB, row_id)


def test_allowlist_rejects_unlisted_operation_before_reaching_odoo():
    """res.partner is allowlisted for read/search_read/write only --
    NOT create or unlink. A call for an unlisted operation on an
    otherwise-allowlisted model must be refused.
    """
    client, row_id, _ = _make_client_and_key("allowlist-op")
    try:
        raised = False
        try:
            client.unlink("res.partner", 999999)
        except AllowlistViolationError:
            raised = True
        assert raised, "unlink on res.partner is NOT allowlisted and must be refused"
        print("PASS: unlink on res.partner (an unlisted operation) is correctly refused "
              "before ever reaching Odoo")

        raised_create = False
        try:
            client.create("res.partner", {"name": "should never be created"})
        except AllowlistViolationError:
            raised_create = True
        assert raised_create, "create on res.partner is NOT allowlisted and must be refused"
        print("PASS: create on res.partner (an unlisted operation) is correctly refused")
    finally:
        revoke_task_api_key(DUPLICATE_DB, row_id)


def test_allowlist_rejects_unlisted_model_entirely():
    """A model not mentioned in odoo_tool_allowlist.yaml AT ALL must be
    refused, regardless of operation.
    """
    client, row_id, _ = _make_client_and_key("allowlist-model")
    try:
        raised = False
        try:
            client.read("res.users", 1, ["login"])
        except AllowlistViolationError:
            raised = True
        assert raised, "res.users is not on the allowlist at all and must be refused"
        print("PASS: a model not on the allowlist at all (res.users) is correctly refused, "
              "regardless of operation")
    finally:
        revoke_task_api_key(DUPLICATE_DB, row_id)


if __name__ == "__main__":
    test_allowlist_loads_and_contains_res_partner()
    test_search_read_and_read_always_include_last_update()
    test_write_with_correct_last_update_succeeds()
    test_concurrency_conflict_is_rejected_not_silently_overwritten()
    test_allowlist_rejects_unlisted_operation_before_reaching_odoo()
    test_allowlist_rejects_unlisted_model_entirely()
    print("\nALL ODOO TOOL CLIENT TESTS PASSED")
