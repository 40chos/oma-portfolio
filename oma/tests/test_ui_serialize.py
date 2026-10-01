"""Phase 16: small, real mapping tests for manager/ui_serialize.py --
uses real-shaped detail dicts (matching what CodeReviewSpecialist and
TestingQASpecialist genuinely return), not invented shapes.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from manager.ui_serialize import serialize_code_review_findings, serialize_testing_qa_checks


def test_serialize_code_review_findings():
    detail = {
        "findings": [
            {"location": "models/models.py:12", "severity": "blocking", "explanation": "Missing dependency."},
            {"location": "views/views.xml:3", "severity": "minor", "explanation": "Cosmetic label wording."},
        ]
    }
    result = serialize_code_review_findings(detail)
    assert result == [
        {"text": "Missing dependency.", "blocking": True},
        {"text": "Cosmetic label wording.", "blocking": False},
    ]
    print("PASS: serialize_code_review_findings() maps real {location,severity,explanation} -> {text,blocking}")


def test_serialize_code_review_findings_empty():
    assert serialize_code_review_findings({"findings": []}) == []
    print("PASS: no findings -> empty list, not a fabricated placeholder")


def test_serialize_testing_qa_checks():
    detail = {
        "reproduction_confirmed": True,
        "uncovered_paths": [],
        "coverage_diff": "...",
        "spot_check_mismatch": False,
        "claimed_uncovered_paths": [],
    }
    result = serialize_testing_qa_checks(detail)
    assert {"text": "Reproduction confirmed", "pass": True} in result
    assert {"text": "Spot-check matches the self-report", "pass": True} in result
    print("PASS: serialize_testing_qa_checks() maps a real, clean VerificationResult correctly")


def test_serialize_testing_qa_checks_with_failures():
    detail = {
        "reproduction_confirmed": False,
        "uncovered_paths": ["models/models.py:10", "models/models.py:11"],
        "coverage_diff": "...",
        "spot_check_mismatch": True,
        "claimed_uncovered_paths": [],
    }
    result = serialize_testing_qa_checks(detail)
    assert {"text": "Reproduction FAILED", "pass": False} in result
    assert {"text": "Spot-check found a real, unclaimed coverage gap", "pass": False} in result
    assert {"text": "Uncovered: models/models.py:10", "pass": False} in result
    assert {"text": "Uncovered: models/models.py:11", "pass": False} in result
    print("PASS: serialize_testing_qa_checks() correctly surfaces real failures, including per-path uncovered checks")


if __name__ == "__main__":
    test_serialize_code_review_findings()
    test_serialize_code_review_findings_empty()
    test_serialize_testing_qa_checks()
    test_serialize_testing_qa_checks_with_failures()
    print("\nALL UI SERIALIZE TESTS PASSED")
