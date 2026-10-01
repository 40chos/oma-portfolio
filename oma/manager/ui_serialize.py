"""Phase 16 (mockup's Agents panel): small, real mappings from a
specialist's own existing, structured output into the exact shape the
UI expects -- never a redesign of the specialist's own real data
(CodeReviewSpecialist's findings, Phase 10; TestingQASpecialist's
reproduction/spot-check result, Phase 11), just a serialization step.
"""

from __future__ import annotations


def serialize_code_review_findings(detail: dict) -> list[dict]:
    """CodeReviewSpecialist's real detail['findings'] (each a real
    {location, severity, explanation} from Phase 10) -> the mockup's
    own {text, blocking} shape.
    """
    return [
        {"text": f.get("explanation", ""), "blocking": f.get("severity") == "blocking"}
        for f in detail.get("findings", [])
    ]


def serialize_testing_qa_checks(detail: dict) -> list[dict]:
    """TestingQASpecialist's real detail (Phase 11's own
    reproduction_confirmed/spot_check_mismatch/uncovered_paths) -> the
    mockup's own {text, pass} checks list. Every check here maps
    directly to a real, already-computed field -- nothing invented.
    """
    checks: list[dict] = []
    if "reproduction_confirmed" in detail:
        checks.append({
            "text": "Reproduction confirmed" if detail["reproduction_confirmed"] else "Reproduction FAILED",
            "pass": bool(detail["reproduction_confirmed"]),
        })
    if "spot_check_mismatch" in detail:
        checks.append({
            "text": "Spot-check matches the self-report" if not detail["spot_check_mismatch"]
                    else "Spot-check found a real, unclaimed coverage gap",
            "pass": not detail["spot_check_mismatch"],
        })
    for path in detail.get("uncovered_paths") or []:
        checks.append({"text": f"Uncovered: {path}", "pass": False})
    return checks
