"""Phase 35 fix (2026-08-13 overnight) -- tools_odoo.odoo_schema_client's ServerProxy
instances had no socket timeout at all, confirmed live to cause an indefinite hang
(`do_poll`, 10+ minutes) that freezes the entire single-worker oma-chat-ui.service.
Verifies the timeout transport is real and actually wired into the connection, without
needing a live Odoo instance (pure unit test on the transport object itself).
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tools_odoo.odoo_schema_client import (  # noqa: E402
    _XMLRPC_TIMEOUT_SECONDS,
    _TimeoutTransport,
    _timeout_transport,
)


def test_timeout_transport_sets_a_real_socket_timeout_on_its_connection():
    transport = _timeout_transport()
    assert isinstance(transport, _TimeoutTransport)
    conn = transport.make_connection("127.0.0.1:1")  # never actually connects -- just builds the object
    assert conn.timeout == _XMLRPC_TIMEOUT_SECONDS
    print(f"PASS: _TimeoutTransport sets a real {_XMLRPC_TIMEOUT_SECONDS}s socket timeout on its connection")


def test_models_proxy_and_shared_key_proxies_use_the_timeout_transport():
    import inspect

    import tools_odoo.odoo_schema_client as mod

    source = inspect.getsource(mod)
    # Every real xmlrpc.client.ServerProxy construction in this file must pass transport=
    # _timeout_transport() -- a real, live-confirmed hang happened because one of these three
    # didn't. Simple, honest source-level check: no ServerProxy( call in this file may omit it.
    for line in source.splitlines():
        if "xmlrpc.client.ServerProxy(" in line:
            assert "transport=_timeout_transport()" in line, (
                f"a ServerProxy construction is missing the timeout transport: {line.strip()!r}"
            )
    print("PASS: every ServerProxy construction in odoo_schema_client.py uses the timeout transport")


if __name__ == "__main__":
    test_timeout_transport_sets_a_real_socket_timeout_on_its_connection()
    test_models_proxy_and_shared_key_proxies_use_the_timeout_transport()
    print("ALL PASSED")
