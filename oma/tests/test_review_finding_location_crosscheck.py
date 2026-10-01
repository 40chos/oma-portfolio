"""P12 Tier A item 14 (docs/planning/PHASE30_ARCH_SYNTHESIS_CRITIQUE_FINAL_2026-07-30.md §4):
tests for ReviewFinding.severity's new Literal constraint and the new
_filter_findings_with_unmatched_location() cross-check -- ReviewFinding.location was
previously never checked against the diffed files at all. Pure, deterministic, zero live
calls.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pydantic import ValidationError

from specialists.code_review.specialist import ReviewFinding, _filter_findings_with_unmatched_location


def _finding(location: str, explanation: str = "x", severity: str = "blocking") -> ReviewFinding:
    return ReviewFinding(location=location, explanation=explanation, severity=severity)


# --- ReviewFinding.severity Literal constraint ------------------------------

def test_all_four_real_prompt_values_are_accepted():
    for sev in ("blocking", "major", "minor", "info"):
        ReviewFinding(location="x", severity=sev, explanation="x")
    print("PASS: all four real values the generation prompt asks for (blocking/major/minor/info) are accepted")


def test_an_invented_severity_value_is_rejected():
    try:
        ReviewFinding(location="x", severity="critical", explanation="x")
    except ValidationError:
        pass
    else:
        raise AssertionError("expected a ValidationError for an invented severity value")
    print("PASS: a severity value outside the real four-value schema is rejected, not silently accepted")


# --- _filter_findings_with_unmatched_location() -----------------------------

def test_finding_naming_a_real_reviewed_file_is_untouched():
    files = {"models/models.py": "content", "security/ir.model.access.csv": "content"}
    findings = [_finding("models/models.py:42")]
    filtered = _filter_findings_with_unmatched_location(findings, files)
    assert filtered[0].severity == "blocking"
    print("PASS: a finding whose location matches a real reviewed file (by basename) is left untouched")


def test_finding_naming_a_file_never_reviewed_is_downgraded():
    files = {"models/models.py": "content"}
    findings = [_finding("views/nonexistent_view.xml:10")]
    filtered = _filter_findings_with_unmatched_location(findings, files)
    assert filtered[0].severity == "info"
    assert "does not match any file actually reviewed" in filtered[0].explanation
    print("PASS: a finding naming a file that was never part of the real diff is downgraded")


def test_purely_descriptive_location_is_left_untouched():
    files = {"models/models.py": "content"}
    findings = [_finding("overall module structure")]
    filtered = _filter_findings_with_unmatched_location(findings, files)
    assert filtered[0].severity == "blocking"
    print("PASS: a purely descriptive, non-path location (no '.' token) is left untouched -- nothing concrete to cross-check")


def test_non_blocking_finding_never_touched():
    files = {"models/models.py": "content"}
    findings = [_finding("views/nonexistent_view.xml:10", severity="minor")]
    filtered = _filter_findings_with_unmatched_location(findings, files)
    assert filtered[0].severity == "minor"
    print("PASS: only 'blocking' findings are ever downgraded, minor/info pass through unchanged")


def test_basename_match_works_regardless_of_directory_prefix():
    files = {"/mnt/extra-addons/oma_x/models/models.py": "content"}
    findings = [_finding("models/models.py:5")]
    filtered = _filter_findings_with_unmatched_location(findings, files)
    assert filtered[0].severity == "blocking"
    print("PASS: basename matching works even when the real files dict uses full container paths")


if __name__ == "__main__":
    test_all_four_real_prompt_values_are_accepted()
    test_an_invented_severity_value_is_rejected()
    test_finding_naming_a_real_reviewed_file_is_untouched()
    test_finding_naming_a_file_never_reviewed_is_downgraded()
    test_purely_descriptive_location_is_left_untouched()
    test_non_blocking_finding_never_touched()
    test_basename_match_works_regardless_of_directory_prefix()
    print("\nALL REVIEW-FINDING LOCATION-CROSSCHECK TESTS PASSED")
