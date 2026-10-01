"""Phase 36 §0.9c -- unit tests for tools_odoo/knowledge_graph/incremental_sync.py.

Mock-driven for the module-parse/subprocess boundaries; real filesystem I/O (tmp_path) for
the JSONL replace-in-place logic, since that's this module's own real, worth-exercising-for-
real surface -- same convention as test_security_posture_alert_check.py.
"""

import asyncio
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import tools_odoo.knowledge_graph.incremental_sync as sync_module


class TestTriggerIncrementalGraphSync:
    def test_killswitch_env_var_skips_entirely(self):
        os.environ["OMA_SKIP_GRAPH_INCREMENTAL_SYNC"] = "1"
        try:
            with patch.object(sync_module.asyncio, "create_task") as mock_create_task:
                sync_module.trigger_incremental_graph_sync("my_module")
            mock_create_task.assert_not_called()
        finally:
            os.environ.pop("OMA_SKIP_GRAPH_INCREMENTAL_SYNC", None)

    def test_no_running_event_loop_fails_open_without_raising(self):
        # Called synchronously (no event loop) -- must not raise.
        sync_module.trigger_incremental_graph_sync("my_module")

    def test_schedules_a_background_task_when_event_loop_is_running(self):
        async def _drive():
            with patch.object(sync_module, "_run_incremental_sync", new=AsyncMock(return_value=None)) as mock_run:
                sync_module.trigger_incremental_graph_sync("my_module", task_id="t1")
                # Let the scheduled task actually get a chance to run/complete.
                await asyncio.sleep(0)
            mock_run.assert_called_once_with("my_module", "t1")

        asyncio.run(_drive())


class TestFetchModuleToLocalTemp:
    """Real, confirmed bug found and fixed live (2026-08-13): this function replaces a
    previous implementation that checked a LOCAL path (`_MODULE_DEV_ADDONS_DIR`, matching
    the STRING used for the REMOTE, SSH-only path inside the container) -- meaning the old
    code silently no-op'ed for every real module, forever, since that local path never
    existed on this host. These tests mock `_run_in_container` (the same real, env-based
    SSH helper every other real module-content read in this codebase already uses) instead
    of relying on local filesystem presence, which is what a correct test of the real,
    fixed behavior actually needs to exercise.
    """

    def test_returns_none_when_find_reports_module_not_present(self):
        with patch("tools_odoo.module_dev.toolchain._run_in_container") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
            result = sync_module._fetch_module_to_local_temp("missing_module")
        assert result is None

    def test_returns_none_when_find_itself_fails(self):
        with patch("tools_odoo.module_dev.toolchain._run_in_container") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="boom")
            result = sync_module._fetch_module_to_local_temp("missing_module")
        assert result is None

    def test_reconstructs_real_files_locally_from_remote_content(self):
        remote_dir = "/mnt/extra-addons/my_module"

        def _fake_run_in_container(cmd, *args, **kwargs):
            if cmd.startswith("find"):
                return subprocess.CompletedProcess(
                    args=[], returncode=0,
                    stdout=f"{remote_dir}/__manifest__.py\n{remote_dir}/models/models.py\n",
                    stderr="",
                )
            if cmd == f"cat {remote_dir}/__manifest__.py":
                return subprocess.CompletedProcess(args=[], returncode=0, stdout="{'depends': ['base']}", stderr="")
            if cmd == f"cat {remote_dir}/models/models.py":
                return subprocess.CompletedProcess(args=[], returncode=0, stdout="class X: pass", stderr="")
            raise AssertionError(f"unexpected command: {cmd!r}")

        with patch("tools_odoo.module_dev.toolchain._run_in_container", side_effect=_fake_run_in_container):
            local_dir = sync_module._fetch_module_to_local_temp("my_module")

        try:
            assert local_dir is not None
            assert (local_dir / "__manifest__.py").read_text() == "{'depends': ['base']}"
            assert (local_dir / "models" / "models.py").read_text() == "class X: pass"
        finally:
            if local_dir is not None:
                import shutil as _shutil
                _shutil.rmtree(local_dir.parent, ignore_errors=True)

    def test_excludes_pycache_from_the_remote_find(self):
        with patch("tools_odoo.module_dev.toolchain._run_in_container") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
            sync_module._fetch_module_to_local_temp("my_module")
        find_cmd = mock_run.call_args_list[0].args[0]
        assert "__pycache__" in find_cmd and "*.pyc" in find_cmd

    def test_unicode_decode_error_on_one_file_skips_it_not_the_whole_fetch(self):
        remote_dir = "/mnt/extra-addons/my_module"

        def _fake_run_in_container(cmd, *args, **kwargs):
            if cmd.startswith("find"):
                return subprocess.CompletedProcess(
                    args=[], returncode=0,
                    stdout=f"{remote_dir}/__manifest__.py\n{remote_dir}/some_binary_file\n",
                    stderr="",
                )
            if cmd == f"cat {remote_dir}/__manifest__.py":
                return subprocess.CompletedProcess(args=[], returncode=0, stdout="{'depends': []}", stderr="")
            if cmd == f"cat {remote_dir}/some_binary_file":
                raise UnicodeDecodeError("utf-8", b"\xa5", 0, 1, "invalid start byte")
            raise AssertionError(f"unexpected command: {cmd!r}")

        with patch("tools_odoo.module_dev.toolchain._run_in_container", side_effect=_fake_run_in_container):
            local_dir = sync_module._fetch_module_to_local_temp("my_module")

        try:
            assert local_dir is not None
            assert (local_dir / "__manifest__.py").read_text() == "{'depends': []}"
            assert not (local_dir / "some_binary_file").exists()
        finally:
            if local_dir is not None:
                import shutil as _shutil
                _shutil.rmtree(local_dir.parent, ignore_errors=True)


