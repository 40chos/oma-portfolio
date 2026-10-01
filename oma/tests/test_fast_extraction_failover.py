"""Real infra incident (2026-07-28): GPU Worker 03 (the fast-extraction
host, `gpu_worker_03_fast_extraction`) went into sustained 429 Too Many
Requests -- confirmed live over 15+ minutes, external load unrelated to
this codebase. `OMA_FAST_EXTRACTION_FAILOVER_TO_REASONING` (unset by
default, preserving today's exact routing) reroutes fast-extraction
calls to GPU Worker 02's shared reasoning model instead, so the
pipeline can keep working off a healthy host during the outage.
"""

import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import infra.gateway_client as gc


def test_default_routing_unchanged_when_failover_env_unset():
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop(gc._FAST_EXTRACTION_FAILOVER_ENV, None)
        assert gc.backend_for_model("qwen3-9b-fast-extraction") == gc.BACKEND_FAST_EXTRACTION
        assert gc._wire_model_id("qwen3-9b-fast-extraction") == "Qwen3-9B-int4-32k"
    print("PASS: default (unset) routing is unchanged -- fast-extraction still targets Worker 03")


def test_failover_reroutes_to_reasoning_backend_and_correct_wire_model():
    with patch.dict(os.environ, {gc._FAST_EXTRACTION_FAILOVER_ENV: "1"}):
        assert gc.backend_for_model("qwen3-9b-fast-extraction") == gc.BACKEND_REASONING
        assert gc._wire_model_id("qwen3-9b-fast-extraction") == "JA-GPU2-27B-INT4-64K", (
            "must send Worker 02's own real wire model id, not Worker 03's -- sending the wrong "
            "one to the wrong host would fail outright"
        )
    print("PASS: failover reroutes fast-extraction calls to Worker 02's reasoning backend, with "
          "the correct wire model id for that host")


def test_failover_never_affects_coder_or_reasoning_model_routing():
    """The failover must be scoped ONLY to fast-extraction -- coder and
    the ordinary reasoning model routing must be completely untouched."""
    with patch.dict(os.environ, {gc._FAST_EXTRACTION_FAILOVER_ENV: "1"}):
        assert gc.backend_for_model("qwen3-coder-30b-a3b") == gc.BACKEND_CODER
        assert gc.backend_for_model("qwen3.6-27b") == gc.BACKEND_REASONING
        assert gc._wire_model_id("qwen3.6-27b") == "JA-GPU2-27B-INT4-64K"
    print("PASS: failover is scoped only to fast-extraction -- coder/reasoning routing untouched")


def test_failover_env_accepts_common_truthy_spellings():
    for value in ("1", "true", "True", "yes", "YES"):
        with patch.dict(os.environ, {gc._FAST_EXTRACTION_FAILOVER_ENV: value}):
            assert gc._fast_extraction_failover_active(), f"expected {value!r} to be treated as truthy"
    for value in ("0", "false", "", "no"):
        with patch.dict(os.environ, {gc._FAST_EXTRACTION_FAILOVER_ENV: value}):
            assert not gc._fast_extraction_failover_active(), f"expected {value!r} to be treated as falsy"
    print("PASS: the env var accepts common truthy/falsy spellings correctly")


if __name__ == "__main__":
    test_default_routing_unchanged_when_failover_env_unset()
    test_failover_reroutes_to_reasoning_backend_and_correct_wire_model()
    test_failover_never_affects_coder_or_reasoning_model_routing()
    test_failover_env_accepts_common_truthy_spellings()
    print("\nALL FAST-EXTRACTION FAILOVER TESTS PASSED")
