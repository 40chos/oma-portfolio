"""Phase 32 implementation (2026-08-11): unit tests for the new lightweight
UI-action-presence check (specialists/testing_qa/ui_action_presence_check.py).
Uses the real, confirmed-live minimal ticket form arch (no header buttons,
no chatter) as the regression fixture for the exact gap found tonight, plus
a corrected arch to prove the check passes once the view is actually wired.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from specialists.testing_qa.ui_action_presence_check import (
    check_goal_named_ui_actions_present,
    format_ui_action_gap_warning,
)

_REAL_MINIMAL_ARCH_CONFIRMED_LIVE = """
<form string="Service Ticket">
    <sheet>
        <group>
            <field name="name"/>
            <field name="state"/>
            <field name="equipment_id"/>
        </group>
    </sheet>
</form>
""".strip()

_CORRECTED_ARCH_WITH_BUTTONS_AND_CHATTER = """
<form string="Service Ticket">
    <header>
        <button name="action_start_work" string="Start" type="object" class="oe_highlight"/>
        <button name="action_resolve" string="Resolve" type="object"/>
        <field name="state" widget="statusbar"/>
    </header>
    <sheet>
        <group>
            <field name="name"/>
            <field name="equipment_id"/>
        </group>
    </sheet>
    <div class="oe_chatter">
        <field name="message_ids"/>
    </div>