class TestReparseAndUpdateSourceJsonl:
    def test_module_fetch_returning_none_is_a_silent_no_op(self, tmp_path, monkeypatch):
        jsonl_path = tmp_path / "final_module_graph.jsonl"
        jsonl_path.write_text('{"module": "other"}\n')
        monkeypatch.setattr(sync_module, "_SOURCE_JSONL", jsonl_path)

        with patch.object(sync_module, "_fetch_module_to_local_temp", return_value=None):
            sync_module._reparse_and_update_source_jsonl("missing_module")

        assert jsonl_path.read_text() == '{"module": "other"}\n'  # untouched

    def test_replaces_existing_module_record_in_place(self, tmp_path, monkeypatch):
        module_dir = tmp_path / "fetched" / "my_module"
        module_dir.mkdir(parents=True)
        (module_dir / "__manifest__.py").write_text("{'depends': ['base']}")

        jsonl_path = tmp_path / "final_module_graph.jsonl"
        jsonl_path.write_text(
            json.dumps({"module": "other", "deps": []}) + "\n"
            + json.dumps({"module": "my_module", "deps": ["stale"]}) + "\n"
        )
        monkeypatch.setattr(sync_module, "_SOURCE_JSONL", jsonl_path)

        with patch.object(sync_module, "_fetch_module_to_local_temp", return_value=module_dir):
            sync_module._reparse_and_update_source_jsonl("my_module")

        lines = [json.loads(l) for l in jsonl_path.read_text().splitlines() if l.strip()]
        assert len(lines) == 2
        other = next(r for r in lines if r["module"] == "other")
        assert other["deps"] == []  # untouched
        mine = next(r for r in lines if r["module"] == "my_module")
        assert mine["deps"] == ["base"]  # freshly re-parsed, not the stale value

    def test_appends_new_module_record_when_not_previously_present(self, tmp_path, monkeypatch):
        module_dir = tmp_path / "fetched" / "brand_new_module"
        module_dir.mkdir(parents=True)

        jsonl_path = tmp_path / "final_module_graph.jsonl"
        jsonl_path.write_text(json.dumps({"module": "other"}) + "\n")
        monkeypatch.setattr(sync_module, "_SOURCE_JSONL", jsonl_path)

        with patch.object(sync_module, "_fetch_module_to_local_temp", return_value=module_dir):
            sync_module._reparse_and_update_source_jsonl("brand_new_module")

        lines = [json.loads(l) for l in jsonl_path.read_text().splitlines() if l.strip()]
        assert {r["module"] for r in lines} == {"other", "brand_new_module"}

    def test_creates_jsonl_when_it_does_not_exist_yet(self, tmp_path, monkeypatch):
        module_dir = tmp_path / "fetched" / "first_module"
        module_dir.mkdir(parents=True)
        jsonl_path = tmp_path / "does_not_exist_yet.jsonl"
        monkeypatch.setattr(sync_module, "_SOURCE_JSONL", jsonl_path)

        with patch.object(sync_module, "_fetch_module_to_local_temp", return_value=module_dir):
            sync_module._reparse_and_update_source_jsonl("first_module")

        lines = [json.loads(l) for l in jsonl_path.read_text().splitlines() if l.strip()]
        assert lines == [{"module": "first_module"}] or lines[0]["module"] == "first_module"

    def test_malformed_line_in_jsonl_is_preserved_untouched(self, tmp_path, monkeypatch):
        module_dir = tmp_path / "fetched" / "my_module"
        module_dir.mkdir(parents=True)

        jsonl_path = tmp_path / "final_module_graph.jsonl"
        jsonl_path.write_text("not valid json{{{\n" + json.dumps({"module": "my_module"}) + "\n")
        monkeypatch.setattr(sync_module, "_SOURCE_JSONL", jsonl_path)

        with patch.object(sync_module, "_fetch_module_to_local_temp", return_value=module_dir):
            sync_module._reparse_and_update_source_jsonl("my_module")

        content = jsonl_path.read_text()
        assert "not valid json{{{" in content  # malformed line left alone, not dropped


