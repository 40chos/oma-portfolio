"""Phase 29 (2026-07-30): unit test for
_scoped_edit_timeout_sec_for_prompt() -- the real, confirmed live root
cause of a genuine asyncio.TimeoutError against a healthy backend on
the school_student task (9500+ accumulated rounds): the scoped-edit
prompt, which embeds the full prior committed content, grew large
enough that it needed more than call_structured()'s own default 90s
timeout to even process, regardless of the backend's real health.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from specialists.build.specialist import _scoped_edit_timeout_sec_for_prompt


def test_small_prompt_gets_the_default_floor():
    assert _scoped_edit_timeout_sec_for_prompt("short prompt") == 90.0
    print("PASS: a small prompt still gets the original 90s default -- no behavior change")


def test_a_large_prompt_scales_up():
    large_prompt = "x" * 180_000
    result = _scoped_edit_timeout_sec_for_prompt(large_prompt)
    assert result > 90.0, "a genuinely large prompt must get more than the bare default"
    assert result == 600.0, "this specific size should hit the 600s ceiling"
    print(f"PASS: a large prompt scales up to {result}s")


def test_never_exceeds_the_600s_ceiling():
    enormous_prompt = "x" * 10_000_000
    result = _scoped_edit_timeout_sec_for_prompt(enormous_prompt)
    assert result == 600.0, "must never exceed the 600s ceiling regardless of prompt size"
    print("PASS: never exceeds the 600s ceiling, even for an enormous prompt")


if __name__ == "__main__":
    test_small_prompt_gets_the_default_floor()
    test_a_large_prompt_scales_up()
    test_never_exceeds_the_600s_ceiling()
    print("\nALL SCOPED-EDIT TIMEOUT-SCALING TESTS PASSED")
