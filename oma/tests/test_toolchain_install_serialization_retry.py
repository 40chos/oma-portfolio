"""Phase 31 §9/Phase B (2026-08-08): real, root-caused bug found live during the round-3
controlled concurrency experiment. Confirmed via the actual traceback recorded in Postgres
(agent_memory_events, task 0cc73a5d-1c15-4c98-b492-c0cb6c85ae45):

    psycopg2.errors.SerializationFailure: could not serialize access due to concurrent update

...raised from inside a fresh `odoo-bin -i` subprocess's own Registry.new()/load_modules()/
env.flush_all() when TWO genuinely independent constraint nodes (different target Odoo models,
correctly non-colliding on infra.fencing's own per-module lock) both installed concurrently
against the SAME physical duplicate database. This is a Postgres-documented, by-design transient
condition (SQLSTATE 40001) meant to be retried by the client, not treated as a real content
failure -- confirmed correct per Postgres's own docs and matching this same file's existing
retry pattern for the sibling transient "processing a scheduled action" condition in
_install_module_via_warm_worker().
"""

import os
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import tools_odoo.module_dev.toolchain as toolchain_module
from tools_odoo.module_dev.toolchain import install_module

_REAL_SERIALIZATION_FAILURE_LOG = (
    "2026-08-08 05:58:30,022 238784 ERROR odoo16_dev_fresh_20260707_123141 odoo.modules.registry: "
    "Failed to load registry\n"
    "Traceback (most recent call last):\n"
    "  File \"/opt/site/16/odoo/modules/registry.py\", line 87, in new\n"
    "    odoo.modules.load_modules(registry, force_demo, status, update_module)\n"
    "  File \"/opt/site/16/odoo/models.py\", line 3917, in _write\n"
    "    cr.execute(query, params + [sub_ids])\n"
    "  File \"/opt/site/16/odoo/sql_db.py\", line 321, in execute\n"
    "    res = self._obj.execute(query, params)\n"
    "psycopg2.errors.SerializationFailure: could not serialize access due to concurrent update\n"
    "2026-08-08 05:58:30,028 238784 CRITICAL odoo16_dev_fresh_20260707_123141 odoo.service.server: "
    "Failed to initialize database `odoo16_dev_fresh_20260707_123141`.\n"
)

_REAL_SUCCESS_LOG = (
    "2026-08-08 06:10:00,000 999 INFO odoo16_dev_fresh_20260707_123141 odoo.modules.loading: "
    "Module oma_x loaded in 1.20s, 42 queries\n"
)

_REAL_UNRELATED_CRASH_LOG = (
    "CRITICAL odoo16_dev_fresh_20260707_123141 odoo.modules.registry: "
    "Failed to load registry\n"
    "Traceback (most recent call last):\n"
    "ValueError: Invalid field name 'this_field_does_not_exist' on model 'product.template'\n"
)


def _setup(monkeypatch, install_call_logs, state="installed"):
    """install_call_logs: list of stdout strings returned by successive real INSTALL subprocess
    calls (not the state-check call, which always comes last and is handled separately) -- lets
    a test simulate "fails N times with a given log, then succeeds/fails for real."
    """
    def fake_get_or_create_shared_key(db, login):
        return 2, "fake-api-key"

    monkeypatch.setattr(toolchain_module, "_get_or_create_shared_key", fake_get_or_create_shared_key, raising=False)
    import tools_odoo.odoo_schema_client as schema_client_module
    monkeypatch.setattr(schema_client_module, "_get_or_create_shared_key", fake_get_or_create_shared_key)
    monkeypatch.setattr(toolchain_module, "_find_missing_manifest_data_files", lambda *a, **k: None)
    monkeypatch.setattr(toolchain_module, "is_fast_path_eligible", lambda db: False)
    import time as real_time_module
    monkeypatch.setattr(real_time_module, "sleep", lambda *a, **k: None)  # never really wait in a unit test --
    # install_module()'s own retry loop does `import time as _time` locally, which binds to this
    # SAME global `time` module object, so patching it here reaches that local alias too.

    install_calls = {"n": 0}

    def fake_run_in_container(bash_command, timeout=180, container=None):
        # The real install command always contains "-i " (module install flag); the later
        # positive-state-verification call is a different, base64-encoded Python script --
        # distinguished the same way the existing sibling test file in this repo already does:
        # by call order relative to how many real install attempts have happened so far.
        if "-i " in bash_command and "--stop-after-init" in bash_command:
            idx = install_calls["n"]
            install_calls["n"] += 1
            log = install_call_logs[min(idx, len(install_call_logs) - 1)]
            return subprocess.CompletedProcess(args=[], returncode=0 if "success" in log or "loaded" in log else 1, stdout=log, stderr="")
        return subprocess.CompletedProcess(args=[], returncode=0, stdout=f"INSTALL_STATE_CHECK:{state}\n", stderr="")

    monkeypatch.setattr(toolchain_module, "_run_in_container", fake_run_in_container)
    return install_calls


