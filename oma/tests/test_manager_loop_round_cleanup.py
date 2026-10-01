"""Phase 20 Area 2 (2026-07-17): unit tests for
manager/loop.py's cleanup_module_from_failed_round() -- pure logic
against a monkeypatched uninstall_module, no live SSH/DB/gateway
needed, same style as today's other isolated test files.

Real bug this fixes: a round whose own install genuinely succeeded
(Build's real install_result.success=True) but got rejected on a
LATER, normal verification failure (Code-Review, an "already real"
model-name check, etc. -- not an exception) used to leave that real
install permanently contaminating the shared database, since only
PartialTaskFailure (an exception) ever triggered the existing
compensating-actions cleanup. Confirmed live, repeatedly: tasks
defining a brand-new model (e.g. 'asset.registry', 'vendor.review')
round-1-installed for real, got rejected by Code-Review on an
unrelated scope issue, and every subsequent retry failed immediately
because the model name was still genuinely real in the shared
database.
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import manager.loop as loop_module
from manager.loop import cleanup_module_from_failed_round


def test_no_cleanup_when_build_never_claimed_complete():
    """The common, harmless case: a round rejected by pre-write
    validation never reached a real install at all -- nothing to clean
    up, and this must be distinguishable from a genuine cleanup need.
    """
    result = asyncio.run(
        cleanup_module_from_failed_round({"module_name": "oma_x", "db": "odoo16_dev"}, False)
    )
    assert result is None
    print("PASS: no cleanup attempted when build_output.claims_complete is False")


def test_no_cleanup_when_detail_missing_module_name_or_db():
    result = asyncio.run(cleanup_module_from_failed_round({}, True))
    assert result is None
    print("PASS: no cleanup attempted when detail lacks module_name/db (nothing to clean up)")


def test_cleanup_invoked_and_succeeds(monkeypatch):
    calls = []

    class FakeInstallResult:
        success = True
        message = "Uninstall completed."

    def fake_uninstall_module(module_name, db):
        calls.append((module_name, db))
        return FakeInstallResult()

    monkeypatch.setattr("tools_odoo.module_dev.toolchain.uninstall_module", fake_uninstall_module)

    result = asyncio.run(
        cleanup_module_from_failed_round({"module_name": "oma_broken_task", "db": "odoo16_dev"}, True)
    )
    assert result == (True, "Uninstall completed.")
    assert calls == [("oma_broken_task", "odoo16_dev")]
    print("PASS: a real install that later failed verification is uninstalled for real, with the correct module/db")


def test_cleanup_failure_is_reported_not_swallowed(monkeypatch):
    class FakeInstallResult:
        success = False
        message = "Uninstall failed: module not found."

    def fake_uninstall_module(module_name, db):
        return FakeInstallResult()

    monkeypatch.setattr("tools_odoo.module_dev.toolchain.uninstall_module", fake_uninstall_module)

    result = asyncio.run(
        cleanup_module_from_failed_round({"module_name": "oma_x", "db": "odoo16_dev"}, True)
    )
    assert result == (False, "Uninstall failed: module not found.")
    print("PASS: a real uninstall failure is reported (success=False), not silently treated as success")


def test_cleanup_exception_is_caught_and_reported():
    """A real exception from uninstall_module (e.g. an SSH failure)
    must never crash the round loop -- best-effort cleanup, the round's
    own real failure remains the primary signal either way.
    """
    import tools_odoo.module_dev.toolchain as toolchain_module
    original = toolchain_module.uninstall_module

    def raising_uninstall_module(module_name, db):
        raise RuntimeError("SSH connection failed")

    toolchain_module.uninstall_module = raising_uninstall_module
    try:
        result = asyncio.run(
            cleanup_module_from_failed_round({"module_name": "oma_x", "db": "odoo16_dev"}, True)
        )
    finally:
        toolchain_module.uninstall_module = original

    assert result is not None
    success, message = result
    assert success is False
    assert "SSH connection failed" in message
    print("PASS: an exception during cleanup is caught and reported, never crashes the caller")


if __name__ == "__main__":
    test_no_cleanup_when_build_never_claimed_complete()
    test_no_cleanup_when_detail_missing_module_name_or_db()
    test_cleanup_invoked_and_succeeds()
    test_cleanup_failure_is_reported_not_swallowed()
    test_cleanup_exception_is_caught_and_reported()
    print("\nALL ROUND-CLEANUP TESTS PASSED")
