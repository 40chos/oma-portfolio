"""Real, confirmed bug found live (2026-08-11, task 18fca388, post_init_hook cleanup round):
`_extract_reproduction_target()`'s own goal-literal-name heuristic matched a fully-qualified XML
external id embedded in the goal text (e.g.
'oma_build_a_complete_field_ab52b7f8.access_oma_service_ticket', naming the ir.model.access.csv
ROW a post_init_hook revokes, never any ORM field) purely because it superficially looks like a
snake_case identifier. A real Odoo field name is always a single, undotted Python identifier --
this is a purely syntactic, unambiguous signal, the same class of false-negative the existing
button/menu/non-field-constraint/method shape skips in specialists/testing_qa/specialist.py
already exist for.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from specialists.testing_qa.specialist import _target_looks_like_an_xmlid_not_a_field


def test_xmlid_shaped_target_is_detected():
    assert _target_looks_like_an_xmlid_not_a_field(
        "oma_build_a_complete_field_ab52b7f8.access_oma_service_ticket"
    )
    assert _target_looks_like_an_xmlid_not_a_field("base.group_user")
    print("PASS: dotted XML-external-id-shaped targets are correctly detected as not a field")


def test_ordinary_field_name_is_not_flagged():
    assert not _target_looks_like_an_xmlid_not_a_field("preferred_language")
    assert not _target_looks_like_an_xmlid_not_a_field("access_control_field")
    assert not _target_looks_like_an_xmlid_not_a_field("")
    print("PASS: ordinary, undotted field names are never misflagged as an xmlid")


def test_bare_xmlid_suffix_detected_via_goal_cross_reference():
    """Real, confirmed gap found live (2026-08-11, same task, same night): the extraction is not
    consistent about keeping the module prefix -- on one round it returned the full dotted
    xmlid, on a LATER round it returned only the bare record-id suffix ('access_oma_service_
    ticket', no dot at all), which the strict dotted-shape check alone can never catch, since a
    bare identifier is syntactically indistinguishable from a genuine field name. The goal
    itself still literally names the full xmlid, so cross-referencing it recovers the signal.
    """
    goal = (
        "resolve the external id 'oma_build_a_complete_field_ab52b7f8.access_oma_service_ticket' "
        "via env.ref(...), unlinking that one specific record."
    )
    assert _target_looks_like_an_xmlid_not_a_field("access_oma_service_ticket", goal)
    print("PASS: a bare xmlid suffix is still detected via cross-referencing the goal's own literal mention")


def test_bare_suffix_not_flagged_without_a_matching_goal_mention():
    goal = "Add a 'access_oma_service_ticket' Char field to res.partner."  # not xmlid-shaped in the goal either
    assert not _target_looks_like_an_xmlid_not_a_field("access_oma_service_ticket", "")
    assert not _target_looks_like_an_xmlid_not_a_field("preferred_language", goal)
    print("PASS: a bare, ordinary-looking name is never flagged without a real dotted xmlid mention in the goal")


if __name__ == "__main__":
    test_xmlid_shaped_target_is_detected()
    test_ordinary_field_name_is_not_flagged()
    test_bare_xmlid_suffix_detected_via_goal_cross_reference()
    test_bare_suffix_not_flagged_without_a_matching_goal_mention()
    print("\nALL REPRODUCTION-TARGET XMLID-SHAPE-SKIP TESTS PASSED")
