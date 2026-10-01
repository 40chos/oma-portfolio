"""Phase 30, P4 (Phase A/B, §5/§6) recheck (2026-07-30): unit test for a
real, confirmed bug in _check_draft_is_relevant_and_non_trivial() --
found live inspecting the real output of a full 141-cluster
batch_resolve_compound_backlog.py run. The existing "non-trivial body"
filter excluded a bare docstring Expr but not a bare `pass` or a bare
`import`, so a function whose ENTIRE real body was `pass` (or an unused
import followed by `pass`) survived as "non-trivial" and reached
contracts/pending_validators/ as a "passed" draft. Two real, confirmed
instances from that exact run:
  1. `def action_import(self): pass` -- literally reproduces the BUG
     PATTERN the cluster was about, not a detector for it.
  2. A real docstring, an unused `import re`, several lines of comments
     (comments vanish entirely from the AST -- not real signal), and a
     single trailing `pass`.
Both are used here verbatim (not paraphrased) as the regression fixture.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from scripts.draft_validator_from_cluster import _check_draft_is_relevant_and_non_trivial

_REAL_BAD_DRAFT_1 = "def action_import(self):\n    pass\n"

_REAL_BAD_DRAFT_2 = (
    "def _validate_sensitive_fields_encrypted(generated):\n"
    "    \"\"\"Detects sensitive fields stored without encryption.\"\"\"\n"
    "    import re\n"
    "    # scan for lines containing `fields.` and check for sensitive keywords\n"
    "    # this is tricky so I'll just note the approach here\n"
    "    pass\n"
)

_REAL_GOOD_DRAFT = (
    "def _validate_security_csv_not_empty(generated):\n"
    "    \"\"\"Rejects a security_csv with only a header row.\"\"\"\n"
    "    import csv, io\n"
    "    rows = list(csv.reader(io.StringIO(generated.security_csv)))\n"
    "    if len(rows) <= 1:\n"
    "        raise ValueError('security_csv contains only a header row with no data entries.')\n"
)


def test_rejects_a_bare_pass_body_that_reproduces_the_bug_pattern_itself():
    ok, msg = _check_draft_is_relevant_and_non_trivial(_REAL_BAD_DRAFT_1, {"sig": "action_import is a placeholder"})
    assert ok is False
    # Rejected by the signature check now (no 'generated' param, checked
    # first) rather than the body-content check -- either is a correct,
    # real rejection of this exact real bad draft; the signature check
    # simply catches it earlier since this draft ALSO has that defect.
    assert "no 'generated' parameter" in msg or "empty/trivial body" in msg
    print("PASS: a bare `pass` body with no 'generated' parameter is correctly rejected")


def test_rejects_an_unused_import_plus_trailing_pass():
    ok, msg = _check_draft_is_relevant_and_non_trivial(
        _REAL_BAD_DRAFT_2, {"sig": "Sensitive OAuth2 access token stored in plaintext"},
    )
    assert ok is False
    assert "empty/trivial body" in msg
    print("PASS: an unused import followed by a trailing pass is correctly rejected as trivial")


def test_still_accepts_a_genuinely_substantive_draft():
    ok, msg = _check_draft_is_relevant_and_non_trivial(_REAL_GOOD_DRAFT, {"sig": "security csv contains only a header row"})
    assert ok is True, f"a real, substantive validator must still pass: {msg}"
    print("PASS: a genuinely substantive draft (real logic, a real raise) is unaffected by the fix")


# Real, confirmed shapes found live (2026-07-30) during a human-grade,
# line-by-line review of a full batch run's own real drafted output
# (per the project owner's own explicit request) -- two drafts were literal
# TEMPLATES of correct overridden-method code, not validators, each
# with a real ast.Return statement (so the pass/import filter above
# never caught them) but no 'generated' parameter at all.
_REAL_BAD_DRAFT_3_WRONG_SIGNATURE_CREATE = "def create(self, vals_list):\n    return super().create(vals_list)\n"
_REAL_BAD_DRAFT_4_WRONG_SIGNATURE_WRITE = "def write(self, vals):\n    return super().write(vals)\n"


def test_rejects_a_real_looking_method_template_with_no_generated_parameter():
    ok, msg = _check_draft_is_relevant_and_non_trivial(
        _REAL_BAD_DRAFT_3_WRONG_SIGNATURE_CREATE, {"sig": "create method does not call super"},
    )
    assert ok is False
    assert "no 'generated' parameter" in msg
    print("PASS: a real-looking method template (real Return statement, wrong signature) is rejected")


def test_rejects_a_second_real_wrong_signature_template():
    ok, msg = _check_draft_is_relevant_and_non_trivial(
        _REAL_BAD_DRAFT_4_WRONG_SIGNATURE_WRITE, {"sig": "super write vals call is placed"},
    )
    assert ok is False
    assert "no 'generated' parameter" in msg
    print("PASS: the second real wrong-signature template is also rejected")


if __name__ == "__main__":
    test_rejects_a_bare_pass_body_that_reproduces_the_bug_pattern_itself()
    test_rejects_an_unused_import_plus_trailing_pass()
    test_still_accepts_a_genuinely_substantive_draft()
    test_rejects_a_real_looking_method_template_with_no_generated_parameter()
    test_rejects_a_second_real_wrong_signature_template()
    print("\nALL DRAFT-VALIDATOR RELEVANCE-CHECK-REJECTS-EMPTY-STUBS TESTS PASSED")