class TestRunEtlIncremental:
    def test_missing_etl_script_is_a_silent_no_op(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sync_module, "_ETL_SCRIPT", tmp_path / "does_not_exist.py")
        with patch.object(sync_module.subprocess, "run") as mock_run:
            sync_module._run_etl_incremental("my_module")
        mock_run.assert_not_called()

    def test_invokes_etl_with_incremental_mode_and_module_scope(self, tmp_path, monkeypatch):
        script = tmp_path / "odoo_kg_to_neo4j.py"
        script.write_text("# fake")
        monkeypatch.setattr(sync_module, "_ETL_SCRIPT", script)

        with patch.object(
            sync_module.subprocess, "run",
            return_value=subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr=""),
        ) as mock_run:
            sync_module._run_etl_incremental("my_module")

        args = mock_run.call_args.args[0]
        assert "--mode" in args and "incremental" in args
        assert "--module" in args and "my_module" in args

    def test_nonzero_exit_logs_a_warning_but_does_not_raise(self, tmp_path, monkeypatch):
        script = tmp_path / "odoo_kg_to_neo4j.py"
        script.write_text("# fake")
        monkeypatch.setattr(sync_module, "_ETL_SCRIPT", script)

        with patch.object(
            sync_module.subprocess, "run",
            return_value=subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="boom"),
        ):
            sync_module._run_etl_incremental("my_module")  # must not raise


class TestRunIncrementalSyncFailsOpen:
    def test_exception_in_reparse_step_never_propagates(self):
        async def _drive():
            with patch.object(sync_module, "_reparse_and_update_source_jsonl", side_effect=RuntimeError("boom")):
                await sync_module._run_incremental_sync("my_module", task_id=None)

        asyncio.run(_drive())  # must not raise

    def test_exception_in_etl_step_never_propagates(self):
        async def _drive():
            with patch.object(sync_module, "_reparse_and_update_source_jsonl", return_value=None), \
                 patch.object(sync_module, "_run_etl_incremental", side_effect=RuntimeError("boom")):
                await sync_module._run_incremental_sync("my_module", task_id=None)

        asyncio.run(_drive())  # must not raise
