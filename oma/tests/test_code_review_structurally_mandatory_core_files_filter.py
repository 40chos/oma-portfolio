"""Real, confirmed bug found live (2026-08-11, task 18fca388, post_init_hook cleanup): a goal
whose own wording said "the ONLY code this module needs is __manifest__.py and the post_init_hook
function" caused Code-Review to repeatedly flag models/models.py and security/ir.model.access.csv
-- both structurally MANDATORY on every module this pipeline generates
(_scoped_edit_missing_required_files() in specialists/build/specialist.py hard-fails a round that
omits either) -- as "unnecessary complexity" that "should be deleted", byte-identically across 3
consecutive rounds, even after an explicit human resume note directly overriding the complaint.
Fixed deterministically: specialists/code_review/specialist.py's
_filter_hallucinated_findings_blocking_on_structurally_mandatory_core_files() downgrades exactly
this shape of finding.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from specialists.code_review.specialist import (
    ReviewFinding,
    _filter_hallucinated_findings_blocking_on_structurally_mandatory_core_files,
    _filter_hallucinated_findings_claiming_the_standard_module_root_init_is_a_circular_import,
)


def test_downgrades_delete_recommendation_for_models_py():
    findings = [
        ReviewFinding(
            location="models/models.py", severity="blocking",
            explanation=(
                "File imports 'models' but the task goal states 'Do not add any new model, "
                "field, or view', so the import is unnecessary and should be removed."
            ),
        ),
    ]
    result = _filter_hallucinated_findings_blocking_on_structurally_mandatory_core_files(findings)
    assert result[0].severity == "info"
    print("PASS: a 'delete models/models.py' finding is downgraded")


def test_downgrades_delete_recommendation_for_security_csv():
    findings = [
        ReviewFinding(
            location="security/ir.model.access.csv", severity="blocking",
            explanation=(
                "File exists but is empty; the task goal states 'Do not add any new model, "
                "field, or view', so this file is unnecessary and should be deleted to "
                "minimize complexity."
            ),
        ),
    ]
    result = _filter_hallucinated_findings_blocking_on_structurally_mandatory_core_files(findings)
    assert result[0].severity == "info"
    print("PASS: a 'delete security/ir.model.access.csv' finding is downgraded")


def test_downgrades_manifest_data_reference_finding():
    findings = [
        ReviewFinding(
            location="__manifest__.py", severity="blocking",
            explanation=(
                "Manifest declares 'data': ['security/ir.model.access.csv'] but the task goal "
                "explicitly states 'ONLY manifest and hook code', and the CSV is empty; "
                "including an empty CSV file in data is unnecessary complexity."
            ),
        ),
    ]
    result = _filter_hallucinated_findings_blocking_on_structurally_mandatory_core_files(findings)
    assert result[0].severity == "info"
    print("PASS: a manifest-data-reference finding about a mandatory file is downgraded")


def test_downgrades_third_rephrasing_variant_requires_no_new_x():
    """Real, confirmed gap found live (2026-08-11, same task, same night, third distinct
    rephrasing observed in as many rounds): 'this file and its manifest reference must be
    removed...', 'task goal requires no new access records, so this file must be deleted
    entirely...' -- neither contains 'unnecessary' nor the exact 'model, field, or view' phrase,
    confirming a single fixed wording match will always eventually be dodged by rephrasing.
    """
    findings = [
        ReviewFinding(
            location="security/ir.model.access.csv", severity="blocking",
            explanation=(
                "Manifest declares 'data': ['security/ir.model.access.csv'] but the task goal "
                "explicitly states 'Do not add any new model, field, or view' and the CSV is "
                "empty; this file and its manifest reference must be removed to prevent install "
                "errors or confusion."
            ),
        ),
        ReviewFinding(
            location="security/ir.model.access.csv", severity="blocking",
            explanation=(
                "File exists on disk but is empty (header only) and unreferenced by the goal; "
                "task goal requires no new access records, so this file must be deleted "
                "entirely to prevent future install crashes or confusion."
            ),
        ),
    ]
    result = _filter_hallucinated_findings_blocking_on_structurally_mandatory_core_files(findings)
    assert result[0].severity == "info"
    assert result[1].severity == "info"
    print("PASS: a third, distinct rephrasing variant ('must be removed', 'requires no new X') "
          "is also correctly downgraded")


def test_downgrades_delete_recommendation_for_root_init_py():
    """Real, confirmed gap found live (2026-08-11, same task, same night, one round after this
    filter's own first fix): __init__.py (root, 'from . import models') and models/__init__.py
    are equally structurally mandatory, deterministically scaffolded, never omittable by Build --
    missing from the original path list, so this exact complaint shape survived untouched.
    """
    findings = [
        ReviewFinding(
            location="__init__.py", severity="blocking",
            explanation=(
                "File imports 'models' but the task goal states 'Do not add any new model, "
                "field, or view', so the models directory and its imports are unnecessary and "
                "should be removed."
            ),
        ),
        ReviewFinding(
            location="models/__init__.py:1", severity="blocking",
            explanation="File imports 'models' but the task goal states '...', so the import is unnecessary and should be removed.",
        ),
    ]
    result = _filter_hallucinated_findings_blocking_on_structurally_mandatory_core_files(findings)
    assert result[0].severity == "info"
    assert result[1].severity == "info", "a ':<line>' suffix on the location must not prevent matching"
    print("PASS: delete recommendations for both __init__.py and models/__init__.py are downgraded")


def test_does_not_swallow_a_genuine_finding_on_a_different_init_py_via_substring_collision():
    """Real, confirmed near-miss caught before it shipped: '__init__.py' is a substring of
    'controllers/__init__.py' (and 'tests/__init__.py'), so a naive `in` check would wrongly
    downgrade a genuine, correct "delete this stale controllers/__init__.py" finding -- exactly
    the kind of leftover-scaffold-boilerplate complaint this pipeline legitimately raises
    elsewhere. Matching must be exact-path (optionally with a ':<line>' suffix), never substring.
    """
    findings = [
        ReviewFinding(
            location="controllers/__init__.py", severity="blocking",
            explanation="This file is unnecessary and should be deleted -- controllers/ was never used.",
        ),
    ]
    result = _filter_hallucinated_findings_blocking_on_structurally_mandatory_core_files(findings)
    assert result[0].severity == "blocking", (
        "a genuine finding about a DIFFERENT, non-mandatory __init__.py-named file must never "
        "be swallowed by substring collision with the module-root __init__.py"
    )
    print("PASS: a genuine finding on a different, non-mandatory __init__.py-suffixed path is never touched")


def test_downgrades_when_location_is_generic_but_explanation_names_the_mandatory_file():
    """Real, confirmed gap found live (2026-08-11, same task, same night, after the earlier
    wording-robustness fixes already landed): the model does not reliably put the mandatory
    path in `location` at all -- confirmed live, a finding whose own explanation text literally
    quotes 'security/ir.model.access.csv' and uses exactly the already-matched deletion
    language still had a generic/unrelated `location` value, so the filter never engaged.
    """
    findings = [
        ReviewFinding(
            location="Manifest", severity="blocking",
            explanation=(
                "The manifest declares 'data': ['security/ir.model.access.csv'] but the task "
                "goal explicitly states 'Do not add any new model, field, or view -- the ONLY "
                "code this module needs is its own __manifest__.py ... and the post_init_hook "
                "function itself', and the provided CSV is empty; including an empty data file "
                "is unnecessary complexity and violates the goal's constraint to only include "
                "the manifest and hook."
            ),
        ),
    ]
    result = _filter_hallucinated_findings_blocking_on_structurally_mandatory_core_files(findings)
    assert result[0].severity == "info"
    print("PASS: a finding naming a mandatory file only in its explanation text (not location) is still downgraded")


def test_leaves_a_genuine_unrelated_finding_on_models_py_untouched():
    findings = [
        ReviewFinding(
            location="models/models.py", severity="blocking",
            explanation="Uses a bare except: clause, swallowing all exceptions silently.",
        ),
    ]
    result = _filter_hallucinated_findings_blocking_on_structurally_mandatory_core_files(findings)
    assert result[0].severity == "blocking"
    print("PASS: a genuine, unrelated finding on a mandatory file is never touched")


def test_leaves_a_delete_recommendation_on_a_non_mandatory_file_untouched():
    findings = [
        ReviewFinding(
            location="views/views.xml", severity="blocking",
            explanation="This view is unnecessary and should be deleted -- the goal wants no views.",
        ),
    ]
    result = _filter_hallucinated_findings_blocking_on_structurally_mandatory_core_files(findings)
    assert result[0].severity == "blocking", (
        "views/views.xml is genuinely optional -- a real 'delete it' finding there must stay blocking"
    )
    print("PASS: a delete recommendation on a non-mandatory file is never touched")


def test_leaves_non_blocking_findings_untouched():
    findings = [
        ReviewFinding(
            location="models/models.py", severity="info",
            explanation="This file is unnecessary and should be deleted.",
        ),
    ]
    result = _filter_hallucinated_findings_blocking_on_structurally_mandatory_core_files(findings)
    assert result[0].severity == "info"
    assert result[0].explanation == findings[0].explanation, "a non-blocking finding must never be rewritten"
    print("PASS: a non-blocking finding is left completely untouched")


def test_downgrades_false_circular_import_claim_against_standard_root_init():
    """Real, confirmed bug found live (2026-08-11, task 18fca388, one round after the
    structurally-mandatory-core-files filter above): 'File imports 'models' from itself
    ('from . import models') which is a circular import error and will crash the module
    import.' -- a parent package importing its own child submodule is never circular; this
    exact line is deterministically scaffolded on every module this pipeline generates.
    """
    findings = [
        ReviewFinding(
            location="__init__.py", severity="blocking",
            explanation=(
                "File imports 'models' from itself ('from . import models') which is a "
                "circular import error and will crash the module import."
            ),
        ),
    ]
    result = _filter_hallucinated_findings_claiming_the_standard_module_root_init_is_a_circular_import(findings)
    assert result[0].severity == "info"
    print("PASS: a false 'circular import' claim against the standard __init__.py is downgraded")


def test_circular_import_filter_never_touches_a_different_files_genuine_finding():
    findings = [
        ReviewFinding(
            location="models/models.py", severity="blocking",
            explanation="models/a.py and models/b.py import each other -- a genuine circular import.",
        ),
    ]
    result = _filter_hallucinated_findings_claiming_the_standard_module_root_init_is_a_circular_import(findings)
    assert result[0].severity == "blocking", (
        "a genuine circular-import finding about DIFFERENT files must never be touched by a "
        "filter scoped only to the module-root __init__.py's own standard line"
    )
    print("PASS: a genuine circular-import finding about different files is never touched")


if __name__ == "__main__":
    test_downgrades_delete_recommendation_for_models_py()
    test_downgrades_delete_recommendation_for_security_csv()
    test_downgrades_manifest_data_reference_finding()
    test_downgrades_third_rephrasing_variant_requires_no_new_x()
    test_downgrades_delete_recommendation_for_root_init_py()
    test_does_not_swallow_a_genuine_finding_on_a_different_init_py_via_substring_collision()
    test_leaves_a_genuine_unrelated_finding_on_models_py_untouched()
    test_leaves_a_delete_recommendation_on_a_non_mandatory_file_untouched()
    test_leaves_non_blocking_findings_untouched()
    test_downgrades_when_location_is_generic_but_explanation_names_the_mandatory_file()
    test_downgrades_false_circular_import_claim_against_standard_root_init()
    test_circular_import_filter_never_touches_a_different_files_genuine_finding()
    print("\nALL CODE-REVIEW STRUCTURALLY-MANDATORY-CORE-FILES FILTER TESTS PASSED")
