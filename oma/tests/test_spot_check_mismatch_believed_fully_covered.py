"""Real, confirmed structural bug found live (2026-08-06, fix-pass task 005):
compute_spot_check_mismatch() (tools_odoo/spot_check.py) used to diff
claimed_uncovered_paths (free-text prose, per _self_report_coverage()'s own prompt --
"List any such paths as best you can describe them, EVEN APPROXIMATELY") against
real_uncovered_paths ("file.py:line" strings from Python's own coverage tool) as a literal
set difference -- two formats that can never intersect, so this function returned a
"mismatch" (failing the round) whenever real_uncovered_paths was non-empty AT ALL,
regardless of what the self-report honestly said. Confirmed live: task 005 (a correct
@api.onchange fix, no blocking Code-Review findings, self-report correctly and honestly said
believed_fully_covered=False with an accurate prose explanation) still failed on this check.

Fixed: a self-report that honestly discloses incomplete coverage (believed_fully_covered=False)
is never a mismatch on its own -- only a report that FALSELY claims full coverage
(believed_fully_covered=True) while reality found a real gap is a genuine mismatch.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tools_odoo.spot_check import compute_spot_check_mismatch


def test_no_mismatch_when_nothing_is_really_uncovered():
    assert compute_spot_check_mismatch([], [], believed_fully_covered=True) is False
    print("PASS: no real gap at all -- never a mismatch, regardless of the claim")


def test_no_mismatch_when_self_report_honestly_discloses_incomplete_coverage():
    """The exact task 005 shape: real coverage found the onchange method's body uncovered
    (expected -- Odoo install never triggers @api.onchange), and the self-report HONESTLY said
    believed_fully_covered=False with a prose explanation -- must never be flagged as a mismatch
    just because the prose format doesn't literally match "file:line" strings.
    """
    real_uncovered = ["models/models.py:8", "models/models.py:9", "models/models.py:11"]
    claimed = [
        "The onchange logic that updates the 'Assigned to' field when a project is selected "
        "on the fieldjob form is not executed during installation."
    ]
    assert compute_spot_check_mismatch(claimed, real_uncovered, believed_fully_covered=False) is False
    print("PASS: an honest 'not fully covered' self-report is never penalized just because its "
          "free-text description doesn't literally match the real file:line strings")


def test_mismatch_when_self_report_falsely_claims_full_coverage():
    """The genuine, meaningful mismatch this check exists to catch: the self-report claims
    believed_fully_covered=True (nothing is missing) but reality found a real gap -- an
    overconfident, wrong claim.
    """
    real_uncovered = ["models/models.py:8"]
    assert compute_spot_check_mismatch([], real_uncovered, believed_fully_covered=True) is True
    print("PASS: a false 'fully covered' claim contradicted by real coverage is still correctly "
          "flagged as a mismatch")


def test_no_mismatch_when_claim_is_empty_but_honest_about_it():
    """claimed_uncovered_paths itself can be an empty list while believed_fully_covered is
    correctly False (a specialist that knows coverage is incomplete but couldn't articulate
    specific paths) -- still never a mismatch, since the honesty signal is believed_fully_covered,
    not the (structurally incomparable) claimed_uncovered_paths list.
    """
    real_uncovered = ["models/models.py:8"]
    assert compute_spot_check_mismatch([], real_uncovered, believed_fully_covered=False) is False
    print("PASS: an empty claimed-paths list with an honest believed_fully_covered=False is "
          "still never flagged")


def test_default_believed_fully_covered_is_false_backwards_compatible():
    """The new parameter defaults to False (never claiming full coverage) -- any caller that
    doesn't pass it explicitly gets the SAFEST behavior (never a false mismatch), not the
    old broken always-mismatch-if-any-gap-exists behavior.
    """
    real_uncovered = ["models/models.py:8"]
    assert compute_spot_check_mismatch([], real_uncovered) is False
    print("PASS: the default parameter value is backwards-safe, never re-introducing the old bug")


if __name__ == "__main__":
    test_no_mismatch_when_nothing_is_really_uncovered()
    test_no_mismatch_when_self_report_honestly_discloses_incomplete_coverage()
    test_mismatch_when_self_report_falsely_claims_full_coverage()
    test_no_mismatch_when_claim_is_empty_but_honest_about_it()
    test_default_believed_fully_covered_is_false_backwards_compatible()
    print("\nALL SPOT-CHECK MISMATCH TESTS PASSED")
