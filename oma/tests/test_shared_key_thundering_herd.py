"""Phase 26A follow-up (2026-07-27, task 004): real, live-reproduced
concurrency bug -- best-of-N (2-3 candidates generated via
asyncio.gather(), each running its own OS thread via asyncio.to_thread())
raced on `_get_or_create_shared_key()`'s cold-cache path, each
independently kicking off an 8-20s `create_task_api_key()` odoo-bin-shell
call. Confirmed live: a real, genuinely-existing `_inherit` target
(`project.fieldjob`, independently confirmed to have 49 real fields) was
reported as "does not exist as a real model anywhere" -- consistent with
one of the racing threads' work failing/timing out and
`_read_real_field_rows()`'s own broad `except Exception: return None`
swallowing it as a false "model doesn't exist" result. Fixed with a
per-process lock serializing the cold-cache path.
"""

import os
import sys
import threading
import time
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import tools_odoo.odoo_schema_client as osc


def test_concurrent_cold_cache_calls_only_create_the_key_once():
    osc._shared_key_cache.clear()
    create_calls = []
    create_lock = threading.Lock()

    def slow_create_task_api_key(db, login, key_name):
        with create_lock:
            create_calls.append((db, login, key_name))
        time.sleep(0.2)  # simulate the real 8-20s odoo-bin-shell cost
        return 1, "fake-plaintext-key"

    class _FakeCommonProxy:
        def authenticate(self, db, login, key, ctx):
            return 2

    with patch("tools_odoo.odoo_schema_client.create_task_api_key", side_effect=slow_create_task_api_key), \
         patch("tools_odoo.odoo_schema_client.load_odoo_settings", return_value=type("S", (), {"url": "http://fake"})()), \
         patch("xmlrpc.client.ServerProxy", return_value=_FakeCommonProxy()):
        results = []
        errors = []

        def worker():
            try:
                results.append(osc._get_or_create_shared_key("odoo16_dev", "Admin"))
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

    assert not errors, f"no worker should raise -- got {errors!r}"
    assert len(create_calls) == 1, (
        f"expected exactly ONE real key-creation call regardless of how many threads raced in on a "
        f"cold cache -- got {len(create_calls)}: {create_calls!r}"
    )
    assert all(r == (2, "fake-plaintext-key") for r in results), (
        f"every racing thread must still get back the single, real, cached result -- got {results!r}"
    )
    print(f"PASS: {len(threads)} concurrent cold-cache callers triggered exactly 1 real key creation, "
          f"all received the correctly cached result")


if __name__ == "__main__":
    test_concurrent_cold_cache_calls_only_create_the_key_once()
    print("\nALL SHARED-KEY THUNDERING-HERD TESTS PASSED")
