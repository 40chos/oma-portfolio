"""Phase 29 (2026-07-29): unit test for the real, confirmed live bug in
_extract_error_excerpt() -- the school_student task's own automated_tests
round reported "Sandbox install failed: Install failed (rc=255) -- see
log_tail for detail" 4+ times in a row, every time with a content-free
log_tail that only ever showed the sandbox container's own KNOWN-BENIGN
"Exception in thread odoo.service.httpd ... Address already in use"
block (module loading demonstrably continues normally immediately after
it, every time) -- because that benign exception is ALSO a real
`Traceback (most recent call last)` marker, it kept winning
_extract_error_excerpt()'s own "anchor on the first marker" search,
pushing the actual failure (further down in the log) out of the fixed-
size window every single time.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tools_odoo.module_dev.toolchain import _extract_error_excerpt

_REAL_BENIGN_HTTPD_BLOCK = (
    "Exception in thread odoo.service.httpd:\n"
    "Traceback (most recent call last):\n"
    '  File "/usr/lib/python3.9/threading.py", line 954, in _bootstrap_inner\n'
    "    self.run()\n"
    '  File "/usr/lib/python3.9/threading.py", line 892, in run\n'
    "    self._target(*self._args, **self._kwargs)\n"
    '  File "/opt/site/16/odoo/service/server.py", line 522, in http_thread\n'
    "    self.httpd = ThreadedWSGIServerReloadable(self.interface, self.port, self.app)\n"
    '  File "/opt/site/16/odoo/service/server.py", line 182, in __init__\n'
    "    super(ThreadedWSGIServerReloadable, self).__init__(host, port, app,\n"
    '  File "/usr/lib/python3/dist-packages/werkzeug/serving.py", line 740, in __init__\n'
    "    HTTPServer.__init__(self, server_address, handler)\n"
    '  File "/usr/lib/python3.9/socketserver.py", line 452, in __init__\n'
    "    self.server_bind()\n"
    '  File "/opt/site/16/odoo/service/server.py", line 198, in server_bind\n'
    "    super(ThreadedWSGIServerReloadable, self).server_bind()\n"
    '  File "/usr/lib/python3.9/http/server.py", line 138, in server_bind\n'
    "    socketserver.TCPServer.server_bind(self)\n"
    '  File "/usr/lib/python3.9/socketserver.py", line 466, in server_bind\n'
    "    self.socket.bind(self.server_address)\n"
    "OSError: [Errno 98] Address already in use\n"
)
# Real, confirmed follow-up bug found live (2026-07-29, same task, later
# round): the ORIGINAL, shorter benign-block fixture above (~250 chars
# from "http_thread" to "Address already in use") passed this test while
# the real, full-length stack trace (~840 chars between those same two
# anchors, matching Odoo's own real traceback depth) still slipped past
# both the skip-regex's own gap tolerance AND the surrounding-window
# size, silently reintroducing the exact bug this file was built to
# close. This fixture matches the REAL captured length exactly, so this
# specific regression can never silently recur.
assert _REAL_BENIGN_HTTPD_BLOCK.index("Address already in use") - _REAL_BENIGN_HTTPD_BLOCK.index("http_thread") > 800, (
    "keep this fixture's http_thread-to-Address-already-in-use gap close to the real ~840-char "
    "distance actually captured live -- a shorter fixture would not have caught the real regression"
)


def test_skips_the_real_live_benign_httpd_exception_and_finds_the_real_error():
    real_traceback = (
        "Traceback (most recent call last):\n"
        '  File "models/models.py", line 5, in _compute_age\n'
        "    x = 1 / 0\n"
        "ZeroDivisionError: division by zero\n"
    )
    combined = _REAL_BENIGN_HTTPD_BLOCK + ("module loading noise... " * 200) + real_traceback
    excerpt = _extract_error_excerpt(combined)
    assert "ZeroDivisionError" in excerpt, "the real error past the benign block must be surfaced"
    assert "Address already in use" not in excerpt or "ZeroDivisionError" in excerpt, (
        "the window must land on the real error, not stay anchored on the benign one"
    )
    print("PASS: the real, confirmed live benign httpd port-bind exception is skipped, "
          "surfacing the actual failure that follows it")


def test_falls_back_to_the_benign_block_when_it_is_the_only_marker_at_all():
    combined = _REAL_BENIGN_HTTPD_BLOCK + ("no other traceback anywhere in this log " * 50)
    excerpt = _extract_error_excerpt(combined)
    assert "Address already in use" in excerpt, (
        "when the benign block is genuinely the ONLY marker in the whole log, it must still be "
        "shown (better than nothing) rather than silently returning empty context"
    )
    print("PASS: falls back to the benign block itself when no other real marker exists at all")


def test_never_skips_a_genuinely_different_real_exception():
    real_traceback = (
        "Traceback (most recent call last):\n"
        '  File "models/models.py", line 3, in <module>\n'
        "    import nonexistent_module\n"
        "ModuleNotFoundError: No module named 'nonexistent_module'\n"
    )
    combined = real_traceback + ("trailing noise " * 100)
    excerpt = _extract_error_excerpt(combined)
    assert "ModuleNotFoundError" in excerpt, "a genuinely different real exception must never be skipped"
    print("PASS: a genuinely different, real exception (not the benign httpd shape) is never skipped")


if __name__ == "__main__":
    test_skips_the_real_live_benign_httpd_exception_and_finds_the_real_error()
    test_falls_back_to_the_benign_block_when_it_is_the_only_marker_at_all()
    test_never_skips_a_genuinely_different_real_exception()
    print("\nALL ERROR-EXCERPT BENIGN-HTTPD-SKIP TESTS PASSED")
