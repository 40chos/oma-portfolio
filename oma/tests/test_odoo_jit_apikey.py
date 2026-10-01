"""Phase 7 step 4: the Odoo-side JIT API-key wrapper, tested for real
against the duplicate Odoo database -- not just "the function ran
without an exception," but proven end to end: a freshly-created key
actually authenticates over XML-RPC, and the SAME key genuinely stops
working immediately after revoke_task_api_key() deletes its record.
"""

import os
import sys
import uuid
import xmlrpc.client

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from infra.odoo_jit_apikey import create_task_api_key, revoke_task_api_key

DUPLICATE_DB = "odoo16_dev_dup_20260707"
DEDICATED_USER = "oma_agent"
ODOO_URL = "http://127.0.0.1:18071"


def _authenticate_with_key(api_key: str) -> int | bool:
    common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common")
    return common.authenticate(DUPLICATE_DB, DEDICATED_USER, api_key, {})


def test_jit_apikey_lifecycle():
    task_id = f"phase7-test-{uuid.uuid4().hex[:8]}"
    row_id, plaintext_key = create_task_api_key(DUPLICATE_DB, DEDICATED_USER, task_id)
    assert row_id > 0
    assert len(plaintext_key) > 0
    print(f"PASS: created a fresh API key (row #{row_id}) for task {task_id!r}")

    uid = _authenticate_with_key(plaintext_key)
    assert uid and uid is not False, "the freshly-created key must actually authenticate"
    print(f"PASS: the fresh key genuinely authenticates over XML-RPC (uid={uid})")

    revoke_task_api_key(DUPLICATE_DB, row_id)
    print(f"PASS: revoked key row #{row_id}")

    uid_after_revoke = _authenticate_with_key(plaintext_key)
    assert uid_after_revoke is False, (
        "the SAME key must no longer authenticate after revoke_task_api_key() -- "
        f"got uid={uid_after_revoke!r} instead of False"
    )
    print("PASS: the exact same key correctly stops authenticating immediately after revocation "
          "-- no permanent key was ever left behind")


if __name__ == "__main__":
    test_jit_apikey_lifecycle()
    print("\nALL ODOO JIT APIKEY TESTS PASSED")
