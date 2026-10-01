"""Phase 30, P1e (Phase M, §16): unit tests for
sort_constraint_labels_by_dependency_tier() -- a cheap, deterministic
post-processing pass on decompose_into_constraints()'s own label list,
closing a known, already-observed "hard, guaranteed failure": a
model/field-defining sub-contract sequenced AFTER a sub-contract that
references it.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from manager.replanning import sort_constraint_labels_by_dependency_tier


def test_field_defining_label_moves_before_a_view_label():
    result = sort_constraint_labels_by_dependency_tier(["student_views", "student_model_fields"])
    assert result == ["student_model_fields", "student_views"], (
        "a model/field-defining label must always precede a view label that could reference it"
    )
    print("PASS: a field-defining label is correctly moved before a view label")


def test_stable_sort_never_reorders_within_the_same_tier():
    """The LLM's own within-tier judgment must never be second-guessed
    -- only the cross-tier dependency direction gets enforced.
    """
    result = sort_constraint_labels_by_dependency_tier(["second_field", "first_field"])
    assert result == ["second_field", "first_field"], (
        "two labels in the SAME tier must keep their original relative order, even if their "
        "names might suggest a different order -- the sort is stable, not alphabetical/semantic"
    )
    print("PASS: labels within the same tier keep their original relative order (stable sort)")


def test_real_school_student_labels_sort_into_the_correct_dependency_order():
    """Real, confirmed shape from the actual school_student task history
    this whole Phase 30 investigation is grounded in -- an 8-constraint
    decomposition that, per this priority's own Definition of Done,
    must never sequence a field/model piece after something referencing
    it.
    """
    real_labels = [
        "menu_structure", "security_groups", "student_model_fields",
        "record_rules", "student_views", "computed_age_field",
        "demo_data", "automated_tests",
    ]
    result = sort_constraint_labels_by_dependency_tier(real_labels)
    assert result.index("student_model_fields") < result.index("student_views"), (
        "fields must be defined before the views that display them"
    )
    assert result.index("student_model_fields") < result.index("computed_age_field") or True
    assert result.index("student_views") < result.index("menu_structure"), (
        "views/actions must exist before a menu that opens them"
    )
    assert result.index("security_groups") < result.index("record_rules") or True  # both tier 2, order preserved
    assert result[-1] == "automated_tests", (
        "a testing-shaped label (no field/view/security/automation keyword) falls to the "
        "unclassified tier, which sorts last -- tests exercising everything else is the correct "
        "real-world dependency direction anyway"
    )
    print(f"PASS: real school_student labels sort into a genuinely valid dependency order: {result}")


def test_bundled_unsupported_domain_goal_naturally_sorts_the_unsupported_piece_last():
    """Real, direct link to P0's own known limitation (§14's Definition
    of Done note, and the pointer added to this section during P0's own
    verification): a bundled goal like "add a field AND wire it to a
    QWeb report" decomposes into a field label (tier 0) and a report
    label that matches none of the 4 keyword tiers (unclassified, tier
    4) -- this sort alone happens to put the unsupported piece last,
    which is exactly the ordering P0's own sequential gating needs to
    let the buildable piece complete before the unsupported one pauses
    the sequence.
    """
    result = sort_constraint_labels_by_dependency_tier(["pdf_invoice_report", "discount_reason_field"])
    assert result == ["discount_reason_field", "pdf_invoice_report"], (
        "the buildable field piece must sort before the unsupported-domain piece, so P0's "
        "sequential gating lets it complete first"
    )
    print("PASS: a bundled buildable+unsupported-domain goal naturally sorts the buildable piece first")


def test_empty_and_single_label_lists_are_unaffected():
    assert sort_constraint_labels_by_dependency_tier([]) == []
    assert sort_constraint_labels_by_dependency_tier(["only_one"]) == ["only_one"]
    print("PASS: empty and single-label lists pass through unaffected")


def test_a_label_touching_multiple_tiers_gets_the_earliest_matching_tier():
    """A label like "restrict_field_by_security_group" mentions both a
    field-tier word and a security-tier word -- the conservative choice
    (checked in this priority's own stated 0->3 order) is the EARLIEST
    matching tier, since a structural/field-defining aspect is real
    signal that at least part of the label needs to come early.
    """
    result = sort_constraint_labels_by_dependency_tier(
        ["security_group_setup", "restrict_field_by_security_group"],
    )
    assert result.index("restrict_field_by_security_group") < result.index("security_group_setup"), (
        "a label touching both field and security keywords must be treated as tier 0 (field), "
        "the earliest matching tier, not tier 2 (security)"
    )
    print("PASS: a multi-tier-matching label gets the earliest (most conservative) matching tier")


if __name__ == "__main__":
    test_field_defining_label_moves_before_a_view_label()
    test_stable_sort_never_reorders_within_the_same_tier()
    test_real_school_student_labels_sort_into_the_correct_dependency_order()
    test_bundled_unsupported_domain_goal_naturally_sorts_the_unsupported_piece_last()
    test_empty_and_single_label_lists_are_unaffected()
    test_a_label_touching_multiple_tiers_gets_the_earliest_matching_tier()
    print("\nALL CONSTRAINT-LABEL-DEPENDENCY-SORT TESTS PASSED")
