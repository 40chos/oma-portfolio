"""Phase 26D -- regression tests for scripts/oma_debris_sweep.py's staleness
classification and destructive-action gating. Mocked XML-RPC/Postgres/Redis
throughout (this tool operates directly against a real, shared database --
never exercised against the real target in a test), matching this repo's own
convention of mocking exactly the external boundary and nothing else.

The safety-critical case (§26D.5's own regression gate: a module belonging to
an active, in-progress, or still-resumable task must NEVER be classified as
stale) gets its own dedicated tests below, not just incidental coverage.
"""
from unittest.mock import MagicMock, patch

import scripts.oma_debris_sweep as sweep


def _redis_stub(state_value=None):
    r = MagicMock()
    r.get.return_value = state_value
    return r


def test_classify_staleness_excludes_task_with_live_checkpoint():
    with patch.object(sweep, "get_latest_checkpoint", return_value={"id": 1}):
        stale, reason = sweep.classify_staleness("task-a", _redis_stub(), set())
    assert stale is False
    assert "checkpoint" in reason


def test_classify_staleness_excludes_task_with_live_redis_state():
    with patch.object(sweep, "get_latest_checkpoint", return_value=None):
        stale, reason = sweep.classify_staleness("task-a", _redis_stub(state_value=b"running"), set())
    assert stale is False
    assert "Redis" in reason


def test_classify_staleness_excludes_pending_decision():
    with patch.object(sweep, "get_latest_checkpoint", return_value=None):
        stale, reason = sweep.classify_staleness("task-a", _redis_stub(), {"task-a"})
    assert stale is False
    assert "pending" in reason


def test_classify_staleness_excludes_task_with_passing_outcome():
    """The single most safety-critical case in this file: a task that
    actually PASSED must never have its module swept, even though it is
    just as 'terminal' (will never run again) as a genuinely abandoned
    one -- its module is the real, wanted deliverable, not debris.
    """
    with patch.object(sweep, "get_latest_checkpoint", return_value=None), \
         patch.object(sweep, "_outcome_row_for_task", return_value={"summary": "passed"}):
        stale, reason = sweep.classify_staleness("task-a", _redis_stub(), set())
    assert stale is False
    assert "passing outcome" in reason


def test_classify_staleness_flags_genuinely_abandoned_task():
    with patch.object(sweep, "get_latest_checkpoint", return_value=None), \
         patch.object(sweep, "_outcome_row_for_task", return_value=None):
        stale, reason = sweep.classify_staleness("task-a", _redis_stub(), set())
    assert stale is True
    assert "abandoned" in reason


def test_annotated_rows_unresolved_task_id_is_never_stale():
    """A module whose owning task_id can't be resolved at all must default
    to NOT stale -- an unknown owner is exactly the case this tool cannot
    safely reason about, so it must never be swept.
    """
    with patch.object(sweep, "list_oma_modules", return_value=[
        {"name": "oma_unknown_deadbeef", "state": "installed", "write_date": "2026-08-01"},
    ]), patch.object(sweep, "build_module_task_index", return_value={}), \
         patch.object(sweep, "get_redis_client", return_value=_redis_stub()), \
         patch.object(sweep, "list_pending_task_ids", return_value=[]), \
         patch.object(sweep, "list_pending_escalations", return_value=[]):
        rows = sweep._annotated_rows("odoo16_dev")
    assert len(rows) == 1
    assert rows[0]["stale"] is False
    assert rows[0]["task_id"] is None


def test_list_stale_returns_only_genuinely_abandoned_modules():
    modules = [
        {"name": "oma_passed_task_aaaaaaaa", "state": "installed", "write_date": "2026-08-01"},
        {"name": "oma_abandoned_task_bbbbbbbb", "state": "installed", "write_date": "2026-08-01"},
        {"name": "oma_live_task_cccccccc", "state": "installed", "write_date": "2026-08-01"},
    ]
    task_index = {
        "oma_passed_task_aaaaaaaa": "task-passed",
        "oma_abandoned_task_bbbbbbbb": "task-abandoned",
        "oma_live_task_cccccccc": "task-live",
    }

    def fake_checkpoint(task_id):
        return {"id": 1} if task_id == "task-live" else None

    def fake_outcome(task_id):
        return {"summary": "passed"} if task_id == "task-passed" else None

    with patch.object(sweep, "list_oma_modules", return_value=modules), \
         patch.object(sweep, "build_module_task_index", return_value=task_index), \
         patch.object(sweep, "get_redis_client", return_value=_redis_stub()), \
         patch.object(sweep, "list_pending_task_ids", return_value=[]), \
         patch.object(sweep, "list_pending_escalations", return_value=[]), \
         patch.object(sweep, "get_latest_checkpoint", side_effect=fake_checkpoint), \
         patch.object(sweep, "_outcome_row_for_task", side_effect=fake_outcome):
        rows = sweep._annotated_rows("odoo16_dev")
        stale_names = {r["name"] for r in rows if r["stale"]}

    assert stale_names == {"oma_abandoned_task_bbbbbbbb"}