</form>
""".strip()

_GOAL_TEXT = (
    'Add two new buttons INSIDE a ticket: "Start" and "Resolve", and make sure the ticket '
    "is tracked in the chatter/activity log so every state change is visible with a status bar."
)


def test_real_confirmed_gap_is_detected_against_the_minimal_arch():
    result = check_goal_named_ui_actions_present(_GOAL_TEXT, _REAL_MINIMAL_ARCH_CONFIRMED_LIVE)
    assert result.applicable is True
    assert "Start" in result.named_buttons_missing
    assert "Resolve" in result.named_buttons_missing
    assert result.chatter_expected is True and result.chatter_present is False
    assert result.statusbar_expected is True and result.statusbar_present is False
    assert result.has_gap is True
    warning = format_ui_action_gap_warning(result)
    assert "Start" in warning and "Resolve" in warning
    print("PASS: the real, confirmed-live missing-buttons/chatter/statusbar gap is detected")


def test_corrected_arch_passes_with_no_gap():
    result = check_goal_named_ui_actions_present(_GOAL_TEXT, _CORRECTED_ARCH_WITH_BUTTONS_AND_CHATTER)
    assert result.named_buttons_missing == []
    assert result.chatter_present is True
    assert result.statusbar_present is True
    assert result.has_gap is False
    print("PASS: once the view is actually wired up, the check reports no gap")


def test_not_applicable_when_goal_names_no_ui_interaction():
    goal = "Add a computed field 'total_hours' to oma.service.ticket, no UI changes needed."
    result = check_goal_named_ui_actions_present(goal, _REAL_MINIMAL_ARCH_CONFIRMED_LIVE)
    assert result.applicable is False
    assert result.has_gap is False
    print("PASS: a goal naming no concrete UI interaction is correctly marked not applicable")


def test_ordinary_use_of_tracking_does_not_false_trigger_chatter_expected():
    """Real, confirmed bug found live overnight (2026-08-14): the word "tracking" in its
    ordinary English sense ("a model for TRACKING supply requests") -- nothing to do with
    Odoo's own chatter feature -- used to match this check's regex and false-trigger
    chatter_expected=True. Confirmed live: 3 of 3 real "new self-contained module" tasks
    got this exact false positive in one overnight batch, each incorrectly recorded as a
    qualifying failure and escalating that direction's real certification bar. None of
    these goals asked for a chatter widget.
    """
    real_goals_from_the_incident = [
        "Create a new module that defines a brand new custom model to track equipment "
        "maintenance logs, with fields for equipment name (Char), maintenance date (Date), "
        "and technician notes (Text).",
        "Create a brand new module that defines a brand new custom model named "
        "'oma.book.club.selection' for tracking monthly book club picks. Include exactly "
        "these three fields: book_title (Char), selected_month (Date), page_count "
        "(Integer).",
        "Build a small new module that defines a brand new custom model named "
        "'oma.shipping.label.log' for tracking printed shipping labels. Include exactly "
        "these three fields: tracking_number (Char), printed_date (Date), carrier_name "
        "(Char).",
    ]
    minimal_arch_no_chatter = "<form><sheet><group><field name='name'/></group></sheet></form>"
    for goal in real_goals_from_the_incident:
        result = check_goal_named_ui_actions_present(goal, minimal_arch_no_chatter)
        assert result.chatter_expected is False, f"false-triggered on: {goal!r}"
        assert result.has_gap is False
    print("PASS: ordinary English use of 'tracking'/'track' no longer false-triggers chatter_expected")


def test_real_chatter_mention_still_correctly_detected():
    goal = "The record should show a chatter/activity log so changes are visible."
    result = check_goal_named_ui_actions_present(goal, "<form><sheet/></form>")
    assert result.chatter_expected is True
    assert result.has_gap is True
    print("PASS: a real, explicit chatter/activity-log mention is still correctly detected")


def test_partial_gap_only_flags_the_missing_piece():
    goal = 'Add a "Start" button to the ticket form.'
    arch = """
    <form>
        <header>
            <button name="action_start_work" string="Start" type="object"/>
        </header>
    </form>
    """
    result = check_goal_named_ui_actions_present(goal, arch)
    assert result.named_buttons_missing == []
    assert result.chatter_expected is False
    assert result.has_gap is False
    print("PASS: a goal naming only a button (no chatter/statusbar mention) checks only that button")


def test_quick_filter_button_phrasing_does_not_false_trigger_named_button_expectation():
    """Real, confirmed bug found live (2026-08-15): "quick filter button"/"filter button" is
    Odoo's own common way to describe a real <filter> element in a SEARCH view -- structurally
    not a <button> form-view widget at all. Before this fix, the bare word "button" in this
    phrasing made this check wrongly expect a <button string="..."> matching the filter's own
    label, which of course never exists in the form view, producing a real, confirmed false
    UI-action-gap warning on 3 of 3 real quick-filter tasks in one night, including one whose
    actual deliverable (a real <filter> element) was fully correct.
    """
    real_goals_from_the_incident = [
        "Add a quick filter button to the product.template list view called 'Low Stock' "
        "that shows only products where the quantity on hand is below 5.",
        "In the crm.lead list, I want a quick filter button called 'Accepted' that shows "
        "only records where the stage is Won.",
        "Add a filter button to the sale.order list view called 'High Value' that shows "
        "only orders where the total amount is greater than 10000.",
    ]
    minimal_arch_no_buttons = "<form><sheet><group><field name='name'/></group></sheet></form>"
    for goal in real_goals_from_the_incident:
        result = check_goal_named_ui_actions_present(goal, minimal_arch_no_buttons)
        assert result.named_buttons_expected == [], f"false-triggered on: {goal!r}"
        assert result.has_gap is False, f"false-triggered on: {goal!r}"
    print("PASS: 'quick filter button'/'filter button' phrasing no longer false-triggers a named-button expectation")


def test_a_real_button_mention_still_triggers_the_check_normally():
    """The fix above must be narrow, scoped only to the "no OTHER button mention at all" case
    that caused the real false positives -- a goal that separately names a genuine form button
    must still be checked exactly as before (this fix doesn't attempt fine-grained per-label
    filtering, only whether the check engages at all)."""
    goal = "Add a \"Mark Reviewed\" button to the form that sets a review flag."
    result = check_goal_named_ui_actions_present(goal, "<form><sheet/></form>")
    assert "Mark Reviewed" in result.named_buttons_expected
    assert result.has_gap is True
    print("PASS: a genuine named-button goal is still correctly detected")


if __name__ == "__main__":
    test_real_confirmed_gap_is_detected_against_the_minimal_arch()
    test_corrected_arch_passes_with_no_gap()
    test_not_applicable_when_goal_names_no_ui_interaction()
    test_partial_gap_only_flags_the_missing_piece()
    test_ordinary_use_of_tracking_does_not_false_trigger_chatter_expected()
    test_real_chatter_mention_still_correctly_detected()
    test_quick_filter_button_phrasing_does_not_false_trigger_named_button_expectation()
    test_a_real_button_mention_still_triggers_the_check_normally()
    print("\nALL UI ACTION PRESENCE CHECK TESTS PASSED")
