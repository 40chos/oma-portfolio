"""Phase 29 (2026-07-30): unit test for a real, confirmed live bug --
the school_student task (9500+ accumulated rounds) hit a real
asyncio.TimeoutError against a genuinely healthy backend, because
call_structured() had no way to request more than the gateway's
default 90s timeout regardless of how large its own prompt was.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import asyncio

from pydantic import BaseModel

from infra.structured_output import call_structured


class _Schema(BaseModel):
    x: int


class _FakeClient:
    def __init__(self):
        self.last_kwargs: dict | None = None

    async def generate(self, **kwargs):
        self.last_kwargs = kwargs
        return '{"x": 1}'


def test_timeout_sec_is_passed_through_when_given():
    client = _FakeClient()
    asyncio.run(call_structured(client, "model", "prompt", _Schema, timeout_sec=300.0))
    assert client.last_kwargs.get("timeout_sec") == 300.0
    print("PASS: an explicit timeout_sec is passed through to client.generate()")


def test_no_timeout_sec_key_when_not_given_preserves_prior_behavior():
    client = _FakeClient()
    asyncio.run(call_structured(client, "model", "prompt", _Schema))
    assert "timeout_sec" not in client.last_kwargs, (
        "omitting timeout_sec must not pass timeout_sec=None through -- every existing call site "
        "must keep relying on generate()'s own default exactly as before"
    )
    print("PASS: omitting timeout_sec preserves every existing call site's exact prior behavior")


if __name__ == "__main__":
    test_timeout_sec_is_passed_through_when_given()
    test_no_timeout_sec_key_when_not_given_preserves_prior_behavior()
    print("\nALL STRUCTURED-OUTPUT TIMEOUT-PASSTHROUGH TESTS PASSED")
