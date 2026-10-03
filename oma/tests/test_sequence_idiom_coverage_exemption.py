"""Phase 25C: tests for
specialists.testing_qa.specialist._exempt_verified_sequence_idiom_from_coverage_gap()
-- pure, offline, dependency-free (no live gateway/DB needed).

Real, severe bug found live (2026-07-25, Phase 25C's own regression
gate, task 006's resubmission): tools_odoo.spot_check.run_coverage_and_diff()
measures INSTALL-time line execution only -- a plain `-i module
--stop-after-init` run never calls create() at all, so the deterministically
-injected, provably-correct standard Odoo sequence-assignment idiom
(specialists/build/specialist.py's own `_autofix_goal_named_sequence_field_
missing()`) was flagged as a coverage gap purely because nothing in this
project's own verification methodology ever creates a real record to
exercise it -- a structural blind spot in the coverage tool, not a real,
unverified gap in the code.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from specialists.testing_qa.specialist import (
    _exempt_verified_onchange_idiom_from_coverage_gap,
    _exempt_verified_sequence_idiom_from_coverage_gap,
    _exempt_verified_state_button_idiom_from_coverage_gap,
    _exempt_verified_sum_compute_idiom_from_coverage_gap,
)

_STANDARD_IDIOM_MODELS_PY = (
    "from odoo import api, fields, models\n\n"
    "class ProjectFieldjob(models.Model):\n"
    "    _inherit = 'project.fieldjob'\n"
    "    name = fields.Char(string='Name', default='New', copy=False, readonly=True)\n"
    "\n"
    "    @api.model_create_multi\n"
    "    def create(self, vals_list):\n"
    "        for vals in vals_list:\n"
    "            if vals.get('name', 'New') == 'New':\n"
    "                vals['name'] = self.env['ir.sequence'].next_by_code('project.fieldjob') or 'New'\n"
    "        return super().create(vals_list)\n"
)


def test_exempts_lines_inside_a_confirmed_standard_sequence_idiom():
    module_files = {"/mnt/extra-addons/oma_x/models/models.py": _STANDARD_IDIOM_MODELS_PY}
    # Real, live-observed shape: lines 9-12 (the create() method's own
    # for/if/assignment/return body) reported as uncovered.
    uncovered = [
        "models/models.py:9", "models/models.py:10", "models/models.py:11", "models/models.py:12",
    ]
    result = _exempt_verified_sequence_idiom_from_coverage_gap(module_files, uncovered)
    assert result == [], f"expected every line inside the confirmed standard idiom's create() body to be exempted, got: {result!r}"
    print("PASS: lines inside a confirmed standard sequence-assignment idiom's create() body are exempted")


def test_never_exempts_lines_outside_the_create_method():
    module_files = {"/mnt/extra-addons/oma_x/models/models.py": _STANDARD_IDIOM_MODELS_PY}
    # Line 1 (the import line) is a real, separate, genuinely-uncovered
    # gap having nothing to do with the sequence idiom -- must survive.
    uncovered = ["models/models.py:1", "models/models.py:9"]
    result = _exempt_verified_sequence_idiom_from_coverage_gap(module_files, uncovered)
    assert result == ["models/models.py:1"], f"expected only the in-idiom line exempted, got: {result!r}"
    print("PASS: a genuinely unrelated uncovered line outside create() is never exempted")


def test_never_exempts_anything_when_the_idiom_is_not_genuinely_present():
    """A create() override that does NOT match the exact, confirmed
    standard idiom (e.g. missing the real ir.sequence call) must never
    have any of its lines exempted -- this function must never guess
    that an unrelated create() override is "the same safe pattern."
    """
    non_idiom_models_py = (
        "from odoo import models, fields\n\n"
        "class X(models.Model):\n    _inherit = 'x'\n\n"
        "    def create(self, vals_list):\n"
        "        # some other, unrelated custom logic\n"
        "        return super().create(vals_list)\n"
    )
    module_files = {"/mnt/extra-addons/oma_x/models/models.py": non_idiom_models_py}
    uncovered = ["models/models.py:6", "models/models.py:7"]
    result = _exempt_verified_sequence_idiom_from_coverage_gap(module_files, uncovered)
    assert result == uncovered, "must never exempt anything unless the exact standard idiom is confirmed present"
    print("PASS: a non-standard create() override never gets any lines exempted")


def test_never_exempts_anything_when_there_is_no_models_py_at_all():
    result = _exempt_verified_sequence_idiom_from_coverage_gap({}, ["models/models.py:9"])
    assert result == ["models/models.py:9"]
    print("PASS: no models.py found -- conservative, no exemption applied")


# Real, exact generated code from a real live failure (2026-08-08, the "flagship field-service"
# task's own `equipment_registry` node, escalated after 2 identical, non-convergent rounds): a
# field named `tracking_number` (not `name`), guarded via `'X' not in vals or not vals['X']`
# (not `== 'New'`), using the singular, classic `def create(self, vals):` signature (not
# `vals_list`). A REAL behavioral probe had already, independently, dynamically confirmed this
# exact code correctly auto-generated a real tracking number (`{'tracking_number': 'EQ-00001'}`)
# -- yet the coverage exemption never fired because it only ever recognized the ONE literal
# `name`/`'New'` textbook shape, not this equally valid, equally standard general idiom.
_GENERAL_FIELD_IDIOM_MODELS_PY = (
    "from odoo import models, fields, api\n\n"
    "class Equipment(models.Model):\n"
    "    _name = 'oma.equipment'\n"
    "    _description = 'Equipment Registry'\n\n"
    "    name = fields.Char(required=True, string='Name')\n"
    "    tracking_number = fields.Char(string='Tracking Number', required=True, copy=False, index=True)\n"
    "    condition_rating = fields.Selection([\n"
    "        ('excellent', 'Excellent'),\n"
    "        ('good', 'Good'),\n"
    "        ('fair', 'Fair'),\n"
    "        ('poor', 'Poor')\n"
    "    ], string='Condition Rating', default='good')\n"
    "\n"
    "    _sql_constraints = [\n"
    "        ('tracking_number_unique', 'UNIQUE(tracking_number)', 'Each equipment must have a unique tracking number!')\n"
    "    ]\n\n"
    "    @api.model\n"
    "    def create(self, vals):\n"
    "        if 'tracking_number' not in vals or not vals['tracking_number']:\n"
    "            vals['tracking_number'] = self.env['ir.sequence'].next_by_code('oma.equipment')\n"
    "        return super().create(vals)\n"
)


def test_exempts_lines_inside_a_general_field_name_sequence_idiom():
    module_files = {"models/models.py": _GENERAL_FIELD_IDIOM_MODELS_PY}
    # The exact real coverage_diff observed live: models.py 13 statements, 3 missed, lines 22-24
    # (the create() method's own if/assignment/return body).
    uncovered = ["models/models.py:22", "models/models.py:23", "models/models.py:24"]
    result = _exempt_verified_sequence_idiom_from_coverage_gap(module_files, uncovered)
    assert result == [], (
        f"expected every line inside the confirmed general-field-name sequence idiom's create() "
        f"body to be exempted (a REAL behavioral probe had already confirmed this exact code "
        f"correct), got: {result!r}"
    )
    print("PASS: lines inside a confirmed general-field-name (not just 'name') sequence idiom's "
          "create() body are exempted, closing the real live gap this incident found")


def test_general_field_idiom_never_exempts_when_guard_and_assignment_name_different_fields():
    """The guard and the next_by_code() assignment must name the SAME field -- an unrelated
    guard on one field paired with a next_by_code() assignment on a DIFFERENT field is not the
    verified-safe idiom and must never be exempted (this is exactly what stops the general
    pattern from being too permissive).
    """
    mismatched_models_py = (
        "from odoo import models, fields, api\n\n"
        "class X(models.Model):\n"
        "    _name = 'oma.x'\n"
        "    other_field = fields.Char()\n"
        "    tracking_number = fields.Char()\n\n"
        "    @api.model\n"
        "    def create(self, vals):\n"
        "        if 'other_field' not in vals:\n"
        "            vals['tracking_number'] = self.env['ir.sequence'].next_by_code('oma.x')\n"
        "        return super().create(vals)\n"
    )
    module_files = {"models/models.py": mismatched_models_py}
    uncovered = ["models/models.py:8", "models/models.py:9"]
    result = _exempt_verified_sequence_idiom_from_coverage_gap(module_files, uncovered)
    assert result == uncovered, (
        "a guard on one field paired with a next_by_code() assignment on a DIFFERENT, "
        "unrelated field must never be exempted -- that's not the verified-safe idiom"
    )
    print("PASS: a guard/assignment pair naming DIFFERENT fields is never exempted, keeping "
          "the general widening precise")


# Real, exact generated code from a real live failure (2026-08-09, the "flagship field-service"
# task's own `service_ticket_model` node, escalated after Code-Review's own two hallucinated
# blocking findings were fixed): a models.py with TWO models, each with its own idiomatic
# sequence-assignment create() -- `_CREATE_METHOD_BLOCK_RE.search()` (singular) only ever found
# the FIRST one (ServiceTicket's), leaving Equipment's create() -- an equally valid, equally
# idiomatic, DIFFERENT create() override further down the same file -- still flagged.
_TWO_CREATE_METHODS_MODELS_PY = (
    "from odoo import models, fields, api\n\n"
    "class ServiceTicket(models.Model):\n"
    "    _name = 'oma.service.ticket'\n\n"
    "    name = fields.Char(string='Ticket Number', required=True, copy=False, index=True)\n\n"
    "    @api.model_create_multi\n"
    "    def create(self, vals_list):\n"
    "        for vals in vals_list:\n"
    "            if not vals.get('name'):\n"
    "                vals['name'] = self.env['ir.sequence'].next_by_code('oma.service.ticket')\n"
    "        return super().create(vals_list)\n"
    "\n\n"
    "class Equipment(models.Model):\n"
    "    _name = 'oma.equipment'\n\n"
    "    tracking_number = fields.Char(string='Tracking Number', required=True, copy=False)\n\n"
    "    @api.model\n"
    "    def create(self, vals):\n"
    "        if 'tracking_number' not in vals:\n"
    "            vals['tracking_number'] = self.env['ir.sequence'].next_by_code('oma.equipment')\n"
    "        return super().create(vals)\n"
)


def test_exempts_every_create_method_when_a_file_has_more_than_one():
    module_files = {"models/models.py": _TWO_CREATE_METHODS_MODELS_PY}
    # ServiceTicket's create() body (lines 10-13) AND Equipment's create() body (lines 22-25),
    # both genuinely idiomatic, both previously left half-exempted by the old search()-based
    # single-match behavior.
    uncovered = [
        "models/models.py:10", "models/models.py:11", "models/models.py:12", "models/models.py:13",
        "models/models.py:22", "models/models.py:23", "models/models.py:24", "models/models.py:25",
    ]
    result = _exempt_verified_sequence_idiom_from_coverage_gap(module_files, uncovered)
    assert result == [], (
        f"expected BOTH create() methods' idiomatic bodies exempted, not just the first one "
        f"found -- got: {result!r}"
    )
    print("PASS: every idiomatic create() method in a multi-model file is exempted, not just "
          "the first one, closing the real live gap found on task 07141af5's "
          "service_ticket_model node")


def test_second_create_method_still_exempt_when_first_is_non_idiomatic():
    """The per-block idiom check must be independent -- a non-idiomatic first create() must
    never suppress exemption of a genuinely idiomatic second one.
    """
    mixed_models_py = _TWO_CREATE_METHODS_MODELS_PY.replace(
        "if not vals.get('name'):\n                vals['name'] = self.env['ir.sequence'].next_by_code('oma.service.ticket')",
        "vals['name'] = 'unconditional, non-idiomatic write'",
    )
    module_files = {"models/models.py": mixed_models_py}
    uncovered = ["models/models.py:11", "models/models.py:23"]
    result = _exempt_verified_sequence_idiom_from_coverage_gap(module_files, uncovered)
    assert "models/models.py:11" in result, "the non-idiomatic first create() line must stay uncovered"
    assert "models/models.py:23" not in result, "the second, genuinely idiomatic create() must still be exempted"
    print("PASS: each create() block is checked independently -- a non-idiomatic first block "
          "never suppresses exemption of a genuinely idiomatic second block")


_STATE_BUTTON_MODELS_PY = (
    "from odoo import models, fields, api\n\n"
    "class ServiceTicket(models.Model):\n"
    "    _name = 'oma.service.ticket'\n\n"
    "    state = fields.Selection([('new', 'New'), ('assigned', 'Assigned')], default='new')\n\n"
    "    def action_assign(self):\n"
    "        self.ensure_one()\n"
    "        if self.state == 'new':\n"
    "            self.state = 'assigned'\n\n"
    "    def action_start(self):\n"
    "        self.ensure_one()\n"
    "        if self.state == 'assigned':\n"
    "            self.state = 'in_progress'\n"
)


def test_exempts_lines_inside_confirmed_state_button_methods():
    """Real, confirmed bug found live (2026-08-09, task 07141af5's flagship run,
    service_ticket_model node): four workflow button methods
    (action_assign/action_start/action_resolve/action_close), each the standard Odoo
    ensure_one()+guarded-state-transition idiom, were flagged as a coverage gap purely because
    nothing in this project's own install-time verification methodology clicks a button --
    driving a real, needless ask_operator escalation even though the code was genuinely correct.
    """
    module_files = {"models/models.py": _STATE_BUTTON_MODELS_PY}
    # Real live shape: lines 8-11 (action_assign's body) and 13-16 (action_start's body).
    uncovered = [f"models/models.py:{n}" for n in (8, 9, 10, 11, 13, 14, 15, 16)]
    result = _exempt_verified_state_button_idiom_from_coverage_gap(module_files, uncovered)
    assert result == [], (
        f"expected every line inside both confirmed state-button methods to be exempted, got: {result!r}"
    )
    print("PASS: lines inside confirmed ensure_one()+guarded-state-transition button methods "
          "are exempted, closing the real live gap found on task 07141af5's "
          "service_ticket_model node")


def test_state_button_exemption_never_touches_lines_outside_the_methods():
    module_files = {"models/models.py": _STATE_BUTTON_MODELS_PY}
    uncovered = ["models/models.py:1", "models/models.py:8"]
    result = _exempt_verified_state_button_idiom_from_coverage_gap(module_files, uncovered)
    assert result == ["models/models.py:1"], f"expected only the in-method line exempted, got: {result!r}"
    print("PASS: a genuinely unrelated uncovered line outside the button methods is never exempted")


def test_state_button_exemption_never_fires_on_a_method_with_extra_logic():
    """A button method doing anything beyond the exact ensure_one()+guarded-state-transition
    shape (a real side effect, an extra field write, a non-state condition) must never be
    exempted -- that's a genuinely different method that could actually be broken.
    """
    extra_logic_models_py = (
        "from odoo import models, fields, api\n\n"
        "class ServiceTicket(models.Model):\n"
        "    _name = 'oma.service.ticket'\n\n"
        "    def action_close(self):\n"
        "        self.ensure_one()\n"
        "        if self.state == 'resolved':\n"
        "            self.state = 'closed'\n"
        "            self.message_post(body='Ticket closed')\n"
    )
    module_files = {"models/models.py": extra_logic_models_py}
    uncovered = ["models/models.py:7", "models/models.py:8", "models/models.py:9", "models/models.py:10"]
    result = _exempt_verified_state_button_idiom_from_coverage_gap(module_files, uncovered)
    assert result == uncovered, "a method with extra logic beyond the exact idiom must never be exempted"
    print("PASS: a button method with any extra logic beyond the exact idiom is never exempted")


_SUM_COMPUTE_MODELS_PY = (
    "from odoo import api, fields, models\n\n"
    "class ProjectFieldjob(models.Model):\n"
    "    _inherit = 'project.fieldjob'\n"
    "    amount_total = fields.Monetary(string='Amount Total', "
    "compute='_compute_amount_total', store=True)\n\n"
    "    @api.depends('line_ids.price_unit')\n"
    "    def _compute_amount_total(self):\n"
    "        for record in self:\n"
    "            record.amount_total = sum(record.line_ids.mapped('price_unit'))\n"
)
_SUM_COMPUTE_GOAL = (
    "On the fieldjob record, I want to see the total of all line prices shown automatically in "
    "the header.\n\nModule: project_fieldjob\nModel: project.fieldjob\n"
    "Field: amount_total (Monetary, compute=_compute_amount_total, depends on "
    "line_ids.price_unit, store=True)\n"
)


def test_exempts_lines_inside_a_confirmed_sum_compute_idiom():
    """Phase 25D (2026-07-26): the sibling exemption -- confirmed live,
    task 004's own resubmission still failed purely on this exact
    coverage blind spot, even after every real Code-Review false
    positive about the same field had already been closed.
    """
    module_files = {"/mnt/extra-addons/oma_x/models/models.py": _SUM_COMPUTE_MODELS_PY}
    # Lines 8-10: "def _compute_amount_total(self):" through the return
    # body -- the method block itself; line 7 (the @api.depends
    # decorator) is outside the block regex, same as its sibling.
    uncovered = [
        "models/models.py:8", "models/models.py:9", "models/models.py:10",
    ]
    result = _exempt_verified_sum_compute_idiom_from_coverage_gap(module_files, _SUM_COMPUTE_GOAL, uncovered)
    assert result == [], f"expected every line inside the confirmed compute method body to be exempted, got: {result!r}"
    print("PASS: lines inside a confirmed sum-compute idiom's method body are exempted")


def test_sum_compute_exemption_never_touches_lines_outside_the_method():
    module_files = {"/mnt/extra-addons/oma_x/models/models.py": _SUM_COMPUTE_MODELS_PY}
    uncovered = ["models/models.py:1", "models/models.py:8"]
    result = _exempt_verified_sum_compute_idiom_from_coverage_gap(module_files, _SUM_COMPUTE_GOAL, uncovered)
    assert result == ["models/models.py:1"], f"expected only the in-method line exempted, got: {result!r}"
    print("PASS: a genuinely unrelated uncovered line outside the compute method is never exempted")


def test_sum_compute_exemption_never_fires_when_dependency_does_not_match():
    """The ground-truth guard: a compute method whose real @api.depends
    does NOT match the goal's own stated dependency (a genuinely
    different or incorrect implementation) must never be exempted.
    """
    wrong_models_py = _SUM_COMPUTE_MODELS_PY.replace("line_ids.price_unit", "line_ids.quantity")
    module_files = {"/mnt/extra-addons/oma_x/models/models.py": wrong_models_py}
    uncovered = ["models/models.py:7", "models/models.py:8"]
    result = _exempt_verified_sum_compute_idiom_from_coverage_gap(module_files, _SUM_COMPUTE_GOAL, uncovered)
    assert result == uncovered, "must never exempt when the real dependency doesn't match the goal's stated one"
    print("PASS: never exempted when the real code's dependency doesn't match the goal's stated one")


def test_sum_compute_exemption_never_fires_without_a_concrete_compute_goal():
    module_files = {"/mnt/extra-addons/oma_x/models/models.py": _SUM_COMPUTE_MODELS_PY}
    uncovered = ["models/models.py:7"]
    result = _exempt_verified_sum_compute_idiom_from_coverage_gap(
        module_files, "Add a plain field.\n\nField: x (Text)\n", uncovered,
    )
    assert result == uncovered, "must never exempt anything without a concrete compute=/depends-on goal shape"
    print("PASS: never exempted without a concrete compute=/depends-on goal shape")


_ONCHANGE_MODELS_PY = (
    "from odoo import api, fields, models\n\n"
    "class ProjectFieldjob(models.Model):\n"
    "    _inherit = 'project.fieldjob'\n\n"
    "    @api.onchange('project_id')\n"
    "    def _onchange_project_id(self):\n"
    "        if self.project_id and self.project_id.user_id:\n"
    "            self.user_id = self.project_id.user_id\n"
    "        elif not self.project_id:\n"
    "            self.user_id = False\n"
)
_ONCHANGE_GOAL = (
    "When I select a project on the fieldjob form, I want the 'Assigned to' field to "
    "automatically fill with the project manager.\n\n"
    "Module: project_fieldjob\nModel: project.fieldjob\n"
    "Trigger field: project_id\nTarget field: user_id\nMethod name: _onchange_project_id\n"
)


def test_exempts_lines_inside_a_confirmed_onchange_idiom():
    """Phase 25D (2026-07-26): confirmed live, task 005's own
    resubmission -- the same install-time-only coverage blind spot as
    the sum-compute/sequence idioms, for @api.onchange methods: nothing
    in this project's own verification methodology fills in a form
    field to trigger an onchange during a plain install.
    """
    module_files = {"/mnt/extra-addons/oma_x/models/models.py": _ONCHANGE_MODELS_PY}
    # Lines 7-11: "def _onchange_project_id(self):" through the last
    # elif branch -- the method block itself (line 6, the decorator, is
    # outside the block regex, same as its create()/compute siblings).
    uncovered = [f"models/models.py:{n}" for n in range(7, 12)]
    result = _exempt_verified_onchange_idiom_from_coverage_gap(module_files, _ONCHANGE_GOAL, uncovered)
    assert result == [], f"expected every line inside the confirmed onchange method body to be exempted, got: {result!r}"
    print("PASS: lines inside a confirmed @api.onchange idiom's method body are exempted")


def test_onchange_exemption_never_touches_lines_outside_the_method():
    module_files = {"/mnt/extra-addons/oma_x/models/models.py": _ONCHANGE_MODELS_PY}
    uncovered = ["models/models.py:1", "models/models.py:7"]
    result = _exempt_verified_onchange_idiom_from_coverage_gap(module_files, _ONCHANGE_GOAL, uncovered)
    assert result == ["models/models.py:1"], f"expected only the in-method line exempted, got: {result!r}"
    print("PASS: a genuinely unrelated uncovered line outside the onchange method is never exempted")


def test_onchange_exemption_never_fires_when_trigger_does_not_match():
    """The ground-truth guard: an onchange decorated with a DIFFERENT
    trigger field than the goal states must never be exempted.
    """
    wrong_models_py = _ONCHANGE_MODELS_PY.replace("@api.onchange('project_id')", "@api.onchange('partner_id')")
    module_files = {"/mnt/extra-addons/oma_x/models/models.py": wrong_models_py}
    uncovered = ["models/models.py:6", "models/models.py:7"]
    result = _exempt_verified_onchange_idiom_from_coverage_gap(module_files, _ONCHANGE_GOAL, uncovered)
    assert result == uncovered, "must never exempt when the real trigger field doesn't match the goal's stated one"
    print("PASS: never exempted when the real trigger field doesn't match the goal's stated one")


def test_onchange_exemption_never_fires_without_a_concrete_onchange_goal():
    module_files = {"/mnt/extra-addons/oma_x/models/models.py": _ONCHANGE_MODELS_PY}
    uncovered = ["models/models.py:6"]
    result = _exempt_verified_onchange_idiom_from_coverage_gap(
        module_files, "Add a plain field.\n\nField: x (Text)\n", uncovered,
    )
    assert result == uncovered, "must never exempt anything without a concrete Method name:/Trigger field: goal shape"
    print("PASS: never exempted without a concrete Method name:/Trigger field: goal shape")


if __name__ == "__main__":
    test_exempts_lines_inside_a_confirmed_standard_sequence_idiom()
    test_never_exempts_lines_outside_the_create_method()
    test_never_exempts_anything_when_the_idiom_is_not_genuinely_present()
    test_never_exempts_anything_when_there_is_no_models_py_at_all()
    test_exempts_lines_inside_a_general_field_name_sequence_idiom()
    test_general_field_idiom_never_exempts_when_guard_and_assignment_name_different_fields()
    test_exempts_every_create_method_when_a_file_has_more_than_one()
    test_second_create_method_still_exempt_when_first_is_non_idiomatic()
    test_exempts_lines_inside_confirmed_state_button_methods()
    test_state_button_exemption_never_touches_lines_outside_the_methods()
    test_state_button_exemption_never_fires_on_a_method_with_extra_logic()
    test_exempts_lines_inside_a_confirmed_sum_compute_idiom()
    test_sum_compute_exemption_never_touches_lines_outside_the_method()
    test_sum_compute_exemption_never_fires_when_dependency_does_not_match()
    test_sum_compute_exemption_never_fires_without_a_concrete_compute_goal()
    test_exempts_lines_inside_a_confirmed_onchange_idiom()
    test_onchange_exemption_never_touches_lines_outside_the_method()
    test_onchange_exemption_never_fires_when_trigger_does_not_match()
    test_onchange_exemption_never_fires_without_a_concrete_onchange_goal()
    print("\nALL COVERAGE-EXEMPTION TESTS PASSED")