def test_a_transient_serialization_failure_is_retried_and_eventually_succeeds(monkeypatch):
    """The real, headline fix: the FIRST attempt hits the exact real SerializationFailure
    signature found live; the SECOND attempt (same underlying content, just no longer racing
    another concurrent install) succeeds -- must be reported as a genuine success, not a false
    failure, and the retry must actually have happened (not just gotten lucky on attempt 1).
    """
    install_calls = _setup(monkeypatch, [_REAL_SERIALIZATION_FAILURE_LOG, _REAL_SUCCESS_LOG])
    result = install_module("oma_x", "odoo16_dev_fresh_20260707_123141")
    assert result.success is True, (
        f"a transient SerializationFailure that clears on retry must end in a real success, "
        f"not a false failure -- got error_kind={result.error_kind!r}: {result.message}"
    )
    assert install_calls["n"] == 2, (
        f"must have genuinely retried the install once after the transient failure, not just "
        f"gotten lucky on the first attempt or retried more than needed -- got {install_calls['n']} attempts"
    )
    print("PASS: a transient SerializationFailure is retried and a genuine eventual success is reported correctly")


def test_a_serialization_failure_that_never_clears_still_eventually_fails_for_real(monkeypatch):
    """Never masks a genuinely, persistently broken install forever -- after exhausting the
    bounded retry budget, a still-failing install is reported as a real failure, same as today.
    """
    install_calls = _setup(monkeypatch, [_REAL_SERIALIZATION_FAILURE_LOG])  # every attempt gets the same log
    result = install_module("oma_x", "odoo16_dev_fresh_20260707_123141")
    assert result.success is False
    assert result.error_kind == "generic_failure"
    assert install_calls["n"] == 3, (
        f"must retry up to the bounded limit (3 attempts total) before giving up, never fewer "
        f"(masking a maybe-transient case too early) or more (an unbounded retry loop) -- "
        f"got {install_calls['n']} attempts"
    )
    print(f"PASS: a persistent SerializationFailure still correctly fails for real after {install_calls['n']} bounded attempts")


def test_a_genuine_unrelated_failure_is_never_retried(monkeypatch):
    """The retry must be scoped EXACTLY to the SerializationFailure signature -- a real content
    bug (a genuinely wrong field reference, here) must still surface on the very first attempt,
    unchanged, never masked or delayed by a pointless retry loop.
    """
    install_calls = _setup(monkeypatch, [_REAL_UNRELATED_CRASH_LOG])
    result = install_module("oma_x", "odoo16_dev_fresh_20260707_123141")
    assert result.success is False
    assert result.error_kind == "generic_failure"
    assert install_calls["n"] == 1, (
        f"a genuine, unrelated failure must never trigger a retry at all -- got {install_calls['n']} attempts"
    )
    print("PASS: a genuine, unrelated install failure is reported on the first attempt, never retried")


if __name__ == "__main__":
    import types

    class _FakeMonkeypatch:
        def __init__(self):
            self._undo = []

        def setattr(self, target, name, value, raising=True):
            if isinstance(target, str):
                mod = sys.modules[target]
                target = mod
            old = getattr(target, name, None)
            self._undo.append((target, name, old))
            setattr(target, name, value)

    for fn in (
        test_a_transient_serialization_failure_is_retried_and_eventually_succeeds,
        test_a_serialization_failure_that_never_clears_still_eventually_fails_for_real,
        test_a_genuine_unrelated_failure_is_never_retried,
    ):
        mp = _FakeMonkeypatch()
        fn(mp)
    print("\nALL SERIALIZATION-RETRY TESTS PASSED")
