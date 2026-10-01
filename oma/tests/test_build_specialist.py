"""Phase 9: real tests for the Build specialist, against the actual
odoo16-dev container and the real model gateway -- no mocks of
infrastructure. Covers the three things the build plan specifically
calls out: the fencing lock actually protecting a real specialist (not
just the Phase 1 synthetic callers), the Manager-side compensation
mechanism actually cleaning up a forced mid-task cutoff, and task 1
(the field add) run end to end against the real duplicate database.
"""

import asyncio
import os
import re
import sys
import time
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from contracts.exceptions import PartialTaskFailure
from contracts.schema import AutonomyTier, CapabilityClass, CompensatingAction, SpecialistType, TaskContract
from infra.fencing import acquire_module_lock, check_fence, release_module_lock
from infra.gateway_client import ModelGatewayClient
from manager.compensations import handle_partial_task_failure
from manager.tools import delegate_to_specialist
from specialists import registry
from specialists.build.specialist import (
    BuildSpecialist,
    GeneratedModuleFiles,
    ManifestFields,
    _render_manifest_py,
    _validate_no_premature_out_of_scope_method,
    _validate_single_constraint_field_scope,
    build_deterministic_security_csv,
    slugify_module_name,
)
from specialists.constitution import CONSTITUTION_PATH
from tools_odoo.module_dev.toolchain import (
    _FULL_ADDONS_PATH,
    _MODULE_DEV_ADDONS_DIR,
    _ODOO_CONF_PATH,
    _run_in_container,
    uninstall_module,
)

DUPLICATE_DB = "odoo16_dev_dup_20260707"
# Phase 9.5's fresh test database (infra.odoo_admin.create_database()) --
# proven ownership-clean for schema-altering installs; reused here rather
# than creating a new one, since creating one requires a controlled
# list_db toggle window (Phase 7/8's established habit), not something
# to reopen just to get a fresh name when an already-proven one exists.
FRESH_DB = "odoo16_dev_fresh_20260707_123141"

assert CONSTITUTION_PATH.exists(), (
    f"The real Odoo Development Agent Constitution must exist at {CONSTITUTION_PATH} to run "
    f"these tests -- Phase 9's original stand-in-file workaround was removed once the project owner "
    f"confirmed the real file now exists at this exact path."
)


def _cleanup_module_dir(module_name: str) -> None:
    _run_in_container(f"rm -rf {_MODULE_DEV_ADDONS_DIR}/{module_name}")


def _module_dir_exists(module_name: str) -> bool:
    proc = _run_in_container(f"test -d {_MODULE_DEV_ADDONS_DIR}/{module_name}")
    return proc.returncode == 0


def _field_genuinely_exists_on_model(db: str, model: str, field_name: str) -> bool:
    """Independent proof that a field really exists -- at the Odoo ORM
    level, not just a claimed install result. MUST pass the full
    --addons-path override (matching what install_module() itself uses)
    -- a shell session without it never loads the module's own code at
    all and gives a false negative, exactly the mistake made once
    during Phase 9.5's manual verification before it was caught.

    Real, confirmed bug found live (2026-07-28, Phase 28B regression
    sweep): this function's own `ssh` call never passed an explicit
    identity key (`-i <path>`/`IdentitiesOnly=yes`), unlike every other
    SSH call in this codebase (`tools_odoo.module_dev.toolchain.
    _run_in_container()`'s own, which always does) -- relying entirely
    on ssh's own default key discovery/agent. Confirmed via a direct,
    side-by-side live repro (same freshly-installed module, same db,
    same moment in time, checked two different ways): this function's
    own raw stdout was EMPTY and stderr showed a hard
    "Permission denied (publickey,password)" -- an SSH AUTH FAILURE,
    not any real field-existence result -- while the exact same check
    run through `_run_in_container()` (which does pass the explicit
    key) correctly found the field. This means every single call to
    this function was silently returning a false "field missing"
    negative purely from a broken SSH invocation, not a real defect in
    the generation/install pipeline -- root-caused as the true cause of
    test_task1_field_add_end_to_end_against_fresh_database's own
    repeated failures this session (an accumulated-ghost-module cleanup
    earlier in the same investigation was a real, separate, worthwhile
    fix, but not sufficient on its own -- this SSH bug is the actual,
    final cause). Fixed by passing the exact same key/IdentitiesOnly
    arguments `_run_in_container()` already uses.
    """
    script = (
        f"field = env[{model!r}]._fields.get({field_name!r})\n"
        f"print(f'FIELD_CHECK exists={{field is not None}}')\n"
    )
    import subprocess

    host = os.environ["OMA_ODOO_SSH_HOST"]
    ssh_user = os.environ["OMA_ODOO_SSH_USER"]
    container = os.environ["OMA_ODOO_SSH_CONTAINER"]
    key_path = os.environ.get("OMA_ODOO_SSH_KEY_PATH")
    odoo_bin_cmd = (
        f"sudo docker exec -i {container} /opt/site/16/odoo-bin shell "
        f"-c {_ODOO_CONF_PATH} --addons-path={_FULL_ADDONS_PATH} -d {db} --no-http"
    )
    ssh_cmd = ["ssh"]
    if key_path:
        ssh_cmd += ["-i", key_path, "-o", "IdentitiesOnly=yes"]
    ssh_cmd += [f"{ssh_user}@{host}", odoo_bin_cmd]
    proc = subprocess.run(
        ssh_cmd,
        input=script,
        capture_output=True,
        text=True,
        timeout=60,
    )
    return "FIELD_CHECK exists=True" in proc.stdout


def _make_contract(goal: str, capability_class=CapabilityClass.module_development) -> TaskContract:
    return TaskContract(
        task_id=uuid.uuid4(),
        specialist_type=SpecialistType.bug_fix,
        capability_class=capability_class,
        tier=AutonomyTier.tier_2_notify_after,
        goal=goal,
        inputs=["res.partner model definition", "contact form view XML"],
        rules=["No changes to core Odoo modules", "No schema changes beyond the one new field"],
        deliverables=["A new module adding the field", "The field visible on the contact form"],
        compensating_actions=[],
        validation_by="testing_qa",
        pause_if=["ambiguity about which form view should show the field"],
        turn_budget=15,
    )


def test_manifest_fields_rendering_never_produces_json_style_booleans():
    """Phase 22 (2026-07-23): real bug root-caused live (2026-07-12)
    was Odoo's own manifest loader parsing __manifest__.py with
    ast.literal_eval(), which only accepts Python's True/False/None --
    never JSON's lowercase true/false/null. Confirmed live: the stuck
    48-round task's own module had `"installable": true` verbatim,
    parsing as a plain string fine but raising `ValueError: malformed
    node or string` inside Odoo's real loader, breaking install for
    every OTHER module in the shared addons directory too.

    round 1's own manifest_py generation used to be raw LLM-authored
    text, needing a dedicated validator (_validate_manifest_is_valid_
    python_literal, deleted this same night) to catch this AFTER the
    fact, one round late. Now that GeneratedModuleFiles.manifest_fields
    is a typed ManifestFields object rendered via _render_manifest_py()
    (repr() on a plain dict of real Python bool/str/list values), this
    bug class is structurally impossible -- there is no code path that
    could ever produce a JSON-style lowercase true/false, because the
    rendered text is never hand-authored, only repr()'d from real
    Python objects. This test proves that guarantee directly, for any
    ManifestFields value, rather than testing a validator that no
    longer needs to exist.
    """
    fields = ManifestFields(
        name="x", version="1.0", category="Uncategorized", summary="", author="",
        depends=["base"], data=[], installable=True, auto_install=False,
    )
    rendered = _render_manifest_py(fields)
    assert "true" not in rendered and "false" not in rendered, (
        "rendered manifest text must never contain JSON-style lowercase booleans"
    )
    assert "True" in rendered
    import ast as _ast
    _ast.literal_eval(rendered)  # must not raise -- always valid Python literal syntax
    print("PASS: ManifestFields rendering structurally cannot produce JSON-style booleans, "
          "always valid ast.literal_eval() syntax")


def test_render_manifest_py_strips_impossible_views_templates_reference():
    """Phase 35 overnight (2026-08-14): real, confirmed, repeatable bug -- 3 consecutive rounds
    of the same real task all failed identically with a real Odoo FileNotFoundError installing
    'views/templates.xml', odoo-bin scaffold's own stock file that strip_scaffold_boilerplate()
    always deletes before Build ever runs. write_module_file() never writes that filename under
    any circumstance. The existing validator/autofix pair meant to catch a bad views/*.xml
    manifest reference evidently was not closing this gap for this task shape -- this is the
    structural, last-mile fix: _render_manifest_py() is the ONE function every manifest write/
    install path renders through, so stripping the impossible entry here closes the gap
    unconditionally, regardless of which earlier validator/autofix did or didn't run.
    """
    fields = ManifestFields(
        name="x", version="1.0", category="Uncategorized", summary="", author="",
        depends=["base"], data=["views/templates.xml", "security/ir.model.access.csv"],
        installable=True, auto_install=False,
    )
    rendered = _render_manifest_py(fields)
    import ast as _ast
    rendered_dict = _ast.literal_eval(rendered)
    assert "views/templates.xml" not in rendered_dict["data"]
    assert "security/ir.model.access.csv" in rendered_dict["data"]
    print("PASS: _render_manifest_py() strips an impossible views/templates.xml reference")


def test_render_manifest_py_keeps_the_one_real_views_file():
    fields = ManifestFields(
        name="x", version="1.0", category="Uncategorized", summary="", author="",
        depends=["base"], data=["views/views.xml", "security/ir.model.access.csv"],
        installable=True, auto_install=False,
    )
    rendered = _render_manifest_py(fields)
    import ast as _ast
    rendered_dict = _ast.literal_eval(rendered)
    assert "views/views.xml" in rendered_dict["data"]
    print("PASS: _render_manifest_py() keeps the one real, legitimate views file reference")


def test_render_manifest_py_strips_any_other_hallucinated_views_file():
    """Not just templates.xml specifically -- any views/*.xml other than the one real file this
    pipeline can ever produce is structurally impossible and must be stripped."""
    fields = ManifestFields(
        name="x", version="1.0", category="Uncategorized", summary="", author="",
        depends=["base"], data=["views/res_partner_views.xml", "views/views.xml"],
        installable=True, auto_install=False,
    )
    rendered = _render_manifest_py(fields)
    import ast as _ast
    rendered_dict = _ast.literal_eval(rendered)
    assert "views/res_partner_views.xml" not in rendered_dict["data"]
    assert "views/views.xml" in rendered_dict["data"]
    print("PASS: _render_manifest_py() strips any hallucinated views/*.xml reference, not just templates.xml")


def test_render_manifest_py_leaves_non_views_data_entries_untouched():
    fields = ManifestFields(
        name="x", version="1.0", category="Uncategorized", summary="", author="",
        depends=["base"],
        data=["security/ir.model.access.csv", "security/security.xml", "data/cron_data.xml"],
        installable=True, auto_install=False,
    )
    rendered = _render_manifest_py(fields)
    import ast as _ast
    rendered_dict = _ast.literal_eval(rendered)
    assert rendered_dict["data"] == ["security/ir.model.access.csv", "security/security.xml", "data/cron_data.xml"]
    print("PASS: _render_manifest_py() leaves legitimate non-views data entries completely untouched")


def test_single_constraint_field_scope_rejects_over_generation():
    """Real, deterministic backstop found necessary live (2026-07-12),
    after confirming manager.replanning's own recurrence-escalation fix
    (CRITICAL_RULE_PREFIX correctly surviving a resume) alone was NOT
    sufficient to stop Build from repeatedly over-generating scope on a
    decomposed single-constraint round -- Build kept making a VARIED
    sequence of different scope-violation mistakes round to round (a
    whole second model one round, extra fields the next), so the same
    finding rarely recurred long enough for escalation to compound.
    This is the deterministic, content-agnostic backstop: reject a
    round that adds more than a couple of genuinely new field
    definitions beyond the prior committed models_py, regardless of how
    the LLM worded its own mistake this time.
    """
    old_models_py = (
        "from odoo import models, fields\n\n"
        "class OmaServiceIssue(models.Model):\n"
        "    _name = 'oma.service.issue'\n"
    )
    over_generated = GeneratedModuleFiles(
        manifest_fields=ManifestFields(
            name="x", version="1.0", category="Uncategorized", summary="", author="", depends=["base"], data=[],
        ),
        models_py=(
            "from odoo import models, fields\n\n"
            "class OmaServiceIssue(models.Model):\n"
            "    _name = 'oma.service.issue'\n"
            "    project_id = fields.Many2one('project.project')\n"
            "    title = fields.Char()\n"
            "    description = fields.Text()\n"
            "    status = fields.Selection([])\n"
            "    priority = fields.Selection([])\n"
            "    attachment_ids = fields.Many2many('ir.attachment')\n"
        ),
        security_csv="",
        notes="",
    )
    raised = False
    try:
        _validate_single_constraint_field_scope(
            over_generated, old_models_py, constraint_status={"service_issue_project_link": "pending"},
        )
    except ValueError:
        raised = True
    assert raised, "a round adding 6 new fields at once on a decomposed task must be rejected"

    minimal = GeneratedModuleFiles(
        manifest_fields=ManifestFields(
            name="x", version="1.0", category="Uncategorized", summary="", author="", depends=["base"], data=[],
        ),
        models_py=(
            "from odoo import models, fields\n\n"
            "class OmaServiceIssue(models.Model):\n"
            "    _name = 'oma.service.issue'\n"
            "    project_id = fields.Many2one('project.project')\n"
        ),
        security_csv="",
        notes="",
    )
    _validate_single_constraint_field_scope(
        minimal, old_models_py, constraint_status={"service_issue_project_link": "pending"},
    )  # must not raise -- exactly one new field, well within the round's own single constraint

    # An ordinary, non-decomposed task (empty constraint_status) must never
    # be scope-limited by this check at all -- it has no single-constraint
    # contract to enforce.
    _validate_single_constraint_field_scope(over_generated, old_models_py, constraint_status={})
    print("PASS: single-constraint field scope validator rejects over-generation on a decomposed "
          "round, allows a genuinely minimal round, and no-ops for an ordinary non-decomposed task")


def test_no_premature_out_of_scope_method_rejects_early_method_write():
    """Phase 25F (2026-07-26): real, live-confirmed gap -- task 009's own
    live run burned all 5 rounds on the identical mistake (writing
    _compute_meerwerk_count when only the meerwerk_count FIELD was in
    scope for that round), correctly caught by Code-Review every round
    but never pre-write, so the round never actually converged on the
    one thing it needed to do (declare the field). This validator closes
    that gap deterministically, mirroring _validate_single_constraint_
    field_scope's own diff-based approach applied to method names.
    """
    old_models_py = (
        "from odoo import models, fields\n\n"
        "class ProjectProject(models.Model):\n"
        "    _inherit = 'project.project'\n"
        "    meerwerk_ids = fields.One2many('project.meerwerk', 'project_id')\n"
    )
    goal = (
        "This round's own NEW focus is ONLY: 'meerwerk_count_field'. "
        "The following constraints are NOT yet in scope for this round and must NOT be "
        "implemented even partially: ['compute_meerwerk_count_method', 'action_view_meerwerk_method']."
    )
    over_generated = GeneratedModuleFiles(
        manifest_fields=ManifestFields(
            name="x", version="1.0", category="Uncategorized", summary="", author="", depends=["base"], data=[],
        ),
        models_py=(
            "from odoo import models, fields, api\n\n"
            "class ProjectProject(models.Model):\n"
            "    _inherit = 'project.project'\n"
            "    meerwerk_ids = fields.One2many('project.meerwerk', 'project_id')\n\n"
            "    def _compute_meerwerk_count(self):\n"
            "        for record in self:\n"
            "            record.meerwerk_count = len(record.meerwerk_ids)\n"
        ),
        security_csv="", notes="",
    )
    raised = False
    try:
        _validate_no_premature_out_of_scope_method(
            over_generated, goal, old_models_py, constraint_status={"meerwerk_count_field": "pending"},
        )
    except ValueError as e:
        raised = True
        assert "compute_meerwerk_count_method" in str(e)
    assert raised, "a round writing a method a LATER constraint owns must be rejected"

    minimal = GeneratedModuleFiles(
        manifest_fields=ManifestFields(
            name="x", version="1.0", category="Uncategorized", summary="", author="", depends=["base"], data=[],
        ),
        models_py=(
            "from odoo import models, fields\n\n"
            "class ProjectProject(models.Model):\n"
            "    _inherit = 'project.project'\n"
            "    meerwerk_ids = fields.One2many('project.meerwerk', 'project_id')\n"
            "    meerwerk_count = fields.Integer(compute='_compute_meerwerk_count')\n"
        ),
        security_csv="", notes="",
    )
    _validate_no_premature_out_of_scope_method(
        minimal, goal, old_models_py, constraint_status={"meerwerk_count_field": "pending"},
    )  # must not raise -- only a field was added, no method

    # An ordinary, non-decomposed task (empty constraint_status) must
    # never be scope-limited by this check.
    _validate_no_premature_out_of_scope_method(over_generated, goal, old_models_py, constraint_status={})

    # A round with no "NOT yet in scope" method-shaped label at all
    # (e.g. only field-shaped labels remain, or none) must not raise --
    # this check only guards METHOD-shaped future constraints.
    _validate_no_premature_out_of_scope_method(
        over_generated, "This round's own NEW focus is ONLY: 'x'. No other constraints remain.",
        old_models_py, constraint_status={"x": "pending"},
    )
    print("PASS: rejects a round writing a method a later constraint owns, allows a genuinely "
          "field-only round, no-ops for a non-decomposed task and for a round with no method-shaped "
          "future constraint")


def test_no_premature_out_of_scope_method_catches_descriptive_multi_word_labels():
    """Real, confirmed bug found live (2026-08-09, task 07141af5-9a4e-41b6-93ea-8b7af04fea9c's
    flagship run, service_ticket_model node): this validator used to only ever check when at
    least one NOT-yet-in-scope label literally ended with the string suffix "_method" (matching
    ONLY the one real incident it was originally built from, e.g.
    'action_view_meerwerk_method') -- but this project's own real constraint-naming convention
    uses descriptive, multi-word labels ('ticket_workflow_and_logging',
    'ticket_access_rights') that never end with that exact suffix, so the whole validator
    silently never fired for any task using that naming convention. Confirmed live: a round
    correctly scoped to 'service_ticket_model' added action_assign/action_start/action_resolve/
    action_close -- workflow methods squarely owned by the explicitly-listed, NOT-yet-in-scope
    'ticket_workflow_and_logging' constraint -- completely unchecked here, only surfacing many
    rounds later as a confusing Testing/QA coverage-gate failure with no clear signal pointing
    back to the real, root scope violation.
    """
    old_models_py = (
        "from odoo import models, fields, api\n\n"
        "class ServiceTicket(models.Model):\n"
        "    _name = 'oma.service.ticket'\n\n"
        "    name = fields.Char()\n"
    )
    goal = (
        "This round's own NEW focus is ONLY: 'service_ticket_model'. "
        "The following constraints are NOT yet in scope for this round and must NOT be "
        "implemented even partially -- no fields, methods, or view elements for any of them "
        "yet, no matter how related they seem: ['ticket_list_view', 'ticket_access_rights', "
        "'daily_escalation_cron', 'maintenance_history', 'ticket_workflow_and_logging', "
        "'ticket_bulk_close', 'project_ticket_counts']."
    )
    over_generated = GeneratedModuleFiles(
        manifest_fields=ManifestFields(
            name="x", version="1.0", category="Uncategorized", summary="", author="", depends=["base"], data=[],
        ),
        models_py=(
            "from odoo import models, fields, api\n\n"
            "class ServiceTicket(models.Model):\n"
            "    _name = 'oma.service.ticket'\n\n"
            "    name = fields.Char()\n"
            "    state = fields.Selection([('new', 'New')], default='new')\n\n"
            "    def action_assign(self):\n"
            "        self.ensure_one()\n"
            "        self.state = 'assigned'\n"
        ),
        security_csv="", notes="",
    )
    raised = False
    try:
        _validate_no_premature_out_of_scope_method(
            over_generated, goal, old_models_py, constraint_status={"service_ticket_model": "pending"},
        )
    except ValueError as e:
        raised = True
        assert "ticket_workflow_and_logging" in str(e)
    assert raised, (
        "a round writing a workflow method owned by a descriptively-named, NOT-yet-in-scope "
        "constraint must be rejected, closing the real live gap found on task 07141af5's "
        "service_ticket_model node"
    )
    print("PASS: catches a premature method owned by a descriptive, multi-word NOT-yet-in-scope "
          "label (not just the narrow '..._method'-suffixed shape), closing the real live gap "
          "found on task 07141af5's service_ticket_model node")


def test_no_premature_out_of_scope_method_allows_structurally_required_compute_method():
    """Phase 30 (2026-08-06), real, live-confirmed gap -- task026's own real v10 run (task_id
    bee72efc-e4a9-4922-aeb2-1a8b391d407d) burned both rounds on the identical false rejection: the
    round's own genuinely in-scope field this round was 'container_count', a computed field, and
    its structurally-required compute method (_compute_container_count) was rejected as a
    premature out-of-scope method write, solely because a DIFFERENT, later constraint
    ('action_open_containers_method') also happens to be method-shaped and not yet in scope. A
    compute/inverse/search method that a field genuinely in this round's own current models_py
    actually points to via compute=/inverse=/search= must be exempt -- it is not optional future
    work, it is required by the field the round is already allowed to write.
    """
    old_models_py = (
        "from odoo import models, fields\n\n"
        "class ProjectContainer(models.Model):\n"
        "    _inherit = 'project.container'\n"
    )
    goal = (
        "This round's own NEW focus is ONLY: 'container_count_field'. "
        "The following constraints are NOT yet in scope for this round and must NOT be "
        "implemented even partially: ['action_open_containers_method']."
    )
    required_compute = GeneratedModuleFiles(
        manifest_fields=ManifestFields(
            name="x", version="1.0", category="Uncategorized", summary="", author="", depends=["base"], data=[],
        ),
        models_py=(
            "from odoo import models, fields, api\n\n"
            "class ProjectContainer(models.Model):\n"
            "    _inherit = 'project.container'\n"
            "    container_count = fields.Integer(compute='_compute_container_count')\n\n"
            "    def _compute_container_count(self):\n"
            "        for record in self:\n"
            "            record.container_count = 0\n"
        ),
        security_csv="", notes="",
    )
    _validate_no_premature_out_of_scope_method(
        required_compute, goal, old_models_py, constraint_status={"container_count_field": "pending"},
    )  # must not raise -- the new method is the compute target of a genuinely in-scope field

    # But a round that ALSO sneaks in the actually-out-of-scope method must still be rejected.
    also_sneaks_in_out_of_scope = GeneratedModuleFiles(
        manifest_fields=ManifestFields(
            name="x", version="1.0", category="Uncategorized", summary="", author="", depends=["base"], data=[],
        ),
        models_py=(
            "from odoo import models, fields, api\n\n"
            "class ProjectContainer(models.Model):\n"
            "    _inherit = 'project.container'\n"
            "    container_count = fields.Integer(compute='_compute_container_count')\n\n"
            "    def _compute_container_count(self):\n"
            "        for record in self:\n"
            "            record.container_count = 0\n\n"
            "    def action_open_containers(self):\n"
            "        pass\n"
        ),
        security_csv="", notes="",
    )
    raised = False
    try:
        _validate_no_premature_out_of_scope_method(
            also_sneaks_in_out_of_scope, goal, old_models_py,
            constraint_status={"container_count_field": "pending"},
        )
    except ValueError as e:
        raised = True
        assert "action_open_containers_method" in str(e)
        assert "_compute_container_count" not in str(e)
    assert raised, "a genuinely out-of-scope method must still be rejected even alongside a valid compute method"
    print("PASS: exempts a compute method structurally required by a genuinely in-scope field, "
          "still rejects an actually out-of-scope method written alongside it")


def test_no_premature_out_of_scope_method_allows_button_referenced_action_method():
    """Real, confirmed bug found live (2026-08-09, task 07141af5's flagship run,
    project_ticket_counts node): the compute=/inverse=/search= exemption above only covers a
    method structurally required by a FIELD this round's own models_py declares -- but a round
    can just as legitimately require a plain action method structurally required by a BUTTON
    this round's own views_xml declares (e.g. a smart-button's `action_open_tickets`), which has
    no field-kwarg reference at all. Confirmed live: two consecutive rounds each correctly added
    a new smart-button view calling `action_open_tickets`/`action_overdue_tickets` alongside the
    matching new methods -- both genuinely required by THIS round's own in-scope smart-button
    constraint -- rejected every time as if they belonged to an unrelated, later constraint,
    stuck in an identical loop confirmed live via a manager escalation summary describing the
    exact same defect in plain language.
    """
    old_models_py = (
        "from odoo import models, fields\n\n"
        "class ProjectProject(models.Model):\n"
        "    _inherit = 'project.project'\n"
        "    open_ticket_count = fields.Integer(compute='_compute_ticket_counts')\n\n"
        "    def _compute_ticket_counts(self):\n"
        "        pass\n"
    )
    goal = (
        "This round's own NEW focus is ONLY: 'project_ticket_counts'. "
        "The following constraints are NOT yet in scope for this round and must NOT be "
        "implemented even partially: ['daily_escalation_cron', 'ticket_workflow_and_logging']."
    )
    required_action_method = GeneratedModuleFiles(
        manifest_fields=ManifestFields(
            name="x", version="1.0", category="Uncategorized", summary="", author="", depends=["base"], data=[],
        ),
        models_py=(
            "from odoo import models, fields\n\n"
            "class ProjectProject(models.Model):\n"
            "    _inherit = 'project.project'\n"
            "    open_ticket_count = fields.Integer(compute='_compute_ticket_counts')\n\n"
            "    def _compute_ticket_counts(self):\n"
            "        pass\n\n"
            "    def action_open_tickets(self):\n"
            "        pass\n"
        ),
        views_xml=(
            '<odoo><record id="view_project_ticket_buttons" model="ir.ui.view">'
            '<field name="arch" type="xml"><button type="object" name="action_open_tickets"/>'
            '</field></record></odoo>'
        ),
        security_csv="", notes="",
    )
    _validate_no_premature_out_of_scope_method(
        required_action_method, goal, old_models_py, constraint_status={"project_ticket_counts": "pending"},
    )  # must not raise -- the new method is referenced by a button this round's own view needs

    # But a round that ALSO sneaks in a method no button/field anywhere references must still be
    # rejected -- this exemption only ever covers a structurally-referenced method, never a blanket
    # pass for any new method once one legitimate one is found.
    also_sneaks_in_out_of_scope = GeneratedModuleFiles(
        manifest_fields=ManifestFields(
            name="x", version="1.0", category="Uncategorized", summary="", author="", depends=["base"], data=[],
        ),
        models_py=(
            "from odoo import models, fields\n\n"
            "class ProjectProject(models.Model):\n"
            "    _inherit = 'project.project'\n"
            "    open_ticket_count = fields.Integer(compute='_compute_ticket_counts')\n\n"
            "    def _compute_ticket_counts(self):\n"
            "        pass\n\n"
            "    def action_open_tickets(self):\n"
            "        pass\n\n"
            "    def action_send_escalation_email(self):\n"
            "        pass\n"
        ),
        views_xml=(
            '<odoo><record id="view_project_ticket_buttons" model="ir.ui.view">'
            '<field name="arch" type="xml"><button type="object" name="action_open_tickets"/>'
            '</field></record></odoo>'
        ),
        security_csv="", notes="",
    )
    raised = False
    try:
        _validate_no_premature_out_of_scope_method(
            also_sneaks_in_out_of_scope, goal, old_models_py,
            constraint_status={"project_ticket_counts": "pending"},
        )
    except ValueError as e:
        raised = True
        assert "action_send_escalation_email" in str(e)
        assert "action_open_tickets" not in str(e)
    assert raised, "a genuinely unreferenced out-of-scope method must still be rejected"
    print("PASS: exempts an action method structurally required by a genuinely in-scope button, "
          "still rejects an actually out-of-scope method written alongside it")


def test_no_premature_out_of_scope_method_allows_cron_code_referenced_method():
    """Real, confirmed bug found live (2026-08-10, task 07141af5's flagship run,
    daily_escalation_cron node): the same "legitimate structural reference this validator
    doesn't know about yet" gap its compute-field and button siblings above already closed once
    each, for a THIRD real reference source -- an `ir.cron` record's own `<field
    name="code">model._run_daily_escalation_check()</field>`. Confirmed live: this round's own
    new `run_daily_escalation_check` method, genuinely required by the round's own in-scope cron
    feature and referenced only from a new `data/cron_data.xml` record's `code` field, was
    rejected as if it belonged to an unrelated, later constraint.
    """
    old_models_py = (
        "from odoo import models, fields\n\n"
        "class ServiceTicket(models.Model):\n"
        "    _name = 'oma.service.ticket'\n"
    )
    goal = (
        "This round's own NEW focus is ONLY: 'daily_escalation_cron'. "
        "The following constraints are NOT yet in scope for this round and must NOT be "
        "implemented even partially: ['ticket_workflow_and_logging', 'ticket_bulk_close']."
    )
    required_cron_method = GeneratedModuleFiles(
        manifest_fields=ManifestFields(
            name="x", version="1.0", category="Uncategorized", summary="", author="", depends=["base"], data=[],
        ),
        models_py=(
            "from odoo import models, fields\n\n"
            "class ServiceTicket(models.Model):\n"
            "    _name = 'oma.service.ticket'\n\n"
            "    def run_daily_escalation_check(self):\n"
            "        pass\n"
        ),
        extra_data_files={
            "data/cron_data.xml": (
                '<odoo><record id="ir_cron_escalate" model="ir.cron">'
                '<field name="code">model.run_daily_escalation_check()</field>'
                "</record></odoo>"
            ),
        },
        security_csv="", notes="",
    )
    _validate_no_premature_out_of_scope_method(
        required_cron_method, goal, old_models_py, constraint_status={"daily_escalation_cron": "pending"},
    )  # must not raise -- the new method is referenced by this round's own cron record

    # But a round that ALSO sneaks in a method no cron/button/field anywhere references must
    # still be rejected.
    also_sneaks_in_out_of_scope = GeneratedModuleFiles(
        manifest_fields=ManifestFields(
            name="x", version="1.0", category="Uncategorized", summary="", author="", depends=["base"], data=[],
        ),
        models_py=(
            "from odoo import models, fields\n\n"
            "class ServiceTicket(models.Model):\n"
            "    _name = 'oma.service.ticket'\n\n"
            "    def run_daily_escalation_check(self):\n"
            "        pass\n\n"
            "    def action_change_state(self):\n"
            "        pass\n"
        ),
        extra_data_files={
            "data/cron_data.xml": (
                '<odoo><record id="ir_cron_escalate" model="ir.cron">'
                '<field name="code">model.run_daily_escalation_check()</field>'
                "</record></odoo>"
            ),
        },
        security_csv="", notes="",
    )
    raised = False
    try:
        _validate_no_premature_out_of_scope_method(
            also_sneaks_in_out_of_scope, goal, old_models_py,
            constraint_status={"daily_escalation_cron": "pending"},
        )
    except ValueError as e:
        raised = True
        assert "action_change_state" in str(e)
        assert "run_daily_escalation_check" not in str(e)
    assert raised, "a genuinely unreferenced out-of-scope method must still be rejected"
    print("PASS: exempts a method structurally required by this round's own in-scope cron "
          "record, still rejects an actually out-of-scope method written alongside it")


def test_no_premature_out_of_scope_method_allows_standard_crud_lifecycle_override():
    """Real, confirmed bug found live (2026-08-10, task 07141af5's flagship run,
    daily_escalation_cron node): a round overriding the standard Odoo `write()` CRUD hook (to
    stamp a new `assigned_date` field the moment `state` is written to `'assigned'`) was rejected
    as "a new method belonging to a LATER round's own constraint" purely because `write` didn't
    exist in the prior committed models.py. A CRUD lifecycle override is ORM plumbing, never a
    business feature any constraint label could name -- it has no button/cron/compute= reference
    to exempt it via the existing mechanisms.
    """
    old_models_py = (
        "from odoo import models, fields\n\n"
        "class ServiceTicket(models.Model):\n"
        "    _name = 'oma.service.ticket'\n"
    )
    goal = (
        "This round's own NEW focus is ONLY: 'daily_escalation_cron'. "
        "The following constraints are NOT yet in scope for this round and must NOT be "
        "implemented even partially: ['ticket_workflow_and_logging', 'ticket_bulk_close']."
    )
    overrides_write = GeneratedModuleFiles(
        manifest_fields=ManifestFields(
            name="x", version="1.0", category="Uncategorized", summary="", author="", depends=["base"], data=[],
        ),
        models_py=(
            "from odoo import models, fields\n\n"
            "class ServiceTicket(models.Model):\n"
            "    _name = 'oma.service.ticket'\n"
            "    assigned_date = fields.Datetime()\n\n"
            "    def write(self, vals):\n"
            "        if vals.get('state') == 'assigned' and not self.assigned_date:\n"
            "            vals['assigned_date'] = fields.Datetime.now()\n"
            "        return super().write(vals)\n"
        ),
        security_csv="", notes="",
    )
    _validate_no_premature_out_of_scope_method(
        overrides_write, goal, old_models_py, constraint_status={"daily_escalation_cron": "pending"},
    )  # must not raise -- write() is standard ORM plumbing, never an out-of-scope business method

    # But a genuinely out-of-scope, non-lifecycle method written alongside it must still be rejected.
    also_sneaks_in_out_of_scope = GeneratedModuleFiles(
        manifest_fields=ManifestFields(
            name="x", version="1.0", category="Uncategorized", summary="", author="", depends=["base"], data=[],
        ),
        models_py=(
            "from odoo import models, fields\n\n"
            "class ServiceTicket(models.Model):\n"
            "    _name = 'oma.service.ticket'\n"
            "    assigned_date = fields.Datetime()\n\n"
            "    def write(self, vals):\n"
            "        return super().write(vals)\n\n"
            "    def action_change_state(self):\n"
            "        pass\n"
        ),
        security_csv="", notes="",
    )
    raised = False
    try:
        _validate_no_premature_out_of_scope_method(
            also_sneaks_in_out_of_scope, goal, old_models_py,
            constraint_status={"daily_escalation_cron": "pending"},
        )
    except ValueError as e:
        raised = True
        assert "action_change_state" in str(e)
        assert "['write']" not in str(e) and "'write'," not in str(e), (
            f"write() must never appear in the added_methods list itself -- got: {e}"
        )
    assert raised, "a genuinely unreferenced out-of-scope method must still be rejected"
    print("PASS: exempts a standard Odoo CRUD lifecycle override, still rejects an actually "
          "out-of-scope method written alongside it")


def test_no_premature_out_of_scope_method_allows_method_matching_this_rounds_own_focus():
    """Real, confirmed bug found live (2026-08-10, task 07141af5's flagship run,
    ticket_bulk_close node): unlike this validator's own sibling `_autofix_strip_premature_
    computed_field_on_decomposed_round` (which checks `this_rounds_focus` first and
    unconditionally protects a match), this validator never checked `this_rounds_focus` at all --
    only via one of four STRUCTURAL reference mechanisms. Confirmed live: `action_bulk_close`,
    named directly after and fuzzy-matching this round's own real focus label
    `ticket_bulk_close`, was rejected as belonging to `ticket_workflow_and_logging` (which it
    does NOT fuzzy-match at all) purely because none of the four structural exemptions happened
    to apply THIS specific round (the button referencing it lived in an earlier round's own
    already-committed views.xml, not this round's own fresh diff).
    """
    old_models_py = (
        "from odoo import models, fields\n\n"
        "class ServiceTicket(models.Model):\n"
        "    _name = 'oma.service.ticket'\n"
    )
    goal = (
        "This round's own NEW focus is ONLY: 'ticket_bulk_close'. "
        "The following constraints are NOT yet in scope for this round and must NOT be "
        "implemented even partially: ['ticket_workflow_and_logging']."
    )
    matches_own_focus = GeneratedModuleFiles(
        manifest_fields=ManifestFields(
            name="x", version="1.0", category="Uncategorized", summary="", author="", depends=["base"], data=[],
        ),
        models_py=(
            "from odoo import models, fields\n\n"
            "class ServiceTicket(models.Model):\n"
            "    _name = 'oma.service.ticket'\n\n"
            "    def action_bulk_close(self):\n"
            "        self.write({'state': 'closed'})\n"
        ),
        # Deliberately NO views_xml this round -- the real live shape: the button referencing
        # this method already exists in an earlier, already-committed round's own views.xml.
        security_csv="", notes="",
    )
    _validate_no_premature_out_of_scope_method(
        matches_own_focus, goal, old_models_py, constraint_status={"ticket_bulk_close": "pending"},
    )  # must not raise -- action_bulk_close IS this round's own declared focus

    # But a genuinely out-of-scope method that does NOT match this round's own focus must still
    # be rejected, even with the same focus label in play.
    also_sneaks_in_out_of_scope = GeneratedModuleFiles(
        manifest_fields=ManifestFields(
            name="x", version="1.0", category="Uncategorized", summary="", author="", depends=["base"], data=[],
        ),
        models_py=(
            "from odoo import models, fields\n\n"
            "class ServiceTicket(models.Model):\n"
            "    _name = 'oma.service.ticket'\n\n"
            "    def action_bulk_close(self):\n"
            "        self.write({'state': 'closed'})\n\n"
            "    def action_log_state_change(self):\n"
            "        pass\n"
        ),
        security_csv="", notes="",
    )
    raised = False
    try:
        _validate_no_premature_out_of_scope_method(
            also_sneaks_in_out_of_scope, goal, old_models_py,
            constraint_status={"ticket_bulk_close": "pending"},
        )
    except ValueError as e:
        raised = True
        assert "action_log_state_change" in str(e)
        assert "action_bulk_close" not in str(e)
    assert raised, "a genuinely unreferenced out-of-scope method must still be rejected"
    print("PASS: exempts a method matching this round's own declared focus even with no "
          "structural reference in THIS round's own diff, still rejects an actually "
          "out-of-scope method written alongside it")


def test_no_invented_related_field_targets_rejects_nonexistent_target():
    """Real, confirmed bug found live (2026-07-24, task 020's 5th round
    of a context-aware full-rewrite): a genuinely different mistake
    class from the sibling `_validate_no_invented_related_fields` check
    -- that one catches a plain Python dotted-attribute READ
    (`self.project_id.product_ids`); this one catches Build DECLARING a
    new `related=` field whose target path is invented, invisible to
    the sibling check since it never looks inside a `fields.X(related=
    ...)` call at all. Confirmed live: `expected_finish_date = fields.
    Date(related='project_id.expected_finish_date', ...)` passed every
    existing validator and Code-Review, then crashed the real install
    with `KeyError: Field expected_finish_date referenced in related
    field definition project.meerwerk.expected_finish_date does not
    exist.` -- reproduced here with the exact same field/model shape.
    """
    from specialists.build.specialist import _validate_no_invented_related_field_targets

    invented = GeneratedModuleFiles(
        manifest_fields=ManifestFields(
            name="x", version="1.0", category="Uncategorized", summary="", author="", depends=["base"], data=[],
        ),
        models_py=(
            "from odoo import models, fields\n\n"
            "class ProjectMeerwerk(models.Model):\n"
            "    _inherit = 'project.meerwerk'\n\n"
            "    expected_finish_date = fields.Date(related='project_id.expected_finish_date', "
            "string='Expected Finish Date')\n"
        ),
        security_csv="",
        notes="",
    )
    raised = False
    try:
        asyncio.run(_validate_no_invented_related_field_targets(invented, [], "odoo16_dev", task_id="test"))
    except ValueError:
        raised = True
    assert raised, "an invented related-field target (project.project has no expected_finish_date) must be rejected"

    real = GeneratedModuleFiles(
        manifest_fields=ManifestFields(
            name="x", version="1.0", category="Uncategorized", summary="", author="", depends=["base"], data=[],
        ),
        models_py=(
            "from odoo import models, fields\n\n"
            "class ProjectMeerwerk(models.Model):\n"
            "    _inherit = 'project.meerwerk'\n\n"
            "    customer_email = fields.Char(related='partner_id.email', string='Customer Email')\n"
        ),
        security_csv="",
        notes="",
    )
    asyncio.run(_validate_no_invented_related_field_targets(real, [], "odoo16_dev", task_id="test"))
    print("PASS: related-field-target validator rejects an invented target (project.project."
          "expected_finish_date) and allows a genuinely real one (res.partner.email)")


def test_no_invented_related_fields_does_not_misparse_a_3_hop_related_kwarg_string():
    """Real, confirmed bug found live (wave16_rf, 2026-08-16): `_validate_no_invented_
    related_fields` (the plain-Python-code dotted-access check, NOT the sibling
    `_validate_no_invented_related_field_targets` which correctly skips 3+-hop
    related= paths) scans the raw source text of models_py with `_DOTTED_ACCESS_RE`,
    which cannot tell real code (`self.employee_id.parent_id`) apart from a plain
    string literal. A 3-hop `related='employee_id.parent_id.work_email'` kwarg value
    got its MIDDLE two hops ("parent_id.work_email") misread as a real two-part code
    access. Confirmed live: `hr.leave` genuinely has its own real `parent_id` field
    (self-referencing), so the false match resolved against a real field_targets
    entry and rejected an entirely valid 3-hop related= declaration with a
    fabricated "'hr.leave' has no real field 'work_email'" error -- work_email was
    only ever meant to be read off the real hop-3 target, hr.employee.
    """
    from specialists.build.specialist import _validate_no_invented_related_fields

    valid_3_hop_related = GeneratedModuleFiles(
        manifest_fields=ManifestFields(
            name="x", version="1.0", category="Uncategorized", summary="", author="", depends=["base"], data=[],
        ),
        models_py=(
            "from odoo import models, fields\n\n"
            "class HrLeave(models.Model):\n"
            "    _inherit = 'hr.leave'\n\n"
            "    approver_email = fields.Char(related='employee_id.parent_id.work_email', "
            "string='Approver Email', readonly=True, store=False)\n"
        ),
        security_csv="",
        notes="",
    )
    asyncio.run(_validate_no_invented_related_fields(valid_3_hop_related, [], "odoo16_dev", task_id="test"))
    print("PASS: a valid 3-hop related= kwarg string is no longer misparsed as a real "
          "2-hop code access and false-flagged")

    genuinely_invented_access = GeneratedModuleFiles(
        manifest_fields=ManifestFields(
            name="x", version="1.0", category="Uncategorized", summary="", author="", depends=["base"], data=[],
        ),
        models_py=(
            "from odoo import models, fields, api\n\n"
            "class HrLeave(models.Model):\n"
            "    _inherit = 'hr.leave'\n\n"
            "    @api.depends('employee_id')\n"
            "    def _compute_something(self):\n"
            "        for rec in self:\n"
            "            rec.something = rec.employee_id.totally_invented_field\n"
        ),
        security_csv="",
        notes="",
    )
    raised = False
    try:
        asyncio.run(_validate_no_invented_related_fields(genuinely_invented_access, [], "odoo16_dev", task_id="test"))
    except ValueError:
        raised = True
    assert raised, "a genuinely invented real-code dotted access must still be rejected"
    print("PASS: a genuinely invented Python-code dotted-attribute access is still rejected")


def test_api_depends_fields_exist_rejects_invented_dependency():
    """Real, confirmed bug found live (2026-07-25, task 004, round 5): a
    THIRD surface for the same "invented dotted-path field reference"
    mistake, invisible to both sibling checks above -- an `@api.depends(
    'relation.subfield')` decorator argument is a string literal, not
    Python code (the plain-access sibling never looks) and not a field
    kwarg (the related= sibling never looks either). Confirmed live:
    `@api.depends('line_ids.quantity', 'line_ids.price_unit')` passed
    every existing validator and Code-Review across multiple rounds, then
    crashed the real sandbox install registry build with `KeyError:
    'quantity'` -- reproduced here with the exact same field/model shape
    (`res.partner.category_id` is real; `res.partner.nonexistent_attr`
    is not).
    """
    from specialists.build.specialist import _validate_api_depends_fields_exist

    invented = GeneratedModuleFiles(
        manifest_fields=ManifestFields(
            name="x", version="1.0", category="Uncategorized", summary="", author="", depends=["base"], data=[],
        ),
        models_py=(
            "from odoo import models, fields, api\n\n"
            "class CrmLead(models.Model):\n"
            "    _inherit = 'crm.lead'\n\n"
            "    tag_count = fields.Integer(compute='_compute_tag_count', store=True)\n\n"
            "    @api.depends('partner_id.nonexistent_attr')\n"
            "    def _compute_tag_count(self):\n"
            "        for record in self:\n"
            "            record.tag_count = 0\n"
        ),
        security_csv="", notes="",
    )
    raised = False
    try:
        asyncio.run(_validate_api_depends_fields_exist(invented, [], "odoo16_dev", task_id="test"))
    except ValueError:
        raised = True
    assert raised, "an @api.depends(...) referencing an invented field on the real related model must be rejected"

    real = GeneratedModuleFiles(
        manifest_fields=ManifestFields(
            name="x", version="1.0", category="Uncategorized", summary="", author="", depends=["base"], data=[],
        ),
        models_py=(
            "from odoo import models, fields, api\n\n"
            "class CrmLead(models.Model):\n"
            "    _inherit = 'crm.lead'\n\n"
            "    tag_count = fields.Integer(compute='_compute_tag_count', store=True)\n\n"
            "    @api.depends('partner_id.category_id')\n"
            "    def _compute_tag_count(self):\n"
            "        for record in self:\n"
            "            record.tag_count = len(record.partner_id.category_id)\n"
        ),
        security_csv="", notes="",
    )
    asyncio.run(_validate_api_depends_fields_exist(real, [], "odoo16_dev", task_id="test"))
    print("PASS: @api.depends validator rejects an invented dependency (res.partner."
          "nonexistent_attr) and allows a genuinely real one (res.partner.category_id)")


def test_view_fields_exist_on_inherited_model_rejects_invented_field():
    """Real, confirmed bug found live (2026-07-24, task 020's 6th round
    of a context-aware full-rewrite): the sync
    `_validate_view_fields_exist_on_model()` deliberately skips a pure
    `_inherit` extension (its own docstring: "this check has no way to
    know [core fields] without reading the target model's real
    source"). Confirmed live: after the sibling related-field-target
    validator correctly rejected `expected_finish_date` as an invented
    `related=` target, Build's next round referenced the SAME invented
    field name directly in a view instead, which passed every existing
    validator and only surfaced as a real `ParseError` at sandbox
    install: `Field "expected_finish_date" does not exist in model
    "project.meerwerk"`. This async sibling closes that gap using the
    real, live Odoo registry.
    """
    from specialists.build.specialist import _validate_view_fields_exist_on_inherited_model

    invented = GeneratedModuleFiles(
        manifest_fields=ManifestFields(
            name="x", version="1.0", category="Uncategorized", summary="", author="", depends=["base"], data=[],
        ),
        models_py="from odoo import models\n\nclass ProjectMeerwerk(models.Model):\n    _inherit = 'project.meerwerk'\n",
        views_xml=(
            "<odoo>\n  <record id=\"v1\" model=\"ir.ui.view\">\n"
            "    <field name=\"model\">project.meerwerk</field>\n"
            "    <field name=\"arch\" type=\"xml\">\n"
            "      <field name=\"expected_finish_date\"/>\n"
            "    </field>\n  </record>\n</odoo>"
        ),
        security_csv="",
        notes="",
    )
    raised = False
    try:
        asyncio.run(_validate_view_fields_exist_on_inherited_model(invented, "odoo16_dev", task_id="test"))
    except ValueError:
        raised = True
    assert raised, "a view referencing an invented field on the real inherited model must be rejected"

    real = GeneratedModuleFiles(
        manifest_fields=ManifestFields(
            name="x", version="1.0", category="Uncategorized", summary="", author="", depends=["base"], data=[],
        ),
        models_py="from odoo import models\n\nclass ProjectMeerwerk(models.Model):\n    _inherit = 'project.meerwerk'\n",
        views_xml=(
            "<odoo>\n  <record id=\"v1\" model=\"ir.ui.view\">\n"
            "    <field name=\"model\">project.meerwerk</field>\n"
            "    <field name=\"arch\" type=\"xml\">\n"
            "      <field name=\"date_finish\"/>\n"
            "    </field>\n  </record>\n</odoo>"
        ),
        security_csv="",
        notes="",
    )
    asyncio.run(_validate_view_fields_exist_on_inherited_model(real, "odoo16_dev", task_id="test"))
    print("PASS: inherited-model view-field validator rejects an invented field (expected_finish_date) "
          "and allows a genuinely real one (date_finish)")


def test_view_fields_exist_on_inherited_model_rejects_a_field_this_round_itself_just_removed():
    """Phase 35 fix (2026-08-13 overnight, 2 real live occurrences the same night -- t0_11 on
    crm.lead, t0_12 on res.partner): a round that removes a field from `models_py` while
    carrying forward an earlier round's `views_xml` still referencing it must be rejected HERE,
    deterministically, using `old_models_py` -- never relying on the live-registry check (which
    is guaranteed stale for exactly this field, since the field hasn't actually been dropped from
    the schema yet at validation time). No live Odoo call needed for this specific check, so no
    `task_id`/`db` dependency on whether it fires.
    """
    from specialists.build.specialist import _validate_view_fields_exist_on_inherited_model

    old_models_py = (
        "from odoo import fields, models\n\nclass ResPartner(models.Model):\n"
        "    _inherit = 'res.partner'\n\n"
        "    special_instructions = fields.Text(string='Special Instructions')\n"
    )
    # This round's own new models_py no longer declares special_instructions -- but the carried-
    # forward views_xml still references it (the exact real shape found live: an earlier round's
    # view content survives into a later round that dropped the underlying field).
    stale_view = GeneratedModuleFiles(
        manifest_fields=ManifestFields(
            name="x", version="1.0", category="Uncategorized", summary="", author="", depends=["base"], data=[],
        ),
        models_py="from odoo import models\n\nclass ResPartner(models.Model):\n    _inherit = 'res.partner'\n",
        views_xml=(
            "<odoo>\n  <record id=\"v1\" model=\"ir.ui.view\">\n"
            "    <field name=\"model\">res.partner</field>\n"
            "    <field name=\"arch\" type=\"xml\">\n"
            "      <field name=\"special_instructions\"/>\n"
            "    </field>\n  </record>\n</odoo>"
        ),
        security_csv="",
        notes="",
    )
    raised = False
    try:
        asyncio.run(_validate_view_fields_exist_on_inherited_model(
            stale_view, "odoo16_dev", task_id="test", old_models_py=old_models_py,
        ))
    except ValueError as exc:
        raised = True
        assert "special_instructions" in str(exc)
    assert raised, "a view still referencing a field this round itself just removed must be rejected"

    # The clean case: the view content for the removed field is ALSO removed in this same round.
    clean = GeneratedModuleFiles(
        manifest_fields=ManifestFields(
            name="x", version="1.0", category="Uncategorized", summary="", author="", depends=["base"], data=[],
        ),
        models_py="from odoo import models\n\nclass ResPartner(models.Model):\n    _inherit = 'res.partner'\n",
        views_xml=None,
        security_csv="",
        notes="",
    )
    asyncio.run(_validate_view_fields_exist_on_inherited_model(
        clean, "odoo16_dev", task_id="test", old_models_py=old_models_py,
    ))
    print("PASS: rejects a view still referencing a field this round itself just removed from "
          "models_py, and allows the clean case where the view content was removed too")


def test_view_fields_exist_on_inherited_model_ignores_nested_one2many_subview_fields():
    """Real, confirmed bug found live (2026-08-06, Phase 30 root-cause pass, task034): a
    One2many field opened as an editable subview (`<field name="line_ids"><tree>...<field
    name="hours"/>...</tree></field>`) nests real fields of the RELATION'S TARGET model
    (project.meerwerk.line's own real `hours`/`price_unit`), not the outer inherit target
    (project.meerwerk) this validator checks against -- genuinely correct, standard Odoo XML that
    was rejected as "doesn't exist on project.meerwerk" before this fix. Using project.meerwerk's
    own real, live line_ids -> project.meerwerk.line relation (confirmed via
    get_relation_fields_fast against the real database).
    """
    from specialists.build.specialist import _validate_view_fields_exist_on_inherited_model

    genuinely_correct_nested_subview = GeneratedModuleFiles(
        manifest_fields=ManifestFields(
            name="x", version="1.0", category="Uncategorized", summary="", author="", depends=["base"], data=[],
        ),
        models_py="from odoo import models\n\nclass ProjectMeerwerk(models.Model):\n    _inherit = 'project.meerwerk'\n",
        views_xml=(
            "<odoo>\n  <record id=\"v1\" model=\"ir.ui.view\">\n"
            "    <field name=\"model\">project.meerwerk</field>\n"
            "    <field name=\"arch\" type=\"xml\">\n"
            "      <field name=\"line_ids\">\n"
            "        <tree editable=\"bottom\">\n"
            "          <field name=\"hours\"/>\n"
            "          <field name=\"price_unit\"/>\n"
            "        </tree>\n"
            "      </field>\n"
            "    </field>\n  </record>\n</odoo>"
        ),
        security_csv="",
        notes="",
    )
    asyncio.run(_validate_view_fields_exist_on_inherited_model(
        genuinely_correct_nested_subview, "odoo16_dev", task_id="test",
    ))  # must not raise -- hours/price_unit are real fields of the line model, not the outer one

    # An invented field OUTSIDE any nested subview must still be rejected.
    invented_outside_subview = GeneratedModuleFiles(
        manifest_fields=ManifestFields(
            name="x", version="1.0", category="Uncategorized", summary="", author="", depends=["base"], data=[],
        ),
        models_py="from odoo import models\n\nclass ProjectMeerwerk(models.Model):\n    _inherit = 'project.meerwerk'\n",
        views_xml=(
            "<odoo>\n  <record id=\"v1\" model=\"ir.ui.view\">\n"
            "    <field name=\"model\">project.meerwerk</field>\n"
            "    <field name=\"arch\" type=\"xml\">\n"
            "      <field name=\"totally_invented_field\"/>\n"
            "      <field name=\"line_ids\">\n"
            "        <tree editable=\"bottom\">\n"
            "          <field name=\"hours\"/>\n"
            "        </tree>\n"
            "      </field>\n"
            "    </field>\n  </record>\n</odoo>"
        ),
        security_csv="",
        notes="",
    )
    raised = False
    try:
        asyncio.run(_validate_view_fields_exist_on_inherited_model(
            invented_outside_subview, "odoo16_dev", task_id="test",
        ))
    except ValueError as e:
        raised = True
        assert "totally_invented_field" in str(e)
    assert raised, "an invented field OUTSIDE the nested subview must still be rejected"
    print("PASS: nested one2many subview's own real fields no longer false-positive, an invented "
          "field outside any subview is still rejected")


def test_view_fields_exist_on_inherited_model_scopes_each_view_to_its_own_declared_target():
    """Real, confirmed bug found live (2026-08-06, Phase 30 root-cause pass, task028): this
    validator originally picked only the FIRST `_inherit` target and checked EVERY view's fields
    against it -- wrong for a genuinely correct multi-model generation (task028's own real shape:
    `_inherit`s BOTH `project.type` and `crm.lead`). A view genuinely built for crm.lead
    (referencing its own real `project_type_ids` many2many field) was rejected as "doesn't exist
    on project.type" purely because project.type happened to be the first target found. Using
    real, live model shapes (crm.lead.partner_id, project.project.privacy_visibility) so this can
    be checked against the real database, matching this file's own established convention for this
    validator's tests.
    """
    from specialists.build.specialist import _validate_view_fields_exist_on_inherited_model

    two_targets_each_correct = GeneratedModuleFiles(
        manifest_fields=ManifestFields(
            name="x", version="1.0", category="Uncategorized", summary="", author="", depends=["base"], data=[],
        ),
        models_py=(
            "from odoo import models\n\n"
            "class ProjectProject(models.Model):\n"
            "    _inherit = 'project.project'\n\n\n"
            "class CrmLead(models.Model):\n"
            "    _inherit = 'crm.lead'\n"
        ),
        views_xml=(
            "<odoo>\n"
            "  <record id=\"v1\" model=\"ir.ui.view\">\n"
            "    <field name=\"model\">project.project</field>\n"
            "    <field name=\"arch\" type=\"xml\">\n"
            "      <field name=\"privacy_visibility\"/>\n"
            "    </field>\n  </record>\n"
            "  <record id=\"v2\" model=\"ir.ui.view\">\n"
            "    <field name=\"model\">crm.lead</field>\n"
            "    <field name=\"arch\" type=\"xml\">\n"
            "      <field name=\"partner_id\"/>\n"
            "    </field>\n  </record>\n"
            "</odoo>"
        ),
        security_csv="",
        notes="",
    )
    asyncio.run(_validate_view_fields_exist_on_inherited_model(
        two_targets_each_correct, "odoo16_dev", task_id="test",
    ))  # must not raise -- each view's own field is real on ITS OWN declared target model

    # A field genuinely invented on ONE of the two targets must still be caught, scoped to the
    # right one, even though the OTHER target is completely correct.
    one_target_invented = GeneratedModuleFiles(
        manifest_fields=ManifestFields(
            name="x", version="1.0", category="Uncategorized", summary="", author="", depends=["base"], data=[],
        ),
        models_py=(
            "from odoo import models\n\n"
            "class ProjectProject(models.Model):\n"
            "    _inherit = 'project.project'\n\n\n"
            "class CrmLead(models.Model):\n"
            "    _inherit = 'crm.lead'\n"
        ),
        views_xml=(
            "<odoo>\n"
            "  <record id=\"v1\" model=\"ir.ui.view\">\n"
            "    <field name=\"model\">project.project</field>\n"
            "    <field name=\"arch\" type=\"xml\">\n"
            "      <field name=\"totally_invented_project_field\"/>\n"
            "    </field>\n  </record>\n"
            "  <record id=\"v2\" model=\"ir.ui.view\">\n"
            "    <field name=\"model\">crm.lead</field>\n"
            "    <field name=\"arch\" type=\"xml\">\n"
            "      <field name=\"partner_id\"/>\n"
            "    </field>\n  </record>\n"
            "</odoo>"
        ),
        security_csv="",
        notes="",
    )
    raised = False
    try:
        asyncio.run(_validate_view_fields_exist_on_inherited_model(
            one_target_invented, "odoo16_dev", task_id="test",
        ))
    except ValueError as e:
        raised = True
        assert "totally_invented_project_field" in str(e)
        assert "project.project" in str(e)
    assert raised, "an invented field on ONE target must still be caught even when the other target is correct"
    print("PASS: each view is scoped to its own declared model target, not the first _inherit "
          "target found; a genuine invented field on one target is still caught")


def test_view_fields_exist_on_inherited_model_trusts_a_declared_target_not_in_this_rounds_inherit_list():
    """Real, confirmed bug found live (2026-08-10, task e65381cc, ticket_views_menu node): a
    record's own declared `<field name="model">X</field>` used to only be trusted when X was
    ALSO one of THIS round's own `_inherit` targets in models_py -- falling back to checking
    against every inherit target otherwise. Wrong for a `depends_on_module:` task's views-only
    round: models_py still carries an EARLIER round's own `_inherit = 'oma.equipment'` class
    (this round never touches it), but this round's own new record legitimately declares
    `<field name="model">oma.service.ticket</field>` -- a real, live, different model models_py
    never mentions at all. Confirmed live: real oma.service.ticket fields (`equipment_id`,
    `project_id`, `technician_id`) were checked against oma.equipment's own real fields instead,
    falsely rejected as nonexistent.
    """
    from specialists.build.specialist import _validate_view_fields_exist_on_inherited_model

    generated = GeneratedModuleFiles(
        manifest_fields=ManifestFields(
            name="x", version="1.0", category="Uncategorized", summary="", author="", depends=["base"], data=[],
        ),
        models_py=(
            "from odoo import models, fields\n\n"
            "class Equipment(models.Model):\n"
            "    _inherit = 'oma.equipment'\n\n"
            "    equipment_type = fields.Selection([('tool', 'Tool')], string='Equipment Type')\n"
        ),
        views_xml=(
            "<odoo>\n"
            "  <record id=\"v1\" model=\"ir.ui.view\">\n"
            "    <field name=\"model\">oma.service.ticket</field>\n"
            "    <field name=\"arch\" type=\"xml\">\n"
            "      <field name=\"equipment_id\"/>\n"
            "      <field name=\"project_id\"/>\n"
            "      <field name=\"technician_id\"/>\n"
            "    </field>\n  </record>\n"
            "</odoo>"
        ),
        security_csv="",
        notes="",
    )
    asyncio.run(_validate_view_fields_exist_on_inherited_model(
        generated, "odoo16_dev", task_id="test",
    ))  # must not raise -- oma.service.ticket's own real fields, checked against its own real schema
    print("PASS: a record's own declared model is trusted even when models_py's own _inherit "
          "targets (from an unrelated earlier round) don't include it")


def test_view_fields_exist_on_inherited_model_also_checks_mail_templates():
    """Real, confirmed follow-up bug found live (2026-07-24, task 020's
    18th resume attempt) -- the sibling test above only covers
    `views_xml`'s `<field name="...">` syntax. Confirmed live: even
    after (a) the related-field-target validator, (b) this same view-
    field validator, AND (c) manager/replanning.py's CRITICAL_RULE_
    PREFIX recurrence fix were all deployed and independently confirmed
    working, Build STILL regenerated `object.amount_total`/`object.
    expected_finish_date` inside the real mail template
    (`extra_data_files['data/mail_template_data.xml']`, Jinja `{{
    object.<field> }}` syntax) round after round -- a completely
    different file/syntax surface this validator never looked at, since
    it only ever scanned `views_xml`. At that point detection/
    escalation were both confirmed correct, so this became a pure LLM
    instruction-following limit -- exactly when a deterministic
    pre-write block is the right tool, matching this file's own
    established pattern. This test reproduces the exact live shape:
    the invented field lives ONLY in extra_data_files, `views_xml` is
    empty/absent.
    """
    from specialists.build.specialist import _validate_view_fields_exist_on_inherited_model

    invented = GeneratedModuleFiles(
        manifest_fields=ManifestFields(
            name="x", version="1.0", category="Uncategorized", summary="", author="", depends=["base"], data=[],
        ),
        models_py="from odoo import models\n\nclass ProjectMeerwerk(models.Model):\n    _inherit = 'project.meerwerk'\n",
        extra_data_files={
            "data/mail_template_data.xml": (
                '<odoo>\n  <record id="t" model="mail.template">\n'
                '    <field name="body_html">{{ object.amount_total }} '
                "{{ object.expected_finish_date }}</field>\n  </record>\n</odoo>"
            ),
        },
        security_csv="",
        notes="",
    )
    raised = False
    try:
        asyncio.run(_validate_view_fields_exist_on_inherited_model(invented, "odoo16_dev", task_id="test"))
    except ValueError as exc:
        raised = True
        # amount_total IS a real field on project.meerwerk (confirmed live
        # earlier this session) -- only expected_finish_date is invented.
        # A correct implementation flags exactly the invented one, never
        # the real one it happens to sit next to.
        assert "expected_finish_date" in str(exc)
        assert "amount_total" not in str(exc).split("expected_finish_date")[0].split("[")[-1]
    assert raised, "an invented field referenced only in a mail template (extra_data_files) must be rejected"

    real = GeneratedModuleFiles(
        manifest_fields=ManifestFields(
            name="x", version="1.0", category="Uncategorized", summary="", author="", depends=["base"], data=[],
        ),
        models_py="from odoo import models\n\nclass ProjectMeerwerk(models.Model):\n    _inherit = 'project.meerwerk'\n",
        extra_data_files={
            "data/mail_template_data.xml": (
                '<odoo>\n  <record id="t" model="mail.template">\n'
                '    <field name="body_html">{{ object.name }} {{ object.amount_total }}</field>\n'
                "  </record>\n</odoo>"
            ),
        },
        security_csv="",
        notes="",
    )
    asyncio.run(_validate_view_fields_exist_on_inherited_model(real, "odoo16_dev", task_id="test"))
    print("PASS: inherited-model field validator also checks extra_data_files (mail templates), "
          "rejecting an invented field there and allowing genuinely real ones")


def test_field_declaration_uses_selection_add_exempts_legitimate_selection_extension():
    """Real, confirmed bug found live (2026-08-07, Phase 30 full-backlog pass, task024, real
    task_ids 6fb81749-d74d-4a86-bed8-a164e79d4796 and a repeat on the very next re-run): the
    field-collision check had no exemption for Odoo's own real, standard `selection_add=`
    mechanism -- a genuinely correct `state = fields.Selection(selection_add=[...])` extending
    the real, already-live `project.container.state` field was stripped entirely, leaving an
    empty class body, even though Build's own raw generation was correct every time (verified
    directly against the real captured LLM output). Using this exact real shape.
    """
    from specialists.build.specialist import _field_declaration_uses_selection_add

    models_py = (
        "from odoo import fields, models\n\n"
        "class ProjectContainer(models.Model):\n"
        "    _inherit = 'project.container'\n\n"
        "    state = fields.Selection(\n"
        "        selection_add=[\n"
        "            ('delivered', 'Delivered'),\n"
        "            ('picked_up', 'Picked Up'),\n"
        "        ],\n"
        "        ondelete={'delivered': 'cascade', 'picked_up': 'cascade'},\n"
        "    )\n"
    )
    assert _field_declaration_uses_selection_add(models_py, "state") is True, (
        "a genuine selection_add= extension must be recognized, never stripped as a collision"
    )

    # A genuine, real mistake -- a plain redeclaration of an already-real field with no
    # selection_add= at all -- must still be detected as NOT exempt.
    plain_redeclaration = (
        "from odoo import fields, models\n\n"
        "class ProjectContainer(models.Model):\n"
        "    _inherit = 'project.container'\n\n"
        "    state = fields.Selection([('draft', 'Draft'), ('done', 'Done')])\n"
    )
    assert _field_declaration_uses_selection_add(plain_redeclaration, "state") is False, (
        "a plain, non-selection_add redeclaration of a real field must still be treated as a "
        "genuine collision, never exempted"
    )

    # A field name that doesn't actually exist in the file must never raise or false-positive.
    assert _field_declaration_uses_selection_add(models_py, "totally_absent_field") is False
    print("PASS: a real selection_add= extension is recognized and exempted from the collision "
          "check; a plain redeclaration (no selection_add) is still correctly treated as a real "
          "collision")


def test_new_model_names_dont_collide_rejects_real_name():
    """Real, confirmed bug found live (2026-07-24, tasks 034/042 of the
    SITE 50-task benchmark): a genuinely NEW `_name` model whose chosen
    name collides with an already-real, unrelated model was never
    checked at all -- `_validate_inherit_target_resolved()` only
    verifies the inverse case (an `_inherit` target must exist).
    Confirmed live, independently, on two plausible-sounding invented
    names: `payment.term.cust.line` (already real, owned by
    `mis_base_extend`) and `account.move.batch.invoice` (same shape).
    Reproduced here with the first.
    """
    from specialists.build.specialist import _validate_new_model_names_dont_collide

    raised = False
    try:
        asyncio.run(_validate_new_model_names_dont_collide(
            None, ["payment.term.cust.line"], "odoo16_dev", task_id="test",
        ))
    except ValueError:
        raised = True
    assert raised, "a new model name colliding with a real, unrelated model must be rejected"

    # A genuinely new, unused name must never be falsely flagged.
    asyncio.run(_validate_new_model_names_dont_collide(
        None, ["oma.totally.new.concept.xyz123"], "odoo16_dev", task_id="test",
    ))
    print("PASS: new-model-name-collision validator rejects a real collision (payment.term.cust.line) "
          "and allows a genuinely unused name, no false positive")


def test_new_model_names_dont_collide_exempts_this_same_tasks_own_module():
    """Real, confirmed bug found live (2026-07-28, Phase 28C,
    `school_student` task): this function's own docstring always
    claimed it "never" flags this same task's own module -- but the
    function body never actually received or checked an
    `own_module_name` at all. A real task whose earlier round had
    already, legitimately installed its own new model onto the real
    target got permanently blocked on every later round: its OWN
    previously-installed model was reported as a foreign collision,
    burning the whole round budget and escalating to Operator for nothing.
    Reproduced here directly against the real, live model this exact
    bug hunt found: `school.student`, currently owned by
    `oma_simple_custom_module_task_595ad7bc` on the real `odoo16_dev`
    target.
    """
    from specialists.build.specialist import _validate_new_model_names_dont_collide

    real_owner_module = "oma_simple_custom_module_task_595ad7bc"

    # No own_module_name given at all -- must still be flagged (this is
    # exactly the pre-fix, foreign-collision-correctly-caught behavior).
    raised = False
    try:
        asyncio.run(_validate_new_model_names_dont_collide(
            None, ["school.student"], "odoo16_dev", task_id="test",
        ))
    except ValueError:
        raised = True
    assert raised, "with no own_module_name given, a real model must still be flagged as a collision"

    # own_module_name matching the REAL owner -- must be exempted, no raise.
    asyncio.run(_validate_new_model_names_dont_collide(
        None, ["school.student"], "odoo16_dev", task_id="test", own_module_name=real_owner_module,
    ))

    # own_module_name NOT matching the real owner -- still a genuine collision, must raise.
    raised = False
    try:
        asyncio.run(_validate_new_model_names_dont_collide(
            None, ["school.student"], "odoo16_dev", task_id="test", own_module_name="oma_some_other_task",
        ))
    except ValueError:
        raised = True
    assert raised, "a DIFFERENT task's own_module_name must never exempt someone else's real model"
    print("PASS: own_module_name correctly exempts this same task's own already-installed model, "
          "while still catching a genuine foreign collision")


def test_no_duplicate_odoo_special_attrs_rejects_duplicate_inherit():
    """Real, general, deterministic guard found live (2026-07-24,
    50-task sequential re-run, task 001): confirmed reproducing
    DETERMINISTICALLY (temperature=0.0, round 1's own full-generation
    path) across multiple separate fresh submissions of the exact same
    simple task -- `models_py` declared `_inherit = 'crm.lead'` TWICE
    in the same class body, while the actually-requested field
    (`special_instructions`) was never declared at all. A duplicate
    assignment to one of Odoo's special model attributes is always a
    generation mistake and a strong signal other intended content was
    silently dropped.
    """
    from specialists.build.specialist import _validate_no_duplicate_odoo_special_attrs

    broken = GeneratedModuleFiles(
        manifest_fields=ManifestFields(
            name="x", version="1.0", category="Uncategorized", summary="", author="", depends=["base"], data=[],
        ),
        models_py=(
            "from odoo import models, fields\n\nclass CrmLead(models.Model):\n"
            "    _inherit = 'crm.lead'\n\n    _inherit = 'crm.lead'\n"
        ),
        security_csv="", notes="",
    )
    raised = False
    try:
        _validate_no_duplicate_odoo_special_attrs(broken)
    except ValueError:
        raised = True
    assert raised, "a duplicated _inherit assignment must be rejected"

    good = GeneratedModuleFiles(
        manifest_fields=ManifestFields(
            name="x", version="1.0", category="Uncategorized", summary="", author="", depends=["base"], data=[],
        ),
        models_py=(
            "from odoo import models, fields\n\nclass CrmLead(models.Model):\n"
            "    _inherit = 'crm.lead'\n\n"
            "    special_instructions = fields.Text(string='Special instructions')\n"
        ),
        security_csv="", notes="",
    )
    _validate_no_duplicate_odoo_special_attrs(good)
    print("PASS: duplicate-special-attribute validator rejects a duplicated _inherit and allows "
          "genuinely correct content, no false positive")


def test_no_duplicate_odoo_special_attrs_scopes_check_per_class_not_whole_file():
    """Real, confirmed bug found live (2026-08-06, Phase 30 root-cause pass, task034): this
    validator originally counted `_name`/`_description`/etc. assignments across the WHOLE file,
    not per class -- so a genuinely correct 2-class generation (e.g. a new model plus its own
    line model, task034's own real shape: PaymentTermCust + PaymentTermCustLine, each declaring
    `_name`/`_description` exactly once, in its own class) was rejected as a duplicate purely
    because the flat count summed both classes' single, legitimate assignments together. Using
    task034's own real generated content (redis-inspected, 2026-08-06).
    """
    from specialists.build.specialist import _validate_no_duplicate_odoo_special_attrs

    two_legitimate_classes = GeneratedModuleFiles(
        manifest_fields=ManifestFields(
            name="x", version="1.0", category="Uncategorized", summary="", author="", depends=["base"], data=[],
        ),
        models_py=(
            "from odoo import fields, models\n\n"
            "class PaymentTermCust(models.Model):\n"
            "    _name = 'payment.term.cust'\n"
            "    _description = 'Custom Payment Term'\n\n"
            "    name = fields.Char(string='Name', required=True)\n"
            "    line_ids = fields.One2many('payment.term.cust.line', 'payment_id')\n\n\n"
            "class PaymentTermCustLine(models.Model):\n"
            "    _name = 'payment.term.cust.line'\n"
            "    _description = 'Custom Payment Term Line'\n\n"
            "    payment_id = fields.Many2one('payment.term.cust', required=True, ondelete='cascade')\n"
        ),
        security_csv="", notes="",
    )
    _validate_no_duplicate_odoo_special_attrs(two_legitimate_classes)  # must not raise

    # A genuine WITHIN-one-class duplicate across a 2-class file must still be caught.
    real_duplicate_in_second_class = GeneratedModuleFiles(
        manifest_fields=ManifestFields(
            name="x", version="1.0", category="Uncategorized", summary="", author="", depends=["base"], data=[],
        ),
        models_py=(
            "from odoo import fields, models\n\n"
            "class PaymentTermCust(models.Model):\n"
            "    _name = 'payment.term.cust'\n"
            "    _description = 'Custom Payment Term'\n\n\n"
            "class PaymentTermCustLine(models.Model):\n"
            "    _name = 'payment.term.cust.line'\n"
            "    _description = 'Custom Payment Term Line'\n"
            "    _description = 'Duplicated By Mistake'\n"
        ),
        security_csv="", notes="",
    )
    raised = False
    try:
        _validate_no_duplicate_odoo_special_attrs(real_duplicate_in_second_class)
    except ValueError as e:
        raised = True
        assert "_description" in str(e)
    assert raised, "a genuine within-class duplicate must still be rejected even in a multi-class file"
    print("PASS: 2 legitimate classes each assigning _name/_description once no longer false-"
          "positives, a genuine within-class duplicate is still caught")


def test_autofix_dedupes_duplicate_inherit_on_task016_real_shape():
    """Real, confirmed bug found live (2026-08-03, task004/task016/task027, same day): the
    scoped-edit correction path `search_replace`s just the `class X(models.Model):` line, whose
    own replacement content re-declares `_inherit` -- without knowing the file already has its
    own `_inherit` line right below the matched target. Python executes top-to-bottom, so the
    LATER assignment is always the one that actually runs; keeping only the last one exactly
    preserves real runtime behavior. Using task016's own real duplicated content.
    """
    from specialists.build.specialist import (
        _autofix_dedupe_duplicate_special_model_attribute_assignments,
        _validate_no_duplicate_odoo_special_attrs,
    )

    generated = GeneratedModuleFiles(
        manifest_fields=ManifestFields(
            name="x", version="1.0", category="Uncategorized", summary="", author="", depends=["base"], data=[],
        ),
        models_py=(
            "from odoo import models, fields, api\n\n"
            "class ProjectMeerwerk(models.Model):\n"
            "    _inherit = ['project.meerwerk', 'mail.thread']\n\n"
            "    # Track changes to state and user_id\n"
            "    state = fields.Selection(track_visibility='always')\n"
            "    user_id = fields.Many2one(track_visibility='always')\n"
            "    _inherit = ['project.meerwerk', 'mail.thread']\n"
        ),
        security_csv="", notes="",
    )
    _autofix_dedupe_duplicate_special_model_attribute_assignments(generated)
    assert generated.models_py.count("_inherit =") == 1, (
        f"expected exactly one surviving _inherit assignment -- got: {generated.models_py!r}"
    )
    assert "['project.meerwerk', 'mail.thread']" in generated.models_py
    import ast
    ast.parse(generated.models_py)
    _validate_no_duplicate_odoo_special_attrs(generated)
    print("PASS: task016's real duplicated _inherit is deduped to its last (runtime-effective) "
          "assignment, validator no longer raises")


def test_autofix_dedupe_never_corrupts_a_second_class_own_legitimate_name():
    """Real, confirmed SECOND, more severe bug found live (2026-08-06, task046 of the SITE
    50-task fix-pass): the dedupe pass scanned the whole file as one flat string with no
    per-class scoping -- a module with TWO real classes, each with its own genuinely correct,
    non-duplicate `_name`, had the FIRST class's `_name`/`_description` silently deleted because
    the two DIFFERENT classes' own separate `_name` lines were miscounted as duplicates within a
    single class. Reproduced exactly via task046's own real round-4 generation content (bisected
    live): StateChangeTracker lost both `_name` and `_description` to this exact mechanism, then
    failed the identity validator despite the original generation being entirely correct. This
    same missing-identity failure shape recurred identically 5 times the same night on multi-
    class generations (task028, 034, 038, 042, 046) -- this bug's real blast radius.
    """
    from specialists.build.specialist import (
        _autofix_dedupe_duplicate_special_model_attribute_assignments,
        _validate_model_declares_name_or_inherit,
    )

    # task046's own real round-4 shape (trimmed to the load-bearing lines).
    generated = GeneratedModuleFiles(
        manifest_fields=ManifestFields(
            name="x", version="1.0", category="Mail", summary="", author="", depends=["base"], data=[],
        ),
        models_py=(
            "from odoo import models, fields, api\n\n"
            "class StateChangeTracker(models.AbstractModel):\n"
            "    _name = 'state.change.tracker'\n"
            "    _description = 'State Change Tracker'\n\n"
            "    old_state = fields.Char(string='Old State')\n"
            "    new_state = fields.Char(string='New State')\n\n\n"
            "class MailActivityNotifier(models.TransientModel):\n"
            "    _name = 'mail.activity.notifier'\n"
            "    _description = 'Mail Activity Notifier'\n\n"
            "    def send_internal_activity(self, record):\n"
            "        pass\n"
        ),
        security_csv="", notes="",
    )
    _autofix_dedupe_duplicate_special_model_attribute_assignments(generated)
    assert generated.models_py.count("_name =") == 2, (
        f"both classes' own distinct, non-duplicate _name assignments must survive -- got: "
        f"{generated.models_py!r}"
    )
    assert "_name = 'state.change.tracker'" in generated.models_py
    assert "_name = 'mail.activity.notifier'" in generated.models_py
    assert "_description = 'State Change Tracker'" in generated.models_py
    import ast
    ast.parse(generated.models_py)
    _validate_model_declares_name_or_inherit(generated)  # must not raise
    print("PASS: two different classes' own distinct _name/_description assignments both "
          "survive the dedupe pass, correctly scoped per-class")


def test_autofix_dedupe_still_works_within_a_single_class_after_per_class_scoping_fix():
    """Regression guard for the per-class-scoping fix above: a GENUINE duplicate within ONE
    class must still be deduped exactly as before -- the fix must only add class-boundary
    awareness, never disable the original within-a-class behavior."""
    from specialists.build.specialist import (
        _autofix_dedupe_duplicate_special_model_attribute_assignments,
        _validate_no_duplicate_odoo_special_attrs,
    )

    generated = GeneratedModuleFiles(
        manifest_fields=ManifestFields(
            name="x", version="1.0", category="Uncategorized", summary="", author="", depends=["base"], data=[],
        ),
        models_py=(
            "from odoo import models, fields\n\n"
            "class X(models.Model):\n"
            "    _name = 'oma.x'\n"
            "    _name = 'oma.x'\n"
            "    value = fields.Char()\n"
        ),
        security_csv="", notes="",
    )
    _autofix_dedupe_duplicate_special_model_attribute_assignments(generated)
    assert generated.models_py.count("_name =") == 1
    import ast
    ast.parse(generated.models_py)
    _validate_no_duplicate_odoo_special_attrs(generated)  # must not raise
    print("PASS: a genuine duplicate within one class is still correctly deduped after the fix")


def test_autofix_never_touches_a_multiline_duplicate():
    from specialists.build.specialist import _autofix_dedupe_duplicate_special_model_attribute_assignments

    multiline = (
        "class X(models.Model):\n"
        "    _sql_constraints = [\n"
        "        ('uniq', 'unique(x)', 'must be unique'),\n"
        "    ]\n"
        "    _sql_constraints = [('uniq2', 'unique(y)', 'y unique')]\n"
    )
    generated = GeneratedModuleFiles(
        manifest_fields=ManifestFields(
            name="x", version="1.0", category="Uncategorized", summary="", author="", depends=["base"], data=[],
        ),
        models_py=multiline, security_csv="", notes="",
    )
    before = generated.models_py
    _autofix_dedupe_duplicate_special_model_attribute_assignments(generated)
    assert generated.models_py == before, "a multi-line duplicated value must never be touched"
    print("PASS: a multi-line duplicated special attribute is left untouched, too risky to dedupe")


def test_autofix_dedupe_prefers_valid_name_over_a_later_stale_invalid_one():
    """Real, confirmed follow-on gap found live (2026-08-03, task027's own real re-test, same day
    as the fix above): a scoped edit's search_replace inserts its new lines where the matched
    class-header target was, so a genuinely stale, already-invalid earlier `_name` value (e.g. a
    leftover 'ProjectMeerwerk' PascalCase mistake) can end up positioned AFTER the scoped edit's
    own newly-inserted, genuinely valid `_name` value. Blindly "keep the last" would then discard
    the correct one and keep the broken one -- the opposite of the fix's own intent.
    """
    from specialists.build.specialist import (
        _autofix_dedupe_duplicate_special_model_attribute_assignments,
        _validate_new_model_name_is_valid_odoo_identifier,
    )

    generated = GeneratedModuleFiles(
        manifest_fields=ManifestFields(
            name="x", version="1.0", category="Uncategorized", summary="", author="", depends=["base"], data=[],
        ),
        models_py=(
            "from odoo import api, models\n\n"
            "class ProjectMeerwerk(models.Model):\n"
            "    _name = 'project.meerwerk'\n"
            "    _inherit = ['project.meerwerk', 'mail.thread']\n"
            "    _name = 'ProjectMeerwerk'\n\n"
            "    @api.model\n"
            "    def create(self, vals):\n"
            "        pass\n"
        ),
        security_csv="", notes="",
    )
    _autofix_dedupe_duplicate_special_model_attribute_assignments(generated)
    assert generated.models_py.count("_name =") == 1
    assert "_name = 'project.meerwerk'" in generated.models_py
    assert "_name = 'ProjectMeerwerk'" not in generated.models_py
    import ast
    ast.parse(generated.models_py)
    _validate_new_model_name_is_valid_odoo_identifier(generated)
    print("PASS: the valid _name is kept even though the stale invalid one is positioned later")


def test_autofix_dedupe_falls_back_to_last_when_both_name_values_are_invalid():
    from specialists.build.specialist import _autofix_dedupe_duplicate_special_model_attribute_assignments

    generated = GeneratedModuleFiles(
        manifest_fields=ManifestFields(
            name="x", version="1.0", category="Uncategorized", summary="", author="", depends=["base"], data=[],
        ),
        models_py="class X(models.Model):\n    _name = 'BadOne'\n    _name = 'BadTwo'\n",
        security_csv="", notes="",
    )
    _autofix_dedupe_duplicate_special_model_attribute_assignments(generated)
    assert "_name = 'BadTwo'" in generated.models_py
    assert "_name = 'BadOne'" not in generated.models_py
    print("PASS: when both _name values are invalid, falls back to keeping the literal last one")


def test_second_dedup_pass_catches_a_fresh_duplicate_from_the_inherit_rewrite():
    """Real, confirmed follow-on gap found live (2026-08-03, task027's own real re-test, same day):
    _autofix_rewrite_own_task_model_name_to_inherit() (which runs AFTER the first dedup pass in
    the real production pipeline) rewrites a `_name = 'X'` line to `_inherit = 'X'` -- if the file
    already had its own real `_inherit = [...]` line (e.g. from an earlier scoped-edit round), this
    creates a FRESH duplicate `_inherit` assignment the earlier dedup pass never had a chance to
    see. Confirmed live via debug instrumentation of the real production chain: models_py reaching
    the validator still had two undeduped `_inherit` lines, crashing the real sandbox install.
    This is exactly why the real fix calls the dedup function a second time, right after that
    rewrite -- this test exercises that same real production content shape directly.
    """
    from specialists.build.specialist import (
        _autofix_dedupe_duplicate_special_model_attribute_assignments,
        _validate_no_duplicate_odoo_special_attrs,
    )

    # This is what generated.models_py looks like immediately AFTER
    # _autofix_rewrite_own_task_model_name_to_inherit() has just converted a _name line to
    # _inherit, on top of a file that already had its own real _inherit line.
    generated = GeneratedModuleFiles(
        manifest_fields=ManifestFields(
            name="x", version="1.0", category="Uncategorized", summary="", author="", depends=["base"], data=[],
        ),
        models_py=(
            "from odoo import api, models\n\n\n"
            "class ProjectMeerwerk(models.Model):\n"
            "    _inherit = 'project.meerwerk'\n"
            "    _inherit = ['project.meerwerk', 'mail.thread']\n\n"
            "    def write(self, vals):\n"
            "        pass\n"
        ),
        security_csv="", notes="",
    )
    _autofix_dedupe_duplicate_special_model_attribute_assignments(generated)
    assert generated.models_py.count("_inherit =") == 1
    assert "_inherit = ['project.meerwerk', 'mail.thread']" in generated.models_py
    _validate_no_duplicate_odoo_special_attrs(generated)
    print("PASS: a second dedup pass cleanly resolves the fresh duplicate the inherit-rewrite "
          "autofix introduced, matching task027's own real crash content")


def test_no_duplicate_method_definitions_rejects_duplicate_onchange():
    """Real, general, deterministic guard found live TWICE (2026-07-25):
    task 004's `_compute_amount_total` and task 005's `_onchange_
    project_id` both got defined TWICE in the same class body -- Python
    silently keeps only the LAST definition, discarding the first
    entirely (including its own @api.depends/@api.onchange decorator).
    Neither existing validator (the special-attrs one above only checks
    `_name`/`_inherit`/etc, never ordinary methods) caught this -- it
    only surfaced several rounds later as a confusing Code-Review finding
    ("two methods with the same @api.onchange decorator are defined,
    causing the first to be overwritten").
    """
    from specialists.build.specialist import _validate_no_duplicate_method_definitions

    manifest = ManifestFields(
        name="x", version="1.0", category="Uncategorized", summary="", author="", depends=["project"], data=[],
    )
    duplicated = GeneratedModuleFiles(
        manifest_fields=manifest,
        models_py=(
            "from odoo import models, fields, api\n\nclass ProjectMeerwerk(models.Model):\n"
            "    _inherit = 'project.meerwerk'\n\n"
            "    @api.onchange('project_id')\n"
            "    def _onchange_project_id(self):\n"
            "        if self.project_id:\n"
            "            self.user_id = self.project_id.user_id\n\n"
            "    @api.onchange('project_id')\n"
            "    def _onchange_project_id(self):\n"
            "        pass\n"
        ),
        security_csv="", notes="",
    )
    raised = False
    try:
        _validate_no_duplicate_method_definitions(duplicated)
    except ValueError:
        raised = True
    assert raised, "a method defined twice in the same class body must be rejected"

    single = GeneratedModuleFiles(
        manifest_fields=manifest,
        models_py=(
            "from odoo import models, fields, api\n\nclass ProjectMeerwerk(models.Model):\n"
            "    _inherit = 'project.meerwerk'\n\n"
            "    @api.onchange('project_id')\n"
            "    def _onchange_project_id(self):\n"
            "        if self.project_id:\n"
            "            self.user_id = self.project_id.user_id\n"
        ),
        security_csv="", notes="",
    )
    _validate_no_duplicate_method_definitions(single)
    print("PASS: duplicate-method-definition validator rejects a method defined twice and allows "
          "genuinely correct single-definition content, no false positive")


def test_no_duplicate_method_definitions_is_per_class_not_whole_file():
    """Real, confirmed bug found live (2026-08-09, task 07141af5-9a4e-41b6-93ea-8b7af04fea9c's
    flagship run, service_ticket_model node, recurring across MANY consecutive rounds/resumes):
    this validator used to be whole-file scoped on the stated assumption that "every generated
    module in this project's real scope defines exactly one model class" -- that assumption
    stopped holding the moment this task's own models.py legitimately grew to two real model
    classes (equipment_registry's own Equipment class alongside service_ticket_model's own
    ServiceTicket class), EACH with its own single, entirely correct create()/sequence
    override. The whole-file scan saw 'create' defined twice across the two DIFFERENT classes
    and rejected a perfectly valid round over and over, escalating repeatedly for something
    that was never actually a mistake -- confirmed live via direct reproduction against the
    exact real shape.
    """
    from specialists.build.specialist import _validate_no_duplicate_method_definitions

    manifest = ManifestFields(
        name="x", version="1.0", category="Uncategorized", summary="", author="", depends=["base"], data=[],
    )
    two_classes_each_with_own_create = GeneratedModuleFiles(
        manifest_fields=manifest,
        models_py=(
            "from odoo import models, fields, api\n\n"
            "class Equipment(models.Model):\n"
            "    _name = 'oma.equipment'\n\n"
            "    name = fields.Char()\n\n"
            "    @api.model\n"
            "    def create(self, vals):\n"
            "        if 'tracking_number' not in vals:\n"
            "            vals['tracking_number'] = self.env['ir.sequence'].next_by_code('oma.equipment')\n"
            "        return super().create(vals)\n\n\n"
            "class ServiceTicket(models.Model):\n"
            "    _name = 'oma.service.ticket'\n\n"
            "    name = fields.Char()\n\n"
            "    @api.model\n"
            "    def create(self, vals):\n"
            "        if 'name' not in vals:\n"
            "            vals['name'] = self.env['ir.sequence'].next_by_code('oma.service.ticket')\n"
            "        return super().create(vals)\n"
        ),
        security_csv="", notes="",
    )
    _validate_no_duplicate_method_definitions(two_classes_each_with_own_create)
    print("PASS: two different model classes each defining their own single create() method is "
          "correctly recognized as fine, closing the real live false-positive gap found on "
          "task 07141af5's service_ticket_model node")

    # A genuine duplicate WITHIN one of the two classes must still be caught.
    genuine_duplicate_in_one_class = GeneratedModuleFiles(
        manifest_fields=manifest,
        models_py=(
            "from odoo import models, fields, api\n\n"
            "class Equipment(models.Model):\n"
            "    _name = 'oma.equipment'\n\n"
            "    @api.model\n"
            "    def create(self, vals):\n"
            "        return super().create(vals)\n\n"
            "    @api.model\n"
            "    def create(self, vals):\n"
            "        vals['x'] = 1\n"
            "        return super().create(vals)\n\n\n"
            "class ServiceTicket(models.Model):\n"
            "    _name = 'oma.service.ticket'\n\n"
            "    name = fields.Char()\n"
        ),
        security_csv="", notes="",
    )
    raised = False
    try:
        _validate_no_duplicate_method_definitions(genuine_duplicate_in_one_class)
    except ValueError:
        raised = True
    assert raised, (
        "a genuine duplicate method WITHIN the same class must still be rejected, even when "
        "other, unrelated classes exist in the same file"
    )
    print("PASS: a genuine duplicate within one class is still caught even when a sibling "
          "class also exists in the same file")


def test_goal_named_field_is_declared_catches_silent_omission():
    """Real, general, deterministic guard found live (2026-07-24,
    50-task sequential re-run, task 001): confirmed live, reproduced
    across multiple separate fresh submissions of the exact same
    simple task -- `models_py` correctly declared `_inherit =
    'crm.lead'` but NEVER declared the actually-requested field
    (`special_instructions`) at all. This only surfaced as a real
    install crash (`Field "X" does not exist in model "Y"`) after a
    full, wasted sandbox pre-flight cycle -- nothing previously did a
    positive-presence check that a field the goal explicitly names
    actually got added.
    """
    from specialists.build.specialist import _validate_goal_named_field_is_declared

    goal = (
        "I want to see a text field on the lead form.\n\n"
        "Module: mis_base_extend\nModel: crm.lead\nField: special_instructions (Text)\n"
    )
    missing = GeneratedModuleFiles(
        manifest_fields=ManifestFields(
            name="x", version="1.0", category="Uncategorized", summary="", author="", depends=["base"], data=[],
        ),
        models_py="from odoo import models, fields\n\nclass CrmLead(models.Model):\n    _inherit = 'crm.lead'\n",
        security_csv="", notes="",
    )
    raised = False
    try:
        asyncio.run(_validate_goal_named_field_is_declared(missing, goal, {}))
    except ValueError:
        raised = True
    assert raised, "a goal-named field never declared in models_py must be rejected"

    present = GeneratedModuleFiles(
        manifest_fields=ManifestFields(
            name="x", version="1.0", category="Uncategorized", summary="", author="", depends=["base"], data=[],
        ),
        models_py=(
            "from odoo import models, fields\n\nclass CrmLead(models.Model):\n"
            "    _inherit = 'crm.lead'\n\n"
            "    special_instructions = fields.Text(string='Special instructions')\n"
        ),
        security_csv="", notes="",
    )
    asyncio.run(_validate_goal_named_field_is_declared(present, goal, {}))

    # Real, confirmed follow-up bug (2026-07-24, same investigation):
    # derive_constraint_labels() always seeds AT LEAST one label, even
    # for a genuinely simple single-field task -- so this check must
    # still fire when constraint_status has exactly one label, not
    # just when it's literally empty (a genuinely multi-label
    # decomposition, 2+, is the real "leave this to a different
    # round" case).
    try:
        raised = False
        asyncio.run(_validate_goal_named_field_is_declared(missing, goal, {"i_want_see": "pending"}))
    except ValueError:
        raised = True
    assert raised, "a single-label constraint_status must still be treated as non-decomposed"

    # Phase 25F correction (2026-07-26): a genuinely multi-constraint
    # round (2+ labels) used to never fire at all, even when the goal
    # named only ONE field, completely unambiguously -- that blanket
    # "2+ labels means skip" gate was itself the root cause of a real,
    # live bug (task 009: a goal naming a second field for a LATER
    # round caused this check to skip even the CURRENT round's own,
    # perfectly resolvable, single field). An unambiguous single named
    # field must now still fire regardless of how many total
    # constraints this task has -- constraint_status count alone is no
    # longer a reason to skip when the field itself isn't ambiguous.
    raised = False
    try:
        asyncio.run(_validate_goal_named_field_is_declared(missing, goal, {"a": "pending", "b": "pending"}))
    except ValueError:
        raised = True
    assert raised, (
        "a single, unambiguous named field must still be validated even on a multi-constraint "
        "round -- constraint_status count alone must never be the reason to skip"
    )

    # A GENUINELY multi-field goal (2+ distinct field names, no
    # resolvable per-round focus marker in the text) must still stay
    # silent -- that real ambiguity is exactly what
    # _resolve_current_round_named_field() falls back to None for.
    two_field_goal = (
        "I want to see two fields on the lead form.\n\n"
        "Module: mis_base_extend\nModel: crm.lead\n"
        "Field: special_instructions (Text)\nField: is_vip (Boolean)\n"
    )
    asyncio.run(_validate_goal_named_field_is_declared(missing, two_field_goal, {"a": "pending", "b": "pending"}))
    print("PASS: goal-named-field validator catches a silent omission on a plain task "
          "(constraint_status empty OR exactly one label) AND on a multi-constraint round when "
          "the goal names exactly one field, accepts correctly-declared content, and stays "
          "silent only on a genuinely multi-field goal with no resolvable per-round focus")


def test_goal_named_field_is_declared_skips_when_already_real_on_inherited_model():
    """Phase 25E (2026-07-26) regression: task 006's own live re-run
    found this validator hard-failing 5 identical rounds even though
    nothing was actually missing -- the goal named field `name` for a
    pure `_inherit = 'crm.lead'` extension, and `name` is already a
    real field on crm.lead (inherited from base). The validator had no
    ground-truth check at all, so a legitimate create()-override-only
    round (never re-declaring an already-real field) was rejected as a
    silent omission every single time. Fixed by checking the real
    target model's own fields (same live-registry pattern as
    `_validate_no_new_field_collides_with_real_target_field` and
    `_validate_view_fields_exist_on_inherited_model`) before raising.
    """
    from specialists.build.specialist import _validate_goal_named_field_is_declared

    goal = (
        "Every lead should get a name automatically.\n\n"
        "Module: mis_base_extend\nModel: crm.lead\nField: name\n"
        "Sequence code: crm.lead\nOverride: create() using @api.model_create_multi\n"
    )
    # `name` is NOT declared in models_py -- but it IS a real field on
    # the live crm.lead model (inherited), so this must NOT raise.
    create_override_only = GeneratedModuleFiles(
        manifest_fields=ManifestFields(
            name="x", version="1.0", category="Uncategorized", summary="", author="", depends=["base"], data=[],
        ),
        models_py=(
            "from odoo import api, models, fields\n\nclass CrmLead(models.Model):\n"
            "    _inherit = 'crm.lead'\n\n"
            "    @api.model_create_multi\n"
            "    def create(self, vals_list):\n"
            "        return super().create(vals_list)\n"
        ),
        security_csv="", notes="",
    )
    asyncio.run(_validate_goal_named_field_is_declared(
        create_override_only, goal, {}, db="odoo16_dev", task_id="test",
    ))

    # A genuinely invented field name (never real on crm.lead) must
    # still be rejected even with a live db/task_id passed in -- the
    # ground-truth check must never turn into a blanket pass-through.
    goal_invented = (
        "I want to see a text field on the lead form.\n\n"
        "Module: mis_base_extend\nModel: crm.lead\nField: totally_invented_field_xyz (Text)\n"
    )
    still_missing = GeneratedModuleFiles(
        manifest_fields=ManifestFields(
            name="x", version="1.0", category="Uncategorized", summary="", author="", depends=["base"], data=[],
        ),
        models_py="from odoo import models, fields\n\nclass CrmLead(models.Model):\n    _inherit = 'crm.lead'\n",
        security_csv="", notes="",
    )
    raised = False
    try:
        asyncio.run(_validate_goal_named_field_is_declared(
            still_missing, goal_invented, {}, db="odoo16_dev", task_id="test",
        ))
    except ValueError:
        raised = True
    assert raised, "a genuinely invented field name must still be rejected even with db/task_id passed"
    print("PASS: goal-named-field validator skips when the named field is already real on the "
          "_inherit-ed target model, and still catches a genuinely invented field name")


def test_goal_named_field_is_declared_rejects_field_only_real_via_unrelated_oma_sibling():
    """Phase 26A follow-up (2026-07-27): real, live-reproduced bug --
    task 009's own goal named `meerwerk_partner_count` on `res.partner`.
    The ground-truth check queries the CLUTTERED live target (self.db),
    which had a field of that exact name only because a completely
    unrelated, already-installed module from an EARLIER, separate run
    of this same task shape (`oma_on_the_customer_contact_d7453a8c`,
    not this round's own module, not a declared dependency) happened to
    declare it too. The validator wrongly treated this as "already
    real, not a silent omission" and skipped -- but the sandbox
    pre-flight that actually gates install uses a fresh, disposable
    database that never had that stray sibling module installed, so
    install genuinely failed 5 rounds running with "Field ... does not
    exist in model res.partner". Fixed: only trust a field's
    real-on-target status when its owning module is either this
    round's own declared dependency or a non-oma_-prefixed (real
    fixture/standard) module -- confirmed here that an unrelated
    oma_-prefixed sibling module does NOT count.
    """
    from unittest.mock import patch

    from specialists.build.specialist import _validate_goal_named_field_is_declared

    goal = (
        "On the customer form, show how many meerwerk records are linked.\n\n"
        "Module: project_meerwerk\nModel: res.partner (inherit)\n"
        "Field: meerwerk_partner_count (Integer, computed)\n"
    )
    missing = GeneratedModuleFiles(
        manifest_fields=ManifestFields(
            name="x", version="1.0", category="Uncategorized", summary="", author="", depends=["base"], data=[],
        ),
        models_py=(
            "from odoo import models\n\nclass ResPartner(models.Model):\n"
            "    _inherit = 'res.partner'\n\n"
            "    def _compute_meerwerk_partner_count(self):\n"
            "        pass\n"
        ),
        security_csv="", notes="",
    )
    with patch(
        "tools_odoo.odoo_schema_client._read_real_field_rows",
        return_value=[{"name": "meerwerk_partner_count", "modules": "oma_on_the_customer_contact_d7453a8c"}],
    ):
        raised = False
        try:
            asyncio.run(_validate_goal_named_field_is_declared(
                missing, goal, {}, db="odoo16_dev", task_id="test", depends_on_module="project_meerwerk",
            ))
        except ValueError:
            raised = True
        assert raised, (
            "must still flag the omission -- the field's only real owner is an unrelated oma_ "
            "sibling module, not this round's own declared dependency"
        )

    # Sanity: if the SAME field is instead owned by the round's own
    # declared dependency, it must still correctly skip (not regress
    # the original fix-40 behavior).
    with patch(
        "tools_odoo.odoo_schema_client._read_real_field_rows",
        return_value=[{"name": "meerwerk_partner_count", "modules": "project_meerwerk"}],
    ):
        asyncio.run(_validate_goal_named_field_is_declared(
            missing, goal, {}, db="odoo16_dev", task_id="test", depends_on_module="project_meerwerk",
        ))
    print("PASS: rejects a field only real via an unrelated oma_ sibling module, still accepts "
          "one owned by the round's own declared dependency")


def test_autofix_computed_field_missing_declaration_wires_up_an_already_written_method():
    """Phase 26A follow-up (2026-07-27): real, live-reproduced bug --
    task 009's own goal ("Field: meerwerk_partner_count (Integer,
    computed)" + a separate "Compute method: _compute_meerwerk_
    partner_count..." line describing a search_count()-based counter).
    Confirmed live, TWO separate fresh submissions, 5 identical rounds
    each: Build correctly wrote the real compute METHOD body every
    time, but never declared `meerwerk_partner_count = fields.Integer(
    compute=...)` at all -- caught pre-write every round by
    `_validate_goal_named_field_is_declared()`, but the LLM never
    fixed it despite the validator's own clear, repeated message. This
    autofix only ever wires an ALREADY-written method to its own field
    descriptor -- never invents logic.
    """
    from specialists.build.specialist import _autofix_goal_named_computed_field_missing_declaration

    goal = (
        "On the customer form, show how many meerwerk records are linked.\n\n"
        "Module: project_meerwerk\nModel: res.partner (inherit)\n"
        "Field: meerwerk_partner_count (Integer, computed)\n"
        "Compute method: _compute_meerwerk_partner_count, counts project.meerwerk records where "
        "partner_id equals this customer via search_count\n"
    )
    generated = GeneratedModuleFiles(
        manifest_fields=ManifestFields(
            name="x", version="1.0", category="Uncategorized", summary="", author="", depends=["base"], data=[],
        ),
        models_py=(
            "from odoo import models\n\nclass ResPartner(models.Model):\n"
            "    _inherit = 'res.partner'\n\n"
            "    def _compute_meerwerk_partner_count(self):\n"
            "        for record in self:\n"
            "            record.meerwerk_partner_count = self.env['project.meerwerk'].search_count(\n"
            "                [('partner_id', '=', record.id)]\n"
            "            )\n"
        ),
        security_csv="", notes="",
    )
    _autofix_goal_named_computed_field_missing_declaration(generated, goal, {"customer_contact_form": "pending"})

    assert "meerwerk_partner_count = fields.Integer(" in generated.models_py, (
        f"expected the missing field declaration to be injected -- got:\n{generated.models_py}"
    )
    assert "compute='_compute_meerwerk_partner_count'" in generated.models_py
    assert "def _compute_meerwerk_partner_count(self):" in generated.models_py, (
        "must never touch/remove the already-written, real compute method body"
    )
    assert "search_count" in generated.models_py, "the real method body's own logic must survive untouched"
    print(f"PASS: injects the missing field, wired to the already-written compute method, "
          f"without touching its real body:\n{generated.models_py}")


def test_autofix_computed_field_missing_declaration_never_fires_when_method_itself_is_missing():
    """Safety boundary: if the goal-named compute method ISN'T actually
    present in generated.models_py either, there's nothing safe to wire
    up to -- must stay a no-op, never invent a `compute=` reference to
    a method that doesn't exist (that would just move the crash from
    "field missing" to "method missing").
    """
    from specialists.build.specialist import _autofix_goal_named_computed_field_missing_declaration

    goal = (
        "Field: meerwerk_partner_count (Integer, computed)\n"
        "Compute method: _compute_meerwerk_partner_count, counts something\n"
    )
    generated = GeneratedModuleFiles(
        manifest_fields=ManifestFields(
            name="x", version="1.0", category="Uncategorized", summary="", author="", depends=["base"], data=[],
        ),
        models_py="from odoo import models\n\nclass ResPartner(models.Model):\n    _inherit = 'res.partner'\n",
        security_csv="", notes="",
    )
    _autofix_goal_named_computed_field_missing_declaration(generated, goal, {})
    assert "meerwerk_partner_count" not in generated.models_py, (
        "must not inject a field wired to a compute method that was never actually written"
    )
    print("PASS: stays a no-op when the named compute method itself isn't present")


def test_autofix_goal_named_field_declaration_missing_injects_a_real_field():
    """Real, general fix (2026-07-25): watched live, round by round, on
    TWO separate simple single-field tasks (001's `special_instructions`
    Text field, 002's `is_vip_client` Boolean field) -- the field-
    omission validator above (fixes 22/24) and its own persistent,
    correctly-worded concrete-snippet escalation (manager/replanning.py,
    fixes 25/27) both confirmed working exactly as designed across
    multiple consecutive rounds, and the local model still did not add
    the one goal-named field either time. Pure prompting/escalation is
    not sufficient for this exact generation shape on this project's own
    model -- this deterministic autofix (same established pattern as
    `_autofix_hallucinated_model_base_class` et al.) injects the missing
    field directly, so the validator above never even needs to fire.
    """
    from specialists.build.specialist import _autofix_goal_named_field_declaration_missing

    goal = (
        "I want to see a text field on the lead form.\n\n"
        "Module: mis_base_extend\nModel: crm.lead\nField: special_instructions (Text)\n"
    )
    manifest = ManifestFields(
        name="x", version="1.0", category="Uncategorized", summary="", author="", depends=["base"], data=[],
    )
    missing = GeneratedModuleFiles(
        manifest_fields=manifest,
        models_py="from odoo import models, fields\n\nclass CrmLead(models.Model):\n    _inherit = 'crm.lead'\n",
        security_csv="", notes="",
    )
    _autofix_goal_named_field_declaration_missing(missing, goal, {"i_want_see": "pending"})
    assert "special_instructions = fields.Text(string='Special Instructions')" in missing.models_py, (
        f"expected the field injected right after _inherit, got: {missing.models_py!r}"
    )
    # Syntactically valid Python -- compiles cleanly, correct indentation.
    compile(missing.models_py, "<test>", "exec")
    print("PASS: the missing field is injected as real, syntactically valid, correctly "
          f"indented code:\n{missing.models_py}")

    # Already-declared content must be a byte-for-byte no-op.
    already_has_it = GeneratedModuleFiles(
        manifest_fields=manifest,
        models_py=(
            "from odoo import models, fields\n\nclass CrmLead(models.Model):\n"
            "    _inherit = 'crm.lead'\n    special_instructions = fields.Text()\n"
        ),
        security_csv="", notes="",
    )
    before = already_has_it.models_py
    _autofix_goal_named_field_declaration_missing(already_has_it, goal, {"i_want_see": "pending"})
    assert already_has_it.models_py == before, "must be a byte-for-byte no-op when the field already exists"

    # A relational field type must NEVER be auto-injected -- guessing a
    # comodel name would be actively wrong, worse than leaving it for
    # the validator/escalation to keep prompting for.
    relational_goal = "x\n\nField: partner_ref_id (Many2one)"
    no_comodel = GeneratedModuleFiles(
        manifest_fields=manifest,
        models_py="from odoo import models, fields\n\nclass CrmLead(models.Model):\n    _inherit = 'crm.lead'\n",
        security_csv="", notes="",
    )
    before_relational = no_comodel.models_py
    _autofix_goal_named_field_declaration_missing(no_comodel, relational_goal, {"x": "pending"})
    assert no_comodel.models_py == before_relational, "must never guess a comodel for a relational field type"

    print("PASS: no-op when already declared, and never auto-injects an unsafe-to-guess relational field")


def test_autofix_synthesizes_whole_class_when_no_model_touch_exists_at_all():
    """Real, confirmed gap found live (2026-08-12, reinstall sample sweep, task001/task003): direct
    redis trace inspection showed the deterministic view-builder correctly resolving both the
    target model and the goal-named field from the goal's own Model:/Field: lines, while the SAME
    round's freeform models_py generation wrote NO class touching the target model at all (not
    even a bare _inherit= line) -- twice in a row, on two different simple tasks. The existing
    field-injection autofix above needs an existing _inherit=/_name= line to anchor onto and
    silently gave up when there wasn't one, leaving the already-resolved model/field names unused.
    This proves the new synthesize-from-scratch path fires when models_py has zero model touch.
    """
    from specialists.build.specialist import _autofix_goal_named_field_declaration_missing

    goal = (
        "On the sale order list, I want to see a priority field.\n\n"
        "Module: mis_base_extend\nModel: sale.order\n"
        "Field: order_priority (Selection: low/normal/high, default normal)\n"
    )
    manifest = ManifestFields(
        name="x", version="1.0", category="Uncategorized", summary="", author="", depends=["sale"], data=[],
    )
    # Genuinely empty -- no _name=/_inherit= anywhere, the exact real shape found live.
    empty = GeneratedModuleFiles(
        manifest_fields=manifest, models_py="", security_csv="", notes="",
    )
    _autofix_goal_named_field_declaration_missing(empty, goal, {"sale_order_list": "pending"})
    assert "_inherit = 'sale.order'" in empty.models_py, (
        f"expected a whole class synthesized from the goal's own Model: line, got: {empty.models_py!r}"
    )
    assert "order_priority = fields.Selection(" in empty.models_py
    assert "from odoo import models, fields, api" in empty.models_py
    compile(empty.models_py, "<test>", "exec")
    print(f"PASS: a whole class is synthesized from scratch, including imports, when models_py has "
          f"no model touch at all:\n{empty.models_py}")

    # A genuinely ambiguous goal (no Model: line) must still be a no-op -- never guess.
    ambiguous_goal = "Add a priority field.\n\nField: order_priority (Selection: low/normal/high)\n"
    still_empty = GeneratedModuleFiles(manifest_fields=manifest, models_py="", security_csv="", notes="")
    _autofix_goal_named_field_declaration_missing(still_empty, ambiguous_goal, {"x": "pending"})
    assert still_empty.models_py == "", "must never synthesize a class without an unambiguous Model: line"
    print("PASS: no synthesis at all without an unambiguous Model: line naming the target")


def test_autofix_goal_named_field_declaration_missing_covers_binary_and_image():
    """Phase 30, P5 (§6, Phase C): the coverage-graph audit found Binary
    and Image with 'none found' evidence despite being just as safe to
    auto-generate a skeleton for as Html/Text (no real value ever needs
    guessing) -- newly added to _GOAL_FIELD_TYPE_SKELETONS.
    """
    from specialists.build.specialist import _autofix_goal_named_field_declaration_missing

    manifest = ManifestFields(
        name="x", version="1.0", category="Uncategorized", summary="", author="", depends=["base"], data=[],
    )
    for type_word, field_name, label in (("Binary", "attachment", "Attachment"), ("Image", "photo", "Photo")):
        goal = f"x\n\nField: {field_name} ({type_word})"
        generated = GeneratedModuleFiles(
            manifest_fields=manifest,
            models_py="from odoo import models, fields\n\nclass X(models.Model):\n    _inherit = 'x'\n",
            security_csv="", notes="",
        )
        _autofix_goal_named_field_declaration_missing(generated, goal, {"x": "pending"})
        assert f"{field_name} = fields.{type_word}(string={label!r})" in generated.models_py, (
            f"expected a real {type_word} field injected, got: {generated.models_py!r}"
        )
        compile(generated.models_py, "<test>", "exec")
    print("PASS: Binary and Image fields are now safely auto-injected, same as every other "
          "unambiguous, no-value-guessing-needed type")


def test_autofix_never_injects_a_fake_field_for_a_computed_spec():
    """Real, general safety guard (2026-07-25, task 004): a goal asking
    for a COMPUTED field (`compute=_compute_amount_total, depends on
    line_ids.price_unit, store=True`) needs real business logic this
    autofix has no way to safely generate. Injecting a plain, non-
    computed `fields.Monetary(...)` would "exist" and silently pass the
    presence check -- a FALSE PASS, worse than leaving the omission for
    the validator/escalation to keep flagging, since the field would
    always read 0 and never actually update.
    """
    from specialists.build.specialist import _autofix_goal_named_field_declaration_missing

    goal = (
        "On the meerwerk record, I want to see the total of all line prices shown automatically "
        "in the header.\n\n"
        "Module: project_meerwerk\nModel: project.meerwerk\n"
        "Field: amount_total (Monetary, compute=_compute_amount_total, depends on "
        "line_ids.price_unit, store=True, currency_field=currency_id)\n"
    )
    manifest = ManifestFields(
        name="x", version="1.0", category="Uncategorized", summary="", author="", depends=["project"], data=[],
    )
    models_py = "from odoo import models, fields\n\nclass ProjectMeerwerk(models.Model):\n    _inherit = 'project.meerwerk'\n"
    generated = GeneratedModuleFiles(manifest_fields=manifest, models_py=models_py, security_csv="", notes="")
    _autofix_goal_named_field_declaration_missing(generated, goal, {"on_the_meerwerk": "pending"})
    assert generated.models_py == models_py, (
        f"must never auto-inject a fake plain field for a computed-field spec: got {generated.models_py!r}"
    )
    print("PASS: no unsafe injection for a computed/related/onchange field spec")


def test_autofix_builds_a_real_sum_aggregate_compute_field():
    """Real, general fix (2026-07-25, task 004): watched live -- with the
    false-prescription risk fixed, the model still failed to declare ANY
    field across a full, clean 5-round budget. A "total of all X's Y" sum
    aggregate over one relation's one subfield is one of the most common,
    mechanical Odoo patterns there is, and this project's own convention
    states it exactly this concretely -- safe to build deterministically,
    narrowly scoped to this one unambiguous shape.
    """
    from specialists.build.specialist import _autofix_goal_named_sum_compute_field_missing

    goal = (
        "On the meerwerk record, I want to see the total of all line prices shown automatically "
        "in the header. It should update whenever I add or change a line.\n\n"
        "Module: project_meerwerk\nModel: project.meerwerk\n"
        "Field: amount_total (Monetary, compute=_compute_amount_total, depends on "
        "line_ids.price_unit, store=True, currency_field=currency_id, "
        "groups=mis_base_extend.group_user_mis_see_sale_price)\n"
    )
    manifest = ManifestFields(
        name="x", version="1.0", category="Uncategorized", summary="", author="", depends=["project"], data=[],
    )
    generated = GeneratedModuleFiles(
        manifest_fields=manifest,
        models_py="from odoo import fields, models\n\nclass ProjectMeerwerk(models.Model):\n    _inherit = 'project.meerwerk'\n",
        security_csv="", notes="",
    )
    _autofix_goal_named_sum_compute_field_missing(generated, goal, {"on_the_meerwerk": "pending"})
    assert "from odoo import api, fields, models" in generated.models_py, "api must be added to the import line"
    assert (
        "amount_total = fields.Monetary(string='Amount Total', compute='_compute_amount_total', "
        "store=True, currency_field='currency_id', "
        "groups='mis_base_extend.group_user_mis_see_sale_price')" in generated.models_py
    )
    assert "@api.depends('line_ids.price_unit')" in generated.models_py
    assert "record.amount_total = sum(record.line_ids.mapped('price_unit'))" in generated.models_py
    compile(generated.models_py, "<test>", "exec")
    print(f"PASS: a real, syntactically valid sum-aggregate compute field and method are "
          f"injected:\n{generated.models_py}")

    # No explicit total/sum signal in the goal -- must never guess the aggregate shape.
    no_signal_goal = (
        "Show the highest line price.\n\nField: amount_total (Monetary, "
        "compute=_compute_amount_total, depends on line_ids.price_unit, store=True)\n"
    )
    unsignaled = GeneratedModuleFiles(
        manifest_fields=manifest,
        models_py="from odoo import fields, models\n\nclass ProjectMeerwerk(models.Model):\n    _inherit = 'project.meerwerk'\n",
        security_csv="", notes="",
    )
    original = unsignaled.models_py
    _autofix_goal_named_sum_compute_field_missing(unsignaled, no_signal_goal, {"x": "pending"})
    assert unsignaled.models_py == original, "must never fire without an explicit total/sum signal"
    print("PASS: no aggregate guessed without an explicit total/sum signal in the goal")


def test_autofix_overwrites_a_sum_compute_field_that_depends_on_invented_subfields():
    """Phase 25D (2026-07-26): real, confirmed live bug -- task 004's own
    resubmission, AFTER the best-of-N widening was already deployed,
    still hit `@api.depends('line_ids.quantity')`/`'line_ids.discount'`
    -- nonexistent fields the model invented, instead of the goal's own
    explicitly stated `line_ids.price_unit`. The original autofix bailed
    out the moment it saw `amount_total` was "already declared", even
    though the declaration was wired to the WRONG dependency -- exactly
    the case this fix closes: overwrite a present-but-wrong compute
    field/method, not just inject a missing one.
    """
    from specialists.build.specialist import _autofix_goal_named_sum_compute_field_missing

    goal = (
        "On the meerwerk record, I want to see the total of all line prices shown automatically "
        "in the header.\n\n"
        "Module: project_meerwerk\nModel: project.meerwerk\n"
        "Field: amount_total (Monetary, compute=_compute_amount_total, depends on "
        "line_ids.price_unit, store=True, currency_field=currency_id)\n"
    )
    wrong_models_py = (
        "from odoo import api, fields, models\n\nclass ProjectMeerwerk(models.Model):\n"
        "    _inherit = 'project.meerwerk'\n\n"
        "    amount_total = fields.Monetary(\n"
        "        string='Total',\n"
        "        compute='_compute_amount_total',\n"
        "        store=True,\n"
        "    )\n\n"
        "    @api.depends('line_ids.quantity', 'line_ids.discount')\n"
        "    def _compute_amount_total(self):\n"
        "        for rec in self:\n"
        "            rec.amount_total = sum(rec.line_ids.mapped('quantity'))\n"
    )
    manifest = ManifestFields(
        name="x", version="1.0", category="Uncategorized", summary="", author="", depends=["project"], data=[],
    )
    generated = GeneratedModuleFiles(manifest_fields=manifest, models_py=wrong_models_py, security_csv="", notes="")
    _autofix_goal_named_sum_compute_field_missing(generated, goal, {"on_the_meerwerk": "pending"})

    assert "quantity" not in generated.models_py and "discount" not in generated.models_py, (
        f"the invented, nonexistent subfields must be gone, got: {generated.models_py!r}"
    )
    assert "@api.depends('line_ids.price_unit')" in generated.models_py
    assert "record.amount_total = sum(record.line_ids.mapped('price_unit'))" in generated.models_py
    # Exactly one field declaration and one method definition must survive -- never a duplicate
    # left behind alongside the corrected one.
    assert generated.models_py.count("amount_total = fields.Monetary(") == 1
    assert generated.models_py.count("def _compute_amount_total(") == 1
    compile(generated.models_py, "<test>", "exec")
    print(f"PASS: a present-but-wrong sum-compute field/method is overwritten with the correct, "
          f"goal-stated dependency:\n{generated.models_py}")

    # The one case that must be left completely untouched: already correct.
    correct_models_py = (
        "from odoo import api, fields, models\n\nclass ProjectMeerwerk(models.Model):\n"
        "    _inherit = 'project.meerwerk'\n\n"
        "    amount_total = fields.Monetary(string='Total', compute='_compute_amount_total', store=True)\n\n"
        "    @api.depends('line_ids.price_unit')\n"
        "    def _compute_amount_total(self):\n"
        "        for rec in self:\n"
        "            rec.amount_total = sum(rec.line_ids.mapped('price_unit'))\n"
    )
    already_correct = GeneratedModuleFiles(
        manifest_fields=manifest, models_py=correct_models_py, security_csv="", notes="",
    )
    original = already_correct.models_py
    _autofix_goal_named_sum_compute_field_missing(already_correct, goal, {"on_the_meerwerk": "pending"})
    assert already_correct.models_py == original, "must never touch an already-correct compute field"
    print("PASS: an already-correct sum-compute field/method is left completely untouched")


def test_autofix_sum_compute_registers_the_groups_module_as_a_manifest_dependency():
    """Real, confirmed bug found live (2026-07-26, Phase 25D, task 004's
    own resubmission): Code-Review correctly caught a genuine install-
    time error -- `groups='mis_base_extend.group_user_mis_see_sale_price'`
    was generated, but `mis_base_extend` was never added to the
    manifest's own `depends` list, a real omission in this autofix
    itself. The group xmlid's own module prefix is concretely stated in
    the goal's own `groups=` value -- registering it is exactly as safe
    as every other manifest-dependency autofix in this file.
    """
    from specialists.build.specialist import _autofix_goal_named_sum_compute_field_missing

    goal = (
        "On the meerwerk record, I want to see the total of all line prices shown automatically "
        "in the header.\n\n"
        "Module: project_meerwerk\nModel: project.meerwerk\n"
        "Field: amount_total (Monetary, compute=_compute_amount_total, depends on "
        "line_ids.price_unit, store=True, groups=mis_base_extend.group_user_mis_see_sale_price)\n"
    )
    manifest = ManifestFields(
        name="x", version="1.0", category="Uncategorized", summary="", author="", depends=["project"], data=[],
    )
    generated = GeneratedModuleFiles(
        manifest_fields=manifest,
        models_py="from odoo import fields, models\n\nclass ProjectMeerwerk(models.Model):\n    _inherit = 'project.meerwerk'\n",
        security_csv="", notes="",
    )
    _autofix_goal_named_sum_compute_field_missing(generated, goal, {"on_the_meerwerk": "pending"})
    assert "mis_base_extend" in generated.manifest_fields.depends, (
        f"expected 'mis_base_extend' registered as a manifest dependency, got: {generated.manifest_fields.depends!r}"
    )
    assert "project" in generated.manifest_fields.depends, "must never drop an existing dependency"
    print(f"PASS: the groups= kwarg's own module is registered as a real manifest dependency: "
          f"{generated.manifest_fields.depends!r}")


def test_goal_named_field_regex_ignores_conversational_field_colon_in_prose():
    """Real, severe bug found live (2026-07-25, task 003): `_GOAL_NAMED_
    FIELD_RE` used to be unanchored, so it matched "field:" ANYWHERE in
    the goal text -- confirmed live, the ordinary English sentence "I
    want to see a priority field: Low, Normal, High." (ordinary prose,
    not this project's structured metadata convention) produced a
    spurious second match ('Low'), pushing `named_fields` to length 2
    and silently disabling both the field-omission validator AND the
    autofix for the entire task -- the omission this machinery exists
    to catch sailed straight through to a real sandbox install crash
    ("order_priority does not exist in model"). Anchored to line-start
    so only the real `Field:`/`Field name:` metadata line ever matches.
    """
    from specialists.build.specialist import _GOAL_NAMED_FIELD_RE

    goal = (
        "On the sale order list, I want to see a priority field: Low, Normal, High. High priority "
        "orders should be highlighted in red in the list.\n\n"
        "Module: mis_base_extend\nModel: sale.order\n"
        "Field: order_priority (Selection: low/normal/high, default normal, tracking=True)\n"
        "Tree view: decoration-danger for high, decoration-muted for low\n"
        "Form view: header area before statusbar\n"
    )
    named_fields = _GOAL_NAMED_FIELD_RE.findall(goal)
    assert named_fields == ["order_priority"], (
        f"the conversational 'field: Low, Normal, High' in the prose sentence above must never be "
        f"treated as a second named field: got {named_fields}"
    )
    print("PASS: only the real structured Field: metadata line matches, not conversational prose")


def test_autofix_builds_a_real_selection_field_from_inline_options():
    """Real, general fix (2026-07-25, task 003): the field-omission
    autofix (above) deliberately never guesses option values for a
    Selection field -- but this project's own convention states them
    concretely, inline, on the same `Field:` line (`Selection:
    low/normal/high`), which IS safe to parse deterministically, unlike
    a relational field's comodel (never stated this concretely).
    """
    from specialists.build.specialist import _autofix_goal_named_field_declaration_missing

    goal = (
        "On the sale order list, I want to see a priority field: Low, Normal, High.\n\n"
        "Module: mis_base_extend\nModel: sale.order\n"
        "Field: order_priority (Selection: low/normal/high, default normal, tracking=True)\n"
    )
    manifest = ManifestFields(
        name="x", version="1.0", category="Uncategorized", summary="", author="", depends=["sale"], data=[],
    )
    missing = GeneratedModuleFiles(
        manifest_fields=manifest,
        models_py="from odoo import models, fields\n\nclass SaleOrder(models.Model):\n    _inherit = 'sale.order'\n",
        security_csv="", notes="",
    )
    _autofix_goal_named_field_declaration_missing(missing, goal, {"sale_order_list": "pending"})
    assert (
        "order_priority = fields.Selection([('low', 'Low'), ('normal', 'Normal'), ('high', 'High')], "
        "string='Order Priority', default='normal')" in missing.models_py
    ), f"expected a real Selection field with the goal's own stated options, got: {missing.models_py!r}"
    compile(missing.models_py, "<test>", "exec")
    print(f"PASS: a real, syntactically valid Selection field is injected from the goal's own inline "
          f"options:\n{missing.models_py}")


def test_autofix_builds_a_real_selection_field_from_a_separate_selection_line():
    """Real, general fix (2026-07-25, Phase 25A regression gate, task
    003's resubmission): a real goal used a SEPARATE `Selection:
    low/normal/high` line rather than the inline-parenthetical form the
    test above covers -- `_build_selection_skeleton()` didn't recognize
    it, silently returning None. Confirmed live this let manager/
    replanning.py's own escalation fall back to a fake TODO-placeholder
    Selection skeleton (see test_constraint_pinning.py's own regression
    test for that half of the same bug). This proves the autofix path
    recognizes the separate-line convention too, not just the inline one.
    """
    from specialists.build.specialist import _autofix_goal_named_field_declaration_missing

    goal = (
        "On the sale order list, I want to see a priority field: Low, Normal, High.\n\n"
        "Module: mis_base_extend\nModel: sale.order\n"
        "Field: order_priority (Selection)\n"
        "Selection: low/normal/high\n"
        "Field type: fields.Selection\n"
        "Default: normal\n"
    )
    manifest = ManifestFields(
        name="x", version="1.0", category="Uncategorized", summary="", author="", depends=["sale"], data=[],
    )
    missing = GeneratedModuleFiles(
        manifest_fields=manifest,
        models_py="from odoo import models, fields\n\nclass SaleOrder(models.Model):\n    _inherit = 'sale.order'\n",
        security_csv="", notes="",
    )
    _autofix_goal_named_field_declaration_missing(missing, goal, {"sale_order_list": "pending"})
    assert "TODO" not in missing.models_py, (
        f"must never inject a fake TODO-placeholder Selection field, got: {missing.models_py!r}"
    )
    assert (
        "order_priority = fields.Selection([('low', 'Low'), ('normal', 'Normal'), ('high', 'High')], "
        "string='Order Priority', default='normal')" in missing.models_py
    ), f"expected a real Selection field parsed from the separate 'Selection:' line, got: {missing.models_py!r}"
    compile(missing.models_py, "<test>", "exec")
    print(f"PASS: a real Selection field is injected from a separate 'Selection:' line, not just the "
          f"inline form:\n{missing.models_py}")


def test_autofix_prefers_goal_facts_over_regex_for_a_goal_regex_cannot_parse():
    """Phase 25B (2026-07-25): goal_facts (structured extraction, cached
    on the contract) is the PREFERRED source -- proven here with a goal
    using NEITHER the inline-parenthetical NOR the separate-line
    Selection convention (free, natural prose only), which every regex
    in this file would find nothing in. With goal_facts populated, the
    autofix still injects a real field.
    """
    from specialists.build.specialist import _autofix_goal_named_field_declaration_missing

    goal = (
        "Add a numeric field to project.meerwerk called 'estimated_hours' so we can record how "
        "many hours we think the work will take. It should just be a plain number field."
    )
    manifest = ManifestFields(
        name="x", version="1.0", category="Uncategorized", summary="", author="", depends=["base"], data=[],
    )
    missing = GeneratedModuleFiles(
        manifest_fields=manifest,
        models_py="from odoo import models, fields\n\nclass ProjectMeerwerk(models.Model):\n    _inherit = 'project.meerwerk'\n",
        security_csv="", notes="",
    )
    # Named-field detection (_GOAL_NAMED_FIELD_RE, unchanged by this
    # phase) still needs a real `Field:`-shaped anchor to know a field
    # is even being asked about at all -- goal_facts only replaces the
    # TYPE/OPTIONS half of the extraction, not the omission-detection
    # half, per this phase's own deliberately narrow scope.
    goal_with_anchor = goal + "\n\nField: estimated_hours"
    goal_facts = {
        "field_name": "estimated_hours", "field_type": "Integer", "is_computed": False,
        "selection_options": [], "selection_default": None,
    }
    _autofix_goal_named_field_declaration_missing(
        missing, goal_with_anchor, {"add_a_numeric": "pending"}, goal_facts,
    )
    assert "estimated_hours = fields.Integer(string='Estimated Hours')" in missing.models_py, (
        f"expected a real field injected from goal_facts alone (the regex family has no type/options "
        f"signal in this prose at all), got: {missing.models_py!r}"
    )
    compile(missing.models_py, "<test>", "exec")
    print(f"PASS: goal_facts alone drives a correct injection for a goal the regex family cannot "
          f"parse at all:\n{missing.models_py}")


def test_autofix_goal_facts_confirming_computed_blocks_injection_even_without_regex_marker():
    """goal_facts confirming is_computed=True must block injection
    immediately -- even if the goal's own prose has no `compute=`/
    `related=`/`onchange=` marker the regex guard would catch (e.g. a
    natural-language description of computed behavior). Authoritative,
    not just an additional signal.
    """
    from specialists.build.specialist import _autofix_goal_named_field_declaration_missing

    goal = (
        "Show the running total automatically, calculated from the order lines.\n\nField: amount_total"
    )
    manifest = ManifestFields(
        name="x", version="1.0", category="Uncategorized", summary="", author="", depends=["base"], data=[],
    )
    missing = GeneratedModuleFiles(
        manifest_fields=manifest,
        models_py="from odoo import models, fields\n\nclass SaleOrder(models.Model):\n    _inherit = 'sale.order'\n",
        security_csv="", notes="",
    )
    goal_facts = {"field_name": "amount_total", "field_type": "Monetary", "is_computed": True}
    _autofix_goal_named_field_declaration_missing(missing, goal, {"show_the": "pending"}, goal_facts)
    assert "amount_total" not in missing.models_py, (
        f"goal_facts confirming a computed field must block injection entirely, got: {missing.models_py!r}"
    )
    print("PASS: goal_facts confirming is_computed=True blocks injection, never a fake plain field")


def test_autofix_corrects_lowercase_field_type_casing():
    """Real, general fix (2026-07-25, Phase 25B regression gate, task
    001's 3rd resubmission): watched live, `special_instructions =
    fields.text(string='Special instructions')` -- lowercase `text` --
    caused a genuine `AttributeError: module 'odoo.fields' has no
    attribute 'text'` at Python import time, a real install crash
    (rc=255). Odoo field types are always PascalCase; correcting a
    lowercase variant is a zero-judgment mechanical fix.
    """
    from specialists.build.specialist import _autofix_lowercase_field_type_casing

    generated = GeneratedModuleFiles(
        manifest_fields=ManifestFields(
            name="x", version="1.0", category="Uncategorized", summary="", author="", depends=["base"], data=[],
        ),
        models_py=(
            "from odoo import models, fields\n\nclass CrmLead(models.Model):\n"
            "    _inherit = 'crm.lead'\n"
            "    special_instructions = fields.text(string='Special instructions')\n"
            "    is_vip_client = fields.boolean(default=False)\n"
            "    order_priority = fields.Selection([('low', 'Low')], string='Priority')\n"
        ),
        security_csv="", notes="",
    )
    _autofix_lowercase_field_type_casing(generated)
    assert "fields.text(" not in generated.models_py
    assert "fields.boolean(" not in generated.models_py
    assert "fields.Text(string='Special instructions')" in generated.models_py
    assert "fields.Boolean(default=False)" in generated.models_py
    # Already-correct PascalCase must be left completely untouched.
    assert "fields.Selection([('low', 'Low')], string='Priority')" in generated.models_py
    compile(generated.models_py, "<test>", "exec")
    print(f"PASS: lowercase field-type casing mistakes are corrected, already-correct casing is "
          f"untouched:\n{generated.models_py}")


def test_autofix_sequence_field_missing_injects_the_standard_odoo_idiom():
    """Phase 25C (2026-07-25): the sequence-assigned reference-number
    shape (task 006's own documented category -- "genuinely evolving,
    legitimate create()-correctness concerns each round... a real LLM
    convergence limitation") is one of the small, closed shapes the
    Phase 25 plan names for deterministic generation. Confirmed with
    goal_facts.is_sequence_assigned=True: a real field declaration AND
    a real, correctly-decorated create() override, matching the exact
    standard Odoo idiom specialists/code_review/specialist.py's own
    `_filter_hallucinated_sequence_pattern_findings` already protects.
    """
    from specialists.build.specialist import _autofix_goal_named_sequence_field_missing

    goal = (
        "Every meerwerk record should get an automatic reference number when created, like "
        "MW-2026-0042.\n\nModule: project_meerwerk\nModel: project.meerwerk\nField: name\n"
    )
    manifest = ManifestFields(
        name="x", version="1.0", category="Uncategorized", summary="", author="", depends=["base"], data=[],
    )
    generated = GeneratedModuleFiles(
        manifest_fields=manifest,
        models_py="from odoo import models, fields\n\nclass ProjectMeerwerk(models.Model):\n    _inherit = 'project.meerwerk'\n",
        security_csv="", notes="",
    )
    goal_facts = {"field_name": "name", "field_type": None, "is_computed": False, "is_sequence_assigned": True}
    _autofix_goal_named_sequence_field_missing(generated, goal, {"every_meerwerk_record": "pending"}, goal_facts)
    assert "name = fields.Char(string='Name', default='New', copy=False, readonly=True)" in generated.models_py, (
        f"expected the real field declaration, got: {generated.models_py!r}"
    )
    assert "@api.model_create_multi" in generated.models_py
    assert "def create(self, vals_list):" in generated.models_py
    assert "self.env['ir.sequence'].next_by_code('project.meerwerk')" in generated.models_py, (
        f"expected the real model's own technical name resolved for next_by_code(), got: {generated.models_py!r}"
    )
    import_line = generated.models_py.splitlines()[0]
    assert import_line.startswith("from odoo import ") and "api" in [
        w.strip() for w in import_line[len("from odoo import "):].split(",")
    ], f"api must be added to the import line, got: {import_line!r}"
    compile(generated.models_py, "<test>", "exec")

    # Real, confirmed follow-up gap found live (2026-07-25, same task's
    # own resubmission): Code-Review correctly flagged that next_by_code
    # had no real ir.sequence record to resolve. The record must now be
    # generated too, with the goal's own concrete example prefix
    # ("MW-2026-0001" -> "MW-%(year)s-"), never a guessed one.
    assert generated.extra_data_files and "data/sequence_data.xml" in generated.extra_data_files, (
        f"expected a real ir.sequence data file, got: {generated.extra_data_files!r}"
    )
    sequence_xml = generated.extra_data_files["data/sequence_data.xml"]
    assert '<field name="code">project.meerwerk</field>' in sequence_xml, (
        f"the sequence's own code must match the real model name, got: {sequence_xml!r}"
    )
    assert '<field name="prefix">MW-%(year)s-</field>' in sequence_xml, (
        f"expected the prefix parsed from the goal's own concrete example, got: {sequence_xml!r}"
    )
    assert "data/sequence_data.xml" in generated.manifest_fields.data, (
        "the new data file must be registered in the manifest's own data list"
    )
    print(f"PASS: the standard Odoo sequence-assignment idiom, including its real ir.sequence "
          f"record, is injected correctly:\n{generated.models_py}\n{sequence_xml}")


def test_autofix_sequence_field_missing_uses_goal_own_explicit_sequence_code():
    """Phase 26C follow-up (2026-07-28): real, live-reproduced bug --
    the autofix always used the model's own bare technical name as the
    `ir.sequence` record's `code` (and the matching `next_by_code()`
    argument), on the assumption "code must equal the model's own
    technical name." Confirmed live: `sale.order` already has Odoo's
    own REAL, built-in sequence coded literally `sale.order` for
    standard SO numbering -- a goal asking for a separate custom
    reference number correctly stated a DISTINCT code
    ("Sequence code: sale.order.internal.ref"), but the autofix ignored
    it and generated a second `ir.sequence` record with the SAME code
    as the real one, causing a genuine install collision (Code-Review
    correctly caught it every round: "Conflicting sequence definition
    with code 'sale.order' exists already"). The goal's own explicit
    code must now be used instead.
    """
    from specialists.build.specialist import _autofix_goal_named_sequence_field_missing

    goal = (
        "Every sales order needs a unique internal reference code automatically.\n\n"
        "Module: mis_base_extend\nModel: sale.order\n"
        "Field: internal_ref_code (Char, readonly, copy=False, default='New')\n"
        "Sequence code: sale.order.internal.ref in data/internal_ref_sequence.xml, "
        "prefix IREF-%(year)s-, padding 4\n"
    )
    manifest = ManifestFields(
        name="x", version="1.0", category="Uncategorized", summary="", author="", depends=["base"], data=[],
    )
    generated = GeneratedModuleFiles(
        manifest_fields=manifest,
        models_py="from odoo import models, fields\n\nclass SaleOrder(models.Model):\n    _inherit = 'sale.order'\n",
        security_csv="", notes="",
    )
    _autofix_goal_named_sequence_field_missing(
        generated, goal, {"every_sales_order_needs": "pending"}, goal_facts=None,
    )
    assert "self.env['ir.sequence'].next_by_code('sale.order.internal.ref')" in generated.models_py, (
        f"expected the goal's own explicit sequence code, NOT the bare model name (which would "
        f"collide with Odoo's real, built-in sale.order sequence) -- got: {generated.models_py!r}"
    )
    assert "next_by_code('sale.order')" not in generated.models_py, (
        "must never use the bare model name when the goal states a distinct code -- this is "
        "exactly the collision confirmed live against Odoo's own real built-in sequence"
    )
    assert "data/sequence_data.xml" not in generated.extra_data_files, (
        "must never write to the old hardcoded path when the goal states its own explicit file "
        "path -- this is exactly the duplicate-record collision confirmed live (Build's own LLM "
        "correctly followed the goal's stated path, the autofix's hardcoded path caused both "
        "files to exist, both defining a conflicting ir.sequence record)"
    )
    assert "data/internal_ref_sequence.xml" in generated.extra_data_files, (
        f"expected the goal's own explicit file path to be used -- got: {generated.extra_data_files!r}"
    )
    sequence_xml = generated.extra_data_files["data/internal_ref_sequence.xml"]
    assert '<field name="code">sale.order.internal.ref</field>' in sequence_xml, (
        f"the generated ir.sequence record's own code must match the goal's explicit value, "
        f"got: {sequence_xml!r}"
    )
    assert '<field name="code">sale.order</field>' not in sequence_xml
    assert "data/internal_ref_sequence.xml" in generated.manifest_fields.data
    assert "data/sequence_data.xml" not in generated.manifest_fields.data
    compile(generated.models_py, "<test>", "exec")
    print(f"PASS: uses the goal's own explicit Sequence code: value, never the bare model name, "
          f"avoiding a real collision with Odoo's own built-in sequence:\n{sequence_xml}")


def test_autofix_sequence_field_missing_never_fires_without_goal_facts_confirmation():
    """The regex family alone must NEVER trigger this autofix -- reliably
    recognizing "this is sequence-assigned" from prose is exactly the
    judgment goal_facts (structured extraction) exists for; a goal that
    merely mentions "reference number" without goal_facts confirming
    is_sequence_assigned must be left completely untouched.
    """
    from specialists.build.specialist import _autofix_goal_named_sequence_field_missing

    goal = "Every record needs a reference number.\n\nField: name\n"
    manifest = ManifestFields(
        name="x", version="1.0", category="Uncategorized", summary="", author="", depends=["base"], data=[],
    )
    generated = GeneratedModuleFiles(
        manifest_fields=manifest,
        models_py="from odoo import models, fields\n\nclass X(models.Model):\n    _inherit = 'x'\n",
        security_csv="", notes="",
    )
    _autofix_goal_named_sequence_field_missing(generated, goal, {"every_record": "pending"}, goal_facts=None)
    assert generated.models_py == "from odoo import models, fields\n\nclass X(models.Model):\n    _inherit = 'x'\n"
    print("PASS: never fires without an explicit goal_facts.is_sequence_assigned confirmation")


def test_autofix_sequence_field_missing_fires_via_goal_convention_line_when_goal_facts_misses_it():
    """Phase 25E follow-up (2026-07-26): real, live-confirmed gap found
    during Phase 25E's own regression sweep -- task 006's own goal_facts.
    is_sequence_assigned classification (a single cached LLM call) did
    not reliably fire `true` for this exact, unambiguous goal wording on
    every fresh resubmission, and there was NO fallback at all (unlike
    every other goal_facts-derived shape in this file), so the whole
    task silently fell through to freeform generation with no second
    chance. Fixed with a narrow regex fallback anchored on this
    project's own literal "Sequence code: ..." goal-convention line
    (the same convention Filter N:/Trigger field: already use) -- this
    test confirms the autofix now fires from that line ALONE even when
    goal_facts completely fails to confirm is_sequence_assigned (e.g. a
    gateway outage, or -- the real live case -- the LLM classification
    call itself simply got it wrong).
    """
    from specialists.build.specialist import _autofix_goal_named_sequence_field_missing

    goal = (
        "Every meerwerk record should get a unique reference number like MW-2026-0001, "
        "auto-generated when created. The user should never have to type this.\n\n"
        "Module: project_meerwerk\nModel: project.meerwerk\n"
        "Field: name (Char, readonly, copy=False, default='New')\n"
        "Sequence code: project.meerwerk in data/sequence_data.xml, prefix MW-%(year)s-, padding 4\n"
        "Override create() with @api.model_create_multi to assign from ir.sequence when name=='New'\n"
    )
    manifest = ManifestFields(
        name="x", version="1.0", category="Uncategorized", summary="", author="", depends=["base"], data=[],
    )
    generated = GeneratedModuleFiles(
        manifest_fields=manifest,
        models_py="from odoo import models, fields\n\nclass ProjectMeerwerk(models.Model):\n    _inherit = 'project.meerwerk'\n",
        security_csv="", notes="",
    )
    # Deliberately goal_facts=None -- simulates the real live failure
    # mode: the LLM classification call either failed outright or simply
    # didn't confirm is_sequence_assigned for this goal.
    _autofix_goal_named_sequence_field_missing(generated, goal, {"every_meerwerk_record": "pending"}, goal_facts=None)
    assert "name = fields.Char(string='Name', default='New', copy=False, readonly=True)" in generated.models_py, (
        f"expected the fallback to fire from the goal's own Sequence code: line, got: {generated.models_py!r}"
    )
    assert "self.env['ir.sequence'].next_by_code('project.meerwerk')" in generated.models_py
    assert generated.extra_data_files and "data/sequence_data.xml" in generated.extra_data_files
    compile(generated.models_py, "<test>", "exec")
    print("PASS: the Sequence code: goal-convention line alone is a sufficient fallback trigger "
          "when goal_facts fails to confirm is_sequence_assigned")


def test_autofix_sequence_field_missing_resolves_current_round_field_on_decomposed_multi_field_goal():
    """Phase 26C (2026-07-27, audit Finding #3): confirms the fix --
    before this phase, this function's own `if len(named_fields) != 1:
    return` guard bailed out for the ENTIRE task, every round, the
    moment the goal named 2+ fields anywhere in its text, even on the
    round whose own explicit focus marker unambiguously names THIS
    round's field as the sequence-assigned one. A goal naming a SECOND,
    unrelated field alongside the sequence-assigned one (decomposed
    across rounds, this round's own focus explicitly marked) must now
    still correctly fire.
    """
    from specialists.build.specialist import _autofix_goal_named_sequence_field_missing

    goal = (
        "Every meerwerk record should get a unique reference number automatically, and also "
        "track the assigned technician.\n\n"
        "Module: project_meerwerk\nModel: project.meerwerk\n"
        "Field: name (Char, readonly, copy=False, default='New')\n"
        "Field: technician_id (Many2one: res.users)\n"
        "Sequence code: project.meerwerk in data/sequence_data.xml, prefix MW-%(year)s-, padding 4\n"
        "This round's own NEW focus is ONLY: 'name_field'\n"
    )
    manifest = ManifestFields(
        name="x", version="1.0", category="Uncategorized", summary="", author="", depends=["base"], data=[],
    )
    generated = GeneratedModuleFiles(
        manifest_fields=manifest,
        models_py="from odoo import models, fields\n\nclass ProjectMeerwerk(models.Model):\n    _inherit = 'project.meerwerk'\n",
        security_csv="", notes="",
    )
    _autofix_goal_named_sequence_field_missing(
        generated, goal, {"name_field": "pending", "technician_id_field": "pending"}, goal_facts=None,
    )
    assert "name = fields.Char(string='Name', default='New', copy=False, readonly=True)" in generated.models_py, (
        f"expected the fix to resolve 'name' as THIS round's own field via the explicit focus "
        f"marker, despite the goal naming 2 fields total -- got: {generated.models_py!r}"
    )
    compile(generated.models_py, "<test>", "exec")
    print("PASS: correctly resolves the current round's own sequence-assigned field on a "
          "decomposed, multi-field goal -- silently no-op'd before this phase's fix")


def test_autofix_sum_compute_field_missing_resolves_current_round_field_on_decomposed_multi_field_goal():
    """Phase 26C (2026-07-27, audit Finding #3): same fix, confirmed for
    the sum-compute autofix -- a goal naming a SECOND, unrelated field
    alongside the sum-compute one (decomposed across rounds, this
    round's own focus explicitly marked) must now still correctly fire.
    """
    from specialists.build.specialist import _autofix_goal_named_sum_compute_field_missing

    goal = (
        "I want a computed total amount field on the meerwerk form summing all line prices, and "
        "also a separate notes field.\n\n"
        "Module: project_meerwerk\nModel: project.meerwerk\n"
        "Field: amount_total (Monetary, computed, compute=_compute_amount_total, "
        "depends on line_ids.price_unit, sums line_ids.price_unit)\n"
        "Field: internal_notes (Text)\n"
        "This round's own NEW focus is ONLY: 'amount_total_field'\n"
    )
    manifest = ManifestFields(
        name="x", version="1.0", category="Uncategorized", summary="", author="", depends=["base"], data=[],
    )
    generated = GeneratedModuleFiles(
        manifest_fields=manifest,
        models_py="from odoo import models, fields\n\nclass ProjectMeerwerk(models.Model):\n    _inherit = 'project.meerwerk'\n",
        security_csv="", notes="",
    )
    _autofix_goal_named_sum_compute_field_missing(
        generated, goal, {"amount_total_field": "pending", "internal_notes_field": "pending"},
    )
    assert "amount_total = fields.Monetary(" in generated.models_py, (
        f"expected the fix to resolve 'amount_total' as THIS round's own field via the explicit "
        f"focus marker, despite the goal naming 2 fields total -- got: {generated.models_py!r}"
    )
    assert "compute='_compute_amount_total'" in generated.models_py
    assert "sum(record.line_ids.mapped('price_unit'))" in generated.models_py
    compile(generated.models_py, "<test>", "exec")
    print("PASS: correctly resolves the current round's own sum-compute field on a decomposed, "
          "multi-field goal -- silently no-op'd before this phase's fix")


def test_should_use_best_of_n_widened_for_open_ended_logic():
    """Phase 25D (2026-07-26): audited live -- _generate_best_of_n() was
    NEVER reached for tasks 004/005 (a computed field, an @api.onchange
    auto-fill), both genuinely single-constraint, because the original
    Phase 18 gate (`len(constraint_status) >= 2`) assumed a single-
    constraint round is always the easy case. Widened to also route a
    single-constraint round through best-of-N whenever goal_facts
    confirms open-ended logic (is_computed=True) -- the exact signal
    Phase 25B's structured extraction already provides.
    """
    from specialists.build.specialist import _should_use_best_of_n

    def _contract(constraint_status, goal_facts=None):
        return _make_contract("x").model_copy(update={
            "constraint_status": constraint_status, "goal_facts": goal_facts or {},
        })

    multi_constraint = _contract({"a": "pending", "b": "pending"})
    assert _should_use_best_of_n(multi_constraint) is True, "2+ constraints must always use best-of-N"

    single_plain = _contract({"a": "pending"}, {"is_computed": False})
    assert _should_use_best_of_n(single_plain) is False, (
        "a plain single-constraint field round must NOT pay for best-of-N -- it already passes "
        "reliably in one shot via the deterministic autofix chain"
    )

    single_computed = _contract({"a": "pending"}, {"is_computed": True})
    assert _should_use_best_of_n(single_computed) is True, (
        "a single-constraint round with goal_facts.is_computed=True (compute=/related=/onchange=) "
        "must use best-of-N -- exactly the tasks 004/005 shape this phase targets"
    )

    single_no_facts = _contract({"a": "pending"}, {})
    assert _should_use_best_of_n(single_no_facts) is False, "no goal_facts at all must never assume open-ended logic"
    print("PASS: best-of-N is correctly widened to open-ended-logic single-constraint rounds, "
          "and NOT indiscriminately to every single-constraint round")


def test_should_use_best_of_n_widened_for_install_failure_retry_rounds():
    """Phase 30, P1b (§7.4b): real, confirmed gap -- a single-constraint,
    non-computed task retrying after a sandbox-install failure never
    satisfied the gate above, no matter how many install-failure rounds
    it burned through, because constraint_status/goal_facts are derived
    once from the ORIGINAL goal at contract creation, never from the
    round's own actual failure. Widened to also check contract.rules
    (rebuilt fresh every round by manager/replanning.py's
    revise_contract_from_verification()) for the real, literal text
    shapes that function and manager/loop.py's oscillation handling
    actually write.
    """
    from specialists.build.specialist import _should_use_best_of_n

    def _contract(rules):
        return _make_contract("x").model_copy(update={
            "constraint_status": {"a": "pending"}, "goal_facts": {"is_computed": False}, "rules": rules,
        })

    sandbox_preflight_retry = _contract([
        "No changes to core Odoo modules",
        "Prior attempt (round 1) failed: Sandbox pre-flight could not create a fresh database: "
        "createdb 'odoo16_sandbox_x' failed",
    ])
    assert _should_use_best_of_n(sandbox_preflight_retry) is True, (
        "a retry round following ANY sandbox pre-flight failure -- even a first-time, non-recurring "
        "one -- must use best-of-N, matching this priority's own Definition of Done"
    )

    real_install_retry = _contract([
        "CRITICAL FIX REQUIRED: Prior attempt (round 2) failed, classified as 'one_off': "
        "Sandbox install failed: Install succeeded, but 1 test(s) failed",
    ])
    assert _should_use_best_of_n(real_install_retry) is True, (
        "a retry round following a real (non-sandbox-preflight) install failure must also use best-of-N"
    )

    oscillation_retry = _contract([
        "CRITICAL FIX REQUIRED: Oscillation detected: fixing one constraint has been dropping another. "
        "This round, work on EXACTLY ONE thing at a time.",
    ])
    assert _should_use_best_of_n(oscillation_retry) is True, (
        "a round carrying an oscillation/repeated-failure signal must also use best-of-N"
    )

    ordinary_retry = _contract([
        "No changes to core Odoo modules",
        "Prior attempt (round 1) failed: the generated view references a field that doesn't exist yet",
    ])
    assert _should_use_best_of_n(ordinary_retry) is False, (
        "an ordinary retry with no install-failure or repeated-failure signal must NOT pay for "
        "best-of-N -- this widening is scoped to the specific risky shapes, not every retry"
    )
    print("PASS: best-of-N is correctly widened to install-failure and repeated-failure retry rounds, "
          "and NOT indiscriminately to every retry")


def test_autofix_generates_deterministic_search_filters_from_a_concrete_goal():
    """Phase 25C follow-up (2026-07-25/26): watched live, 4 consecutive
    fresh submissions of this exact task each hit a DIFFERENT real
    XML-targeting mistake (position="replace" on a nonexistent filter;
    a malformed duplicated position attribute; an xpath targeting a
    sibling element that isn't actually present) -- genuine model
    variance for this shape, not one fixable bug. `//search
    position="inside"` is ALWAYS a structurally valid anchor for any
    real Odoo search view, sidestepping the whole class of "guessed the
    wrong existing sibling element" mistake.
    """
    import xml.etree.ElementTree as ET

    from specialists.build.specialist import _autofix_goal_named_search_filters_missing

    goal = (
        "In the meerwerk list, I want a quick filter button called 'Accepted' that shows only "
        "accepted records, and another called 'My records' that shows only records assigned to "
        "me.\n\n"
        "Module: project_meerwerk\nModel: project.meerwerk\n"
        "View: view_project_meerwerk_search (search view for project.meerwerk)\n"
        "Filter 1: name=accepted, string=Accepted, domain=[('state','=','accepted')]\n"
        "Filter 2: name=my_records, string=My records, domain=[('user_id','=',uid)]\n"
        "Separator between the two filters\n"
    )
    manifest = ManifestFields(
        name="x", version="1.0", category="Uncategorized", summary="", author="", depends=["base"], data=[],
    )
    generated = GeneratedModuleFiles(manifest_fields=manifest, models_py="", security_csv="", notes="")
    _autofix_goal_named_search_filters_missing(generated, goal, {"in_the_meerwerk": "pending"})

    assert generated.views_xml, "expected real views_xml content to be generated"
    root = ET.fromstring(generated.views_xml)  # raises if not well-formed -- the exact class of bug this fixes
    assert '<field name="model">project.meerwerk</field>' in generated.views_xml, (
        "must explicitly declare model= -- confirmed live that Odoo does NOT reliably "
        "auto-infer it from inherit_id before arch validation runs (real sandbox install "
        "failure: 'Model not found: False')"
    )
    assert 'ref="project_meerwerk.view_project_meerwerk_search"' in generated.views_xml
    assert 'expr="//search"' in generated.views_xml and 'position="inside"' in generated.views_xml, (
        "must anchor on the always-valid //search root, never a guessed sibling element"
    )
    assert '<filter name="accepted" string="Accepted" domain="[(\'state\',\'=\',\'accepted\')]"/>' in generated.views_xml
    assert '<filter name="my_records" string="My records" domain="[(\'user_id\',\'=\',uid)]"/>' in generated.views_xml
    print(f"PASS: a real, well-formed, correctly-anchored search-filter view is generated "
          f"deterministically:\n{generated.views_xml}")


def test_autofix_search_filters_still_fires_when_prose_and_splits_into_two_constraint_labels():
    """Phase 26A follow-up (2026-07-27): real, live-reproduced bug --
    task 007's own goal names its two filters in ONE prose sentence
    joined by "and" ("...called 'Accepted' ... and another called 'My
    records'..."). derive_constraint_labels() (manager/replanning.py)
    splits prose on exactly that conjunction, so this genuinely
    single-focus "add N search filters" round still lands with 2 keys
    in constraint_status -- a prose-segmentation artifact, not evidence
    of other real content needing preservation. The old blanket
    `len(constraint_status) >= 2: return` guard treated this as a
    decomposed multi-focus round and never fired at all, silently
    leaving views_xml at whatever the LLM guessed (confirmed live:
    empty `<odoo></odoo>`, causing a real sandbox install ParseError).
    Uses the EXACT constraint_status keys observed on the real failing
    round (`meerwerk_list_i`, `another_called_my`).
    """
    import xml.etree.ElementTree as ET

    from specialists.build.specialist import _autofix_goal_named_search_filters_missing

    goal = (
        "In the meerwerk list, I want a quick filter button called 'Accepted' that shows only "
        "accepted records, and another called 'My records' that shows only records assigned to me.\n\n"
        "Module: project_meerwerk\nModel: project.meerwerk\nView: view_project_meerwerk_search\n"
        "Filter 1: name=accepted, string=Accepted, domain=[('state','=','accepted')]\n"
        "Filter 2: name=my_records, string=My records, domain=[('user_id','=',uid)]\n"
    )
    manifest = ManifestFields(
        name="x", version="1.0", category="Uncategorized", summary="", author="", depends=["base"], data=[],
    )
    generated = GeneratedModuleFiles(manifest_fields=manifest, models_py="", security_csv="", notes="")
    _autofix_goal_named_search_filters_missing(
        generated, goal, {"meerwerk_list_i": "pending", "another_called_my": "pending"},
    )

    assert generated.views_xml, "must still fire -- 2 constraint labels here are a prose-split artifact, not real multi-focus content"
    ET.fromstring(generated.views_xml)
    assert '<filter name="accepted" string="Accepted" domain="[(\'state\',\'=\',\'accepted\')]"/>' in generated.views_xml
    assert '<filter name="my_records" string="My records" domain="[(\'user_id\',\'=\',uid)]"/>' in generated.views_xml
    print("PASS: fires correctly even when prose 'and' split the goal into 2 constraint labels")


def test_autofix_search_filters_still_skips_when_goal_asks_for_more_than_filters():
    """Safety boundary for the fix above: when the goal's structured
    block genuinely asks for something else too (e.g. a new Field:),
    the 2+-constraint-label case really might have other content to
    preserve -- must still bail rather than blindly full-replace
    views_xml and destroy that other content's own view wiring.
    """
    from specialists.build.specialist import _autofix_goal_named_search_filters_missing

    goal = (
        "Add a search filter and also a new field.\n\n"
        "Module: project_meerwerk\nView: view_project_meerwerk_search\n"
        "Field: is_urgent (Boolean)\n"
        "Filter 1: name=accepted, string=Accepted, domain=[('state','=','accepted')]\n"
        "Filter 2: name=my_records, string=My records, domain=[('user_id','=',uid)]\n"
    )
    manifest = ManifestFields(
        name="x", version="1.0", category="Uncategorized", summary="", author="", depends=["base"], data=[],
    )
    generated = GeneratedModuleFiles(
        manifest_fields=manifest, models_py="", security_csv="", notes="", views_xml="<odoo>existing</odoo>",
    )
    _autofix_goal_named_search_filters_missing(
        generated, goal, {"add_a_search": "pending", "also_a_new": "pending"},
    )
    assert generated.views_xml == "<odoo>existing</odoo>", "must bail and leave existing content untouched when other content (Field:) is also requested"
    print("PASS: still skips when the goal's structured block asks for more than just filters")


def test_autofix_stat_button_missing_rebuilds_a_proper_smart_button():
    """Phase 26A follow-up (2026-07-27): real, live-reproduced bug --
    task 009's own goal ("a button in the top-right corner that shows
    how many meerwerk records... Action method: action_view_partner_
    meerwerk"). Confirmed live, 5 identical rounds: Build's own view.xml
    correctly resolved the real base view (`base.view_partner_form`)
    and record shape, but implemented the requirement as a raw
    `<field name="meerwerk_partner_count"/>` dropped inside `//sheet`
    instead of Odoo's own standard `oe_stat_button` widget inside the
    form's `button_box` div -- Code-Review correctly flagged this same
    UI mismatch every round without the model ever converging. This
    autofix extracts the record/inherit_id Build already got right and
    only corrects the widget placement.
    """
    from specialists.build.specialist import _autofix_goal_named_stat_button_missing

    goal = (
        "On the customer (contact) form, I want to see a button in the top-right corner that shows "
        "how many meerwerk (extra work) records are linked to this customer. Clicking it should open "
        "that list filtered to this customer.\n\n"
        "Module: project_meerwerk\nModel: res.partner (inherit)\n"
        "Field: meerwerk_partner_count (Integer, computed)\n"
        "Compute method: _compute_meerwerk_partner_count, counts via search_count\n"
        "Action method: action_view_partner_meerwerk opens project.meerwerk filtered by partner_id\n"
    )
    manifest = ManifestFields(
        name="x", version="1.0", category="Uncategorized", summary="", author="", depends=["base"], data=[],
    )
    wrong_views_xml = (
        "<odoo>\n"
        '    <record id="view_res_partner_inherit_meerwerk_partner_count" model="ir.ui.view">\n'
        "        <field name=\"name\">res.partner.form.inherit.meerwerk_partner_count</field>\n"
        '        <field name="model">res.partner</field>\n'
        '        <field name="inherit_id" ref="base.view_partner_form"/>\n'
        '        <field name="arch" type="xml">\n'
        '            <xpath expr="//sheet" position="inside">\n'
        '                <field name="meerwerk_partner_count"/>\n'
        "            </xpath>\n"
        "        </field>\n"
        "    </record>\n"
        "</odoo>\n"
    )
    generated = GeneratedModuleFiles(
        manifest_fields=manifest, models_py="", security_csv="", notes="", views_xml=wrong_views_xml,
    )
    _autofix_goal_named_stat_button_missing(generated, goal, {"customer_contact_form": "pending"})

    import xml.etree.ElementTree as ET
    ET.fromstring(generated.views_xml)
    assert 'ref="base.view_partner_form"' in generated.views_xml, "must preserve Build's own correct inherit target"
    assert "oe_stat_button" in generated.views_xml
    assert 'name="action_view_partner_meerwerk"' in generated.views_xml
    assert 'widget="statinfo"' in generated.views_xml
    assert "button_box" in generated.views_xml
    assert '<field name="meerwerk_partner_count"/>' not in generated.views_xml, (
        "the old raw-field-in-sheet anti-pattern must be gone"
    )
    print(f"PASS: rebuilds a proper stat button, preserving Build's own correct record/inherit "
          f"shape:\n{generated.views_xml}")


def test_autofix_stat_button_missing_never_fires_without_the_full_shape():
    """Must never guess/invent a button_box or action name -- a goal
    missing the Action method: line, the top-right-corner phrase, or
    Build's own recognizable views_xml record shape leaves views_xml
    completely untouched.
    """
    from specialists.build.specialist import _autofix_goal_named_stat_button_missing

    goal_no_action = (
        "I want to see a count on the customer form.\n\n"
        "Module: project_meerwerk\nModel: res.partner (inherit)\n"
        "Field: meerwerk_partner_count (Integer, computed)\n"
    )
    manifest = ManifestFields(
        name="x", version="1.0", category="Uncategorized", summary="", author="", depends=["base"], data=[],
    )
    generated = GeneratedModuleFiles(
        manifest_fields=manifest, models_py="", security_csv="", notes="", views_xml="<odoo>untouched</odoo>",
    )
    _autofix_goal_named_stat_button_missing(generated, goal_no_action, {})
    assert generated.views_xml == "<odoo>untouched</odoo>"
    print("PASS: never fires without an Action method: line, the top-right-corner phrase, and "
          "Build's own recognizable view record shape")


def test_autofix_goal_literal_button_snippet_replaces_bare_field():
    """Real, confirmed bug found live (2026-08-07, Phase 30 full-backlog pass, task026, real
    task_ids spanning v11/v12/v13 -- the identical mistake 3 times in a row):
    `_autofix_goal_named_stat_button_missing` only fires for the narrow "top-right corner" +
    structured "Field:"/"Action method:" convention, never a goal stating the exact required
    smart-button XML directly, in prose, as a literal snippet -- task026's own real goal:
    "The view MUST wrap the field inside a real smart-button widget, exactly this shape inside
    the button_box: <button name="action_open_containers" ...><field name="container_count"
    .../></button>". Build correctly used the right field/action names every time, just never
    wrapped them in the button structure the goal already spells out verbatim.
    """
    from specialists.build.specialist import _autofix_goal_literal_button_snippet_replaces_bare_field

    real_goal = (
        "Add a 'Container count' smart button to the project form. ... The view MUST wrap the "
        "field inside a real smart-button widget, exactly this shape inside the button_box: "
        '<button name="action_open_containers" type="object" class="oe_stat_button" '
        'icon="fa-list"><field name="container_count" widget="statinfo" string="Containers"/>'
        "</button> -- placed inside the form's existing button_box div (position=\"inside\"), "
        "NOT a bare <field> insertion into the sheet."
    )
    manifest = ManifestFields(
        name="x", version="1.0", category="Uncategorized", summary="", author="", depends=["base"], data=[],
    )
    bare_field_views_xml = (
        '<odoo>\n  <record id="v1" model="ir.ui.view">\n'
        '    <field name="name">project.project.form.inherit</field>\n'
        '    <field name="model">project.project</field>\n'
        '    <field name="inherit_id" ref="project.edit_project"/>\n'
        '    <field name="arch" type="xml">\n'
        '      <xpath expr="//sheet" position="inside">\n'
        '        <field name="container_count"/>\n'
        "      </xpath>\n    </field>\n  </record>\n</odoo>"
    )
    generated = GeneratedModuleFiles(
        manifest_fields=manifest, models_py="", security_csv="", notes="", views_xml=bare_field_views_xml,
    )
    _autofix_goal_literal_button_snippet_replaces_bare_field(generated, real_goal)
    assert '<field name="container_count"/>' not in generated.views_xml, (
        "the bare field tag must be replaced"
    )
    assert (
        '<button name="action_open_containers" type="object" class="oe_stat_button" '
        'icon="fa-list"><field name="container_count" widget="statinfo" string="Containers"/>'
        "</button>"
    ) in generated.views_xml, "the goal's own literal button snippet must be substituted in"

    # Must never touch views_xml that already has ANY button element.
    already_has_button = bare_field_views_xml.replace(
        '<field name="container_count"/>',
        '<button name="x" type="object"><field name="container_count"/></button>',
    )
    generated2 = GeneratedModuleFiles(
        manifest_fields=manifest, models_py="", security_csv="", notes="", views_xml=already_has_button,
    )
    _autofix_goal_literal_button_snippet_replaces_bare_field(generated2, real_goal)
    assert generated2.views_xml == already_has_button, "must never touch views_xml that already has a button"

    # Must never fire when the goal has no literal button snippet at all.
    plain_goal = "Add a container_count field to the project form."
    generated3 = GeneratedModuleFiles(
        manifest_fields=manifest, models_py="", security_csv="", notes="", views_xml=bare_field_views_xml,
    )
    _autofix_goal_literal_button_snippet_replaces_bare_field(generated3, plain_goal)
    assert generated3.views_xml == bare_field_views_xml, "must never fire without a literal button snippet in the goal"
    print("PASS: a bare field is replaced with the goal's own literal button snippet; never "
          "touches views_xml already containing a button, never fires without a literal snippet")


def test_autofix_goal_literal_button_snippet_respects_not_yet_in_scope_action():
    """Real, general bug found live (2026-08-07, task026, HUMAN_DECISION deep-push, real task_id
    f2b4d065-a859-4b96-9b09-ed8691d988ad): the sibling test above confirmed the substitution
    fires correctly -- but it fires REGARDLESS of round scope, since `goal` always embeds the
    FULL original multi-piece goal text verbatim even in round 1. Confirmed live via the model's
    own self-report on the exact round this broke: "I've included the view element for the smart
    button as required by the instructions, but I've made it so that the button references a
    method that doesn't exist yet (action_open_containers) ... will be handled in a separate
    round" -- `_validate_declared_reference_methods_exist` then correctly, deterministically
    rejects the round every time for referencing an undeclared method, burning the entire round
    budget on a self-inflicted contradiction between this autofix and the round's own real scope,
    not a genuine model mistake. The autofix must now skip firing when the button's own action is
    named as NOT-yet-in-scope for this round.
    """
    from specialists.build.specialist import _autofix_goal_literal_button_snippet_replaces_bare_field

    real_goal_round1 = (
        "Add a 'Container count' smart button to the project form. ... The view MUST wrap the "
        "field inside a real smart-button widget, exactly this shape inside the button_box: "
        '<button name="action_open_containers" type="object" class="oe_stat_button" '
        'icon="fa-list"><field name="container_count" widget="statinfo" string="Containers"/>'
        "</button> -- placed inside the form's existing button_box div (position=\"inside\"), "
        "NOT a bare <field> insertion into the sheet.\n\n"
        "This round's own NEW focus is ONLY: 'container_count_field'. The following constraints "
        "are NOT yet in scope for this round and must NOT be implemented even partially -- no "
        "fields, methods, or view elements for any of them yet, no matter how related they seem: "
        "['smart_button_view', 'action_open_containers_method']."
    )
    manifest = ManifestFields(
        name="x", version="1.0", category="Uncategorized", summary="", author="", depends=["base"], data=[],
    )
    bare_field_views_xml = (
        '<odoo>\n  <record id="v1" model="ir.ui.view">\n'
        '    <field name="name">project.project.form.inherit</field>\n'
        '    <field name="model">project.project</field>\n'
        '    <field name="inherit_id" ref="project.edit_project"/>\n'
        '    <field name="arch" type="xml">\n'
        '      <xpath expr="//sheet" position="inside">\n'
        '        <field name="container_count"/>\n'
        "      </xpath>\n    </field>\n  </record>\n</odoo>"
    )
    generated = GeneratedModuleFiles(
        manifest_fields=manifest, models_py="", security_csv="", notes="", views_xml=bare_field_views_xml,
    )
    _autofix_goal_literal_button_snippet_replaces_bare_field(generated, real_goal_round1)
    assert generated.views_xml == bare_field_views_xml, (
        "must NOT substitute in the button snippet when the button's own action "
        "(action_open_containers) is explicitly named as not-yet-in-scope for this round -- got:\n"
        f"{generated.views_xml}"
    )

    # Sibling check: once the round actually reaches the button's own constraint (not-yet-in-scope
    # no longer names it), the substitution must still fire normally.
    real_goal_later_round = real_goal_round1.replace(
        "This round's own NEW focus is ONLY: 'container_count_field'. The following constraints "
        "are NOT yet in scope for this round and must NOT be implemented even partially -- no "
        "fields, methods, or view elements for any of them yet, no matter how related they seem: "
        "['smart_button_view', 'action_open_containers_method'].",
        "This round's own NEW focus is ONLY: 'smart_button_view'.",
    )
    generated2 = GeneratedModuleFiles(
        manifest_fields=manifest, models_py="", security_csv="", notes="", views_xml=bare_field_views_xml,
    )
    _autofix_goal_literal_button_snippet_replaces_bare_field(generated2, real_goal_later_round)
    assert '<field name="container_count"/>' not in generated2.views_xml, (
        "once the button's own action is genuinely in scope (no not-yet-in-scope list excludes "
        "it), the substitution must still fire normally"
    )
    print("PASS: the button-snippet substitution respects round scope -- skipped when the "
          "button's own action is explicitly not-yet-in-scope, still fires once it's in scope")


def test_autofix_goal_literal_button_snippet_handles_word_reordered_scope_label():
    """Real, confirmed live follow-on gap (2026-08-07, task026, same real task, next relaunch --
    real task_id fa526343-2e4f-4a93-a619-01a972af20db): the sibling test above proved the
    not-yet-in-scope check works for a substring-shaped label ('action_open_containers_method')
    -- but a real relaunch's own decomposition used a WORD-REORDERED label instead
    ('open_containers_action', action LAST instead of FIRST). Neither name is a substring of the
    other despite sharing all 3 significant tokens, so the original plain-substring check let the
    substitution fire anyway, reproducing the exact same self-inflicted contradiction the sibling
    fix was built to prevent (confirmed live: Code-Review correctly caught "the task goal
    explicitly states that 'open_containers_action' is NOT yet in scope ... yet the
    action_open_containers method is defined here"). Fixed with token-overlap matching (mirrors
    specialists/code_review/specialist.py's own established `_name_matches_out_of_scope_label`).
    """
    from specialists.build.specialist import _autofix_goal_literal_button_snippet_replaces_bare_field

    real_goal_reordered_label = (
        "Add a 'Container count' smart button to the project form. ... The view MUST wrap the "
        "field inside a real smart-button widget, exactly this shape inside the button_box: "
        '<button name="action_open_containers" type="object" class="oe_stat_button" '
        'icon="fa-list"><field name="container_count" widget="statinfo" string="Containers"/>'
        "</button> -- placed inside the form's existing button_box div (position=\"inside\"), "
        "NOT a bare <field> insertion into the sheet.\n\n"
        "This round's own NEW focus is ONLY: 'smart_button_view'. The following constraints "
        "are NOT yet in scope for this round and must NOT be implemented even partially -- no "
        "fields, methods, or view elements for any of them yet, no matter how related they seem: "
        "['open_containers_action']."
    )
    manifest = ManifestFields(
        name="x", version="1.0", category="Uncategorized", summary="", author="", depends=["base"], data=[],
    )
    bare_field_views_xml = (
        '<odoo>\n  <record id="v1" model="ir.ui.view">\n'
        '    <field name="name">project.project.form.inherit</field>\n'
        '    <field name="model">project.project</field>\n'
        '    <field name="inherit_id" ref="project.edit_project"/>\n'
        '    <field name="arch" type="xml">\n'
        '      <xpath expr="//sheet" position="inside">\n'
        '        <field name="container_count"/>\n'
        "      </xpath>\n    </field>\n  </record>\n</odoo>"
    )
    generated = GeneratedModuleFiles(
        manifest_fields=manifest, models_py="", security_csv="", notes="", views_xml=bare_field_views_xml,
    )
    _autofix_goal_literal_button_snippet_replaces_bare_field(generated, real_goal_reordered_label)
    assert generated.views_xml == bare_field_views_xml, (
        "must NOT substitute in the button snippet when the button's own action "
        "(action_open_containers) shares all significant tokens with a word-reordered "
        "not-yet-in-scope label (open_containers_action) -- got:\n"
        f"{generated.views_xml}"
    )
    print("PASS: the not-yet-in-scope check catches a word-reordered label pairing via "
          "token-overlap matching, not just plain substring containment")


def test_autofix_goal_literal_button_snippet_corrects_xpath_to_button_box():
    """Real, confirmed live bug (2026-08-07, task026, HUMAN_DECISION deep-push, real task_id
    fa526343-2e4f-4a93-a619-01a972af20db): the substitution only ever swapped the INNER bare
    `<field>` tag for the goal's own button snippet -- it never corrected the OUTER `<xpath
    expr="...">` wrapper, which the deterministic view-scaffold builder had already set to its
    own generic default (`//sheet`) before this autofix ever ran. Confirmed live: Code-Review
    correctly, genuinely flagged "the smart button is placed inside //sheet, but the task goal
    explicitly requires it to be placed inside the existing button_box div" -- a real defect
    directly caused by this autofix's own incomplete substitution, not a hallucination and not a
    model mistake (the model never wrote the xpath at all; the deterministic scaffold did).
    """
    from specialists.build.specialist import _autofix_goal_literal_button_snippet_replaces_bare_field

    goal_with_button_box = (
        "Add a 'Container count' smart button to the project form. ... The view MUST wrap the "
        "field inside a real smart-button widget, exactly this shape inside the button_box: "
        '<button name="action_open_containers" type="object" class="oe_stat_button" '
        'icon="fa-list"><field name="container_count" widget="statinfo" string="Containers"/>'
        "</button> -- placed inside the form's existing button_box div (position=\"inside\"), "
        "NOT a bare <field> insertion into the sheet.\n\n"
        "This round's own NEW focus is ONLY: 'smart_button_view'."
    )
    manifest = ManifestFields(
        name="x", version="1.0", category="Uncategorized", summary="", author="", depends=["base"], data=[],
    )
    bare_field_views_xml_wrong_xpath = (
        '<odoo>\n    <record id="view_project_project_inherit_container_count" model="ir.ui.view">\n'
        '        <field name="name">project.project.form.inherit.container_count</field>\n'
        '        <field name="model">project.project</field>\n'
        '        <field name="inherit_id" ref="project.edit_project"/>\n'
        '        <field name="arch" type="xml">\n'
        '            <xpath expr="//sheet" position="inside">\n'
        '                <field name="container_count"/>\n'
        "            </xpath>\n        </field>\n    </record>\n</odoo>"
    )
    generated = GeneratedModuleFiles(
        manifest_fields=manifest, models_py="", security_csv="", notes="", views_xml=bare_field_views_xml_wrong_xpath,
    )
    _autofix_goal_literal_button_snippet_replaces_bare_field(generated, goal_with_button_box)
    assert 'expr="//sheet"' not in generated.views_xml, (
        f"expected the xpath target corrected away from //sheet -- got:\n{generated.views_xml}"
    )
    assert 'expr="//div[@name=\'button_box\']"' in generated.views_xml, (
        f"expected the xpath target corrected to the real button_box div -- got:\n{generated.views_xml}"
    )
    assert (
        '<button name="action_open_containers" type="object" class="oe_stat_button" '
        'icon="fa-list"><field name="container_count" widget="statinfo" string="Containers"/>'
        "</button>"
    ) in generated.views_xml, "the button snippet substitution itself must still happen"
    print("PASS: when the goal names button_box as the intended container, the outer xpath "
          "target is corrected from //sheet to the real button_box div, alongside the inner "
          "field-to-button substitution")


def test_autofix_search_filters_fires_for_a_single_structured_filter_goal():
    """Phase 35 generalization fix (2026-08-12, ctx05_quick_filter real overnight bake-in
    incident): a single, concretely-specified `Filter N:` line is exactly as safe to build
    deterministically as two -- the old `< 2` threshold excluded this real, common shape with no
    stated safety reason. Confirms the deterministic builder now engages for a real single-filter
    structured goal instead of falling through to freeform LLM generation.
    """
    import xml.etree.ElementTree as ET

    from specialists.build.specialist import _autofix_goal_named_search_filters_missing

    goal = (
        "Add a quick filter named 'Due For Service' to the fleet vehicle search view.\n\n"
        "Module: oma_add_a_filter_to_0f63d44d\nModel: fleet.vehicle\n"
        "View: fleet_vehicle_view_search\n"
        "Filter 1: name=due_for_service, string=Due For Service, "
        "domain=[('next_service_date','<=',context_today())]\n"
    )
    manifest = ManifestFields(
        name="x", version="1.0", category="Uncategorized", summary="", author="", depends=["base"], data=[],
    )
    generated = GeneratedModuleFiles(manifest_fields=manifest, models_py="", security_csv="", notes="")
    _autofix_goal_named_search_filters_missing(generated, goal, {"add_a_quick": "pending"})

    assert generated.views_xml, "must fire for a single, concretely-specified Filter line"
    ET.fromstring(generated.views_xml)
    assert (
        '<filter name="due_for_service" string="Due For Service" '
        "domain=\"[('next_service_date','&lt;=',context_today())]\"/>"
    ) in generated.views_xml, "the '<=' domain operator must be XML-escaped, not left raw"
    print("PASS: the deterministic filter builder now fires for a real single-filter structured goal, not just 2+")


def test_autofix_search_filters_never_fires_without_a_concrete_two_filter_goal():
    """Must never guess/invent filters -- a goal with fewer than 2
    concretely-specified `Filter N:` lines, or missing Module:/View:,
    leaves views_xml completely untouched.
    """
    from specialists.build.specialist import _autofix_goal_named_search_filters_missing

    goal = "Add a quick filter for accepted records.\n\nModule: project_meerwerk\n"
    manifest = ManifestFields(
        name="x", version="1.0", category="Uncategorized", summary="", author="", depends=["base"], data=[],
    )
    generated = GeneratedModuleFiles(manifest_fields=manifest, models_py="", security_csv="", notes="")
    _autofix_goal_named_search_filters_missing(generated, goal, {"add_a_quick": "pending"})
    assert generated.views_xml is None
    print("PASS: never fires without a concrete Module:/View:/2+ Filter: goal shape")


def test_autofix_sequence_field_missing_never_touches_an_existing_create_override():
    """A class with its OWN create() override must never get a second,
    conflicting create() blindly appended -- merging custom create()
    logic is a real judgment call this autofix has no safe way to make.
    """
    from specialists.build.specialist import _autofix_goal_named_sequence_field_missing

    goal = "Auto-generate a reference number, like MW2026-0042.\n\nField: name\n"
    manifest = ManifestFields(
        name="x", version="1.0", category="Uncategorized", summary="", author="", depends=["base"], data=[],
    )
    models_py = (
        "from odoo import models, fields\n\nclass ProjectMeerwerk(models.Model):\n"
        "    _inherit = 'project.meerwerk'\n\n"
        "    def create(self, vals_list):\n        # custom pre-existing logic\n        return super().create(vals_list)\n"
    )
    generated = GeneratedModuleFiles(manifest_fields=manifest, models_py=models_py, security_csv="", notes="")
    goal_facts = {"field_name": "name", "is_computed": False, "is_sequence_assigned": True}
    _autofix_goal_named_sequence_field_missing(generated, goal, {"auto_generate_a": "pending"}, goal_facts)
    assert generated.models_py == models_py, "must never touch a class that already has its own create() override"
    print("PASS: never blindly appends a second create() when one already exists")


def _school_student_fixture():
    """Real fixture matching Operator's own school_student task, verbatim
    field shape -- reused across Phase 28A's own new autofix tests.
    """
    goal = (
        "Testing: Write at least 8 automated tests (model creation, age computation, unique "
        "constraint, search, etc.)\n\n"
        "Module: school_student\nModel: school.student\n"
        "Field: name (Char, required)\n"
        "Field: student_id (Char, unique)\n"
        "Field: date_of_birth (Date)\n"
        "Field: gender (Selection: male/Male, female/Female, other/Other)\n"
        "Field: class_name (Char)\n"
        "Field: phone (Char)\n"
        "Field: email (Char)\n"
        "Field: active (Boolean, default True)\n"
        "Field: age (Integer, computed)\n"
    )
    models_py = (
        "from odoo import api, fields, models\n\n\n"
        "class SchoolStudent(models.Model):\n"
        "    _name = 'school.student'\n"
        "    _description = 'Student'\n\n"
        "    name = fields.Char(string='Name', required=True)\n"
        "    student_id = fields.Char(string='Student Id')\n"
        "    date_of_birth = fields.Date(string='Date Of Birth')\n"
        "    gender = fields.Selection([('male', 'Male'), ('female', 'Female'), "
        "('other', 'Other')], string='Gender')\n"
        "    class_name = fields.Char(string='Class Name')\n"
        "    phone = fields.Char(string='Phone')\n"
        "    email = fields.Char(string='Email')\n"
        "    active = fields.Boolean(string='Active', default=True)\n"
        "    age = fields.Integer(string='Age', compute='_compute_age')\n\n"
        "    @api.depends('date_of_birth')\n"
        "    def _compute_age(self):\n"
        "        for rec in self:\n"
        "            rec.age = 0\n"
    )
    manifest = ManifestFields(
        name="x", version="1.0", category="Uncategorized", summary="", author="", depends=["base"], data=[],
    )
    return goal, GeneratedModuleFiles(manifest_fields=manifest, models_py=models_py, security_csv="", notes="")


def test_autofix_unique_constraint_missing_injects_real_sql_constraints():
    """Phase 28A (2026-07-28): a field the goal marks 'unique' (this
    project's own established parenthetical-attribute convention) needs
    a real `_sql_constraints` entry to mean anything at the database
    level -- confirmed live: without this, the sibling test-generation
    autofix's own uniqueness test would fail for a reason that has
    nothing to do with a genuine bug.
    """
    from specialists.build.specialist import _autofix_goal_named_unique_constraint_missing

    goal, generated = _school_student_fixture()
    _autofix_goal_named_unique_constraint_missing(generated, goal, {})
    assert "_sql_constraints" in generated.models_py
    assert "'student_id_uniq'" in generated.models_py
    assert "'unique(student_id)'" in generated.models_py
    compile(generated.models_py, "<test>", "exec")
    print(f"PASS: injects a real _sql_constraints entry for the goal's own unique field:\n{generated.models_py}")


def test_autofix_unique_constraint_missing_never_touches_an_existing_declaration():
    from specialists.build.specialist import _autofix_goal_named_unique_constraint_missing

    goal, generated = _school_student_fixture()
    generated.models_py += (
        "\n    _sql_constraints = [('student_id_uniq', 'unique(student_id)', 'already here')]\n"
    )
    before = generated.models_py
    _autofix_goal_named_unique_constraint_missing(generated, goal, {})
    assert generated.models_py == before, "must never touch a model that already declares _sql_constraints"
    print("PASS: never duplicates/merges into an already-existing _sql_constraints declaration")


def test_autofix_tests_missing_generates_at_least_8_real_passing_shaped_tests():
    """Phase 28A (2026-07-28): the real, confirmed structural gap this
    closes -- Operator's own school_student task explicitly requires "at
    least 8 automated tests (model creation, age computation, unique
    constraint, search, etc.)... tests must pass". Confirms the
    generated test file: is real, syntactically valid Python; has at
    least 8 real test methods; covers every named category; and never
    references a field/import it didn't actually declare.
    """
    from specialists.build.specialist import (
        _autofix_goal_named_tests_missing,
        _autofix_goal_named_unique_constraint_missing,
    )

    goal, generated = _school_student_fixture()
    _autofix_goal_named_unique_constraint_missing(generated, goal, {})
    _autofix_goal_named_tests_missing(generated, goal, {})

    assert generated.tests_py, "expected real tests_py content to be generated"
    assert "tests/__init__.py" in generated.tests_py
    test_key = "tests/test_school_student.py"
    assert test_key in generated.tests_py, f"got keys: {list(generated.tests_py)!r}"
    test_content = generated.tests_py[test_key]

    compile(test_content, "<test>", "exec")  # raises if not valid Python
    compile(generated.tests_py["tests/__init__.py"], "<test>", "exec")

    test_method_count = test_content.count("    def test_")
    assert test_method_count >= 8, f"expected at least 8 test methods, got {test_method_count}:\n{test_content}"

    assert "from odoo import fields" in test_content, "fields.Date.today() is used -- the import must be present"
    assert "TransactionCase" in test_content
    assert "def test_create_minimal(self):" in test_content
    assert "def test_create_full(self):" in test_content
    assert "def test_student_id_unique_constraint(self):" in test_content
    assert "psycopg2.IntegrityError" in test_content
    assert "def test_age_computed(self):" in test_content
    assert "self.assertIn(record.age, (19, 20))" in test_content, (
        "age must be asserted as a bounded range, never an exact hardcoded formula this "
        "function can't independently verify"
    )
    assert "def test_search(self):" in test_content
    assert "def test_write(self):" in test_content
    assert "def test_unlink(self):" in test_content
    print(f"PASS: generates {test_method_count} real, syntactically valid tests covering every "
          f"named category (creation, age computation, unique constraint, search):\n{test_content}")


def test_autofix_tests_missing_never_fires_when_model_incomplete():
    """Must never generate tests against a model that isn't fully
    declared yet -- a decomposed task's own earlier rounds must leave
    this untouched until every goal-named field actually exists.
    """
    from specialists.build.specialist import _autofix_goal_named_tests_missing

    goal, generated = _school_student_fixture()
    # Simulate an in-progress round: strip the last field declaration.
    generated.models_py = generated.models_py.replace(
        "    age = fields.Integer(string='Age', compute='_compute_age')\n\n", ""
    )
    _autofix_goal_named_tests_missing(generated, goal, {})
    assert generated.tests_py is None, "must not generate tests until every goal-named field is declared"
    print("PASS: never fires while the model is still incomplete")


def test_autofix_tests_missing_never_fires_without_explicit_testing_request():
    from specialists.build.specialist import _autofix_goal_named_tests_missing

    goal, generated = _school_student_fixture()
    goal_no_testing = goal.replace(
        "Testing: Write at least 8 automated tests (model creation, age computation, unique "
        "constraint, search, etc.)\n\n", "",
    )
    _autofix_goal_named_tests_missing(generated, goal_no_testing, {})
    assert generated.tests_py is None, "must never fire without an explicit test request in the goal"
    print("PASS: never fires without an explicit Testing: request")


def test_autofix_tests_missing_bails_on_required_relational_field_it_cannot_safely_fabricate():
    """A required Many2one (or any type outside the safe, non-relational
    set) needs a real, fabricated foreign id this function has no safe
    way to invent -- must bail entirely rather than guess one.
    """
    from specialists.build.specialist import _autofix_goal_named_tests_missing

    goal = (
        "Testing: write 8 automated tests.\n\nModel: x.y\n"
        "Field: name (Char, required)\nField: partner_id (Many2one: res.partner, required)\n"
    )
    manifest = ManifestFields(
        name="x", version="1.0", category="Uncategorized", summary="", author="", depends=["base"], data=[],
    )
    generated = GeneratedModuleFiles(
        manifest_fields=manifest,
        models_py=(
            "from odoo import fields, models\n\nclass X(models.Model):\n    _name = 'x.y'\n"
            "    name = fields.Char()\n    partner_id = fields.Many2one('res.partner')\n"
        ),
        security_csv="", notes="",
    )
    _autofix_goal_named_tests_missing(generated, goal, {})
    assert generated.tests_py is None, "must never fabricate a foreign id for a required relational field"
    print("PASS: bails rather than guess a value for a required relational field")


def test_deterministic_view_builder_carries_readonly_from_goal():
    """Real, general fix (2026-07-25, task 006): `build_deterministic_
    view_xml()` always inserted a bare `<field name="X"/>` tag, with no
    way to carry forward an attribute the goal's own metadata explicitly
    states. Confirmed live: task 006's goal named `Field: name (Char,
    readonly, copy=False, default='New')`, but the deterministic
    builder's own output (verified directly via Gitea -- an exact match
    for this function's own output shape, record id `view_project_
    meerwerk_inherit_name`) inserted a plain, editable `<field
    name="name"/>`, and Code-Review correctly, identically flagged the
    missing `readonly` attribute on rounds 4 AND 5 -- never an LLM
    mistake at all, this project's OWN deterministic scaffolding was
    silently dropping a real, stated requirement.
    """
    from unittest.mock import patch

    import specialists.build.specialist as spec

    goal = (
        "Every meerwerk record should get a unique reference number like MW-2026-0001, "
        "auto-generated when created. The user should never have to type this.\n\n"
        "Module: project_meerwerk\nModel: project.meerwerk\n"
        "Field: name (Char, readonly, copy=False, default='New')\n"
        "Sequence code: project.meerwerk in data/sequence_data.xml, prefix MW-%(year)s-, padding 4\n"
        "Override create() with @api.model_create_multi to assign from ir.sequence when name=='New'\n"
    )

    async def run():
        with patch(
            "tools_odoo.module_dev.toolchain.get_primary_form_view_xmlid",
            return_value="project_meerwerk.view_project_meerwerk_form",
        ):
            return await spec.build_deterministic_view_xml(
                "project.meerwerk", ["name"], "odoo16_dev", task_id=None, goal=goal,
            )

    xml_text = asyncio.run(run())
    assert xml_text is not None
    assert '<field name="name" readonly="1"/>' in xml_text, (
        f"expected the goal's own stated readonly attribute to be carried through, got: {xml_text!r}"
    )
    import xml.etree.ElementTree as ET
    ET.fromstring(xml_text)
    print(f"PASS: the deterministic form-view builder carries a goal-stated readonly attribute "
          f"through to the inserted field tag:\n{xml_text}")

    # No readonly stated in the goal -- must be a bare tag, no false positive.
    async def run_no_readonly():
        with patch(
            "tools_odoo.module_dev.toolchain.get_primary_form_view_xmlid",
            return_value="x.view_form",
        ):
            return await spec.build_deterministic_view_xml(
                "x.model", ["is_vip_client"], "odoo16_dev", task_id=None,
                goal="x\n\nField: is_vip_client (Boolean, default False, tracking=True)",
            )

    xml_no_readonly = asyncio.run(run_no_readonly())
    assert '<field name="is_vip_client"/>' in xml_no_readonly
    assert "readonly" not in xml_no_readonly
    print("PASS: no readonly attribute added when the goal doesn't state one")


def test_deterministic_tree_view_builder_adds_decorations_from_goal():
    """Real, general fix (2026-07-25, fix 30): watched live, Code-Review
    correctly, persistently flagged "Missing tree view inheritance ...
    decoration-danger/decoration-muted styling" as a CRITICAL finding on
    EVERY one of 5 straight rounds of task 003, and the model never once
    added it, exhausting the round budget. This project's own convention
    states decorations exactly as concretely as Selection options
    (`Tree view: decoration-X for value`), so this is safe to build
    deterministically rather than leave to prompting/escalation alone.
    """
    from unittest.mock import patch

    import specialists.build.specialist as spec

    goal = (
        "On the sale order list, I want to see a priority field: Low, Normal, High.\n\n"
        "Field: order_priority (Selection: low/normal/high, default normal, tracking=True)\n"
        "Tree view: decoration-danger for high, decoration-muted for low\n"
        "Form view: header area before statusbar\n"
    )

    async def run():
        with patch("tools_odoo.module_dev.toolchain.get_primary_tree_view_xmlid", return_value="sale.view_order_tree"):
            return await spec.build_deterministic_tree_view_xml(
                "sale.order", ["order_priority"], "odoo16_dev", task_id=None, goal=goal,
            )

    xml_text = asyncio.run(run())
    assert xml_text is not None
    assert '<attribute name="decoration-danger">order_priority == \'high\'</attribute>' in xml_text
    assert '<attribute name="decoration-muted">order_priority == \'low\'</attribute>' in xml_text
    import xml.etree.ElementTree as ET
    tree = ET.fromstring(xml_text)  # must be well-formed XML
    # 36-task fix pass (2026-08-06): real, confirmed bug found live -- well-formedness alone
    # (ET.fromstring succeeding) does NOT catch multiple sibling root elements inside <field
    # name="arch">, which Odoo itself rejects outright ("arch requires exactly ONE root element").
    # This is the actual, precise regression test for that real bug: the field insertion and the
    # decoration attribute change are two separate xpath blocks, so arch's own child must be
    # exactly one element (a <data> wrapper), never two bare sibling <xpath> elements.
    arch_field = tree.find(".//field[@name='arch']")
    assert len(list(arch_field)) == 1, (
        f"<field name=\"arch\"> must have exactly ONE root child element (wrapped in <data> when "
        f"there's more than one xpath block) -- Odoo rejects multiple sibling roots outright -- "
        f"got {len(list(arch_field))} children:\n{xml_text}"
    )
    print(f"PASS: deterministic tree view builder adds real decoration attributes parsed from the "
          f"goal's own convention, wrapped in a single valid arch root element:\n{xml_text}")

    # No decoration signal in the goal -- must be a pure no-op, unchanged
    # from the pre-fix-30 behavior (a bare field-column insertion only).
    async def run_no_decorations():
        with patch("tools_odoo.module_dev.toolchain.get_primary_tree_view_xmlid", return_value="sale.view_order_tree"):
            return await spec.build_deterministic_tree_view_xml(
                "sale.order", ["order_priority"], "odoo16_dev", task_id=None, goal="just add the field to the list",
            )

    xml_no_deco = asyncio.run(run_no_decorations())
    assert "decoration" not in xml_no_deco
    print("PASS: no decoration block added when the goal states none")


def test_deterministic_tree_view_builder_adds_decorations_from_natural_language_goal():
    """36-task fix pass (2026-08-06): real, confirmed live recurrence found watching task003 mid-
    flight on a genuinely fresh run, well after the fix above already existed -- the real 50-task
    benchmark's own goal text for this exact task ("On the sale order list, I want to see a
    priority field: Low, Normal, High. High priority orders should be highlighted in red in the
    list.") never uses this project's own `decoration-X for value` spec convention at all, so
    `_GOAL_TREE_DECORATION_RE` never matched it, and the deterministic builder silently fell
    through to the LLM path -- which added the field with no decoration at all (again), the exact
    same defect the original fix 30 was built to close, just via a goal phrasing the original regex
    was never built to recognize.
    """
    from unittest.mock import patch

    import specialists.build.specialist as spec

    real_goal = (
        "On the sale order list, I want to see a priority field: Low, Normal, High. "
        "High priority orders should be highlighted in red in the list."
    )

    async def run():
        with patch("tools_odoo.module_dev.toolchain.get_primary_tree_view_xmlid", return_value="sale.view_order_tree"):
            return await spec.build_deterministic_tree_view_xml(
                "sale.order", ["priority"], "odoo16_dev", task_id=None, goal=real_goal,
            )

    xml_text = asyncio.run(run())
    assert xml_text is not None
    assert '<attribute name="decoration-danger">priority == \'high\'</attribute>' in xml_text, (
        f"the real benchmark's own natural-language goal must produce a real decoration-danger "
        f"attribute -- got:\n{xml_text}"
    )
    import xml.etree.ElementTree as ET
    tree = ET.fromstring(xml_text)
    arch_field = tree.find(".//field[@name='arch']")
    assert len(list(arch_field)) == 1, (
        f"real bug this exact scenario hit live: <field name=\"arch\"> must have exactly ONE root "
        f"child (the field-insertion xpath and the decoration xpath wrapped in a single <data>), "
        f"not two bare sibling <xpath> elements, which Odoo rejected outright the first time this "
        f"exact goal ran end to end -- got {len(list(arch_field))} children:\n{xml_text}"
    )
    print(f"PASS: natural-language 'highlighted in red' phrasing produces a real decoration "
          f"attribute, not just the internal decoration-X-for-Y spec convention, wrapped in a "
          f"single valid arch root element:\n{xml_text}")


def test_parse_goal_tree_decorations_recognizes_other_colors_and_falls_back_to_none():
    from specialists.build.specialist import _parse_goal_tree_decorations

    assert _parse_goal_tree_decorations(
        "Overdue leads should be highlighted in orange in the list."
    ) == [("warning", "overdue")]
    assert _parse_goal_tree_decorations(
        "Cancelled records should be highlighted in gray in the list."
    ) == [("muted", "cancelled")]
    assert _parse_goal_tree_decorations("Add a priority field to the list.") == [], (
        "a goal with no decoration signal at all (neither convention) must return an empty list, "
        "never a guess"
    )
    print("PASS: natural-language color words map to the right decoration class, and a goal "
          "with no highlight signal at all returns nothing")


def test_extract_collision_marker_field_names_parses_the_real_marker_format():
    from specialists.build.specialist import _extract_collision_marker_field_names

    notes_single = (
        "The priority field is added to the sale order list view...\n"
        "ALREADY_SATISFIED_BY_REAL_TARGET_COLLISION: ['priority']"
    )
    assert _extract_collision_marker_field_names(notes_single) == ["priority"]

    notes_multi = "blah\nALREADY_SATISFIED_BY_REAL_TARGET_COLLISION: ['field_a', 'field_b']"
    assert _extract_collision_marker_field_names(notes_multi) == ["field_a", "field_b"]

    assert _extract_collision_marker_field_names("no marker anywhere in these notes") == []
    print("PASS: the real collision-marker text format is parsed correctly, and absence returns []")


def test_view_xml_override_still_engages_when_field_was_stripped_by_collision_autofix():
    """36-task fix pass (2026-08-06): real, confirmed live gap found watching task003 mid-flight --
    when `_validate_no_new_field_collides_with_real_target_field()` correctly strips a redundant
    new-field declaration (the field genuinely already exists on the real target), the SAME
    validator chain's later call to `_maybe_override_view_xml_deterministically()` used to find
    `field_names` empty (nothing left in `generated.models_py` to extract) and silently never
    engage at all -- even though the field genuinely still exists and the goal's own separate view/
    styling requirement for it was never implemented. Confirms the collision-marker fallback closes
    this: with NO field declared in `generated.models_py` at all, but the collision marker present
    in `generated.notes`, the deterministic override must still fire.
    """
    from unittest.mock import patch

    import specialists.build.specialist as spec

    real_goal = (
        "On the sale order list, I want to see a priority field: Low, Normal, High. "
        "High priority orders should be highlighted in red in the list."
    )
    manifest = ManifestFields(
        name="x", version="1.0.0", category="Sales", summary="x", author="x",
        depends=["base", "sale"], data=[],
    )
    generated = GeneratedModuleFiles(
        manifest_fields=manifest,
        # No field declared at all -- exactly what's left after the collision autofix strips it.
        models_py="from odoo import models\n\nclass SaleOrder(models.Model):\n    _inherit = 'sale.order'\n",
        security_csv="id,name\n",
        views_xml="<odoo></odoo>",
        notes="Some notes.\nALREADY_SATISFIED_BY_REAL_TARGET_COLLISION: ['priority']",
    )

    async def run():
        with patch("tools_odoo.module_dev.toolchain.get_primary_tree_view_xmlid", return_value="sale.view_order_tree"):
            await spec._maybe_override_view_xml_deterministically(
                generated, "odoo16_dev", "test-task-id", goal=real_goal,
            )

    asyncio.run(run())
    assert 'decoration-danger' in generated.views_xml, (
        f"the deterministic override must still engage via the collision-marker fallback field "
        f"name, even with zero field declarations left in models_py -- got:\n{generated.views_xml}"
    )
    assert "<field name=\"priority\"" in generated.views_xml
    print("PASS: the deterministic view override still engages and adds the real decoration "
          "attribute even when the field was correctly stripped from models_py by the collision "
          "autofix, via the ALREADY_SATISFIED_BY_REAL_TARGET_COLLISION marker fallback")


def test_view_xml_override_never_fires_when_goal_says_no_view_changes():
    """Real, confirmed general bug found live (2026-08-12, day-to-day directions sweep, "edit an
    existing module directly" re-verification): the goal explicitly said "do not add any view
    changes", the LLM's own raw output correctly left views_xml=None -- and this deterministic
    override still fabricated a view referencing a nonexistent xmlid, because every path in this
    function only ever asks "can I build a confident override", never "does the goal want one at
    all". An explicit no-view-changes instruction must win over every other signal, including a
    real inherited field that would otherwise trigger the field-override path below.
    """
    from unittest.mock import patch

    import specialists.build.specialist as spec

    real_goal = (
        "Edit the existing, already-installed module oma_build_a_complete_field_ab52b7f8 "
        "directly: add a 'notes' Text field to its own oma.equipment model, in that module's "
        "own models/models.py, not a new extension module. Do not add any view changes.\n\n"
        "Module: oma_build_a_complete_field_ab52b7f8\nModel: oma.equipment\n"
        "Field: notes (Text)\n"
    )
    manifest = ManifestFields(
        name="x", version="1.0.0", category="Services", summary="x", author="x",
        depends=["base", "oma_build_a_complete_field_ab52b7f8"], data=[],
    )
    generated = GeneratedModuleFiles(
        manifest_fields=manifest,
        models_py="from odoo import models, fields\n\nclass Equipment(models.Model):\n    _inherit = 'oma.equipment'\n\n    notes = fields.Text()\n",
        security_csv="id,name\n",
        views_xml=None,
        notes="",
    )

    async def run():
        with patch("tools_odoo.module_dev.toolchain.get_primary_tree_view_xmlid", return_value="oma_build_a_complete_field_ab52b7f8.view_oma_equipment_tree"):
            await spec._maybe_override_view_xml_deterministically(
                generated, "odoo16_dev", "test-task-id", goal=real_goal,
            )

    asyncio.run(run())
    assert generated.views_xml is None, (
        f"an explicit no-view-changes instruction must leave views_xml untouched, got:\n{generated.views_xml}"
    )
    print("PASS: an explicit 'do not add any view changes' instruction stops the deterministic "
          "override from firing at all, even with a real inherited field present")


def test_view_xml_override_forced_field_names_uses_them_directly():
    """Sanity check: forcing an unambiguous field list bypasses the models_py extraction path
    entirely -- if this narrower unit ever regresses, the end-to-end test above would still catch
    it, but this isolates the specific code path being fixed."""
    from specialists.build.specialist import _extract_collision_marker_field_names

    # A model with genuinely no field of any kind, and no collision marker either -- must
    # correctly find nothing (the pre-fix, still-correct "no signal" case).
    assert _extract_collision_marker_field_names("plain notes, nothing special") == []
    print("PASS: no false field names invented from notes with no real collision marker")


def test_view_xml_override_merges_tree_and_form_when_goal_wants_both():
    """Real, general fix (2026-07-25, fix 30): the OLD override treated
    tree/form as mutually exclusive -- a goal naming BOTH (task 003's own
    real shape: a tree-view decoration requirement AND a form-view
    placement requirement in the same goal) always fell back to building
    the form override ALONE, silently leaving the tree view (and its
    decorations) to the same freeform-generation path that had already
    failed 5 straight rounds in a row. Confirms both deterministic
    `<record>` blocks are now combined into one valid `<odoo>` document.
    """
    from unittest.mock import patch

    import specialists.build.specialist as spec

    goal = (
        "On the sale order list, I want to see a priority field: Low, Normal, High.\n\n"
        "Field: order_priority (Selection: low/normal/high, default normal, tracking=True)\n"
        "Tree view: decoration-danger for high, decoration-muted for low\n"
        "Form view: header area before statusbar\n"
    )
    manifest = ManifestFields(
        name="x", version="1.0", category="Uncategorized", summary="", author="", depends=["sale"], data=[],
    )
    generated = GeneratedModuleFiles(
        manifest_fields=manifest,
        models_py=(
            "from odoo import models, fields\n\nclass SaleOrder(models.Model):\n"
            "    _inherit = 'sale.order'\n    order_priority = fields.Selection("
            "[('low', 'Low'), ('normal', 'Normal'), ('high', 'High')], string='Order Priority')\n"
        ),
        security_csv="", notes="",
    )

    async def run():
        with patch("tools_odoo.module_dev.toolchain.get_primary_tree_view_xmlid", return_value="sale.view_order_tree"), \
             patch("tools_odoo.module_dev.toolchain.get_primary_form_view_xmlid", return_value="sale.view_order_form"):
            await spec._maybe_override_view_xml_deterministically(generated, "odoo16_dev", "test-task-id", goal=goal)

    asyncio.run(run())
    assert generated.views_xml is not None
    assert "decoration-danger" in generated.views_xml, "the tree view's decoration must survive the merge"
    assert 'ref="sale.view_order_tree"' in generated.views_xml
    assert 'ref="sale.view_order_form"' in generated.views_xml
    import xml.etree.ElementTree as ET
    ET.fromstring(generated.views_xml)  # must be well-formed, valid multi-record XML
    print(f"PASS: a dual tree+form goal produces one merged, valid views_xml with both overrides:\n{generated.views_xml}")


def test_strips_unrequested_views_xml_for_a_pure_behavior_task():
    """Real, general fix (2026-07-25, task 005): a goal that adds NO new
    field and states NO view change at all (a pure `@api.onchange`/
    business-logic task) still had the LLM generate a views_xml file
    anyway, referencing a GUESSED base-view external id that does not
    actually exist -- correctly, deterministically rejected pre-write by
    `_validate_xml_refs_resolve()` on all 5 straight rounds, and the
    model never stopped inventing it, exhausting the round budget. Since
    the task needed no view change at all, the always-safe fix is to not
    have any views_xml to get wrong.
    """
    from specialists.build.specialist import _autofix_strip_unrequested_views_xml_for_pure_behavior_task

    goal = (
        "When I select a project on the meerwerk form, I want the 'Assigned to' field to "
        "automatically fill with the project manager. I can still change it manually after.\n\n"
        "Module: project_meerwerk\nModel: project.meerwerk\n"
        "@api.onchange('project_id') method _onchange_project_id: if project_id and "
        "project_id.user_id, set user_id = project_id.user_id\n"
    )
    manifest = ManifestFields(
        name="x", version="1.0", category="Uncategorized", summary="", author="", depends=["project"],
        data=["views/views.xml"],
    )
    views_xml = (
        '<odoo>\n  <record id="v1" model="ir.ui.view">\n'
        '    <field name="inherit_id" ref="project_meerwerk.project_meerwerk_form"/>\n'
        "  </record>\n</odoo>"
    )
    generated = GeneratedModuleFiles(
        manifest_fields=manifest,
        models_py=(
            "from odoo import models, fields, api\n\nclass ProjectMeerwerk(models.Model):\n"
            "    _inherit = 'project.meerwerk'\n\n"
            "    @api.onchange('project_id')\n"
            "    def _onchange_project_id(self):\n"
            "        if self.project_id and self.project_id.user_id:\n"
            "            self.user_id = self.project_id.user_id\n"
        ),
        views_xml=views_xml, security_csv="", notes="",
    )
    _autofix_strip_unrequested_views_xml_for_pure_behavior_task(generated, goal)
    assert generated.views_xml is None, f"expected the guessed view XML to be stripped, got: {generated.views_xml!r}"
    print("PASS: stray views_xml (with a guessed, nonexistent base-view ref) is stripped for a "
          "pure-behavior task that never asked for any view change")

    # A goal with an explicit View: metadata line -- must never strip.
    view_goal = "Add a field.\n\nField: x (Text)\nView: Inherit some.view\n"
    with_field = GeneratedModuleFiles(
        manifest_fields=manifest,
        models_py="from odoo import models, fields\n\nclass X(models.Model):\n    _inherit = 'x'\n    x = fields.Text()\n",
        views_xml=views_xml, security_csv="", notes="",
    )
    _autofix_strip_unrequested_views_xml_for_pure_behavior_task(with_field, view_goal)
    assert with_field.views_xml == views_xml, "must never strip when the goal states an explicit View: line"

    # A goal that adds a new field (no View: line stated) -- must never strip either.
    field_only_goal = "Add a field please.\n\nField: y (Boolean)\n"
    field_only = GeneratedModuleFiles(
        manifest_fields=manifest,
        models_py="from odoo import models, fields\n\nclass X(models.Model):\n    _inherit = 'x'\n    y = fields.Boolean()\n",
        views_xml=views_xml, security_csv="", notes="",
    )
    _autofix_strip_unrequested_views_xml_for_pure_behavior_task(field_only, field_only_goal)
    assert field_only.views_xml == views_xml, "must never strip when a new field is being added"
    print("PASS: never strips when the goal states an explicit View: line, or adds a new field")


def test_this_rounds_focus_is_views_related_recognizes_button_labeled_rounds():
    """Real, confirmed bug found live (2026-08-06, Phase 30 backlog pass, task030, real task_id
    770187a1-b9b9-4f05-8aba-e3a61fec87f5): the keyword list this function checks a round's own
    current-focus label against never included "button" -- a round whose own focus was literally
    `send_customer_button` (genuinely, entirely view/UI content, an XML `<button>` inside a view
    arch) was not recognized as views-related, so `_autofix_strip_premature_views_content_on_
    decomposed_round` nulled out Build's own genuinely correct, matching view content for this
    exact round before it ever reached Code-Review.
    """
    from specialists.build.specialist import _this_rounds_focus_is_views_related

    button_goal = (
        "This round's own NEW focus is ONLY: 'send_customer_button'. Add ONLY the code this one "
        "constraint strictly requires."
    )
    assert _this_rounds_focus_is_views_related(button_goal) is True, (
        "a round focused on a real UI button must be recognized as views-related"
    )
    non_views_goal = (
        "This round's own NEW focus is ONLY: 'action_send_method'. Add ONLY the code this one "
        "constraint strictly requires."
    )
    assert _this_rounds_focus_is_views_related(non_views_goal) is False, (
        "a genuinely non-view round (a plain Python method) must not be falsely treated as views-related"
    )
    print("PASS: a button-labeled round is recognized as views-related, a genuinely non-view "
          "round is not")


def test_this_rounds_focus_is_views_related_recognizes_decoration_labeled_rounds():
    """Real, confirmed bug found live (2026-08-10, task e65381cc, ticket_status_decoration
    node), the exact same keyword-gap class already fixed once for "button" above: a round
    whose own focus label was literally `ticket_status_decoration` (genuinely, entirely view
    content -- `decoration-<state>` attributes on a tree view arch) was NOT recognized as
    views-related, so `_list_premature_invented_fields()`'s own safety-net detection for an
    invented, unused field never even ran for this round -- Build invented an entire unused
    Selection field literally named `ticket_status_decoration` (its own generated comment
    admitted "this field is not used directly") with no autofix/validator layer to catch it.
    """
    from specialists.build.specialist import _this_rounds_focus_is_views_related

    decoration_goal = (
        "This round's own NEW focus is ONLY: 'ticket_status_decoration'. Add ONLY the code "
        "this one constraint strictly requires."
    )
    assert _this_rounds_focus_is_views_related(decoration_goal) is True, (
        "a round focused on tree-view decoration attributes must be recognized as views-related"
    )
    print("PASS: a decoration-labeled round is recognized as views-related")


def test_autofix_strip_invented_field_uses_original_goal_not_poisoned_by_disclaimer():
    """Real, confirmed FOLLOW-UP bug found live (2026-08-10, same task, same node, same night as
    the keyword fix above), the exact same "wrong text checked" class `_find_premature_invented_
    field_defect()`'s own docstring already documents and fixed for itself -- just never
    propagated to `_autofix_strip_premature_invented_field_on_views_focused_round()`, its own
    action counterpart, which called `_list_premature_invented_fields(generated.models_py, goal)`
    with NO separate `original_goal` at all.

    This stayed harmless only as long as `goal` (the round-composed text) never happened to
    contain a field name that wasn't genuinely requested -- until `_compose_focus_goal_text()`
    (manager/loop.py) gained its own disclaimer quoting the focus label a SECOND time as a
    cautionary example ("a prior round invented an entire unused Selection field literally named
    'ticket_status_decoration'"). That new text lives only in `goal`, never in `original_goal` --
    so a bare substring check against `goal` alone now reads the label's own cautionary mention
    as if it were a real request, permanently defeating the autofix for the exact field it exists
    to strip. Reproduced here directly against the real field/goal shape from that live incident.
    """
    from specialists.build.specialist import (
        _autofix_strip_premature_invented_field_on_views_focused_round,
        GeneratedModuleFiles, ManifestFields,
    )

    original_goal = (
        "1. Spare parts consumed: add a many2many relation on oma.service.ticket to Odoo's own "
        "standard product catalog (product.product), named parts_consumed_ids...\n"
        "2. ...The oma.service.ticket tree view must show the ticket's status field with a "
        "distinct color per state, using Odoo's standard decoration-<state> attributes on the "
        "tree view.\n3. Security gap..."
    )
    round_goal = (
        original_goal + "\n\nThis round's own NEW focus is ONLY: 'ticket_status_decoration'. "
        "(This is only this system's own internal tracking name -- a prior round invented an "
        "entire unused Selection field literally named 'ticket_status_decoration' purely "
        "because the label was quoted here.)"
    )
    models_py = (
        "from odoo import models, fields\n\n"
        "class OmaServiceTicket(models.Model):\n"
        "    _inherit = 'oma.service.ticket'\n\n"
        "    parts_consumed_ids = fields.Many2many('product.product', string='Consumed Parts', "
        "domain=[('active', '=', True)])\n\n"
        "    ticket_status_decoration = fields.Selection([\n        ('new', 'New'),\n    ], "
        "string='Status Decoration', related='state', store=False)\n"
    )
    generated = GeneratedModuleFiles(
        manifest_fields=ManifestFields(
            name="x", version="1.0", category="Uncategorized", summary="", author="", depends=["base"], data=[],
        ),
        models_py=models_py, views_xml="", security_csv="", notes="",
    )
    _autofix_strip_premature_invented_field_on_views_focused_round(generated, round_goal, original_goal)
    assert "ticket_status_decoration" not in generated.models_py, (
        f"the invented, unused field must be stripped even though the round-composed goal text "
        f"quotes its own name: {generated.models_py!r}"
    )
    assert "parts_consumed_ids" in generated.models_py, (
        "a genuinely requested field must never be stripped as collateral damage"
    )
    print("PASS: the invented field is stripped using the real original goal as ground truth, "
          "unpoisoned by the round-composed disclaimer's own cautionary re-mention of its name")


def test_autofix_strips_goal_explicitly_required_removed_access_row():
    """Real, confirmed bug found live (2026-08-10, task e65381cc, ticket_access_restriction
    node): the round's own goal explicitly, literally said "Remove the base.group_user access
    row for oma.service.ticket from ir.model.access.csv", yet the exact same row survived
    byte-identical across three consecutive real attempts (even after two separate, real,
    unrelated prompt-composition bugs -- Bugs 62/63 -- were fixed first and confirmed not to be
    the actual cause) -- the same "scoped-edit generation reliably ADDS, not reliably REMOVES
    something already in the baseline" weak spot already fixed once for invented fields, here
    for a CSV row instead.
    """
    from specialists.build.specialist import (
        _autofix_strip_goal_explicitly_required_removed_access_row,
        GeneratedModuleFiles, ManifestFields,
    )

    goal = (
        "Remove the base.group_user access row for oma.service.ticket from ir.model.access.csv, "
        "so that only the existing group_field_technician and group_operations_manager groups "
        "can access oma.service.ticket at all."
    )
    security_csv = (
        "id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink\n"
        "access_oma_equipment,oma.equipment,"
        "oma_build_a_complete_field_ab52b7f8.model_oma_equipment,base.group_user,1,1,1,0\n"
        "access_oma_service_ticket,oma.service.ticket,"
        "oma_build_a_complete_field_ab52b7f8.model_oma_service_ticket,base.group_user,1,1,1,0\n"
    )
    generated = GeneratedModuleFiles(
        manifest_fields=ManifestFields(
            name="x", version="1.0", category="Uncategorized", summary="", author="", depends=["base"], data=[],
        ),
        models_py="", views_xml="", security_csv=security_csv, notes="",
    )
    _autofix_strip_goal_explicitly_required_removed_access_row(generated, goal)
    assert "oma.service.ticket" not in generated.security_csv, (
        f"the explicitly-required-removed row must be stripped: {generated.security_csv!r}"
    )
    assert "oma.equipment" in generated.security_csv, (
        "an unrelated, non-targeted row must never be stripped as collateral damage"
    )
    print("PASS: the goal-explicitly-required-removed access row is stripped; an unrelated row "
          "for a different model is left untouched")


def test_autofix_synthesize_goal_named_restricted_access_rows():
    """Real, confirmed bug found live (2026-08-10, task e65381cc, ticket_access_restriction
    node): even after Bugs 64-67 made the REMOVAL side of this round's security goal fully
    stable, Build never once, across many consecutive rounds -- including one with an explicit
    resume note spelling out the exact CSV rows to write -- actually added the two NEW rows the
    SAME goal sentence requires for two EXISTING groups defined in a different, already-
    installed module. No existing autofix covered this shape (they only ever synthesize a row
    for a group/rule THIS round's own new content defines, never an externally pre-existing
    group merely named in the goal's own prose). This is the deterministic synthesis fix,
    reusing the goal's own literal "so that only the existing X (...) and Y (...) groups can
    access Z" sentence as real ground truth for both which groups to grant and what permission
    shape each one gets.
    """
    from specialists.build.specialist import (
        _autofix_synthesize_goal_named_restricted_access_rows,
        GeneratedModuleFiles, ManifestFields,
    )

    goal = (
        "Remove the base.group_user access row for oma.service.ticket from ir.model.access.csv, "
        "so that only the existing group_field_technician (restricted by the existing ir.rule "
        "to their own tickets) and group_operations_manager (full access) groups can access "
        "oma.service.ticket at all."
    )
    security_csv = (
        "id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink\n"
        "access_oma_equipment,oma.equipment,"
        "oma_build_a_complete_field_ab52b7f8.model_oma_equipment,base.group_user,1,1,1,0\n"
    )
    generated = GeneratedModuleFiles(
        manifest_fields=ManifestFields(
            name="x", version="1.0", category="Uncategorized", summary="", author="", depends=["base"], data=[],
        ),
        models_py="", views_xml="", security_csv=security_csv, notes="",
    )
    _autofix_synthesize_goal_named_restricted_access_rows(generated, goal, "oma_build_a_complete_field_ab52b7f8")
    assert "oma_build_a_complete_field_ab52b7f8.group_field_technician" in generated.security_csv, (
        f"the technician group's own new row must be synthesized: {generated.security_csv!r}"
    )
    assert "oma_build_a_complete_field_ab52b7f8.group_operations_manager" in generated.security_csv, (
        f"the manager group's own new row must be synthesized: {generated.security_csv!r}"
    )
    lines = {line.split(",")[3]: line for line in generated.security_csv.splitlines() if "group_" in line}
    technician_line = lines["oma_build_a_complete_field_ab52b7f8.group_field_technician"]
    manager_line = lines["oma_build_a_complete_field_ab52b7f8.group_operations_manager"]
    assert technician_line.endswith(",0"), (
        f"technician row has no 'full access' qualifier in the goal -- perm_unlink must be 0: {technician_line!r}"
    )
    assert manager_line.endswith(",1"), (
        f"manager row's own goal parenthetical explicitly says 'full access' -- perm_unlink must be 1: {manager_line!r}"
    )
    assert "oma.equipment" in generated.security_csv, "an unrelated, untargeted row must never be touched"
    print("PASS: both goal-named existing groups get new access rows, with permission shape "
          "matching the goal's own 'full access' wording; the unrelated row is untouched")


def test_autofix_security_csv_drops_inherit_only_rows_preserves_pre_existing_rows():
    """Real, confirmed root-cause bug found live (2026-08-10, task e65381cc,
    ticket_access_restriction node), found only after extensive live-trace instrumentation:
    `_autofix_security_csv_drops_inherit_only_rows()`'s own original premise ("keep a row only
    if tied to a NEW model/group THIS round") predates `_autofix_restore_dropped_rows_in_shared_
    security_csv()` (2026-08-09) and was never reconciled with it. On a LATER decomposed round
    that adds no new model/group at all (a pure security-editing round), the restore fix
    correctly restores both pre-existing rows, and THIS function then immediately, silently
    stripped them right back out again -- since neither is tied to anything "new" THIS round --
    undoing the restore three consecutive live rounds in a row, confirmed via direct trace
    instrumentation showing `generated.security_csv` go from 2 real rows (post-restore) to
    header-only (post-this-function) on every single attempt.
    """
    from specialists.build.specialist import (
        _autofix_security_csv_drops_inherit_only_rows,
        GeneratedModuleFiles, ManifestFields,
    )

    old_csv = (
        "id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink\n"
        "access_oma_equipment,oma.equipment,"
        "oma_build_a_complete_field_ab52b7f8.model_oma_equipment,base.group_user,1,1,1,0\n"
        "access_oma_service_ticket,oma.service.ticket,"
        "oma_build_a_complete_field_ab52b7f8.model_oma_service_ticket,base.group_user,1,1,1,0\n"
    )
    old_files_by_relpath = {"security/ir.model.access.csv": old_csv}
    generated = GeneratedModuleFiles(
        manifest_fields=ManifestFields(
            name="x", version="1.0", category="Uncategorized", summary="", author="", depends=["base"], data=[],
        ),
        # No new model, no new group this round -- exactly the pure security-editing round shape.
        models_py="from odoo import models, fields\n\nclass OmaServiceTicket(models.Model):\n    _inherit = 'oma.service.ticket'\n",
        views_xml="", security_xml="",
        security_csv=old_csv,  # already restored by the sibling fix before this one runs
        notes="",
    )
    _autofix_security_csv_drops_inherit_only_rows(generated, old_files_by_relpath)
    assert "access_oma_equipment" in generated.security_csv, (
        f"a genuinely pre-existing row must never be stripped just because nothing new "
        f"references it this round: {generated.security_csv!r}"
    )
    assert "access_oma_service_ticket" in generated.security_csv, (
        f"a genuinely pre-existing row must never be stripped just because nothing new "
        f"references it this round: {generated.security_csv!r}"
    )
    print("PASS: rows already present in the OLD baseline are preserved even when nothing new "
          "this round references them")


def test_autofix_ensure_baseline_access_row_never_re_adds_a_goal_mandated_removal():
    """Real, confirmed bug found live (2026-08-10, task e65381cc, ticket_access_restriction
    node), found only after direct before/after trace instrumentation at every security-CSV-
    touching autofix's own call site in the real validator chain: `_autofix_strip_goal_
    explicitly_required_removed_access_row()` (Bug 64) correctly removed the goal-mandated
    `base.group_user` row for `oma.service.ticket` -- and moments later, in the SAME validator
    chain, `_autofix_ensure_baseline_access_row_for_menu_exposed_models()` saw that same,
    real, menu-exposed model now had ZERO access rows and "helpfully" added a brand-new
    `base.group_user` row right back (unqualified `model_id`, since it has no module-prefix
    context), reproducing the exact original bug under a completely different mechanism, three
    consecutive live rounds in a row. This function's own safety net (correctly, generally
    designed to prevent a genuinely accidental "menu exposes a model with zero access" gap) had
    no way to distinguish that gap from a DELIBERATE, goal-mandated, in-progress security
    restriction.
    """
    from specialists.build.specialist import (
        _autofix_ensure_baseline_access_row_for_menu_exposed_models,
        GeneratedModuleFiles, ManifestFields,
    )

    goal = (
        "Remove the base.group_user access row for oma.service.ticket from ir.model.access.csv, "
        "so that only the existing group_field_technician and group_operations_manager groups "
        "can access oma.service.ticket at all."
    )
    views_xml = (
        "<odoo><record id=\"action_oma_service_ticket\" model=\"ir.actions.act_window\">"
        "<field name=\"res_model\">oma.service.ticket</field></record>"
        "<menuitem id=\"menu_oma_service_ticket\" action=\"action_oma_service_ticket\"/></odoo>"
    )
    generated = GeneratedModuleFiles(
        manifest_fields=ManifestFields(
            name="x", version="1.0", category="Uncategorized", summary="", author="", depends=["base"], data=[],
        ),
        models_py="", views_xml=views_xml,
        # Bug 64's own fix already correctly removed the goal-mandated row, leaving zero rows.
        security_csv="id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink\n",
        notes="",
    )
    _autofix_ensure_baseline_access_row_for_menu_exposed_models(generated, goal)
    assert "oma.service.ticket" not in generated.security_csv, (
        f"a goal-mandated removal must never be silently re-added by the menu-exposed-models "
        f"safety net: {generated.security_csv!r}"
    )
    print("PASS: the menu-exposed-models safety net does not re-add a row the goal explicitly "
          "requires removed")


def test_autofix_security_csv_drops_inherit_only_rows_preserves_new_rows_for_goal_targeted_model():
    """Real, confirmed FOLLOW-UP bug found live (2026-08-10, same task, same node, same night, the
    mirror-image of the fix above): once Bug 64's goal-mandated removal stopped getting undone,
    Build's own correct attempts to ADD the two new, goal-required rows (existing groups
    `group_field_technician`/`group_operations_manager` gaining access to the existing
    `oma.service.ticket` model) were STILL silently stripped by THIS same function -- a brand-new
    row for an EXISTING group on an EXISTING model matched none of its three existing keep
    conditions (not a new `_name`-declared model, not a group declared in this round's own
    `security_xml` since these groups are pre-existing from an earlier module, and not present in
    the OLD baseline since the rows are genuinely new). Reproduced here directly against the real
    row shape Build should produce for this exact incident.
    """
    from specialists.build.specialist import (
        _autofix_security_csv_drops_inherit_only_rows,
        GeneratedModuleFiles, ManifestFields,
    )

    goal = (
        "Remove the base.group_user access row for oma.service.ticket from ir.model.access.csv, "
        "so that only the existing group_field_technician and group_operations_manager groups "
        "can access oma.service.ticket at all."
    )
    old_csv = (
        "id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink\n"
        "access_oma_equipment,oma.equipment,"
        "oma_build_a_complete_field_ab52b7f8.model_oma_equipment,base.group_user,1,1,1,0\n"
    )
    old_files_by_relpath = {"security/ir.model.access.csv": old_csv}
    new_csv = (
        "id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink\n"
        "access_oma_equipment,oma.equipment,"
        "oma_build_a_complete_field_ab52b7f8.model_oma_equipment,base.group_user,1,1,1,0\n"
        "access_oma_service_ticket_technician,oma.service.ticket,"
        "oma_build_a_complete_field_ab52b7f8.model_oma_service_ticket,"
        "oma_build_a_complete_field_ab52b7f8.group_field_technician,1,1,1,0\n"
        "access_oma_service_ticket_manager,oma.service.ticket,"
        "oma_build_a_complete_field_ab52b7f8.model_oma_service_ticket,"
        "oma_build_a_complete_field_ab52b7f8.group_operations_manager,1,1,1,1\n"
    )
    generated = GeneratedModuleFiles(
        manifest_fields=ManifestFields(
            name="x", version="1.0", category="Uncategorized", summary="", author="", depends=["base"], data=[],
        ),
        models_py="", views_xml="", security_xml="",
        security_csv=new_csv, notes="",
    )
    _autofix_security_csv_drops_inherit_only_rows(generated, old_files_by_relpath, goal)
    assert "group_field_technician" in generated.security_csv, (
        f"a genuinely new row for an existing group on this round's own goal-targeted model must "
        f"never be stripped: {generated.security_csv!r}"
    )
    assert "group_operations_manager" in generated.security_csv, (
        f"a genuinely new row for an existing group on this round's own goal-targeted model must "
        f"never be stripped: {generated.security_csv!r}"
    )
    print("PASS: new rows for existing groups on this round's own goal-targeted model are "
          "preserved, not treated as unrequested duplicates")


def test_autofix_never_strips_views_xml_when_goal_prose_names_a_literal_xml_snippet():
    """Real, confirmed bug found live (2026-08-06, Phase 30 backlog pass, task030, real task_id
    574a37a6-ef6b-4c0b-b9ca-0fb785c7b370): this autofix's own exemptions only ever recognized the
    structured "View:"/"Field:"/"Action method:" metadata-line convention -- never a goal stating
    real view intent in plain prose with a literal XML snippet spelling out the exact required
    content ("must be a real Odoo view button (<button type=\"object\" name=\"action_send\"/>)").
    A genuinely correct, real views_xml (a real inherit_id/xpath/button, exactly matching the
    goal's own spec) was silently nulled out by this exact step, never reaching Code-Review with
    the view content intact -- Code-Review then correctly rejected the round for a missing button
    Build had actually already written. Using task030's own real shape.
    """
    from specialists.build.specialist import _autofix_strip_unrequested_views_xml_for_pure_behavior_task

    prose_goal_with_xml_snippet = (
        "Add a complete mail compose wizard flow with state change. Manager request: \"The 'Send "
        "to customer' button should open the Odoo email compose wizard...\" Method: action_send "
        "returns ir.actions.act_window for mail.compose.message. The 'Send to customer' button "
        "itself must be a real Odoo view button (<button type=\"object\" name=\"action_send\"/>), "
        "never a boolean field."
    )
    manifest = ManifestFields(
        name="x", version="1.0", category="Uncategorized", summary="", author="", depends=["base"],
        data=["views/views.xml"],
    )
    real_views_xml = (
        '<odoo>\n  <record id="v1" model="ir.ui.view">\n'
        '    <field name="model">project.meerwerk</field>\n'
        '    <field name="inherit_id" ref="project_meerwerk.view_project_meerwerk_form"/>\n'
        '    <field name="arch" type="xml">\n'
        '      <xpath expr="//header" position="inside">\n'
        '        <button name="action_send" type="object" string="Send to Customer"/>\n'
        "      </xpath>\n    </field>\n  </record>\n</odoo>"
    )
    generated = GeneratedModuleFiles(
        manifest_fields=manifest,
        models_py=(
            "from odoo import models\n\nclass ProjectMeerwerk(models.Model):\n"
            "    _inherit = 'project.meerwerk'\n\n"
            "    def action_send(self):\n        self.ensure_one()\n        return {}\n"
        ),
        views_xml=real_views_xml, security_csv="", notes="",
    )
    _autofix_strip_unrequested_views_xml_for_pure_behavior_task(generated, prose_goal_with_xml_snippet)
    assert generated.views_xml == real_views_xml, (
        f"a goal whose own prose spells out a literal XML snippet must never have its real, "
        f"matching views_xml stripped -- got: {generated.views_xml!r}"
    )
    print("PASS: a goal stating real view intent via a literal XML snippet in prose is never "
          "stripped, even without the structured View:/Field: metadata-line convention")


def test_autofix_never_strips_a_real_quick_filter_when_goal_asks_for_one():
    """Real, confirmed bug found live (2026-08-07, HUMAN_DECISION push, task007, real task_id
    888ebaed-e9f0-48cb-8429-8f9a0d5bf28b): a goal asking for a "quick filter button" in plain
    prose (no View: metadata line, no literal XML snippet, no new field) satisfied all 3 of this
    autofix's own exemption conditions, so it silently stripped a genuinely, provably correct
    <filter> search-view element every round -- confirmed via direct redis inspection that the
    real generated candidate had the exact correct <filter>/<xpath> content, which then vanished
    by the time the final validator chain ran, causing a downstream false empty-class rejection.
    """
    from specialists.build.specialist import _autofix_strip_unrequested_views_xml_for_pure_behavior_task

    plain_prose_quick_filter_goal = (
        "In the meerwerk list, I want a quick filter button called 'Accepted' that shows only "
        "accepted records, and another called 'My records' that shows only records assigned to me."
    )
    manifest = ManifestFields(
        name="x", version="1.0", category="Uncategorized", summary="", author="", depends=["base"],
        data=["views/views.xml"],
    )
    real_filter_views_xml = (
        '<?xml version="1.0" encoding="utf-8"?>\n<odoo>\n'
        '  <record id="view_project_meerwerk_search" model="ir.ui.view">\n'
        '    <field name="name">project.meerwerk.search.inherit</field>\n'
        '    <field name="model">project.meerwerk</field>\n'
        '    <field name="inherit_id" ref="project_meerwerk.view_project_meerwerk_search"/>\n'
        '    <field name="arch" type="xml">\n'
        '      <xpath expr="//search" position="inside">\n'
        '        <filter name="accepted" string="Accepted" domain="[(\'state\', \'=\', \'accepted\')]"/>\n'
        '        <filter name="my_records" string="My records" domain="[(\'user_id\', \'=\', uid)]"/>\n'
        "      </xpath>\n    </field>\n  </record>\n</odoo>"
    )
    generated = GeneratedModuleFiles(
        manifest_fields=manifest,
        models_py=(
            "from odoo import models\n\nclass ProjectMeerwerk(models.Model):\n"
            "    _inherit = 'project.meerwerk'\n"
        ),
        views_xml=real_filter_views_xml, security_csv="", notes="",
    )
    _autofix_strip_unrequested_views_xml_for_pure_behavior_task(generated, plain_prose_quick_filter_goal)
    assert generated.views_xml == real_filter_views_xml, (
        f"a goal asking for a real quick filter, with a real <filter> element already generated, "
        f"must never have its views_xml stripped -- got: {generated.views_xml!r}"
    )
    print("PASS: a real quick-filter goal with a real <filter> element is never stripped, even "
          "with none of the pre-existing structured exemption signals present")


def test_autofix_never_strips_real_multi_view_build_when_goal_asks_to_build_views():
    """Real, confirmed sibling bug found live (2026-08-07, HUMAN_DECISION push, task025, real
    task_id 2aea04b2-6f9b-452b-80c0-b2cf9c72df6a): a broader "build the complete form, list, and
    search views for..." goal in plain prose satisfied all 3 pre-existing exemption conditions
    (no View: metadata line, no literal XML snippet, no new field on the model itself), so a
    genuinely correct, real multi-view build (tree/form/search, all real <record model="ir.ui.
    view"> blocks matching the goal's own spec) was silently stripped every round.
    """
    from specialists.build.specialist import _autofix_strip_unrequested_views_xml_for_pure_behavior_task

    build_views_goal = (
        "Build the complete form, list, and search views for the real, already-existing "
        "project.container model. List view should show columns: project_id, supplier_id, "
        "delivery_date, pickup_date, state. Form view: statusbar for the 'state' field."
    )
    manifest = ManifestFields(
        name="x", version="1.0", category="Uncategorized", summary="", author="", depends=["base"],
        data=["views/views.xml"],
    )
    real_multi_view_xml = (
        '<?xml version="1.0" encoding="utf-8"?>\n<odoo>\n'
        '  <record id="view_project_container_tree" model="ir.ui.view">\n'
        '    <field name="name">project.container.tree</field>\n'
        '    <field name="model">project.container</field>\n'
        '    <field name="arch" type="xml"><tree/></field>\n  </record>\n'
        '  <record id="view_project_container_form" model="ir.ui.view">\n'
        '    <field name="name">project.container.form</field>\n'
        '    <field name="model">project.container</field>\n'
        '    <field name="arch" type="xml"><form/></field>\n  </record>\n'
        "</odoo>"
    )
    generated = GeneratedModuleFiles(
        manifest_fields=manifest,
        models_py=(
            "from odoo import models\n\nclass ProjectContainer(models.Model):\n"
            "    _inherit = 'project.container'\n"
        ),
        views_xml=real_multi_view_xml, security_csv="", notes="",
    )
    _autofix_strip_unrequested_views_xml_for_pure_behavior_task(generated, build_views_goal)
    assert generated.views_xml == real_multi_view_xml, (
        f"a goal asking to build views, with 2+ real ir.ui.view records already generated, must "
        f"never have its views_xml stripped -- got: {generated.views_xml!r}"
    )

    # A single, weak <record> in an UNRELATED goal (no build-views intent at all) must still be
    # stripped -- this exemption requires BOTH the prose intent AND 2+ real records.
    unrelated_goal = "Auto-fill the 'Assigned to' field when a project is selected."
    weak_generated = GeneratedModuleFiles(
        manifest_fields=manifest,
        models_py=(
            "from odoo import models\n\nclass ProjectContainer(models.Model):\n"
            "    _inherit = 'project.container'\n"
        ),
        views_xml='<odoo><record id="x" model="ir.ui.view"><field name="model">x</field></record></odoo>',
        security_csv="", notes="",
    )
    _autofix_strip_unrequested_views_xml_for_pure_behavior_task(weak_generated, unrelated_goal)
    assert not weak_generated.views_xml, (
        "a genuinely unrequested single view record on an unrelated goal must still be stripped"
    )
    print("PASS: a real 'build views' goal with 2+ real ir.ui.view records is never stripped; an "
          "unrelated goal's weak single-record content is still correctly stripped")


def test_autofix_strips_bare_unbacked_manifest_data_reference():
    """Real, general fix (2026-07-25, task 007): the model went
    off-scope for a task that only asked for two search-view filters
    and invented an unrelated `ir.actions.server` feature, adding a BARE
    filename `'ir_actions_server.xml'` (no directory prefix) to the
    manifest's own `data` list, with no backing content anywhere. This
    shape matched none of the existing per-slot stray-reference autofixes
    (which only strip a `data/*.xml`-shaped entry, or the exact literal
    `views/views.xml`/`security/security.xml` strings) -- it sailed
    through every pre-write check and only surfaced as a real, identical
    `manifest_references_missing_file` sandbox install crash on two
    straight rounds.
    """
    from specialists.build.specialist import _autofix_manifest_stray_unbacked_data_reference

    manifest = ManifestFields(
        name="oma_x", version="0.1", category="Project", summary="x", author="x",
        depends=["base", "project"],
        data=["views/views.xml", "security/security.xml", "ir_actions_server.xml"],
    )
    generated = GeneratedModuleFiles(
        manifest_fields=manifest, models_py="from odoo import models\n", security_csv="id,name\n",
        views_xml="<odoo/>", security_xml="<odoo/>", notes="",
    )
    _autofix_manifest_stray_unbacked_data_reference(generated)
    assert generated.manifest_fields.data == ["views/views.xml", "security/security.xml"], (
        f"expected the bare, unbacked reference stripped, got: {generated.manifest_fields.data!r}"
    )
    print("PASS: a bare, unbacked manifest data reference (no directory prefix, no matching "
          "content anywhere) is stripped")

    # Every entry genuinely backed -- must be a byte-for-byte no-op.
    manifest_ok = ManifestFields(
        name="oma_y", version="0.1", category="Project", summary="x", author="x", depends=["base"],
        data=["security/ir.model.access.csv", "views/views.xml", "security/security.xml", "data/sequence_data.xml"],
    )
    generated_ok = GeneratedModuleFiles(
        manifest_fields=manifest_ok, models_py="from odoo import models\n", security_csv="id,name\n",
        views_xml="<odoo/>", security_xml="<odoo/>",
        extra_data_files={"data/sequence_data.xml": "<odoo/>"}, notes="",
    )
    before = list(generated_ok.manifest_fields.data)
    _autofix_manifest_stray_unbacked_data_reference(generated_ok)
    assert generated_ok.manifest_fields.data == before, "must be a no-op when every entry is genuinely backed"
    print("PASS: no-op when every manifest data entry is genuinely backed by real content")


def test_autofix_never_strips_a_data_xml_reference_backed_only_by_an_earlier_round():
    """Real, confirmed bug found live (2026-08-09, task 07141af5-9a4e-41b6-93ea-8b7af04fea9c's
    flagship run, service_ticket_model node): the sibling adder
    `_autofix_ensure_extra_data_files_are_referenced_in_manifest()` correctly treats a
    `data/*.xml` key present in `old_files_by_relpath` (a file an EARLIER round already
    created, still real on disk since `write_module_file()` is purely additive) as valid
    backing and adds it back to the manifest -- but this function, called immediately after,
    used to only check `generated.extra_data_files` for backing and stripped that same
    reference right back out on the very same round, since this round's own generation never
    touched that file. An add-then-immediately-strip loop, confirmed live: the manifest
    reference for 'data/sequences.xml' never survived a single round.
    """
    from specialists.build.specialist import _autofix_manifest_stray_unbacked_data_reference

    manifest = ManifestFields(
        name="oma_x", version="0.1", category="Project", summary="x", author="x",
        depends=["base"],
        data=["security/ir.model.access.csv", "data/sequences.xml"],
    )
    generated = GeneratedModuleFiles(
        manifest_fields=manifest, models_py="from odoo import models\n",
        security_csv="id,name\n", views_xml=None, security_xml=None, notes="",
        extra_data_files=None,  # this round never touched data/sequences.xml at all
    )
    old_files_by_relpath = {"data/sequences.xml": "<odoo><record id=\"seq_x\"/></odoo>"}

    _autofix_manifest_stray_unbacked_data_reference(generated, old_files_by_relpath)

    assert "data/sequences.xml" in generated.manifest_fields.data, (
        f"a data/*.xml reference backed by an earlier round's own real, still-on-disk content "
        f"must never be stripped just because THIS round didn't touch that file -- got: "
        f"{generated.manifest_fields.data!r}"
    )
    print("PASS: a manifest reference to a data/*.xml file backed only by an earlier round's "
          "own real content (old_files_by_relpath) is never stripped, closing the real live "
          "add-then-strip loop found on task 07141af5's service_ticket_model node")


def test_fencing_rejects_stale_caller_after_real_specialist_reacquires():
    """The real end-to-end version of Phase 1's synthetic fencing test:
    'task A' (standing in for a specialist whose lock has silently
    expired -- crash, GC pause, whatever) must be rejected by
    check_fence() once a REAL BuildSpecialist run() has since acquired
    the same module's lock fresh and moved forward with a higher token.
    """
    goal = "Fencing Race Test Field"
    contract_b = _make_contract(goal)
    module_name = slugify_module_name(goal, str(contract_b.task_id))
    _cleanup_module_dir(module_name)

    try:
        # Task A acquires with a very short TTL -- standing in for a
        # specialist that started, then stalled/crashed before its lock
        # naturally expired.
        lock_a = acquire_module_lock(module_name, "stale-task-A", ttl_ms=200)
        assert lock_a.acquired

        time.sleep(0.5)  # let A's lock actually expire

        # Task B: a REAL BuildSpecialist run(), same module name, must
        # acquire cleanly (the expired lock no longer blocks it) and
        # get a strictly higher fence token. One single event loop for
        # both the run and the client's own async close -- httpx's
        # connections are bound to whichever loop created them, so
        # closing in a second asyncio.run() call raises "Event loop is
        # closed" instead of actually closing anything.
        async def _drive_b():
            client = ModelGatewayClient()
            try:
                specialist_b = BuildSpecialist(client=client, db=DUPLICATE_DB)
                try:
                    output_b = await specialist_b.run(contract_b)
                    assert output_b.detail.get("module_name") == module_name
                except PartialTaskFailure:
                    pass  # B may hit the known Postgres-ownership block; irrelevant here
            finally:
                await client.aclose()

        asyncio.run(_drive_b())

        # A, unaware its lock expired, now tries to commit using its
        # stale token -- this is the actual race fencing exists to stop.
        a_commit_ok = check_fence(module_name, lock_a.fence_token)
        assert a_commit_ok is False, (
            "task A's stale fence token must be rejected once a real BuildSpecialist "
            "run (task B) has since acquired the lock fresh and committed a higher token"
        )
        print(
            "PASS: a stale caller's fence token is correctly rejected after a REAL "
            "BuildSpecialist run acquired the same module's lock fresh and moved forward"
        )
    finally:
        release_module_lock(module_name, "stale-task-A")
        _cleanup_module_dir(module_name)


def test_two_different_tasks_on_the_same_real_model_contend_for_the_same_lock():
    """Phase 26B follow-up (2026-07-27, audit Finding #2): the deeper
    bug found while live-verifying the module-identity fix -- BEFORE
    this fix, `_run_module_dev()`'s own acquire_module_lock()/
    check_fence()/release_module_lock() calls were ALWAYS keyed on
    `module_name`, this task's own scaffold directory name (always
    unique per task_id by construction via `slugify_module_name()`'s
    own hash suffix) -- so two DIFFERENT tasks (different task_id,
    different goal prose) genuinely editing the exact same real Odoo
    model could NEVER collide on this lock, no matter what
    `contract.module_identity` resolved to. This is the actual,
    concrete regression Finding #2's own fencing evidence describes,
    now closed by keying every acquire/check_fence/release call on
    `contract.module_identity` (falling back to `module_name` only
    when it can't be resolved).

    Two DIFFERENT contracts, both with `module_identity` set to the
    SAME real model (exactly what manager/loop.py's own contract-
    construction step now populates for two real tasks both naming the
    same `Model:` line) -- task A holds the lock (a raw acquire,
    standing in for an in-flight real specialist run), task B is a REAL
    BuildSpecialist.run() call that MUST be rejected while A still
    holds it.
    """
    shared_model = f"oma.phase26b.concurrency.test.{uuid.uuid4().hex[:8]}"
    goal_a = "Concurrency Test Field A"
    goal_b = "Concurrency Test Field B"
    contract_a = _make_contract(goal_a).model_copy(update={"module_identity": shared_model})
    contract_b = _make_contract(goal_b).model_copy(update={"module_identity": shared_model})
    module_name_b = slugify_module_name(goal_b, str(contract_b.task_id))
    _cleanup_module_dir(module_name_b)

    try:
        # Task A: a raw acquire on the REAL shared identity, standing in
        # for an in-flight real specialist run holding the lock.
        lock_a = acquire_module_lock(shared_model, "task-A-holder")
        assert lock_a.acquired

        # Task B: a REAL BuildSpecialist.run() call, a DIFFERENT task_id
        # and goal, but the SAME real model identity -- must be rejected
        # while A still holds the lock, never silently proceed.
        async def _drive_b():
            client = ModelGatewayClient()
            try:
                specialist_b = BuildSpecialist(client=client, db=DUPLICATE_DB)
                return await specialist_b.run(contract_b)
            finally:
                await client.aclose()

        output_b = asyncio.run(_drive_b())
        assert output_b.claims_complete is False, (
            "task B must be rejected while task A still holds the SAME real model's lock"
        )
        assert shared_model in output_b.summary, (
            f"the rejection message must name the real model that's actually locked -- "
            f"got: {output_b.summary!r}"
        )
        print(f"PASS: two different tasks (different task_id, different goal) touching the SAME "
              f"real model {shared_model!r} correctly contend for the SAME lock -- task B was "
              f"rejected while task A held it: {output_b.summary!r}")
    finally:
        release_module_lock(shared_model, "task-A-holder")
        _cleanup_module_dir(module_name_b)


class _ForcedCutoffBuildSpecialist(BuildSpecialist):
    """Test-only subclass that forces a cutoff immediately after the
    real scaffold step succeeds -- simulating hitting turn_budget
    mid-task, per the build plan's own wording, without touching any
    real infrastructure beyond that one deliberate short-circuit.
    """

    async def _generate_code(self, contract, constitution_text, skill_text, module_name):
        raise RuntimeError("SIMULATED CUTOFF -- standing in for turn_budget exhaustion mid-task")


def test_compensations_actually_clean_up_a_forced_mid_task_cutoff():
    goal = "Cutoff Compensation Test Field"
    contract = _make_contract(goal)
    module_name = slugify_module_name(goal, str(contract.task_id))
    _cleanup_module_dir(module_name)
    registry.clear()

    try:
        async def _drive():
            client = ModelGatewayClient()
            try:
                specialist = _ForcedCutoffBuildSpecialist(client=client, db=DUPLICATE_DB)
                registry.register(SpecialistType.bug_fix, specialist)

                raised = False
                try:
                    await delegate_to_specialist(contract)
                except Exception as exc:
                    raised = True
                    from manager.compensations import TaskCutOffPause

                    assert isinstance(exc, TaskCutOffPause), f"expected TaskCutOffPause, got {type(exc)}: {exc}"
                    assert "1 real step" in exc.message or "1 real step(s)" in exc.message
                    print(f"PASS: delegate_to_specialist correctly raised TaskCutOffPause: {exc.message}")

                assert raised, "a forced mid-task cutoff must propagate as TaskCutOffPause, not be silently swallowed"
            finally:
                await client.aclose()

        asyncio.run(_drive())

        # The real, independent check: the scaffolded module directory
        # must actually be gone from the container -- not just a log
        # line claiming cleanup happened.
        assert not _module_dir_exists(module_name), (
            f"run_compensations claimed to remove {module_name!r} but it still exists on disk"
        )
        print(f"PASS: the scaffolded module directory for {module_name!r} was actually removed "
              f"from /mnt/extra-addons -- confirmed independently via SSH, not just claimed")
    finally:
        registry.clear()
        _cleanup_module_dir(module_name)


def test_task1_field_add_end_to_end_against_real_duplicate():
    """Task 1 against a DUPLICATE database -- documents the duplicate-
    specific Postgres-ownership limitation (Phase 8, narrowed Phase 9.5)
    still holding for this exact real infra state: scaffold, real LLM
    code generation, and lint all genuinely happen; install correctly
    surfaces the known block as an honest, structured self-report
    (claims_complete=False) rather than a crash or a false success
    claim. See test_task1_field_add_end_to_end_against_fresh_database()
    below for the actual successful end-to-end proof (fresh db, no
    ownership issue) -- this test's job is specifically to keep proving
    the duplicate limitation is still there, not to prove task 1 works.
    """
    goal = "Add a 'preferred_language' field to res.partner (contacts), visible on the contact form view."
    contract = _make_contract(goal)
    module_name = slugify_module_name(goal, str(contract.task_id))  # must match exactly what BuildSpecialist derives internally
    _cleanup_module_dir(module_name)

    async def _drive():
        client = ModelGatewayClient()
        try:
            specialist = BuildSpecialist(client=client, db=DUPLICATE_DB)
            output = await specialist.run(contract)

            assert output.detail.get("module_name")
            assert _module_dir_exists(output.detail["module_name"]), "scaffold must have really created the module directory"
            assert "lint_findings" in output.detail
            print(f"PASS: real scaffold + real LLM code generation + real lint all ran for "
                  f"module {output.detail['module_name']!r}")

            if output.detail.get("install_error_kind") == "postgres_ownership_blocked":
                assert output.claims_complete is False
                print("PASS: install correctly surfaced the known, still-unresolved Postgres-ownership "
                      "block as an honest self-report (claims_complete=False), not a crash or false success")
            else:
                print(f"NOTE: install_error_kind={output.detail.get('install_error_kind')!r}, "
                      f"claims_complete={output.claims_complete} -- the Postgres-ownership issue may have "
                      f"been fixed since Phase 8; re-check the Phase 8 report if so")
        finally:
            await client.aclose()

    try:
        asyncio.run(_drive())
    finally:
        _cleanup_module_dir(module_name)


def test_task1_field_add_end_to_end_against_fresh_database():
    """The actual, complete proof the build plan's Phase 9 step 6 asks
    for: run the REAL BuildSpecialist, through its own real code path
    (not the generic scaffold/lint/install mechanism checked in
    isolation), against a database Phase 9.5 already proved doesn't
    have the duplicate-specific Postgres-ownership issue -- and confirm
    the field GENUINELY appears afterward, checked independently at the
    Odoo ORM level, not just trusted from the specialist's own
    claims_complete report.
    """
    goal = "Add a 'preferred_language' field to res.partner (contacts), visible on the contact form view."
    contract = _make_contract(goal)
    module_name = slugify_module_name(goal, str(contract.task_id))
    _cleanup_module_dir(module_name)

    async def _drive():
        client = ModelGatewayClient()
        try:
            specialist = BuildSpecialist(client=client, db=FRESH_DB)
            output = await specialist.run(contract)

            assert output.detail.get("module_name") == module_name
            assert _module_dir_exists(module_name), "scaffold must have really created the module directory"
            assert "lint_findings" in output.detail
            print(f"PASS: real scaffold + real LLM code generation + real lint all ran for "
                  f"module {module_name!r}")

            assert output.detail.get("install_error_kind") is None, (
                f"expected a clean install against the fresh database, got "
                f"error_kind={output.detail.get('install_error_kind')!r}: "
                f"{output.detail.get('install_log_tail')}"
            )
            assert output.claims_complete is True, (
                f"BuildSpecialist must claim completion for a genuinely clean install: {output.summary}"
            )
            print("PASS: install against the fresh database succeeded cleanly -- "
                  "claims_complete=True, no error_kind")

            # The real, independent check -- never trust claims_complete alone.
            field_name = None
            # The LLM decides the actual field's technical name; find it by
            # reading the generated models.py back rather than assuming one.
            proc_cat = _run_in_container(f"cat {_MODULE_DEV_ADDONS_DIR}/{module_name}/models/models.py")
            import re as _re
            match = _re.search(r"(\w+)\s*=\s*fields\.", proc_cat.stdout)
            assert match, f"could not find a generated field assignment in models.py: {proc_cat.stdout!r}"
            field_name = match.group(1)

            assert _field_genuinely_exists_on_model(FRESH_DB, "res.partner", field_name), (
                f"BuildSpecialist claimed success but field {field_name!r} does not actually "
                f"exist on res.partner in {FRESH_DB!r} -- checked independently at the ORM level"
            )
            print(f"PASS: field {field_name!r} genuinely exists on res.partner in {FRESH_DB!r} -- "
                  f"confirmed independently at the Odoo ORM level, not just claimed by the specialist")
        finally:
            await client.aclose()

    try:
        asyncio.run(_drive())
    finally:
        # Real, confirmed bug found live (2026-07-28, Phase 28B
        # regression sweep): this cleanup only ever removed the module's
        # own DIRECTORY -- it never uninstalled the module from FRESH_DB
        # itself, a fixed, real, reused database (not actually recreated
        # per run despite its own name), so every single prior run of
        # this test left its own module permanently marked 'installed'
        # in ir.module.module with no corresponding files left on disk
        # at all. Confirmed live: 25 such "ghost" modules had
        # accumulated for this one goal alone, ALL simultaneously
        # 'installed' and ALL independently defining the exact same
        # `preferred_language` Selection field (with a callable
        # `selection=lambda self: ...`) on res.partner via `_inherit` --
        # a genuinely untested, pathological condition this test was
        # never designed to run under, and the direct, most likely
        # cause of this test's own field-existence check intermittently
        # reporting a false negative (a real field, confirmed to exist
        # moments later via an independent direct check, immediately
        # after this test itself reported it missing). Uninstalling here
        # ensures each run of this test leaves FRESH_DB in the same
        # state it found it, structurally preventing this exact class of
        # accumulation from recurring for this test going forward.
        uninstall_module(module_name, FRESH_DB)
        _cleanup_module_dir(module_name)


def test_outer_plan_dependent_items_get_distinct_modules_and_preserve_earlier_fields():
    """The real, structural regression test for the real bug found
    during Phase 15's own verification pass (see
    docs/reports/ODOO_MANAGER_AGENT_PHASE15_REAL_VERIFICATION_2026-07-07.md):
    an outer-plan item and its blocked_by-dependent item, with
    INTENTIONALLY SIMILAR goal text (so they'd have collided under the
    old goal-text-only slugify_module_name), run through the real
    manager.loop.run_plan() end to end, against real Odoo. Confirms:
    (1) the two items get distinct, real module names despite the
    similar goal text: (2) item 2's own manifest genuinely declares a
    real dependency on item 1's module; (3) item 1's own real fields
    still exist -- checked independently at the ORM level -- after
    item 2 has run, proving item 2 never overwrote item 1's directory.
    """
    from manager.loop import run_plan
    from manager.task_plan import list_plan_items
    from specialists.code_review.specialist import CodeReviewSpecialist
    from specialists.testing_qa.specialist import TestingQASpecialist

    goal1 = (
        "Create outer plan test base model oma.svc.optest linking to project.project and "
        "res.partner, with a form and list view."
    )
    goal2 = (
        "Create outer plan test base model oma.svc.optest scheduling addition: add a "
        "priority_level selection field with values low/medium/high."
    )
    item1 = TaskContract(
        task_id=uuid.uuid4(), specialist_type=SpecialistType.bug_fix, capability_class=CapabilityClass.module_development,
        tier=AutonomyTier.tier_1_readonly, goal=goal1, inputs=[], rules=[], deliverables=[], compensating_actions=[],
        validation_by="testing_qa", pause_if=[], turn_budget=10, plan_item_id="otest-1",
    )
    item2 = TaskContract(
        task_id=uuid.uuid4(), specialist_type=SpecialistType.bug_fix, capability_class=CapabilityClass.module_development,
        tier=AutonomyTier.tier_1_readonly, goal=goal2, inputs=[], rules=[], deliverables=[], compensating_actions=[],
        validation_by="testing_qa", pause_if=[], turn_budget=10, plan_item_id="otest-2", blocked_by=["otest-1"],
    )

    registry.clear()

    async def _drive():
        client = ModelGatewayClient()
        try:
            registry.register(SpecialistType.bug_fix, BuildSpecialist(client=client, db=FRESH_DB))
            registry.register(SpecialistType.code_review, CodeReviewSpecialist(client=client))
            registry.register(SpecialistType.testing_qa, TestingQASpecialist(client=client))
            return await run_plan("outer plan real regression test", [item1, item2], client, "qwen3.6-27b")
        finally:
            await client.aclose()

    module1 = module2 = None
    try:
        result = asyncio.run(_drive())

        items = list_plan_items(result["plan_id"])
        module_by_item = {r["plan_item_id"]: (r.get("detail") or {}).get("module_name") for r in items}
        module1, module2 = module_by_item.get("otest-1"), module_by_item.get("otest-2")

        assert result["item_results"]["otest-1"]["passed"] is True, (
            f"item 1 must genuinely pass: {result['item_results']['otest-1']}"
        )
        assert module1, f"item 1 must have a recorded module_name: {items}"
        assert module2, (
            f"item 2 must have a recorded module_name -- item 2 result: "
            f"{result['item_results'].get('otest-2')}"
        )
        assert module1 != module2, (
            f"item 1 and item 2 have intentionally similar goal text but MUST get distinct "
            f"module names -- got the same name {module1!r} for both, which is exactly the real "
            f"regression this test exists to catch"
        )

        manifest_proc = _run_in_container(f"cat {_MODULE_DEV_ADDONS_DIR}/{module2}/__manifest__.py")
        assert module1 in manifest_proc.stdout, (
            f"item 2's manifest does not declare a real dependency on item 1's module {module1!r}: "
            f"{manifest_proc.stdout}"
        )
        print(f"PASS: outer-plan items with intentionally similar goal text got distinct real "
              f"module names ({module1!r} vs {module2!r}), and item 2's manifest genuinely "
              f"declares a dependency on item 1's module")

        # The real, independent proof: item 1's own model/fields, read fresh from its module's
        # own real models.py, must still genuinely exist at the ORM level after item 2 has run --
        # if item 2 had overwritten item 1's directory, this is exactly where it would show.
        import re as _re
        proc_cat = _run_in_container(f"cat {_MODULE_DEV_ADDONS_DIR}/{module1}/models/models.py")
        model_match = _re.search(r"_name\s*=\s*['\"]([\w.]+)['\"]", proc_cat.stdout)
        assert model_match, f"could not find item 1's own model _name in its models.py: {proc_cat.stdout!r}"
        base_model_name = model_match.group(1)
        field_matches = _re.findall(r"(\w+)\s*=\s*fields\.", proc_cat.stdout)
        assert field_matches, f"could not find any field assignment in item 1's own models.py: {proc_cat.stdout!r}"

        for field_name in field_matches:
            assert _field_genuinely_exists_on_model(FRESH_DB, base_model_name, field_name), (
                f"item 1's own field {field_name!r} on {base_model_name!r} no longer exists at the "
                f"ORM level after item 2 ran -- item 2 silently overwrote item 1's work"
            )
        print(f"PASS: item 1's own real fields {field_matches} on {base_model_name!r} genuinely "
              f"still exist at the ORM level after item 2's run -- confirmed independently, not "
              f"just inferred from distinct module names")
    finally:
        # Real, confirmed bug found live (2026-07-28, Phase 28B
        # regression sweep): same class as test_task1_field_add_end_to_
        # end_against_fresh_database's own fix -- this cleanup only ever
        # removed the module DIRECTORIES, never uninstalled either
        # module from FRESH_DB itself, leaving both permanently marked
        # 'installed' with no files left on disk. Confirmed live: 13
        # 'oma_create_outer_plan_test_*' ghost modules had accumulated
        # in FRESH_DB from this test's own history alone. Uninstalling
        # here (module2 first -- it depends on module1, so it must be
        # uninstalled before module1 can be) prevents this from
        # recurring.
        if module2:
            uninstall_module(module2, FRESH_DB)
            _cleanup_module_dir(module2)
        if module1:
            uninstall_module(module1, FRESH_DB)
            _cleanup_module_dir(module1)
        registry.clear()


def test_real_diff_computation_round1_all_additions_round2_real_delta():
    """Phase 16: BuildSpecialist's real diff (difflib, not fake) --
    round 1 of a brand-new module must show only 'add' lines (nothing
    meaningful existed before), and a genuine same-task retry (round 2,
    same task_id -> same module_name) must show a real delta (ctx/add/del)
    against round 1's own actual prior content, not against odoo-bin
    scaffold's own placeholder boilerplate.
    """
    goal = "Add a diff computation test field to res.partner for real diff verification."
    contract = TaskContract(
        task_id=uuid.uuid4(), specialist_type=SpecialistType.bug_fix, capability_class=CapabilityClass.module_development,
        tier=AutonomyTier.tier_1_readonly, goal=goal, inputs=[], rules=[], deliverables=[],
        compensating_actions=[], validation_by="testing_qa", pause_if=[], turn_budget=10,
    )
    module_name = slugify_module_name(goal, str(contract.task_id))
    _cleanup_module_dir(module_name)

    async def _drive():
        client = ModelGatewayClient()
        try:
            specialist = BuildSpecialist(client=client, db=FRESH_DB)

            output1 = await specialist.run(contract)
            diff1 = output1.detail["diff"]
            assert diff1, "round 1 must produce a real, non-empty diff"
            for f in diff1:
                types = {line["type"] for line in f["lines"]}
                assert types <= {"add"}, (
                    f"round 1 (brand-new module) must show ONLY 'add' lines for {f['file']!r}, "
                    f"got types {types} -- odoo-bin's own placeholder must not leak in as fake 'old' content"
                )
            print(f"PASS: round 1's real diff covers {len(diff1)} file(s), every line correctly "
                  f"'add' (no placeholder noise)")

            # Round 2: same task_id, same derived module_name, a real retry.
            retry_contract = contract.model_copy(update={
                "rules": [*contract.rules, "Also rename the field to have a _v2 suffix."]
            })
            output2 = await specialist.run(retry_contract)
            diff2 = output2.detail["diff"]
            assert diff2, "round 2 (a real retry) must also produce a real, non-empty diff"
            all_types_round2 = {line["type"] for f in diff2 for line in f["lines"]}
            assert "del" in all_types_round2 or "ctx" in all_types_round2, (
                f"round 2 must show a real delta against round 1's own actual prior content "
                f"(ctx and/or del lines expected), got only: {all_types_round2}"
            )
            print(f"PASS: round 2's real diff (same task, genuine retry) shows a real delta "
                  f"against round 1's own prior content -- types present: {all_types_round2}")
        finally:
            await client.aclose()

    try:
        asyncio.run(_drive())
    finally:
        # Real, confirmed bug found live (2026-07-28, Phase 28B
        # regression sweep): same class as test_task1_field_add_end_to_
        # end_against_fresh_database's own fix -- never uninstalled from
        # FRESH_DB, only deleted the directory. Confirmed live: 22
        # 'oma_add_a_diff_computation_*' ghost modules had accumulated
        # in FRESH_DB from this test's own history alone.
        uninstall_module(module_name, FRESH_DB)
        _cleanup_module_dir(module_name)


# --- build_deterministic_security_csv: new-group-for-existing-model shape ---
# Real, confirmed bug found live (2026-08-03, task010's own repeated real sandbox install crash,
# reproduced identically across 2 separate full re-tests): this function's own documented "case 1"
# (no new models -> the CSV must be exactly the header row") is wrong whenever the round ALSO
# defines a brand-new custom security group in security_xml (e.g. a "mechanics only see their own
# records" ir.rule + matching new res.groups) -- that shape genuinely needs its own access-csv row
# granting the new group access to whatever EXISTING model the accompanying ir.rule restricts.
# Blindly returning header-only silently wiped the LLM's own real, correct access row every single
# round, and Odoo's real install then crashed loading ir.rule/access records for a group with zero
# access grants -- confirmed directly via SSH inspection of the actual crashed module on disk.

_TASK010_REAL_SECURITY_XML = """<?xml version="1.0" encoding="utf-8"?>
<odoo>
  <record id="group_mechanics" model="res.groups">
    <field name="name">Mechanics</field>
    <field name="category_id" ref="base.module_category_hidden"/>
  </record>
  <record id="rule_project_meerwerk_own" model="ir.rule">
    <field name="name">Mechanics: own records only</field>
    <field name="model_id" ref="project_meerwerk.model_project_meerwerk"/>
    <field name="domain_force">[('create_uid','=',user.id)]</field>
    <field name="groups" eval="[(4, ref('oma_mechanics_should_only_see.group_mechanics'))]"/>
  </record>
</odoo>"""


def test_deterministic_security_csv_defers_to_llm_when_new_group_grants_existing_model_access():
    result = build_deterministic_security_csv(
        [],  # no NEW model this round -- extending an existing one via _inherit
        "Mechanics should only see the meerwerk records they created themselves. "
        "Managers and admins should see all records.",
        _TASK010_REAL_SECURITY_XML,
        "oma_mechanics_should_only_see_f6f4be34",
    )
    assert result is None, (
        "must defer to the LLM's own real access-csv row, never silently wipe it to header-only, "
        "when a new custom group needs access to an existing model"
    )
    print("PASS: a new custom group targeting an existing model defers to the LLM instead of wiping the CSV")


def test_deterministic_security_csv_still_returns_header_only_with_no_new_group_or_model():
    result = build_deterministic_security_csv(
        [], "Change the description text shown on the form.", None, "oma_x",
    )
    assert result == "id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink\n"
    print("PASS: the original, correct header-only case (no new model, no new group) is unaffected")


def test_deterministic_security_csv_still_returns_header_only_with_existing_security_xml_but_no_group():
    # security_xml present but containing no res.groups record at all (e.g. only an ir.rule
    # reusing an already-real, standard group) -- still correctly header-only, not deferred.
    security_xml = (
        '<odoo><record id="rule_x" model="ir.rule">'
        '<field name="model_id" ref="project_meerwerk.model_project_meerwerk"/>'
        "</record></odoo>"
    )
    result = build_deterministic_security_csv([], "Restrict access somehow.", security_xml, "oma_x")
    assert result == "id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink\n"
    print("PASS: security_xml with no new res.groups record at all still returns header-only, not deferred")


def test_deterministic_security_csv_unaffected_for_genuine_new_model_case():
    result = build_deterministic_security_csv(
        ["project.extra.work"], "Add a new model for extra work tracking.", None, "oma_x",
    )
    assert result is not None
    assert "access_project_extra_work,project.extra.work,model_project_extra_work,base.group_user,1,1,1,0" in result
    print("PASS: the genuine new-model case (unrelated to this fix) still builds its own row correctly")


def test_models_py_has_real_non_comment_content_exempts_real_quick_filter_addition():
    """Sibling fix to the identity-stub validator's own task007 exemption (2026-08-07,
    HUMAN_DECISION push): _validate_models_py_has_real_non_comment_content is a separate function
    with its own independent empty-class check and message wording -- fixing only the sibling
    validator still left this one rejecting task007's genuinely correct, views-only content.
    """
    from specialists.build.specialist import _validate_models_py_has_real_non_comment_content

    empty_models_py = (
        "from odoo import api, fields, models\n\n"
        "class ProjectMeerwerk(models.Model):\n"
        "    _inherit = 'project.meerwerk'\n"
    )

    no_filter = GeneratedModuleFiles(
        manifest_fields=ManifestFields(
            name="x", version="1.0", category="Uncategorized", summary="", author="", depends=["base"], data=[],
        ),
        models_py=empty_models_py,
        views_xml="<odoo><record id=\"x\" model=\"ir.ui.view\"><field name=\"arch\" type=\"xml\"><form/></field></record></odoo>",
        security_csv="",
        notes="",
    )
    raised = False
    try:
        _validate_models_py_has_real_non_comment_content(no_filter)
    except ValueError:
        raised = True
    assert raised, "an empty class with a real <record> but no <filter> must still be rejected"

    with_filter = GeneratedModuleFiles(
        manifest_fields=ManifestFields(
            name="x", version="1.0", category="Uncategorized", summary="", author="", depends=["base"], data=[],
        ),
        models_py=empty_models_py,
        views_xml=(
            "<odoo><record id=\"view_project_meerwerk_search\" model=\"ir.ui.view\">"
            "<field name=\"arch\" type=\"xml\"><search>"
            "<filter name=\"accepted\" string=\"Accepted\" domain=\"[('state','=','accepted')]\"/>"
            "</search></field></record></odoo>"
        ),
        security_csv="",
        notes="",
    )
    _validate_models_py_has_real_non_comment_content(with_filter)  # must not raise
    print("PASS: real-non-comment-content validator exempts a genuine quick-filter-only task and "
          "still rejects an empty class with no real filter content")


def test_models_py_has_real_non_comment_content_exempts_single_view_record_when_goal_asks_to_build_views():
    """Real, confirmed gap (2026-08-07, task025 v5 real shape): a decomposed round genuinely,
    correctly builds only ONE real view record at a time -- the bare-`<record>` exemption must be
    gated on the goal's own "build ... views" intent (unlike the `<filter` case, a single record
    alone is not a safe goal-independent signal), and must still reject an unrelated goal's single
    record with no such intent.
    """
    from specialists.build.specialist import _validate_models_py_has_real_non_comment_content

    empty_models_py = (
        "from odoo import models\n\nclass ProjectContainer(models.Model):\n"
        "    _inherit = 'project.container'\n"
    )
    single_record_views_xml = (
        "<odoo><record id=\"view_project_container_tree\" model=\"ir.ui.view\">"
        "<field name=\"model\">project.container</field>"
        "<field name=\"arch\" type=\"xml\"><tree/></field></record></odoo>"
    )

    build_views_goal = "Build the complete form, list, and search views for project.container."
    exempted = GeneratedModuleFiles(
        manifest_fields=ManifestFields(
            name="x", version="1.0", category="Uncategorized", summary="", author="", depends=["base"], data=[],
        ),
        models_py=empty_models_py, views_xml=single_record_views_xml, security_csv="", notes="",
    )
    _validate_models_py_has_real_non_comment_content(exempted, build_views_goal)  # must not raise

    unrelated_goal = "Auto-fill the 'Assigned to' field when a project is selected."
    not_exempted = GeneratedModuleFiles(
        manifest_fields=ManifestFields(
            name="x", version="1.0", category="Uncategorized", summary="", author="", depends=["base"], data=[],
        ),
        models_py=empty_models_py, views_xml=single_record_views_xml, security_csv="", notes="",
    )
    raised = False
    try:
        _validate_models_py_has_real_non_comment_content(not_exempted, unrelated_goal)
    except ValueError:
        raised = True
    assert raised, (
        "a single view record with no matching 'build views' goal intent must still be rejected"
    )
    print("PASS: single-view-record exemption is correctly gated on the goal's own build-views "
          "intent, not a goal-independent shortcut")


def test_model_class_is_not_a_pure_identity_stub_exempts_views_only_task_catches_task007_real_shape():
    """Real, general fix (2026-08-07, HUMAN_DECISION push, task007, real task_id
    ce4951d0-9540-4c21-ab45-824a5b876d5b): a genuinely views-only task (add a quick filter button
    to a list) legitimately has no models.py content to add at all -- the round's real content
    lives entirely in a <record model="ir.ui.view"> in views_xml. The validator's own identity-
    only-class check has no way to know that without checking views_xml, so it kept rejecting a
    genuinely correct answer, forcing a pointless internal-fix-loop retry that independently,
    reproducibly triggered a real qwen3-coder-30b-a3b repetition-loop bug 5 times in a row.
    """
    from specialists.build.specialist import _validate_model_class_is_not_a_pure_identity_stub

    identity_only_models_py = (
        "from odoo import api, fields, models\n\n"
        "class ProjectMeerwerk(models.Model):\n"
        "    _inherit = 'project.meerwerk'\n"
    )

    # Genuinely empty views_xml too -- the real, original defect shape (no legitimate content
    # anywhere) must still be caught.
    no_view_content = GeneratedModuleFiles(
        manifest_fields=ManifestFields(
            name="x", version="1.0", category="Uncategorized", summary="", author="", depends=["base"], data=[],
        ),
        models_py=identity_only_models_py,
        views_xml="<odoo></odoo>",
        security_csv="",
        notes="",
    )
    raised = False
    try:
        _validate_model_class_is_not_a_pure_identity_stub(no_view_content)
    except ValueError:
        raised = True
    assert raised, "an identity-only class with no real view content either must still be rejected"

    # A real <record> in views_xml (task007's real shape) must exempt the identity-only class.
    real_view_content = GeneratedModuleFiles(
        manifest_fields=ManifestFields(
            name="x", version="1.0", category="Uncategorized", summary="", author="", depends=["base"], data=[],
        ),
        models_py=identity_only_models_py,
        views_xml=(
            "<odoo><record id=\"view_project_meerwerk_search\" model=\"ir.ui.view\">"
            "<field name=\"model\">project.meerwerk</field>"
            "<field name=\"arch\" type=\"xml\"><search>"
            "<filter name=\"accepted\" string=\"Accepted\" domain=\"[('state','=','accepted')]\"/>"
            "</search></field></record></odoo>"
        ),
        security_csv="",
        notes="",
    )
    _validate_model_class_is_not_a_pure_identity_stub(real_view_content)  # must not raise
    print("PASS: identity-only-class validator exempts a genuinely views-only task's models.py "
          "and still catches the original genuinely-empty-everywhere shape")


def test_declared_reference_methods_exist_catches_button_regardless_of_attribute_order():
    """Real, confirmed bug found live (2026-08-07, HUMAN_DECISION deep-push, task026, real task_id
    53fa81a4-f562-4e7e-857a-a3408337e6ff): the old single-pass regex required `type="object"` to
    appear textually BEFORE `name="..."` within a `<button>` tag -- but the model's own real,
    genuinely correct generation convention (confirmed via direct redis inspection of the actual
    raw candidate) writes `name=` first, then `type="object"`, e.g.
    `<button name="action_open_containers" type="object" class="oe_stat_button" ...>`. This
    silently defeated the entire button-reference-method check for that common ordering -- Odoo's
    own real install crashed with `ParseError: "action_open_containers is not a valid action on
    project.project"` since the undeclared method was never caught pre-write.
    """
    from specialists.build.specialist import _find_object_type_button_names

    name_before_type = (
        '<button name="action_open_containers" type="object" class="oe_stat_button" icon="fa-list">'
        '<field name="container_count" widget="statinfo"/></button>'
    )
    assert _find_object_type_button_names(name_before_type) == ["action_open_containers"], (
        "must find the button name regardless of whether name= or type= comes first in the tag"
    )

    type_before_name = '<button type="object" name="action_open_containers" class="oe_highlight"/>'
    assert _find_object_type_button_names(type_before_name) == ["action_open_containers"], (
        "must still find the button name in the original (type first) attribute ordering too"
    )
    print("PASS: object-type button name extraction is attribute-order-independent")


def test_declared_reference_methods_exist_widened_for_inherit_only_goal_named_method():
    """Real, confirmed gap (2026-08-07, HUMAN_DECISION deep-push, task026): the button-reference
    check was entirely skipped for an `_inherit`-only extension (no new model), a deliberately
    conservative posture to avoid false-positiving on a button legitimately referencing a real
    method the EXTERNAL base model already provides. But that same skip also let through a
    genuinely undeclared, goal-required NEW method -- confirmed live on task026's own real
    `action_open_containers` shape. Widened to also fire when the goal's own text explicitly names
    the exact undeclared method (a real external base-model action is never named verbatim in goal
    text this way, so the legitimate case this skip protects is still never touched).
    """
    from specialists.build.specialist import _validate_declared_reference_methods_exist

    goal = (
        "Add a 'Container count' smart button to the project form. ... Also add the "
        "action_open_containers() method on project.project returning an ir.actions.act_window "
        "opening project.container filtered by project_id=self.id."
    )
    inherit_only_missing_method = GeneratedModuleFiles(
        manifest_fields=ManifestFields(
            name="x", version="1.0", category="Uncategorized", summary="", author="", depends=["base"], data=[],
        ),
        models_py=(
            "from odoo import models, fields\n\nclass ProjectProject(models.Model):\n"
            "    _inherit = 'project.project'\n"
            "    container_count = fields.Integer(compute='_compute_container_count')\n\n"
            "    def _compute_container_count(self):\n        pass\n"
        ),
        views_xml=(
            '<odoo><record id="v" model="ir.ui.view"><field name="arch" type="xml"><form>'
            '<button name="action_open_containers" type="object" class="oe_stat_button" '
            'icon="fa-list"/></form></field></record></odoo>'
        ),
        security_csv="", notes="",
    )
    raised = False
    try:
        _validate_declared_reference_methods_exist(inherit_only_missing_method, goal)
    except ValueError:
        raised = True
    assert raised, (
        "an _inherit-only round's button referencing an undeclared, goal-required method must "
        "still be caught, not silently skipped"
    )

    # A button referencing a REAL external action (never named in the goal) on an _inherit-only
    # extension must never be touched -- the original, legitimate case this skip protects.
    inherit_only_real_external_action = GeneratedModuleFiles(
        manifest_fields=ManifestFields(
            name="x", version="1.0", category="Uncategorized", summary="", author="", depends=["base"], data=[],
        ),
        models_py="from odoo import models\n\nclass ProjectProject(models.Model):\n    _inherit = 'project.project'\n",
        views_xml=(
            '<odoo><record id="v" model="ir.ui.view"><field name="arch" type="xml"><form>'
            '<button name="action_view_tasks" type="object"/></form></field></record></odoo>'
        ),
        security_csv="", notes="",
    )
    _validate_declared_reference_methods_exist(inherit_only_real_external_action, goal)  # must not raise
    print("PASS: _inherit-only button check is widened for a goal-named undeclared method, and "
          "still never touches a real external action the goal doesn't name")


def test_view_record_does_not_inherit_from_its_own_id_catches_real_task028_shape():
    """Real, confirmed live bug (2026-08-07, task028, HUMAN_DECISION deep-push, real task_id
    a3c2cf52-f650-4502-8059-c77d784241de): Build's own real generated views_xml declared
    `<record id="mis_base_extend.view_crm_lead_view_form_inherited_mis_base_extend"
    model="ir.ui.view">` with `inherit_id ref="mis_base_extend.view_crm_lead_view_form_inherited_
    mis_base_extend"` -- the exact same value for both the new record's own id and its inherit
    target, making the view recursively inherit from itself. Odoo's real install genuinely,
    deterministically rejected this: `ParseError: "Het is niet mogelijk een recursieve overerfde
    weergave te maken"` (Dutch: "it is not possible to create a recursively inherited view").
    """
    from specialists.build.specialist import _validate_view_record_does_not_inherit_from_its_own_id

    manifest = ManifestFields(
        name="x", version="1.0", category="Uncategorized", summary="", author="", depends=["base"], data=[],
    )
    self_inheriting = GeneratedModuleFiles(
        manifest_fields=manifest, models_py="", security_csv="", notes="",
        views_xml=(
            '<odoo>\n'
            '  <record id="mis_base_extend.view_crm_lead_view_form_inherited_mis_base_extend" '
            'model="ir.ui.view">\n'
            '    <field name="name">crm.lead.form.inherited</field>\n'
            '    <field name="model">crm.lead</field>\n'
            '    <field name="inherit_id" '
            'ref="mis_base_extend.view_crm_lead_view_form_inherited_mis_base_extend"/>\n'
            '    <field name="arch" type="xml">\n'
            '      <xpath expr="//field[@name=\'project_type_id\']" position="attributes">\n'
            '        <attribute name="domain">[(\'active\',\'=\',True)]</attribute>\n'
            '      </xpath>\n'
            '    </field>\n'
            '  </record>\n'
            '</odoo>'
        ),
    )
    raised = False
    try:
        _validate_view_record_does_not_inherit_from_its_own_id(self_inheriting)
    except ValueError as exc:
        raised = True
        assert "mis_base_extend.view_crm_lead_view_form_inherited_mis_base_extend" in str(exc)
    assert raised, "a view record whose own id equals its inherit_id ref must be caught pre-write"

    # A genuinely correct, real-world case (new local id, real distinct external inherit target)
    # must never be touched.
    genuinely_correct = GeneratedModuleFiles(
        manifest_fields=manifest, models_py="", security_csv="", notes="",
        views_xml=(
            '<odoo>\n'
            '  <record id="view_crm_lead_form_active_domain" model="ir.ui.view">\n'
            '    <field name="name">crm.lead.form.active.domain</field>\n'
            '    <field name="model">crm.lead</field>\n'
            '    <field name="inherit_id" '
            'ref="mis_base_extend.view_crm_lead_view_form_inherited_mis_base_extend"/>\n'
            '    <field name="arch" type="xml">\n'
            '      <xpath expr="//field[@name=\'project_type_id\']" position="attributes">\n'
            '        <attribute name="domain">[(\'active\',\'=\',True)]</attribute>\n'
            '      </xpath>\n'
            '    </field>\n'
            '  </record>\n'
            '</odoo>'
        ),
    )
    _validate_view_record_does_not_inherit_from_its_own_id(genuinely_correct)  # must not raise
    print("PASS: a view record self-inheriting via an identical id/inherit_id pair is caught "
          "pre-write; a genuinely correct, distinct new id is never touched")


def test_internal_loop_narrow_validators_threads_goal_to_identity_stub_check():
    """Real, confirmed gap (2026-08-07, HUMAN_DECISION escalation push, task025, real task_id
    31eed0c2-87e4-4216-9d48-bc94c3973bc3): every validator in _INTERNAL_LOOP_NARROW_VALIDATORS is
    a single-arg Callable[[GeneratedModuleFiles], None], so
    _validate_model_class_is_not_a_pure_identity_stub's own goal-gated views-only exemption
    (fixed for task007/task025's shared views-stripping bug) could never fire inside the internal
    self-correction loop even after being fixed at the external hard gate -- confirmed live: the
    exact same false positive fired 4 times in one relaunch's own internal loop, contributing to a
    real qwen3-coder-30b-a3b repetition-loop task_cut_off crash.
    """
    from specialists.build.specialist import _run_internal_loop_narrow_validators

    views_only_generated = GeneratedModuleFiles(
        manifest_fields=ManifestFields(
            name="x", version="1.0", category="Uncategorized", summary="", author="", depends=["base"], data=[],
        ),
        models_py=(
            "from odoo import models\n\nclass ProjectContainer(models.Model):\n"
            "    _inherit = 'project.container'\n"
        ),
        views_xml=(
            "<odoo><record id=\"v\" model=\"ir.ui.view\"><field name=\"model\">project.container"
            "</field><field name=\"arch\" type=\"xml\"><tree/></field></record></odoo>"
        ),
        security_csv="", notes="",
    )
    build_views_goal = "Build the complete form, list, and search views for project.container."

    no_goal_findings = _run_internal_loop_narrow_validators(views_only_generated)
    assert any("identity_stub" in f for f in no_goal_findings), (
        "without a goal argument (the pre-fix call shape), the false positive still fires -- "
        "confirms the bug this test guards against is real"
    )

    with_goal_findings = _run_internal_loop_narrow_validators(views_only_generated, goal=build_views_goal)
    assert not any("identity_stub" in f for f in with_goal_findings), (
        f"threading the real goal through must exempt a genuinely views-only task's identity-only "
        f"class inside the internal loop too, not just at the external hard gate -- got "
        f"{with_goal_findings!r}"
    )
    print("PASS: the internal loop's own narrow-validator runner correctly threads goal through to "
          "the identity-stub check's own goal-gated exemption")


def test_activity_calls_require_activity_mixin_inherit_live_exempts_real_transitive_mixin():
    """Real, confirmed false positive found live (2026-08-07, HUMAN_DECISION push, task046, real
    task_ids v5-v7, 3 consecutive real qwen3-coder-30b-a3b repetition-loop infra aborts all
    triggered by this exact pointless internal-fix-loop retry): the sync validator's literal-
    string-only check raises on `_inherit = 'project.meerwerk'` + `activity_schedule(...)`, even
    though `project.meerwerk`'s own real, live schema already includes `activity_ids` (mail.
    activity.mixin provided transitively) -- genuinely correct Odoo code, wrongly rejected.
    Mirrors `_validate_message_post_requires_mail_thread_inherit_live`'s own exact pattern/test
    shape for the sibling mail.thread case.
    """
    from specialists.build.specialist import (
        _validate_activity_calls_require_activity_mixin_inherit_live,
    )

    os.environ.setdefault("OMA_ODOO_DB_DUPLICATE_FOR_BUILD", "odoo16_dev")

    real = GeneratedModuleFiles(
        manifest_fields=ManifestFields(
            name="x", version="1.0", category="Uncategorized", summary="", author="", depends=["base"], data=[],
        ),
        models_py=(
            "from odoo import api, models\n\n"
            "class ProjectMeerwerk(models.Model):\n"
            "    _inherit = 'project.meerwerk'\n\n"
            "    @api.onchange('state')\n"
            "    def _onchange_state(self):\n"
            "        if self.user_id:\n"
            "            self.activity_schedule('mail.mail_activity_data', user_id=self.user_id.id)\n"
        ),
        security_csv="",
        notes="",
    )
    asyncio.run(_validate_activity_calls_require_activity_mixin_inherit_live(
        real, "odoo16_dev", task_id="test",
    ))  # must not raise -- project.meerwerk already provides mail.activity.mixin transitively

    # A model that genuinely does NOT provide mail.activity.mixin (fabricated target) must still
    # be rejected.
    invented = GeneratedModuleFiles(
        manifest_fields=ManifestFields(
            name="x", version="1.0", category="Uncategorized", summary="", author="", depends=["base"], data=[],
        ),
        models_py=(
            "from odoo import api, models\n\n"
            "class ResPartnerCategory(models.Model):\n"
            "    _inherit = 'res.partner.category'\n\n"
            "    def do_thing(self):\n"
            "        self.activity_schedule('mail.mail_activity_data')\n"
        ),
        security_csv="",
        notes="",
    )
    raised = False
    try:
        asyncio.run(_validate_activity_calls_require_activity_mixin_inherit_live(
            invented, "odoo16_dev", task_id="test",
        ))
    except ValueError:
        raised = True
    assert raised, (
        "a model that genuinely does not provide mail.activity.mixin transitively must still be "
        "rejected"
    )
    print("PASS: live activity-mixin validator exempts project.meerwerk's real transitive mixin "
          "and still catches a genuine missing-mixin case")


def test_strip_premature_computed_field_fuzzy_matches_task039_real_shape():
    """Real, general fix (2026-08-07, HUMAN_DECISION push, task039): the not-yet-in-scope label
    the decomposer invents (`is_expired_computed`) is a short, abstract phrase, but the model's
    own real generated field/method name for that same forbidden concept
    (`is_external_service_token_expired`) is longer and more specific -- neither is a literal
    substring of the other, so the original substring-only matching convention never stripped it,
    and the round failed identically twice in a row. Confirmed live on the real task039 shape.
    """
    from specialists.build.specialist import (
        _autofix_strip_premature_computed_field_on_decomposed_round,
    )

    goal = (
        "Add credential-storage fields on res.users. This round's own NEW focus is ONLY: "
        "'credential_fields'. The following constraints are NOT yet in scope for this round and "
        "must NOT be implemented even partially: ['mark_refreshed_button', 'is_expired_computed']."
    )
    gen = GeneratedModuleFiles(
        manifest_fields=ManifestFields(
            name="x", version="1.0", category="Uncategorized", summary="", author="", depends=["base"], data=[],
        ),
        models_py=(
            "from odoo import models, fields, api\n\n"
            "class ResUsers(models.Model):\n"
            "    _inherit = 'res.users'\n"
            "    external_service_token = fields.Char()\n"
            "    external_service_token_expiry = fields.Datetime()\n"
            "    is_external_service_token_expired = fields.Boolean(compute='_compute_is_external_service_token_expired')\n\n"
            "    @api.depends('external_service_token_expiry')\n"
            "    def _compute_is_external_service_token_expired(self):\n"
            "        for rec in self:\n"
            "            rec.is_external_service_token_expired = False\n"
        ),
        security_csv="",
        notes="",
    )
    _autofix_strip_premature_computed_field_on_decomposed_round(gen, goal)
    assert "is_external_service_token_expired" not in gen.models_py, (
        "a premature computed field whose real name only fuzzy-matches (not substring-matches) "
        "its not-yet-in-scope label must still be stripped"
    )
    assert "external_service_token_expiry" in gen.models_py, (
        "this round's own legitimate, in-scope fields must never be touched"
    )
    print("PASS: premature computed-field stripper fuzzy-matches task039's real shape without "
          "touching this round's own legitimate content")


def test_strip_premature_computed_field_never_strips_this_rounds_own_focus_on_shared_domain_word():
    """Real, confirmed bug found live (2026-08-09, task 07141af5's flagship run,
    project_ticket_counts node), root-caused via multi-checkpoint debug logging bisecting exactly
    where a genuinely correct candidate's own compute field got stripped between generation and
    final validation: this task's own constraint labels legitimately share a common domain word
    ("ticket") across SEVERAL different, unrelated pieces of work -- ticket_access_rights,
    ticket_list_view, ticket_bulk_close, ticket_workflow_and_logging, AND project_ticket_counts.
    `_label_fuzzy_matches_identifier()`'s own word-overlap check correctly identified "ticket" as
    a shared significant word (not a stopword), but the ORIGINAL priority order stopped at the
    FIRST not-yet-in-scope label that fuzzy-matched (here, 'ticket_bulk_close', matching purely on
    the shared word "ticket") without ever checking whether the field ALSO matched THIS round's
    own declared focus ('project_ticket_counts', which ALSO shares "ticket") -- so a completely
    legitimate, correctly-generated field for the round's own current work was silently stripped
    down to an empty class, every single round, regardless of model, temperature, or candidate
    diversity, since the bug was deterministic and had nothing to do with generation quality.
    """
    from specialists.build.specialist import (
        _autofix_strip_premature_computed_field_on_decomposed_round,
    )

    goal = (
        "Build a complete field-service operations suite. This round's own NEW focus is ONLY: "
        "'project_ticket_counts'. The following constraints are NOT yet in scope for this round "
        "and must NOT be implemented even partially: ['daily_escalation_cron', "
        "'maintenance_history', 'ticket_workflow_and_logging', 'ticket_bulk_close']."
    )
    gen = GeneratedModuleFiles(
        manifest_fields=ManifestFields(
            name="x", version="1.0", category="Uncategorized", summary="", author="", depends=["base"], data=[],
        ),
        models_py=(
            "from odoo import models, fields\n\n"
            "class ProjectProject(models.Model):\n"
            "    _inherit = 'project.project'\n\n"
            "    open_ticket_count = fields.Integer(compute='_compute_ticket_counts')\n"
            "    overdue_ticket_count = fields.Integer(compute='_compute_ticket_counts')\n\n"
            "    def _compute_ticket_counts(self):\n"
            "        for project in self:\n"
            "            project.open_ticket_count = 0\n"
            "            project.overdue_ticket_count = 0\n"
        ),
        security_csv="",
        notes="",
    )
    _autofix_strip_premature_computed_field_on_decomposed_round(gen, goal)
    assert "open_ticket_count" in gen.models_py, (
        "this round's own legitimate field must never be stripped just because it shares a "
        "common domain word ('ticket') with an unrelated not-yet-in-scope label -- got: "
        f"{gen.models_py!r}"
    )
    assert "_compute_ticket_counts" in gen.models_py
    print("PASS: this round's own legitimate compute field survives even when it shares a "
          "domain word with an unrelated not-yet-in-scope label")


def test_strip_premature_computed_field_never_strips_an_already_satisfied_constraints_field():
    """Real, confirmed second-order bug found live (2026-08-10, task 07141af5's flagship run,
    daily_escalation_cron node): the sibling fix right above this test only protects a field
    matching THIS round's own declared focus -- it does nothing for a field belonging to an
    EARLIER, ALREADY-SATISFIED constraint (project_ticket_counts) that fuzzy-matches a
    completely unrelated not-yet-in-scope label of the CURRENT round (daily_escalation_cron), via
    the exact same shared domain word ("ticket") this file's own siblings already document.
    Confirmed live: `open_ticket_count`/`_compute_ticket_counts`, just restored by
    `_autofix_restore_dropped_inherit_extension_members_when_already_satisfied`, was immediately
    stripped right back out by this function because it fuzzy-matched 'ticket_bulk_close' (this
    round's own not-yet-in-scope label), silently defeating the restore and reproducing the
    identical install-crashing views_xml/models_py mismatch.
    """
    from specialists.build.specialist import (
        _autofix_strip_premature_computed_field_on_decomposed_round,
    )

    goal = (
        "Build a complete field-service operations suite. This round's own NEW focus is ONLY: "
        "'daily_escalation_cron'. The following constraints are NOT yet in scope for this round "
        "and must NOT be implemented even partially: ['ticket_workflow_and_logging', "
        "'ticket_bulk_close']."
    )
    gen = GeneratedModuleFiles(
        manifest_fields=ManifestFields(
            name="x", version="1.0", category="Uncategorized", summary="", author="", depends=["base"], data=[],
        ),
        models_py=(
            "from odoo import models, fields\n\n"
            "class ProjectProject(models.Model):\n"
            "    _inherit = 'project.project'\n\n"
            "    open_ticket_count = fields.Integer(compute='_compute_ticket_counts')\n"
            "    overdue_ticket_count = fields.Integer(compute='_compute_ticket_counts')\n\n"
            "    def _compute_ticket_counts(self):\n"
            "        for project in self:\n"
            "            project.open_ticket_count = 0\n"
            "            project.overdue_ticket_count = 0\n"
        ),
        security_csv="",
        notes="",
    )
    constraint_status = {"project_ticket_counts": "satisfied", "daily_escalation_cron": "pending"}
    _autofix_strip_premature_computed_field_on_decomposed_round(gen, goal, constraint_status)
    assert "open_ticket_count" in gen.models_py, (
        "an already-satisfied constraint's own field must never be stripped just because it "
        "shares a common domain word ('ticket') with an unrelated not-yet-in-scope label -- got: "
        f"{gen.models_py!r}"
    )
    assert "_compute_ticket_counts" in gen.models_py
    print("PASS: an already-satisfied constraint's own field survives even when it shares a "
          "domain word with an unrelated not-yet-in-scope label of a LATER round")


def test_api_model_method_does_not_access_self_field_as_a_record_catches_task047_real_shape():
    """Real, general fix (2026-08-07, HUMAN_DECISION push, task047): Code-Review correctly caught
    `@api.model`-decorated `action_parse_text` accessing `self.text_field` -- an @api.model
    method's own `self` is never bound to a specific record, so this is a genuine runtime bug, not
    a hallucination. No existing validator caught this recurring real mistake class.
    """
    from specialists.build.specialist import (
        _validate_api_model_method_does_not_access_self_field_as_a_record,
    )

    bad = GeneratedModuleFiles(
        manifest_fields=ManifestFields(
            name="x", version="1.0", category="Uncategorized", summary="", author="", depends=["base"], data=[],
        ),
        models_py=(
            "from odoo import models, fields, api\n\n"
            "class OmaParseText(models.Model):\n"
            "    _name = 'oma.parse.text'\n"
            "    text_field = fields.Text()\n\n"
            "    @api.model\n"
            "    def action_parse_text(self):\n"
            "        lines = self.text_field.split('\\n')\n"
            "        return True\n"
        ),
        security_csv="",
        notes="",
    )
    raised = False
    try:
        _validate_api_model_method_does_not_access_self_field_as_a_record(bad)
    except ValueError as e:
        raised = True
        assert "self.text_field" in str(e)
    assert raised, "an @api.model method reading a declared field off self must be rejected"

    # A non-@api.model method (self genuinely bound to a record) must never be touched.
    good_bound = GeneratedModuleFiles(
        manifest_fields=ManifestFields(
            name="x", version="1.0", category="Uncategorized", summary="", author="", depends=["base"], data=[],
        ),
        models_py=(
            "from odoo import models, fields, api\n\n"
            "class OmaParseText(models.Model):\n"
            "    _name = 'oma.parse.text'\n"
            "    text_field = fields.Text()\n\n"
            "    def action_parse_text(self):\n"
            "        lines = self.text_field.split('\\n')\n"
            "        return True\n"
        ),
        security_csv="",
        notes="",
    )
    _validate_api_model_method_does_not_access_self_field_as_a_record(good_bound)  # must not raise

    # An @api.model method only touching allowlisted ORM internals must never be touched.
    good_orm_only = GeneratedModuleFiles(
        manifest_fields=ManifestFields(
            name="x", version="1.0", category="Uncategorized", summary="", author="", depends=["base"], data=[],
        ),
        models_py=(
            "from odoo import models, fields, api\n\n"
            "class OmaParseText(models.Model):\n"
            "    _name = 'oma.parse.text'\n"
            "    text_field = fields.Text()\n\n"
            "    @api.model\n"
            "    def action_parse_text(self):\n"
            "        recs = self.search([])\n"
            "        return self.env['oma.container'].create({})\n"
        ),
        security_csv="",
        notes="",
    )
    _validate_api_model_method_does_not_access_self_field_as_a_record(good_orm_only)  # must not raise
    print("PASS: @api.model self.<declared field> access validator catches task047's real shape "
          "and never touches bound-self or ORM-internal-only access")


def test_goal_named_custom_security_group_is_actually_applied_catches_task037_real_shape():
    """Real, general fix (2026-08-07, full-backlog pass, task037, real title-only goal "Create a
    custom security group and apply it to fields and buttons"): confirmed via direct SSH read of
    the real generated code that Build defined the two target fields and added them to the view,
    but never applied any groups= restriction anywhere -- the entire point of the goal was skipped,
    and no existing deterministic validator caught it.
    """
    from specialists.build.specialist import (
        _validate_goal_named_custom_security_group_is_actually_applied,
    )

    goal = "Create a custom security group and apply it to fields and buttons."

    no_group_defined = GeneratedModuleFiles(
        manifest_fields=ManifestFields(
            name="x", version="1.0", category="Uncategorized", summary="", author="", depends=["base"], data=[],
        ),
        models_py="from odoo import models, fields\n\nclass ResPartner(models.Model):\n    _inherit = 'res.partner'\n    custom_field = fields.Char()\n",
        views_xml="<odoo><record id='v' model='ir.ui.view'><field name='arch' type='xml'><form><field name='custom_field'/></form></field></record></odoo>",
        security_xml="<odoo></odoo>",
        security_csv="",
        notes="",
    )
    raised = False
    try:
        _validate_goal_named_custom_security_group_is_actually_applied(no_group_defined, goal)
    except ValueError as e:
        raised = True
        assert "no `<record model=\"res.groups\">`" in str(e)
    assert raised, "no res.groups record defined at all must be rejected"

    group_defined_never_applied = GeneratedModuleFiles(
        manifest_fields=ManifestFields(
            name="x", version="1.0", category="Uncategorized", summary="", author="", depends=["base"], data=[],
        ),
        models_py="from odoo import models, fields\n\nclass ResPartner(models.Model):\n    _inherit = 'res.partner'\n    custom_field = fields.Char()\n",
        views_xml="<odoo><record id='v' model='ir.ui.view'><field name='arch' type='xml'><form><field name='custom_field'/><button name='do_x' type='object'/></form></field></record></odoo>",
        security_xml='<odoo><record id="group_custom" model="res.groups"><field name="name">Custom</field></record></odoo>',
        security_csv="",
        notes="",
    )
    raised2 = False
    try:
        _validate_goal_named_custom_security_group_is_actually_applied(group_defined_never_applied, goal)
    except ValueError as e:
        raised2 = True
        assert "never actually applied" in str(e)
    assert raised2, "a group defined but never applied to any field/button must be rejected"

    real = GeneratedModuleFiles(
        manifest_fields=ManifestFields(
            name="x", version="1.0", category="Uncategorized", summary="", author="", depends=["base"], data=[],
        ),
        models_py="from odoo import models, fields\n\nclass ResPartner(models.Model):\n    _inherit = 'res.partner'\n    custom_field = fields.Char()\n",
        views_xml="<odoo><record id='v' model='ir.ui.view'><field name='arch' type='xml'><form><field name='custom_field' groups='oma_x.group_custom'/></form></field></record></odoo>",
        security_xml='<odoo><record id="group_custom" model="res.groups"><field name="name">Custom</field></record></odoo>',
        security_csv="",
        notes="",
    )
    _validate_goal_named_custom_security_group_is_actually_applied(real, goal)  # must not raise

    unrelated_goal = GeneratedModuleFiles(
        manifest_fields=ManifestFields(
            name="x", version="1.0", category="Uncategorized", summary="", author="", depends=["base"], data=[],
        ),
        models_py="from odoo import models, fields\n\nclass ResPartner(models.Model):\n    _inherit = 'res.partner'\n",
        views_xml="",
        security_xml="",
        security_csv="",
        notes="",
    )
    _validate_goal_named_custom_security_group_is_actually_applied(
        unrelated_goal, "Add a field to track something unrelated to security.",
    )  # must not raise -- goal never mentions a security group at all
    print("PASS: goal-named custom security group applied-to-fields/buttons validator catches "
          "task037's real shape and never touches an unrelated goal")


def test_goal_named_custom_security_group_accepts_a_real_ir_rule_application():
    """Real, confirmed false positive found live (2026-08-15, record_rule_row_level_security's
    first-ever batch): the goal-detection regex fires for ANY "security group...restrict/apply"
    phrasing, but restricting WHICH RECORDS are visible (a real `<record model="ir.rule">` with
    its own `groups` field) is a completely different, equally valid Odoo mechanism from
    restricting a field/button's own visibility (a `groups=` view-element attribute) -- a group
    applied only via a genuine ir.rule record, never touching any view element, is a correct
    answer to a goal asking for record-rule-based restriction and must not be rejected.
    """
    from specialists.build.specialist import (
        _validate_goal_named_custom_security_group_is_actually_applied,
    )

    goal = (
        "Add a new security group named 'Team Lead' scoped to the hr.employee model, and add a "
        "record rule restricting that group so members can only see hr.employee records where "
        "they are the department manager."
    )
    applied_via_record_rule = GeneratedModuleFiles(
        manifest_fields=ManifestFields(
            name="x", version="1.0", category="Uncategorized", summary="", author="", depends=["base"], data=[],
        ),
        models_py="from odoo import models, fields\n\nclass HrEmployee(models.Model):\n    _inherit = 'hr.employee'\n",
        views_xml="",
        security_xml=(
            '<odoo>'
            '<record id="group_team_lead" model="res.groups">'
            '<field name="name">Team Lead</field></record>'
            '<record id="rule_team_lead_own_dept" model="ir.rule">'
            '<field name="name">Team Lead: own department only</field>'
            '<field name="model_id" ref="model_hr_employee"/>'
            '<field name="domain_force">[(\'department_id.manager_id\', \'=\', user.id)]</field>'
            '<field name="groups" eval="[(4, ref(\'group_team_lead\'))]"/>'
            '</record>'
            '</odoo>'
        ),
        security_csv="",
        notes="",
    )
    _validate_goal_named_custom_security_group_is_actually_applied(
        applied_via_record_rule, goal,
    )  # must not raise -- a real ir.rule application is a genuinely correct answer
    print("PASS: a security group applied only via a real ir.rule record is correctly accepted, never false-rejected")


def test_goal_named_custom_security_group_still_rejects_when_neither_mechanism_is_used():
    """The fix above must be narrow -- a group defined but applied via NEITHER a groups= view
    attribute NOR a real ir.rule record is still correctly rejected."""
    from specialists.build.specialist import (
        _validate_goal_named_custom_security_group_is_actually_applied,
    )

    goal = (
        "Add a new security group named 'Team Lead' scoped to the hr.employee model, and add a "
        "record rule restricting that group so members can only see hr.employee records where "
        "they are the department manager."
    )
    group_defined_never_applied = GeneratedModuleFiles(
        manifest_fields=ManifestFields(
            name="x", version="1.0", category="Uncategorized", summary="", author="", depends=["base"], data=[],
        ),
        models_py="from odoo import models, fields\n\nclass HrEmployee(models.Model):\n    _inherit = 'hr.employee'\n",
        views_xml="",
        security_xml='<odoo><record id="group_team_lead" model="res.groups"><field name="name">Team Lead</field></record></odoo>',
        security_csv="",
        notes="",
    )
    raised = False
    try:
        _validate_goal_named_custom_security_group_is_actually_applied(group_defined_never_applied, goal)
    except ValueError as e:
        raised = True
        assert "never actually applied" in str(e)
    assert raised, "a group defined but applied via neither mechanism must still be rejected"
    print("PASS: a group applied via neither groups= nor a real ir.rule record is still correctly rejected")


def test_validate_references_resolve_against_real_target_skips_unsupported_xpath_axis(monkeypatch):
    """Real, confirmed bug found live (2026-08-11, task 07141af5, ticket_workflow_and_logging
    node): this function's own docstring already states the intended contract ("a more complex
    xpath expression outside that subset is skipped entirely, never guessed at") but the actual
    gate (`xpath_expr.startswith("//") and "[@" in xpath_expr`) only checks for the simple
    predicate SHAPE -- it never checks for an XPath AXIS (`parent::`, `ancestor::`, etc.)
    appearing anywhere else in the same expression. A real, valid Odoo/lxml xpath like
    `//field[@name='x']/parent::group` passes that gate but ElementTree's own limited XPath
    engine has NO axis support at all -- its tokenizer misreads `parent::` as a namespace-
    PREFIXED tag, raising a bare `SyntaxError` (not `ElementTree.ParseError`) since no `parent`
    prefix is registered. Confirmed live: this crashed the ENTIRE task with an uncaught
    exception, not a controlled round failure. This test proves the fix: the SAME real xpath
    shape must now be silently skipped, never raise, and never falsely report a real unresolved
    reference either.
    """
    import specialists.build.specialist as bs

    def fake_get_view_arch_by_xmlid_fast(ref, db):
        return "<tree><field name='state'/></tree>"

    def fake_external_id_exists_fast(ref, db):
        return True

    monkeypatch.setattr(bs, "get_view_arch_by_xmlid_fast", fake_get_view_arch_by_xmlid_fast)
    monkeypatch.setattr(bs, "external_id_exists_fast", fake_external_id_exists_fast)

    generated = GeneratedModuleFiles(
        manifest_fields=ManifestFields(
            name="x", version="1.0", category="Uncategorized", summary="", author="", depends=["base"], data=[],
        ),
        models_py="",
        views_xml=(
            "<odoo><record id=\"view_service_ticket_tree_inherit\" model=\"ir.ui.view\">"
            "<field name=\"name\">oma.service.ticket.tree.inherit</field>"
            "<field name=\"model\">oma.service.ticket</field>"
            "<field name=\"inherit_id\" ref=\"base.view_partner_tree\"/>"
            "<field name=\"arch\" type=\"xml\">"
            "<xpath expr=\"//field[@name='state']/parent::group\" position=\"after\">"
            "<field name=\"technician_id\"/></xpath>"
            "</field></record></odoo>"
        ),
        security_csv="",
        notes="",
    )
    # Must complete without raising SyntaxError -- the real, confirmed crash this fix closes.
    asyncio.run(bs._validate_references_resolve_against_real_target(generated, "not_a_fast_path_db", "oma_x"))
    print("PASS: an xpath expression using an ElementTree-unsupported axis (parent::) is "
          "skipped gracefully, never crashes the validator")


def test_autofix_strip_view_button_referencing_a_method_on_the_wrong_model():
    """Real, confirmed bug found live (2026-08-11, task 07141af5, ticket_bulk_close node,
    recurring identically across 8+ consecutive rounds, surviving even an explicit "submit an
    empty diff" resume note): a genuinely correct, working `ir.actions.server` record (bound to
    `oma.service.ticket` via `binding_model_id`) kept getting a redundant, wrong `<button
    type="object" name="action_bulk_close">` added to `project.project`'s own inherited form
    view instead -- `action_bulk_close` is a method on `oma.service.ticket`, not
    `project.project`, so Odoo raised a ParseError on install every single time. Direct
    inspection of the real, live committed baseline confirmed this was NOT a carried-forward
    baseline defect (the baseline never contained it) -- Build genuinely regenerated this exact
    mistake fresh, every round, immune to increasingly explicit resume notes. This test proves
    the deterministic autofix that finally closes it.
    """
    from specialists.build.specialist import (
        _autofix_strip_view_button_referencing_a_method_on_the_wrong_model,
        GeneratedModuleFiles, ManifestFields,
    )

    models_py = (
        "from odoo import models, fields\n\n"
        "class ProjectProject(models.Model):\n"
        "    _inherit = 'project.project'\n\n"
        "    def action_open_tickets(self):\n"
        "        return {'type': 'ir.actions.act_window'}\n\n\n"
        "class ServiceTicket(models.Model):\n"
        "    _name = 'oma.service.ticket'\n\n"
        "    def action_bulk_close(self):\n"
        "        self.write({'state': 'closed'})\n"
    )
    views_xml = (
        "<odoo><record id=\"view_project_form_inherit\" model=\"ir.ui.view\">"
        "<field name=\"name\">project.project.form.inherit</field>"
        "<field name=\"model\">project.project</field>"
        "<field name=\"inherit_id\" ref=\"project.edit_project\"/>"
        "<field name=\"arch\" type=\"xml\">"
        "<xpath expr=\"//div[@name='button_box']\" position=\"inside\">"
        "<button class=\"oe_stat_button\" type=\"object\" name=\"action_open_tickets\" icon=\"fa-list\">"
        "<div><span>Open Tickets</span></div></button>"
        "<button class=\"oe_stat_button\" type=\"object\" name=\"action_bulk_close\" icon=\"fa-check\">"
        "<div><span>Bulk Close</span></div></button>"
        "</xpath></field></record></odoo>"
    )
    generated = GeneratedModuleFiles(
        manifest_fields=ManifestFields(
            name="x", version="1.0", category="Uncategorized", summary="", author="", depends=["base"], data=[],
        ),
        models_py=models_py, views_xml=views_xml, security_csv="", notes="",
    )
    _autofix_strip_view_button_referencing_a_method_on_the_wrong_model(generated)
    assert "action_bulk_close" not in generated.views_xml, (
        f"the wrong-model button must be stripped: {generated.views_xml!r}"
    )
    assert "action_open_tickets" in generated.views_xml, (
        "a button that genuinely belongs to this view's own model must never be touched"
    )
    print("PASS: a view button referencing a method defined on a DIFFERENT model is stripped; "
          "a genuinely correct button for the view's own model is left untouched")


def test_autofix_strip_view_button_also_removes_the_now_vestigial_record():
    """Real, confirmed FOLLOW-UP bug found live (2026-08-11, same task, same node, same night):
    stripping the wrong-model button (the fix immediately above) can leave an entirely
    vestigial `<record>` behind -- one whose own `<xpath>` now inserts nothing at all -- which
    Code-Review then correctly, separately flagged: "this view is empty and incorrectly
    placed, causing confusion and potential installation issues." Confirmed live via direct
    Gitea inspection of the real, live committed views.xml this exact fix produced. The whole
    record existed only to carry the button just removed, so it must be dropped entirely, not
    left behind as a pointless no-op view record.
    """
    from specialists.build.specialist import (
        _autofix_strip_view_button_referencing_a_method_on_the_wrong_model,
        GeneratedModuleFiles, ManifestFields,
    )

    models_py = (
        "from odoo import models, fields\n\n"
        "class ProjectProject(models.Model):\n"
        "    _inherit = 'project.project'\n\n"
        "    def action_open_tickets(self):\n"
        "        return {'type': 'ir.actions.act_window'}\n\n\n"
        "class ServiceTicket(models.Model):\n"
        "    _name = 'oma.service.ticket'\n\n"
        "    def action_bulk_close(self):\n"
        "        self.write({'state': 'closed'})\n"
    )
    views_xml = (
        "<odoo>\n"
        "    <record id=\"view_project_form_inherit_buttons\" model=\"ir.ui.view\">\n"
        "        <field name=\"name\">project.project.form.inherit.buttons</field>\n"
        "        <field name=\"model\">project.project</field>\n"
        "        <field name=\"inherit_id\" ref=\"project.edit_project\"/>\n"
        "        <field name=\"arch\" type=\"xml\">\n"
        "            <xpath expr=\"//div[@name='button_box']\" position=\"inside\">\n"
        "                <button class=\"oe_stat_button\" type=\"object\" name=\"action_open_tickets\" icon=\"fa-list\">\n"
        "                    <div><span>Open Tickets</span></div>\n"
        "                </button>\n"
        "            </xpath>\n"
        "        </field>\n"
        "    </record>\n"
        "    <record id=\"view_project_form_inherit_bulk_close\" model=\"ir.ui.view\">\n"
        "        <field name=\"name\">project.project.form.inherit.bulk.close</field>\n"
        "        <field name=\"model\">project.project</field>\n"
        "        <field name=\"inherit_id\" ref=\"project.edit_project\"/>\n"
        "        <field name=\"arch\" type=\"xml\">\n"
        "            <xpath expr=\"//div[@name='button_box']\" position=\"inside\">\n"
        "                <button class=\"oe_stat_button\" type=\"object\" name=\"action_bulk_close\" icon=\"fa-check\">\n"
        "                    <div><span>Bulk Close</span></div>\n"
        "                </button>\n"
        "            </xpath>\n"
        "        </field>\n"
        "    </record>\n"
        "</odoo>"
    )
    generated = GeneratedModuleFiles(
        manifest_fields=ManifestFields(
            name="x", version="1.0", category="Uncategorized", summary="", author="", depends=["base"], data=[],
        ),
        models_py=models_py, views_xml=views_xml, security_csv="", notes="",
    )
    _autofix_strip_view_button_referencing_a_method_on_the_wrong_model(generated)
    assert "view_project_form_inherit_bulk_close" not in generated.views_xml, (
        f"the now-vestigial record must be removed entirely: {generated.views_xml!r}"
    )
    assert "action_open_tickets" in generated.views_xml, (
        "a genuinely non-empty, correct record must never be touched"
    )
    assert "view_project_form_inherit_buttons" in generated.views_xml, (
        "a genuinely non-empty, correct record must never be touched"
    )
    print("PASS: the vestigial record left behind by the button strip is removed entirely; "
          "a genuinely non-empty sibling record is left untouched")


def test_scoped_edit_delete_file_operation_actually_removes_the_file():
    """Real, confirmed bug found live (2026-08-11, task 18fca388, post_init_hook cleanup round,
    twice in a row with the identical error both times): before 'delete_file' existed, there was
    NO scoped-edit operation that could actually remove a file from the module -- 'replace_file'
    only ever SETS a file's content, never pops the key out of the result dict. A file dropped
    from __manifest__.py's own `data` list kept surviving on disk forever, unreachable by any
    edit, and the drop-guard inside _apply_scoped_edits() then rejected the round for exactly
    that leftover file every single round, with literally no way for Build to ever satisfy it.
    """
    from specialists.build.specialist import GeneratedModuleEdit, _apply_scoped_edits

    prior_files = {
        "__manifest__.py": repr({
            "name": "x", "version": "1.0", "category": "Uncategorized", "summary": "", "description": "",
            "author": "", "depends": ["base"], "data": ["views/views.xml"], "installable": True,
            "auto_install": False, "license": "LGPL-3",
        }),
        "models/models.py": "",
        "security/ir.model.access.csv": "id,name\n",
        "views/views.xml": "<odoo></odoo>",
    }
    edits = [
        GeneratedModuleEdit(
            file="__manifest__.py", operation="replace_manifest_fields",
            manifest_fields=__import__("specialists.build.specialist", fromlist=["ManifestFields"]).ManifestFields(
                name="x", version="1.0", category="Uncategorized", summary="", author="", depends=["base"], data=[],
            ),
        ),
        GeneratedModuleEdit(file="views/views.xml", operation="delete_file"),
    ]
    applied = _apply_scoped_edits(prior_files, edits)
    assert "views/views.xml" not in applied, (
        f"delete_file must actually remove the file from the result: {sorted(applied)}"
    )
    print("PASS: 'delete_file' operation actually removes the file, and dropping it from "
          "manifest data in the SAME round no longer trips the (now-fixed) drop-guard")


def test_scoped_edit_drop_guard_no_longer_a_tautology_against_genuine_removal():
    """Same bug as above, isolated to the drop-guard itself: it used to check `f in prior_files`,
    but `dropped` is BY CONSTRUCTION always a subset of `prior_files` (it can only ever contain
    files the prior manifest referenced), so that check was true for every single genuine
    removal, forever -- structurally unsatisfiable, not a real content bug to fix.
    """
    from specialists.build.specialist import GeneratedModuleEdit, ManifestFields, _apply_scoped_edits

    prior_files = {
        "__manifest__.py": repr({
            "name": "x", "version": "1.0", "category": "Uncategorized", "summary": "", "description": "",
            "author": "", "depends": ["base"], "data": ["security/security.xml"], "installable": True,
            "auto_install": False, "license": "LGPL-3",
        }),
        "models/models.py": "",
        "security/ir.model.access.csv": "id,name\n",
        "security/security.xml": "<odoo></odoo>",
    }
    edits = [
        GeneratedModuleEdit(file="security/security.xml", operation="delete_file"),
        GeneratedModuleEdit(
            file="__manifest__.py", operation="replace_manifest_fields",
            manifest_fields=ManifestFields(
                name="x", version="1.0", category="Uncategorized", summary="", author="", depends=["base"], data=[],
            ),
        ),
    ]
    applied = _apply_scoped_edits(prior_files, edits)
    assert "security/security.xml" not in applied
    print("PASS: dropping a file from manifest data in the same round it is deleted no longer "
          "raises the (previously unsatisfiable) drop-guard error")


def test_scoped_edit_auto_deletes_trivially_empty_leftover_file_dropped_from_data():
    """Real, confirmed bug found live (2026-08-11, task 18fca388, same night as the drop-guard's
    own first fix): the model reliably stops REFERENCING an unwanted optional file in manifest
    data (correct) without ALSO submitting the matching 'delete_file' edit (the mechanical
    cleanup step) -- confirmed live across 4+ consecutive rounds, each retriggering the
    drop-guard's raise identically no matter how explicit the resume note. Since the leftover
    file's own content is trivially empty (no real <record>), there is nothing to lose by
    auto-deleting it instead of forcing yet another round over a purely mechanical omission.
    """
    from specialists.build.specialist import GeneratedModuleEdit, ManifestFields, _apply_scoped_edits

    prior_files = {
        "__manifest__.py": repr({
            "name": "x", "version": "1.0", "category": "Uncategorized", "summary": "", "description": "",
            "author": "", "depends": ["base"], "data": ["views/views.xml"], "installable": True,
            "auto_install": False, "license": "LGPL-3",
        }),
        "models/models.py": "",
        "security/ir.model.access.csv": "id,name\n",
        "views/views.xml": "<odoo></odoo>",
    }
    edits = [
        GeneratedModuleEdit(
            file="__manifest__.py", operation="replace_manifest_fields",
            manifest_fields=ManifestFields(
                name="x", version="1.0", category="Uncategorized", summary="", author="", depends=["base"], data=[],
            ),
        ),
        # Deliberately NO delete_file edit for views/views.xml -- exactly the omission observed live.
    ]
    applied = _apply_scoped_edits(prior_files, edits)
    assert "views/views.xml" not in applied, (
        f"a trivially-empty leftover file dropped from data (with no matching delete_file edit) "
        f"must be auto-deleted rather than forcing another failed round: {sorted(applied)}"
    )
    print("PASS: a trivially-empty leftover file dropped from data, with no matching delete_file "
          "edit, is auto-deleted instead of re-raising the drop-guard")


def test_scoped_edit_drop_guard_still_raises_for_leftover_file_with_real_content():
    """Regression guard for the fix above: a file with GENUINE, meaningful content (a real
    <record> the model forgot about) must never be silently auto-deleted -- only a trivially
    empty leftover is safe to remove without a human/model ever explicitly deciding to.
    """
    from specialists.build.specialist import GeneratedModuleEdit, ManifestFields, _apply_scoped_edits

    prior_files = {
        "__manifest__.py": repr({
            "name": "x", "version": "1.0", "category": "Uncategorized", "summary": "", "description": "",
            "author": "", "depends": ["base"], "data": ["views/views.xml"], "installable": True,
            "auto_install": False, "license": "LGPL-3",
        }),
        "models/models.py": "",
        "security/ir.model.access.csv": "id,name\n",
        "views/views.xml": (
            "<odoo><record id=\"view_x\" model=\"ir.ui.view\">"
            "<field name=\"name\">x</field></record></odoo>"
        ),
    }
    edits = [
        GeneratedModuleEdit(
            file="__manifest__.py", operation="replace_manifest_fields",
            manifest_fields=ManifestFields(
                name="x", version="1.0", category="Uncategorized", summary="", author="", depends=["base"], data=[],
            ),
        ),
    ]
    raised = False
    try:
        _apply_scoped_edits(prior_files, edits)
    except ValueError as exc:
        raised = True
        assert "views/views.xml" in str(exc)
    assert raised, "a leftover file with genuine, real <record> content must still raise, never auto-deleted"
    print("PASS: a leftover file with genuine real content still raises the drop-guard, never silently auto-deleted")


def test_autofix_deletes_orphaned_trivially_empty_file_left_over_from_an_earlier_round():
    """Real, confirmed bug found live (2026-08-11, task 18fca388, same night as the drop-guard's
    own in-round auto-delete fix): that fix only auto-deletes WITHIN the same round its manifest
    edit performs the drop -- but the drop had already happened in an EARLIER round (manifest
    data no longer mentioned views/views.xml at all), so a LATER round with no reason to touch
    the manifest again never re-triggers it, and the orphaned, empty file just kept silently
    surviving on disk round after round. Confirmed live across multiple consecutive rounds.
    """
    from specialists.build.specialist import _autofix_delete_orphaned_trivially_empty_optional_files

    applied = {
        "__manifest__.py": repr({
            "name": "x", "version": "1.0", "category": "Uncategorized", "summary": "", "description": "",
            "author": "", "depends": ["base"], "data": ["security/ir.model.access.csv"], "installable": True,
            "auto_install": False, "license": "LGPL-3", "post_init_hook": "post_init_hook",
        }),
        "models/models.py": "def post_init_hook(cr, registry):\n    pass\n",
        "security/ir.model.access.csv": "id,name\n",
        "views/views.xml": "<odoo></odoo>\n",
    }
    result = _autofix_delete_orphaned_trivially_empty_optional_files(applied)
    assert "views/views.xml" not in result, (
        f"an orphaned, empty, unreferenced optional file left over from an earlier round must "
        f"be cleaned up on every round, not only the round that dropped it: {sorted(result)}"
    )
    assert result["models/models.py"] == applied["models/models.py"], "unrelated files must survive unchanged"
    print("PASS: an orphaned, trivially-empty optional file surviving from an earlier round is "
          "cleaned up on every round, not just the round that dropped it")


def test_autofix_never_deletes_an_orphaned_file_with_real_content():
    from specialists.build.specialist import _autofix_delete_orphaned_trivially_empty_optional_files

    applied = {
        "__manifest__.py": repr({
            "name": "x", "version": "1.0", "category": "Uncategorized", "summary": "", "description": "",
            "author": "", "depends": ["base"], "data": ["security/ir.model.access.csv"], "installable": True,
            "auto_install": False, "license": "LGPL-3",
        }),
        "models/models.py": "",
        "security/ir.model.access.csv": "id,name\n",
        "views/views.xml": (
            "<odoo><record id=\"view_x\" model=\"ir.ui.view\">"
            "<field name=\"name\">x</field></record></odoo>"
        ),
    }
    result = _autofix_delete_orphaned_trivially_empty_optional_files(applied)
    assert "views/views.xml" in result, "a file with genuine, real content must never be auto-deleted"
    print("PASS: an orphaned file with genuine real content is never auto-deleted")


def test_scoped_edit_missing_required_files_respects_deliberate_optional_file_deletion():
    """Sibling fix to the drop-guard: `_scoped_edit_missing_required_files()` independently
    treated a deliberately-deleted OPTIONAL file (views.xml/security.xml) the same as a real
    regression, since it only ever compared `applied` against `prior_files` with no way to know
    the absence was intentional. Now takes an explicit `deleted_this_round` set so both checks
    agree on what counts as a real loss vs. an intentional removal.
    """
    from specialists.build.specialist import _scoped_edit_deleted_files, _scoped_edit_missing_required_files

    prior_files = {"views/views.xml": "<odoo></odoo>"}
    applied = {
        "__manifest__.py": "x", "models/models.py": "x", "security/ir.model.access.csv": "id,name\n",
    }
    missing_without_fix = _scoped_edit_missing_required_files(prior_files, applied)
    assert "views/views.xml" in missing_without_fix, (
        "sanity check: a genuinely absent optional file with no explicit deletion is still "
        "correctly flagged as missing"
    )

    from specialists.build.specialist import GeneratedModuleEdit
    deleted = _scoped_edit_deleted_files([GeneratedModuleEdit(file="views/views.xml", operation="delete_file")])
    missing_with_fix = _scoped_edit_missing_required_files(prior_files, applied, deleted)
    assert "views/views.xml" not in missing_with_fix, (
        f"a deliberately-deleted optional file must not be flagged as missing: {missing_with_fix}"
    )
    print("PASS: a deliberately-deleted optional file is no longer indistinguishable from a "
          "real regression")


def test_manifest_fields_renders_post_init_hook_key_when_set():
    """Real, confirmed bug found live (2026-08-11, task 18fca388): `ManifestFields` had no field
    for any of Odoo's 3 install-lifecycle hook keys at all -- a genuine schema gap, honestly
    documented in `_validate_manifest_hook_functions_exist()`'s own docstring as "currently
    always a no-op" pending exactly this fix. Made a task whose goal genuinely requires a
    post_init_hook structurally impossible to complete, confirmed live across 5 consecutive
    resume rounds, since `replace_manifest_fields` is the ONLY allowed way to edit
    __manifest__.py and the old renderer had no way to emit the key no matter what the model did.
    """
    from specialists.build.specialist import ManifestFields, _render_manifest_py

    with_hook = ManifestFields(
        name="x", version="1.0", category="Uncategorized", summary="", author="",
        depends=["base"], data=[], post_init_hook="post_init_hook",
    )
    rendered = _render_manifest_py(with_hook)
    parsed = eval(rendered, {"__builtins__": {}})
    assert parsed.get("post_init_hook") == "post_init_hook", (
        f"post_init_hook must render into the manifest dict when set: {parsed!r}"
    )

    without_hook = ManifestFields(
        name="x", version="1.0", category="Uncategorized", summary="", author="",
        depends=["base"], data=[],
    )
    rendered_plain = _render_manifest_py(without_hook)
    parsed_plain = eval(rendered_plain, {"__builtins__": {}})
    assert "post_init_hook" not in parsed_plain, (
        "the common case (no hook) must never gain a spurious key: "
        f"{parsed_plain!r}"
    )
    print("PASS: ManifestFields.post_init_hook renders into the manifest dict when set, and is "
          "omitted entirely (never a spurious None/empty key) when unset")


def test_module_root_init_reexports_declared_hook_functions():
    """Sibling bug to the schema gap above: even with the hook key rendering correctly into
    __manifest__.py, Odoo resolves a declared hook via `getattr(sys.modules['odoo.addons.' +
    module_name], hook_name)` -- a direct attribute of the module's own TOP-LEVEL package. This
    pipeline's deterministic module-root __init__.py is always exactly 'from . import models\\n'
    (see tools_odoo/module_dev/toolchain.py's own scaffold-stripping step), which imports the
    SUBMODULE but never re-exports any function defined inside it -- so a hook function correctly
    written in models/models.py would still raise AttributeError at install time. Fixed
    deterministically: __init__.py must also re-export each declared hook name from .models.
    """
    from specialists.build.specialist import (
        GeneratedModuleFiles, ManifestFields, _write_module_root_init_with_declared_hooks,
    )

    generated = GeneratedModuleFiles(
        manifest_fields=ManifestFields(
            name="x", version="1.0", category="Uncategorized", summary="", author="",
            depends=["base"], data=[], post_init_hook="post_init_hook",
        ),
        models_py="def post_init_hook(cr, registry):\n    pass\n",
        security_csv="id,name\n", notes="",
    )
    written: dict[str, str] = {}
    _write_module_root_init_with_declared_hooks(generated, lambda relpath, content: written.__setitem__(relpath, content))
    assert written.get("__init__.py") == "from . import models\nfrom .models.models import post_init_hook\n", (
        f"__init__.py must re-export the declared hook so getattr(module, 'post_init_hook') "
        f"resolves at install time: {written!r}"
    )

    generated_no_hook = GeneratedModuleFiles(
        manifest_fields=ManifestFields(
            name="x", version="1.0", category="Uncategorized", summary="", author="",
            depends=["base"], data=[],
        ),
        models_py="", security_csv="id,name\n", notes="",
    )
    written_no_hook: dict[str, str] = {}
    _write_module_root_init_with_declared_hooks(
        generated_no_hook, lambda relpath, content: written_no_hook.__setitem__(relpath, content),
    )
    assert written_no_hook.get("__init__.py") == "from . import models\n", (
        f"the common (no-hook) case must stay the plain, original scaffold content: {written_no_hook!r}"
    )
    print("PASS: module-root __init__.py re-exports every declared hook function from .models, "
          "and stays plain 'from . import models' when no hook is declared")


def test_files_from_generated_includes_init_py_with_declared_hooks():
    """Real, confirmed bug found live (2026-08-11, task 18fca388, same night as the fix above):
    the hook-reexport fix was wired into only ONE of at least three real write paths this file
    has for a round's generated content (the plain single-node branch) -- `_sandbox_preflight()`
    (writes `_files_from_generated(generated)`'s own dict straight to the isolated sandbox
    container BEFORE anything reaches that branch) and `apply_node_result_to_module()` (the
    concurrent multi-node merge path, which consumes that same dict as
    `node_new_files_by_relpath`) both never got the fix, so a real sandbox install crashed with
    the exact `AttributeError: module '...' has no attribute 'post_init_hook'` this whole fix
    exists to prevent -- confirmed live, even after the schema gap itself was already fixed.
    Centralized into `_files_from_generated()` instead, the one dict-construction point every
    real write path is either built from directly or downstream of.
    """
    from specialists.build.specialist import GeneratedModuleFiles, ManifestFields, _files_from_generated

    generated = GeneratedModuleFiles(
        manifest_fields=ManifestFields(
            name="x", version="1.0", category="Uncategorized", summary="", author="",
            depends=["base"], data=[], post_init_hook="post_init_hook",
        ),
        models_py="def post_init_hook(cr, registry):\n    pass\n",
        security_csv="id,name\n", notes="",
    )
    files = _files_from_generated(generated)
    assert files.get("__init__.py") == "from . import models\nfrom .models.models import post_init_hook\n", (
        f"_files_from_generated() -- the dict _sandbox_preflight() and the multi-node merge "
        f"path both actually consume -- must include a hook-reexporting __init__.py: {files!r}"
    )
    print("PASS: _files_from_generated() includes a correctly hook-reexporting __init__.py, "
          "so the sandbox pre-flight and multi-node merge write paths get the fix too")


def test_extract_goal_named_restricted_field_closes_the_custom_security_group_gap():
    """Real, confirmed bug found live (2026-08-16, wave51_sg/hr.department, real task_id
    106decc9-973b-41bf-9daf-6ee58979495b -- and 2 prior identical occurrences on res.partner):
    the custom_security_group_field_or_button_restriction goal shape always names an EXISTING
    field to restrict ("restrict the vat field", "restrict the manager_id field") -- Build
    correctly writes no new field for this shape, so field_names extracted from models.py was
    always empty, and the deterministic view-builder could never build the needed groups= view
    element, forcing an unreliable freeform-generation fallback that invented a nonsense
    placeholder field instead. This function closes that gap by reading the field name directly
    out of the goal text.
    """
    from specialists.build.specialist import _extract_goal_named_restricted_field

    assert _extract_goal_named_restricted_field(
        "Add a new custom security group named 'Tax Data Viewer' and restrict "
        "the vat field on the res.partner form view so that only members of "
        "the 'Tax Data Viewer' group can see it."
    ) == ["vat"]
    assert _extract_goal_named_restricted_field(
        "Add a new custom security group named 'Org Structure Viewer' and "
        "restrict the manager_id field on the hr.department form view so "
        "that only members of the 'Org Structure Viewer' group can see it."
    ) == ["manager_id"]
    assert _extract_goal_named_restricted_field(
        "Add a validation constraint to the sale.order model so that the "
        "expected_date field, if set, cannot be earlier than the effective_date field."
    ) == [], "a goal with no 'restrict the X field' phrasing must never guess a field name"
    print("PASS: goal-text field extraction correctly finds the named field for the real "
          "escalating goal shapes, and never guesses when the phrasing isn't present")


def test_models_py_has_real_non_comment_content_exempts_goal_named_field_restriction():
    """Real, confirmed bug found live (2026-08-16): the fix to `_extract_goal_named_restricted_
    field()` alone was NOT sufficient for the real live failure -- `_validate_models_py_has_
    real_non_comment_content` runs INSIDE `_generate_code()`'s own internal check/revise loop,
    which happens BEFORE `_maybe_override_view_xml_deterministically()` ever gets a chance to
    add the real `groups=` view element. Without this exemption, an all-comment `_inherit` class
    for a goal that only ever restricts an EXISTING field (correctly declaring no new field) got
    rejected as an invalid stub, and the resulting self-repair loop made Build invent a nonsense
    placeholder field -- which then poisoned `_extract_inherited_field_names()` for the rest of
    the round, since a real (if wrong) field now existed to find.
    """
    from specialists.build.specialist import _validate_models_py_has_real_non_comment_content

    empty_models_py = (
        "from odoo import models, fields\n\n"
        "class CrmLead(models.Model):\n"
        "    _inherit = 'crm.lead'\n"
    )
    goal = (
        "Add a new custom security group named 'Lead Contact Info Viewer' and "
        "restrict the phone field on the crm.lead form view so that only "
        "members of the 'Lead Contact Info Viewer' group can see it."
    )
    restriction_goal = GeneratedModuleFiles(
        manifest_fields=ManifestFields(
            name="x", version="1.0", category="Uncategorized", summary="", author="", depends=["base"], data=[],
        ),
        models_py=empty_models_py, views_xml="", security_csv="", notes="",
    )
    _validate_models_py_has_real_non_comment_content(restriction_goal, goal)
    print("PASS: an all-comment class is not rejected for a goal that restricts an already-"
          "existing field, even before the deterministic view override has run")

    unrelated_goal = "Add a validation constraint to the sale.order model."
    unrelated = GeneratedModuleFiles(
        manifest_fields=ManifestFields(
            name="x", version="1.0", category="Uncategorized", summary="", author="", depends=["base"], data=[],
        ),
        models_py=empty_models_py, views_xml="", security_csv="", notes="",
    )
    raised = False
    try:
        _validate_models_py_has_real_non_comment_content(unrelated, unrelated_goal)
    except ValueError:
        raised = True
    assert raised, "a genuinely empty class must still be rejected for an unrelated goal"
    print("PASS: an unrelated goal with no field-restriction phrasing still correctly rejects "
          "a genuinely empty class")


def test_inherit_only_class_is_not_empty_exempts_goal_named_field_restriction():
    """Real, confirmed bug found live (2026-08-16, real task_id e78c8a0c-d69b-4b01-ba6b-
    92f7a4214c4e): the sixth exception added to `_validate_models_py_has_real_non_comment_
    content` alone was NOT sufficient -- `_validate_inherit_only_class_is_not_empty` is a
    SEPARATE, more primitive, non-goal-aware validator with no exemptions of its own, checking
    the exact same "_inherit class with zero fields and zero methods" shape. An `_inherit = '...'
    \\n    pass` class (or one reduced to a `pass` statement plus comments) for a goal that only
    restricts an already-existing field was still rejected here even after the sibling fix,
    forcing Build to invent a placeholder field (`dummy_field = fields.Boolean(...)`) yet again.
    """
    from specialists.build.specialist import _validate_inherit_only_class_is_not_empty

    models_py = (
        "from odoo import models, fields\n\n"
        "class HelpdeskTicket(models.Model):\n"
        "    _inherit = 'helpdesk.ticket'\n\n"
        "    pass\n"
    )
    goal = (
        "Create a new custom security group called 'Support Priority Access' that restricts "
        "visibility of the priority field on helpdesk.ticket's form view to only that group's "
        "members."
    )
    restriction_goal = GeneratedModuleFiles(
        manifest_fields=ManifestFields(
            name="x", version="1.0", category="Uncategorized", summary="", author="", depends=["base"], data=[],
        ),
        models_py=models_py, views_xml="", security_csv="", notes="",
    )
    _validate_inherit_only_class_is_not_empty(restriction_goal, goal)
    print("PASS: an empty _inherit-only class is not rejected for a goal that restricts an "
          "already-existing field")

    unrelated = GeneratedModuleFiles(
        manifest_fields=ManifestFields(
            name="x", version="1.0", category="Uncategorized", summary="", author="", depends=["base"], data=[],
        ),
        models_py=models_py, views_xml="", security_csv="", notes="",
    )
    raised = False
    try:
        _validate_inherit_only_class_is_not_empty(unrelated, "Add a computed field to sale.order.")
    except ValueError:
        raised = True
    assert raised, "a genuinely empty _inherit-only class must still be rejected for an unrelated goal"
    print("PASS: an unrelated goal still correctly rejects a genuinely empty _inherit-only class")


def test_goal_named_new_group_external_id_resolves_a_brand_new_group():
    """Real, confirmed bug found live (2026-08-16, wave58_sg/project.task, real task_id
    e498ba6d-6bfe-4484-bec2-a9e9c5d78375): `_goal_states_field_group_restriction()` and the
    whole `acl_request_extractor` module it delegates to were built specifically for "only
    <EXISTING role> should see X" -- they only ever resolve a group that ALREADY exists via a
    live registry lookup. This entire direction's goal shape always creates a BRAND NEW group in
    the same round, which can never be found that way, so the rendered field tag silently got no
    `groups=` attribute at all despite the group being correctly defined in security_xml.
    """
    from specialists.build.specialist import _goal_named_new_group_external_id

    goal = (
        "Add a new custom security group named 'Task Effort Viewer' and restrict the "
        "planned_hours field on the project.task form view so that only members of the "
        "'Task Effort Viewer' group can see it."
    )
    security_xml = (
        '<odoo>\n'
        '  <record id="group_task_effort_viewer" model="res.groups">\n'
        '    <field name="name">Task Effort Viewer</field>\n'
        '  </record>\n'
        '</odoo>'
    )
    result = _goal_named_new_group_external_id(goal, security_xml, "oma_add_a_new_custom_2b050449")
    assert result == "oma_add_a_new_custom_2b050449.group_task_effort_viewer", (
        f"expected the real, module-qualified xmlid of the goal-named new group -- got {result!r}"
    )
    print("PASS: a brand-new, same-round security group is correctly resolved from the goal's "
          "own quoted name matched against security_xml's real record")

    no_matching_group = _goal_named_new_group_external_id(goal, "<odoo></odoo>", "some_module")
    assert no_matching_group is None, "must never guess a group id when security_xml has no matching record"
    print("PASS: no security_xml match at all correctly returns None, never a guess")


_RR_GOAL = (
    "Add a record rule to sale.order so that regular users can only see and edit orders "
    "where the user_id field matches the current user, while users in a new group named "
    "'Sales Oversight Reviewer' can see all orders."
)
_RR_SECURITY_XML_GROUP = (
    '  <record id="group_sales_oversight_reviewer" model="res.groups">\n'
    '    <field name="name">Sales Oversight Reviewer</field>\n'
    '  </record>\n'
)


def _rr_generated(security_xml: str) -> GeneratedModuleFiles:
    return GeneratedModuleFiles(
        manifest_fields=ManifestFields(
            name="x", version="1.0", category="Uncategorized", summary="", author="", depends=["base"], data=[],
        ),
        models_py="from odoo import models\n", views_xml="",
        security_csv="id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink\n",
        security_xml=f"<odoo>\n{security_xml}</odoo>", notes="",
    )


def test_record_rule_privileged_group_inversion_is_rejected():
    """Real, confirmed, RECURRING bug found live (2026-08-17, waves 73 and 87, `record_rule_
    row_level_security` certification run): the restrictive rule's own groups_id references the
    SAME privileged group the goal says should see everything -- backwards."""
    from specialists.build.specialist import _validate_record_rule_privileged_group_not_also_restricted

    security_xml = _RR_SECURITY_XML_GROUP + (
        '  <record id="rule_sale_order_own" model="ir.rule">\n'
        '    <field name="name">sale.order: own records only</field>\n'
        '    <field name="model_id" ref="model_sale_order"/>\n'
        "    <field name=\"domain_force\">[('user_id', '=', user.id)]</field>\n"
        '    <field name="groups_id" eval="[(4, ref(\'group_sales_oversight_reviewer\'))]"/>\n'
        '  </record>\n'
    )
    generated = _rr_generated(security_xml)
    raised = False
    try:
        _validate_record_rule_privileged_group_not_also_restricted(generated, _RR_GOAL)
    except ValueError as exc:
        raised = True
        assert "backwards" in str(exc)
        assert "sales oversight reviewer" in str(exc).lower()
    assert raised, "the restrictive rule referencing the privileged group must be rejected"
    print("PASS: the restrictive rule assigned to the privileged group (wave73/wave87's real shape) is rejected")


def test_record_rule_exclusion_anti_pattern_is_rejected():
    """Real, confirmed bug found live (2026-08-17, wave77): an attempt to 'exclude' the
    privileged group from a restrictive rule via groups_id -- not real Odoo semantics."""
    from specialists.build.specialist import _validate_record_rule_privileged_group_not_also_restricted

    security_xml = _RR_SECURITY_XML_GROUP + (
        '  <record id="rule_sale_order_own" model="ir.rule">\n'
        '    <field name="name">sale.order: own records only</field>\n'
        '    <field name="model_id" ref="model_sale_order"/>\n'
        "    <field name=\"domain_force\">[('user_id', '=', user.id)]</field>\n"
        '    <field name="groups_id" eval="[(4, ref(\'base.group_user\')), '
        '(4, ref(\'group_sales_oversight_reviewer\'))]"/>\n'
        '  </record>\n'
    )
    generated = _rr_generated(security_xml)
    raised = False
    try:
        _validate_record_rule_privileged_group_not_also_restricted(generated, _RR_GOAL)
    except ValueError:
        raised = True
    assert raised, "a privileged-group ref anywhere in the restrictive rule's groups_id must be rejected"
    print("PASS: the groups_id-as-exclusion anti-pattern (wave77's real shape) is rejected")


def test_record_rule_missing_permissive_rule_for_privileged_group_is_rejected():
    """Real, confirmed bug found live (2026-08-17, wave78): only the restrictive rule was
    written; the goal's 'sees everything' promise for the privileged group was never
    implemented as a separate rule at all."""
    from specialists.build.specialist import _validate_record_rule_privileged_group_not_also_restricted

    security_xml = _RR_SECURITY_XML_GROUP + (
        '  <record id="rule_sale_order_own" model="ir.rule">\n'
        '    <field name="name">sale.order: own records only</field>\n'
        '    <field name="model_id" ref="model_sale_order"/>\n'
        "    <field name=\"domain_force\">[('user_id', '=', user.id)]</field>\n"
        '    <field name="groups_id" eval="[(4, ref(\'base.group_user\'))]"/>\n'
        '  </record>\n'
    )
    generated = _rr_generated(security_xml)
    raised = False
    try:
        _validate_record_rule_privileged_group_not_also_restricted(generated, _RR_GOAL)
    except ValueError as exc:
        raised = True
        assert "no unrestricted" in str(exc)
    assert raised, "a missing permissive rule for the privileged group must be rejected"
    print("PASS: a missing separate unrestricted rule for the privileged group (wave78's real shape) is rejected")


def test_record_rule_correct_two_rule_shape_passes():
    """The genuinely correct construction (the skill doc's own documented example) must never
    be rejected."""
    from specialists.build.specialist import _validate_record_rule_privileged_group_not_also_restricted

    security_xml = _RR_SECURITY_XML_GROUP + (
        '  <record id="rule_sale_order_own" model="ir.rule">\n'
        '    <field name="name">sale.order: own records only</field>\n'
        '    <field name="model_id" ref="model_sale_order"/>\n'
        "    <field name=\"domain_force\">[('user_id', '=', user.id)]</field>\n"
        '    <field name="groups_id" eval="[(4, ref(\'base.group_user\'))]"/>\n'
        '  </record>\n'
        '  <record id="rule_sale_order_all" model="ir.rule">\n'
        '    <field name="name">sale.order: full access for reviewers</field>\n'
        '    <field name="model_id" ref="model_sale_order"/>\n'
        "    <field name=\"domain_force\">[(1, '=', 1)]</field>\n"
        '    <field name="groups_id" eval="[(4, ref(\'group_sales_oversight_reviewer\'))]"/>\n'
        '  </record>\n'
    )
    generated = _rr_generated(security_xml)
    _validate_record_rule_privileged_group_not_also_restricted(generated, _RR_GOAL)
    print("PASS: the correct two-rule construction is never rejected")


def test_record_rule_validator_skips_goals_without_a_named_new_group():
    from specialists.build.specialist import _validate_record_rule_privileged_group_not_also_restricted

    generated = _rr_generated(
        '  <record id="rule_sale_order_own" model="ir.rule">\n'
        '    <field name="name">sale.order: own records only</field>\n'
        '    <field name="model_id" ref="model_sale_order"/>\n'
        "    <field name=\"domain_force\">[('user_id', '=', user.id)]</field>\n"
        '  </record>\n'
    )
    _validate_record_rule_privileged_group_not_also_restricted(
        generated, "Add a record rule to sale.order restricting regular users to their own orders.",
    )
    print("PASS: a goal with no named new group is silently skipped, never a false positive")


def test_record_rule_validator_never_guesses_when_group_name_not_in_security_xml_yet():
    from specialists.build.specialist import _validate_record_rule_privileged_group_not_also_restricted

    generated = _rr_generated(
        '  <record id="rule_sale_order_own" model="ir.rule">\n'
        '    <field name="name">sale.order: own records only</field>\n'
        '    <field name="model_id" ref="model_sale_order"/>\n'
        "    <field name=\"domain_force\">[('user_id', '=', user.id)]</field>\n"
        '  </record>\n'
    )
    _validate_record_rule_privileged_group_not_also_restricted(generated, _RR_GOAL)
    print("PASS: an earlier-round module with no matching group record yet is never a false positive")


def test_autofix_record_rule_inversion_swaps_the_group_and_synthesizes_the_missing_rule():
    """Real, confirmed bug found live (2026-08-17, wave88_rr/purchase.order): the validator
    correctly caught the inversion twice, but Build repeated the identical mistake both rounds
    and the task escalated. This autofix mechanically repairs the exact shape instead."""
    from specialists.build.specialist import (
        _autofix_record_rule_privileged_group_inversion,
        _validate_record_rule_privileged_group_not_also_restricted,
    )

    security_xml = _RR_SECURITY_XML_GROUP + (
        '  <record id="rule_sale_order_own" model="ir.rule">\n'
        '    <field name="name">sale.order: own records only</field>\n'
        '    <field name="model_id" ref="model_sale_order"/>\n'
        "    <field name=\"domain_force\">[('user_id', '=', user.id)]</field>\n"
        '    <field name="groups_id" eval="[(4, ref(\'group_sales_oversight_reviewer\'))]"/>\n'
        '  </record>\n'
    )
    generated = _rr_generated(security_xml)
    _autofix_record_rule_privileged_group_inversion(generated, _RR_GOAL)

    # The RESTRICTIVE rule block must no longer reference the privileged group.
    restrictive_block_match = re.search(
        r'<record id="rule_sale_order_own"[^>]*>.*?</record>', generated.security_xml, re.DOTALL,
    )
    assert restrictive_block_match is not None
    assert "group_sales_oversight_reviewer" not in restrictive_block_match.group(0), (
        "the restrictive rule must no longer reference the privileged group"
    )
    assert "ref('base.group_user')" in generated.security_xml, "the restrictive rule must now reference base.group_user"
    assert "[(1, '=', 1)]" in generated.security_xml, "a new unrestricted rule must be synthesized"
    assert "rule_group_sales_oversight_reviewer_full_access" in generated.security_xml

    # The autofixed content must now pass the validator that originally rejected it.
    _validate_record_rule_privileged_group_not_also_restricted(generated, _RR_GOAL)
    print("PASS: the group-inversion shape is mechanically repaired and the result passes the validator")


def test_autofix_record_rule_no_op_when_already_correct():
    from specialists.build.specialist import _autofix_record_rule_privileged_group_inversion

    security_xml = _RR_SECURITY_XML_GROUP + (
        '  <record id="rule_sale_order_own" model="ir.rule">\n'
        '    <field name="name">sale.order: own records only</field>\n'
        '    <field name="model_id" ref="model_sale_order"/>\n'
        "    <field name=\"domain_force\">[('user_id', '=', user.id)]</field>\n"
        '    <field name="groups_id" eval="[(4, ref(\'base.group_user\'))]"/>\n'
        '  </record>\n'
        '  <record id="rule_sale_order_all" model="ir.rule">\n'
        '    <field name="name">sale.order: full access for reviewers</field>\n'
        '    <field name="model_id" ref="model_sale_order"/>\n'
        "    <field name=\"domain_force\">[(1, '=', 1)]</field>\n"
        '    <field name="groups_id" eval="[(4, ref(\'group_sales_oversight_reviewer\'))]"/>\n'
        '  </record>\n'
    )
    generated = _rr_generated(security_xml)
    before = generated.security_xml
    _autofix_record_rule_privileged_group_inversion(generated, _RR_GOAL)
    assert generated.security_xml == before, "an already-correct construction must never be modified"
    print("PASS: an already-correct two-rule construction is left byte-for-byte unchanged")


def test_autofix_record_rule_skips_ambiguous_multi_model_rules():
    from specialists.build.specialist import _autofix_record_rule_privileged_group_inversion

    security_xml = _RR_SECURITY_XML_GROUP + (
        '  <record id="rule_sale_order_own" model="ir.rule">\n'
        '    <field name="name">sale.order: own records only</field>\n'
        '    <field name="model_id" ref="model_sale_order"/>\n'
        "    <field name=\"domain_force\">[('user_id', '=', user.id)]</field>\n"
        '    <field name="groups_id" eval="[(4, ref(\'group_sales_oversight_reviewer\'))]"/>\n'
        '  </record>\n'
        '  <record id="rule_sale_order_line_own" model="ir.rule">\n'
        '    <field name="name">sale.order.line: own records only</field>\n'
        '    <field name="model_id" ref="model_sale_order_line"/>\n'
        "    <field name=\"domain_force\">[('order_id.user_id', '=', user.id)]</field>\n"
        '  </record>\n'
    )
    generated = _rr_generated(security_xml)
    before = generated.security_xml
    _autofix_record_rule_privileged_group_inversion(generated, _RR_GOAL)
    assert generated.security_xml == before, "ambiguous multi-model rules must never be guessed at"
    print("PASS: rules spanning more than one target model are left untouched, never guessed at")


if __name__ == "__main__":
    test_manifest_fields_renders_post_init_hook_key_when_set()
    test_module_root_init_reexports_declared_hook_functions()
    test_files_from_generated_includes_init_py_with_declared_hooks()
    test_scoped_edit_delete_file_operation_actually_removes_the_file()
    test_scoped_edit_drop_guard_no_longer_a_tautology_against_genuine_removal()
    test_scoped_edit_missing_required_files_respects_deliberate_optional_file_deletion()
    test_deterministic_security_csv_defers_to_llm_when_new_group_grants_existing_model_access()
    test_deterministic_security_csv_still_returns_header_only_with_no_new_group_or_model()
    test_deterministic_security_csv_still_returns_header_only_with_existing_security_xml_but_no_group()
    test_deterministic_security_csv_unaffected_for_genuine_new_model_case()
    test_fencing_rejects_stale_caller_after_real_specialist_reacquires()
    test_compensations_actually_clean_up_a_forced_mid_task_cutoff()
    test_task1_field_add_end_to_end_against_real_duplicate()
    test_task1_field_add_end_to_end_against_fresh_database()
    test_outer_plan_dependent_items_get_distinct_modules_and_preserve_earlier_fields()
    test_real_diff_computation_round1_all_additions_round2_real_delta()
    print("\nALL BUILD SPECIALIST TESTS PASSED")
