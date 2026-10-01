"""Phase 20 Area 2 (2026-07-20, UPDATE 20): unit tests for
specialists/build/specialist.py's
_autofix_strip_premature_security_content_on_decomposed_round() and
the matching relaxation in _validate_security_csv_covers_new_models()
-- pure logic, no live SSH/DB/gateway needed.

Real bug this fixes: #43/#44/#45 each burned their entire 5-round
retry budget on an identical non-progress loop -- Build kept
re-adding a real security_csv access row (or security_xml) despite
the round's own goal explicitly saying that exact constraint (e.g.
'ticket_access_rule') is NOT yet in scope this round. Code-Review
correctly rejected it every round (confirmed, this was never a
Code-Review bug -- see UPDATE 16), but nothing ever stopped Build
from generating the same premature content again. 2026 research on
non-convergent LLM self-repair loops converges on: once a violation
category repeats, convert it into a deterministic pre-write filter
rather than re-prompting and hoping for compliance.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from specialists.build.specialist import (
    GeneratedModuleFiles,
    ManifestFields,
    _any_security_related_constraint_already_satisfied,
    _autofix_restore_dropped_security_content_when_already_satisfied,
    _autofix_strip_premature_security_content_on_decomposed_round,
    _has_remaining_decomposed_constraints,
    _this_rounds_focus_is_security_related,
    _validate_security_csv_covers_new_models,
    _validate_security_csv_not_empty,
)

_REAL_43_GOAL = (
    "Create a small new Odoo module that defines a brand new custom model called "
    "service.ticket (a simple support ticket with a name and description field), and "
    "adds a new security group called 'Service Ticket Managers' with full read, write, "
    "create, and delete access to this new model via its own access rule row. "
    "This round's own NEW focus is ONLY: 'service_ticket_model'. Add ONLY the code this "
    "one constraint needs. The following constraints are NOT yet in scope for this round "
    "and must NOT be implemented even partially: ['ticket_managers_group', 'ticket_access_rule']."
)

_SECURITY_FOCUS_GOAL = (
    "Create a small new Odoo module... This round's own NEW focus is ONLY: "
    "'ticket_access_rule'. The following constraints are NOT yet in scope for this round "
    "and must NOT be implemented even partially: []."
)

_PLAIN_GOAL = "Add a single new field 'preferred_language' to res.partner."


def _make_generated(security_csv: str, security_xml: str | None = None, manifest_has_security_ref: bool = False) -> GeneratedModuleFiles:
    manifest_fields = ManifestFields(
        name="oma_test", version="0.1", category="Uncategorized", summary="", author="",
        depends=["base"],
        data=(["security/ir.model.access.csv"] + (["security/security.xml"] if manifest_has_security_ref else [])),
    )
    return GeneratedModuleFiles(
        manifest_fields=manifest_fields,
        models_py="from odoo import models, fields\n\nclass ServiceTicket(models.Model):\n    _name = 'service.ticket'\n",
        security_csv=security_csv,
        security_xml=security_xml,
        notes="",
    )


def test_has_remaining_decomposed_constraints_true_for_real_43_goal():
    assert _has_remaining_decomposed_constraints(_REAL_43_GOAL) is True
    print("PASS: real #43 goal text is correctly recognized as a decomposed round with more to come")


def test_focus_is_security_related_false_for_model_round():
    assert _this_rounds_focus_is_security_related(_REAL_43_GOAL) is False
    print("PASS: 'service_ticket_model' focus is correctly NOT flagged as security-related")


def test_focus_is_security_related_true_for_access_rule_round():
    assert _this_rounds_focus_is_security_related(_SECURITY_FOCUS_GOAL) is True
    print("PASS: 'ticket_access_rule' focus is correctly flagged as security-related")


def test_keeps_new_models_own_baseline_row_even_when_a_future_round_owns_full_security():
    """Real, DEEPER bug found live (2026-08-08, two independent real live tasks -- a
    'flagship field-service' suite and an 'equipment-maintenance' suite -- both reproducing
    the identical failure): this test used to assert the opposite (the row gets stripped to
    header-only) on the theory that a later, still-pending 'ticket_access_rule'-style
    constraint would add the real row eventually, so stripping now was safe. That theory is
    wrong: `_validate_security_csv_not_empty()` (specialists/build/specialist.py, added
    2026-07-30, run on EVERY round unconditionally, with no decomposed-round/future-round
    awareness at all -- confirmed by reading its own code) hard-fails the CURRENT round's own
    install-verification the instant a new model exists with a header-only security_csv,
    regardless of whether a future round will later add fuller, group-based security on top.
    A planned future round does not help an install attempt happening right now. This
    interaction was never exercised anywhere in this file before -- every test here calls
    the stripper in isolation, never combined with `_validate_security_csv_not_empty()` -- so
    this exact real, live-confirmed failure was invisible to the whole existing test suite
    (see the new `test_stripped_and_then_unconditionally_validated_together...` test below,
    which closes exactly that gap). The row for `service.ticket` (the genuinely new model
    THIS round declares) must now survive; a later round is still free to ADD its own new
    group's row on top, an ordinary extension never blocked by this row merely existing.
    """
    generated = _make_generated(
        security_csv=(
            "id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink\n"
            "access_service_ticket,access.service.ticket,model_service_ticket,,1,1,1,0"
        ),
    )
    _autofix_strip_premature_security_content_on_decomposed_round(generated, _REAL_43_GOAL)
    lines = generated.security_csv.strip().splitlines()
    assert len(lines) == 2, (
        "the new model's own baseline row must survive even though a future round "
        "('ticket_access_rule') will later add fuller security -- stripping it here would "
        "make THIS round's own install-verification fail immediately"
    )
    assert "access_service_ticket" in generated.security_csv
    print("PASS: a new model's own baseline access row survives a decomposed round even when "
          "a future round will later own fuller, group-based security")


def test_also_strips_the_now_empty_csvs_own_manifest_reference():
    """Extended fix (2026-07-20, live on #43 pass 9): Code-Review still
    correctly rejected a round whose CSV was stripped to header-only
    but whose manifest still referenced it at all ("File exists but is
    empty and out of scope") -- the manifest reference itself must go too.

    Fixture corrected 2026-08-08: the row must be for a model OTHER than the one this round's
    own `models_py` declares (`service.ticket`) -- a row for the round's own genuinely new
    model is no longer stripped at all (see `test_keeps_new_models_own_baseline_row_even_
    when_a_future_round_owns_full_security` above for why), so this test now uses a row for
    an unrelated model to keep testing what it's actually meant to: that content which IS
    still genuinely premature gets its manifest reference dropped along with it.
    """
    generated = _make_generated(
        security_csv=(
            "id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink\n"
            "access_unrelated_model,access.unrelated.model,model_unrelated_model,,1,1,1,0"
        ),
    )
    _autofix_strip_premature_security_content_on_decomposed_round(generated, _REAL_43_GOAL)
    assert "security/ir.model.access.csv" not in generated.manifest_py
    print("PASS: the now-empty security_csv's own manifest reference is also stripped, not just its rows")


def test_strips_premature_security_xml_and_its_manifest_reference():
    generated = _make_generated(
        security_csv="id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink",
        security_xml="<odoo><record id=\"group_x\" model=\"res.groups\"><field name=\"name\">X</field></record></odoo>",
        manifest_has_security_ref=True,
    )
    _autofix_strip_premature_security_content_on_decomposed_round(generated, _REAL_43_GOAL)
    assert generated.security_xml == ""
    assert "security/security.xml" not in generated.manifest_py
    print("PASS: premature security_xml content and its manifest reference are both stripped")


def test_does_not_strip_on_the_rounds_own_security_focused_round():
    generated = _make_generated(
        security_csv=(
            "id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink\n"
            "access_service_ticket,access.service.ticket,model_service_ticket,oma_test.group_ticket_managers,1,1,1,0"
        ),
        security_xml="<odoo><record id=\"group_ticket_managers\" model=\"res.groups\"><field name=\"name\">Managers</field></record></odoo>",
        manifest_has_security_ref=True,
    )
    _autofix_strip_premature_security_content_on_decomposed_round(generated, _SECURITY_FOCUS_GOAL)
    assert "access_service_ticket" in generated.security_csv
    assert generated.security_xml != ""
    print("PASS: a round whose OWN focus is the security/access work is never stripped")


def test_does_not_strip_on_a_plain_non_decomposed_task():
    generated = _make_generated(
        security_csv=(
            "id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink\n"
            "access_x,access.x,model_x,,1,1,1,0"
        ),
    )
    _autofix_strip_premature_security_content_on_decomposed_round(generated, _PLAIN_GOAL)
    assert "access_x" in generated.security_csv
    print("PASS: a plain, non-decomposed task's real security content is never touched")


def test_validator_relaxed_for_model_only_round_with_header_only_csv():
    generated = _make_generated(
        security_csv="id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink",
    )
    # Should NOT raise -- previously this would have raised "no access row for new model(s)"
    _validate_security_csv_covers_new_models(generated, _REAL_43_GOAL)
    print("PASS: the new-model-needs-access-row validator no longer fights the autofix on a decomposed round")


def test_strips_the_manifest_reference_even_when_the_csv_is_already_header_only():
    """Real, confirmed bug found live (2026-07-28, Phase 28C,
    `school_student` task): the old `len(rows) > 1` guard only ever
    stripped the manifest's own `data` reference when REAL access rows
    were present. A round that already, correctly, generated a
    header-only CSV (nothing premature in the file's own content) still
    left the stray `security/ir.model.access.csv` reference in `data`
    completely untouched -- Code-Review correctly rejects even a bare,
    inert reference as out-of-scope (the same principle this file's own
    `test_also_strips_the_now_empty_csvs_own_manifest_reference` already
    established for the non-empty case). Confirmed live as a direct,
    repeated cause of a 5-round non-convergent loop.
    """
    generated = _make_generated(
        security_csv="id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink",
    )
    assert "security/ir.model.access.csv" in generated.manifest_py
    _autofix_strip_premature_security_content_on_decomposed_round(generated, _REAL_43_GOAL)
    assert "security/ir.model.access.csv" not in generated.manifest_py
    print("PASS: a header-only security_csv's own stray manifest reference is now also stripped")


_COMPUTED_AGE_FIELD_FOCUS_GOAL = (
    "Create a small new Odoo module... This round's own NEW focus is ONLY: "
    "'computed_age_field'. The following constraints are NOT yet in scope for this round "
    "and must NOT be implemented even partially: ['demo_data', 'automated_tests']."
)

_REAL_SECURITY_XML = (
    '<odoo>\n'
    '    <record id="group_school_admin" model="res.groups">\n'
    '        <field name="name">School / Admin</field>\n'
    '    </record>\n'
    '</odoo>'
)


def test_any_security_related_constraint_already_satisfied_true_for_security_groups():
    assert _any_security_related_constraint_already_satisfied({
        "security_groups": "satisfied", "record_rules": "satisfied", "computed_age_field": "pending",
    }) is True
    print("PASS: security_groups=satisfied is correctly recognized as security content that must "
          "be preserved")


def test_any_security_related_constraint_already_satisfied_false_when_none_satisfied_yet():
    assert _any_security_related_constraint_already_satisfied({
        "security_groups": "pending", "record_rules": "pending",
    }) is False
    print("PASS: no false positive when no security-related constraint has been satisfied yet")


def test_does_not_regress_already_satisfied_security_content_on_a_later_round():
    """Real, confirmed bug found live (2026-07-29, Phase 28C,
    school_student task, computed_age_field round): the exact sibling
    of the views_xml regression found and fixed earlier the same
    night, for security_xml instead -- with security_groups and
    record_rules both already satisfied, the very next round
    (computed_age_field) wiped security_xml entirely, confirmed live
    via a genuine `FileNotFoundError: security/security.xml` at
    install time (the real file was simply gone from disk).
    """
    generated = _make_generated(
        security_csv="id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink",
        security_xml=_REAL_SECURITY_XML,
        manifest_has_security_ref=True,
    )
    constraint_status = {
        "student_model_fields": "satisfied", "student_views": "satisfied",
        "menu_structure": "satisfied", "security_groups": "satisfied", "record_rules": "satisfied",
        "computed_age_field": "pending", "demo_data": "pending", "automated_tests": "pending",
    }
    _autofix_strip_premature_security_content_on_decomposed_round(
        generated, _COMPUTED_AGE_FIELD_FOCUS_GOAL, constraint_status,
    )
    assert generated.security_xml == _REAL_SECURITY_XML, "already-earned security content must survive"
    assert "security/security.xml" in generated.manifest_py
    print("PASS: already-satisfied security_groups/record_rules content is preserved on a later, "
          "unrelated round, closing the real live regression")


def test_restores_dropped_security_content_when_build_never_regenerated_it():
    generated = _make_generated(
        security_csv="id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink",
        security_xml=None,
    )
    constraint_status = {"security_groups": "satisfied", "record_rules": "satisfied"}
    _autofix_restore_dropped_security_content_when_already_satisfied(
        generated, constraint_status, _REAL_SECURITY_XML,
    )
    assert generated.security_xml == _REAL_SECURITY_XML
    assert "security/security.xml" in generated.manifest_py
    print("PASS: already-satisfied security content Build simply never regenerated is restored "
          "from the prior committed round's own real content")


def test_never_overwrites_security_content_build_genuinely_did_write():
    generated = _make_generated(
        security_csv="id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink",
        security_xml="<odoo><record id='new_thing' model='res.groups'/></odoo>",
    )
    constraint_status = {"security_groups": "satisfied"}
    _autofix_restore_dropped_security_content_when_already_satisfied(
        generated, constraint_status, _REAL_SECURITY_XML,
    )
    assert generated.security_xml == "<odoo><record id='new_thing' model='res.groups'/></odoo>"
    print("PASS: never overwrites security content Build genuinely did write this round")


_NO_SECURITY_ROUND_LEFT_GOAL = (
    "Create a small new Odoo module that defines a brand new custom model called "
    "oma.meerwerk.email.content... This round's own NEW focus is ONLY: "
    "'email_content_model'. The following constraints are NOT yet in scope for this round "
    "and must NOT be implemented even partially: ['email_template', 'write_override']."
)


def test_preserves_new_models_baseline_row_when_no_future_round_owns_security():
    """P14 item 2 benchmark finding (2026-08-02): real, live-confirmed dead loop (tasks 020/029)
    -- a round declares a genuinely new model and has real remaining constraints, but NONE of them
    are security-labeled (no future round is ever coming to add the baseline access row). Unlike
    the `service_ticket_model` scenario above (where 'ticket_access_rule' IS still pending),
    stripping here would leave the model permanently inaccessible with nothing to ever fix it.
    """
    generated = _make_generated(
        security_csv=(
            "id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink\n"
            "access_oma_meerwerk_email_content,oma.meerwerk.email.content,"
            "model_oma_meerwerk_email_content,base.group_user,1,1,1,0"
        ),
    )
    generated.models_py = (
        "from odoo import models, fields\n\nclass MeerwerkEmailContent(models.Model):\n"
        "    _name = 'oma.meerwerk.email.content'\n"
    )
    _autofix_strip_premature_security_content_on_decomposed_round(generated, _NO_SECURITY_ROUND_LEFT_GOAL)
    assert "access_oma_meerwerk_email_content" in generated.security_csv
    print("PASS: a new model's baseline row survives when no future round is scheduled to add it")


def test_baseline_row_survives_regardless_of_which_future_constraints_remain():
    """Sibling case, same fixture shape as `test_preserves_new_models_baseline_row_when_no_
    future_round_owns_security` above, but with a genuinely pending security-labeled
    constraint ('ticket_access_rule') -- confirms the fix applies uniformly, not just to the
    no-future-round case. Corrected 2026-08-08 (see `test_keeps_new_models_own_baseline_
    row_even_when_a_future_round_owns_full_security` above for the full real incident this
    closes) -- this test used to assert the row gets stripped here specifically because a
    future round owns security; that was the exact real, live-confirmed bug.
    """
    generated = _make_generated(
        security_csv=(
            "id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink\n"
            "access_service_ticket,service.ticket,model_service_ticket,base.group_user,1,1,1,0"
        ),
    )
    _autofix_strip_premature_security_content_on_decomposed_round(generated, _REAL_43_GOAL)
    lines = generated.security_csv.strip().splitlines()
    assert len(lines) == 2
    assert "access_service_ticket" in generated.security_csv
    print("PASS: the baseline row survives even when a future round ('ticket_access_rule') "
          "will later own fuller security")


def test_stripped_and_then_unconditionally_validated_together_never_hard_fails():
    """Closes the real test-coverage gap the incident above exposed: every other test in this
    file calls the stripper in isolation, never combined with the separate, unconditional,
    decomposed-round-UNAWARE `_validate_security_csv_not_empty()` (added 2026-07-30, run on
    every real round regardless of how many constraints remain) -- so a real interaction bug
    between the two was invisible here even though both are exercised elsewhere individually.
    Runs the exact real sequence a live round actually goes through: generate -> strip
    (premature-content autofix) -> the hard, unconditional validator -- and confirms it never
    raises for a genuinely new model's own round, regardless of what's still pending.
    """
    generated = _make_generated(
        security_csv=(
            "id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink\n"
            "access_service_ticket,access.service.ticket,model_service_ticket,,1,1,1,0"
        ),
    )
    _autofix_strip_premature_security_content_on_decomposed_round(generated, _REAL_43_GOAL)
    _validate_security_csv_not_empty(generated)  # must not raise
    print("PASS: the real generate -> strip -> validate sequence never hard-fails a genuinely "
          "new model's own round, closing the real interaction gap between the two functions")


def test_validator_still_enforces_for_a_plain_non_decomposed_task():
    generated = _make_generated(security_csv="id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink")
    raised = False
    try:
        _validate_security_csv_covers_new_models(generated, _PLAIN_GOAL)
    except ValueError:
        raised = True
    assert raised
    print("PASS: the validator still enforces its original rule for a plain, non-decomposed task")


if __name__ == "__main__":
    test_has_remaining_decomposed_constraints_true_for_real_43_goal()
    test_focus_is_security_related_false_for_model_round()
    test_focus_is_security_related_true_for_access_rule_round()
    test_keeps_new_models_own_baseline_row_even_when_a_future_round_owns_full_security()
    test_also_strips_the_now_empty_csvs_own_manifest_reference()
    test_strips_premature_security_xml_and_its_manifest_reference()
    test_does_not_strip_on_the_rounds_own_security_focused_round()
    test_does_not_strip_on_a_plain_non_decomposed_task()
    test_strips_the_manifest_reference_even_when_the_csv_is_already_header_only()
    test_validator_relaxed_for_model_only_round_with_header_only_csv()
    test_any_security_related_constraint_already_satisfied_true_for_security_groups()
    test_any_security_related_constraint_already_satisfied_false_when_none_satisfied_yet()
    test_does_not_regress_already_satisfied_security_content_on_a_later_round()
    test_restores_dropped_security_content_when_build_never_regenerated_it()
    test_never_overwrites_security_content_build_genuinely_did_write()
    test_preserves_new_models_baseline_row_when_no_future_round_owns_security()
    test_baseline_row_survives_regardless_of_which_future_constraints_remain()
    test_stripped_and_then_unconditionally_validated_together_never_hard_fails()
    test_validator_still_enforces_for_a_plain_non_decomposed_task()
    print("\nALL BUILD PREMATURE-SECURITY-STRIP TESTS PASSED")
