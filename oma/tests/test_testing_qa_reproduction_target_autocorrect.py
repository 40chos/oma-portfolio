"""Phase 20 Area 2 (2026-07-17): unit tests for
specialists/testing_qa/specialist.py's
_autocorrect_hallucinated_reproduction_target -- pure logic against
monkeypatched dependencies, no live SSH/DB/gateway needed, same style
as test_security_xml_generation.py. Kept separate from
test_testing_qa_specialist.py since that file's own fixtures require a
real odoo16-dev container and gateway.

Real bug this fixes: _extract_reproduction_target() (a separate LLM
call from anything Build generates) hallucinated a field name
('user_id') that doesn't exist on the real model even though the
correct real field ('user_ids') was genuinely present in the module's
own real generated code -- confirmed live via a real record-rule task
that failed reproduction 4 of 5 rounds on an otherwise-correct module.
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import uuid

import specialists.testing_qa.specialist as testing_qa_module
from contracts.schema import AutonomyTier, CapabilityClass, SpecialistType, TaskContract
from specialists.testing_qa.specialist import (
    ReproductionTarget,
    ReproductionTargetList,
    SecurityAccessClaim,
    TestingQASpecialist,
    _collision_confirmed_field_names_from_inputs,
    _has_remaining_decomposed_constraints,
    _is_this_rounds_own_focus,
    _MENU_FOCUS_KEYWORD_RE,
    _SECURITY_FOCUS_KEYWORD_RE,
    _this_rounds_own_focus_label,
)


def _make_contract(**overrides) -> TaskContract:
    base = dict(
        task_id=uuid.uuid4(),
        specialist_type=SpecialistType.bug_fix,
        capability_class=CapabilityClass.module_development,
        tier=AutonomyTier.tier_2_notify_after,
        goal="On the meerwerk record, show the total of all line prices in the header.",
        inputs=[],
        rules=[],
        deliverables=["a module"],
        compensating_actions=[],
        validation_by="testing_qa",
        pause_if=[],
        turn_budget=15,
    )
    base.update(overrides)
    return TaskContract(**base)


def _make_specialist():
    return TestingQASpecialist(client=None, routine_model="fake-model")


def test_autocorrects_hallucinated_field_to_real_pluralized_field(monkeypatch):
    def fake_check_field_exists(db, model, field_name):
        return field_name != "user_id"  # only the hallucinated name is missing
    def fake_get_model_fields(model, db):
        return ["id", "name", "create_uid", "user_ids", "activity_user_id"]
    def fake_read_module_files(module_name):
        return {
            "security/security.xml": (
                "<record id=\"rule_x\" model=\"ir.rule\">"
                "<field name=\"domain_force\">[('create_uid','=',user.id),('user_ids','in',[user.id])]</field>"
                "</record>"
            ),
        }
    monkeypatch.setattr(testing_qa_module, "check_field_exists_on_model", fake_check_field_exists)
    monkeypatch.setattr(testing_qa_module, "get_model_fields", fake_get_model_fields)
    monkeypatch.setattr(testing_qa_module, "read_module_files", fake_read_module_files)

    specialist = _make_specialist()
    target = ReproductionTarget(model="project.task", field_name="user_id")
    result = asyncio.run(
        specialist._autocorrect_hallucinated_reproduction_target(target, "oma_test", "odoo16_dev")
    )
    assert result.field_name == "user_ids"
    assert result.model == "project.task"
    print("PASS: hallucinated 'user_id' auto-corrected to the real 'user_ids' actually used in the generated code")


def test_leaves_target_unchanged_when_field_genuinely_real(monkeypatch):
    def fake_check_field_exists(db, model, field_name):
        return True
    monkeypatch.setattr(testing_qa_module, "check_field_exists_on_model", fake_check_field_exists)

    specialist = _make_specialist()
    target = ReproductionTarget(model="res.partner", field_name="name")
    result = asyncio.run(
        specialist._autocorrect_hallucinated_reproduction_target(target, "oma_test", "odoo16_dev")
    )
    assert result is target
    print("PASS: a genuinely real field is never touched")


def test_leaves_target_unchanged_when_ambiguous(monkeypatch):
    """Two real fields share the invented field's stem -- must never
    guess between them.
    """
    def fake_check_field_exists(db, model, field_name):
        return False
    def fake_get_model_fields(model, db):
        return ["id", "user_ids", "user_id_backup_ids"]
    def fake_read_module_files(module_name):
        return {
            "security/security.xml": (
                "<field name=\"domain_force\">[('user_ids','in',[1]),('user_id_backup_ids','in',[1])]</field>"
            ),
        }
    monkeypatch.setattr(testing_qa_module, "check_field_exists_on_model", fake_check_field_exists)
    monkeypatch.setattr(testing_qa_module, "get_model_fields", fake_get_model_fields)
    monkeypatch.setattr(testing_qa_module, "read_module_files", fake_read_module_files)

    specialist = _make_specialist()
    target = ReproductionTarget(model="project.task", field_name="user_id")
    result = asyncio.run(
        specialist._autocorrect_hallucinated_reproduction_target(target, "oma_test", "odoo16_dev")
    )
    assert result is target
    print("PASS: an ambiguous match (multiple candidate real fields) is left unchanged, never guessed")


def test_leaves_target_unchanged_when_no_candidate_is_actually_used_in_code(monkeypatch):
    """A real field shares a stem with the invented name, but is never
    actually referenced in the module's own generated code -- must not
    be treated as a legitimate correction (a coincidental name match,
    not proof Build genuinely used it).
    """
    def fake_check_field_exists(db, model, field_name):
        return False
    def fake_get_model_fields(model, db):
        return ["id", "user_ids"]
    def fake_read_module_files(module_name):
        return {"models/models.py": "from odoo import models\nclass X(models.Model):\n    pass\n"}
    monkeypatch.setattr(testing_qa_module, "check_field_exists_on_model", fake_check_field_exists)
    monkeypatch.setattr(testing_qa_module, "get_model_fields", fake_get_model_fields)
    monkeypatch.setattr(testing_qa_module, "read_module_files", fake_read_module_files)

    specialist = _make_specialist()
    target = ReproductionTarget(model="project.task", field_name="user_id")
    result = asyncio.run(
        specialist._autocorrect_hallucinated_reproduction_target(target, "oma_test", "odoo16_dev")
    )
    assert result is target
    print("PASS: a real field not actually referenced anywhere in the generated code is not used as a correction")


def test_autocorrects_hallucinated_field_with_zero_stem_overlap_to_the_one_real_defined_field(monkeypatch):
    """Real bug found live (2026-08-06, fix-pass task004): the stem-match tier above only
    catches NEAR-MISS hallucinations -- it does nothing when the extraction invents a name
    with zero stem overlap with the real field. Confirmed live: goal "total of all line
    prices... in the header" -> extraction invented 'line_prices_total', but the module's
    real generated models.py genuinely defined `amount_total = fields.Monetary(...,
    compute='_compute_amount_total', store=True)` with a correct compute method -- the
    extraction call simply never read it. Reproduction FAILED identically across 2 rounds on
    an otherwise-correct module before this fix.
    """
    def fake_check_field_exists(db, model, field_name):
        return field_name != "line_prices_total"
    def fake_get_model_fields(model, db):
        return ["id", "name", "line_ids", "currency_id", "amount_total"]
    def fake_read_module_files(module_name):
        return {
            "models/models.py": (
                "from odoo import api, fields, models\n\n"
                "class ProjectMeerwerk(models.Model):\n"
                "    _inherit = 'project.meerwerk'\n\n"
                "    amount_total = fields.Monetary(\n"
                "        string='Amount Total',\n"
                "        currency_field='currency_id',\n"
                "        compute='_compute_amount_total',\n"
                "        store=True,\n"
                "    )\n\n"
                "    @api.depends('line_ids.price_unit')\n"
                "    def _compute_amount_total(self):\n"
                "        for record in self:\n"
                "            record.amount_total = sum(l.price_unit for l in record.line_ids)\n"
            ),
        }
    monkeypatch.setattr(testing_qa_module, "check_field_exists_on_model", fake_check_field_exists)
    monkeypatch.setattr(testing_qa_module, "get_model_fields", fake_get_model_fields)
    monkeypatch.setattr(testing_qa_module, "read_module_files", fake_read_module_files)

    specialist = _make_specialist()
    target = ReproductionTarget(model="project.meerwerk", field_name="line_prices_total")
    result = asyncio.run(
        specialist._autocorrect_hallucinated_reproduction_target(target, "oma_test", "odoo16_dev")
    )
    assert result.field_name == "amount_total"
    assert result.model == "project.meerwerk"
    print("PASS: a hallucinated field name with zero stem overlap is corrected to the one real "
          "field the module's own code genuinely defines")


def test_does_not_autocorrect_to_an_unrelated_field_when_goal_literally_names_the_claimed_field(monkeypatch):
    """Real, confirmed bug found live (2026-08-11, task 18fca388's own regression test,
    test_deliberately_broken_fix_is_caught_by_reproduction_check): a deliberately broken fix
    claims (explicitly, quoted, in the goal) to add 'should_not_exist_field' but actually
    defines a completely unrelated 'a_completely_different_field' -- before this fix, the
    "exactly one new field defined" tier blindly "corrected" the target to the unrelated field
    anyway, turning a genuine, deliberate bug into a false reproduction pass. When the goal
    literally names the claimed field, a genuinely missing field must stay a genuine failure.
    """
    def fake_check_field_exists(db, model, field_name):
        return False  # should_not_exist_field genuinely does not exist
    def fake_get_model_fields(model, db):
        return ["id", "name", "a_completely_different_field"]
    def fake_read_module_files(module_name):
        return {
            "models/models.py": (
                "from odoo import fields, models\n\n"
                "class BrokenFixPartner(models.Model):\n"
                "    _inherit = 'res.partner'\n\n"
                "    a_completely_different_field = fields.Char(string='Not What Was Asked For')\n"
            ),
        }
    monkeypatch.setattr(testing_qa_module, "check_field_exists_on_model", fake_check_field_exists)
    monkeypatch.setattr(testing_qa_module, "get_model_fields", fake_get_model_fields)
    monkeypatch.setattr(testing_qa_module, "read_module_files", fake_read_module_files)

    specialist = _make_specialist()
    target = ReproductionTarget(model="res.partner", field_name="should_not_exist_field")
    goal = "Add a 'should_not_exist_field' field to res.partner."
    result = asyncio.run(
        specialist._autocorrect_hallucinated_reproduction_target(
            target, "oma_test", "odoo16_dev", goal_text=goal,
        )
    )
    assert result is target, (
        f"a goal that literally names the claimed field must never be 'corrected' to an "
        f"unrelated field just because it's the only one the module happens to define: {result!r}"
    )
    print("PASS: an explicit, goal-literal field claim that genuinely doesn't exist stays a "
          "genuine failure, never silently corrected to an unrelated field")


def test_does_not_autocorrect_when_module_defines_multiple_new_fields(monkeypatch):
    """Ambiguous case for the new tier: the module defines TWO new fields via `fields.` --
    must never guess which one the hallucinated name was supposed to mean.
    """
    def fake_check_field_exists(db, model, field_name):
        return False
    def fake_get_model_fields(model, db):
        return ["id", "amount_total", "amount_tax"]
    def fake_read_module_files(module_name):
        return {
            "models/models.py": (
                "from odoo import fields, models\n\n"
                "class X(models.Model):\n"
                "    _inherit = 'project.meerwerk'\n\n"
                "    amount_total = fields.Monetary(compute='_compute_total', store=True)\n"
                "    amount_tax = fields.Monetary(compute='_compute_tax', store=True)\n"
            ),
        }
    monkeypatch.setattr(testing_qa_module, "check_field_exists_on_model", fake_check_field_exists)
    monkeypatch.setattr(testing_qa_module, "get_model_fields", fake_get_model_fields)
    monkeypatch.setattr(testing_qa_module, "read_module_files", fake_read_module_files)

    specialist = _make_specialist()
    target = ReproductionTarget(model="project.meerwerk", field_name="grand_total")
    result = asyncio.run(
        specialist._autocorrect_hallucinated_reproduction_target(target, "oma_test", "odoo16_dev")
    )
    assert result is target
    print("PASS: multiple newly-defined real fields is left ambiguous and never guessed")


def test_autocorrects_cross_model_when_goal_names_the_real_owning_model(monkeypatch):
    """Real, confirmed bug found live (2026-08-06, fix-pass task048): the extraction call
    correctly returned the LITERAL field name the goal names ('satisfaction_ids'), per its own
    prompt rule (1), but attributed it to the WRONG one of the two models the multi-model goal
    names -- the goal explicitly says "Inherit project.project to add satisfaction_ids
    One2many", but the extraction returned model='project.satisfaction' instead of
    'project.project'. Every prior tier in this function stays scoped to the claimed model and
    can never fix this. New cross-model tier: when the goal literally names another dotted model
    and the field is confirmed live on EXACTLY that one other model, correct the model.
    """
    def fake_check_field_exists(db, model, field_name):
        return False  # satisfaction_ids does not exist on project.satisfaction
    def fake_get_model_fields(model, db):
        # non-empty, but with no name-stem overlap with 'satisfaction_ids' -- lets execution
        # reach the new cross-model tier instead of the "genuine uncertainty" empty-list return
        return ["id", "name", "score", "comment"]
    def fake_read_module_files(module_name):
        return {"models/models.py": "from odoo import fields, models\n"}
    def fake_get_model_fields_fast(model, db, login='Admin'):
        if model == "project.project":
            return ["id", "name", "satisfaction_ids", "avg_score"]
        if model == "project.satisfaction":
            return ["id", "name", "score", "comment"]  # real fields, but not satisfaction_ids
        return None
    monkeypatch.setattr(testing_qa_module, "check_field_exists_on_model", fake_check_field_exists)
    monkeypatch.setattr(testing_qa_module, "get_model_fields", fake_get_model_fields)
    monkeypatch.setattr(testing_qa_module, "read_module_files", fake_read_module_files)
    monkeypatch.setattr(testing_qa_module, "is_fast_path_eligible", lambda db: True)
    import tools_odoo.odoo_schema_client as schema_client_module
    monkeypatch.setattr(schema_client_module, "get_model_fields_fast", fake_get_model_fields_fast)

    specialist = _make_specialist()
    target = ReproductionTarget(model="project.satisfaction", field_name="satisfaction_ids")
    goal = (
        "Build a new module called project_satisfaction. Inherit project.project to add "
        "satisfaction_ids One2many and avg_score computed Float."
    )
    result = asyncio.run(
        specialist._autocorrect_hallucinated_reproduction_target(
            target, "oma_test", "odoo16_dev", task_id="t1", goal_text=goal,
        )
    )
    assert result.model == "project.project"
    assert result.field_name == "satisfaction_ids"
    print("PASS: a field hallucinated onto the wrong of two goal-named models is corrected to "
          "the real owning model")


def test_cross_model_tier_never_fires_without_goal_text(monkeypatch):
    """Regression guard: with no goal_text passed at all (every pre-existing call site that
    doesn't thread it), the new tier must never activate -- same as before this fix."""
    def fake_check_field_exists(db, model, field_name):
        return False
    def fake_get_model_fields(model, db):
        return []
    def fake_read_module_files(module_name):
        return {"models/models.py": "from odoo import fields, models\n"}
    monkeypatch.setattr(testing_qa_module, "check_field_exists_on_model", fake_check_field_exists)
    monkeypatch.setattr(testing_qa_module, "get_model_fields", fake_get_model_fields)
    monkeypatch.setattr(testing_qa_module, "read_module_files", fake_read_module_files)
    monkeypatch.setattr(testing_qa_module, "is_fast_path_eligible", lambda db: True)

    specialist = _make_specialist()
    target = ReproductionTarget(model="project.satisfaction", field_name="satisfaction_ids")
    result = asyncio.run(
        specialist._autocorrect_hallucinated_reproduction_target(target, "oma_test", "odoo16_dev", task_id="t1")
    )
    assert result is target
    print("PASS: with no goal_text supplied, the cross-model tier never activates")


def test_cross_model_tier_never_guesses_when_multiple_models_have_the_field(monkeypatch):
    """Ambiguous case: two OTHER goal-named models both genuinely have a field with this exact
    name -- must never guess which one, same 'never guess' discipline as every sibling tier."""
    def fake_check_field_exists(db, model, field_name):
        return False
    def fake_get_model_fields(model, db):
        return ["id", "description"]  # non-empty, no stem overlap with 'name'
    def fake_read_module_files(module_name):
        return {"models/models.py": "from odoo import fields, models\n"}
    def fake_get_model_fields_fast(model, db, login='Admin'):
        if model == "project.x":
            return ["id", "description"]
        if model in ("project.project", "res.partner"):
            return ["id", "name"]
        return None
    monkeypatch.setattr(testing_qa_module, "check_field_exists_on_model", fake_check_field_exists)
    monkeypatch.setattr(testing_qa_module, "get_model_fields", fake_get_model_fields)
    monkeypatch.setattr(testing_qa_module, "read_module_files", fake_read_module_files)
    monkeypatch.setattr(testing_qa_module, "is_fast_path_eligible", lambda db: True)
    import tools_odoo.odoo_schema_client as schema_client_module
    monkeypatch.setattr(schema_client_module, "get_model_fields_fast", fake_get_model_fields_fast)

    specialist = _make_specialist()
    target = ReproductionTarget(model="project.x", field_name="name")
    goal = "Link project.project and res.partner together, add a name somewhere."
    result = asyncio.run(
        specialist._autocorrect_hallucinated_reproduction_target(
            target, "oma_test", "odoo16_dev", task_id="t1", goal_text=goal,
        )
    )
    assert result is target
    print("PASS: two candidate models both having the field is left ambiguous, never guessed")


def test_collision_confirmed_field_names_from_inputs_parses_the_real_entry():
    inputs = ["verify_module:oma_xyz:odoo16_dev", "collision_confirmed_fields:['amount_total']"]
    assert _collision_confirmed_field_names_from_inputs(inputs) == ["amount_total"]
    print("PASS: the collision_confirmed_fields: input entry is parsed correctly")


def test_collision_confirmed_field_names_from_inputs_empty_when_absent():
    inputs = ["verify_module:oma_xyz:odoo16_dev"]
    assert _collision_confirmed_field_names_from_inputs(inputs) == []
    print("PASS: no collision_confirmed_fields entry returns an empty list")


def test_autocorrects_using_build_confirmed_collision_field_even_with_zero_stem_overlap_and_no_code_trace(monkeypatch):
    """Real bug this fixes (2026-08-06, fix-pass task 004, the deepest root cause): Build's own
    collision-autofix correctly recognized 'amount_total' as already-real and genuinely correctly
    stripped its own redundant re-declaration from models_py -- meaning NEITHER the stem-match
    tier NOR the newly-defined-field tier can ever find it (it's not in the code at all, by
    design, and it has zero stem overlap with the hallucinated 'line_prices_total'). Only the
    collision_confirmed_fields channel (Build's own live-schema-confirmed ground truth, threaded
    from manager/tools.py) can correct this.
    """
    def fake_check_field_exists(db, model, field_name):
        return field_name != "line_prices_total"  # only the hallucinated name is missing
    def fake_get_model_fields(model, db):
        return ["id", "name", "line_ids", "amount_total"]
    def fake_read_module_files(module_name):
        # Correctly stripped -- no field declaration left at all, by design.
        return {"models/models.py": "class ProjectMeerwerk(models.Model):\n    _inherit = 'project.meerwerk'\n"}
    monkeypatch.setattr(testing_qa_module, "check_field_exists_on_model", fake_check_field_exists)
    monkeypatch.setattr(testing_qa_module, "get_model_fields", fake_get_model_fields)
    monkeypatch.setattr(testing_qa_module, "read_module_files", fake_read_module_files)

    specialist = _make_specialist()
    target = ReproductionTarget(model="project.meerwerk", field_name="line_prices_total")
    result = asyncio.run(
        specialist._autocorrect_hallucinated_reproduction_target(
            target, "oma_test", "odoo16_dev", collision_confirmed_fields=["amount_total"],
        )
    )
    assert result.field_name == "amount_total"
    assert result.model == "project.meerwerk"
    print("PASS: Build's own collision-confirmed field is used even when neither the stem-match "
          "nor the newly-defined-field tier could ever find it")


def test_does_not_use_collision_confirmed_fields_when_ambiguous():
    specialist = _make_specialist()
    target = ReproductionTarget(model="project.meerwerk", field_name="line_prices_total")
    import unittest.mock as mock
    with mock.patch.object(testing_qa_module, "check_field_exists_on_model", return_value=False):
        result = asyncio.run(
            specialist._autocorrect_hallucinated_reproduction_target(
                target, "oma_test", "odoo16_dev",
                collision_confirmed_fields=["amount_total", "amount_tax"],
            )
        )
    assert result is target
    print("PASS: more than one collision-confirmed field is left ambiguous, never guessed")


def test_final_round_widening_autocorrects_a_hallucinated_earlier_target_instead_of_false_regression(monkeypatch):
    """Real bug this fixes (2026-08-06, fix-pass task 004): `_extract_all_reproduction_targets()`
    (the final-round widening check's own extraction call) has the exact same invented-field
    failure mode as `_extract_reproduction_target()` -- but before this fix, its own re-
    verification loop (`_reverify_earlier_constraints_field_targets`) went straight to a raw
    existence check with ZERO autocorrect, so a hallucinated name here reported a false "an
    earlier constraint's field no longer exists" regression even when the round's own primary
    check (which DOES autocorrect) had just independently confirmed the real field seconds
    earlier. Confirmed live: task 004's round correctly confirmed
    `project.meerwerk.amount_total` via the primary check, then this widening step independently
    re-hallucinated `line_prices_total` and reported a false regression.
    """
    async def fake_extract_all(self, contract, module_name):
        return ReproductionTargetList(
            targets=[ReproductionTarget(model="project.meerwerk", field_name="line_prices_total")]
        )
    def fake_check_field_exists(db, model, field_name):
        return field_name != "line_prices_total"  # only the hallucinated name is missing
    def fake_get_model_fields(model, db):
        return ["id", "name", "line_ids", "amount_total"]
    def fake_read_module_files(module_name):
        return {"models/models.py": "class ProjectMeerwerk(models.Model):\n    _inherit = 'project.meerwerk'\n"}
    monkeypatch.setattr(testing_qa_module.TestingQASpecialist, "_extract_all_reproduction_targets", fake_extract_all)
    monkeypatch.setattr(testing_qa_module, "check_field_exists_on_model", fake_check_field_exists)
    monkeypatch.setattr(testing_qa_module, "get_model_fields", fake_get_model_fields)
    monkeypatch.setattr(testing_qa_module, "read_module_files", fake_read_module_files)

    specialist = _make_specialist()
    contract = _make_contract(inputs=["collision_confirmed_fields:['amount_total']"])
    already_checked = ReproductionTarget(model="project.meerwerk", field_name="amount_total")
    confirmed, notes = asyncio.run(
        specialist._reverify_earlier_constraints_field_targets(contract, "oma_test", "odoo16_dev", already_checked)
    )
    assert confirmed is True
    assert notes == ""
    print("PASS: the widening check autocorrects a hallucinated earlier target using the "
          "collision-confirmed field instead of reporting a false regression")


def test_final_round_widening_still_reports_a_genuine_regression(monkeypatch):
    """Regression guard for the fix above: a genuinely missing earlier field (no collision
    confirmation, no code trace, no stem match -- a real regression) must still be reported,
    never silently swallowed just because autocorrect now runs first.
    """
    async def fake_extract_all(self, contract, module_name):
        return ReproductionTargetList(
            targets=[ReproductionTarget(model="project.meerwerk", field_name="genuinely_removed_field")]
        )
    def fake_check_field_exists(db, model, field_name):
        return False  # genuinely missing, no matter what
    def fake_get_model_fields(model, db):
        return ["id", "name", "line_ids"]
    def fake_read_module_files(module_name):
        return {"models/models.py": "class ProjectMeerwerk(models.Model):\n    _inherit = 'project.meerwerk'\n"}
    monkeypatch.setattr(testing_qa_module.TestingQASpecialist, "_extract_all_reproduction_targets", fake_extract_all)
    monkeypatch.setattr(testing_qa_module, "check_field_exists_on_model", fake_check_field_exists)
    monkeypatch.setattr(testing_qa_module, "get_model_fields", fake_get_model_fields)
    monkeypatch.setattr(testing_qa_module, "read_module_files", fake_read_module_files)

    specialist = _make_specialist()
    contract = _make_contract(inputs=[])  # no collision confirmation this time
    already_checked = ReproductionTarget(model="project.meerwerk", field_name="amount_total")
    confirmed, notes = asyncio.run(
        specialist._reverify_earlier_constraints_field_targets(contract, "oma_test", "odoo16_dev", already_checked)
    )
    assert confirmed is False
    assert "genuinely_removed_field" in notes
    print("PASS: a genuine earlier-constraint regression is still correctly reported")


def test_verify_security_access_claim_not_applicable_case_is_never_called():
    """Callers should never invoke the verifier for a non-applicable
    claim -- this test just documents that applicable=False claims are
    never passed to _verify_security_access_claim in run(); the field
    itself has no meaning here, so this is a smoke check on the schema.
    """
    claim = SecurityAccessClaim(applicable=False)
    assert claim.applicable is False
    print("PASS: applicable=False claim constructs cleanly, nothing to verify")


def test_verify_security_access_claim_group_missing_fails(monkeypatch):
    def fake_check_group_exists(db, group_name):
        return False
    monkeypatch.setattr(testing_qa_module, "check_group_exists", fake_check_group_exists)

    specialist = _make_specialist()
    claim = SecurityAccessClaim(applicable=True, group_name="Ghost Group", model="res.partner")
    passed, notes = asyncio.run(specialist._verify_security_access_claim(claim, "odoo16_dev"))
    assert passed is False
    assert "Ghost Group" in notes
    print("PASS: a claimed group that doesn't exist in the real registry fails verification")


def test_verify_security_access_claim_group_exists_no_perm_claim_passes(monkeypatch):
    def fake_check_group_exists(db, group_name):
        return True
    monkeypatch.setattr(testing_qa_module, "check_group_exists", fake_check_group_exists)

    specialist = _make_specialist()
    claim = SecurityAccessClaim(applicable=True, group_name="Real Group")
    passed, notes = asyncio.run(specialist._verify_security_access_claim(claim, "odoo16_dev"))
    assert passed is True
    print("PASS: a claimed group that exists, with no specific permission claim, passes on existence alone")


def test_verify_security_access_claim_group_wrong_permissions_fails(monkeypatch):
    def fake_check_group_exists(db, group_name):
        return True
    def fake_check_group_model_access(db, group_name, model, expects_read, expects_write, expects_create, expects_unlink):
        return False, "permission mismatch: delete: expected True, actual False"
    monkeypatch.setattr(testing_qa_module, "check_group_exists", fake_check_group_exists)
    monkeypatch.setattr(testing_qa_module, "check_group_model_access", fake_check_group_model_access)

    specialist = _make_specialist()
    claim = SecurityAccessClaim(
        applicable=True, group_name="Real Group", model="res.partner",
        expects_read=True, expects_write=True, expects_create=True, expects_unlink=True,
    )
    passed, notes = asyncio.run(specialist._verify_security_access_claim(claim, "odoo16_dev"))
    assert passed is False
    assert "mismatch" in notes
    print("PASS: a group with the wrong real permissions fails verification even though it exists")


def test_verify_security_access_claim_group_correct_permissions_passes(monkeypatch):
    def fake_check_group_exists(db, group_name):
        return True
    def fake_check_group_model_access(db, group_name, model, expects_read, expects_write, expects_create, expects_unlink):
        return True, "matches all claimed permissions"
    monkeypatch.setattr(testing_qa_module, "check_group_exists", fake_check_group_exists)
    monkeypatch.setattr(testing_qa_module, "check_group_model_access", fake_check_group_model_access)

    specialist = _make_specialist()
    claim = SecurityAccessClaim(
        applicable=True, group_name="Real Group", model="res.partner",
        expects_read=True, expects_write=False, expects_create=False, expects_unlink=False,
    )
    passed, notes = asyncio.run(specialist._verify_security_access_claim(claim, "odoo16_dev"))
    assert passed is True
    print("PASS: a group with correct real permissions passes verification")


def test_verify_security_access_claim_field_restriction_wrong_group_fails(monkeypatch):
    def fake_check_field_group_restricted(db, model, field_name, group_name=None, group_xmlid=None):
        claimed = group_xmlid or group_name
        return False, f"{model}.{field_name} is restricted to ['Other Group'], not the claimed group {claimed!r}"
    monkeypatch.setattr(testing_qa_module, "check_field_group_restricted", fake_check_field_group_restricted)

    specialist = _make_specialist()
    claim = SecurityAccessClaim(
        applicable=True, model="res.partner", restricted_field_name="secondary_email", group_name="Real Group",
    )
    passed, notes = asyncio.run(specialist._verify_security_access_claim(claim, "odoo16_dev"))
    assert passed is False
    print("PASS: a field restricted to the wrong group fails verification")


def test_verify_security_access_claim_field_restriction_correct_passes(monkeypatch):
    def fake_check_field_group_restricted(db, model, field_name, group_name=None, group_xmlid=None):
        claimed = group_xmlid or group_name
        return True, f"{model}.{field_name} is correctly restricted to {claimed!r}"
    monkeypatch.setattr(testing_qa_module, "check_field_group_restricted", fake_check_field_group_restricted)

    specialist = _make_specialist()
    claim = SecurityAccessClaim(
        applicable=True, model="res.partner", restricted_field_name="secondary_email", group_name="Real Group",
    )
    passed, notes = asyncio.run(specialist._verify_security_access_claim(claim, "odoo16_dev"))
    assert passed is True
    print("PASS: a field correctly restricted to the claimed group passes verification")


def test_verify_security_access_claim_genuine_uncertainty_fails_conservatively(monkeypatch):
    """A None result (SSH/timeout/genuine uncertainty) must NEVER be
    treated as passing -- recreating the exact false-positive gap this
    whole fix exists to close.
    """
    def fake_check_group_exists(db, group_name):
        return None
    monkeypatch.setattr(testing_qa_module, "check_group_exists", fake_check_group_exists)

    specialist = _make_specialist()
    claim = SecurityAccessClaim(applicable=True, group_name="Real Group")
    passed, notes = asyncio.run(specialist._verify_security_access_claim(claim, "odoo16_dev"))
    assert passed is False
    assert "uncertainty" in notes.lower() or "could not verify" in notes.lower()
    print("PASS: genuine uncertainty (None) is treated as NOT passed, never guessed as success")


def test_has_remaining_decomposed_constraints_true_with_labels():
    """Real bug found live (2026-07-20): the security-access claim
    check used to run unconditionally on EVERY round, including
    intermediate rounds of a decomposed task -- which by design only
    implements ONE constraint per round. A task shaped 'new model + new
    group + access rule' correctly has NO group yet on round 1's own
    'model' constraint; the security-claim check reporting that as a
    failure was itself the real bug, not Code-Review.
    """
    goal = (
        "Create a small new Odoo module...\n\n"
        "This round's own NEW focus is ONLY: 'service_ticket_model'. "
        "The following constraints are NOT yet in scope for this round and must NOT be "
        "implemented even partially: ['ticket_managers_group', 'ticket_access_rule']."
    )
    assert _has_remaining_decomposed_constraints(goal) is True
    print("PASS: a goal naming remaining not-yet-in-scope constraints is correctly detected")


def test_has_remaining_decomposed_constraints_false_for_plain_task():
    goal = "Add a single new field 'preferred_language' to res.partner."
    assert _has_remaining_decomposed_constraints(goal) is False
    print("PASS: a plain, non-decomposed task's goal correctly has no remaining constraints")


def test_own_focus_gate_never_skips_the_current_rounds_own_real_claim():
    """Real, confirmed bug found live (2026-07-28, Phase 28C,
    school_student task, menu_structure round): `_has_remaining_
    decomposed_constraints` alone answers "are there OTHER constraints
    somewhere after this one" -- true on every round except the very
    last. Gating a claim-check skip on that ALONE means the
    menu_structure round's own real, current menu-structure claim got
    silently skipped (defaulting menu_check_passed=True) purely
    because security_groups/record_rules/etc still existed LATER in
    the plan -- the exact "trivially, simply pass" shortcut explicitly
    ruled out for this work. `_is_this_rounds_own_focus` is the real
    fix: it must return True (never skip) whenever the round's own
    parsed focus label genuinely matches the claim's own shape,
    regardless of how many other constraints remain further down the
    plan.
    """
    goal = (
        "Build school_student...\n\n"
        "This round's own NEW focus is ONLY: 'menu_structure'. "
        "The following constraints are NOT yet in scope for this round: "
        "['security_groups', 'record_rules', 'computed_age_field', 'demo_data', 'automated_tests']."
    )
    focus_label = _this_rounds_own_focus_label(goal)
    assert focus_label == "menu_structure"
    assert _has_remaining_decomposed_constraints(goal) is True  # other constraints DO remain...
    assert _is_this_rounds_own_focus(focus_label, _MENU_FOCUS_KEYWORD_RE) is True  # ...but this IS the menu round
    assert _is_this_rounds_own_focus(focus_label, _SECURITY_FOCUS_KEYWORD_RE) is False  # not the security round
    print("PASS: the menu_structure round's own real claim is never skipped just because later "
          "constraints remain -- the true root cause of the live false-skip is closed")


def test_own_focus_gate_still_skips_a_genuinely_later_rounds_claim():
    goal = (
        "Build school_student...\n\n"
        "This round's own NEW focus is ONLY: 'student_model_fields'. "
        "The following constraints are NOT yet in scope for this round: "
        "['student_views', 'menu_structure', 'security_groups']."
    )
    focus_label = _this_rounds_own_focus_label(goal)
    assert focus_label == "student_model_fields"
    assert _is_this_rounds_own_focus(focus_label, _MENU_FOCUS_KEYWORD_RE) is False
    assert _is_this_rounds_own_focus(focus_label, _SECURITY_FOCUS_KEYWORD_RE) is False
    print("PASS: a claim about a genuinely later, not-yet-reached round is still correctly skipped")


def test_own_focus_gate_always_verifies_a_non_decomposed_task():
    goal = "Restrict field 'salary' to group 'HR Managers' on res.partner."
    focus_label = _this_rounds_own_focus_label(goal)
    assert focus_label is None
    assert _is_this_rounds_own_focus(focus_label, _SECURITY_FOCUS_KEYWORD_RE) is True
    print("PASS: a plain, non-decomposed task's own claim is always verified, never skipped")


def test_verify_security_access_claim_button_restriction_wrong_group_fails(monkeypatch):
    """Real, general fix (2026-07-25, task 008): a button-restriction
    claim (SHAPE 3) was completely uncovered by every existing
    verification path before this fix -- neither the field-existence
    reproduction check (a button will never appear in fields_get()'s
    own field list) nor the field-visibility security-claim check
    (which only reads an ORM field's own `.groups` attribute, not a
    view's `<button groups="...">` XML attribute) could ever confirm a
    genuinely correct button-restriction fix. Confirmed live: a real
    task restricting the "Send to customer" button to `base.group_system`
    was reported failing identically across 5 straight rounds despite
    Code-Review independently confirming the fix was correct.
    """
    import tools_odoo.odoo_schema_client as schema_client_module

    def fake_check_button_group_restricted(db, model, button_name, group_xmlid, group_name):
        return False, f"button {button_name!r} on {model!r} is restricted to ['Other Group'], not {group_xmlid or group_name!r}"
    monkeypatch.setattr(schema_client_module, "check_button_group_restricted_fast", fake_check_button_group_restricted)

    specialist = _make_specialist()
    claim = SecurityAccessClaim(
        applicable=True, model="project.meerwerk", restricted_button_name="action_send",
        restricted_group_xmlid="base.group_system",
    )
    passed, notes = asyncio.run(
        specialist._verify_security_access_claim(claim, "odoo16_dev", task_id="test-task-008")
    )
    assert passed is False
    print("PASS: a button restricted to the wrong group fails verification")


def test_verify_security_access_claim_button_restriction_correct_passes(monkeypatch):
    import tools_odoo.odoo_schema_client as schema_client_module

    def fake_check_button_group_restricted(db, model, button_name, group_xmlid, group_name):
        return True, f"button {button_name!r} on {model!r} is correctly restricted to {group_xmlid!r}"
    monkeypatch.setattr(schema_client_module, "check_button_group_restricted_fast", fake_check_button_group_restricted)

    specialist = _make_specialist()
    claim = SecurityAccessClaim(
        applicable=True, model="project.meerwerk", restricted_button_name="action_send",
        restricted_group_xmlid="base.group_system",
    )
    passed, notes = asyncio.run(
        specialist._verify_security_access_claim(claim, "odoo16_dev", task_id="test-task-008")
    )
    assert passed is True
    print("PASS: a button correctly restricted to the claimed group passes verification")


def test_verify_security_access_claim_button_restriction_uncertain_fails_conservatively(monkeypatch):
    """P12 Tier A item 12 (2026-08-01): updated for the new odoo-bin-shell fallback --
    no task_id/fast-path means the fast path is never attempted, but the dispatcher now
    correctly falls back to check_button_group_restricted() (the real slow path) instead of
    unconditionally reporting uncertainty. When that fallback itself returns None (genuine
    uncertainty, e.g. an infra failure), the overall claim must still fail conservatively --
    same posture as every sibling check, just reached via the new fallback instead of skipping
    it entirely.
    """
    def fake_check_button_group_restricted(db, model, button_name, group_xmlid, group_name):
        return None  # genuine uncertainty from the slow path itself
    monkeypatch.setattr(testing_qa_module, "check_button_group_restricted", fake_check_button_group_restricted)

    specialist = _make_specialist()
    claim = SecurityAccessClaim(
        applicable=True, model="project.meerwerk", restricted_button_name="action_send",
        restricted_group_xmlid="base.group_system",
    )
    passed, notes = asyncio.run(specialist._verify_security_access_claim(claim, "odoo16_dev", task_id=None))
    assert passed is False
    assert "uncertainty" in notes.lower() or "could not verify" in notes.lower()
    print("PASS: genuine uncertainty from the new shell fallback itself is still treated as NOT passed")


def test_reproduction_field_check_skipped_for_a_button_restriction_claim():
    """The field-existence reproduction check (`_check_field_exists_on_
    model`) must never gate `passed` for a button-restriction task --
    its own target (a button name) will NEVER satisfy a field-existence
    check, since a button is not an ORM field. Confirmed live: task 008
    reported "Reproduction FAILED for project.meerwerk.action_send"
    identically across 5 rounds despite a genuinely correct fix,
    entirely because this check was running against the wrong target.
    This test locks in that a button-restriction claim is correctly
    detected so the skip logic in run() actually fires (the skip
    condition itself: `security_claim.applicable and security_claim.
    restricted_button_name`).
    """
    claim = SecurityAccessClaim(
        applicable=True, model="project.meerwerk", restricted_button_name="action_send",
        restricted_group_xmlid="base.group_system",
    )
    assert claim.applicable and claim.restricted_button_name, (
        "a button-restriction claim must be detected as applicable with restricted_button_name set, "
        "so run()'s own skip condition for the field-existence reproduction check actually fires"
    )
    print("PASS: a button-restriction claim is correctly shaped to trigger the reproduction-check skip")


def test_has_remaining_decomposed_constraints_false_when_list_is_empty():
    """The final round of a decomposed task -- genuinely nothing left,
    matching manager/loop.py's own _reconstruct_resume_order logic for
    the identical marker.
    """
    goal = (
        "This round's own NEW focus is ONLY: 'ticket_access_rule'. "
        "The following constraints are NOT yet in scope for this round: []."
    )
    assert _has_remaining_decomposed_constraints(goal) is False
    print("PASS: an empty remaining-constraints list (final round) is correctly treated as none left")


if __name__ == "__main__":
    test_has_remaining_decomposed_constraints_true_with_labels()
    test_has_remaining_decomposed_constraints_false_for_plain_task()
    test_own_focus_gate_never_skips_the_current_rounds_own_real_claim()
    test_own_focus_gate_still_skips_a_genuinely_later_rounds_claim()
    test_own_focus_gate_always_verifies_a_non_decomposed_task()
    test_has_remaining_decomposed_constraints_false_when_list_is_empty()
    test_autocorrects_hallucinated_field_to_real_pluralized_field()
    test_leaves_target_unchanged_when_field_genuinely_real()
    test_leaves_target_unchanged_when_ambiguous()
    test_leaves_target_unchanged_when_no_candidate_is_actually_used_in_code()
    test_autocorrects_hallucinated_field_with_zero_stem_overlap_to_the_one_real_defined_field()
    test_does_not_autocorrect_to_an_unrelated_field_when_goal_literally_names_the_claimed_field()
    test_does_not_autocorrect_when_module_defines_multiple_new_fields()
    test_autocorrects_cross_model_when_goal_names_the_real_owning_model()
    test_cross_model_tier_never_fires_without_goal_text()
    test_cross_model_tier_never_guesses_when_multiple_models_have_the_field()
    test_verify_security_access_claim_not_applicable_case_is_never_called()
    test_verify_security_access_claim_group_missing_fails()
    test_verify_security_access_claim_group_exists_no_perm_claim_passes()
    test_verify_security_access_claim_group_wrong_permissions_fails()
    test_verify_security_access_claim_group_correct_permissions_passes()
    test_verify_security_access_claim_field_restriction_wrong_group_fails()
    test_verify_security_access_claim_field_restriction_correct_passes()
    test_verify_security_access_claim_genuine_uncertainty_fails_conservatively()
    test_verify_security_access_claim_button_restriction_wrong_group_fails()
    test_verify_security_access_claim_button_restriction_correct_passes()
    test_verify_security_access_claim_button_restriction_uncertain_fails_conservatively()
    test_reproduction_field_check_skipped_for_a_button_restriction_claim()
    print("\nALL TESTING/QA REPRODUCTION-TARGET AUTOCORRECT TESTS PASSED")
