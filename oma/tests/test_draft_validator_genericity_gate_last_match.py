"""Phase 29 (2026-07-29): unit test for a real, confirmed bug in
genericity_gate() -- found live while investigating whether a smaller
model (Phi-4-mini, GPU Worker 4) could safely replace the 27B model for
this check. A naive first-match regex search for "ANSWER: PASS/FAIL"
grabbed a SPURIOUS match from the model echoing the prompt's own
instructions back verbatim early in its reasoning ("Output Format:
Must end with exactly `ANSWER: PASS` or `ANSWER: FAIL`") -- producing a
false PASS for a validator that obviously hardcoded 'school.student'/
'model_school_student', while the model's own actual, later conclusion
correctly said FAIL. Confirmed live against the real 27B reasoning
model itself (not a Phi-4-mini-specific issue) -- this bug could have
let a real hardcoded, task-specific validator slip past the gate any
time a model's own reasoning happened to quote the prompt's format
instructions before its real conclusion.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import scripts.draft_validator_from_cluster as draft_module

_REAL_ECHOED_INSTRUCTIONS_THEN_REAL_ANSWER = """\
Here's a thinking process:

1.  **Analyze User Input:**
   - **Output Format:** Must end with exactly `ANSWER: PASS` or `ANSWER: FAIL`. If FAIL, explain
     what was hardcoded on the line before the answer.

2.  **Examine the Code for Hardcoded Literals:**
   - `"school.student"` and `"model_school_student"` are specific, task-specific literal identifiers.

3.  **Evaluate Against Criteria:**
   - Therefore, it fails the requirement.

The code hardcodes the specific Odoo model name "school.student" and the security record ID
"model_school_student" directly in its conditional checks.
ANSWER: FAIL
"""


def test_finds_the_real_final_verdict_not_the_echoed_instruction_text(monkeypatch):
    monkeypatch.setattr(draft_module, "_call_llm", lambda prompt, max_tokens=900: _REAL_ECHOED_INSTRUCTIONS_THEN_REAL_ANSWER)
    passed, response = draft_module.genericity_gate("def _validate_x(generated): pass")
    assert passed is False, (
        "the real final ANSWER: FAIL must win over the earlier echoed 'ANSWER: PASS' instruction text"
    )
    print("PASS: genericity_gate() correctly finds the real final verdict, not an echoed instruction match")


def test_still_correctly_passes_a_genuinely_passing_response(monkeypatch):
    response_text = "Reasoning about a clean, general validator...\nANSWER: PASS\n"
    monkeypatch.setattr(draft_module, "_call_llm", lambda prompt, max_tokens=900: response_text)
    passed, _ = draft_module.genericity_gate("def _validate_x(generated): pass")
    assert passed is True
    print("PASS: a genuinely passing response is still correctly read as PASS")


def test_multiple_answer_occurrences_uses_the_last_one():
    import re
    text = "blah ANSWER: PASS more reasoning changed my mind ANSWER: FAIL final"
    matches = re.findall(r"ANSWER:\s*(PASS|FAIL)", text.upper())
    assert matches[-1] == "FAIL", "sanity check: findall + [-1] picks the last occurrence, not the first"
    print("PASS: the underlying findall+[-1] mechanics behave as expected")


if __name__ == "__main__":
    print("(test_finds_the_real_final_verdict_not_the_echoed_instruction_text and "
          "test_still_correctly_passes_a_genuinely_passing_response require pytest's monkeypatch fixture)")
    test_multiple_answer_occurrences_uses_the_last_one()
