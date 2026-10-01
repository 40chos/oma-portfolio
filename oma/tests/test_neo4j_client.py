"""Phase 36 §3.2 -- infra/neo4j_client.py: singleton driver factory, credentials
sourced only from infra.settings, never a literal address/password anywhere in
this module. Mocked GraphDatabase.driver throughout -- no real Neo4j instance
is loaded with Odoo data yet (that happens in a later migration phase); live
integration testing against the real instance is a documented follow-up.
"""

import os
import sys
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from infra import neo4j_client


@pytest.fixture(autouse=True)
def _clear_singleton():
    neo4j_client.reset_neo4j_driver_cache_for_tests()
    yield
    neo4j_client.reset_neo4j_driver_cache_for_tests()


def _env(**overrides):
    base = {
        "NEO4J_URI": "bolt://10.1.13.151:7687",
        "NEO4J_USER": "test-user",
        "NEO4J_PASSWORD": "test-password",
    }
    base.update(overrides)
    return base


def test_get_neo4j_driver_reads_credentials_only_from_settings():
    fake_driver = MagicMock(name="driver")
    with patch.dict(os.environ, _env(), clear=False):
        with patch("infra.neo4j_client.GraphDatabase") as mock_gd:
            mock_gd.driver.return_value = fake_driver
            result = neo4j_client.get_neo4j_driver()

    assert result is fake_driver
    mock_gd.driver.assert_called_once()
    args, kwargs = mock_gd.driver.call_args
    assert args[0] == "bolt://10.1.13.151:7687"
    assert kwargs["auth"] == ("test-user", "test-password")
    assert kwargs["connection_timeout"] == 5.0
    assert kwargs["max_transaction_retry_time"] == 15.0


def test_get_neo4j_driver_is_a_process_wide_singleton():
    with patch.dict(os.environ, _env(), clear=False):
        with patch("infra.neo4j_client.GraphDatabase") as mock_gd:
            mock_gd.driver.side_effect = [MagicMock(name="d1"), MagicMock(name="d2")]
            first = neo4j_client.get_neo4j_driver()
            second = neo4j_client.get_neo4j_driver()

    assert first is second
    mock_gd.driver.assert_called_once()


def test_get_neo4j_read_driver_returns_same_singleton_not_a_second_driver():
    with patch.dict(os.environ, _env(), clear=False):
        with patch("infra.neo4j_client.GraphDatabase") as mock_gd:
            mock_gd.driver.return_value = MagicMock(name="d")
            write_handle = neo4j_client.get_neo4j_driver()
            read_handle = neo4j_client.get_neo4j_read_driver()

    assert read_handle is write_handle
    mock_gd.driver.assert_called_once()


def test_get_neo4j_driver_respects_timeout_env_overrides():
    with patch.dict(
        os.environ,
        _env(NEO4J_CONNECTION_TIMEOUT_S="2.5", NEO4J_MAX_RETRY_TIME_S="9.0"),
        clear=False,
    ):
        with patch("infra.neo4j_client.GraphDatabase") as mock_gd:
            mock_gd.driver.return_value = MagicMock(name="d")
            neo4j_client.get_neo4j_driver()

    _, kwargs = mock_gd.driver.call_args
    assert kwargs["connection_timeout"] == 2.5
    assert kwargs["max_transaction_retry_time"] == 9.0


def test_missing_required_env_var_raises_runtime_error():
    env = _env()
    del env["NEO4J_PASSWORD"]
    with patch.dict(os.environ, env, clear=True):
        with pytest.raises(RuntimeError, match="NEO4J_PASSWORD"):
            neo4j_client.get_neo4j_driver()
