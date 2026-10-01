"""Phase 36 §13.2/§13.4 -- unit tests for
tools_odoo/knowledge_graph/install_state_sync.py.

Mock-driven throughout, matching test_incremental_graph_sync.py's own
established style/conventions for this sibling hook module.
"""

import asyncio
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import tools_odoo.knowledge_graph.install_state_sync as sync_module


class TestSyncModuleInstallState:
    def test_killswitch_env_var_skips_entirely(self):
        os.environ["OMA_SKIP_INSTALL_STATE_SYNC"] = "1"
        try:
            with patch.object(sync_module.asyncio, "create_task") as mock_create_task:
                sync_module.sync_module_install_state("my_module", "mydb")
            mock_create_task.assert_not_called()
        finally:
            os.environ.pop("OMA_SKIP_INSTALL_STATE_SYNC", None)

    def test_no_running_event_loop_fails_open_without_raising(self):
        sync_module.sync_module_install_state("my_module", "mydb")  # must not raise

    def test_schedules_a_background_task_when_event_loop_is_running(self):
        async def _drive():
            with patch.object(
                sync_module, "_run_install_state_sync", new=AsyncMock(return_value=None)
            ) as mock_run:
                sync_module.sync_module_install_state("my_module", "mydb", task_id="t1")
                await asyncio.sleep(0)
            mock_run.assert_called_once_with("my_module", "mydb", "t1")

        asyncio.run(_drive())


class TestFetchAndWriteState:
    def test_none_state_from_get_module_state_fast_is_a_silent_no_op(self):
        fake_client = MagicMock()
        fake_client.get_module_state_fast.return_value = None
        with patch.dict(sys.modules, {"tools_odoo.odoo_schema_client": fake_client}):
            sync_module._fetch_and_write_state("my_module", "mydb")
        # Nothing else should have been attempted -- no assertion needed beyond "did not raise".

    def test_real_state_is_written_via_update_module_install_state(self):
        fake_schema_client = MagicMock()
        fake_schema_client.get_module_state_fast.return_value = "installed"
        fake_neo4j_client = MagicMock()
        fake_driver = MagicMock()
        fake_neo4j_client.get_neo4j_driver.return_value = fake_driver
        fake_graph_queries = MagicMock()

        with patch.dict(
            sys.modules,
            {
                "tools_odoo.odoo_schema_client": fake_schema_client,
                "infra.neo4j_client": fake_neo4j_client,
                "tools_odoo.graph_queries": fake_graph_queries,
            },
        ):
            sync_module._fetch_and_write_state("my_module", "mydb")

        fake_graph_queries.update_module_install_state.assert_called_once()
        call_args = fake_graph_queries.update_module_install_state.call_args.args
        assert call_args[0] is fake_driver
        assert call_args[1] == "my_module"
        assert call_args[2] == "installed"
        assert isinstance(call_args[3], str) and call_args[3]  # a real ISO timestamp string


class TestRunInstallStateSyncFailsOpen:
    def test_exception_in_fetch_and_write_never_propagates(self):
        async def _drive():
            with patch.object(sync_module, "_fetch_and_write_state", side_effect=RuntimeError("boom")):
                await sync_module._run_install_state_sync("my_module", "mydb", task_id=None)

        asyncio.run(_drive())  # must not raise
