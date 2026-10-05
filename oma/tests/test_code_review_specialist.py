"""Phase 10: real tests for the Code-Review specialist, against the
actual odoo16-dev container, the real custom Odoo codebase, and the
real model gateway (qwen3.6-27b) -- no mocks. Covers the build plan's
own required test (a deliberately introduced bad pattern gets flagged),
both TaskContract shapes (diff review and task 4's full audit), and the
structural (not just instructional) read-only property.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import asyncio
import uuid

from contracts.schema import AutonomyTier, CapabilityClass, SpecialistType, TaskContract
from infra.gateway_client import ModelGatewayClient
from specialists.code_review.specialist import CodeReviewSpecialist
from tools_odoo.module_dev.toolchain import _MODULE_DEV_ADDONS_DIR, _run_in_container, scaffold_module, write_module_file

BAD_MODULE = "oma_deliberately_bad_test_module"
AUDIT_TARGET = "garazd_product_label"  # a real, small custom module under /opt/site/site16


def _cleanup_module_dir(module_name: str) -> None:
    _run_in_container(f"rm -rf {_MODULE_DEV_ADDONS_DIR}/{module_name}")


def _write_deliberately_bad_module() -> None:
    _cleanup_module_dir(BAD_MODULE)
    scaffold_module(BAD_MODULE)
    write_module_file(
        BAD_MODULE,
        "__manifest__.py",
        '{\n'
        '    "name": "oma_deliberately_bad_test_module",\n'
        '    "license": "LGPL-3",\n'
        '    "depends": ["base"],\n'
        '    "data": [],\n'
        '}\n',
    )
    write_module_file(
        BAD_MODULE,
        "models/models.py",
        'from odoo import fields, models\n\n\n'
        'class BadPatternPartner(models.Model):\n'
        '    _inherit = "res.partner"\n\n'
        '    # Hardcoded credential -- exactly the kind of thing review must flag.\n'
        '    API_SECRET_KEY = "sk_test_51H8x9K2mZQwMOCKsecretdonotcommit12345"\n\n'
        '    def unsafe_bulk_delete_all_partners(self):\n'
        '        # Overly broad sudo() bypassing all access rules, deleting\n'
        '        # every partner record in the system unconditionally.\n'
        '        self.env["res.partner"].sudo().search([]).unlink()\n',
    )


def _make_contract(goal: str, inputs: list[str], validation_by: str = "code_review") -> TaskContract:
    return TaskContract(
        task_id=uuid.uuid4(),
        specialist_type=SpecialistType.code_review,
        capability_class=CapabilityClass.readonly_investigation,
        tier=AutonomyTier.tier_1_readonly,
        goal=goal,
        inputs=inputs,
        rules=["No changes to core Odoo modules", "Read-only -- no writes at all"],
        deliverables=["A structured list of findings"],
        compensating_actions=[],
        validation_by=validation_by,
        pause_if=[],
        turn_budget=10,
    )


def test_review_flags_deliberately_bad_pattern_as_blocking():
    """The build plan's own required test: a deliberately introduced
    bad pattern (a hardcoded credential, an overly broad sudo() call)
    must actually be flagged, before this specialist is trusted against
    a real Build specialist's output or task 4's real codebase.
    """
    _write_deliberately_bad_module()
    try:
        async def _drive():
            client = ModelGatewayClient()
            try:
                specialist = CodeReviewSpecialist(client=client)
                contract = _make_contract(
                    f"Review the module {BAD_MODULE} for Constitution alignment.",
                    [f"diff_module:{BAD_MODULE}"],
                )
                return await specialist.run(contract)
            finally:
                await client.aclose()

        output = asyncio.run(_drive())

        assert output.detail["mode"] == "diff_review"
        findings = output.detail["findings"]
        blocking = [f for f in findings if f["severity"] == "blocking"]
        assert len(blocking) >= 2, f"expected at least 2 blocking findings, got: {findings}"

        joined = " ".join(f["explanation"].lower() for f in blocking)
        assert "secret" in joined or "credential" in joined or "hardcoded" in joined, (
            f"expected the hardcoded-credential pattern to be flagged: {blocking}"
        )
        assert "sudo" in joined or "delete" in joined or "unlink" in joined, (
            f"expected the overly-broad sudo()/bulk-delete pattern to be flagged: {blocking}"
        )
        assert output.claims_complete is False, "a module with blocking findings must not be approved"
        print(f"PASS: both deliberately introduced bad patterns were flagged as blocking findings "
              f"({len(blocking)} blocking finding(s) total), claims_complete correctly False")
    finally:
        _cleanup_module_dir(BAD_MODULE)


def test_full_audit_against_real_custom_module():
    """Task 4's shape: a full, read-only audit of a real custom Odoo
    module -- not a synthetic test fixture. claims_complete here means
    "the audit itself completed," not a pass/fail judgment.
    """
    async def _drive():
        client = ModelGatewayClient()
        try:
            specialist = CodeReviewSpecialist(client=client)
            contract = _make_contract(
                f"Full read-only quality audit of the {AUDIT_TARGET} custom module.",
                [f"full_codebase_audit:{AUDIT_TARGET}"],
            )
            return await specialist.run(contract)
        finally:
            await client.aclose()

    output = asyncio.run(_drive())

    if "mode" not in output.detail:
        raise AssertionError(f"run() fell through to its error path -- summary was: {output.summary!r}")
    assert output.detail["mode"] == "full_audit"
    assert output.detail["audited_path"] == AUDIT_TARGET
    assert output.detail["files_read"] > 0
    assert isinstance(output.detail["findings"], list)
    assert output.claims_complete is True, "a completed audit must claim completion regardless of findings"
    assert output.summary
    print(f"PASS: full read-only audit of real module {AUDIT_TARGET!r} completed -- "
          f"{output.detail['files_read']} real files read, "
          f"{len(output.detail['findings'])} finding(s), claims_complete=True")


def test_wrong_capability_class_is_refused():
    async def _drive():
        client = ModelGatewayClient()
        try:
            specialist = CodeReviewSpecialist(client=client)
            contract = TaskContract(
                task_id=uuid.uuid4(),
                specialist_type=SpecialistType.code_review,
                capability_class=CapabilityClass.module_development,
                tier=AutonomyTier.tier_2_notify_after,
                goal="Should be refused",
                inputs=[f"diff_module:{BAD_MODULE}"],
                rules=[],
                deliverables=[],
                compensating_actions=[],
                validation_by="code_review",
                pause_if=[],
                turn_budget=5,
            )
            return await specialist.run(contract)
        finally:
            await client.aclose()

    output = asyncio.run(_drive())
    assert output.claims_complete is False
    assert "readonly_investigation" in output.summary
    print("PASS: CodeReviewSpecialist correctly refuses a non-readonly_investigation capability_class")


def test_no_review_target_is_refused_not_guessed():
    async def _drive():
        client = ModelGatewayClient()
        try:
            specialist = CodeReviewSpecialist(client=client)
            contract = _make_contract("A goal with no diff_module: or full_codebase_audit: input.", [])
            return await specialist.run(contract)
        finally:
            await client.aclose()

    output = asyncio.run(_drive())
    assert output.claims_complete is False
    assert "nothing to review" in output.summary
    print("PASS: CodeReviewSpecialist refuses rather than guessing a review target when inputs don't name one")


def test_read_only_enforced_structurally_not_just_by_instruction():
    """Per the build plan's own emphasis: task 4's read-only property
    must be structural, not a promise the specialist keeps. Confirms
    this specialist's own module (and its file-access helper) contain
    no import of anything write-capable at all -- grep-verified, the
    same discipline as test_manager_tools.py's single-write-path proof.
    """
    import ast

    import specialists.code_review.specialist as spec_module
    import tools_odoo.codebase_read as read_module

    forbidden_symbols = {"OdooToolClient", "ModuleDevToolchain", "write_module_file", "install_module", "scaffold_module"}
    for path in (spec_module.__file__, read_module.__file__):
        tree = ast.parse(open(path).read(), filename=path)
        imported_names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_names.update(alias.asname or alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported_names.update(alias.asname or alias.name for alias in node.names)
        bad = imported_names & forbidden_symbols
        assert not bad, f"{path} actually IMPORTS write-capable {bad!r} -- read-only must be structural"
    print("PASS: neither the Code-Review specialist nor its file-access helper reference any "
          "write-capable symbol at all -- read-only is structural, not just instructional")


def test_hallucinated_method_missing_filter_survives_decoy_init_file():
    """Real bugs found live (2026-07-24, task 020's Group C re-run,
    project_fieldjob): Code-Review repeatedly, flatly asserted
    `action_accept` "is not defined in the base model project.fieldjob"
    -- false; it's defined at
    /opt/site/site16/project_fieldjob/models/project_fieldjob.py:121 --
    blocking 7+ real rounds before this was caught. Two separate,
    independent bugs had to be fixed before the downgrade actually
    fired in production, even though isolated hand-built-input testing
    of the filter passed the whole time:

    1. `_SUPER_CALL_RE` only matched the zero-arg `super().method(` form
       -- a real generated override used the explicit
       `super(ClassName, self).method(` form instead, so the call was
       never even extracted.
    2. The file-picking `next(...)` used a loose `"/models/" in path`
       fallback, which also matches `models/__init__.py` (near-empty,
       zero `super()` calls) -- and since dict order follows
       read_module_files()'s own `find` output, that decoy sorted
       BEFORE the real `models/models.py` and got picked instead. This
       is the one that actually mattered in production: the filter
       logic itself was correct the whole time, but was silently
       being run against the wrong file.

    This test reproduces bug 2 specifically -- a dict shaped exactly
    like read_module_files()'s real output (decoy `__init__.py` sorted
    first) -- so a future refactor of the file-picking logic can't
    reintroduce it without this test catching it immediately, no live
    LLM call or container access needed.
    """
    from specialists.code_review.specialist import ReviewFinding, _filter_hallucinated_method_missing_findings

    os.environ.setdefault("OMA_ODOO_DB_DUPLICATE_FOR_BUILD", "odoo16_dev")

    files = {
        # Deliberately BEFORE the real models.py, matching real
        # read_module_files() dict ordering for this exact module shape.
        "/mnt/extra-addons/oma_x/models/__init__.py": "from . import models\n",
        "/mnt/extra-addons/oma_x/models/models.py": (
            "from odoo import models\n\n"
            "class ProjectFieldjob(models.Model):\n"
            "    _inherit = 'project.fieldjob'\n\n"
            "    def action_accept(self):\n"
            "        if hasattr(super(ProjectFieldjob, self), 'action_accept'):\n"
            "            super(ProjectFieldjob, self).action_accept()\n"
        ),
    }
    findings = [
        ReviewFinding(
            location="models/models.py:5", severity="blocking",
            explanation=(
                "The method action_accept is not defined in the base model project.fieldjob, "
                "so super() will raise an AttributeError and the email will never send."
            ),
        ),
    ]
    out = _filter_hallucinated_method_missing_findings(findings, files)
    assert out[0].severity == "info", (
        f"expected the false claim to be downgraded even with a decoy __init__.py present in "
        f"the files dict -- got severity {out[0].severity!r}, explanation {out[0].explanation!r}"
    )
    assert "DOES exist" in out[0].explanation
    print("PASS: the method-existence filter correctly picks the real models.py (not a decoy "
          "models/__init__.py that sorts first) and downgrades the false claim, matching both "
          "super() call forms")


def test_hallucinated_xmlid_missing_filter_downgrades_verified_ref():
    """Real bug found live (2026-07-24, task 020's 14th resume attempt):
    Code-Review blocked a round with a HEDGED, unverified claim -- "The
    model_id ref project_fieldjob.model_project_fieldjob must exist in
    the base module project_fieldjob; if it doesn't, this will fail to
    load, causing install failure" -- for a real, genuinely-existing
    xmlid (confirmed live via a direct grep of the real base module's
    own source: /opt/site/site16/project_fieldjob/security/ir.model.
    access.csv and several other files all reference it). A hedged
    "if it doesn't exist" claim is exactly as ungrounded as a flat
    hallucinated assertion when Code-Review has no tool access to
    actually check -- this filter closes that gap via a real
    ir.model.data lookup.
    """
    from specialists.code_review.specialist import ReviewFinding, _filter_hallucinated_xmlid_missing_findings

    os.environ.setdefault("OMA_ODOO_DB_DUPLICATE_FOR_BUILD", "odoo16_dev")

    files = {
        "/mnt/extra-addons/oma_x/data/mail_template_data.xml": (
            '<odoo>\n  <record id="x" model="mail.template">\n'
            '    <field name="model_id" ref="project_fieldjob.model_project_fieldjob"/>\n'
            "  </record>\n</odoo>"
        ),
    }
    findings = [
        ReviewFinding(
            location="data/mail_template_data.xml:3", severity="blocking",
            explanation=(
                "The model_id ref project_fieldjob.model_project_fieldjob must exist in the "
                "base module project_fieldjob; if it doesn't, this will fail to load, causing "
                "install failure."
            ),
        ),
    ]
    out = _filter_hallucinated_xmlid_missing_findings(findings, files)
    assert out[0].severity == "info", (
        f"expected the unverified 'must exist' claim to be downgraded once confirmed real -- "
        f"got severity {out[0].severity!r}"
    )
    assert "DOES exist" in out[0].explanation
    print("PASS: the xmlid-existence filter correctly verifies a real cross-module ref and "
          "downgrades the unfounded uncertainty")


def test_hallucinated_xmlid_missing_filter_also_checks_security_csv_model_id():
    """Real bug found live (2026-08-06, fix-pass task 024): Code-Review claimed "model_id:id=
    'model_waste_container' does not resolve to a real external id in the live registry,
    causing module installation failure" -- directly contradicted by this exact same round's
    own confirmed successful install (a genuinely unresolvable model_id crashes Odoo's own
    registry-build at install time). `_filter_hallucinated_xmlid_missing_findings()` previously
    only ever scanned `.xml` files' own `ref="module.xmlid"` attributes -- `security/ir.model.
    access.csv`'s `model_id:id` column is a completely different real Odoo reference syntax for
    the same underlying concept, never covered before this fix. Uses a real, live-confirmed-
    existing model_id (project_fieldjob's own model), same "no mocks, real DB" discipline as
    every other test in this file.
    """
    from specialists.code_review.specialist import ReviewFinding, _filter_hallucinated_xmlid_missing_findings

    os.environ.setdefault("OMA_ODOO_DB_DUPLICATE_FOR_BUILD", "odoo16_dev")

    files = {
        "/mnt/extra-addons/project_fieldjob/security/ir.model.access.csv": (
            "id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink\n"
            "access_project_fieldjob,project.fieldjob,model_project_fieldjob,base.group_user,1,1,1,0\n"
        ),
    }
    findings = [
        ReviewFinding(
            location="security/ir.model.access.csv:2", severity="blocking",
            explanation=(
                "model_id:id='model_project_fieldjob' does not resolve to a real external id "
                "in the live registry, causing module installation failure."
            ),
        ),
    ]
    out = _filter_hallucinated_xmlid_missing_findings(findings, files, "project_fieldjob")
    assert out[0].severity == "info", (
        f"expected the unverified CSV model_id claim to be downgraded once confirmed real -- "
        f"got severity {out[0].severity!r}"
    )
    print("PASS: the xmlid-existence filter now also checks security_csv's own model_id:id "
          "column, task 024's own real defect shape")


def test_hallucinated_xmlid_missing_filter_csv_check_still_flags_a_genuine_miss():
    """Regression guard: a genuinely non-existent model_id in the CSV must still be flagged
    blocking -- the widened check must never turn into a blanket pass-through.
    """
    from specialists.code_review.specialist import ReviewFinding, _filter_hallucinated_xmlid_missing_findings

    os.environ.setdefault("OMA_ODOO_DB_DUPLICATE_FOR_BUILD", "odoo16_dev")

    files = {
        "/mnt/extra-addons/oma_x/security/ir.model.access.csv": (
            "id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink\n"
            "access_x,x,model_totally_invented_xyz_never_real,base.group_user,1,1,1,0\n"
        ),
    }
    findings = [
        ReviewFinding(
            location="security/ir.model.access.csv:2", severity="blocking",
            explanation=(
                "model_id:id='model_totally_invented_xyz_never_real' does not resolve to a "
                "real external id in the live registry, causing module installation failure."
            ),
        ),
    ]
    out = _filter_hallucinated_xmlid_missing_findings(findings, files, "oma_x")
    assert out[0].severity == "blocking", (
        f"a genuinely non-existent model_id must still be flagged blocking -- got "
        f"{out[0].severity!r}"
    )
    print("PASS: a genuinely non-existent CSV model_id is still correctly flagged blocking")


def test_hallucinated_direct_field_missing_filter_downgrades_verified_field():
    """Real bug found live (2026-07-24, task 020's 15th resume attempt):
    Code-Review flagged "The template body references object.name, but
    the model project.fieldjob may not have a name field; verify field
    existence to prevent rendering errors" -- `name` is a real,
    directly-declared field on the actual base model (confirmed live:
    `name = fields.Char(string='Reference', readonly=True, copy=False,
    default='New')` in the real source). This is a different surface
    from the method-existence and xmlid-existence siblings: an
    unverified "might not have this field" claim about a mail
    template's `object.<field>` reference.
    """
    from specialists.code_review.specialist import (
        ReviewFinding,
        _filter_hallucinated_direct_field_missing_findings,
    )

    os.environ.setdefault("OMA_ODOO_DB_DUPLICATE_FOR_BUILD", "odoo16_dev")

    files = {
        "/mnt/extra-addons/oma_x/models/models.py": (
            "from odoo import models\n\nclass ProjectFieldjob(models.Model):\n"
            "    _inherit = 'project.fieldjob'\n"
        ),
        "/mnt/extra-addons/oma_x/data/mail_template_data.xml": (
            '<odoo><record><field name="body_html">{{ object.name }}</field></record></odoo>'
        ),
    }
    findings = [
        ReviewFinding(
            location="data/mail_template_data.xml:1", severity="blocking",
            explanation=(
                "The template body references object.name, but the model project.fieldjob "
                "may not have a name field; verify field existence to prevent rendering errors."
            ),
        ),
    ]
    out = _filter_hallucinated_direct_field_missing_findings(findings, files)
    assert out[0].severity == "info", (
        f"expected the unverified 'may not have' claim to be downgraded once confirmed real -- "
        f"got severity {out[0].severity!r}"
    )
    assert "DOES exist" in out[0].explanation
    print("PASS: the direct-field-existence filter correctly verifies object.name is real on "
          "project.fieldjob and downgrades the unfounded uncertainty")


def test_hallucinated_duplicate_field_declaration_filter_downgrades_local_variable_claim():
    """Real, confirmed bug found live (2026-08-09, task 07141af5's flagship run,
    project_ticket_counts node): Code-Review claimed "Field 'cutoff' is declared twice in the
    same class, causing a silent shadowing error that breaks the compute method" -- `cutoff` is
    a plain method-body local variable (`cutoff = fields.Datetime.now() - ...`, a common Odoo
    idiom for reading the current date/time), appearing in TWO DIFFERENT methods
    (_compute_ticket_counts and action_overdue_tickets), never actually declared as a field at
    all -- the exact same false-positive shape already root-caused and fixed on the Build side
    (_MODEL_FIELD_DEF_RE's own fix), independently hallucinated by Code-Review's own separate
    LLM judgment.
    """
    from specialists.code_review.specialist import (
        ReviewFinding,
        _filter_hallucinated_duplicate_field_declaration_findings,
    )

    files = {
        "/mnt/extra-addons/oma_x/models/models.py": (
            "from odoo import models, fields, api\n\n"
            "class ProjectProject(models.Model):\n"
            "    _inherit = 'project.project'\n\n"
            "    open_ticket_count = fields.Integer(compute='_compute_ticket_counts')\n"
            "    overdue_ticket_count = fields.Integer(compute='_compute_ticket_counts')\n\n"
            "    @api.depends('service_ticket_ids.state')\n"
            "    def _compute_ticket_counts(self):\n"
            "        for project in self:\n"
            "            cutoff = fields.Datetime.now() - timedelta(days=3)\n"
            "            project.overdue_ticket_count = 0\n\n"
            "    def action_overdue_tickets(self):\n"
            "        cutoff = fields.Datetime.now() - timedelta(days=3)\n"
            "        return {'domain': [('create_date', '<', cutoff)]}\n"
        ),
    }
    findings = [
        ReviewFinding(
            location="models/models.py:1", severity="blocking",
            explanation=(
                "Field 'cutoff' is declared twice in the same class, causing a silent shadowing "
                "error that breaks the compute method."
            ),
        ),
    ]
    out = _filter_hallucinated_duplicate_field_declaration_findings(findings, files)
    assert out[0].severity == "info", (
        f"expected the false 'declared twice' claim about a local variable to be downgraded -- "
        f"got severity {out[0].severity!r}"
    )
    assert "never actually declared as a field more than once" in out[0].explanation

    # But a genuine duplicate field declaration must still be caught, never blanket-suppressed.
    genuinely_duplicated_files = {
        "/mnt/extra-addons/oma_x/models/models.py": (
            "class X(models.Model):\n    _name = 'x.model'\n"
            "    status = fields.Selection([('a', 'A')])\n"
            "    status = fields.Selection([('b', 'B')])\n"
        ),
    }
    real_findings = [
        ReviewFinding(
            location="models/models.py:1", severity="blocking",
            explanation="Field 'status' is declared twice in the same class.",
        ),
    ]
    real_out = _filter_hallucinated_duplicate_field_declaration_findings(
        real_findings, genuinely_duplicated_files,
    )
    assert real_out[0].severity == "blocking", (
        "a genuinely duplicated field must never be downgraded"
    )
    print("PASS: the duplicate-field-declaration filter correctly downgrades a false claim about "
          "a repeated method-body local variable, while never touching a genuinely duplicated field")


def test_hallucinated_missing_import_filter_downgrades_stale_import_claim():
    """Real, confirmed bug found live (2026-08-10, task 07141af5's flagship run,
    project_ticket_counts node): Code-Review repeatedly claimed "Missing import 'from odoo
    import fields' causes a NameError when running the test" -- confirmed directly against the
    real committed test file that the import WAS already present. This exact stale complaint
    resurfaced repeatedly across many separate resumes even after being explicitly retracted via
    note each time -- a real instance of resume_task_after_checkpoint()'s own round-history
    replay carrying a resolved finding forward indefinitely.
    """
    from specialists.code_review.specialist import (
        ReviewFinding,
        _filter_hallucinated_missing_import_findings,
    )

    files = {
        "tests/test_project_ticket_counts.py": (
            "from odoo.tests.common import TransactionCase\n"
            "from odoo.tests import tagged\n"
            "from odoo import fields, api\n\n"
            "class TestX(TransactionCase):\n    pass\n"
        ),
    }
    findings = [
        ReviewFinding(
            location="tests/test_project_ticket_counts.py:1", severity="blocking",
            explanation="Missing import 'from odoo import fields' causes a NameError when running the test.",
        ),
        ReviewFinding(
            location="tests/test_project_ticket_counts.py:5", severity="blocking",
            explanation="NameError: name 'fields' is not defined; missing import 'from odoo import fields' in test file.",
        ),
        ReviewFinding(
            location="tests/test_project_ticket_counts.py:1", severity="blocking",
            explanation="Missing import for 'fields' module, causing a NameError when the test runs.",
        ),
        ReviewFinding(
            location="tests/test_project_ticket_counts.py:1", severity="blocking",
            explanation="Missing import `from odoo import fields` causes NameError when running tests.",
        ),
    ]
    out = _filter_hallucinated_missing_import_findings(findings, files)
    assert all(o.severity == "info" for o in out), (
        f"expected all four stale 'missing import' phrasings (incl. backtick-quoted) to be "
        f"downgraded -- got {[o.severity for o in out]!r}"
    )

    # But a genuinely missing import must still be caught.
    missing_files = {
        "tests/test_y.py": "class TestY:\n    pass\n",
    }
    real_findings = [
        ReviewFinding(
            location="tests/test_y.py:1", severity="blocking",
            explanation="Missing import 'from odoo import fields' causes a NameError.",
        ),
    ]
    real_out = _filter_hallucinated_missing_import_findings(real_findings, missing_files)
    assert real_out[0].severity == "blocking", "a genuinely missing import must never be downgraded"
    print("PASS: the missing-import filter correctly downgrades a false claim about an already-"
          "present import, while never touching a genuinely missing one")


def test_hallucinated_direct_field_missing_filter_also_checks_view_syntax():
    """Real, confirmed follow-up bug found live (2026-07-24, task 020's
    20th resume attempt): the sibling test above only covers the Jinja
    `object.<field>`/`record.<field>` convention. Confirmed live: once
    the invented `expected_finish_date` field was finally fixed (via
    the close-match suggestion in specialists/build/specialist.py),
    Code-Review immediately hallucinated that the REAL fields
    `date_finish`/`amount_total` "are not defined... nor are they part
    of the base project.fieldjob model" -- referenced via `<field
    name="date_finish">` inside a view's `<arch>` block, a completely
    different syntax this filter never scanned. Mirrors the exact same
    views_xml-vs-Jinja gap already found and fixed once on the Build
    side (specialists/build/specialist.py's
    _validate_view_fields_exist_on_inherited_model).
    """
    from specialists.code_review.specialist import (
        ReviewFinding,
        _filter_hallucinated_direct_field_missing_findings,
    )

    os.environ.setdefault("OMA_ODOO_DB_DUPLICATE_FOR_BUILD", "odoo16_dev")

    files = {
        "/mnt/extra-addons/oma_x/models/models.py": (
            "from odoo import models\n\nclass ProjectFieldjob(models.Model):\n"
            "    _inherit = 'project.fieldjob'\n"
        ),
        "/mnt/extra-addons/oma_x/views/views.xml": (
            '<odoo>\n  <record id="v1" model="ir.ui.view">\n'
            '    <field name="model">project.fieldjob</field>\n'
            '    <field name="arch" type="xml">\n'
            '      <field name="date_finish"/>\n'
            '      <field name="amount_total"/>\n'
            "    </field>\n  </record>\n</odoo>"
        ),
    }
    findings = [
        ReviewFinding(
            location="views/views.xml:5", severity="blocking",
            explanation=(
                "The view inherits and adds fields date_finish and amount_total, but these "
                "fields are not defined in the models_py file nor are they part of the base "
                "project.fieldjob model, causing Odoo to fail to load the view."
            ),
        ),
    ]
    out = _filter_hallucinated_direct_field_missing_findings(findings, files)
    assert out[0].severity == "info", (
        f"expected the unverified 'not defined' claim about real view fields to be downgraded "
        f"-- got severity {out[0].severity!r}"
    )
    assert "DOES exist" in out[0].explanation
    print("PASS: the direct-field-existence filter also checks <field name=...> view syntax, "
          "not just Jinja object.<field>, and downgrades the unfounded uncertainty")


def test_hallucinated_direct_field_missing_filter_also_checks_currency_field_kwarg():
    """Real, confirmed bug found live (2026-07-26, Phase 25D, task 004's
    own resubmission): Code-Review flagged "'currency_id' is not
    declared in this model or inherited from 'project.fieldjob'...
    causing a runtime error" -- `currency_id` is a real, live field on
    the actual base model. Neither Jinja `object.<field>` nor `<field
    name="X">` view syntax -- a THIRD real Odoo field-reference shape,
    a KWARG VALUE inside a field declaration itself
    (`currency_field='currency_id'`), this filter never looked at.
    """
    from specialists.code_review.specialist import (
        ReviewFinding,
        _filter_hallucinated_direct_field_missing_findings,
    )

    os.environ.setdefault("OMA_ODOO_DB_DUPLICATE_FOR_BUILD", "odoo16_dev")

    files = {
        "/mnt/extra-addons/oma_x/models/models.py": (
            "from odoo import api, fields, models\n\nclass ProjectFieldjob(models.Model):\n"
            "    _inherit = 'project.fieldjob'\n"
            "    amount_total = fields.Monetary(string='Amount Total', "
            "compute='_compute_amount_total', store=True, currency_field='currency_id')\n"
        ),
    }
    findings = [
        ReviewFinding(
            location="models.py:5", severity="blocking",
            explanation=(
                "Field 'amount_total' uses 'currency_field=currency_id' but 'currency_id' is not "
                "declared in this model or inherited from 'project.fieldjob' in the provided "
                "context, causing a runtime error."
            ),
        ),
    ]
    out = _filter_hallucinated_direct_field_missing_findings(findings, files)
    assert out[0].severity == "info", (
        f"expected the unverified 'not declared' claim about currency_field to be downgraded once "
        f"confirmed real -- got severity {out[0].severity!r}"
    )
    assert "DOES exist" in out[0].explanation
    print("PASS: the direct-field-existence filter also checks currency_field=... kwarg values, "
          "not just Jinja object.<field> or <field name=...> view syntax")


def _view_files_referencing(model_name: str, field_name: str) -> dict:
    return {
        "/mnt/extra-addons/oma_x/models/models.py": (
            f"from odoo import models\n\nclass X(models.Model):\n    _inherit = {model_name!r}\n"
        ),
        "/mnt/extra-addons/oma_x/views/views.xml": (
            '<odoo><record id="view_x_form" model="ir.ui.view">'
            '<field name="arch" type="xml"><form>'
            f'<field name="{field_name}"/>'
            "</form></field></record></odoo>"
        ),
    }


def test_hallucinated_direct_field_missing_filter_catches_does_not_define_phrasing():
    """Real, confirmed FIFTH rephrasing found live (2026-08-03, full-30-task sweep): 5 separate
    real tasks the same day (001, 002, 003, 013, 015, 021 -- see docs/reports/
    PHASE30_50TASK_SWEEP_BATCH2_BEFORE_AFTER_2026-08-03.md) all hit the identical real root
    cause -- a field legitimately absent from this round's own diff because Build's own
    `_validate_no_new_field_collides_with_real_target_field` autofix correctly removed it (it
    already exists for real on the target model) -- but Code-Review, with no visibility into
    that, confidently (not hedgingly) reported the field as missing using phrasing
    (`_FIELD_UNCERTAIN_CLAIM_RE`'s own pre-2026-08-03 alternatives never matched: "does not
    define", "defines no fields", "never defined in", "field is missing from"), so this exact
    filter -- already fully built, already wired, already able to verify the field is real via a
    live query -- silently never fired. Using task001's own real model/field
    (`crm.lead`/`special_instructions`, confirmed real on the live odoo16_dev target).
    """
    from specialists.code_review.specialist import (
        ReviewFinding,
        _filter_hallucinated_direct_field_missing_findings,
    )

    os.environ.setdefault("OMA_ODOO_DB_DUPLICATE_FOR_BUILD", "odoo16_dev")

    files = _view_files_referencing("crm.lead", "special_instructions")
    findings = [
        ReviewFinding(
            location="views.xml:1", severity="blocking",
            explanation=(
                "The model class inherits crm.lead but does not define the "
                "'special_instructions' field referenced in the view."
            ),
        ),
    ]
    out = _filter_hallucinated_direct_field_missing_findings(findings, files)
    assert out[0].severity == "info", (
        f"expected the 'does not define' claim to be downgraded once confirmed real -- "
        f"got severity {out[0].severity!r}"
    )
    assert "DOES exist" in out[0].explanation
    print("PASS: task001's real 'does not define' phrasing is now caught and downgraded")


def test_hallucinated_direct_field_missing_filter_catches_defines_no_fields_phrasing():
    """Real task002/021 phrasing -- 'defines no fields' (sometimes naming several fields at
    once). Using task021's own real model/field (`project.fieldjob`/`customer_response`,
    confirmed real on the live odoo16_dev target).
    """
    from specialists.code_review.specialist import (
        ReviewFinding,
        _filter_hallucinated_direct_field_missing_findings,
    )

    os.environ.setdefault("OMA_ODOO_DB_DUPLICATE_FOR_BUILD", "odoo16_dev")

    files = _view_files_referencing("project.fieldjob", "customer_response")
    findings = [
        ReviewFinding(
            location="views.xml:1", severity="blocking",
            explanation=(
                "Model class inherits 'project.fieldjob' but defines no fields "
                "(customer_response, customer_response_date, customer_response_user_id) "
                "required by the view and task goal."
            ),
        ),
    ]
    out = _filter_hallucinated_direct_field_missing_findings(findings, files)
    assert out[0].severity == "info", (
        f"expected the 'defines no fields' claim to be downgraded once confirmed real -- "
        f"got severity {out[0].severity!r}"
    )
    print("PASS: task021's real 'defines no fields' phrasing is now caught and downgraded")


def test_hallucinated_direct_field_missing_filter_catches_defines_no_new_fields_phrasing():
    """Real task004 phrasing (2026-08-03 full-30-task sweep) -- 'defines no NEW fields', one word
    inserted between 'no' and 'fields' from the already-covered 'defines no fields' alternative,
    the exact same 'new phrasing dodges the regex' failure this filter family keeps hitting.
    Using task004's own real model/field (`project.fieldjob`/`amount_total`, confirmed real on the
    live odoo16_dev target).
    """
    from specialists.code_review.specialist import (
        ReviewFinding,
        _filter_hallucinated_direct_field_missing_findings,
    )

    os.environ.setdefault("OMA_ODOO_DB_DUPLICATE_FOR_BUILD", "odoo16_dev")

    files = _view_files_referencing("project.fieldjob", "amount_total")
    findings = [
        ReviewFinding(
            location="views.xml:1", severity="blocking",
            explanation=(
                "The model class inherits 'project.fieldjob' but defines no new fields, leaving "
                "the view's reference to 'amount_total' undefined and causing a runtime error."
            ),
        ),
    ]
    out = _filter_hallucinated_direct_field_missing_findings(findings, files)
    assert out[0].severity == "info", (
        f"expected the 'defines no new fields' claim to be downgraded once confirmed real -- "
        f"got severity {out[0].severity!r}"
    )
    print("PASS: task004's real 'defines no new fields' / 'reference to ... undefined' phrasing is now caught and downgraded")


def test_hallucinated_direct_field_missing_filter_catches_does_not_exist_on_the_model_phrasing():
    """Real task028 phrasing (2026-08-03): "references 'project_type_ids' which does not exist on
    the model" -- names the field by its quoted identifier rather than the word "field", so the
    existing "field .{0,10}(?:does not|doesn't) exist" alternative never matched it. Using
    task028's own real model/field (`crm.lead`/`project_type_ids`, confirmed real on the live
    odoo16_dev target -- an earlier round's own real, already-installed work).
    """
    from specialists.code_review.specialist import (
        ReviewFinding,
        _filter_hallucinated_direct_field_missing_findings,
    )

    os.environ.setdefault("OMA_ODOO_DB_DUPLICATE_FOR_BUILD", "odoo16_dev")

    files = _view_files_referencing("crm.lead", "project_type_ids")
    findings = [
        ReviewFinding(
            location="views.xml:1", severity="blocking",
            explanation=(
                "The view references 'project_type_ids' which does not exist on the model, "
                "causing a rendering error."
            ),
        ),
    ]
    out = _filter_hallucinated_direct_field_missing_findings(findings, files)
    assert out[0].severity == "info", (
        f"expected the 'does not exist on the model' claim to be downgraded once confirmed real "
        f"-- got severity {out[0].severity!r}"
    )
    print("PASS: task028's real 'which does not exist on the model' phrasing is now caught and downgraded")


def test_hallucinated_direct_field_missing_filter_catches_never_defined_and_missing_from_phrasings():
    """Real task003/013 phrasings -- 'never defined in the model' and 'field is missing from the
    ... model definition'. Using task013's own real model/field (`project.fieldjob`/
    `invoice_id`, confirmed real on the live odoo16_dev target).
    """
    from specialists.code_review.specialist import (
        ReviewFinding,
        _filter_hallucinated_direct_field_missing_findings,
    )

    os.environ.setdefault("OMA_ODOO_DB_DUPLICATE_FOR_BUILD", "odoo16_dev")

    files = _view_files_referencing("project.fieldjob", "invoice_id")
    findings = [
        ReviewFinding(
            location="views.xml:1", severity="blocking",
            explanation=(
                "The model class is empty; the required `invoice_id` field is missing from "
                "the Python model definition."
            ),
        ),
    ]
    out = _filter_hallucinated_direct_field_missing_findings(findings, files)
    assert out[0].severity == "info", (
        f"expected the 'field is missing from' claim to be downgraded once confirmed real -- "
        f"got severity {out[0].severity!r}"
    )
    print("PASS: task013's real 'field is missing from' phrasing is now caught and downgraded")


def test_hallucinated_direct_field_missing_filter_still_flags_a_genuinely_missing_field():
    """Regression guard: the broadened phrasing match must never turn into a blanket
    "any missing-field claim is unfounded" pass-through -- a field that's genuinely NOT real on
    the target model must still be flagged as blocking, exactly as before this fix.
    """
    from specialists.code_review.specialist import (
        ReviewFinding,
        _filter_hallucinated_direct_field_missing_findings,
    )

    os.environ.setdefault("OMA_ODOO_DB_DUPLICATE_FOR_BUILD", "odoo16_dev")

    files = _view_files_referencing("crm.lead", "totally_invented_field_xyz_never_real")
    findings = [
        ReviewFinding(
            location="views.xml:1", severity="blocking",
            explanation=(
                "The model class inherits crm.lead but does not define the "
                "'totally_invented_field_xyz_never_real' field referenced in the view."
            ),
        ),
    ]
    out = _filter_hallucinated_direct_field_missing_findings(findings, files)
    assert out[0].severity == "blocking", (
        f"a genuinely missing field must still be flagged as blocking -- "
        f"got severity {out[0].severity!r}"
    )
    print("PASS: a genuinely missing (never real) field is still correctly flagged blocking, "
          "not swept up by the broadened phrasing match")


# --- Fifth sibling, 2026-08-06 fix-pass tasks 018/019: Code-Review misreading its own post-
# install <current_schema> snapshot as proof a round's own new field already existed ---

def test_hallucinated_already_exists_filter_downgrades_task019_real_shape():
    """Real bug found live (2026-08-06, fix-pass task 019): Code-Review flagged "The field
    'customer_grouping_rule' is already defined in the provided <current_schema> for
    'project.fieldjob', making this inheritance and field definition redundant..." -- but
    `customer_grouping_rule` is genuinely THIS round's own new field declaration (confirmed via
    direct redis inspection of the real generated models.py). `<current_schema>` is fetched live,
    AFTER this round's own successful install, so it always includes the round's own just-added
    field -- Code-Review misread that as proof of a pre-existing duplicate.
    """
    from specialists.code_review.specialist import (
        ReviewFinding,
        _filter_hallucinated_already_exists_in_schema_findings,
    )

    files = {
        "/mnt/extra-addons/oma_x/models/models.py": (
            "from odoo import models, fields\n\nclass ProjectFieldjob(models.Model):\n"
            "    _inherit = 'project.fieldjob'\n\n"
            "    customer_grouping_rule = fields.Boolean(string='Group by customer')\n"
        ),
    }
    findings = [
        ReviewFinding(
            location="models/models.py:5", severity="blocking",
            explanation=(
                "The field 'customer_grouping_rule' is already defined in the provided "
                "<current_schema> for 'project.fieldjob', making this inheritance and field "
                "definition redundant and likely to cause a duplicate field error or conflict."
            ),
        ),
    ]
    out = _filter_hallucinated_already_exists_in_schema_findings(findings, files)
    assert out[0].severity == "info"
    assert "genuinely declared as a NEW field" in out[0].explanation
    print("PASS: task 019's own real 'already defined in current_schema' hallucination is "
          "correctly downgraded")


def test_hallucinated_already_exists_filter_downgrades_task018_real_shape():
    """Real bug found live (2026-08-06, fix-pass task 018): Code-Review flagged "Redeclaring
    'date_sent' field that already exists in the base model schema causes a conflict..." --
    independently confirmed the field no longer existed on the live schema at all once this
    round's own natural rollback ran, proving it was purely this round's own new addition, not
    a genuine pre-existing collision.
    """
    from specialists.code_review.specialist import (
        ReviewFinding,
        _filter_hallucinated_already_exists_in_schema_findings,
    )

    files = {
        "/mnt/extra-addons/oma_x/models/models.py": (
            "from odoo import models, fields\n\nclass ProjectFieldjob(models.Model):\n"
            "    _inherit = 'project.fieldjob'\n\n"
            "    date_sent = fields.Datetime(string='Sent On')\n"
        ),
    }
    findings = [
        ReviewFinding(
            location="models/models.py:5", severity="blocking",
            explanation=(
                "Redeclaring 'date_sent' field that already exists in the base model schema "
                "causes a conflict and potential data loss or runtime error."
            ),
        ),
    ]
    out = _filter_hallucinated_already_exists_in_schema_findings(findings, files)
    assert out[0].severity == "info"
    print("PASS: task 018's own real 'already exists in the base model schema' hallucination is "
          "correctly downgraded")


def test_hallucinated_already_exists_filter_never_fires_for_a_field_not_in_the_diff():
    """Regression guard: a genuinely different, real pre-existing collision -- the claimed field
    is NOT one this round's own models.py actually declares -- must never be downgraded.
    """
    from specialists.code_review.specialist import (
        ReviewFinding,
        _filter_hallucinated_already_exists_in_schema_findings,
    )

    files = {
        "/mnt/extra-addons/oma_x/models/models.py": (
            "from odoo import models, fields\n\nclass ProjectFieldjob(models.Model):\n"
            "    _inherit = 'project.fieldjob'\n\n"
            "    some_other_field = fields.Char(string='Other')\n"
        ),
    }
    findings = [
        ReviewFinding(
            location="models/models.py:5", severity="blocking",
            explanation=(
                "The field 'genuinely_colliding_field' is already defined in the provided "
                "<current_schema> for 'project.fieldjob'."
            ),
        ),
    ]
    out = _filter_hallucinated_already_exists_in_schema_findings(findings, files)
    assert out[0].severity == "blocking", (
        "a claimed collision on a field NOT declared anywhere in this round's own diff must "
        "never be downgraded -- it may be a genuine, real collision"
    )
    print("PASS: a claim about a field this round never declared is left untouched, never "
          "swept up by the filter")


def test_hallucinated_already_exists_filter_never_fires_when_multiple_fields_mentioned():
    """Ambiguous case: the finding mentions two of this round's own newly-declared fields at
    once -- never guess which one (or both) the claim genuinely applies to.
    """
    from specialists.code_review.specialist import (
        ReviewFinding,
        _filter_hallucinated_already_exists_in_schema_findings,
    )

    files = {
        "/mnt/extra-addons/oma_x/models/models.py": (
            "from odoo import models, fields\n\nclass ProjectFieldjob(models.Model):\n"
            "    _inherit = 'project.fieldjob'\n\n"
            "    field_a = fields.Char(string='A')\n"
            "    field_b = fields.Char(string='B')\n"
        ),
    }
    findings = [
        ReviewFinding(
            location="models/models.py:5", severity="blocking",
            explanation=(
                "Both 'field_a' and 'field_b' are already defined in the provided "
                "<current_schema> for 'project.fieldjob'."
            ),
        ),
    ]
    out = _filter_hallucinated_already_exists_in_schema_findings(findings, files)
    assert out[0].severity == "blocking"
    print("PASS: a finding naming multiple candidate fields at once is left ambiguous, never "
          "guessed")


def test_hallucinated_already_exists_filter_never_fires_without_the_claim_phrasing():
    """A blocking finding that happens to mention a real new field name, but doesn't actually
    use "already exists/defined in schema"-shaped language, must never be touched -- a
    genuinely different kind of blocking issue about the same field.
    """
    from specialists.code_review.specialist import (
        ReviewFinding,
        _filter_hallucinated_already_exists_in_schema_findings,
    )

    files = {
        "/mnt/extra-addons/oma_x/models/models.py": (
            "from odoo import models, fields\n\nclass ProjectFieldjob(models.Model):\n"
            "    _inherit = 'project.fieldjob'\n\n"
            "    customer_grouping_rule = fields.Boolean(string='Group by customer')\n"
        ),
    }
    findings = [
        ReviewFinding(
            location="models/models.py:5", severity="blocking",
            explanation="'customer_grouping_rule' should be a Selection field, not a Boolean, per the goal's own stated options.",
        ),
    ]
    out = _filter_hallucinated_already_exists_in_schema_findings(findings, files)
    assert out[0].severity == "blocking"
    print("PASS: a genuinely different blocking complaint about a real new field is left untouched")


# --- 50-task deep-dive P3 item 7: Task 024's real "_inherit target does not exist" hallucination ---
# docs/reports/PHASE30_50TASK_DEEP_DIVE_MASTER_2026-08-05.md -- real investigation, task_id
# db3a160d-cd78-4849-9339-bd66eaf2cdf8: both of Task 024's rounds show a confirmed successful
# install against `_inherit = 'waste.container'`, directly contradicting Code-Review's own
# repeated claim that the model "does not exist ... causing a crash on module load."
# `_validate_inherit_target_resolved` (Build's own deterministic pre-write check) was never the
# gap -- it correctly, silently passed since the target genuinely resolves. The real defect is
# this Code-Review hallucination, uncovered by no existing filter before this fix.

def test_hallucinated_inherit_target_missing_filter_downgrades_task024_real_shape(monkeypatch):
    import tools_odoo.odoo_schema_client as _schema_client
    from specialists.code_review.specialist import (
        ReviewFinding,
        _filter_hallucinated_inherit_target_missing_findings,
    )

    def fake_get_model_fields_fast(target, db):
        assert target == "waste.container"
        return ["id", "name", "project_id"]  # genuinely resolves -- real fields present
    monkeypatch.setattr(_schema_client, "get_model_fields_fast", fake_get_model_fields_fast)
    monkeypatch.setattr(_schema_client, "is_fast_path_eligible", lambda db: True)
    os.environ["OMA_ODOO_DB_DUPLICATE_FOR_BUILD"] = "odoo16_dev"

    files = {
        "models/models.py": (
            "from odoo import models, fields\n\nclass WasteContainer(models.Model):\n"
            "    _inherit = 'waste.container'\n\n"
            "    project_id = fields.Many2one('project.project', string='Project')\n"
        ),
    }
    findings = [
        ReviewFinding(
            location="models/models.py", severity="blocking",
            explanation=(
                "The model uses `_inherit = 'waste.container'` but the model `waste.container` "
                "does not exist in standard Odoo or the provided dependencies, causing a crash "
                "on module load."
            ),
        ),
    ]
    out = _filter_hallucinated_inherit_target_missing_findings(findings, files)
    assert out[0].severity == "info", (
        f"Task 024's real false 'model does not exist' claim, contradicted by a real live-"
        f"registry lookup, must be downgraded -- got severity {out[0].severity!r}"
    )
    print("PASS: Task 024's real false-nonexistence claim about its own _inherit target is "
          "correctly downgraded as a hallucination")


def test_hallucinated_inherit_target_missing_filter_downgrades_task024_round2_list_form(monkeypatch):
    """Round 2's real shape used the list form of _inherit (`_inherit = ['waste.container']`) and
    a slightly different rephrasing ("Uses _inherit = [...] but the model X does not exist...") --
    both must still be caught."""
    import tools_odoo.odoo_schema_client as _schema_client
    from specialists.code_review.specialist import (
        ReviewFinding,
        _filter_hallucinated_inherit_target_missing_findings,
    )

    monkeypatch.setattr(_schema_client, "get_model_fields_fast", lambda target, db: ["id", "name"])
    monkeypatch.setattr(_schema_client, "is_fast_path_eligible", lambda db: True)
    os.environ["OMA_ODOO_DB_DUPLICATE_FOR_BUILD"] = "odoo16_dev"

    files = {
        "models/models.py": (
            "from odoo import models, fields\n\nclass WasteContainer(models.Model):\n"
            "    _inherit = ['waste.container']\n"
        ),
    }
    findings = [
        ReviewFinding(
            location="models/models.py", severity="blocking",
            explanation=(
                "Uses _inherit = ['waste.container'] but the model waste.container does not "
                "exist in standard Odoo or dependencies, causing a crash on module load."
            ),
        ),
    ]
    out = _filter_hallucinated_inherit_target_missing_findings(findings, files)
    assert out[0].severity == "info"
    print("PASS: Task 024's real round-2 shape (list-form _inherit) is also correctly downgraded")


def test_hallucinated_inherit_target_missing_filter_never_touches_a_genuinely_nonexistent_target(monkeypatch):
    """Regression guard: a genuinely nonexistent _inherit target (the live lookup honestly returns
    None) must never be downgraded -- this is the real, correct defect shape
    `_validate_inherit_target_resolved` exists to catch on Build's own side."""
    import tools_odoo.odoo_schema_client as _schema_client
    from specialists.code_review.specialist import (
        ReviewFinding,
        _filter_hallucinated_inherit_target_missing_findings,
    )

    monkeypatch.setattr(_schema_client, "get_model_fields_fast", lambda target, db: None)
    monkeypatch.setattr(_schema_client, "is_fast_path_eligible", lambda db: True)
    os.environ["OMA_ODOO_DB_DUPLICATE_FOR_BUILD"] = "odoo16_dev"

    files = {
        "models/models.py": (
            "from odoo import models\n\nclass X(models.Model):\n"
            "    _inherit = 'totally.invented.ghost.model'\n"
        ),
    }
    findings = [
        ReviewFinding(
            location="models/models.py", severity="blocking",
            explanation=(
                "The model `totally.invented.ghost.model` does not exist in standard Odoo or "
                "the provided dependencies."
            ),
        ),
    ]
    out = _filter_hallucinated_inherit_target_missing_findings(findings, files)
    assert out[0].severity == "blocking", (
        "a genuinely nonexistent _inherit target (live lookup returns None) must never be "
        "downgraded -- this is a real, correct defect, not a hallucination"
    )
    print("PASS: a genuinely nonexistent _inherit target is never falsely downgraded")


def test_hallucinated_inherit_target_missing_filter_never_touches_an_unrelated_model_claim(monkeypatch):
    """The claimed nonexistent model name must match THIS generation's own real _inherit target --
    a finding naming some other, unrelated model must never be touched."""
    import tools_odoo.odoo_schema_client as _schema_client
    from specialists.code_review.specialist import (
        ReviewFinding,
        _filter_hallucinated_inherit_target_missing_findings,
    )

    monkeypatch.setattr(_schema_client, "get_model_fields_fast", lambda target, db: ["id", "name"])
    monkeypatch.setattr(_schema_client, "is_fast_path_eligible", lambda db: True)
    os.environ["OMA_ODOO_DB_DUPLICATE_FOR_BUILD"] = "odoo16_dev"

    files = {
        "models/models.py": (
            "from odoo import models\n\nclass X(models.Model):\n"
            "    _inherit = 'waste.container'\n"
        ),
    }
    findings = [
        ReviewFinding(
            location="models/models.py", severity="blocking",
            explanation="The model `some.other.unrelated.model` does not exist and is referenced elsewhere.",
        ),
    ]
    out = _filter_hallucinated_inherit_target_missing_findings(findings, files)
    assert out[0].severity == "blocking", (
        "a finding naming a DIFFERENT model than this generation's own real _inherit target "
        "must never be touched"
    )
    print("PASS: a finding naming an unrelated model is never falsely downgraded")


def test_hallucinated_inherit_target_missing_filter_downgrades_generic_unnamed_claim_task034_real_shape(monkeypatch):
    """Real, confirmed bug found live (2026-08-06, Phase 30 root-cause pass, task034, real task_id
    e26b6ce4-b373-4dab-836f-6b28cca8b89c): Code-Review's real, raw finding was "Models use
    _inherit instead of _name, inheriting from non-existent standard models instead of defining
    new custom models" -- never naming a specific model, so the original `model X does not exist`
    claim regex never matched it. Both real `_inherit` targets in this exact generation
    (payment.term.cust, payment.term.cust.line) genuinely, currently existed live and this same
    round's own install had already succeeded against them -- a confirmed hallucination that cost
    a real task an otherwise-clean pass.
    """
    import tools_odoo.odoo_schema_client as _schema_client
    from specialists.code_review.specialist import (
        ReviewFinding,
        _filter_hallucinated_inherit_target_missing_findings,
    )

    monkeypatch.setattr(_schema_client, "get_model_fields_fast", lambda target, db: ["id", "name"])
    monkeypatch.setattr(_schema_client, "is_fast_path_eligible", lambda db: True)
    os.environ["OMA_ODOO_DB_DUPLICATE_FOR_BUILD"] = "odoo16_dev"

    files = {
        "models/models.py": (
            "from odoo import api, fields, models\n\n"
            "class PaymentTermCust(models.Model):\n"
            "    _inherit = 'payment.term.cust'\n"
            "    _description = 'Custom Payment Term'\n\n\n"
            "class PaymentTermCustLine(models.Model):\n"
            "    _inherit = 'payment.term.cust.line'\n"
            "    _description = 'Custom Payment Term Line'\n"
        ),
    }
    findings = [
        ReviewFinding(
            location="models/models.py", severity="blocking",
            explanation=(
                "Models use _inherit instead of _name, inheriting from non-existent standard "
                "models instead of defining new custom models."
            ),
        ),
    ]
    out = _filter_hallucinated_inherit_target_missing_findings(findings, files)
    assert out[0].severity == "info", (
        "a generic, unnamed 'inheriting from non-existent standard models' claim must be "
        "downgraded once every real _inherit target in this generation is confirmed live"
    )

    # But a genuine defect where one target is NOT real must never be downgraded.
    monkeypatch.setattr(
        _schema_client, "get_model_fields_fast",
        lambda target, db: None if target == "payment.term.cust.line" else ["id", "name"],
    )
    out2 = _filter_hallucinated_inherit_target_missing_findings(findings, files)
    assert out2[0].severity == "blocking", (
        "if any real _inherit target can't be confirmed live, the generic claim must stay blocking"
    )
    print("PASS: a generic, unnamed non-existent-inherit-targets claim is downgraded once every "
          "real target is confirmed live, stays blocking if any target is uncertain")


def test_hallucinated_prior_round_base_model_broken_filter_downgrades_task042_real_shape(monkeypatch):
    import tools_odoo.odoo_schema_client as _schema_client
    from specialists.code_review.specialist import (
        ReviewFinding,
        _filter_hallucinated_prior_round_base_model_broken_findings,
    )

    def fake_get_model_fields_fast(target, db):
        assert target == "project.fieldjob"
        return ["id", "name", "state", "project_id"]  # genuinely resolves right now
    monkeypatch.setattr(_schema_client, "get_model_fields_fast", fake_get_model_fields_fast)
    monkeypatch.setattr(_schema_client, "is_fast_path_eligible", lambda db: True)
    os.environ["OMA_ODOO_DB_DUPLICATE_FOR_BUILD"] = "odoo16_dev"

    files = {
        "models/models.py": (
            "from odoo import models, fields\n\nclass ProjectFieldjob(models.Model):\n"
            "    _inherit = 'project.fieldjob'\n\n"
            "    batch_invoice_allowed = fields.Boolean(default=True)\n"
        ),
    }
    findings = [
        ReviewFinding(
            location="models/models.py", severity="blocking",
            explanation=(
                "Class 'ProjectFieldjob' declares _inherit but not _name, which is correct for "
                "inheritance, but the previous attempt error indicates the base class "
                "'project.fieldjob' itself was missing _name/_inherit, causing registry build "
                "failure; this diff assumes the base model is fixed, but if the base model is "
                "still broken, this module will fail to load."
            ),
        ),
    ]
    out = _filter_hallucinated_prior_round_base_model_broken_findings(findings, files)
    assert out[0].severity == "info", (
        f"Task 042's real false 'base model still broken from a previous attempt' claim, "
        f"contradicted by a real live-registry lookup, must be downgraded -- got severity "
        f"{out[0].severity!r}"
    )
    print("PASS: Task 042's real false prior-round-base-model-broken claim is correctly "
          "downgraded as a hallucination")


def test_hallucinated_prior_round_base_model_broken_filter_never_touches_a_genuinely_broken_target(monkeypatch):
    """Regression guard: if the live lookup honestly can't resolve the inherit target (a
    genuinely broken/nonexistent base model), the finding must never be downgraded."""
    import tools_odoo.odoo_schema_client as _schema_client
    from specialists.code_review.specialist import (
        ReviewFinding,
        _filter_hallucinated_prior_round_base_model_broken_findings,
    )

    monkeypatch.setattr(_schema_client, "get_model_fields_fast", lambda target, db: None)
    monkeypatch.setattr(_schema_client, "is_fast_path_eligible", lambda db: True)
    os.environ["OMA_ODOO_DB_DUPLICATE_FOR_BUILD"] = "odoo16_dev"

    files = {
        "models/models.py": (
            "from odoo import models\n\nclass X(models.Model):\n"
            "    _inherit = 'totally.invented.ghost.model'\n"
        ),
    }
    findings = [
        ReviewFinding(
            location="models/models.py", severity="blocking",
            explanation=(
                "The previous attempt error indicates the base class 'totally.invented.ghost.model' "
                "itself was missing _name/_inherit, causing registry build failure."
            ),
        ),
    ]
    out = _filter_hallucinated_prior_round_base_model_broken_findings(findings, files)
    assert out[0].severity == "blocking", (
        "a genuinely unresolvable inherit target (live lookup returns None) must never be "
        "downgraded -- could be a real, correct defect"
    )
    print("PASS: a genuinely unresolvable base model claim is never falsely downgraded")


def test_hallucinated_prior_round_base_model_broken_filter_never_touches_unrelated_findings(monkeypatch):
    """A blocking finding that doesn't use "previous attempt"/"prior round" phrasing at all --
    e.g. a genuine, unrelated defect -- must never be touched by this filter."""
    import tools_odoo.odoo_schema_client as _schema_client
    from specialists.code_review.specialist import (
        ReviewFinding,
        _filter_hallucinated_prior_round_base_model_broken_findings,
    )

    monkeypatch.setattr(_schema_client, "get_model_fields_fast", lambda target, db: ["id", "name"])
    monkeypatch.setattr(_schema_client, "is_fast_path_eligible", lambda db: True)
    os.environ["OMA_ODOO_DB_DUPLICATE_FOR_BUILD"] = "odoo16_dev"

    files = {
        "models/models.py": (
            "from odoo import models\n\nclass X(models.Model):\n"
            "    _inherit = 'project.fieldjob'\n"
        ),
    }
    findings = [
        ReviewFinding(
            location="models/models.py", severity="blocking",
            explanation="The invoice line creation does not include necessary fields like 'account_id'.",
        ),
    ]
    out = _filter_hallucinated_prior_round_base_model_broken_findings(findings, files)
    assert out[0].severity == "blocking", (
        "a genuine, unrelated blocking finding with no prior-attempt phrasing must never be touched"
    )
    print("PASS: an unrelated genuine blocking finding is never falsely downgraded")


def test_hallucinated_empty_security_csv_filter_downgrades_header_only_csv():
    """Real bug found live (2026-07-25, task 005's 3rd fresh submission):
    Code-Review blocked a round with "The access control file is empty,
    which will cause a module installation error or leave the model
    without proper access rights" -- for a task that defines NO new model
    at all (a pure `@api.onchange` extension of an existing one). A
    header-only security CSV (zero data rows) for exactly this shape is
    the objectively CORRECT, documented behavior this project's own
    `build_deterministic_security_csv()` already encodes -- access rights
    for an inherited model come from whichever module first defined it,
    never re-granted per extension module. Code-Review has no way to know
    this project-specific convention from the diff alone.
    """
    from specialists.code_review.specialist import (
        ReviewFinding,
        _filter_hallucinated_empty_security_csv_findings,
    )

    files = {
        "/mnt/extra-addons/oma_x/models/models.py": (
            "from odoo import models, fields, api\n\nclass ProjectFieldjob(models.Model):\n"
            "    _inherit = 'project.fieldjob'\n\n"
            "    @api.onchange('project_id')\n"
            "    def _onchange_project_id(self):\n"
            "        pass\n"
        ),
        "/mnt/extra-addons/oma_x/security/ir.model.access.csv": (
            "id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink\n"
        ),
    }
    findings = [
        ReviewFinding(
            location="security/ir.model.access.csv", severity="blocking",
            explanation=(
                "The access control file is empty, which will cause a module installation "
                "error or leave the model without proper access rights."
            ),
        ),
    ]
    out = _filter_hallucinated_empty_security_csv_findings(findings, files)
    assert out[0].severity == "info", (
        f"expected the header-only-CSV claim to be downgraded for a no-new-model task -- "
        f"got severity {out[0].severity!r}"
    )
    print("PASS: the empty-security-csv filter correctly recognizes a header-only CSV as "
          "objectively correct for a task defining no new model")

    # A genuinely new model -- must NEVER downgrade (real access rows are actually required).
    files_new_model = {
        "/mnt/extra-addons/oma_x/models/models.py": (
            "from odoo import models, fields\n\nclass NewThing(models.Model):\n"
            "    _name = 'oma.new.thing'\n"
        ),
        "/mnt/extra-addons/oma_x/security/ir.model.access.csv": (
            "id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink\n"
        ),
    }
    out_new_model = _filter_hallucinated_empty_security_csv_findings(findings, files_new_model)
    assert out_new_model[0].severity == "blocking", (
        "must never downgrade when a genuinely new model is defined -- real access rows are required"
    )
    print("PASS: never downgrades when a new model is genuinely defined")


def test_hallucinated_empty_security_csv_filter_exempts_decomposed_new_model_round():
    """Real, confirmed bug found live (2026-07-28, Phase 28C,
    `school_student` task): the "genuinely new model always needs real
    access rows" rule is only true OUTSIDE a decomposed round that
    explicitly defers the access-row constraint to a later round.
    Build's own sibling validator (`_validate_security_csv_covers_new_
    models()`) already correctly relaxes this exact rule for exactly
    this shape (Odoo does not require an access row at INSTALL time,
    only at runtime ACL-check time) -- but this filter never knew about
    that relaxation, so a round scoped to ONLY `student_model_fields`
    (with `security_groups`/`record_rules` explicitly named as NOT yet
    in scope) kept getting blocked for a header-only CSV that Build's
    own side had already, correctly, deliberately left empty.
    """
    from specialists.code_review.specialist import (
        ReviewFinding,
        _filter_hallucinated_empty_security_csv_findings,
    )

    decomposed_goal = (
        "Build school_student. This round's own NEW focus is ONLY: 'student_model_fields'. "
        "The following constraints are NOT yet in scope for this round and must NOT be "
        "implemented even partially: ['student_views', 'menu_structure', 'security_groups', "
        "'record_rules', 'computed_age_field', 'demo_data', 'automated_tests']."
    )
    files = {
        "models/models.py": (
            "from odoo import models, fields\n\nclass Student(models.Model):\n"
            "    _name = 'school.student'\n    name = fields.Char(required=True)\n"
        ),
        "security/ir.model.access.csv": (
            "id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink\n"
        ),
    }
    findings = [
        ReviewFinding(
            location="security/ir.model.access.csv", severity="blocking",
            explanation="Access control file is empty, preventing any user from reading or writing to the model.",
        ),
    ]
    out = _filter_hallucinated_empty_security_csv_findings(findings, files, decomposed_goal)
    assert out[0].severity == "info", (
        f"expected the header-only-CSV claim to be downgraded for a decomposed round that "
        f"explicitly defers security to a later round -- got {out[0].severity!r}"
    )
    print("PASS: a decomposed round's own genuinely new model correctly exempts the "
          "header-only-CSV claim when security is explicitly deferred to a later round")

    # A genuinely new model on a PLAIN (non-decomposed) task must still be required.
    out_plain = _filter_hallucinated_empty_security_csv_findings(findings, files, "")
    assert out_plain[0].severity == "blocking", (
        "a plain, non-decomposed task's genuinely new model must still require real access rows"
    )
    print("PASS: a plain, non-decomposed task's new-model access-row requirement is untouched")


def test_hallucinated_empty_security_csv_filter_downgrades_false_emptiness_claim_with_real_data_row():
    """General hardening test, justified on its own logical merits -- NOT tied to a specific
    confirmed real task case. (Correction, 2026-08-05: an earlier version of this test/docstring
    cited this as "Task 019's real Code-Review finding," sourced from a quote in the deep-dive
    report's own "Additional depth found in final audit" section for Task 019. the project owner independently
    traced that quote and found it does not appear anywhere in Task 019's real event data -- it
    belongs to two unrelated, days-old tasks (2026-07-23 and 2026-07-26) that were conflated into
    Task 019's write-up by an error in the deep-dive report's own analysis pipeline, not something
    OMA's real Code-Review specialist ever said about Task 019. The `models.py`/`security_csv`
    fixture content below IS Task 019's own real generated code (independently pulled directly from
    Redis trace history, task_id 1b992e55-7547-46a6-ae75-f9b0ba668a4e) -- only the Code-Review
    finding TEXT paired with it here is illustrative/synthetic, not a real quote from this task.)

    The underlying reasoning this test verifies holds regardless of provenance: the pre-existing
    guard ("has real data rows -- not the header-only case, leave the finding alone") is too
    cautious for this specific claim shape -- `_EMPTY_SECURITY_CSV_CLAIM_RE` only ever matches
    wording that unambiguously asserts the file IS empty -- when that exact claim is made and the
    file demonstrably has real data, the claim is definitionally false, not "possibly about
    something else," no matter which task or context produced it.
    """
    from specialists.code_review.specialist import (
        ReviewFinding,
        _filter_hallucinated_empty_security_csv_findings,
    )

    files = {
        "models/models.py": (
            "from odoo import models, fields\n\nclass ProjectFieldjob(models.Model):\n"
            "    _inherit = 'project.fieldjob'\n\n"
            "    customer_grouping_rule = fields.Boolean(string='Customer Grouping Rule')\n"
        ),
        "security/ir.model.access.csv": (
            "id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink\n"
            "access_project_fieldjob_customer_grouping,project.fieldjob.customer.grouping,"
            "model_project_fieldjob,base.group_user,1,1,1,0\n"
        ),
    }
    findings = [
        ReviewFinding(
            location="security/ir.model.access.csv", severity="blocking",
            explanation=(
                "The proposed change introduces a field that already exists in the base model, "
                "which is a blocking issue. The empty access control file is a major concern, "
                "and the manifest reference to it is a minor issue."
            ),
        ),
    ]
    out = _filter_hallucinated_empty_security_csv_findings(findings, files)
    assert out[0].severity == "info", (
        f"an unambiguous 'empty access control file' claim, contradicted by a genuinely present "
        f"real data row, must be downgraded -- got severity {out[0].severity!r}"
    )
    print("PASS: an unambiguous false-emptiness claim (contradicted by a genuine data row) is "
          "correctly downgraded as a hallucination")


def test_hallucinated_empty_security_csv_filter_never_downgrades_an_unrelated_finding_about_present_csv():
    """A finding that does NOT actually claim the file is empty (doesn't match
    `_EMPTY_SECURITY_CSV_CLAIM_RE` at all) must never be touched by this filter, even when the CSV
    has real data rows -- confirms the fix above is scoped precisely to the emptiness claim, not a
    blanket "CSV has content, downgrade anything mentioning it" rule.
    """
    from specialists.code_review.specialist import (
        ReviewFinding,
        _filter_hallucinated_empty_security_csv_findings,
    )

    files = {
        "models/models.py": "from odoo import models\n\nclass X(models.Model):\n    _inherit = 'x'\n",
        "security/ir.model.access.csv": (
            "id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink\n"
            "access_x,x,model_x,base.group_user,1,1,1,0\n"
        ),
    }
    findings = [
        ReviewFinding(
            location="security/ir.model.access.csv", severity="blocking",
            explanation="The access row grants write permission but this field should be read-only for regular users.",
        ),
    ]
    out = _filter_hallucinated_empty_security_csv_findings(findings, files)
    assert out[0].severity == "blocking", (
        "a genuine, unrelated finding about a CSV that has real content must never be touched by "
        "this filter, regardless of the data-row-presence check added for the emptiness-claim fix"
    )
    print("PASS: an unrelated, genuine finding about a non-empty CSV is never touched")

    # This round's OWN focus being the security work itself must never be exempted either.
    security_focus_goal = (
        "Build school_student. This round's own NEW focus is ONLY: 'security_groups'. "
        "The following constraints are NOT yet in scope for this round and must NOT be "
        "implemented even partially: []."
    )
    out_security_focus = _filter_hallucinated_empty_security_csv_findings(findings, files, security_focus_goal)
    assert out_security_focus[0].severity == "blocking", (
        "a round whose OWN focus IS the security work must never be exempted"
    )
    print("PASS: a round whose own focus is the security work itself is never exempted")


def test_hallucinated_sequence_pattern_filter_downgrades_standard_idiom():
    """Real bug found live (2026-07-25, task 006's 2nd fresh submission):
    Code-Review blocked the SAME two findings identically across 3
    straight rounds -- "Redefining the 'name' field in an inherited
    model overrides the original field definition, potentially breaking
    existing functionality" and "The create() method override uses
    'New' as a fallback which contradicts the requirement that...the
    sequence should always be used" -- for code that, verified directly
    via Gitea, was the textbook-correct, standard Odoo sequence-
    assignment idiom (the same pattern real Odoo core modules like
    sale.order/account.move use themselves). Both complaints misread a
    deliberate, idiomatic design as a bug.
    """
    from specialists.code_review.specialist import (
        ReviewFinding,
        _filter_hallucinated_sequence_pattern_findings,
    )

    files = {
        "/mnt/extra-addons/oma_x/models/models.py": (
            "from odoo import models, fields, api\n\nclass ProjectFieldjob(models.Model):\n"
            "    _inherit = 'project.fieldjob'\n\n"
            "    name = fields.Char(\n"
            "        string='Reference',\n        readonly=True,\n        copy=False,\n"
            "        default='New',\n    )\n\n"
            "    @api.model_create_multi\n"
            "    def create(self, vals_list):\n"
            "        for vals in vals_list:\n"
            "            if vals.get('name', 'New') == 'New':\n"
            "                vals['name'] = self.env['ir.sequence'].next_by_code(\n"
            "                    'project.fieldjob') or 'New'\n"
            "        return super().create(vals_list)\n"
        ),
    }
    findings = [
        ReviewFinding(
            location="models.py", severity="blocking",
            explanation=(
                "Redefining the 'name' field in an inherited model overrides the original field "
                "definition, potentially breaking existing functionality or dependencies on the "
                "original field."
            ),
        ),
        ReviewFinding(
            location="models.py", severity="blocking",
            explanation=(
                "The create() method override uses 'New' as a fallback which contradicts the "
                "requirement that the user should never have to type this and the sequence "
                "should always be used."
            ),
        ),
    ]
    out = _filter_hallucinated_sequence_pattern_findings(findings, files)
    assert all(f.severity == "info" for f in out), (
        f"expected both findings about the standard sequence idiom to be downgraded -- got "
        f"{[f.severity for f in out]!r}"
    )
    print("PASS: both hallucinated findings about the standard Odoo sequence-assignment idiom "
          "are downgraded")

    # The pattern genuinely absent -- must never downgrade.
    plain_files = {
        "/mnt/extra-addons/oma_x/models/models.py": (
            "from odoo import models, fields\n\nclass X(models.Model):\n    _inherit = 'x'\n"
        ),
    }
    out_absent = _filter_hallucinated_sequence_pattern_findings(findings, plain_files)
    assert out_absent[0].severity == "blocking", (
        "must never downgrade when the standard sequence pattern is genuinely absent"
    )
    print("PASS: never downgrades when the standard sequence pattern is genuinely absent")


def test_hallucinated_redundant_line_filter_downgrades_claim_against_real_single_occurrence():
    """Real bug found live (2026-08-09, task 07141af5-9a4e-41b6-93ea-8b7af04fea9c's flagship
    run, service_ticket_model node, the third real hallucination shape found in this same live
    incident): "Redundant list comprehension 'vals_list = [dict(v) for v in vals_list]' is
    unnecessary and was flagged in previous rounds as a generation artifact." -- a genuinely,
    deterministically-repeated line from an EARLIER round was correctly flagged and, after an
    explicit correction note, genuinely fixed down to a single occurrence (confirmed live via
    direct Gitea inspection of the exact commit Code-Review was reviewing) -- yet Code-Review
    repeated the identical claim against that now-fixed content.
    """
    from specialists.code_review.specialist import (
        ReviewFinding,
        _filter_hallucinated_redundant_line_findings_against_real_code,
    )

    files = {
        "oma_build_a_complete_field_ab52b7f8/models/models.py": (
            "from odoo import models, fields, api\n\n"
            "class ServiceTicket(models.Model):\n"
            "    _name = 'oma.service.ticket'\n\n"
            "    name = fields.Char()\n\n"
            "    @api.model_create_multi\n"
            "    def create(self, vals_list):\n"
            "        vals_list = [dict(v) for v in vals_list]\n"
            "        for vals in vals_list:\n"
            "            if not vals.get('name'):\n"
            "                vals['name'] = self.env['ir.sequence'].next_by_code('oma.service.ticket')\n"
            "        return super().create(vals_list)\n"
        ),
    }
    findings = [
        ReviewFinding(
            location="models.py", severity="blocking",
            explanation=(
                "Redundant list comprehension 'vals_list = [dict(v) for v in vals_list]' is "
                "unnecessary and was flagged in previous rounds as a generation artifact."
            ),
        ),
    ]
    out = _filter_hallucinated_redundant_line_findings_against_real_code(findings, files)
    assert out[0].severity == "info", (
        f"expected the false 'redundant line' claim to be downgraded once the real code is "
        f"confirmed to contain the line exactly once -- got {out[0].severity!r}"
    )
    print("PASS: the real, confirmed false 'redundant list comprehension' claim against "
          "genuinely single-occurrence code is downgraded, closing the real live gap found on "
          "task 07141af5's service_ticket_model node")

    # A genuinely repeated line -- must never be downgraded.
    genuinely_duplicated_files = {
        "oma_x/models/models.py": (
            "def create(self, vals_list):\n"
            "    vals_list = [dict(v) for v in vals_list]\n"
            "    vals_list = [dict(v) for v in vals_list]\n"
            "    return super().create(vals_list)\n"
        ),
    }
    out_genuine = _filter_hallucinated_redundant_line_findings_against_real_code(
        findings, genuinely_duplicated_files,
    )
    assert out_genuine[0].severity == "blocking", (
        "a genuinely repeated line must never be downgraded"
    )
    print("PASS: never downgrades when the quoted line genuinely still appears more than once")


def test_hallucinated_redundant_line_filter_downgrades_unquoted_parenthesized_claim():
    """Real bug found live (2026-08-09, task 07141af5-9a4e-41b6-93ea-8b7af04fea9c's flagship
    run, service_ticket_model node, round 33, escalated to ask_operator): the REAL Code-Review
    finding text was "Redundant list comprehension lines (vals_list = [dict(v) for v in
    vals_list]) suggest incomplete cleanup or generation artifact." -- the offending snippet is
    wrapped in PARENTHESES, not quotes, so `_QUOTED_CODE_SNIPPET_RE` (which only matches
    quote-delimited text) extracts nothing, `snippets` is empty, and the old code fell through
    to `else: filtered.append(f)`, keeping this false claim blocking forever even though the
    real committed content (confirmed live via direct Gitea inspection of the per-node branch
    commit Code-Review was actually reviewing) had already been fixed down to a single
    occurrence. This is the exact real wording that caused a real, needless human escalation.
    """
    from specialists.code_review.specialist import (
        ReviewFinding,
        _filter_hallucinated_redundant_line_findings_against_real_code,
    )

    files = {
        "oma_build_a_complete_field_ab52b7f8/models/models.py": (
            "from odoo import models, fields, api\n\n"
            "class ServiceTicket(models.Model):\n"
            "    _name = 'oma.service.ticket'\n\n"
            "    @api.model_create_multi\n"
            "    def create(self, vals_list):\n"
            "        vals_list = [dict(v) for v in vals_list]\n"
            "        for vals in vals_list:\n"
            "            if not vals.get('name'):\n"
            "                vals['name'] = self.env['ir.sequence'].next_by_code('oma.service.ticket')\n"
            "        return super().create(vals_list)\n"
        ),
    }
    findings = [
        ReviewFinding(
            location="models.py", severity="blocking",
            explanation=(
                "Redundant list comprehension lines (vals_list = [dict(v) for v in vals_list]) "
                "suggest incomplete cleanup or generation artifact."
            ),
        ),
    ]
    out = _filter_hallucinated_redundant_line_findings_against_real_code(findings, files)
    assert out[0].severity == "info", (
        f"expected the real, live, unquoted parenthesized false claim to be downgraded -- "
        f"got {out[0].severity!r}"
    )
    print("PASS: the real, live unquoted/parenthesized 'redundant list comprehension' claim is "
          "downgraded, closing the real ask_operator escalation gap found on task 07141af5's "
          "service_ticket_model node round 33")

    # A genuine duplicate, described the same unquoted/parenthesized way -- must stay blocking.
    genuinely_duplicated_files = {
        "oma_x/models/models.py": (
            "def create(self, vals_list):\n"
            "    vals_list = [dict(v) for v in vals_list]\n"
            "    vals_list = [dict(v) for v in vals_list]\n"
            "    return super().create(vals_list)\n"
        ),
    }
    out_genuine = _filter_hallucinated_redundant_line_findings_against_real_code(
        findings, genuinely_duplicated_files,
    )
    assert out_genuine[0].severity == "blocking", (
        "a genuine duplicate line must never be downgraded, even when the claim is unquoted"
    )
    print("PASS: never downgrades an unquoted claim when a real consecutive duplicate line "
          "genuinely exists somewhere in the file")


def test_hallucinated_overwrite_filter_downgrades_claim_against_guarded_assignment():
    """Real bug found live (2026-08-09, task 07141af5-9a4e-41b6-93ea-8b7af04fea9c's flagship
    run, service_ticket_model node, round 33): Code-Review's second blocking finding in the
    same escalated round claimed "The create method override for oma.service.ticket does not
    handle the case where 'name' is already provided, potentially overwriting it with a
    sequence number" -- but the real generated code already reads
    `if not vals.get('name'): vals['name'] = ...`, the exact idiom that definitionally never
    overwrites an already-truthy value (the same idiom the equipment model's own create()
    override already used successfully). A second, ungrounded LLM-judge call invented a
    contradiction that isn't in the real code, contributing to a needless ask_operator escalation.
    """
    from specialists.code_review.specialist import (
        ReviewFinding,
        _filter_hallucinated_overwrite_findings_against_guarded_assignment,
    )

    files = {
        "oma_build_a_complete_field_ab52b7f8/models/models.py": (
            "from odoo import models, fields, api\n\n"
            "class ServiceTicket(models.Model):\n"
            "    _name = 'oma.service.ticket'\n\n"
            "    @api.model_create_multi\n"
            "    def create(self, vals_list):\n"
            "        for vals in vals_list:\n"
            "            if not vals.get('name'):\n"
            "                vals['name'] = self.env['ir.sequence'].next_by_code('oma.service.ticket')\n"
            "        return super().create(vals_list)\n"
        ),
    }
    findings = [
        ReviewFinding(
            location="models.py", severity="blocking",
            explanation=(
                "The create method override for oma.service.ticket does not handle the case "
                "where 'name' is already provided, potentially overwriting it with a sequence "
                "number if not careful."
            ),
        ),
    ]
    out = _filter_hallucinated_overwrite_findings_against_guarded_assignment(findings, files)
    assert out[0].severity == "info", (
        f"expected the false 'overwrite' claim to be downgraded once the real code is "
        f"confirmed to guard the assignment with `if not vals.get('name')` -- "
        f"got {out[0].severity!r}"
    )
    print("PASS: the real, confirmed false 'overwrite already-provided value' claim against a "
          "genuinely guarded assignment is downgraded, closing the real live gap found on task "
          "07141af5's service_ticket_model node round 33")

    # A genuinely unguarded assignment -- the claim must stay blocking.
    unguarded_files = {
        "oma_x/models/models.py": (
            "def create(self, vals_list):\n"
            "    for vals in vals_list:\n"
            "        vals['name'] = self.env['ir.sequence'].next_by_code('oma.service.ticket')\n"
            "    return super().create(vals_list)\n"
        ),
    }
    out_genuine = _filter_hallucinated_overwrite_findings_against_guarded_assignment(
        findings, unguarded_files,
    )
    assert out_genuine[0].severity == "blocking", (
        "a genuinely unguarded, unconditional overwrite must never be downgraded"
    )
    print("PASS: never downgrades when the named field genuinely has no guard in the real code")


def test_hallucinated_self_excluded_scope_filter_downgrades_self_contradicting_finding():
    """Real bug found live (2026-08-09, task 07141af5-9a4e-41b6-93ea-8b7af04fea9c's flagship
    run, service_ticket_model node): "The model 'oma.service.ticket' is defined but the task
    goal requires a 'state' field and workflow buttons, which are explicitly excluded from this
    round's scope, creating a contradiction in the model's completeness." -- the finding names,
    in its own words, that the missing content is explicitly excluded from this round's scope,
    then blocks the round for that same absence. Self-refuting on its own text, no file
    inspection needed.
    """
    from specialists.code_review.specialist import (
        ReviewFinding,
        _filter_hallucinated_findings_blocking_on_self_named_excluded_scope,
    )

    findings = [
        ReviewFinding(
            location="models.py", severity="blocking",
            explanation=(
                "The model 'oma.service.ticket' is defined but the task goal requires a "
                "'state' field and workflow buttons, which are explicitly excluded from this "
                "round's scope, creating a contradiction in the model's completeness for its "
                "intended purpose."
            ),
        ),
    ]
    out = _filter_hallucinated_findings_blocking_on_self_named_excluded_scope(findings)
    assert out[0].severity == "info", (
        f"expected the self-contradicting finding to be downgraded -- got {out[0].severity!r}"
    )
    print("PASS: a finding that names its own missing content as explicitly excluded from "
          "scope, then blocks for that same absence, is downgraded, closing the real live gap "
          "found on task 07141af5's service_ticket_model node")

    # A genuinely different, non-self-contradicting finding must never be touched.
    genuine_findings = [
        ReviewFinding(
            location="models.py", severity="blocking",
            explanation="The 'name' field is missing a required=True attribute.",
        ),
    ]
    out_genuine = _filter_hallucinated_findings_blocking_on_self_named_excluded_scope(genuine_findings)
    assert out_genuine[0].severity == "blocking", (
        "a genuine, non-self-contradicting finding must never be downgraded"
    )
    print("PASS: never touches a genuinely different finding that doesn't contradict itself")


def test_hallucinated_already_satisfied_carried_forward_content_filter_downgrades_real_shape():
    """Real, confirmed bug found live (2026-08-10, task e65381cc, parts_consumed_relation node):
    "The diff adds views, menus, and actions for oma.service.ticket and oma.equipment, which
    violates the explicit round constraint to only implement 'parts_consumed_relation' and
    exclude 'ticket_views_menu' and 'equipment_views_menu'" -- both are real, genuinely
    ALREADY-satisfied constraints from earlier rounds of this same task; carrying their own real
    content forward unchanged is correct, required behavior (the goal text's own words: "MUST
    continue to hold"), never a scope violation, regardless of how the complaint is phrased.
    """
    from specialists.code_review.specialist import (
        ReviewFinding,
        _filter_hallucinated_findings_blocking_on_already_satisfied_carried_forward_content,
    )

    goal = (
        "The following constraints are ALREADY satisfied by earlier work on this same task and "
        "MUST continue to hold -- do not remove or weaken them: ['ticket_views_menu', "
        "'equipment_views_menu']."
    )
    findings = [
        ReviewFinding(
            location="views/views.xml", severity="blocking",
            explanation=(
                "The diff adds views, menus, and actions for oma.service.ticket and "
                "oma.equipment, which violates the explicit round constraint to only implement "
                "'parts_consumed_relation' and exclude 'ticket_views_menu' and "
                "'equipment_views_menu'."
            ),
        ),
        ReviewFinding(
            location="models/models.py", severity="blocking",
            explanation="The compute method has a genuine off-by-one error in its loop bound.",
        ),
    ]
    out = _filter_hallucinated_findings_blocking_on_already_satisfied_carried_forward_content(findings, goal)
    assert out[0].severity == "info", (
        f"expected the already-satisfied carried-forward finding to be downgraded -- got {out[0].severity!r}"
    )
    assert out[1].severity == "blocking", "an unrelated genuine finding must never be touched"
    print("PASS: a finding treating carried-forward, already-satisfied content as a scope "
          "violation is downgraded; an unrelated genuine finding is left untouched")


def test_hallucinated_not_yet_in_scope_carried_forward_content_filter_downgrades_real_shape():
    """Real, confirmed bug found live (2026-08-10, task e65381cc, parts_consumed_relation node):
    "The diff includes security records for oma.service.ticket, which violates the explicit
    round constraint to exclude 'ticket_access_restriction'" -- ticket_access_restriction is a
    real, genuine NOT-yet-in-scope constraint (the one that will eventually remove this exact
    access row), but its own future work being not-yet-done doesn't forbid this round's diff
    from containing the row at all -- it's legitimately unchanged, pre-existing content.
    """
    from specialists.code_review.specialist import (
        ReviewFinding,
        _filter_hallucinated_findings_blocking_on_not_yet_in_scope_carried_forward_content,
    )

    goal = (
        "The following constraints are NOT yet in scope for this round and must NOT be "
        "implemented even partially: ['ticket_access_restriction', 'parts_active_domain']."
    )
    findings = [
        ReviewFinding(
            location="security/ir.model.access.csv", severity="blocking",
            explanation=(
                "The diff includes security records for oma.service.ticket, which violates the "
                "explicit round constraint to exclude 'ticket_access_restriction'."
            ),
        ),
        ReviewFinding(
            location="models/models.py", severity="blocking",
            explanation="The compute method has a genuine off-by-one error in its loop bound.",
        ),
    ]
    out = _filter_hallucinated_findings_blocking_on_not_yet_in_scope_carried_forward_content(findings, goal)
    assert out[0].severity == "info", (
        f"expected the not-yet-in-scope carried-forward finding to be downgraded -- got {out[0].severity!r}"
    )
    assert out[1].severity == "blocking", "an unrelated genuine finding must never be touched"
    print("PASS: a finding treating carried-forward content for a not-yet-in-scope constraint's "
          "own future work as a scope violation is downgraded; an unrelated genuine finding is "
          "left untouched")


def test_hallucinated_not_yet_in_scope_filter_also_catches_concrete_artifact_name_not_just_label():
    """Real, confirmed FOLLOW-UP bug found live (2026-08-10, task e65381cc,
    ticket_status_decoration node, immediately after the fix above): a second finding on the
    SAME round -- "The task explicitly states that the base.group_user access row for
    oma.service.ticket must be removed, but the diff still retains it" -- named the real,
    concrete ARTIFACT (`base.group_user`) the not-yet-in-scope constraint will eventually
    touch, never the constraint's own internal graph LABEL (`ticket_access_restriction`), so
    the label-only match alone never caught it. Fixed by also parsing `_compose_focus_goal_
    text()`'s own "Concretely, this means: ...even though they already exist and their fields
    are real: [...]" sentence (the same real sentence Bug 56's `_GOAL_FORBIDDEN_MODELS_
    SENTENCE_RE` already parses for a different filter) as an additional, real ground-truth
    anchor.
    """
    from specialists.code_review.specialist import (
        ReviewFinding,
        _filter_hallucinated_findings_blocking_on_not_yet_in_scope_carried_forward_content,
    )

    goal = (
        "The following constraints are NOT yet in scope for this round and must NOT be "
        "implemented even partially -- no fields, methods, or view elements for any of them "
        "yet, no matter how related they seem: ['ticket_access_restriction']. Concretely, "
        "this means: do not add or reference ANY view, form, tree, menu, action, or field "
        "for these real models in this round, even though they already exist and their "
        "fields are real: ['base.group_user', 'ir.model.access.csv']."
    )
    findings = [
        ReviewFinding(
            location="security/ir.model.access.csv", severity="blocking",
            explanation=(
                "The task explicitly states that the base.group_user access row for "
                "oma.service.ticket must be removed, but the diff still retains it."
            ),
        ),
        ReviewFinding(
            location="models/models.py", severity="blocking",
            explanation="The compute method has a genuine off-by-one error in its loop bound.",
        ),
    ]
    out = _filter_hallucinated_findings_blocking_on_not_yet_in_scope_carried_forward_content(findings, goal)
    assert out[0].severity == "info", (
        f"expected the concrete-artifact-named carried-forward finding to be downgraded -- got {out[0].severity!r}"
    )
    assert out[1].severity == "blocking", "an unrelated genuine finding must never be touched"
    print("PASS: a finding naming the real concrete artifact (not the constraint's own internal "
          "label) tied to a not-yet-in-scope constraint is still downgraded; an unrelated genuine "
          "finding is left untouched")


def test_hallucinated_carried_forward_out_of_scope_content_filter_downgrades_real_shape():
    """Real, confirmed bug found live (2026-08-10, task e65381cc, ticket_views_menu node): the
    OPPOSITE-direction sibling of the self-named-excluded-scope filter above -- this finding
    complains that merely CARRYING FORWARD unchanged, already-legitimate security CSV content
    (from an already-satisfied constraint, or content this round's own new views still need)
    counts as "touching" out-of-scope work. Every round's diff necessarily represents the full
    current state of shared files, never just this round's own new additions -- inclusion alone
    is never itself a defect. This exact real finding text, verbatim.
    """
    from specialists.code_review.specialist import (
        ReviewFinding,
        _filter_hallucinated_findings_blocking_on_carried_forward_out_of_scope_content,
    )

    findings = [
        ReviewFinding(
            location="security/ir.model.access.csv", severity="blocking",
            explanation=(
                "The diff includes access rows for oma.service.ticket and oma.equipment, but "
                "'ticket_access_restriction' is explicitly NOT-yet-in-scope and must NOT be "
                "implemented, and 'equipment_views_menu' is already satisfied, so touching "
                "security records is out of scope."
            ),
        ),
        ReviewFinding(
            location="models/models.py", severity="blocking",
            explanation="The compute method has a genuine off-by-one error in its loop bound.",
        ),
    ]
    out = _filter_hallucinated_findings_blocking_on_carried_forward_out_of_scope_content(findings)
    assert out[0].severity == "info", (
        f"expected the carried-forward-content finding to be downgraded -- got {out[0].severity!r}"
    )
    assert out[1].severity == "blocking", (
        "an unrelated genuine finding with no not-yet-in-scope/already-satisfied naming must "
        "never be touched"
    )
    print("PASS: a finding treating mere inclusion of unchanged, legitimate content as an "
          "out-of-scope violation is downgraded; an unrelated genuine finding is left untouched")


def test_hallucinated_fabricated_forbidden_model_quote_filter_downgrades_real_shape():
    """Real, confirmed bug found live (2026-08-10, task e65381cc, ticket_views_menu node,
    immediately after two earlier fixes this same session/night correctly removed
    'oma.service.ticket' from the real, current goal text's own forbidden-models list):
    Code-Review still blocked the round quoting "the task explicitly states 'do not add or
    reference ANY view, form, tree, menu, action, or field for these real models in this
    round... oma.service.ticket'" -- a quote that does not exist anywhere in the actual, current
    goal text at all. A genuine fabrication attributing a specific forbidden model to this
    pipeline's own known exact template sentence.
    """
    from specialists.code_review.specialist import (
        ReviewFinding,
        _filter_hallucinated_fabricated_forbidden_model_quote_findings,
    )

    goal = (
        "This round's own NEW focus is ONLY: 'ticket_views_menu'. Concretely, this means: do "
        "not add or reference ANY view, form, tree, menu, action, or field for these real "
        "models in this round, even though they already exist and their fields are real: "
        "['base.group_user', 'ir.model.access.csv', 'parts_consumed_ids', 'product.product']."
    )
    findings = [
        ReviewFinding(
            location="views/views.xml", severity="blocking",
            explanation=(
                "The diff adds views and menus for oma.service.ticket, but the task explicitly "
                "states 'do not add or reference ANY view, form, tree, menu, action, or field "
                "for these real models in this round... oma.service.ticket'."
            ),
        ),
        ReviewFinding(
            location="security/ir.model.access.csv", severity="blocking",
            explanation=(
                "The diff includes an access row for 'ir.model.access.csv', but the task "
                "explicitly states 'do not add or reference ANY view, form, tree, menu, "
                "action, or field for these real models in this round... ir.model.access.csv', "
                "which is genuinely forbidden."
            ),
        ),
        ReviewFinding(
            location="models/models.py", severity="blocking",
            explanation="The compute method has a genuine off-by-one error in its loop bound.",
        ),
    ]
    out = _filter_hallucinated_fabricated_forbidden_model_quote_findings(findings, goal)
    assert out[0].severity == "info", (
        f"expected the fabricated 'oma.service.ticket' quote to be downgraded -- got {out[0].severity!r}"
    )
    assert out[1].severity == "blocking", (
        "a finding correctly citing a model that IS genuinely, currently forbidden must never "
        "be downgraded"
    )
    assert out[2].severity == "blocking", (
        "an unrelated genuine finding with no template quote at all must never be touched"
    )
    print("PASS: a fabricated forbidden-model quote is downgraded; a genuinely correct "
          "citation and an unrelated finding are both left untouched")


def test_hallucinated_ir_rule_permission_capped_filter_downgrades_real_shape():
    """Real bug found live (2026-08-09, task 07141af5-9a4e-41b6-93ea-8b7af04fea9c's flagship
    run, ticket_access_rights node): after fixing the SAME wrong "any ir.rule-referenced group
    needs perm_unlink=1" assumption in four separate deterministic Build-side functions,
    Code-Review -- a separate LLM judge -- kept independently reaching the exact same false
    conclusion regardless of the real, already-correct generated content: "The ir.rule
    'rule_service_ticket_technician_own' restricts access for 'group_field_technician', but
    the access.csv row for this group grants perm_unlink=0; ir.rules can only ever narrow
    access... never grant it." This premise is only true for an elevation-gate rule
    (domain_force=[]); a row-restricting rule (domain_force references user.id) narrows WHICH
    ROWS are visible, never implying delete capability.
    """
    from specialists.code_review.specialist import (
        ReviewFinding,
        _filter_hallucinated_ir_rule_permission_capped_findings,
    )

    security_xml = (
        '<?xml version="1.0" encoding="utf-8"?>\n<odoo>\n'
        '<record id="group_field_technician" model="res.groups">\n'
        '<field name="name">Field Technician</field>\n</record>\n'
        '<record id="rule_service_ticket_technician_own" model="ir.rule">\n'
        '<field name="name">Service Ticket: Technicians only see their own</field>\n'
        '<field name="model_id" ref="oma_x.model_oma_service_ticket"/>\n'
        "<field name=\"domain_force\">[('technician_id', '=', user.id)]</field>\n"
        '<field name="groups" eval="[(4, ref(\'oma_x.group_field_technician\'))]"/>\n'
        '</record>\n</odoo>'
    )
    files = {"security/security.xml": security_xml}
    finding = ReviewFinding(
        location="security.xml", severity="blocking",
        explanation=(
            "The ir.rule 'rule_service_ticket_technician_own' restricts access for "
            "'group_field_technician', but the access.csv row for this group grants "
            "perm_unlink=0; ir.rules can only ever narrow access already granted via "
            "access.csv, never grant it, so this rule is likely pointless for the "
            "permission it implies."
        ),
    )
    out = _filter_hallucinated_ir_rule_permission_capped_findings([finding], files)
    assert out[0].severity == "info", (
        f"expected the false 'rule is pointless' claim to be downgraded -- got {out[0].severity!r}"
    )
    print("PASS: the real, confirmed false 'ir.rule can only narrow, perm_unlink=0 makes it "
          "pointless' claim against a genuine row-restricting rule is downgraded, closing the "
          "real live gap found on task 07141af5's ticket_access_rights node")

    elevation_xml = (
        '<odoo><record id="rule_admin_delete" model="ir.rule">'
        '<field name="domain_force">[]</field>'
        '<field name="groups" eval="[(4, ref(\'oma_x.group_admin\'))]"/>'
        '</record></odoo>'
    )
    genuine_finding = ReviewFinding(
        location="security.xml", severity="blocking",
        explanation=(
            "The ir.rule references 'group_admin', but the access.csv row grants "
            "perm_unlink=0; ir.rules can only ever narrow access, never grant it, so this "
            "rule is pointless."
        ),
    )
    out_genuine = _filter_hallucinated_ir_rule_permission_capped_findings(
        [genuine_finding], {"security/security.xml": elevation_xml},
    )
    assert out_genuine[0].severity == "blocking", (
        "a genuine elevation-gate rule's own denied perm_unlink must never be downgraded"
    )
    print("PASS: never downgrades a genuine elevation-gate rule's own denied perm_unlink claim")


def test_hallucinated_decoration_string_literal_filter_downgrades_real_shape():
    """Real bug found live (2026-08-09, task 07141af5-9a4e-41b6-93ea-8b7af04fea9c's flagship
    run, ticket_list_view node): the SAME hallucination just fixed on the deterministic Build
    side (a string literal like 'resolved' inside `state == 'resolved'` mistaken for a second
    field reference) recurred independently in Code-Review's own separate LLM call: "decoration-
    success=\"state == 'resolved'\" references field 'resolved' which does not exist on the
    model... wait, let me re-read the history." The real views_xml/models.py show 'resolved' is
    only ever a quoted string literal value, never an actual field.
    """
    from specialists.code_review.specialist import (
        ReviewFinding,
        _filter_hallucinated_decoration_string_literal_findings,
    )

    real_views_xml = (
        '<odoo><record id="view_oma_service_ticket_tree" model="ir.ui.view">'
        '<field name="arch" type="xml">'
        '<tree decoration-success="state == \'resolved\'" decoration-danger="state == \'closed\'">'
        '<field name="name"/><field name="state"/>'
        '</tree></field></record></odoo>'
    )
    real_models_py = (
        "class ServiceTicket(models.Model):\n"
        "    _name = 'oma.service.ticket'\n"
        "    state = fields.Selection([('new', 'New'), ('resolved', 'Resolved'), "
        "('closed', 'Closed')])\n"
    )
    files = {"views/views.xml": real_views_xml, "models/models.py": real_models_py}
    finding = ReviewFinding(
        location="views.xml", severity="blocking",
        explanation=(
            "decoration-success=\"state == 'resolved'\" references field 'resolved' which does "
            "not exist on the model; the field is named 'state' and the value is 'resolved', "
            "but the expression syntax is incorrect for Odoo 16 decoration attributes, wait, "
            "let me re-read the history."
        ),
    )
    out = _filter_hallucinated_decoration_string_literal_findings([finding], files)
    assert out[0].severity == "info", (
        f"expected the false decoration string-literal claim to be downgraded -- "
        f"got {out[0].severity!r}"
    )
    print("PASS: the real, confirmed false 'resolved is a missing field' claim against a "
          "decoration-* string literal is downgraded, closing the real live gap found on task "
          "07141af5's ticket_list_view node")

    genuine_finding = ReviewFinding(
        location="views.xml", severity="blocking",
        explanation=(
            "The <field name=\"priority\"/> element references field 'priority' which does not "
            "exist on the model."
        ),
    )
    out_genuine = _filter_hallucinated_decoration_string_literal_findings([genuine_finding], files)
    assert out_genuine[0].severity == "blocking", (
        "a genuinely missing field referenced via <field name=...>, not a decoration literal, "
        "must never be downgraded"
    )
    print("PASS: never downgrades a genuine missing-field finding unrelated to decoration "
          "string literals")


def test_hallucinated_self_reports_already_resolved_filter_downgrades_real_shape():
    """Real bug found live (2026-08-09, task 07141af5-9a4e-41b6-93ea-8b7af04fea9c's flagship
    run, ticket_list_view node, immediately after the decoration string-literal hallucination
    was corrected via a note): Code-Review's NEXT finding literally said "...is actually
    correct syntax for Odoo 16... which is a false positive that has been resolved by Operator's
    arbitration" and STILL kept severity 'blocking'. The finding's own text affirms no real
    issue exists; only the severity field never caught up.
    """
    from specialists.code_review.specialist import (
        ReviewFinding,
        _filter_hallucinated_findings_that_self_report_as_already_resolved,
    )

    finding = ReviewFinding(
        location="views.xml", severity="blocking",
        explanation=(
            "decoration-success=\"state == 'resolved'\" syntax is invalid for Odoo 16 tree "
            "views; decoration attributes must use the format decoration-success=\"state == "
            "'resolved'\" is actually correct syntax for Odoo 16, but the previous review "
            "flagged it as referencing a missing field 'resolved' instead of the value "
            "'resolved' of field 'state', which is a false positive that has been resolved by "
            "Operator's arbitration."
        ),
    )
    out = _filter_hallucinated_findings_that_self_report_as_already_resolved([finding])
    assert out[0].severity == "info", (
        f"expected the self-declared-false-positive finding to be downgraded -- "
        f"got {out[0].severity!r}"
    )
    print("PASS: a finding that explicitly declares itself a false positive/already resolved "
          "but keeps blocking severity is downgraded, closing the real live gap found on task "
          "07141af5's ticket_list_view node")

    genuine_finding = ReviewFinding(
        location="views.xml", severity="blocking",
        explanation="The 'priority' field referenced in the view is invalid and does not exist.",
    )
    out_genuine = _filter_hallucinated_findings_that_self_report_as_already_resolved([genuine_finding])
    assert out_genuine[0].severity == "blocking", (
        "a genuine finding with no self-declared resolution language must never be downgraded"
    )
    print("PASS: never downgrades a genuine finding that never declares itself already resolved")


def test_hallucinated_duplicate_method_filter_downgrades_claim_against_real_single_definition():
    """Real bug found live (2026-08-09, task 07141af5-9a4e-41b6-93ea-8b7af04fea9c's flagship
    run, service_ticket_model node, the same live incident as the sibling missing-content
    filter): "The 'create' method in ServiceTicket is defined twice in the previous attempt
    history, leading to silent overwriting of logic; current diff shows it defined once but
    contains redundant list comprehension lines that suggest incomplete cleanup..." -- the
    exact real, live rewording that slipped past the narrower, phrasing-specific
    `_filter_hallucinated_findings_citing_stale_previous_attempt_error_text` filter, against a
    round whose real, committed models.py genuinely defines `create` exactly once inside
    ServiceTicket (and once, separately, inside Equipment -- correctly recognized as fine by
    the same-day fix to specialists/build/specialist.py's own
    `_validate_no_duplicate_method_definitions`).
    """
    from specialists.code_review.specialist import (
        ReviewFinding,
        _filter_hallucinated_duplicate_method_findings_against_real_code,
    )

    files = {
        "oma_build_a_complete_field_ab52b7f8/models/models.py": (
            "from odoo import models, fields, api\n\n"
            "class ServiceTicket(models.Model):\n"
            "    _name = 'oma.service.ticket'\n\n"
            "    name = fields.Char()\n\n"
            "    @api.model\n"
            "    def create(self, vals):\n"
            "        if 'name' not in vals:\n"
            "            vals['name'] = self.env['ir.sequence'].next_by_code('oma.service.ticket')\n"
            "        return super().create(vals)\n\n\n"
            "class Equipment(models.Model):\n"
            "    _name = 'oma.equipment'\n\n"
            "    @api.model\n"
            "    def create(self, vals):\n"
            "        if 'tracking_number' not in vals:\n"
            "            vals['tracking_number'] = self.env['ir.sequence'].next_by_code('oma.equipment')\n"
            "        return super().create(vals)\n"
        ),
    }
    findings = [
        ReviewFinding(
            location="models.py", severity="blocking",
            explanation=(
                "The 'create' method in ServiceTicket is defined twice in the previous "
                "attempt history, leading to silent overwriting of logic; current diff shows "
                "it defined once but contains redundant list comprehension lines that suggest "
                "incomplete cleanup or potential generation artifact."
            ),
        ),
    ]
    out = _filter_hallucinated_duplicate_method_findings_against_real_code(findings, files)
    assert out[0].severity == "info", (
        f"expected the false 'defined twice' claim to be downgraded once the real code is "
        f"confirmed to define it exactly once per class -- got {out[0].severity!r}"
    )
    print("PASS: the real, confirmed false 'create defined twice' claim against genuinely "
          "single-definition-per-class code is downgraded, closing the real live gap found on "
          "task 07141af5's service_ticket_model node")

    # A genuine duplicate WITHIN one class -- must never be downgraded.
    genuinely_duplicated_files = {
        "oma_x/models/models.py": (
            "from odoo import models, fields, api\n\n"
            "class ServiceTicket(models.Model):\n"
            "    _name = 'oma.service.ticket'\n\n"
            "    @api.model\n"
            "    def create(self, vals):\n"
            "        return super().create(vals)\n\n"
            "    @api.model\n"
            "    def create(self, vals):\n"
            "        vals['x'] = 1\n"
            "        return super().create(vals)\n"
        ),
    }
    out_genuine = _filter_hallucinated_duplicate_method_findings_against_real_code(
        findings, genuinely_duplicated_files,
    )
    assert out_genuine[0].severity == "blocking", (
        "a genuine same-class duplicate must never be downgraded"
    )
    print("PASS: never downgrades when the named method is genuinely duplicated within the "
          "same class")

    # Real, confirmed follow-up bug found live (2026-08-10, ticket_workflow_and_logging node):
    # the identical claim shape, but with a BARE (unquoted) method name -- "Method
    # action_bulk_close is defined twice in the ServiceTicket class (lines 68 and 105)" -- which
    # _QUOTED_IDENTIFIER_RE alone could never match.
    files_single_bulk_close = {
        "oma_build_a_complete_field_ab52b7f8/models/models.py": (
            "from odoo import models, fields\n\n"
            "class ServiceTicket(models.Model):\n"
            "    _name = 'oma.service.ticket'\n\n"
            "    def action_bulk_close(self):\n"
            "        self.write({'state': 'closed'})\n"
        ),
    }
    bare_findings = [
        ReviewFinding(
            location="models.py", severity="blocking",
            explanation=(
                "Method action_bulk_close is defined twice in the ServiceTicket class "
                "(lines 68 and 105), causing the first definition to be silently overwritten "
                "and losing functionality."
            ),
        ),
    ]
    out_bare = _filter_hallucinated_duplicate_method_findings_against_real_code(
        bare_findings, files_single_bulk_close,
    )
    assert out_bare[0].severity == "info", (
        f"expected the bare-phrasing false 'defined twice' claim to be downgraded -- got "
        f"{out_bare[0].severity!r}"
    )
    print("PASS: a bare (unquoted) 'defined twice' claim against genuinely single-definition "
          "code is also downgraded")


def test_hallucinated_missing_content_filter_downgrades_claim_against_present_content():
    """Real bug found live (2026-08-09, task 07141af5-9a4e-41b6-93ea-8b7af04fea9c's flagship
    run, service_ticket_model node): after an earlier round's real content-preservation bug
    (data/sequences.xml genuinely dropping the equipment model's own seq_equipment record) was
    fixed, Code-Review kept repeating the IDENTICAL claim -- "Missing the existing
    'seq_equipment' record required by the 'equipment_registry' constraint" -- against a LATER
    round whose real, committed data/sequences.xml genuinely, verifiably contained BOTH
    records (confirmed live via direct Gitea inspection of the exact commit Code-Review was
    reviewing).
    """
    from specialists.code_review.specialist import (
        ReviewFinding,
        _filter_hallucinated_missing_content_findings_against_real_files,
    )

    files = {
        "oma_build_a_complete_field_ab52b7f8/data/sequences.xml": (
            '<?xml version="1.0" encoding="utf-8"?>\n<odoo>\n'
            '  <record id="seq_equipment" model="ir.sequence">\n'
            "    <field name=\"name\">Equipment Sequence</field>\n"
            "    <field name=\"code\">oma.equipment</field>\n"
            "  </record>\n"
            '  <record id="seq_service_ticket" model="ir.sequence">\n'
            "    <field name=\"name\">Service Ticket Sequence</field>\n"
            "    <field name=\"code\">oma.service.ticket</field>\n"
            "  </record>\n</odoo>\n"
        ),
    }
    findings = [
        ReviewFinding(
            location="data/sequences.xml", severity="blocking",
            explanation=(
                "Missing the existing 'seq_equipment' record required by the "
                "'equipment_registry' constraint; the file must contain both the existing "
                "equipment sequence and the new service ticket sequence."
            ),
        ),
    ]
    out = _filter_hallucinated_missing_content_findings_against_real_files(findings, files)
    assert out[0].severity == "info", (
        f"expected the false 'missing seq_equipment' claim to be downgraded once the real "
        f"file is confirmed to already contain it -- got {out[0].severity!r}"
    )
    print("PASS: the real, confirmed false 'seq_equipment missing' claim against genuinely "
          "present content is downgraded, closing the real live gap found on task 07141af5's "
          "service_ticket_model node")

    # A genuinely missing identifier -- must never be downgraded.
    genuinely_missing_findings = [
        ReviewFinding(
            location="data/sequences.xml", severity="blocking",
            explanation="Missing the required 'seq_maintenance_log' record for the maintenance log sequence.",
        ),
    ]
    out_missing = _filter_hallucinated_missing_content_findings_against_real_files(
        genuinely_missing_findings, files,
    )
    assert out_missing[0].severity == "blocking", (
        "a claim naming an identifier that genuinely does not exist anywhere in the real "
        "files must never be downgraded"
    )
    print("PASS: never downgrades when the named identifier is genuinely absent from the "
          "real generated files")


def test_hallucinated_sequence_pattern_filter_downgrades_general_field_empty_string_claim():
    """Real bug found live (2026-08-07/08, task 657697fc's equipment_registry node): unlike
    the other two tests for this filter, this generation used a general, non-'name' field
    (tracking_number) for the guard+sequence-assignment idiom, and Code-Review's own
    hallucination filter did not recognize that idiom shape at all -- so it kept blocking a
    factually false claim, verbatim from the real failure: "The create method override does
    not handle the case where tracking_number is provided but is an empty string, leading to
    potential duplicate sequence generation or validation errors as flagged in previous
    rounds." The real create() override's own guard (`'tracking_number' not in vals or not
    vals['tracking_number']`) already treats an empty string as falsy and re-assigns it via
    the sequence -- the claim was simply false, verified directly against the real generated
    code, not taken on faith.
    """
    from specialists.code_review.specialist import (
        ReviewFinding,
        _filter_hallucinated_sequence_pattern_findings,
    )

    files = {
        "/mnt/extra-addons/oma_equipment/models/models.py": (
            "from odoo import models, fields, api\n\n"
            "class Equipment(models.Model):\n"
            "    _name = 'oma.equipment'\n"
            "    _description = 'Equipment Registry'\n\n"
            "    name = fields.Char(required=True, string='Name')\n"
            "    tracking_number = fields.Char(\n"
            "        string='Tracking Number', required=True, copy=False, index=True,\n"
            "    )\n\n"
            "    @api.model\n"
            "    def create(self, vals):\n"
            "        if 'tracking_number' not in vals or not vals['tracking_number']:\n"
            "            vals['tracking_number'] = self.env['ir.sequence'].next_by_code(\n"
            "                'oma.equipment')\n"
            "        return super().create(vals)\n"
        ),
    }
    findings = [
        ReviewFinding(
            location="models.py", severity="blocking",
            explanation=(
                "The create method override does not handle the case where tracking_number is "
                "provided but is an empty string, leading to potential duplicate sequence "
                "generation or validation errors as flagged in previous rounds."
            ),
        ),
    ]
    out = _filter_hallucinated_sequence_pattern_findings(findings, files)
    assert out[0].severity == "info", (
        f"expected the false 'does not handle empty string' claim to be downgraded once the "
        f"real guard is verified to already treat an empty string as falsy -- got "
        f"{out[0].severity!r}"
    )
    print("PASS: the real, confirmed false 'empty string not handled' claim against a general "
          "(non-'name') field sequence idiom is downgraded, closing the real live gap found on "
          "task 657697fc's equipment_registry node")

    # A guard that does NOT actually check emptiness (only presence) -- a genuine finding about
    # empty-string handling here would be TRUE, so the claim must never be downgraded.
    presence_only_files = {
        "/mnt/extra-addons/oma_equipment/models/models.py": (
            "from odoo import models, fields, api\n\n"
            "class Equipment(models.Model):\n"
            "    _name = 'oma.equipment'\n\n"
            "    tracking_number = fields.Char()\n\n"
            "    @api.model\n"
            "    def create(self, vals):\n"
            "        if 'tracking_number' not in vals:\n"
            "            vals['tracking_number'] = self.env['ir.sequence'].next_by_code(\n"
            "                'oma.equipment')\n"
            "        return super().create(vals)\n"
        ),
    }
    out_presence_only = _filter_hallucinated_sequence_pattern_findings(findings, presence_only_files)
    assert out_presence_only[0].severity == "blocking", (
        "a bare presence check ('not in vals') does not itself prove empty-string handling -- "
        "a genuine empty-string finding here must never be downgraded"
    )
    print("PASS: a bare key-presence guard (no falsy/empty-string check) never triggers the "
          "empty-string-safe downgrade, keeping the widening precise")


def test_hallucinated_syntax_error_filter_downgrades_claim_against_valid_code():
    """Real bug found live (2026-08-08, task 657697fc-f701-4932-a172-b0132da93cfa's flagship
    run, ticket_workflow node): round 1's real generated models.py genuinely had a SyntaxError
    (a dangling orphaned string fragment left over from a botched edit, confirmed via the real
    sandbox log_tail). Round 2 then genuinely, fully fixed it AND correctly added the whole
    ticket_workflow model/state machine -- confirmed directly against the real Gitea commit
    content, which compiles cleanly. Yet Code-Review repeated the IDENTICAL "SyntaxError:
    invalid syntax due to missing opening parenthesis and quote in _sql_constraints definition"
    claim against this now-valid code, wrongly failing round 2 -- and round 3, misled by that
    same false claim fed back as "CRITICAL FIX REQUIRED" round-history text, threw away round
    2's real, correct work entirely. This test uses round 2's exact real generated content.
    """
    from specialists.code_review.specialist import (
        ReviewFinding,
        _filter_hallucinated_syntax_error_findings_against_valid_code,
    )

    files = {
        "oma_build_a_complete_field_059d5f0e/models/models.py": (
            "from odoo import models, fields, api\n\n"
            "class Equipment(models.Model):\n"
            "    _name = 'oma.equipment'\n"
            "    _description = 'Equipment Registry'\n\n"
            "    name = fields.Char(required=True, string='Name')\n"
            "    tracking_number = fields.Char(string='Tracking Number', required=True, copy=False, index=True)\n\n"
            "    @api.model\n"
            "    def create(self, vals):\n"
            "        if 'tracking_number' not in vals or not vals.get('tracking_number'):\n"
            "            vals['tracking_number'] = self.env['ir.sequence'].next_by_code('oma.equipment')\n"
            "        return super().create(vals)\n\n"
            "    _sql_constraints = [\n"
            "        ('tracking_number_unique', 'UNIQUE(tracking_number)', 'Each equipment must have a unique tracking number!')\n"
            "    ]\n\n\n"
            "class ServiceTicket(models.Model):\n"
            "    _name = 'oma.service.ticket'\n"
            "    _description = 'Service Ticket'\n\n"
            "    name = fields.Char(string='Ticket Name', required=True)\n"
            "    state = fields.Selection([\n"
            "        ('new', 'New'), ('assigned', 'Assigned'), ('in_progress', 'In Progress'),\n"
            "        ('resolved', 'Resolved'), ('closed', 'Closed'),\n"
            "    ], string='State', default='new')\n\n"
            "    def action_start_work(self):\n"
            "        self.write({'state': 'in_progress'})\n\n"
            "    def action_resolve(self):\n"
            "        self.write({'state': 'resolved'})\n"
        ),
    }
    findings = [
        ReviewFinding(
            location="models.py", severity="blocking",
            explanation=(
                "The proposed change contains a critical syntax error that prevents module "
                "installation: SyntaxError: invalid syntax due to missing opening parenthesis "
                "and quote in _sql_constraints definition."
            ),
        ),
    ]
    out = _filter_hallucinated_syntax_error_findings_against_valid_code(findings, files)
    assert out[0].severity == "info", (
        f"expected the false syntax-error claim to be downgraded once every real .py file is "
        f"confirmed to compile() cleanly -- got {out[0].severity!r}"
    )
    print("PASS: the real, confirmed false 'SyntaxError' claim against genuinely valid, "
          "compiling code is downgraded, closing the real live gap found on task 657697fc's "
          "ticket_workflow node")

    # A genuinely broken file (round 1's real shape) -- must never be downgraded.
    broken_files = {
        "oma_build_a_complete_field_059d5f0e/models/models.py": (
            "from odoo import models, fields\n\n"
            "class Equipment(models.Model):\n"
            "    _name = 'oma.equipment'\n\n"
            "    def action_assign_ticket(self):\n"
            "        return {\n"
            "            'type': 'ir.actions.act_window',\n"
            "        }\n"
            "number_unique', 'UNIQUE(tracking_number)', 'Each equipment must have a unique tracking number!')\n"
            "    ]\n"
        ),
    }
    out_broken = _filter_hallucinated_syntax_error_findings_against_valid_code(findings, broken_files)
    assert out_broken[0].severity == "blocking", (
        "a genuinely broken .py file that fails to compile() must never be downgraded"
    )
    print("PASS: never downgrades when the code genuinely fails to compile")


def test_hallucinated_xml_malformed_filter_downgrades_claim_against_valid_xml():
    """The XML-file sibling of the syntax-error filter above. Real, confirmed bug found live
    (2026-08-10, task e65381cc, ticket_views_menu node, 3 consecutive rounds): Code-Review
    repeatedly claimed the generated views.xml was malformed ("the root <odoo> tag is not
    properly closed or the structure is invalid, causing a hard parse failure") -- but the real,
    actually-committed content (confirmed directly via read_last_validated_commit()) parses
    cleanly with Python's own standard xml.etree.ElementTree. This test uses that exact real
    content.
    """
    from specialists.code_review.specialist import (
        ReviewFinding,
        _filter_hallucinated_xml_malformed_findings_against_valid_xml,
    )

    files = {
        "oma_extend_the_existing_field_f2eb1288/views/views.xml": (
            '<?xml version="1.0" encoding="utf-8"?><odoo>'
            '<record id="view_oma_service_ticket_tree" model="ir.ui.view">'
            '<field name="name">oma.service.ticket.tree</field>'
            '<field name="model">oma.service.ticket</field>'
            '<field name="arch" type="xml"><tree string="Service Tickets">'
            '<field name="name"/><field name="state"/></tree></field></record>'
            "</odoo>"
        ),
    }
    findings = [
        ReviewFinding(
            location="views/views.xml", severity="blocking",
            explanation=(
                "The XML file is malformed; the root <odoo> tag is not properly closed or the "
                "structure is invalid, causing a hard parse failure in Odoo."
            ),
        ),
    ]
    out = _filter_hallucinated_xml_malformed_findings_against_valid_xml(findings, files)
    assert out[0].severity == "info", (
        f"expected the false XML-malformed claim to be downgraded once every real .xml file is "
        f"confirmed to parse cleanly -- got {out[0].severity!r}"
    )
    print("PASS: the real, confirmed false 'malformed XML' claim against genuinely valid, "
          "parseable XML is downgraded, closing the real live gap found on task e65381cc's "
          "ticket_views_menu node")

    # A genuinely broken XML file -- must never be downgraded.
    broken_files = {
        "oma_extend_the_existing_field_f2eb1288/views/views.xml": (
            '<?xml version="1.0" encoding="utf-8"?><odoo>'
            '<record id="view_oma_service_ticket_tree" model="ir.ui.view">'
            '<field name="name">oma.service.ticket.tree</field>'
            "</odoo>"  # missing closing </record> -- genuinely malformed
        ),
    }
    out_broken = _filter_hallucinated_xml_malformed_findings_against_valid_xml(findings, broken_files)
    assert out_broken[0].severity == "blocking", (
        "genuinely malformed XML that fails to parse must never be downgraded"
    )
    print("PASS: never downgrades when the XML genuinely fails to parse")


def test_hallucinated_sequence_not_transaction_safe_filter_downgrades_standard_idiom():
    """Real bug found live (2026-08-03, task006's own real re-test): Code-Review blocked
    "self.env['ir.sequence'].next_by_code() which is not transaction-safe and may produce
    duplicate references under concurrent writes" -- ir.sequence.next_by_code() genuinely IS
    transaction-safe by design; this is the standard, safe Odoo idiom, not a concurrency bug.
    """
    from specialists.code_review.specialist import (
        ReviewFinding,
        _filter_hallucinated_sequence_not_transaction_safe_findings,
    )

    files = {
        "/mnt/extra-addons/oma_x/models/models.py": (
            "from odoo import models, fields, api\n\nclass FieldjobRecord(models.Model):\n"
            "    _inherit = 'fieldjob.record'\n\n"
            "    reference = fields.Char(readonly=True, copy=False, index=True)\n\n"
            "    @api.model_create_multi\n"
            "    def create(self, vals_list):\n"
            "        records = super().create(vals_list)\n"
            "        for record in records:\n"
            "            if not record.reference:\n"
            "                record._generate_reference()\n"
            "        return records\n\n"
            "    def _generate_reference(self):\n"
            "        sequence = self.env['ir.sequence'].next_by_code('fieldjob.record.reference')\n"
            "        if sequence:\n"
            "            self.reference = sequence\n"
        ),
    }
    findings = [
        ReviewFinding(
            location="models.py", severity="blocking",
            explanation=(
                "The create() override calls _generate_reference() on each record, but "
                "_generate_reference() uses self.env['ir.sequence'].next_by_code() which is not "
                "transaction-safe and may produce duplicate references under concurrent writes "
                "or if the sequence is not properly configured with a lock."
            ),
        ),
    ]
    out = _filter_hallucinated_sequence_not_transaction_safe_findings(findings, files)
    assert out[0].severity == "info", (
        f"expected the 'not transaction-safe' claim about the standard sequence idiom to be "
        f"downgraded -- got {out[0].severity!r}"
    )
    print("PASS: task006's real 'not transaction-safe' hallucination about the standard "
          "ir.sequence.next_by_code() idiom is downgraded")

    # The standard pattern genuinely absent (a hand-rolled, genuinely non-atomic numbering
    # scheme) -- must never downgrade.
    plain_files = {
        "/mnt/extra-addons/oma_x/models/models.py": (
            "from odoo import models, fields\n\nclass X(models.Model):\n    _inherit = 'x'\n"
        ),
    }
    out_absent = _filter_hallucinated_sequence_not_transaction_safe_findings(findings, plain_files)
    assert out_absent[0].severity == "blocking", (
        "must never downgrade when the standard next_by_code() pattern is genuinely absent"
    )
    print("PASS: never downgrades when the standard sequence pattern is genuinely absent")


def test_hallucinated_selection_add_not_added_filter_downgrades_standard_idiom():
    """Real bug found live (2026-08-03, task011's own real re-test): Code-Review blocked "The
    'paid' state is not added to the 'state' field selection, so the model cannot store or
    display the new state" for code that genuinely DOES add it via the real, standard Odoo
    `selection_add=[('paid', 'Paid')]` idiom for extending an inherited Selection field.
    """
    from specialists.code_review.specialist import (
        ReviewFinding,
        _filter_hallucinated_selection_add_not_added_findings,
    )

    files = {
        "/mnt/extra-addons/oma_x/models/models.py": (
            "from odoo import api, fields, models\n\nclass ProjectFieldjob(models.Model):\n"
            "    _inherit = 'project.fieldjob'\n\n"
            "    state = fields.Selection(\n"
            "        selection_add=[('paid', 'Paid')],\n"
            "        string='Status',\n        required=True,\n        default='draft',\n    )\n"
        ),
    }
    findings = [
        ReviewFinding(
            location="models.py", severity="blocking",
            explanation=(
                "The 'paid' state is not added to the 'state' field selection, so the model "
                "cannot store or display the new state."
            ),
        ),
    ]
    out = _filter_hallucinated_selection_add_not_added_findings(findings, files)
    assert out[0].severity == "info", (
        f"expected the 'not added to selection' claim about the standard selection_add idiom to "
        f"be downgraded -- got {out[0].severity!r}"
    )
    print("PASS: task011's real 'not added to selection' hallucination about the standard "
          "selection_add=[...] idiom is downgraded")

    # The named option genuinely absent from any selection_add -- must never downgrade.
    plain_files = {
        "/mnt/extra-addons/oma_x/models/models.py": (
            "from odoo import models, fields\n\nclass X(models.Model):\n    _inherit = 'x'\n"
        ),
    }
    out_absent = _filter_hallucinated_selection_add_not_added_findings(findings, plain_files)
    assert out_absent[0].severity == "blocking", (
        "must never downgrade when the named option is genuinely absent from any selection_add"
    )
    print("PASS: never downgrades when the option is genuinely absent from selection_add")


def test_hallucinated_field_not_instantiated_filter_downgrades_a_real_call():
    """Real bug found live (2026-07-25, Phase 25B regression gate, task
    001's resubmission): Code-Review blocked a round with "fields.Text
    is a class, not an instance; must be fields.Text() to define a
    valid Odoo field" -- the real, actually-generated code (independently
    confirmed via the round's own diff) already read `special_
    instructions = fields.Text(string='Special Instructions')`, a
    completely correct, properly-instantiated field call. Genuinely
    unrelated to any Phase 25A/25B change (goal_facts extraction only
    ever feeds the deterministic field-omission autofix, never Code-
    Review or the generation prompt) -- the same hallucination-filter
    bug class already fixed 6 times this project, on a new claim shape.
    """
    from specialists.code_review.specialist import (
        ReviewFinding,
        _filter_hallucinated_field_not_instantiated_findings,
    )

    files = {
        "/mnt/extra-addons/oma_x/models/models.py": (
            "from odoo import models, fields\n\nclass CrmLead(models.Model):\n"
            "    _inherit = 'crm.lead'\n"
            "    special_instructions = fields.Text(string='Special Instructions')\n"
        ),
    }
    findings = [
        ReviewFinding(
            location="models.py", severity="blocking",
            explanation=(
                "fields.Text is a class, not an instance; must be fields.Text() to define a "
                "valid Odoo field."
            ),
        ),
    ]
    out = _filter_hallucinated_field_not_instantiated_findings(findings, files)
    assert out[0].severity == "info", (
        f"expected the false 'not instantiated' claim to be downgraded -- got {out[0].severity!r}"
    )
    print("PASS: a false 'field is a class, not an instance' claim is downgraded when the real "
          "code genuinely instantiates it")

    # Real, confirmed bug found live (2026-07-25, same task's VERY NEXT
    # resubmission, right after this filter was first deployed): Code-
    # Review reworded the identical false claim differently -- "uses
    # `fields.Text` instead of `fields.Text()`, causing a runtime
    # error" -- proving this claim's own prose isn't stable round to
    # round, same lesson every sibling filter already learned.
    reworded_findings = [
        ReviewFinding(
            location="models.py", severity="blocking",
            explanation="Field definition uses `fields.Text` instead of `fields.Text()`, causing a runtime error.",
        ),
    ]
    out_reworded = _filter_hallucinated_field_not_instantiated_findings(reworded_findings, files)
    assert out_reworded[0].severity == "info", (
        f"expected the reworded false claim to also be downgraded -- got {out_reworded[0].severity!r}"
    )
    print("PASS: the same false claim, reworded as 'uses X instead of X()', is also downgraded")

    # A genuine bare-class bug (no parentheses anywhere) must NEVER be downgraded.
    broken_files = {
        "/mnt/extra-addons/oma_x/models/models.py": (
            "from odoo import models, fields\n\nclass CrmLead(models.Model):\n"
            "    _inherit = 'crm.lead'\n"
            "    special_instructions = fields.Text\n"
        ),
    }
    out_broken = _filter_hallucinated_field_not_instantiated_findings(findings, broken_files)
    assert out_broken[0].severity == "blocking", (
        "must never downgrade a genuine bare-class instantiation bug"
    )
    print("PASS: a genuine bare-class (uninstantiated) field bug is never downgraded")


def test_hallucinated_module_registration_filter_downgrades_a_real_scaffold():
    """Phase 25D (2026-07-26): real, confirmed live bug -- task 005's own
    resubmission. Code-Review blocked round 5 with "the
    _onchange_project_id method is defined but the module's __init__.py
    does not import the models module correctly or the module is not
    installed/activated, leading to the logic not being applied" -- the
    real, actual __init__.py (independently confirmed by reading it
    directly off the real container) correctly imported `models`, and
    `models/__init__.py` correctly imported the real models file -- the
    exact standard scaffold structure this project's own pipeline always
    sets up and never touches afterward.
    """
    from specialists.code_review.specialist import (
        ReviewFinding,
        _filter_hallucinated_module_registration_findings,
    )

    correct_files = {
        "/mnt/extra-addons/oma_x/__init__.py": "from . import models\n",
        "/mnt/extra-addons/oma_x/models/__init__.py": "from . import models\n",
        "/mnt/extra-addons/oma_x/models/models.py": (
            "from odoo import api, models\n\nclass X(models.Model):\n    _inherit = 'x'\n"
        ),
    }
    findings = [
        ReviewFinding(
            location="__init__.py", severity="blocking",
            explanation=(
                "The _onchange_project_id method is defined but the module's __init__.py does not "
                "import the models module correctly or the module is not installed/activated, "
                "leading to the logic not being applied."
            ),
        ),
    ]
    out = _filter_hallucinated_module_registration_findings(findings, correct_files)
    assert out[0].severity == "info", (
        f"expected the false module-registration claim to be downgraded -- got {out[0].severity!r}"
    )
    print("PASS: a false module-registration claim is downgraded when the real __init__.py chain "
          "is confirmed intact")

    # The one case that must never be touched: a genuinely broken registration chain.
    broken_files = dict(correct_files)
    broken_files["/mnt/extra-addons/oma_x/__init__.py"] = "# nothing imported here\n"
    out_broken = _filter_hallucinated_module_registration_findings(findings, broken_files)
    assert out_broken[0].severity == "blocking", "must never downgrade a genuinely broken registration chain"
    print("PASS: a genuinely broken registration chain is never downgraded")


def test_hallucinated_own_module_collision_filter_downgrades_real_same_task_continuation():
    """Phase 28C (2026-07-28): real, confirmed live bug -- the
    `school_student` task's own live run. Build's own pre-write
    validator (`_validate_new_model_names_dont_collide()`) correctly
    exempts a real model this SAME task's own earlier round already,
    legitimately installed -- but Code-Review has no equivalent
    context at all, so it independently, plausibly concluded the exact
    same real model name was a foreign collision ("Model name
    'school.student' collides with existing model from module
    'oma_...', causing silent override") every round, an unrecoverable
    loop entirely on the review side.
    """
    import os
    from unittest.mock import patch

    os.environ["OMA_ODOO_DB_DUPLICATE_FOR_BUILD"] = "odoo16_dev"
    from specialists.code_review.specialist import (
        ReviewFinding,
        _filter_hallucinated_own_module_collision_findings,
    )

    files = {
        "models/models.py": (
            "from odoo import models, fields\n\n"
            "class SchoolStudent(models.Model):\n    _name = 'school.student'\n"
        ),
    }
    findings = [
        ReviewFinding(
            location="models/models.py", severity="blocking",
            explanation=(
                "Model name 'school.student' collides with existing model from module "
                "'oma_this_same_round_own_module', causing silent override."
            ),
        ),
    ]
    with patch(
        "tools_odoo.odoo_schema_client.is_fast_path_eligible", return_value=True,
    ), patch(
        "tools_odoo.odoo_schema_client.resolve_model_owner_module_fast",
        return_value="oma_this_same_round_own_module",
    ):
        out = _filter_hallucinated_own_module_collision_findings(
            findings, files, "oma_this_same_round_own_module",
        )
    assert out[0].severity == "info", (
        f"expected the false collision claim to be downgraded when the real owner IS this "
        f"same round's own module -- got {out[0].severity!r}"
    )
    print("PASS: a false own-module collision claim is downgraded when the real, live "
          "registry confirms the 'colliding' model is genuinely this round's own module")

    # The one case that must never be touched: a genuine foreign collision
    # (a DIFFERENT module than this round's own actually owns the real model).
    with patch(
        "tools_odoo.odoo_schema_client.is_fast_path_eligible", return_value=True,
    ), patch(
        "tools_odoo.odoo_schema_client.resolve_model_owner_module_fast",
        return_value="oma_a_completely_different_real_task",
    ):
        out_foreign = _filter_hallucinated_own_module_collision_findings(
            findings, files, "oma_this_same_round_own_module",
        )
    assert out_foreign[0].severity == "blocking", "must never downgrade a genuine foreign collision"
    print("PASS: a genuine foreign collision (real owner is NOT this round's own module) "
          "is never downgraded")


def test_hallucinated_out_of_scope_field_required_filter_downgrades_contradictory_claim():
    """Phase 28C (2026-07-28): real, confirmed live bug -- the mirror
    image of `_filter_hallucinated_scope_findings` (that one catches
    Code-Review INVENTING a scope exclusion that isn't real; this one
    catches Code-Review CONTRADICTING a scope exclusion that IS real).
    A round explicitly scoped to ONLY `student_model_fields`, with
    `computed_age_field` named as NOT yet in scope, still got blocked
    with "Field 'age' is missing... violating the round constraint
    'student_model_fields' which requires it" -- Code-Review's own
    independent judgment disagreeing with the SAME round's own explicit
    scope-exclusion instruction, an unwinnable contradiction Build's
    own generation could never resolve either way.
    """
    from specialists.code_review.specialist import (
        ReviewFinding,
        _filter_hallucinated_out_of_scope_field_required_findings,
    )

    goal = (
        "Build a complete school_student module. This round's own NEW focus is ONLY: "
        "'student_model_fields'. The following constraints are NOT yet in scope for this "
        "round and must NOT be implemented even partially: ['student_views', "
        "'menu_structure', 'security_groups', 'record_rules', 'computed_age_field', "
        "'demo_data', 'automated_tests']."
    )
    findings = [
        ReviewFinding(
            location="models/models.py", severity="blocking",
            explanation=(
                "Field 'age' is missing from the model definition, violating the round "
                "constraint 'student_model_fields' which requires it."
            ),
        ),
        ReviewFinding(
            location="models/models.py", severity="blocking",
            explanation="Missing required field 'age' as specified in the round constraint 'student_model_fields'.",
        ),
        ReviewFinding(
            location="security/ir.model.access.csv", severity="blocking",
            explanation="Access control file is empty, preventing any user from reading or writing to the model.",
        ),
        ReviewFinding(
            location="models/models.py", severity="blocking",
            explanation="Field 'student_id' is required but missing the unique constraint.",
        ),
    ]
    out = _filter_hallucinated_out_of_scope_field_required_findings(findings, goal)
    assert out[0].severity == "info" and out[1].severity == "info", (
        "both real-world phrasings of the contradictory 'age is missing' claim must be downgraded"
    )
    assert out[2].severity == "blocking", "an unrelated real finding (empty access control) must never be touched"
    assert out[3].severity == "blocking", (
        "a genuine missing-attribute claim for a field NOT named by any exclusion label "
        "('student_id' is in scope this round) must never be falsely downgraded"
    )
    print("PASS: a false 'field is missing' claim contradicting the round's own real scope "
          "exclusion is downgraded, while unrelated and genuine findings are never touched")


def test_hallucinated_out_of_scope_field_required_filter_catches_plain_english_decoration_claim():
    """Real, confirmed bug found live (2026-08-10, task e65381cc, ticket_views_menu node): a
    finding describing the not-yet-in-scope 'ticket_status_decoration' constraint in pure plain
    English ("show the status field with distinct colors per state using decoration-<state>
    attributes... lacks these decoration attributes") produced ZERO quoted or snake_case
    candidates at all -- `_extract_candidate_identifier_names()` never even offered "decoration"
    as a candidate to compare against the label. Also, `_MISSING_FIELD_GATE_RE` itself didn't
    recognize "lacks" as absence-is-a-defect language at all, a second, independent gap in the
    same real finding. Both are now fixed: bare occurrences of `_SINGLE_TOKEN_ALLOWED_MATCHES`'s
    own narrow Odoo-vocabulary terms (now including "decoration") are extracted as candidates
    even outside quotes/snake_case, and "lacks/lack" is recognized as absence language.
    """
    from specialists.code_review.specialist import (
        ReviewFinding,
        _filter_hallucinated_out_of_scope_field_required_findings,
    )

    goal = (
        "This round's own NEW focus is ONLY: 'ticket_views_menu'. The following constraints "
        "are NOT yet in scope for this round and must NOT be implemented even partially: "
        "['ticket_access_restriction', 'parts_consumed_relation', 'parts_active_domain', "
        "'ticket_status_decoration']."
    )
    findings = [
        ReviewFinding(
            location="views/views.xml", severity="blocking",
            explanation=(
                "The task goal explicitly requires the oma.service.ticket tree view to show the "
                "status field with distinct colors per state using decoration-<state> "
                "attributes, but the provided tree view lacks these decoration attributes."
            ),
        ),
        ReviewFinding(
            location="models/models.py", severity="blocking",
            explanation="Field 'student_id' is required but missing the unique constraint.",
        ),
    ]
    out = _filter_hallucinated_out_of_scope_field_required_findings(findings, goal)
    assert out[0].severity == "info", (
        f"expected the plain-English decoration claim to be downgraded -- got {out[0].severity!r}"
    )
    assert out[1].severity == "blocking", (
        "a genuine, unrelated missing-attribute claim must never be falsely downgraded"
    )
    print("PASS: a plain-English description of a not-yet-in-scope constraint (no quoted or "
          "snake_case identifier at all) is still correctly matched and downgraded")


def test_hallucinated_out_of_scope_field_required_filter_catches_plain_english_record_rule_claim():
    """Real, confirmed bug found live (2026-08-16, wave24_rr/hr.employee, real task_id
    4dc517a2-cf36-4afe-a606-84b15eaffe58): a blocking finding phrased in plain English ("the XML
    only defines the group and no record rule is present") for a round whose own real
    not-yet-in-scope list explicitly named 'record_rules' never produced a candidate matching
    that label -- the requirement was quoted as a full multi-word sentence (not a single quoted
    word `_ALL_QUOTED_IDENTIFIERS_RE` can extract) and "record rule" has no underscore
    (`_BARE_SNAKE_CASE_IDENTIFIER_RE` never matches it either). This escalated the whole task to
    Operator as a `gates_disagree` conflict between round-scope planning (which correctly deferred
    the record rule to a later round) and Code-Review (which read the full original goal and
    contradicted that deferral) -- a real, fixable filter gap, not a genuine need for human
    judgment. Fixed via the same narrow, explicit phrase-to-label-token mapping this file's
    `_SINGLE_TOKEN_ALLOWED_MATCHES` already uses for single bare terms.
    """
    from specialists.code_review.specialist import (
        ReviewFinding,
        _filter_hallucinated_out_of_scope_field_required_findings,
    )

    goal = (
        "This round's own NEW focus is ONLY: 'security_groups'. The following constraints "
        "are NOT yet in scope for this round and must NOT be implemented even partially: "
        "['record_rules']."
    )
    findings = [
        ReviewFinding(
            location="security/security.xml", severity="blocking",
            explanation=(
                "The task goal explicitly requires a record rule that applies ONLY to members "
                "of the 'HR Records Auditor' group, restricting them to see only employees "
                "belonging to their own company, but the XML only defines the group and no "
                "record rule is present."
            ),
        ),
        ReviewFinding(
            location="models/models.py", severity="blocking",
            explanation="Field 'employee_code' is required but missing the unique constraint.",
        ),
    ]
    out = _filter_hallucinated_out_of_scope_field_required_findings(findings, goal)
    assert out[0].severity == "info", (
        f"expected the deferred-to-later-round record rule claim to be downgraded -- got {out[0].severity!r}"
    )
    assert out[1].severity == "blocking", (
        "a genuine, unrelated missing-field claim must never be falsely downgraded"
    )
    print("PASS: a plain-English 'no record rule is present' claim for a round that explicitly "
          "defers 'record_rules' to a later round is correctly matched and downgraded")


def test_hallucinated_out_of_scope_field_required_filter_catches_is_defined_phrasing():
    """Real, confirmed bug found live (2026-08-16, wave24_rr/hr.employee, real task_id
    4dc517a2-cf36-4afe-a606-84b15eaffe58, continuation of the sibling 'is present' fix right
    above): pulled the ACTUAL raw Code-Review finding text from this task's own
    /api/trace_history event log rather than reconstructing it -- the real wording was "no
    record rule is defined in the XML", not "is present". `_MISSING_FIELD_GATE_RE` covered
    "present"/"missing"/"absent"/"empty"/"lacks" but not "defined" at all, so this exact real
    finding never reached the not-yet-in-scope downgrade logic -- confirmed via direct
    reproduction against the real trace text: it stayed 'blocking' before this fix, and this is
    the actual, confirmed root cause of that task's `gates_disagree`/`ask_operator` escalation (not
    a Operator policy question). Fixed by adding `\\bno\\b.{0,40}\\bis defined\\b|\\bnot defined\\b`
    to the gate regex, mirroring the existing `is present`/`not present` pair.
    """
    from specialists.code_review.specialist import (
        ReviewFinding,
        _filter_hallucinated_out_of_scope_field_required_findings,
    )

    goal = (
        "This round's own NEW focus is ONLY: 'hr_records_auditor_group'. The following "
        "constraints are NOT yet in scope for this round and must NOT be implemented even "
        "partially: ['hr_employee_access_row', 'hr_employee_record_rule']."
    )
    findings = [
        ReviewFinding(
            location="security/security.xml", severity="blocking",
            explanation=(
                "The task goal explicitly requires a record rule restricting the group to "
                "their own company, but no record rule is defined in the XML."
            ),
        ),
        ReviewFinding(
            location="models/models.py", severity="blocking",
            explanation="Method 'action_confirm' is not defined but is required by the view.",
        ),
    ]
    out = _filter_hallucinated_out_of_scope_field_required_findings(findings, goal)
    assert out[0].severity == "info", (
        f"expected the deferred-to-later-round 'is defined' claim to be downgraded -- got {out[0].severity!r}"
    )
    assert out[1].severity == "blocking", (
        "a genuine, unrelated 'not defined' claim (action_confirm, not named by any "
        "not-yet-in-scope label) must never be falsely downgraded"
    )
    print("PASS: the real 'no record rule is defined in the XML' finding text is correctly "
          "matched and downgraded, and an unrelated 'not defined' claim stays blocking")


def test_hallucinated_out_of_scope_field_required_filter_still_blocks_record_rule_claim_when_not_deferred():
    """A genuine 'record rule missing' finding for a round that does NOT list 'record_rules' as
    not-yet-in-scope (i.e. this round's own real focus IS the record rule) must never be
    downgraded -- the fix only ever applies when the deferral is real."""
    from specialists.code_review.specialist import (
        ReviewFinding,
        _filter_hallucinated_out_of_scope_field_required_findings,
    )

    goal = (
        "This round's own NEW focus is ONLY: 'record_rules'. The following constraints "
        "are NOT yet in scope for this round and must NOT be implemented even partially: "
        "['some_other_constraint']."
    )
    findings = [
        ReviewFinding(
            location="security/security.xml", severity="blocking",
            explanation="No record rule is present for the 'HR Records Auditor' group.",
        ),
    ]
    out = _filter_hallucinated_out_of_scope_field_required_findings(findings, goal)
    assert out[0].severity == "blocking", (
        "a genuine record-rule-missing finding must stay blocking when this round's own real "
        "focus IS the record rule (not deferred)"
    )
    print("PASS: a genuine record-rule-missing finding is not downgraded when record_rules is "
          "this round's own current focus, not a deferred later constraint")


def test_hallucinated_out_of_scope_field_required_filter_matches_by_token_overlap_not_just_substring():
    """Real, confirmed live bug (2026-08-07, task039, HUMAN_DECISION deep-push, 2 consecutive
    clean relaunches -- real task_ids 5381852f-8aea-4369-b1a0-6e5c6ea4992e and
    1646ddef-f5f9-4fba-9caf-bdbb878d20b7): the original filter only ever checked plain substring
    containment (`field_name in label`), and only ever extracted the FIRST quoted name adjacent to
    the literal word "field". Neither holds for this real finding: the ORIGINAL goal's own literal
    names (`is_external_service_token_expired`, `action_mark_refreshed`) are LONGER than, and not
    lexical substrings of, the round's own separately-generated not-yet-in-scope labels
    (`token_expired_computed`, `mark_refreshed_button`) -- confirmed live, both real relaunches,
    identical finding text. They DO share multiple significant word tokens once split on
    underscores. Also confirms the student_id/student_views false-positive risk (a single shared
    generic token) still correctly stays untouched.
    """
    from specialists.code_review.specialist import (
        ReviewFinding,
        _filter_hallucinated_out_of_scope_field_required_findings,
    )

    goal = (
        "Add credential-storage fields on res.users. This round's own NEW focus is ONLY: "
        "'credential_storage_fields'. The following constraints are NOT yet in scope for this "
        "round and must NOT be implemented even partially: ['mark_refreshed_button', "
        "'token_expired_computed']."
    )
    findings = [
        ReviewFinding(
            location="models/models.py", severity="blocking",
            explanation=(
                "The task goal explicitly requires a computed field "
                "'is_external_service_token_expired' and a method 'action_mark_refreshed', but "
                "the provided code only defines the two storage fields, omitting the required "
                "computed field and method entirely."
            ),
        ),
        ReviewFinding(
            location="models/models.py", severity="blocking",
            explanation="Field 'student_id' is required but missing the unique constraint.",
        ),
    ]
    out = _filter_hallucinated_out_of_scope_field_required_findings(findings, goal)
    assert out[0].severity == "info", (
        f"expected the real task039 finding (both names share >=2 significant tokens with a "
        f"real not-yet-in-scope label, despite neither being a lexical substring) to be "
        f"downgraded -- got {out[0].severity!r}"
    )
    assert out[1].severity == "blocking", (
        "student_id vs the unrelated label student_views shares only ONE generic token "
        "('student') -- this genuine finding must never be falsely downgraded"
    )
    print("PASS: token-overlap matching catches the real task039 shape (longer literal names vs "
          "shorter abstract labels) while still rejecting single-generic-token false positives")


def test_hallucinated_out_of_scope_field_required_filter_catches_backtick_quoted_widget_term():
    """Real, confirmed live gap (2026-08-07, task041 v13, real task_id
    4fcc0ba3-b4e4-470b-9172-cddf37d03eb4): once the not-yet-in-scope reviewer-clarity fix and the
    collision-marker fix both landed, `project_count` stopped being falsely flagged -- but the
    SAME round's own real not-yet-in-scope label ('customer_overview_smart_button') was still
    falsely contradicted, via TWO separate gaps in the same finding: (1) the finding used
    backtick-delimited quoting ("The required `oe_stat_button` ... is missing.") instead of
    straight quotes, which the original quoted-identifier regex didn't catch; (2) even once
    extracted, 'oe_stat_button' (tokens: oe/stat/button) shares only ONE significant token
    ('button') with the label 'customer_overview_smart_button' (tokens:
    customer/overview/smart/button) -- below the >=2 bar. A narrow allow-list of Odoo UI/view
    technical terms (button/widget/wizard/menu) lets a single shared token count when it's one of
    these specific, low-false-positive-risk words, without reopening the student_id/student_views
    false positive (neither 'student' nor 'views' nor 'id' are on the allow-list).
    """
    from specialists.code_review.specialist import (
        ReviewFinding,
        _filter_hallucinated_out_of_scope_field_required_findings,
    )

    goal = (
        "Add a Customer Overview smart button on res.partner. This round's own NEW focus is "
        "ONLY: 'project_count_field'. The following constraints are NOT yet in scope for this "
        "round and must NOT be implemented even partially: ['view_customer_projects_action', "
        "'customer_overview_smart_button']."
    )
    findings = [
        ReviewFinding(
            location="views/views.xml", severity="blocking",
            explanation="The required `oe_stat_button` in the form view header is missing.",
        ),
        ReviewFinding(
            location="models/models.py", severity="blocking",
            explanation="Field 'student_id' is required but missing the unique constraint.",
        ),
    ]
    out = _filter_hallucinated_out_of_scope_field_required_findings(findings, goal)
    assert out[0].severity == "info", (
        f"expected the backtick-quoted, single-technical-token widget finding to be downgraded "
        f"-- got {out[0].severity!r}"
    )
    assert out[1].severity == "blocking", (
        "student_id vs student_views must still stay blocking -- 'student' is not on the narrow "
        "single-token allow-list"
    )
    print("PASS: backtick-quoted technical widget term matches its real not-yet-in-scope label "
          "via the narrow single-token allow-list; the generic single-token false positive is "
          "still correctly rejected")


def test_hallucinated_goal_self_contradiction_filter_downgrades_real_task039_finding():
    """Real, confirmed live bug (2026-08-07, task039, HUMAN_DECISION deep-push, real task_id
    e447a03a-939a-4061-a0a7-d7483b7f6398): even after manager/loop.py's own goal_text was fixed
    to explicitly tell any reviewer that omitting not-yet-in-scope work is correct (not a
    contradiction), Code-Review still occasionally reaches the same "irreconcilable
    contradiction" verdict -- this exact real finding text, verbatim. Not caught by
    `_filter_hallucinated_scope_findings` (the goal genuinely DOES contain real scope-limiting
    language, so that filter correctly leaves it alone) nor by
    `_filter_hallucinated_out_of_scope_field_required_findings` (its gate regex doesn't match
    "goal text also requires X ... creating a contradiction" phrasing). This new filter targets
    the contradiction-framing directly.
    """
    from specialists.code_review.specialist import (
        ReviewFinding,
        _filter_hallucinated_goal_self_contradiction_findings,
    )

    goal = (
        "Add credential-storage fields on res.users. This round's own NEW focus is ONLY: "
        "'credential_fields'. The following constraints are NOT yet in scope for this round and "
        "must NOT be implemented even partially: ['mark_refreshed_action', "
        "'token_expired_computed']."
    )
    findings = [
        ReviewFinding(
            location="models/models.py", severity="blocking",
            explanation=(
                "Task goal explicitly forbids implementing 'mark_refreshed_action' and "
                "'token_expired_computed' in this round, but the goal text also requires "
                "'is_external_service_token_expired' and 'action_mark_refreshed' to be built "
                "now, creating a contradiction that needs clarification or strict adherence to "
                "the 'credential_fields' focus which implies only the storage fields."
            ),
        ),
        ReviewFinding(
            location="models/models.py", severity="blocking",
            explanation="The compute method has a genuine off-by-one error in its loop bound.",
        ),
    ]
    out = _filter_hallucinated_goal_self_contradiction_findings(findings, goal)
    assert out[0].severity == "info", (
        f"expected the real task039 contradiction-framing finding (every quoted name resolves "
        f"to this round's own not-yet-in-scope list) to be downgraded -- got {out[0].severity!r}"
    )
    assert out[1].severity == "blocking", (
        "an unrelated genuine finding with no contradiction language must never be touched"
    )
    print("PASS: the real task039 'irreconcilable contradiction' finding is downgraded; an "
          "unrelated genuine finding is left untouched")


def test_hallucinated_goal_self_contradiction_filter_downgrades_single_exact_label_per_finding():
    """Real, confirmed bug found live (2026-08-10, task e65381cc, ticket_views_menu node): the
    original >=2-distinct-names bar silently let through the single most common real shape this
    filter exists for -- Code-Review naming exactly ONE not-yet-in-scope constraint label
    VERBATIM per finding, one label per finding, across two SEPARATE findings, each individually
    failing the >=2 bar even though each is an exact, unambiguous, zero-doubt quote of a real
    label. An exact substring match to a real label is strong enough evidence on its own; it
    doesn't need a second corroborating name in the SAME finding.
    """
    from specialists.code_review.specialist import (
        ReviewFinding,
        _filter_hallucinated_goal_self_contradiction_findings,
    )

    goal = (
        "The following constraints are NOT yet in scope for this round and must NOT be "
        "implemented even partially: ['ticket_access_restriction', 'parts_consumed_relation', "
        "'parts_active_domain', 'ticket_status_decoration']."
    )
    findings = [
        ReviewFinding(
            location="security/ir.model.access.csv", severity="blocking",
            explanation=(
                "The task goal explicitly requires removing the base.group_user access row for "
                "oma.service.ticket, but the NOT-yet-in-scope list includes "
                "'ticket_access_restriction' as a constraint that must NOT be implemented, "
                "creating a direct contradiction in the task goal itself."
            ),
        ),
        ReviewFinding(
            location="views/views.xml", severity="blocking",
            explanation=(
                "The task goal explicitly requires the oma.service.ticket tree view to show the "
                "state field with distinct colors per state using decoration-<state> attributes, "
                "but the NOT-yet-in-scope list includes 'ticket_status_decoration' as a "
                "constraint that must NOT be implemented, creating a direct contradiction in the "
                "task goal itself."
            ),
        ),
        ReviewFinding(
            location="models/models.py", severity="blocking",
            explanation="The compute method has a genuine off-by-one error in its loop bound.",
        ),
    ]
    out = _filter_hallucinated_goal_self_contradiction_findings(findings, goal)
    assert out[0].severity == "info", f"expected the single-exact-label finding to be downgraded -- got {out[0].severity!r}"
    assert out[1].severity == "info", f"expected the single-exact-label finding to be downgraded -- got {out[1].severity!r}"
    assert out[2].severity == "blocking", "an unrelated genuine finding with no contradiction language must never be touched"
    print("PASS: each finding citing exactly ONE exact, verbatim not-yet-in-scope label is "
          "downgraded on its own, without needing a second corroborating name in the same finding")


def test_hallucinated_collision_satisfied_field_missing_filter_downgrades_real_task041_finding():
    """Real, confirmed live bug (2026-08-07, task041, HUMAN_DECISION deep-push, real task_id
    a38df0e6-57ee-4e2e-b4ab-c07017f634f0): manager/tools.py's run_code_review_diff() already
    appends an "IMPORTANT: [...] already exist as REAL, LIVE, FUNCTIONING ... Do NOT flag [...] as
    missing" sentence whenever Build's own collision-autofix correctly stripped a redundant field
    declaration -- but this pure textual instruction alone was NOT reliably followed, confirmed
    live: Code-Review still flagged "the diff contains no field definition ... for
    'project_count'" as blocking, verbatim, despite the sentence explicitly naming that field.
    """
    from specialists.code_review.specialist import (
        ReviewFinding,
        _filter_hallucinated_collision_satisfied_field_missing_findings,
    )

    goal = (
        "Add a Customer Overview smart button on res.partner. This round's own NEW focus is "
        "ONLY: 'project_count_field'.\n\n"
        "IMPORTANT: ['project_count'] already exist as REAL, LIVE, FUNCTIONING field(s) on the "
        "actual target model (confirmed independently, not a model claim) -- this round's own "
        "code correctly did NOT redeclare them, to avoid colliding with the real existing "
        "field(s). Their absence from this diff's models.py is CORRECT and COMPLETE, not a "
        "missing-implementation defect. Do NOT flag ['project_count'] as missing, unimplemented, "
        "or incomplete."
    )
    findings = [
        ReviewFinding(
            location="models/models.py", severity="blocking",
            explanation=(
                "The task requires adding a computed Integer field 'project_count' on "
                "res.partner, but the diff contains no field definition or method "
                "implementation."
            ),
        ),
        ReviewFinding(
            location="security/ir.model.access.csv", severity="blocking",
            explanation="Access control file is empty, preventing any user from reading or writing to the model.",
        ),
    ]
    out = _filter_hallucinated_collision_satisfied_field_missing_findings(findings, goal)
    assert out[0].severity == "info", (
        f"expected the real task041 collision-satisfied finding to be downgraded -- got "
        f"{out[0].severity!r}"
    )
    assert out[1].severity == "blocking", "an unrelated genuine finding must never be touched"
    print("PASS: the real task041 'field missing' finding for a real collision-satisfied field "
          "is downgraded; an unrelated genuine finding is left untouched")


def test_hallucinated_collision_satisfied_field_missing_filter_catches_unquoted_real_task041_finding():
    """Real, confirmed live follow-on gap (2026-08-07, task041, same real task, next relaunch):
    once the quoted-name match fixed the FIRST real finding shape, a second real Code-Review
    finding for the SAME field recurred with NO quoted field name at all ("no computed method or
    field definition is present"), so there was nothing for the quoted-identifier match to catch.
    The narrow, single-satisfied-field, no-other-name-quoted fallback should still catch it.
    """
    from specialists.code_review.specialist import (
        ReviewFinding,
        _filter_hallucinated_collision_satisfied_field_missing_findings,
    )

    goal = (
        "Add a Customer Overview smart button on res.partner.\n\n"
        "IMPORTANT: ['project_count'] already exist as REAL, LIVE, FUNCTIONING field(s) on the "
        "actual target model (confirmed independently, not a model claim) -- this round's own "
        "code correctly did NOT redeclare them, to avoid colliding with the real existing "
        "field(s). Their absence from this diff's models.py is CORRECT and COMPLETE, not a "
        "missing-implementation defect. Do NOT flag ['project_count'] as missing, unimplemented, "
        "or incomplete."
    )
    findings = [
        ReviewFinding(
            location="models/models.py", severity="blocking",
            explanation=(
                "The task requires the field to use a computed method with search_count, but no "
                "computed method or field definition is present."
            ),
        ),
        ReviewFinding(
            location="security/ir.model.access.csv", severity="blocking",
            explanation="The security file grants perm_unlink to a group that should never have delete access.",
        ),
    ]
    out = _filter_hallucinated_collision_satisfied_field_missing_findings(findings, goal)
    assert out[0].severity == "info", (
        f"expected the unquoted real task041 finding to be downgraded via the single-satisfied-"
        f"field fallback -- got {out[0].severity!r}"
    )
    assert out[1].severity == "blocking", (
        "an unrelated genuine finding on a different file/topic must never be touched by the "
        "narrow single-field fallback"
    )
    print("PASS: the unquoted real task041 finding is downgraded via the single-satisfied-field "
          "fallback; an unrelated genuine finding is left untouched")


def test_hallucinated_collision_satisfied_field_missing_filter_catches_the_actual_accepted_task041_response():
    """Real, confirmed live gap (2026-08-07, task041, real task_id 7863b345-c9c5-45bb-beea-
    dda599f49e27) found by pulling the raw redis LLM call log's own `response` field directly --
    NOT the streamed deltas, which (misleadingly) also included an earlier, aborted-by-repetition-
    loop draft that happened to reason through the contradiction correctly before getting cut off
    mid-generation and discarded. The REAL, accepted, final response (`llm_call.response`, the
    only text this system ever actually acts on) never attempted that reasoning at all -- it went
    straight to a short, flat, wrong claim using "the models.py file is empty" and "no such field
    ... is implemented" phrasing that `_MISSING_FIELD_GATE_RE` didn't cover (it only recognized
    "missing"/"absent"/"is required"/"must be defined" phrasing), so the already-correct single-
    satisfied-field fallback never even got a chance to fire. Widened the gate regex to also cover
    "is empty" and "no such X ... is implemented"/"not implemented" phrasing.
    """
    from specialists.code_review.specialist import (
        ReviewFinding,
        _filter_hallucinated_collision_satisfied_field_missing_findings,
    )

    goal = (
        "Add a Customer Overview smart button on res.partner.\n\n"
        "IMPORTANT: ['project_count'] already exist as REAL, LIVE, FUNCTIONING field(s) on the "
        "actual target model (confirmed independently, not a model claim) -- this round's own "
        "code correctly did NOT redeclare them, to avoid colliding with the real existing "
        "field(s). Their absence from this diff's models.py is CORRECT and COMPLETE, not a "
        "missing-implementation defect. Do NOT flag ['project_count'] as missing, unimplemented, "
        "or incomplete."
    )
    findings = [
        ReviewFinding(
            location="/mnt/extra-addons/oma_add_a_single_customer_75595775/models/models.py",
            severity="blocking",
            explanation=(
                "The task requires adding a computed Integer field named project_count on "
                "res.partner, but the models.py file is empty (only inherits res.partner with "
                "no fields or methods defined)."
            ),
        ),
        ReviewFinding(
            location="/mnt/extra-addons/oma_add_a_single_customer_75595775/models/models.py",
            severity="blocking",
            explanation=(
                "The task explicitly requires a computed field using search_count on "
                "project.project, but no such field or computation method is implemented in the "
                "provided code."
            ),
        ),
    ]
    out = _filter_hallucinated_collision_satisfied_field_missing_findings(findings, goal)
    assert out[0].severity == "info" and out[1].severity == "info", (
        f"expected BOTH real, actually-accepted task041 findings ('is empty' / 'no such X ... is "
        f"implemented' phrasing) to be downgraded via the widened gate -- got "
        f"{[f.severity for f in out]!r}"
    )
    print("PASS: both real, actually-accepted task041 findings (the ones the system genuinely "
          "used, not an aborted draft) are downgraded via the widened gate regex")


def test_hallucinated_stale_scope_exclusion_filter_downgrades_outdated_constraint_name():
    """Real, confirmed live bug (2026-07-28, Phase 28C, `school_student`
    task, the `menu_structure` round): a genuinely more specific failure
    than `_filter_hallucinated_scope_findings` above -- that filter only
    checks whether the goal contains genuine scope language at all; it
    can't catch Code-Review naming the WRONG specific constraint label.
    The round's own real, current contract only excluded
    ['security_groups', 'record_rules', 'computed_age_field',
    'demo_data', 'automated_tests'] -- `student_views` had already been
    satisfied by an earlier round (verified live: its real committed
    views.xml content was genuinely correct and present) -- yet
    Code-Review still blocked citing "'student_views' is explicitly NOT
    in scope," a stale echo of constraint #1's own history still
    lingering in `contract.rules`.
    """
    from specialists.code_review.specialist import (
        ReviewFinding,
        _filter_hallucinated_stale_scope_exclusion_findings,
    )

    goal = (
        "Build school_student. This round's own NEW focus is ONLY: 'menu_structure'. The "
        "following constraints are NOT yet in scope for this round and must NOT be implemented "
        "even partially: ['security_groups', 'record_rules', 'computed_age_field', 'demo_data', "
        "'automated_tests']."
    )
    findings = [
        ReviewFinding(
            location="__manifest__.py", severity="blocking",
            explanation=(
                "Manifest references 'views/views.xml' but 'student_views' is explicitly NOT in "
                "scope for this round; including views in manifest violates the round constraint."
            ),
        ),
        ReviewFinding(
            location="views/views.xml", severity="blocking",
            explanation=(
                "File exists and contains view definitions, but 'student_views' is explicitly "
                "NOT in scope for this round; the file should not exist or be referenced."
            ),
        ),
        ReviewFinding(
            location="models/models.py", severity="blocking",
            explanation="Model references 'security_groups' which is explicitly not in scope for this round.",
        ),
    ]
    out = _filter_hallucinated_stale_scope_exclusion_findings(findings, goal)
    assert out[0].severity == "info" and out[1].severity == "info", (
        "both stale 'student_views is not in scope' claims (a genuinely already-satisfied "
        "constraint) must be downgraded"
    )
    assert out[2].severity == "blocking", (
        "a claim naming a label that IS genuinely still in the round's own real not-yet-in-scope "
        "list ('security_groups') must never be touched"
    )
    print("PASS: a stale scope-exclusion claim naming an already-satisfied constraint is "
          "downgraded, while a claim naming a genuinely still-excluded constraint is untouched")


def test_hallucinated_findings_contradicted_by_own_overall_assessment_downgrades_task030_real_shape():
    """Real, confirmed bug found live (2026-08-06, Phase 30 backlog pass, task030, real task_id
    10cbf536-937e-4477-9317-a03fbd435dae): a single review response's own `overall_assessment`
    directly, affirmatively contradicted its own lone finding's `severity == "blocking"` --
    the assessment said the change "correctly focuses only on the action_send method as required
    by the round's scope, adhering to the specified context keys and avoiding out-of-scope
    elements" (unambiguous "nothing is wrong" language), while the finding's own `explanation`
    turned out to be literal chain-of-thought that reasoned its way to "so it respects the scope"
    and then never updated its own severity to match. Using this exact real shape.
    """
    from specialists.code_review.specialist import (
        ReviewFinding,
        _filter_hallucinated_findings_contradicted_by_own_overall_assessment,
    )

    self_contradicting_finding = [
        ReviewFinding(
            location="models/models.py", severity="blocking",
            explanation=(
                "The method action_send is implemented, but the task goal explicitly states that "
                "the 'send_to_customer_button' and 'message_post_after_hook' are NOT yet in scope "
                "and must NOT be implemented. However, the task goal also says 'This round's own "
                "NEW focus is ONLY: action_send_method'. The implementation of action_send is "
                "correct according to the spec. So it respects the scope."
            ),
        ),
    ]
    real_overall_assessment = (
        "The implementation correctly focuses only on the action_send method as required by the "
        "round's scope, adhering to the specified context keys and avoiding out-of-scope elements."
    )
    out = _filter_hallucinated_findings_contradicted_by_own_overall_assessment(
        self_contradicting_finding, real_overall_assessment,
    )
    assert out[0].severity == "info", (
        "a blocking finding must be downgraded when the review's own overall_assessment "
        "affirmatively contradicts it with no problem language anywhere"
    )

    # A genuinely mixed review (real praise for one part, a real separate problem named) must
    # never be touched -- the presence of ANY problem-shaped vocabulary in the assessment must
    # leave every finding exactly as-is.
    genuine_mixed_finding = [
        ReviewFinding(
            location="models/models.py", severity="blocking",
            explanation="The 'source_id' field references a model that does not exist.",
        ),
    ]
    genuinely_mixed_assessment = (
        "The implementation correctly focuses on the core fields, but the 'source_id' field "
        "references a model that does not exist and must be fixed before this can be considered "
        "complete."
    )
    out2 = _filter_hallucinated_findings_contradicted_by_own_overall_assessment(
        genuine_mixed_finding, genuinely_mixed_assessment,
    )
    assert out2[0].severity == "blocking", (
        "a genuinely mixed review (praise AND a real named problem) must never be touched"
    )
    print("PASS: a self-contradicting review (affirmative 'no issue' assessment vs. a blocking "
          "finding) is downgraded; a genuinely mixed review with a real problem is never touched")

    # Real, confirmed SECOND live rephrasing of the identical self-contradiction (2026-08-06,
    # task030's own v10 real re-run, real task_id 94f1c08f-018a-45c5-a5b1-3a522a55ab98) -- the
    # original regex missed this exact wording ("matches the scope constraint... implementing
    # only X as requested... without adding the out-of-scope Y"), confirming the same fragility
    # this whole hallucination-filter family is designed around: a real rephrasing always slips
    # past a too-narrow first pass.
    second_rephrasing_finding = [
        ReviewFinding(
            location="models/models.py", severity="blocking",
            explanation=(
                "The method action_send is implemented, but the task explicitly states that "
                "'send_to_customer_button' and 'state_change_hook' are NOT yet in scope and must "
                "NOT be implemented. The method action_send is the ONLY thing in scope. However, "
                "the method action_send is present."
            ),
        ),
    ]
    second_rephrasing_assessment = (
        "The implementation matches the scope constraint for this round, implementing only the "
        "action_send method as requested, without adding the out-of-scope button or state "
        "change hook."
    )
    out3 = _filter_hallucinated_findings_contradicted_by_own_overall_assessment(
        second_rephrasing_finding, second_rephrasing_assessment,
    )
    assert out3[0].severity == "info", (
        "the second, real live rephrasing of the same self-contradiction must also be downgraded"
    )
    print("PASS: a second, real live rephrasing of the same self-contradiction is also downgraded")


def test_hallucinated_current_round_conflated_with_deferred_labels_filter_catches_every_real_rephrasing():
    """Real, general, structural fix (2026-08-06, Phase 30 backlog pass, task030): the SAME
    underlying self-contradiction (Code-Review claims a round's own current-focus method
    'implements' a DEFERRED constraint it never actually touches) recurred under a THIRD distinct
    prose rephrasing on a real re-run (task_id a39f33a9-0f48-4b20-9c9c-9be97d8f6168) that the two
    prose-matching filters above both missed: "The implementation of action_send matches the
    technical spec for the 'send_customer_action' focus. However, the task goal explicitly
    states..." -- this filter checks the underlying FACT (does any deferred label's own words
    actually appear in the round's real new content) instead of prose, so it catches this AND the
    two earlier real rephrasings, and any future one, without needing its own new regex each time.
    """
    from contracts.scope import RoundScope
    from specialists.code_review.specialist import (
        ReviewFinding,
        _filter_hallucinated_current_round_conflated_with_deferred_labels_findings,
    )

    scope = RoundScope(
        current_focus="send_customer_action", not_yet_in_scope=["send_customer_button", "state_change_hook"],
        is_decomposed=True,
    )
    files = {
        "models/models.py": (
            "from odoo import models\n\nclass ProjectFieldjob(models.Model):\n"
            "    _inherit = 'project.fieldjob'\n\n"
            "    def action_send(self):\n        self.ensure_one()\n        return {}\n"
        ),
    }
    third_rephrasing_finding = [
        ReviewFinding(
            location="models/models.py", severity="blocking",
            explanation=(
                "The implementation of action_send matches the technical spec for the "
                "'send_customer_action' focus. However, the task goal explicitly states: 'The "
                "following constraints are NOT yet in scope for this round and must NOT be "
                "implemented even partially -- no fields, methods, or view elements for any of "
                "them yet, no matter how related they seem: [send_customer_button, "
                "state_change_hook].'"
            ),
        ),
    ]
    out = _filter_hallucinated_current_round_conflated_with_deferred_labels_findings(
        third_rephrasing_finding, files, scope,
    )
    assert out[0].severity == "info", (
        "the third, real live rephrasing must be caught by the structural (not prose-based) check"
    )

    # A genuine violation -- the round's own real content DOES define something matching a
    # deferred label's own words -- must never be touched.
    genuine_violation_files = {
        "models/models.py": (
            "from odoo import models\n\nclass ProjectFieldjob(models.Model):\n"
            "    _inherit = 'project.fieldjob'\n\n"
            "    def action_send(self):\n        self.ensure_one()\n        return {}\n\n"
            "    def _message_post_after_hook(self, message, msg_vals):\n        pass\n"
        ),
    }
    genuine_violation_finding = [
        ReviewFinding(
            location="models/models.py", severity="blocking",
            explanation=(
                "The 'send_customer_action' round also implements _message_post_after_hook, "
                "which is explicitly deferred as 'state_change_hook' and must not be implemented "
                "yet."
            ),
        ),
    ]
    out2 = _filter_hallucinated_current_round_conflated_with_deferred_labels_findings(
        genuine_violation_finding, genuine_violation_files, scope,
    )
    assert out2[0].severity == "blocking", (
        "a genuine violation (deferred label's own words DO match new real content) must never "
        "be touched"
    )
    print("PASS: the structural, fact-based filter catches a real rephrasing prose-matching "
          "missed, and never touches a genuine violation")


def test_hallucinated_goal_spec_context_key_filter_downgrades_task030_real_shape():
    """Real, general fix (2026-08-06, Phase 30 backlog pass, task030, real task_id
    b2e48922-828d-41d3-8e5c-2df8c7b6ab98): Code-Review objected to the context-dict key
    `mark_fieldjob_as_sent` the round's own `action_send` method sets, on the theory that setting
    a key a LATER round's hook will eventually read is "a partial implementation of a future
    constraint" -- but the task's own real goal text explicitly lists that exact key in its
    'Context keys:' specification as part of `action_send`'s OWN required behavior, not deferred
    at all. Using this exact real goal/finding shape.
    """
    from specialists.code_review.specialist import (
        ReviewFinding,
        _filter_hallucinated_goal_spec_context_key_findings,
    )

    real_goal = (
        "Add a complete mail compose wizard flow with state change. ... Method: action_send "
        "returns ir.actions.act_window for mail.compose.message. Context keys: default_model, "
        "default_res_id (NOT res_ids -- causes SQL error), default_composition_mode='comment', "
        "default_partner_ids=[partner_id.id] (plain list, NOT [(4, id)]), "
        "mark_fieldjob_as_sent=True. State change hook: Override _message_post_after_hook -- if "
        "context has mark_fieldjob_as_sent, set state='sent'."
    )
    real_finding = [
        ReviewFinding(
            location="models/models.py", severity="blocking",
            explanation=(
                "The task explicitly states that 'state_change_hook' (which corresponds to "
                "_message_post_after_hook) is NOT in scope for this round, yet the context key "
                "'mark_fieldjob_as_sent' is added, implying a dependency on a hook that is not "
                "yet implemented, creating a partial implementation of a future constraint."
            ),
        ),
    ]
    out = _filter_hallucinated_goal_spec_context_key_findings(real_finding, real_goal)
    assert out[0].severity == "info", (
        "a context key literally listed in the goal's own 'Context keys:' spec must be downgraded"
    )

    # A context key NOT named in the goal's own spec (a genuinely invented one) must never be
    # touched.
    invented_key_finding = [
        ReviewFinding(
            location="models/models.py", severity="blocking",
            explanation=(
                "The context key 'totally_invented_key' is added, implying a dependency on a "
                "hook that is not yet implemented."
            ),
        ),
    ]
    out2 = _filter_hallucinated_goal_spec_context_key_findings(invented_key_finding, real_goal)
    assert out2[0].severity == "blocking", (
        "a context key NOT named in the goal's own spec must never be touched"
    )
    print("PASS: a context key literally required by the goal's own spec is never treated as "
          "scope creep; a genuinely invented key is still caught")


def test_hallucinated_goal_spec_context_key_filter_catches_4th_real_rephrasing():
    """Real, general fix (2026-08-07, full-backlog pass, task030 v22, decomposed run): a 4th
    distinct prose rephrasing of the same self-contradiction slipped past the original regex --
    'sets 'mark_fieldjob_as_sent=True' in the context, which is the trigger for that out-of-scope
    hook' -- a different sentence shape (key=value inside quotes, 'trigger for' instead of
    'implies/depends on') naming the same real, goal-required context key.
    """
    from specialists.code_review.specialist import (
        ReviewFinding,
        _filter_hallucinated_goal_spec_context_key_findings,
    )

    real_goal = (
        "Add a complete mail compose wizard flow with state change. ... Method: action_send "
        "returns ir.actions.act_window for mail.compose.message. Context keys: default_model, "
        "default_res_id (NOT res_ids -- causes SQL error), default_composition_mode='comment', "
        "default_partner_ids=[partner_id.id] (plain list, NOT [(4, id)]), "
        "mark_fieldjob_as_sent=True. State change hook: Override _message_post_after_hook -- if "
        "context has mark_fieldjob_as_sent, set state='sent'."
    )
    real_finding = [
        ReviewFinding(
            location="models/models.py", severity="blocking",
            explanation=(
                "action_send sets 'mark_fieldjob_as_sent=True' in the context, which is the "
                "trigger for that out-of-scope hook and represents a premature implementation "
                "detail for a not-yet-built feature."
            ),
        ),
    ]
    out = _filter_hallucinated_goal_spec_context_key_findings(real_finding, real_goal)
    assert out[0].severity == "info", (
        "the 4th real prose rephrasing of the same context-key self-contradiction must also be "
        "downgraded"
    )
    print("PASS: 4th real rephrasing of the context-key self-contradiction is caught")


def test_hallucinated_findings_citing_stale_previous_attempt_error_text_downgrades_task047_real_shape():
    """Real, general fix (2026-08-07, HUMAN_DECISION push, task047, real task_id
    69108d64-18bf-478a-933b-628bb821a6ab): round 2 genuinely fixed round 1's real @api.model/
    self.field defect (now passes the text as an explicit argument instead), but Code-Review kept
    the finding blocking purely because it cited round 1's own error/feedback text as
    justification, while the SAME finding's own explanation admitted the current code no longer
    has that exact problem. Using the real finding text verbatim.
    """
    from specialists.code_review.specialist import (
        ReviewFinding,
        _filter_hallucinated_findings_citing_stale_previous_attempt_error_text,
    )

    real_findings = [
        ReviewFinding(
            location="models/models.py:32", severity="blocking",
            explanation=(
                "Method 'parse_text_to_containers' is decorated with @api.model but the caller "
                "'action_parse_text' passes 'self.text_field' as an argument, which is correct, "
                "however the previous attempt error explicitly flagged this pattern as a bug "
                "where @api.model methods access 'self.text_field' directly; while this specific "
                "implementation passes the text as an argument, the decorator @api.model is "
                "semantically questionable."
            ),
        ),
    ]
    out = _filter_hallucinated_findings_citing_stale_previous_attempt_error_text(real_findings)
    assert out[0].severity == "info", (
        "a finding citing a prior round's own error text while admitting the current code is "
        "correct must be downgraded"
    )

    # A finding citing history WITHOUT any admission the current code is fine must never be
    # touched -- it may be a genuinely recurring defect.
    genuinely_recurring = [
        ReviewFinding(
            location="models/models.py", severity="blocking",
            explanation=(
                "The previous attempt error explicitly flagged this exact missing field, and it "
                "is still missing from the current round's models.py."
            ),
        ),
    ]
    out2 = _filter_hallucinated_findings_citing_stale_previous_attempt_error_text(genuinely_recurring)
    assert out2[0].severity == "blocking", (
        "a finding citing history WITHOUT admitting the current code is fine must never be touched"
    )
    print("PASS: stale-previous-attempt-error-citation filter catches task047's real shape and "
          "never touches a genuinely recurring defect")


def test_hallucinated_goal_headline_scope_filter_downgrades_task037_real_shape():
    """50-task deep-dive (docs/reports/PHASE30_50TASK_DEEP_DIVE_MASTER_2026-08-05.md, P1 item 2):
    real, confirmed live finding on Task 037 ("Create a custom security group and apply it to
    fields and buttons"). The round was correctly, deliberately scoped to ONLY
    'field_visibility_restriction', with 'security_group' explicitly deferred -- but Code-Review
    evaluated the diff against the full, un-narrowed original goal and blocked it citing "the
    task goal explicitly asks to 'Create a custom security group' ... failing the primary
    objective." None of the four pre-existing sibling filters catch this shape.
    """
    from contracts.scope import RoundScope
    from specialists.code_review.specialist import (
        ReviewFinding,
        _filter_hallucinated_goal_headline_scope_findings,
    )

    scope = RoundScope(
        current_focus="field_visibility_restriction",
        not_yet_in_scope=["security_group", "button_visibility_restriction"],
        is_decomposed=True,
    )
    findings = [
        ReviewFinding(
            location="models/models.py", severity="blocking",
            explanation=(
                "Adds 'custom_field' to res.partner, but the round's ONLY focus is "
                "'field_visibility_restriction' which requires a security group; no security "
                "group is defined, making the field unrestricted and the implementation "
                "incomplete."
            ),
        ),
        ReviewFinding(
            location="security/security.xml", severity="blocking",
            explanation=(
                "The task goal explicitly asks to 'Create a custom security group', but the "
                "diff contains no security records or group definitions, failing the primary "
                "objective."
            ),
        ),
    ]
    out = _filter_hallucinated_goal_headline_scope_findings(findings, scope)
    assert out[1].severity == "info", (
        "Task 037's real 'goal explicitly asks for X ... failing the primary objective' finding "
        "must be downgraded -- 'security group' is explicitly deferred by this round's own scope"
    )
    print("PASS: Task 037's real finding shape is downgraded when the named headline concept is "
          "explicitly deferred by this round's own scope")


def test_hallucinated_goal_headline_scope_filter_never_touches_task038_real_findings():
    """The explicit regression test for the risk called out in the deep-dive report: Task 038's
    two real blocking findings must NEVER be downgraded by this new filter, even though it shares
    the same general shape (a decomposed round, gates_disagree). Neither finding actually matches
    "goal explicitly asks (to|for) X" -- (1) is Build violating scope by ADDING something
    deferred (the mirror-opposite of what this filter exists to catch), and (2) is a genuinely
    empty in-scope model (Pattern G), not a headline-requirement complaint.
    """
    from contracts.scope import RoundScope
    from specialists.code_review.specialist import (
        ReviewFinding,
        _filter_hallucinated_goal_headline_scope_findings,
    )

    scope = RoundScope(
        current_focus="sales_room_model",
        not_yet_in_scope=["service_lines", "product_lines", "material_lines", "auto_populate_lines"],
        is_decomposed=True,
    )
    findings = [
        ReviewFinding(
            location="models/models.py", severity="blocking",
            explanation=(
                "Adds fields 'service_line_count' and 'product_line_count' which are explicitly "
                "out-of-scope per the task's NOT-yet-in-scope list ['service_lines', "
                "'product_lines', 'material_lines', 'auto_populate_lines'] and the instruction to "
                "add ONLY the code the 'sales_room_model' constraint strictly requires."
            ),
        ),
        ReviewFinding(
            location="models/models.py", severity="blocking",
            explanation=(
                "The model definition is empty of required fields for the 'sales_room_model' "
                "constraint, effectively making the module non-functional for its stated goal "
                "while violating the scope constraint by adding unrelated fields."
            ),
        ),
    ]
    out = _filter_hallucinated_goal_headline_scope_findings(findings, scope)
    assert out[0].severity == "blocking" and out[1].severity == "blocking", (
        "Task 038's two genuine, correct findings must never be downgraded by this filter -- "
        "downgrading either would let a real, still-broken defect through Code-Review uncaught"
    )
    print("PASS: Task 038's two genuine findings (out-of-scope field additions, empty in-scope "
          "model) are never touched by the new goal-headline-scope filter")


def test_hallucinated_goal_headline_scope_filter_never_downgrades_a_genuinely_undeferred_requirement():
    """A "goal explicitly asks for X ... failing the primary objective" finding naming a concept
    that is genuinely NOT deferred by this round's own scope (a real, un-deferred requirement)
    must stay blocking."""
    from contracts.scope import RoundScope
    from specialists.code_review.specialist import (
        ReviewFinding,
        _filter_hallucinated_goal_headline_scope_findings,
    )

    scope = RoundScope(
        current_focus="security_group", not_yet_in_scope=["button_visibility_restriction"],
        is_decomposed=True,
    )
    findings = [
        ReviewFinding(
            location="security/security.xml", severity="blocking",
            explanation=(
                "The task goal explicitly asks to 'Create a custom security group', but the "
                "diff contains no security records or group definitions, failing the primary "
                "objective."
            ),
        ),
    ]
    out = _filter_hallucinated_goal_headline_scope_findings(findings, scope)
    assert out[0].severity == "blocking", (
        "'security group' is this round's own CURRENT focus, not a deferred concept -- a "
        "genuine, un-deferred headline requirement must never be downgraded"
    )
    print("PASS: a genuinely un-deferred headline requirement is never downgraded")


def test_hallucinated_incomplete_compute_dependency_filter_downgrades_invented_scope():
    """Phase 25D (2026-07-26): real, confirmed live bug -- task 004's own
    resubmission, right after the sum-compute autofix was fixed to match
    the goal's own EXACT stated dependency (line_ids.price_unit),
    Code-Review invented a "should also factor in quantity" requirement
    the goal never actually stated (its own technical spec names exactly
    one dependency) -- FOUR times in a row, each time reworded
    differently ("ignores quantity and discount", "failing to trigger
    recomputation... stale data", "ignoring 'quantity'", "Financial
    calculation error... violating Financial Safety"), never converging
    on a fixed set of keywords. The filter was rewritten to match
    STRUCTURALLY (does the finding mention the field or its stated
    dependency at all) instead of chasing wording, gated by a
    ground-truth check against the real generated code -- only ever
    downgrades once the code is independently confirmed to already
    correctly implement the goal's own exact single dependency.
    """
    from specialists.code_review.specialist import (
        ReviewFinding,
        _filter_hallucinated_incomplete_compute_dependency_findings,
    )

    contract = TaskContract(
        task_id=uuid.uuid4(), specialist_type=SpecialistType.code_review,
        capability_class=CapabilityClass.readonly_investigation, tier=AutonomyTier.tier_1_readonly,
        goal=(
            "On the fieldjob record, I want to see the total of all line prices shown "
            "automatically in the header.\n\nModule: project_fieldjob\nModel: project.fieldjob\n"
            "Field: amount_total (Monetary, compute=_compute_amount_total, depends on "
            "line_ids.price_unit, store=True)\n"
        ),
        inputs=[], rules=[], deliverables=[], compensating_actions=[], validation_by=None,
        pause_if=[], turn_budget=10,
    )
    correct_files = {
        "/mnt/extra-addons/oma_x/models/models.py": (
            "from odoo import api, fields, models\n\nclass ProjectFieldjob(models.Model):\n"
            "    _inherit = 'project.fieldjob'\n"
            "    amount_total = fields.Monetary(string='Amount Total', "
            "compute='_compute_amount_total', store=True)\n\n"
            "    @api.depends('line_ids.price_unit')\n"
            "    def _compute_amount_total(self):\n"
            "        for record in self:\n"
            "            record.amount_total = sum(record.line_ids.mapped('price_unit'))\n"
        ),
    }
    # Four real, independently-observed live rewordings of the same
    # invented requirement -- every one must be caught, since none of
    # them share a common keyword.
    rewordings = [
        "Compute method depends only on 'line_ids.price_unit' but ignores 'line_ids.quantity' and "
        "'line_ids.discount', causing incorrect totals when lines have quantity > 1 or discounts.",
        "The @api.depends decorator only tracks 'line_ids.price_unit', failing to trigger "
        "recomputation when lines are added, removed, or when other relevant fields (like "
        "quantity) change, leading to stale data.",
        "Compute method sums 'price_unit' directly, ignoring 'quantity' if it exists on the line "
        "model, resulting in incorrect totals for lines with quantity > 1.",
        "Financial calculation error: sums price_unit instead of line total (price_unit * "
        "quantity), violating Financial Safety.",
    ]
    for explanation in rewordings:
        findings = [ReviewFinding(location="models.py", severity="blocking", explanation=explanation)]
        out = _filter_hallucinated_incomplete_compute_dependency_findings(findings, contract, correct_files)
        assert out[0].severity == "info", (
            f"expected this invented requirement to be downgraded -- got {out[0].severity!r} for: "
            f"{explanation!r}"
        )
    print("PASS: all four independently-observed live rewordings of the invented quantity/discount "
          "requirement are downgraded, once the real code is confirmed correct")

    # A goal stating MULTIPLE dependencies is a genuinely different, more
    # ambiguous shape -- "is this complete" becomes a real question, never touched.
    multi_dep_contract = contract.model_copy(update={
        "goal": contract.goal.replace(
            "depends on line_ids.price_unit, store=True",
            "depends on line_ids.price_unit, line_ids.quantity, store=True",
        ),
    })
    findings = [ReviewFinding(location="models.py", severity="blocking", explanation=rewordings[0])]
    out_multi = _filter_hallucinated_incomplete_compute_dependency_findings(findings, multi_dep_contract, correct_files)
    assert out_multi[0].severity == "blocking", "must never touch a genuinely multi-dependency compute goal"
    print("PASS: never downgraded when the goal states more than one dependency")

    # The ground-truth guard: if the real code does NOT actually match
    # the goal's stated dependency (a genuinely different, real bug),
    # never downgrade -- even though the finding mentions the field.
    wrong_files = {
        "/mnt/extra-addons/oma_x/models/models.py": correct_files[
            "/mnt/extra-addons/oma_x/models/models.py"
        ].replace("line_ids.price_unit", "line_ids.quantity"),
    }
    out_wrong = _filter_hallucinated_incomplete_compute_dependency_findings(findings, contract, wrong_files)
    assert out_wrong[0].severity == "blocking", (
        "must never downgrade when the real code does NOT actually match the goal's stated dependency"
    )
    print("PASS: never downgraded when the real code can't be confirmed to already be correct")

    # Real, confirmed bug found live (2026-07-26, the SAME task, 6th
    # resubmission): "the @api.depends decorator is missing 'line_ids' to
    # track record creation/deletion" -- names the RELATION only, not
    # the field or subfield.
    relation_only_findings = [
        ReviewFinding(
            location="models.py", severity="blocking",
            explanation=(
                "The @api.depends decorator is missing 'line_ids' to track record creation/"
                "deletion, causing stale totals when lines are added or removed."
            ),
        ),
    ]
    out_relation = _filter_hallucinated_incomplete_compute_dependency_findings(
        relation_only_findings, contract, correct_files,
    )
    assert out_relation[0].severity == "info", (
        f"expected a finding mentioning only the relation (line_ids) to also be downgraded -- "
        f"got {out_relation[0].severity!r}"
    )
    print("PASS: a finding mentioning only the relation, not the field/subfield, is also downgraded")


def test_hallucinated_wrong_constraint_attribution_filter_downgrades_real_false_claim():
    """Real, confirmed bug found live (2026-08-10, task 07141af5's flagship run,
    daily_escalation_cron node, resumes r202 and r203): Code-Review repeatedly claimed "Field
    'escalation_message_posted' is defined in this round but belongs to
    'ticket_workflow_and_logging' which is explicitly NOT in scope for this round" (and the
    identical claim for the sibling method `run_daily_escalation_check`) -- factually false: the
    task's own original goal assigns exactly this content to THIS round's own real focus,
    `daily_escalation_cron`, not `ticket_workflow_and_logging` (a completely different
    state-change-logging requirement). An explicit retraction note did not stop the identical
    claim recurring verbatim the very next round.
    """
    from specialists.code_review.specialist import (
        ReviewFinding,
        _filter_hallucinated_wrong_constraint_attribution_findings,
    )

    contract = _make_contract(
        "This round's own NEW focus is ONLY: 'daily_escalation_cron'.",
        ["verify_module:x"],
    ).model_copy(update={"current_constraint_label": "daily_escalation_cron"})
    findings = [
        ReviewFinding(
            location="models/models.py:10", severity="blocking",
            explanation=(
                "Field 'escalation_message_posted' is defined in this round but belongs to "
                "'ticket_workflow_and_logging' which is explicitly NOT in scope for this round."
            ),
        ),
        ReviewFinding(
            location="models/models.py:12", severity="blocking",
            explanation=(
                "Method 'run_daily_escalation_check' is defined in this round but belongs to "
                "'ticket_workflow_and_logging' which is explicitly NOT in scope for this round."
            ),
        ),
        # Real, confirmed follow-up phrasing found live (2026-08-10, resume r204): the exact
        # same claim, but dropping "is defined in this round but".
        ReviewFinding(
            location="models/models.py:10", severity="blocking",
            explanation=(
                "Field 'escalation_message_posted' belongs to 'ticket_workflow_and_logging' "
                "which is explicitly NOT in scope for this round."
            ),
        ),
        # Real, confirmed follow-up phrasing found live (2026-08-10, resume r204): the SAME
        # claim about the cron record's own <field name="code"> reference, phrased entirely
        # differently ("references 'model.X()' which is a method from 'Y'").
        ReviewFinding(
            location="data/cron_data.xml:5", severity="blocking",
            explanation=(
                "Cron record references 'model.run_daily_escalation_check()' which is a method "
                "from 'ticket_workflow_and_logging' (NOT in scope), causing a runtime error or "
                "scope violation."
            ),
        ),
    ]
    out = _filter_hallucinated_wrong_constraint_attribution_findings(findings, contract)
    assert all(o.severity == "info" for o in out), (
        f"expected all four false scope-misattribution phrasings to be downgraded -- got "
        f"{[o.severity for o in out]!r}"
    )

    # But a claim that genuinely fuzzy-matches the CLAIMED label too (a plausible real
    # misplacement, not a mechanical contradiction) must be left untouched.
    plausible_findings = [
        ReviewFinding(
            location="models/models.py:20", severity="blocking",
            explanation=(
                "Method 'ticket_workflow_transition' is defined in this round but belongs to "
                "'ticket_workflow_and_logging' which is explicitly NOT in scope for this round."
            ),
        ),
    ]
    plausible_out = _filter_hallucinated_wrong_constraint_attribution_findings(plausible_findings, contract)
    assert plausible_out[0].severity == "blocking", (
        "a claim whose named member also fuzzy-matches the claimed label must never be "
        "downgraded -- this filter only catches a mechanical self-contradiction"
    )
    print("PASS: a false scope-misattribution claim (member matches THIS round's own focus, not "
          "the claimed label) is downgraded; a plausible one (matches the claimed label too) is not")


def test_hallucinated_bare_group_id_prefix_mismatch_filter_downgrades_real_false_claim():
    """Phase 28C (2026-07-29): real, confirmed live bug -- the
    `school_student` task's own security_groups round. Code-Review
    claimed `<record id="group_school_admin" model="res.groups">`
    (bare, no explicit module prefix) "causes a mismatch" with
    security_csv's own `group_id:id` reference to
    `oma_simple_custom_module_task_595ad7bc.group_school_admin` -- a
    hallucination: standard Odoo xmlid resolution means a bare id
    defined in module M is ALWAYS also addressable as `M.<id>`, no
    mismatch exists. Confirmed live by directly installing this exact
    real content via a fresh odoo-bin process: it installed cleanly.
    """
    from specialists.code_review.specialist import (
        ReviewFinding,
        _filter_hallucinated_bare_group_id_prefix_mismatch_findings,
    )

    module_name = "oma_simple_custom_module_task_595ad7bc"
    files = {
        "security/security.xml": (
            '<?xml version="1.0" encoding="utf-8"?>\n<odoo>\n    <data>\n'
            '        <record id="group_school_user" model="res.groups">\n'
            '            <field name="name">School / User</field>\n'
            '        </record>\n'
            '        <record id="group_school_admin" model="res.groups">\n'
            '            <field name="name">School / Admin</field>\n'
            '        </record>\n    </data>\n</odoo>'
        ),
        "security/ir.model.access.csv": (
            "id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink\n"
            f"access_school_student_admin,school.student,model_school_student,"
            f"{module_name}.group_school_admin,1,1,1,1\n"
        ),
    }
    findings = [
        ReviewFinding(
            location="security/security.xml", severity="blocking",
            explanation=(
                "The group 'group_school_admin' is defined without the module prefix "
                f"'{module_name}.', causing a mismatch with the access CSV reference "
                f"'{module_name}.group_school_admin'."
            ),
        ),
    ]
    out = _filter_hallucinated_bare_group_id_prefix_mismatch_findings(findings, files, module_name)
    assert out[0].severity == "info", (
        f"expected the false 'prefix mismatch' claim about a genuinely, correctly bare group id "
        f"to be downgraded -- got severity {out[0].severity!r}"
    )
    print("PASS: the real, confirmed false 'bare group id vs module-prefixed CSV ref mismatch' "
          "claim is downgraded, closing the real live gap found on school_student")


def test_hallucinated_bare_group_id_prefix_mismatch_filter_survives_reordered_wording():
    """Real, confirmed bug found live (2026-07-29, same task, same
    round, immediately after the first version of this filter
    deployed): the SAME false claim recurred reworded with "module
    prefix" appearing BEFORE "mismatch" instead of after --
    "causing a module prefix mismatch that prevents installation" --
    dodging the original single-order regex. Fixed with an order-
    independent proximity match, same technique already proven
    necessary for _EMPTY_SECURITY_CSV_CLAIM_RE earlier this session.
    """
    from specialists.code_review.specialist import (
        ReviewFinding,
        _filter_hallucinated_bare_group_id_prefix_mismatch_findings,
    )

    module_name = "oma_simple_custom_module_task_595ad7bc"
    files = {
        "security/security.xml": (
            '<odoo><record id="group_school_admin" model="res.groups">'
            '<field name="name">Admin</field></record></odoo>'
        ),
    }
    findings = [
        ReviewFinding(
            location="security/security.xml", severity="blocking",
            explanation=(
                f"Access rule references group '{module_name}.group_school_admin' but "
                "security.xml defines it as 'group_school_admin' (resolving to "
                "'school_student.group_school_admin' or similar), causing a module prefix "
                "mismatch that prevents installation."
            ),
        ),
    ]
    out = _filter_hallucinated_bare_group_id_prefix_mismatch_findings(findings, files, module_name)
    assert out[0].severity == "info", (
        f"expected the reworded false claim ('module prefix mismatch', reversed order) to also "
        f"be downgraded -- got severity {out[0].severity!r}"
    )
    print("PASS: the reworded false claim (module prefix BEFORE mismatch) is also caught, "
          "closing the real gap the first, order-specific regex left open")


def test_hallucinated_model_id_prefix_mismatch_filter_downgrades_real_false_claim():
    """Real, confirmed bug found live (2026-07-29, Phase 28C,
    school_student task, demo_data round): the SAME false-claim shape as
    the bare-group-id-prefix-mismatch filter, but for a model's own
    implicit `model_<name>` xmlid instead of a locally-defined
    `res.groups` record. Code-Review claimed the record rule
    'rule_school_student_delete' referencing 'oma_..._595ad7bc.
    model_school_student' "causes a mismatch" with ir.model.access.csv's
    bare 'model_school_student' reference to the SAME model -- a
    hallucination; both forms resolve to the identical real model.
    """
    from specialists.code_review.specialist import (
        ReviewFinding,
        _filter_hallucinated_model_id_prefix_mismatch_findings,
    )

    module_name = "oma_simple_custom_module_task_595ad7bc"
    files = {
        "security/security.xml": (
            '<odoo><data><record id="rule_school_student_delete" model="ir.rule">'
            f'<field name="model_id" ref="{module_name}.model_school_student"/>'
            "</record></data></odoo>"
        ),
        "security/ir.model.access.csv": (
            "id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink\n"
            "access_school_student,school.student,model_school_student,base.group_user,1,1,1,0\n"
        ),
    }
    findings = [
        ReviewFinding(
            location="security", severity="blocking",
            explanation=(
                "The record rule 'rule_school_student_delete' references "
                f"'{module_name}.model_school_student' but the model_id in ir.model.access.csv is "
                "'model_school_student' without the module prefix, causing a mismatch and potential "
                "loading failure."
            ),
        ),
    ]
    out = _filter_hallucinated_model_id_prefix_mismatch_findings(findings, files, module_name)
    assert out[0].severity == "info", (
        f"expected the false 'model_id prefix mismatch' claim to be downgraded -- got severity "
        f"{out[0].severity!r}"
    )
    print("PASS: the real, confirmed false 'model_id module-prefix mismatch' claim is downgraded, "
          "closing the real live gap found on school_student's demo_data round")


def test_hallucinated_model_id_prefix_mismatch_filter_never_touches_an_unused_model_id():
    from specialists.code_review.specialist import (
        ReviewFinding,
        _filter_hallucinated_model_id_prefix_mismatch_findings,
    )

    module_name = "oma_x"
    files = {"security/security.xml": "<odoo></odoo>"}
    findings = [
        ReviewFinding(
            location="x", severity="blocking",
            explanation=(
                f"References '{module_name}.model_never_used' but access.csv has 'model_never_used' "
                "without the module prefix, causing a mismatch."
            ),
        ),
    ]
    out = _filter_hallucinated_model_id_prefix_mismatch_findings(findings, files, module_name)
    assert out[0].severity == "blocking", (
        "must not downgrade a claim about a model id that never actually appears in this "
        "generation's own real content"
    )
    print("PASS: a model id never actually referenced in this generation's own content is left untouched")


def test_hallucinated_bare_group_id_prefix_mismatch_filter_never_touches_a_genuine_mismatch():
    from specialists.code_review.specialist import (
        ReviewFinding,
        _filter_hallucinated_bare_group_id_prefix_mismatch_findings,
    )

    module_name = "oma_x"
    files = {
        "security/security.xml": (
            '<odoo><record id="group_a" model="res.groups">'
            '<field name="name">A</field></record></odoo>'
        ),
    }
    findings = [
        ReviewFinding(
            location="x", severity="blocking",
            explanation="Some unrelated finding about a totally different mismatch entirely.",
        ),
    ]
    out = _filter_hallucinated_bare_group_id_prefix_mismatch_findings(findings, files, module_name)
    assert out[0].severity == "blocking"
    print("PASS: a genuinely unrelated finding is never touched by this narrow filter")


def test_hallucinated_wrong_model_xmlid_format_filter_downgrades_real_false_claim():
    """Real, confirmed bug found live (2026-07-29, school_student task,
    computed_age_field round escalation): Code-Review claimed
    `ref="model_school_student"` is "incorrect" and that the "correct
    external ID for model 'school.student' in module 'school_student'
    is 'model_school_student_school_student'" -- a hallucination.
    Odoo's real, standard xmlid convention for a model's own
    auto-generated `ir.model` record is exactly
    `model_<model_name_with_dots_as_underscores>`, never a doubled
    module+model form. Confirmed live via repeated real installs of
    this exact bare form across this same task's earlier rounds.
    """
    from specialists.code_review.specialist import (
        ReviewFinding,
        _filter_hallucinated_wrong_model_xmlid_format_findings,
    )

    files = {
        "views/school_student_views.xml": (
            '<?xml version="1.0" encoding="utf-8"?>\n<odoo>\n'
            '    <record id="action_school_student" model="ir.actions.act_window">\n'
            '        <field name="name">Students</field>\n'
            '        <field name="model_id" ref="model_school_student"/>\n'
            '    </record>\n</odoo>'
        ),
    }
    findings = [
        ReviewFinding(
            location="views/school_student_views.xml", severity="blocking",
            explanation=(
                "The external ID 'model_school_student' used in ref=\"model_school_student\" is "
                "incorrect. The correct external ID for model 'school.student' in module "
                "'school_student' is 'model_school_student_school_student'."
            ),
        ),
    ]
    out = _filter_hallucinated_wrong_model_xmlid_format_findings(findings, files)
    assert out[0].severity == "info", (
        f"expected the false 'doubled model xmlid' claim about a genuinely, correctly formed "
        f"bare model_X reference to be downgraded -- got severity {out[0].severity!r}"
    )
    print("PASS: the real, confirmed false 'wrong/doubled model xmlid format' claim is "
          "downgraded, closing the real live gap found on school_student")


def test_hallucinated_wrong_model_xmlid_format_filter_catches_the_csv_column_reworded_variant():
    """Real, confirmed recurrence found live (2026-07-29, same task,
    later computed_age_field round): the SAME false claim recurred
    against the security CSV's own `model_id:id` column (not an XML
    `ref="..."` attribute this time) with different wording -- "the
    access CSV references model_id:id as model_school_student, but the
    correct auto-generated external ID for the model is
    model_oma_simple_custom_module_task_595ad7bc_school_student" --
    prepending the MODULE name rather than doubling the model name.
    "auto-generated" between "correct" and "external id" dodged the
    original tight-adjacency regex; fixed with the same order-
    independent proximity technique already used elsewhere in this
    file. Confirmed false live: the real ir.model.access.csv content
    used the bare form and installed cleanly.
    """
    from specialists.code_review.specialist import (
        ReviewFinding,
        _filter_hallucinated_wrong_model_xmlid_format_findings,
    )

    files = {
        "security/ir.model.access.csv": (
            "id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink\n"
            "access_school_student,school.student,model_school_student,base.group_user,1,1,1,0\n"
        ),
    }
    findings = [
        ReviewFinding(
            location="security/ir.model.access.csv", severity="blocking",
            explanation=(
                "The access CSV references model_id:id as model_school_student, but the correct "
                "auto-generated external ID for the model is "
                "model_oma_simple_custom_module_task_595ad7bc_school_student."
            ),
        ),
    ]
    out = _filter_hallucinated_wrong_model_xmlid_format_findings(findings, files)
    assert out[0].severity == "info", (
        f"expected the CSV-column, module-name-prefixed false claim to be downgraded -- got "
        f"severity {out[0].severity!r}"
    )
    print("PASS: the reworded false claim against the CSV's own model_id:id column (module-name-"
          "prefixed form, 'auto-generated' breaking the tight-adjacency match) is also caught")


def test_hallucinated_wrong_model_xmlid_format_filter_never_touches_a_genuine_mismatch():
    from specialists.code_review.specialist import (
        ReviewFinding,
        _filter_hallucinated_wrong_model_xmlid_format_findings,
    )

    files = {
        "views/x_views.xml": (
            '<odoo><record id="action_x" model="ir.actions.act_window">'
            '<field name="model_id" ref="model_school_student"/></record></odoo>'
        ),
    }
    findings = [
        ReviewFinding(
            location="x", severity="blocking",
            explanation="Some unrelated finding about a totally different xmlid problem entirely.",
        ),
    ]
    out = _filter_hallucinated_wrong_model_xmlid_format_findings(findings, files)
    assert out[0].severity == "blocking"
    print("PASS: a genuinely unrelated finding is never touched by this narrow filter")


def test_hallucinated_wrong_model_xmlid_format_filter_never_touches_a_ref_never_defined():
    """The mirrored bare-model-ref must never be downgraded to `info`
    when it does not actually appear anywhere in this generation's own
    content -- that would paper over a genuinely different, real bug
    (referencing a model this generation never defined at all).
    """
    from specialists.code_review.specialist import (
        ReviewFinding,
        _filter_hallucinated_wrong_model_xmlid_format_findings,
    )

    files = {
        "views/x_views.xml": '<odoo><record id="action_x" model="ir.actions.act_window"/></odoo>',
    }
    findings = [
        ReviewFinding(
            location="x", severity="blocking",
            explanation=(
                "The external ID 'model_other_thing' is incorrect. The correct external ID for "
                "model 'other.thing' in module 'other_module' is 'model_other_thing_other_thing'."
            ),
        ),
    ]
    out = _filter_hallucinated_wrong_model_xmlid_format_findings(findings, files)
    assert out[0].severity == "blocking", (
        "must not downgrade a claim about an xmlid that never actually appears in this "
        "generation's own files -- that could be a genuine bug"
    )
    print("PASS: a claim about an xmlid never actually referenced in this generation's own "
          "content is left untouched")


if __name__ == "__main__":
    test_review_flags_deliberately_bad_pattern_as_blocking()
    test_full_audit_against_real_custom_module()
    test_hallucinated_method_missing_filter_survives_decoy_init_file()
    test_hallucinated_xmlid_missing_filter_downgrades_verified_ref()
    test_hallucinated_direct_field_missing_filter_downgrades_verified_field()
    test_hallucinated_direct_field_missing_filter_also_checks_view_syntax()
    test_hallucinated_direct_field_missing_filter_also_checks_currency_field_kwarg()
    test_hallucinated_empty_security_csv_filter_downgrades_header_only_csv()
    test_hallucinated_empty_security_csv_filter_exempts_decomposed_new_model_round()
    test_hallucinated_own_module_collision_filter_downgrades_real_same_task_continuation()
    test_hallucinated_out_of_scope_field_required_filter_downgrades_contradictory_claim()
    test_hallucinated_stale_scope_exclusion_filter_downgrades_outdated_constraint_name()
    test_hallucinated_sequence_pattern_filter_downgrades_standard_idiom()
    test_hallucinated_redundant_line_filter_downgrades_claim_against_real_single_occurrence()
    test_hallucinated_redundant_line_filter_downgrades_unquoted_parenthesized_claim()
    test_hallucinated_overwrite_filter_downgrades_claim_against_guarded_assignment()
    test_hallucinated_self_excluded_scope_filter_downgrades_self_contradicting_finding()
    test_hallucinated_ir_rule_permission_capped_filter_downgrades_real_shape()
    test_hallucinated_decoration_string_literal_filter_downgrades_real_shape()
    test_hallucinated_self_reports_already_resolved_filter_downgrades_real_shape()
    test_hallucinated_duplicate_method_filter_downgrades_claim_against_real_single_definition()
    test_hallucinated_missing_content_filter_downgrades_claim_against_present_content()
    test_hallucinated_sequence_pattern_filter_downgrades_general_field_empty_string_claim()
    test_hallucinated_syntax_error_filter_downgrades_claim_against_valid_code()
    test_hallucinated_field_not_instantiated_filter_downgrades_a_real_call()
    test_hallucinated_module_registration_filter_downgrades_a_real_scaffold()
    test_hallucinated_bare_group_id_prefix_mismatch_filter_downgrades_real_false_claim()
    test_hallucinated_bare_group_id_prefix_mismatch_filter_survives_reordered_wording()
    test_hallucinated_bare_group_id_prefix_mismatch_filter_never_touches_a_genuine_mismatch()
    test_hallucinated_model_id_prefix_mismatch_filter_downgrades_real_false_claim()
    test_hallucinated_model_id_prefix_mismatch_filter_never_touches_an_unused_model_id()
    test_hallucinated_wrong_model_xmlid_format_filter_downgrades_real_false_claim()
    test_hallucinated_wrong_model_xmlid_format_filter_catches_the_csv_column_reworded_variant()
    test_hallucinated_wrong_model_xmlid_format_filter_never_touches_a_genuine_mismatch()
    test_hallucinated_wrong_model_xmlid_format_filter_never_touches_a_ref_never_defined()
    test_hallucinated_incomplete_compute_dependency_filter_downgrades_invented_scope()
    test_hallucinated_wrong_constraint_attribution_filter_downgrades_real_false_claim()
    test_wrong_capability_class_is_refused()
    test_no_review_target_is_refused_not_guessed()
    test_read_only_enforced_structurally_not_just_by_instruction()
    print("\nALL CODE REVIEW SPECIALIST TESTS PASSED")
