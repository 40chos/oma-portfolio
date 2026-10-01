"""P12 Tier B/C item 28 (docs/planning/PHASE30_ARCH_SYNTHESIS_CRITIQUE_FINAL_2026-07-30.md §4,
A Finding 27): tests for fold_verification_notes() -- the structure-aware sibling of
fold_specialist_result(), for VerificationResult.notes specifically. Real, confirmed gap: the
plain first-30%/last-30% truncation could silently drop a real traceback/file:line reference
sitting in the dropped middle, leaving classify_root_cause() working from a summary with a hole
exactly where the actionable diagnostic detail would be. Pure, deterministic, zero live calls.

Phase 30 §26 follow-up (2026-08-04): fold_verification_notes() now uses its own, much larger
_VERIFICATION_NOTES_CHAR_LIMIT (50,000 chars), decoupled from fold_specialist_result()'s
_SPECIALIST_RESULT_CHAR_LIMIT (5,000, unchanged) -- a real, confirmed gap where the "verbatim
failure text" §26 item 3 promises Build's next-round prompt was still routinely truncated at a
size ordinary install tracebacks/Code-Review findings can exceed. Fixtures below are sized against
_VERIFICATION_NOTES_CHAR_LIMIT specifically now, matching the function actually under test.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from manager.tools import (
    _SPECIALIST_RESULT_CHAR_LIMIT,
    _VERIFICATION_NOTES_CHAR_LIMIT,
    fold_specialist_result,
    fold_verification_notes,
)


def test_short_text_is_never_touched():
    text = "A short summary."
    assert fold_verification_notes(text) == text
    print("PASS: text within the char budget is returned unchanged")


def test_text_under_the_new_larger_limit_is_never_truncated():
    """Real, confirmed gap this closes: text that WOULD have been truncated under the old,
    shared 5,000-char limit (a realistic size for an install traceback or Code-Review finding)
    must now survive completely untouched, since fold_verification_notes() has its own much
    larger limit."""
    text = "A" * (_SPECIALIST_RESULT_CHAR_LIMIT * 3)  # well over the OLD shared limit
    assert len(text) < _VERIFICATION_NOTES_CHAR_LIMIT
    assert fold_verification_notes(text) == text
    print("PASS: text that would have been truncated under the old shared limit now survives fully")


def test_long_text_with_no_diagnostic_content_in_the_middle_falls_back_to_the_plain_shape():
    """With no diagnostic-shaped content anywhere, once text genuinely exceeds
    fold_verification_notes()'s own (larger) limit, it falls back to the same plain
    head/[...]/tail shape fold_specialist_result() uses -- just at its own, different size."""
    text = "A" * (_VERIFICATION_NOTES_CHAR_LIMIT * 2)
    keep_each_side = int(_VERIFICATION_NOTES_CHAR_LIMIT * 0.3)
    result = fold_verification_notes(text)
    assert result.startswith("A" * keep_each_side)
    assert result.endswith("A" * keep_each_side)
    assert "characters truncated" in result
    print("PASS: with no diagnostic-shaped content, exceeding the limit still uses the plain fold shape")


def test_a_traceback_line_in_the_dropped_middle_is_preserved():
    keep = int(_VERIFICATION_NOTES_CHAR_LIMIT * 0.3)
    head_filler = "x" * keep
    tail_filler = "y" * keep
    diagnostic_line = 'File "specialists/build/specialist.py", line 4021, in _validate_xml'
    middle_filler = "z" * int(_VERIFICATION_NOTES_CHAR_LIMIT * 0.2)  # large enough to actually exceed the limit
    text = f"{head_filler}\n{middle_filler}\n{diagnostic_line}\n{middle_filler}\n{tail_filler}"
    result = fold_verification_notes(text)
    assert diagnostic_line in result, "a real traceback line sitting in the truncated middle must survive"
    print("PASS: a File \"...\", line N traceback line in the dropped middle is preserved verbatim")


def test_a_file_colon_line_reference_in_the_dropped_middle_is_preserved():
    keep = int(_VERIFICATION_NOTES_CHAR_LIMIT * 0.3)
    head_filler = "x" * keep
    tail_filler = "y" * keep
    diagnostic_line = "specialists/build/specialist.py:4021"
    middle_filler = "z" * int(_VERIFICATION_NOTES_CHAR_LIMIT * 0.2)  # large enough to actually exceed the limit
    text = f"{head_filler}\n{middle_filler}\n{diagnostic_line}\n{middle_filler}\n{tail_filler}"
    result = fold_verification_notes(text)
    assert diagnostic_line in result
    print("PASS: a bare path.py:line reference in the dropped middle is preserved verbatim")


def test_an_exception_line_in_the_dropped_middle_is_preserved():
    keep = int(_VERIFICATION_NOTES_CHAR_LIMIT * 0.3)
    head_filler = "x" * keep
    tail_filler = "y" * keep
    diagnostic_line = "ValueError: Wrong value for ir.rule.groups: 1"
    middle_filler = "z" * int(_VERIFICATION_NOTES_CHAR_LIMIT * 0.2)  # large enough to actually exceed the limit
    text = f"{head_filler}\n{middle_filler}\n{diagnostic_line}\n{middle_filler}\n{tail_filler}"
    result = fold_verification_notes(text)
    assert diagnostic_line in result
    print("PASS: a genuine exception line (ValueError: ...) in the dropped middle is preserved")


def test_head_and_tail_content_still_present_when_diagnostics_are_preserved():
    keep = int(_VERIFICATION_NOTES_CHAR_LIMIT * 0.3)
    head_filler = "HEAD_MARKER_" + "x" * keep
    tail_filler = "y" * keep + "_TAIL_MARKER"
    diagnostic_line = "TypeError: something broke"
    middle_filler = "z" * int(_VERIFICATION_NOTES_CHAR_LIMIT * 0.2)  # large enough to actually exceed the limit
    text = f"{head_filler}\n{middle_filler}\n{diagnostic_line}\n{middle_filler}\n{tail_filler}"
    result = fold_verification_notes(text)
    assert "HEAD_MARKER_" in result
    assert "_TAIL_MARKER" in result
    print("PASS: the original head/tail content is still present alongside the preserved diagnostics")


def test_duplicate_diagnostic_lines_are_not_repeated():
    keep = int(_VERIFICATION_NOTES_CHAR_LIMIT * 0.3)
    head_filler = "x" * keep
    tail_filler = "y" * keep
    diagnostic_line = "ValueError: same error twice"
    # Large enough that head + middle_fillers + tail genuinely exceeds
    # _VERIFICATION_NOTES_CHAR_LIMIT and the fold actually triggers (unlike a small, fixed
    # filler that was correctly sized for the old, much smaller shared limit).
    middle_filler = "z" * int(_VERIFICATION_NOTES_CHAR_LIMIT * 0.2)
    text = f"{head_filler}\n{middle_filler}\n{diagnostic_line}\n{middle_filler}\n{diagnostic_line}\n{tail_filler}"
    assert len(text) > _VERIFICATION_NOTES_CHAR_LIMIT
    result = fold_verification_notes(text)
    assert result.count(diagnostic_line) == 1
    print("PASS: an identical diagnostic line appearing twice in the dropped middle is only preserved once")


def test_fold_specialist_result_keeps_its_own_smaller_unchanged_limit():
    """fold_specialist_result() (general specialist summaries) is deliberately NOT part of this
    fix -- confirms it still truncates at the original, smaller _SPECIALIST_RESULT_CHAR_LIMIT."""
    text = "A" * (_SPECIALIST_RESULT_CHAR_LIMIT * 2)
    result = fold_specialist_result(text)
    assert "characters truncated" in result
    print("PASS: fold_specialist_result() still uses its own original, smaller limit, unchanged")


if __name__ == "__main__":
    test_short_text_is_never_touched()
    test_text_under_the_new_larger_limit_is_never_truncated()
    test_long_text_with_no_diagnostic_content_in_the_middle_falls_back_to_the_plain_shape()
    test_a_traceback_line_in_the_dropped_middle_is_preserved()
    test_a_file_colon_line_reference_in_the_dropped_middle_is_preserved()
    test_an_exception_line_in_the_dropped_middle_is_preserved()
    test_head_and_tail_content_still_present_when_diagnostics_are_preserved()
    test_duplicate_diagnostic_lines_are_not_repeated()
    test_fold_specialist_result_keeps_its_own_smaller_unchanged_limit()
    print("\nALL FOLD-VERIFICATION-NOTES TESTS PASSED")
