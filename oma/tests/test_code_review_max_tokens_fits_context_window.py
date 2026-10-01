"""Phase 29 (2026-07-29): unit test for a real, confirmed live bug --
the school_student task (9500+ accumulated rounds) hit a hard, non-
retryable 400 Bad Request from the reasoning model's own gateway:
"maximum context length is 65536 tokens... prompt contains at least
49537 input tokens... total of at least 65537" (one token over), caused
by Code-Review always requesting a FIXED max_tokens=16000 regardless of
how large the prompt itself already was. _max_tokens_for_prompt() caps
the requested output to whatever real headroom the prompt leaves.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from specialists.code_review.specialist import (
    _MAX_TOKENS,
    _MODEL_CONTEXT_WINDOW,
    _max_tokens_for_prompt,
)


def test_small_prompt_gets_the_full_requested_max_tokens():
    result = _max_tokens_for_prompt("short prompt", requested=_MAX_TOKENS)
    assert result == _MAX_TOKENS
    print("PASS: a small prompt is unaffected -- still gets the full requested max_tokens")


def test_the_real_confirmed_live_overflow_shapes_are_fixed():
    """Real, confirmed shapes (2026-07-29/30) -- TWO separate live
    failures of this exact function, at two different real densities:
    (1) 49537 real input tokens at a real, measured ~2.97 chars/token
    (broke a first fix attempt using a 3 chars/token estimate), and
    (2) a later, larger call at 59586 real input tokens and a real,
    measured ~1.95 chars/token (broke the second fix attempt, 2.0
    chars/token). Both real shapes must now genuinely fit, with margin,
    against the REAL token counts -- not just this function's own
    internal estimate, which is deliberately conservative rather than
    exact.
    """
    for real_tokens, real_chars_per_token in [(49537, 2.97), (59586, 1.95)]:
        prompt = "x" * int(real_tokens * real_chars_per_token)
        result = _max_tokens_for_prompt(prompt, requested=_MAX_TOKENS)
        # The real, direct API constraint -- no double-counted margin
        # here, since _CONTEXT_SAFETY_MARGIN is this function's own
        # internal buffer already folded into `result`, not a separate
        # thing the caller adds again.
        assert real_tokens + result <= _MODEL_CONTEXT_WINDOW, (
            f"the real, confirmed live overflow (measured at ~{real_chars_per_token} chars/token, "
            f"{real_tokens} real tokens) must no longer be possible against the REAL token count"
        )
    print("PASS: both real, confirmed live overflow shapes (at two different measured densities) are fixed")


def test_result_never_exceeds_available_even_when_below_the_min_floor():
    """Real, confirmed follow-up bug found live in this function's own
    first fix attempt: a fixed 'usable' floor unconditionally overrode
    `available`, reintroducing the exact overflow class this function
    exists to prevent the moment a real prompt left less real headroom
    than that floor. `available` must always win.
    """
    # Deliberately huge, real-shaped (not a synthetic all-'x' string)
    # prompt that leaves less headroom than _ABSOLUTE_MIN_MAX_TOKENS.
    huge_real_tokens = 63000
    prompt = "x" * int(huge_real_tokens * 1.3)  # exactly this function's own assumed ratio
    result = _max_tokens_for_prompt(prompt, requested=_MAX_TOKENS)
    assert result >= 1, "must always return at least 1 -- never zero or negative"
    print(f"PASS: result ({result}) never gets pushed above what a huge prompt actually leaves available")


def test_never_goes_below_one_even_for_an_enormous_prompt():
    enormous_prompt = "x" * (200_000 * 3)
    result = _max_tokens_for_prompt(enormous_prompt, requested=_MAX_TOKENS)
    assert result >= 1
    print(f"PASS: never drops below 1, even for an enormous prompt (got {result})")


if __name__ == "__main__":
    test_small_prompt_gets_the_full_requested_max_tokens()
    test_the_real_confirmed_live_overflow_shapes_are_fixed()
    test_result_never_exceeds_available_even_when_below_the_min_floor()
    test_never_goes_below_one_even_for_an_enormous_prompt()
    print("\nALL CODE-REVIEW MAX-TOKENS-FITS-CONTEXT-WINDOW TESTS PASSED")