def test_uninstall_stale_dry_run_never_calls_real_uninstall():
    stale_rows = [{"name": "oma_abandoned_task_bbbbbbbb", "state": "installed", "write_date": "x",
                   "task_id": "task-abandoned", "stale": True, "reason": "abandoned"}]
    args = MagicMock(module_name=None, stale=True, confirm=False, db="odoo16_dev")
    with patch.object(sweep, "_annotated_rows", return_value=stale_rows), \
         patch.object(sweep, "uninstall_module") as mock_uninstall, \
         patch.object(sweep, "remove_scaffolded_module") as mock_remove:
        sweep.cmd_uninstall(args)
    mock_uninstall.assert_not_called()
    mock_remove.assert_not_called()


def test_uninstall_stale_confirm_calls_real_uninstall_with_exactly_the_stale_set():
    rows = [
        {"name": "oma_passed_task_aaaaaaaa", "state": "installed", "write_date": "x",
         "task_id": "task-passed", "stale": False, "reason": "passing"},
        {"name": "oma_abandoned_task_bbbbbbbb", "state": "installed", "write_date": "x",
         "task_id": "task-abandoned", "stale": True, "reason": "abandoned"},
    ]
    stale_only = [r for r in rows if r["stale"]]
    args = MagicMock(module_name=None, stale=True, confirm=True, db="odoo16_dev")
    mock_result = MagicMock(success=True, message="Uninstall completed.")
    with patch.object(sweep, "_annotated_rows", return_value=stale_only), \
         patch.object(sweep, "uninstall_module", return_value=mock_result) as mock_uninstall, \
         patch.object(sweep, "remove_scaffolded_module") as mock_remove:
        sweep.cmd_uninstall(args)

    mock_uninstall.assert_called_once_with("oma_abandoned_task_bbbbbbbb", "odoo16_dev")
    mock_remove.assert_called_once_with("oma_abandoned_task_bbbbbbbb")


def test_uninstall_single_module_name_bypasses_staleness_filter():
    """uninstall <name> (no --stale) is an explicit, operator-named single
    uninstall -- it must not consult staleness classification at all.
    """
    args = MagicMock(module_name="oma_explicit_target_00000000", stale=False, confirm=False, db="odoo16_dev")
    mock_result = MagicMock(success=True, message="Uninstall completed.")
    with patch.object(sweep, "uninstall_module", return_value=mock_result) as mock_uninstall, \
         patch.object(sweep, "remove_scaffolded_module") as mock_remove:
        sweep.cmd_uninstall(args)
    mock_uninstall.assert_called_once_with("oma_explicit_target_00000000", "odoo16_dev")
    mock_remove.assert_called_once_with("oma_explicit_target_00000000")


def test_uninstall_failed_result_does_not_remove_scaffold_files():
    """If the real uninstall_module() call fails, the scaffold directory
    must be left alone -- removing files for a module Odoo still thinks is
    installed would desync ir.module.module from disk state, the exact
    class of corruption this whole tool exists to clean up.
    """
    args = MagicMock(module_name="oma_explicit_target_00000000", stale=False, confirm=False, db="odoo16_dev")
    mock_result = MagicMock(success=False, message="click-odoo-uninstall failed")
    with patch.object(sweep, "uninstall_module", return_value=mock_result), \
         patch.object(sweep, "remove_scaffolded_module") as mock_remove:
        sweep.cmd_uninstall(args)
    mock_remove.assert_not_called()


def test_slugify_module_name_matches_the_real_build_specialist_derivation():
    """This module deliberately duplicates slugify_module_name() rather than
    importing specialists.build.specialist (see the function's own
    docstring) -- so a drift between the two would silently break every
    module_name -> task_id reverse-index lookup. Pinned here against the
    real function directly.
    """
    from specialists.build.specialist import slugify_module_name as real_slugify

    goal = "Add a priority field to the sale order list"
    task_id = "4a8651b5-8f84-4958-9a04-117c2b1ad7ac"
    assert sweep._slugify_module_name(goal, task_id) == real_slugify(goal, task_id)
