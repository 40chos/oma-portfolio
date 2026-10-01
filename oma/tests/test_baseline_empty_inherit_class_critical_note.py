"""Real, confirmed follow-up gap found live (2026-08-09, task 07141af5's flagship run,
project_ticket_counts node): specialists/build/specialist.py's `_validate_inherit_only_class_is_not_empty()`
(and its sibling `_validate_model_class_is_not_a_pure_identity_stub()`) only ever check a round's
OWN new candidate output -- neither can catch this exact defect already sitting in the
git-committed BASELINE (`old_files_by_relpath`), e.g. from an earlier round that committed it
before this validator existed. Once that happens, every subsequent scoped-edit round sees an
already-existing class with the right `_inherit` line in its own "CURRENT CONTENT" prompt context
and reasonably, but wrongly, treats it as already handled -- scoped-edit generation only ever
touches what it's explicitly told to, so the broken content gets carried forward unchanged,
forever, independent of temperature, notes, or candidate count. Confirmed live: 8+ consecutive
resumes, several with genuine multi-candidate diversity after raising `candidate_count`, all
produced byte-for-byte identical output.

`_find_empty_inherit_class_defect()` is the reusable, non-raising detection core extracted from
the validator specifically so `_run_module_dev()` can ALSO run it against the baseline (once per
round) and, if found, inject an explicit CRITICAL_RULE_PREFIX rule telling the model point-blank
that EXISTING content needs fixing this round, not just its own new output.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from contracts.schema import CRITICAL_RULE_PREFIX, split_critical_rules
from specialists.build.specialist import _find_empty_inherit_class_defect


def test_find_empty_inherit_class_defect_is_non_raising():
    empty = "class ProjectProject(models.Model):\n    _inherit = 'project.project'\n"
    defect = _find_empty_inherit_class_defect(empty)
    assert defect is not None
    assert "EMPTY body" in defect
    assert "project.project" in defect
    print("PASS: _find_empty_inherit_class_defect returns a message, never raises")


def test_find_empty_inherit_class_defect_returns_none_for_healthy_baseline():
    good = (
        "class ProjectProject(models.Model):\n"
        "    _inherit = 'project.project'\n"
        "    open_ticket_count = fields.Integer()\n"
    )
    assert _find_empty_inherit_class_defect(good) is None
    print("PASS: a genuinely healthy baseline returns None, no false positive")


def test_baseline_defect_message_is_injected_as_a_critical_rule():
    # Mirrors _run_module_dev()'s own injection logic exactly: when the baseline has a real
    # defect, it's wrapped and appended to contract.rules with CRITICAL_RULE_PREFIX, so it lands
    # in Build's own prioritized "CRITICAL FIXES REQUIRED" prompt section, not diluted ordinary
    # history.
    baseline_defect = _find_empty_inherit_class_defect(
        "class ProjectProject(models.Model):\n    _inherit = 'project.project'\n"
    )
    assert baseline_defect is not None
    rules = [
        "Original attempt summary: stuck on X",
        f"{CRITICAL_RULE_PREFIX}The EXISTING, already-committed models.py has a real, confirmed "
        f"defect that a prior round left broken -- you must fix it as part of this round's own "
        f"work, not just add new content elsewhere and leave it as-is: {baseline_defect}",
    ]
    critical, ordinary = split_critical_rules(rules)
    assert len(critical) == 1
    assert "EXISTING, already-committed models.py" in critical[0]
    assert "EMPTY body" in critical[0]
    print("PASS: a real baseline defect is injected as a critical rule Build's prompt prioritizes")


if __name__ == "__main__":
    test_find_empty_inherit_class_defect_is_non_raising()
    test_find_empty_inherit_class_defect_returns_none_for_healthy_baseline()
    test_baseline_defect_message_is_injected_as_a_critical_rule()
    print("\nALL BASELINE-EMPTY-INHERIT-CLASS CRITICAL-NOTE TESTS PASSED")
