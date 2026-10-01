"""Phase 34 execution (2026-08-11): unit tests for
tools_odoo/quick_filter_extractor.py -- the deterministic quick-filter
construction mechanism, real, confirmed gap found live in three separate,
identically-worded Batch A survey failures.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tools_odoo.quick_filter_extractor import (
    extract_quick_filters,
    build_deterministic_search_view_snippet,
    render_filter_domain,
)


def test_named_condition_phrasing_no_explicit_domain():
    goal = (
        "Add a quick filter to the CRM lead list called 'My Leads' showing only leads "
        "assigned to the current user."
    )
    result = extract_quick_filters(goal)
    assert len(result) == 1
    assert result[0].label == "My Leads" and result[0].field == "user_id" and result[0].value == "uid"
    print("PASS: named-condition phrasing ('assigned to the current user') resolves to user_id=uid")


def test_explicit_inline_domain_phrasing_multiple_filters():
    goal = (
        "Add a filter to the meerwerk search view: 'Accepted' (state=accepted) and "
        "'My records' (user_id=uid), with a separator."
    )
    result = extract_quick_filters(goal)
    assert len(result) == 2
    assert result[0].label == "Accepted" and result[0].field == "state" and result[0].value == "accepted"
    assert result[1].label == "My records" and result[1].field == "user_id" and result[1].value == "uid"
    print("PASS: two explicit inline-domain filters both extracted correctly")


def test_no_filter_word_returns_none():
    goal = "Add a computed field 'total' to sale.order."
    assert extract_quick_filters(goal) is None
    print("PASS: a goal with no 'filter' word at all returns None")


def test_filter_word_present_but_no_quoted_label_returns_none():
    goal = "Filter the results so only active records show, no label needed."
    assert extract_quick_filters(goal) is None
    print("PASS: 'filter' mentioned but no quoted label present returns None")


def test_single_word_label_echoing_a_state_value_resolves():
    """Real, confirmed recurring gap found live (2026-08-12, day-to-day directions sweep, Batch A,
    task007's own real goal text): 'Accepted' has no idiom in the named-condition table, but the
    goal literally repeats the same word right after 'only' in the same sentence -- a real,
    textually-grounded signal, not a guess. Both filters in the same real goal must now resolve,
    where before the whole extraction bailed because only one of the two had a known idiom."""
    goal = (
        "In the meerwerk list, I want a quick filter button called 'Accepted' that shows only "
        "accepted records, and another called 'My records' that shows only records assigned to me."
    )
    result = extract_quick_filters(goal)
    assert result is not None and len(result) == 2
    assert result[0].label == "Accepted" and result[0].field == "state" and result[0].value == "accepted"
    assert result[1].label == "My records" and result[1].field == "user_id" and result[1].value == "uid"
    print("PASS: a single-word label echoing 'only <label>' resolves to state=<label>, alongside a "
          "second filter resolved via the existing idiom table")


def test_multi_word_label_never_uses_state_echo_fallback():
    """The fallback above must stay narrow: a multi-word label is never a bare state value, even
    if it happens to echo nearby text -- this must still return None, not guess."""
    goal = "Add a filter to the sale order list called 'Big Deal' showing only big deal orders."
    assert extract_quick_filters(goal) is None
    print("PASS: a multi-word label never triggers the state-echo fallback")


def test_unresolvable_named_condition_returns_none_not_a_guess():
    goal = "Add a filter to the sale order list called 'Weird One' showing something unusual."
    assert extract_quick_filters(goal) is None
    print("PASS: a named filter with no resolvable condition returns None, never guesses")


def test_render_filter_domain_uid_vs_literal():
    from tools_odoo.quick_filter_extractor import ExtractedQuickFilter
    f1 = ExtractedQuickFilter(label="X", field="user_id", value="uid")
    f2 = ExtractedQuickFilter(label="Y", field="state", value="accepted")
    assert render_filter_domain(f1) == "[('user_id','=',uid)]"
    assert render_filter_domain(f2) == "[('state','=','accepted')]"
    print("PASS: uid renders unquoted (a real Python name), literal values render quoted")


def test_render_filter_domain_boolean_renders_unquoted():
    """Real fix (2026-08-12, day-to-day directions sweep): a live-schema-verified 'active'
    boolean substitution (fleet.vehicle has no 'state' field) must render True/False as real
    Python booleans, never quoted strings -- a quoted 'True' never matches a real boolean column."""
    from tools_odoo.quick_filter_extractor import ExtractedQuickFilter
    f1 = ExtractedQuickFilter(label="Active", field="active", value="True")
    f2 = ExtractedQuickFilter(label="Inactive", field="active", value="False")
    assert render_filter_domain(f1) == "[('active','=',True)]"
    assert render_filter_domain(f2) == "[('active','=',False)]"
    print("PASS: boolean substitution values render as real Python bools, not quoted strings")


def test_snippet_includes_separator_and_all_filters():
    goal = "Add a filter called 'A' (state=a) and 'B' (state=b)."
    filters = extract_quick_filters(goal)
    snippet = build_deterministic_search_view_snippet(filters)
    assert "<separator/>" in snippet
    assert 'string="A"' in snippet and 'string="B"' in snippet
    print("PASS: the rendered snippet includes a separator and every extracted filter")


if __name__ == "__main__":
    test_named_condition_phrasing_no_explicit_domain()
    test_explicit_inline_domain_phrasing_multiple_filters()
    test_no_filter_word_returns_none()
    test_filter_word_present_but_no_quoted_label_returns_none()
    test_unresolvable_named_condition_returns_none_not_a_guess()
    test_render_filter_domain_uid_vs_literal()
    test_snippet_includes_separator_and_all_filters()
    print("\nALL QUICK FILTER EXTRACTOR TESTS PASSED")
