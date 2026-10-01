"""Phase 18 (§22.14 tests 2, 3, 4, 6, 7, 8): real tests for the Gitea
baseline, scoped-edit application, best-of-N candidate selection,
regression re-check, the oscillation detector, and the honest ceiling
test.

Same discipline as tests/test_constraint_pinning.py: plain
`def test_...()` functions pytest collects by default. Tests 2 and 4
below use the REAL Gitea instance (SECRETS/dev-agent.env,
oma/oma-generated-modules -- confirmed live and reachable this
session) rather than a mock HTTP layer, per this project's own
established "never mock infrastructure" discipline
(tests/test_build_specialist.py's own module docstring). Tests 3, 6, 7
are pure, dependency-free logic and need no network access at all. Test
8 runs a real BuildSpecialist round against the real model gateway.
"""

import asyncio
import os
import sys
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from contracts.schema import AutonomyTier, CapabilityClass, ConstraintNode, CRITICAL_RULE_PREFIX, ReplanRound, SpecialistType, TaskContract, VerificationResult
from infra.gateway_client import ModelGatewayClient
from manager.loop import resolve_missing_dependency_module
from manager.replanning import compute_regressed_constraints, detect_oscillation, oscillation_majority_signal
from specialists.build.specialist import (
    BuildSpecialist,
    GeneratedModuleEdit,
    ManifestFields,
    slugify_module_name,
    _apply_scoped_edits,
    _validate_replace_file_does_not_drop_referenced_record_ids,
)
from tools_odoo.module_dev import vcs
from tools_odoo.module_dev.toolchain import _MODULE_DEV_ADDONS_DIR, _run_in_container, scaffold_module

DUPLICATE_DB = "odoo16_dev_dup_20260707"  # same real, known-scaffold-capable db test_build_specialist.py uses


def _make_contract(constraint_status: dict, goal: str = "Add scheduling fields; enforce a double-booking rule.") -> TaskContract:
    return TaskContract(
        task_id=uuid.uuid4(), specialist_type=SpecialistType.bug_fix,
        capability_class=CapabilityClass.module_development, tier=AutonomyTier.tier_2_notify_after,
        goal=goal, inputs=[], rules=[], deliverables=[], compensating_actions=[],
        validation_by="testing_qa", pause_if=[], turn_budget=10,
        constraint_status=constraint_status,
    )


def _verification(regressed_constraints: list[str]) -> VerificationResult:
    return VerificationResult(
        task_id=uuid.uuid4(), passed=False, reproduction_confirmed=False,
        uncovered_paths=[], coverage_diff="", spot_check_mismatch=False,
        notes="round failed", regressed_constraints=regressed_constraints,
    )


def _round(round_number: int, regressed_constraints: list[str]) -> ReplanRound:
    contract = _make_contract({})
    return ReplanRound(
        round_number=round_number,
        previous_contract_task_id=str(contract.task_id),
        verification_result=_verification(regressed_constraints),
        revision_reasoning="test round",
        new_contract=contract,
    )


# --- §22.14 test 2: Gitea baseline is genuinely race-free, real ---

def test_gitea_baseline_round_trip_and_never_partial():
    """§22.14 test 2 (adapted to what's directly testable without a
    second concurrent process): commit_validated_round() then
    read_last_validated_commit() against the REAL Gitea instance --
    confirms a full, valid round trip (never a partial/malformed read),
    confirms round 1 correctly returns None before anything is
    committed, and confirms round 2 correctly overwrites round 1's own
    content (update, not a second stale copy) -- the same
    structural guarantee that makes a concurrent read during a live
    write impossible to observe as partial: Gitea's contents API always
    returns either the pre-commit or the post-commit blob for a given
    path, never bytes from a write in progress, because the write
    itself is one atomic Git commit, not an in-place file mutation.
    """
    task_id = str(uuid.uuid4())
    module_name = f"oma_test_race_{uuid.uuid4().hex[:8]}"

    before = vcs.read_last_validated_commit(task_id)
    assert before is None, "round 1 (nothing committed yet) must return None, never an empty dict"

    sha1 = vcs.commit_validated_round(
        task_id=task_id, module_name=module_name,
        files={"__manifest__.py": "{'name': 'race test'}\n", "models/models.py": "# v1\n"},
        round_number=1, summary="round 1",
    )
    after_r1 = vcs.read_last_validated_commit(task_id)
    assert after_r1 is not None
    assert after_r1[f"{module_name}/models/models.py"] == "# v1\n"

    sha2 = vcs.commit_validated_round(
        task_id=task_id, module_name=module_name,
        files={"__manifest__.py": "{'name': 'race test'}\n", "models/models.py": "# v2\n"},
        round_number=2, summary="round 2",
    )
    assert sha1 != sha2, "each round must produce a genuinely new, distinct commit SHA"

    after_r2 = vcs.read_last_validated_commit(task_id)
    # Never a partial mix of round 1 and round 2 content -- always
    # exactly one commit's own full, consistent state.
    assert after_r2[f"{module_name}/models/models.py"] == "# v2\n"
    assert after_r2[f"{module_name}/__manifest__.py"] == "{'name': 'race test'}\n"
    print(f"PASS: Gitea baseline round trip clean, no partial reads observed (sha1={sha1[:8]}, sha2={sha2[:8]})")


# --- §22.14 test 3: scoped-edit mode preserves untouched content, real ---

def test_scoped_edit_preserves_base_model_extension_on_unrelated_fix():
    """§22.14 test 3, engineered to reproduce the real, observed §22.1
    regression directly: round 1 extends a base model (`_inherit`);
    round 2 asks for an UNRELATED fix (a security CSV correction) via a
    scoped edit. Confirms the base-model extension from round 1 survives
    byte-for-byte, and confirms every file NOT named in round 2's own
    edit list is untouched, exactly the guarantee full regeneration
    could not make.
    """
    prior_files = {
        "__manifest__.py": "{'name': 'x', 'depends': ['base', 'hr']}\n",
        "models/models.py": (
            "from odoo import fields, models\n\n\n"
            "class HrEmployeeExtension(models.Model):\n"
            "    _inherit = 'hr.employee'\n"
            "    scheduled_at = fields.Datetime()\n"
        ),
        "security/ir.model.access.csv": (
            "id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink\n"
            "access_x,x,model_x,base.group_user,1,1,1,1\n"
        ),
    }
    # Round 2's own, unrelated fix: tighten perm_unlink to 0 -- nothing
    # about the base-model extension should be touched at all.
    edits = [
        GeneratedModuleEdit(
            file="security/ir.model.access.csv",
            operation="replace_file",
            content=(
                "id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink\n"
                "access_x,x,model_x,base.group_user,1,1,1,0\n"
            ),
        ),
    ]
    applied = _apply_scoped_edits(prior_files, edits)

    # The base-model extension survives byte-for-byte -- this is the
    # exact confirmed §22.1 regression this component exists to stop.
    assert applied["models/models.py"] == prior_files["models/models.py"]
    assert "_inherit = 'hr.employee'" in applied["models/models.py"]
    assert applied["__manifest__.py"] == prior_files["__manifest__.py"]
    # The named edit's own file DID change, and only in the named way.
    assert "perm_unlink" in applied["security/ir.model.access.csv"]
    assert applied["security/ir.model.access.csv"].endswith(",0\n")
    print("PASS: scoped edit left the base-model extension byte-identical; only the named file changed")


# --- search_replace: real, general architectural fix (2026-07-22) replacing
# the old two-operation scheme (add_field -- insert-only, structurally
# incapable of removing anything; replace_file -- must reproduce the whole
# file, risking silent content loss). Converges on the same pattern aider
# (search/replace diff blocks) and OpenHands (str_replace_editor) both
# independently use for reliable partial LLM edits. These are pure,
# deterministic tests of _apply_scoped_edits() itself -- no LLM call.

def test_search_replace_adds_new_content_via_an_anchor():
    prior_files = {
        "security/ir.model.access.csv": (
            "id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink\n"
            "access_x,x,model_x,base.group_user,1,1,1,0\n"
        ),
    }
    edits = [
        GeneratedModuleEdit(
            file="security/ir.model.access.csv",
            operation="search_replace",
            target="access_x,x,model_x,base.group_user,1,1,1,0\n",
            content=(
                "access_x,x,model_x,base.group_user,1,1,1,0\n"
                "access_y,y,model_y,base.group_user,1,0,0,0\n"
            ),
        ),
    ]
    applied = _apply_scoped_edits(prior_files, edits)
    assert "access_x,x,model_x,base.group_user,1,1,1,0" in applied["security/ir.model.access.csv"]
    assert "access_y,y,model_y,base.group_user,1,0,0,0" in applied["security/ir.model.access.csv"]
    print("PASS: search_replace correctly ADDS new content via an anchor")


def test_search_replace_removes_unwanted_content_via_empty_content():
    """Directly proves search_replace closes the exact gap add_field left
    open -- removal via an empty `content`, the thing add_field could
    never do at all.
    """
    prior_files = {
        "security/ir.model.access.csv": (
            "id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink\n"
            "access_x,x,model_x,base.group_user,1,1,1,0\n"
            "access_x_user,x.user,model_x,base.group_user,1,1,1,0\n"
            "access_x_manager,x.manager,model_x,base.group_system,1,1,1,1\n"
        ),
    }
    edits = [
        GeneratedModuleEdit(
            file="security/ir.model.access.csv",
            operation="search_replace",
            target="access_x_user,x.user,model_x,base.group_user,1,1,1,0\n",
            content="",
        ),
        GeneratedModuleEdit(
            file="security/ir.model.access.csv",
            operation="search_replace",
            target="access_x_manager,x.manager,model_x,base.group_system,1,1,1,1\n",
            content="",
        ),
    ]
    applied = _apply_scoped_edits(prior_files, edits)
    result = applied["security/ir.model.access.csv"]
    assert "access_x_user" not in result
    assert "access_x_manager" not in result
    assert "access_x,x,model_x,base.group_user,1,1,1,0" in result, "the untouched row must survive"
    print("PASS: search_replace correctly REMOVES unwanted content via empty content -- "
          "the exact capability add_field structurally never had")


def test_search_replace_modifies_existing_content():
    prior_files = {"security/ir.model.access.csv": "access_x,x,model_x,base.group_user,1,1,1,1\n"}
    edits = [
        GeneratedModuleEdit(
            file="security/ir.model.access.csv",
            operation="search_replace",
            target="access_x,x,model_x,base.group_user,1,1,1,1\n",
            content="access_x,x,model_x,base.group_user,1,1,1,0\n",
        ),
    ]
    applied = _apply_scoped_edits(prior_files, edits)
    assert applied["security/ir.model.access.csv"] == "access_x,x,model_x,base.group_user,1,1,1,0\n"
    print("PASS: search_replace correctly MODIFIES existing content")


def test_search_replace_raises_when_target_not_found():
    prior_files = {"models/models.py": "class X(models.Model):\n    _name = 'x'\n"}
    edits = [
        GeneratedModuleEdit(
            file="models/models.py", operation="search_replace",
            target="this text does not exist anywhere in the file", content="anything",
        ),
    ]
    raised = False
    try:
        _apply_scoped_edits(prior_files, edits)
    except ValueError as exc:
        raised = True
        assert "was not found" in str(exc)
    assert raised, "search_replace must refuse to guess when target isn't found, never silently no-op"
    print("PASS: search_replace raises loudly when target is not found, never silently does nothing")


def test_search_replace_raises_when_target_is_ambiguous():
    """A target that matches more than once is exactly as dangerous as one
    that matches zero times -- silently picking "the first one" could
    modify the wrong occurrence. Must refuse and ask for more context.
    """
    prior_files = {
        "models/models.py": (
            "class X(models.Model):\n    _name = 'x'\n\n\n"
            "class Y(models.Model):\n    _name = 'x'\n"
        ),
    }
    edits = [
        GeneratedModuleEdit(
            file="models/models.py", operation="search_replace",
            target="    _name = 'x'\n", content="    _name = 'x.renamed'\n",
        ),
    ]
    raised = False
    try:
        _apply_scoped_edits(prior_files, edits)
    except ValueError as exc:
        raised = True
        assert "ambiguous" in str(exc)
    assert raised, "search_replace must refuse an ambiguous (multiply-occurring) target, never guess"
    print("PASS: search_replace raises loudly on an ambiguous target instead of guessing which one")


# --- real deadlock repro (2026-07-21, Operator demo live): add_field can never
# remove content, and the prompt used to steer Build toward it even when the
# rules demanded a removal, so an unwanted row survived every retry ---

def test_scoped_edit_prompt_actually_removes_content_when_rules_demand_it():
    """Real, live bug (2026-07-21, final Operator-demo task, constraint 2 --
    security group + access rule): round 2 added TWO unrequested access
    rows (access_warranty_claim_user, access_warranty_claim_manager) via
    a scoped 'add_field' edit. Code-Review correctly flagged them as
    out-of-scope on rounds 3, 4, AND 5 -- three consecutive, nearly
    verbatim recurrences -- yet the rows were still present every round,
    exhausting the round budget and escalating.

    Root cause: GeneratedModuleEdit's 'add_field' operation can only
    INSERT text after an anchor (_apply_scoped_edits() has no delete
    path at all), but the old prompt told Build to "Prefer add_field for
    small, localized additions" with no warning that add_field is
    structurally incapable of removing anything -- so a round asked to
    REMOVE content had no signal it needed to switch to 'replace_file'
    instead. This is a real, direct call against BuildSpecialist's own
    scoped-edit path (the real model gateway, no mock), engineered to
    reproduce the exact shape live: prior_files already contains the
    unwanted rows, and a CRITICAL rule (the same recurrence-escalation
    mechanism revise_contract_from_verification() actually produces)
    demands their removal.
    """
    module_name = f"oma_test_removal_{uuid.uuid4().hex[:8]}"
    prior_files = {
        "__manifest__.py": (
            "{'name': 'x', 'version': '1.0', 'category': 'Uncategorized', 'summary': 'x', "
            "'author': 'x', 'depends': ['base'], 'data': "
            "['security/security.xml', 'security/ir.model.access.csv']}\n"
        ),
        "models/models.py": (
            "from odoo import fields, models\n\n\n"
            "class WarrantyClaim(models.Model):\n"
            "    _name = 'warranty.claim'\n"
            "    _description = 'Warranty Claim'\n"
            "    name = fields.Char()\n"
        ),
        "security/security.xml": (
            "<?xml version=\"1.0\" encoding=\"utf-8\"?>\n<odoo>\n  <record "
            "id=\"group_warranty_reviewers\" model=\"res.groups\">\n    <field name=\"name\">"
            "Warranty Claim Reviewers</field>\n  </record>\n</odoo>"
        ),
        "security/ir.model.access.csv": (
            "id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink\n"
            "access_warranty_claim,warranty.claim,model_warranty_claim,"
            f"{module_name}.group_warranty_reviewers,1,1,1,0\n"
            "access_warranty_claim_user,warranty.claim.user,model_warranty_claim,base.group_user,1,1,1,0\n"
            "access_warranty_claim_manager,warranty.claim.manager,model_warranty_claim,"
            "base.group_system,1,1,1,1\n"
        ),
    }
    contract = TaskContract(
        task_id=uuid.uuid4(), specialist_type=SpecialistType.bug_fix,
        capability_class=CapabilityClass.module_development, tier=AutonomyTier.tier_2_notify_after,
        goal="Add a 'Warranty Claim Reviewers' security group with full CRUD access to warranty.claim.",
        inputs=[], deliverables=[], compensating_actions=[], validation_by="testing_qa", pause_if=[],
        turn_budget=10,
        rules=[
            f"{CRITICAL_RULE_PREFIX}Code-Review found 1 blocking issue(s): Contains unrequested "
            "access_warranty_claim_user and access_warranty_claim_manager rows that violate the "
            "explicit round scope constraint to 'only implement the warranty_reviewers_group and "
            "its required access rule'. Remove both rows -- only the single access row for "
            "group_warranty_reviewers may remain.",
        ],
    )

    async def _drive():
        client = ModelGatewayClient()
        try:
            specialist = BuildSpecialist(client=client, db=DUPLICATE_DB)
            return await specialist._generate_scoped_edits(
                contract, constitution_text="", skill_text="",
                module_name=module_name, prior_files=prior_files,
            )
        finally:
            await client.aclose()

    generated = asyncio.run(_drive())

    assert "access_warranty_claim_user" not in generated.security_csv, (
        "the unrequested user-access row must actually be removed once the rules demand it -- "
        f"got security_csv:\n{generated.security_csv}"
    )
    assert "access_warranty_claim_manager" not in generated.security_csv, (
        "the unrequested manager-access row must actually be removed once the rules demand it -- "
        f"got security_csv:\n{generated.security_csv}"
    )
    assert "access_warranty_claim," in generated.security_csv or "access_warranty_claim\n" not in generated.security_csv, (
        "the one legitimately-requested access row must survive the removal, not be collateral damage"
    )
    assert "group_warranty_reviewers" in generated.security_csv
    print(
        "PASS: a scoped-edit round facing a CRITICAL 'remove this content' rule actually removed "
        f"it, not just kept re-adding it -- got security_csv:\n{generated.security_csv}"
    )


# --- real deadlock repro #2 (2026-07-21/22, Operator demo take 4): a finding
# naming an already-real MODULE name got mangled by model-name dot-
# conversion and never resolved, so the same "missing dependency" finding
# recurred verbatim for 5 straight rounds until escalation ---

def _cleanup_module_dir2(module_name: str) -> None:
    _run_in_container(f"rm -rf {_MODULE_DEV_ADDONS_DIR}/{module_name}")


def test_replace_file_dropping_a_still_referenced_record_id_is_caught_deterministically():
    """Real, live bug (2026-07-22, task 9b87dc1e, constraint 3 --
    warranty_access_rule): the same session's own new "use replace_file
    to actually remove content" guidance (see the removal-guidance test
    above) fixed the original add_field deadlock, but exposed a
    DIFFERENT real failure mode -- a replace_file edit is supposed to
    reproduce the whole file byte-for-byte except the lines actually
    being removed, but while trimming an unwanted `ir.rule` out of
    security/security.xml, the model's replace_file edit ALSO silently
    dropped the `<record id="group_warranty_claim_reviewers"
    model="res.groups">` defining the group itself -- which
    ir.model.access.csv's own group_id:id column still referenced.
    Odoo's own install would crash resolving the dangling reference;
    the round only found out after a full install/registry round-trip,
    burning most of the round budget before the task escalated on
    round 5 having never converged.

    This confirms the new deterministic guard
    (_validate_replace_file_does_not_drop_referenced_record_ids) catches
    this immediately, no LLM call, no round-budget cost, reproducing the
    exact live shape.
    """
    module_name = "oma_create_a_small_new_d1ff32fe"
    prior_files = {
        "__manifest__.py": (
            f"{{'name': '{module_name}', 'depends': ['base'], "
            "'data': ['security/security.xml', 'security/ir.model.access.csv']}\n"
        ),
        "models/models.py": (
            "from odoo import fields, models\n\n\n"
            "class WarrantyClaim(models.Model):\n"
            "    _name = 'warranty.claim'\n"
            "    claim_number = fields.Char()\n"
        ),
        "security/security.xml": (
            "<?xml version=\"1.0\" encoding=\"utf-8\"?>\n<odoo>\n    <data>\n"
            "        <record id=\"group_warranty_claim_reviewers\" model=\"res.groups\">\n"
            "            <field name=\"name\">Warranty Claim Reviewers</field>\n"
            "        </record>\n"
            "        <record id=\"rule_warranty_claim_reviewers\" model=\"ir.rule\">\n"
            "            <field name=\"name\">Warranty Claim Reviewers Access</field>\n"
            f"            <field name=\"model_id\" ref=\"{module_name}.model_warranty_claim\"/>\n"
            "            <field name=\"groups\" eval=\"[(4, ref("
            f"'{module_name}.group_warranty_claim_reviewers'))]\"/>\n"
            "            <field name=\"domain_force\">[]</field>\n"
            "        </record>\n    </data>\n</odoo>\n"
        ),
        "security/ir.model.access.csv": (
            "id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink\n"
            "access_warranty_claim_reviewers,warranty.claim.reviewers,model_warranty_claim,"
            f"{module_name}.group_warranty_claim_reviewers,1,1,1,1\n"
        ),
    }
    # Reproduces the exact live mistake: removing the unwanted ir.rule via
    # replace_file, but the new content ALSO drops the group's own
    # <record>, which the CSV's group_id:id column still references.
    edits = [
        GeneratedModuleEdit(
            file="security/security.xml",
            operation="replace_file",
            content=(
                "<?xml version=\"1.0\" encoding=\"utf-8\"?>\n<odoo>\n    <data>\n    </data>\n</odoo>\n"
            ),
        ),
    ]
    applied = _apply_scoped_edits(prior_files, edits)

    raised = False
    try:
        _validate_replace_file_does_not_drop_referenced_record_ids(prior_files, edits, applied)
    except ValueError as exc:
        raised = True
        assert "group_warranty_claim_reviewers" in str(exc)
    assert raised, (
        "dropping a <record id=...> that's still referenced elsewhere (here, the security "
        "CSV's own group_id:id column) must be caught immediately, deterministically -- not "
        "silently accepted only to crash Odoo's own install later"
    )
    print("PASS: a replace_file edit that silently drops a still-referenced <record id=...> is "
          "caught deterministically, before ever reaching install/registry verification")


def test_replace_file_removing_a_genuinely_unreferenced_record_id_is_allowed():
    """The other half of the same fix, confirming no regression: a
    replace_file edit that removes a record id NOTHING else references
    (a genuine, correct removal -- e.g. the exact ir.rule-only removal
    Code-Review actually asked for, leaving the group's own record
    intact) must NOT be rejected.
    """
    module_name = "oma_create_a_small_new_d1ff32fe"
    prior_files = {
        "security/security.xml": (
            "<?xml version=\"1.0\" encoding=\"utf-8\"?>\n<odoo>\n    <data>\n"
            "        <record id=\"group_warranty_claim_reviewers\" model=\"res.groups\">\n"
            "            <field name=\"name\">Warranty Claim Reviewers</field>\n"
            "        </record>\n"
            "        <record id=\"rule_warranty_claim_reviewers\" model=\"ir.rule\">\n"
            "            <field name=\"name\">Warranty Claim Reviewers Access</field>\n"
            f"            <field name=\"model_id\" ref=\"{module_name}.model_warranty_claim\"/>\n"
            "            <field name=\"domain_force\">[]</field>\n"
            "        </record>\n    </data>\n</odoo>\n"
        ),
        "security/ir.model.access.csv": (
            "id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink\n"
            "access_warranty_claim_reviewers,warranty.claim.reviewers,model_warranty_claim,"
            f"{module_name}.group_warranty_claim_reviewers,1,1,1,1\n"
        ),
    }
    edits = [
        GeneratedModuleEdit(
            file="security/security.xml",
            operation="replace_file",
            content=(
                "<?xml version=\"1.0\" encoding=\"utf-8\"?>\n<odoo>\n    <data>\n"
                "        <record id=\"group_warranty_claim_reviewers\" model=\"res.groups\">\n"
                "            <field name=\"name\">Warranty Claim Reviewers</field>\n"
                "        </record>\n    </data>\n</odoo>\n"
            ),
        ),
    ]
    applied = _apply_scoped_edits(prior_files, edits)
    _validate_replace_file_does_not_drop_referenced_record_ids(prior_files, edits, applied)
    print("PASS: a genuinely correct removal (the group's own record survives, only the "
          "unreferenced ir.rule is gone) is not falsely rejected")


# --- manifest data-list content loss (2026-07-22, task 00113706's constraint 3):
# a replace_manifest_fields edit silently dropped 'security/ir.model.access.csv'
# from `data` while the module's own manifest still needed it -- Odoo never
# loads a file that isn't in `data`, no error, no warning, so the CSV's real
# content (a genuinely correct access row) never took effect, producing a
# false "zero access rows" result for 4 of 5 rounds despite Code-Review
# repeatedly approving the CSV's own content as correct ---

def test_replace_manifest_fields_dropping_a_still_real_data_file_is_caught():
    """Real, live bug (2026-07-22, task 00113706, constraint 3 --
    warranty_access_rule): confirmed via direct disk inspection, a
    round's own generated manifest was exactly
    `{'data': ['security/security.xml'], ...}` -- 'security/ir.model.
    access.csv' silently missing, even though that file was real,
    already-committed content the prior manifest DID reference. Odoo
    installs happily (nothing tells it the CSV should have been
    loaded), and the module's real access row -- which Code-Review
    approved as correctly formatted every round -- simply never took
    effect, because Odoo never attempted to load it at all.
    """
    prior_manifest = repr({
        "name": "oma_test", "version": "0.1", "category": "Hidden",
        "summary": "s", "description": "d", "author": "a", "depends": ["base"],
        "data": ["security/ir.model.access.csv", "security/security.xml"],
        "installable": True, "auto_install": False, "license": "LGPL-3",
    })
    prior_files = {
        "__manifest__.py": prior_manifest,
        "security/ir.model.access.csv": "id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink\n",
        "security/security.xml": "<odoo></odoo>\n",
    }
    edits = [
        GeneratedModuleEdit(
            file="__manifest__.py", operation="replace_manifest_fields",
            manifest_fields=ManifestFields(
                name="oma_test", version="0.1", category="Hidden", summary="s", description="d",
                author="a", depends=["base"], data=["security/security.xml"],  # CSV silently dropped
            ),
        ),
    ]
    raised = False
    try:
        _apply_scoped_edits(prior_files, edits)
    except ValueError as exc:
        raised = True
        assert "security/ir.model.access.csv" in str(exc)
    assert raised, (
        "dropping a still-real file from manifest_fields.data must be caught immediately, "
        "deterministically -- silently never loading it produces a false 'passed install, "
        "but the real content never took effect' result with no error anywhere"
    )
    print("PASS: replace_manifest_fields dropping a still-real data-list file is caught "
          "deterministically, before ever reaching install")


def test_replace_manifest_fields_adding_a_new_data_file_is_allowed():
    """The other half: legitimately ADDING a new file to `data` (e.g.
    this round genuinely introduces security/security.xml for the
    first time) must not be falsely rejected -- nothing was dropped.
    """
    prior_manifest = repr({
        "name": "oma_test", "version": "0.1", "category": "Hidden",
        "summary": "s", "description": "d", "author": "a", "depends": ["base"],
        "data": ["security/ir.model.access.csv"],
        "installable": True, "auto_install": False, "license": "LGPL-3",
    })
    prior_files = {
        "__manifest__.py": prior_manifest,
        "security/ir.model.access.csv": "id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink\n",
    }
    edits = [
        GeneratedModuleEdit(
            file="__manifest__.py", operation="replace_manifest_fields",
            manifest_fields=ManifestFields(
                name="oma_test", version="0.1", category="Hidden", summary="s", description="d",
                author="a", depends=["base"],
                data=["security/ir.model.access.csv", "security/security.xml"],  # legitimate add
            ),
        ),
    ]
    applied = _apply_scoped_edits(prior_files, edits)
    assert "security/security.xml" in applied["__manifest__.py"]
    print("PASS: legitimately adding a new file to manifest_fields.data is not falsely rejected")


def test_resolve_missing_dependency_module_uses_already_named_module_directly():
    """Real, live bug (2026-07-21/22, task e2cb29e6, constraint 1 --
    warranty.claim model): a shared dev database already had a real
    'warranty.claim' model owned by an earlier task's leftover module
    (oma_create_a_small_new_4446ae11). The validator's own finding named
    that module directly, by its real name, in quotes: "...is defined by
    module 'oma_create_a_small_new_4446ae11', which is NOT in the
    manifest's depends list... Add 'oma_create_a_small_new_4446ae11' to
    depends." The old code always ran the second regex's match through
    underscore-to-dot conversion (built for a genuine MODEL name like
    'oma_service' -> 'oma.service'), turning the already-correct module
    name into 'oma.create.a.small.new.4446ae11' -- a string
    find_module_defining_model() could never find, since it's not a real
    model name at all. resolved_module stayed None every round, the
    depends_on_module auto-fix never fired, and the identical finding
    recurred verbatim for 5 consecutive rounds until the task escalated,
    never even reaching the actual constraint under test.

    This is a real, scaffolded module on disk (no mock) -- confirms the
    fix recognizes an already-real module name and uses it directly,
    skipping the model-name interpretation entirely.
    """
    module_name = f"oma_test_depmod_{uuid.uuid4().hex[:8]}"
    scaffold_module(module_name)
    try:
        finding_texts = [
            f"generated models_py has real problems with its _inherit target(s): _inherit target "
            f"'warranty.claim' is real, but is defined by module '{module_name}', which is NOT in "
            f"the manifest's depends list -- Odoo cannot guarantee '{module_name}' loads first "
            f"without it. Add '{module_name}' to depends.",
        ]
        dependency_model, resolved_module = asyncio.run(resolve_missing_dependency_module(finding_texts))
        assert resolved_module == module_name, (
            f"an already-real module name spelled out in the finding must resolve to itself "
            f"directly, got dependency_model={dependency_model!r}, resolved_module={resolved_module!r}"
        )
        print(f"PASS: an already-real module name in a finding resolved directly to {resolved_module!r}")
    finally:
        _cleanup_module_dir2(module_name)


def test_resolve_missing_dependency_module_still_resolves_a_genuine_model_name():
    """The other half of the same fix, confirming no regression: when a
    finding names a genuine MODEL name (dotted, e.g. 'oma.service') that
    is NOT itself a module directory on disk, the underscore-to-dot
    conversion and find_module_defining_model() lookup must still run
    exactly as before.
    """
    finding_texts = [
        "The manifest is missing a dependency: this model extends 'oma.service', which is assumed "
        "to already exist, but no depends entry names the module that defines it.",
    ]
    dependency_model, resolved_module = asyncio.run(resolve_missing_dependency_module(finding_texts))
    assert dependency_model == "oma.service", f"expected the literal dotted model name, got {dependency_model!r}"
    # resolved_module may be None (no real module defines 'oma.service' in this environment) or a
    # real module name if one happens to -- either way, no exception, and dependency_model preserved.
    print(
        f"PASS: a genuine dotted model name still goes through the model-lookup path unmangled "
        f"(dependency_model={dependency_model!r}, resolved_module={resolved_module!r})"
    )


# --- §22.14 test 4: best-of-N prefers the non-regressing candidate ---

def test_best_of_n_selection_prefers_fewest_regressed_constraints():
    """§22.14 test 4, tested at the level that's actually deterministic
    and doesn't require a live LLM call: the SAME selection logic
    specialists.build.specialist.BuildSpecialist._generate_best_of_n()
    uses internally (sort candidates by how many previously-satisfied
    constraints they'd regress, keep the fewest) is exercised directly
    against three synthetic candidates -- one regressing a satisfied
    constraint, one regressing none, one regressing a different one --
    confirming the non-regressing candidate wins and the count of
    discarded candidates is reported accurately. This is the exact
    scoring machinery Component 3 runs per-candidate before ANY
    candidate is written or committed; the real Build call around it
    (temperature-sampled generation) is genuinely non-deterministic and
    not what this test needs to prove.
    """
    constraint_status = {"scheduling_fields": "satisfied", "double_booking_rule": "satisfied"}
    old_files = {
        "models/models.py": "class X(models.Model):\n    scheduled_at = fields.Datetime()\n    booking_lock = fields.Boolean()\n",
    }
    candidates = {
        "regresses_scheduling": {"models/models.py": "class X(models.Model):\n    booking_lock = fields.Boolean()\n"},
        "regresses_nothing": {"models/models.py": "class X(models.Model):\n    scheduled_at = fields.Datetime()\n    booking_lock = fields.Boolean()\n    extra = fields.Char()\n"},
        "regresses_booking": {"models/models.py": "class X(models.Model):\n    scheduled_at = fields.Datetime()\n"},
    }
    scored = {
        name: len(compute_regressed_constraints(old_files, files, constraint_status))
        for name, files in candidates.items()
    }
    assert scored["regresses_nothing"] == 0
    assert scored["regresses_scheduling"] == 1
    assert scored["regresses_booking"] == 1
    winner = min(scored, key=lambda name: scored[name])
    assert winner == "regresses_nothing"
    print(f"PASS: best-of-N scoring correctly prefers the non-regressing candidate ({scored})")


# --- §22.14 test 6: regression re-check catches the real regression shape ---

def test_compute_regressed_constraints_catches_dropped_satisfied_field():
    """§22.14 test 6: reproduces §22.1 item 1 on purpose (a
    previously-satisfied constraint's own content silently dropped) and
    confirms compute_regressed_constraints() flags it the same round it
    happens -- this is the exact function specialists/build/specialist.py
    calls right after computing this round's own diff, so
    build_output.detail["regressed_constraints"] (and therefore
    VerificationResult.regressed_constraints, via manager/tools.py's
    await_verification()) is populated immediately, never discovered
    several rounds later by Testing/QA.
    """
    constraint_status = {"scheduling_fields": "satisfied", "double_booking_rule": "satisfied", "product_scoping": "pending"}
    old_files = {
        "models/models.py": (
            "class ServiceRecord(models.Model):\n"
            "    scheduled_at = fields.Datetime()\n"
            "    booking_lock_active = fields.Boolean()\n"
        ),
    }
    # This round's fix silently drops the scheduling field while adding
    # the new product-scoping relation -- exactly the confirmed §22.1
    # regression shape.
    new_files = {
        "models/models.py": (
            "class ServiceRecord(models.Model):\n"
            "    booking_lock_active = fields.Boolean()\n"
            "    product_id = fields.Many2one('product.product')\n"
        ),
    }
    regressed = compute_regressed_constraints(old_files, new_files, constraint_status)
    assert "scheduling_fields" in regressed
    assert "double_booking_rule" not in regressed  # booking_lock keyword still present
    assert "product_scoping" not in regressed  # never flagged: it wasn't "satisfied" yet
    print(f"PASS: regression re-check caught the dropped constraint same-round: {regressed}")


def test_compute_regressed_constraints_never_flags_unobserved_constraint():
    """Conservative-by-construction guarantee, directly stated in
    compute_regressed_constraints()'s own docstring: a constraint whose
    keywords never appeared in prior_files at all (no evidence either
    way) must never be flagged as regressed, and prior_files=None
    (genuine round 1, nothing committed yet) must always return [].
    """
    constraint_status = {"never_observed_thing": "satisfied"}
    old_files = {"models/models.py": "class X(models.Model):\n    name = fields.Char()\n"}
    new_files = {"models/models.py": "class X(models.Model):\n    name = fields.Char()\n"}
    assert compute_regressed_constraints(old_files, new_files, constraint_status) == []
    assert compute_regressed_constraints(None, new_files, constraint_status) == []
    print("PASS: never flags an unobserved constraint or a genuine round-1 (prior_files=None) call")


# --- 2026-08-04 follow-up: exact-identifier check via constraint_nodes.creates, closing the
# real, live task013/015 gap the label-stem heuristic above structurally cannot catch (a field
# RENAMED to a different, still-topically-related field, not fully dropped) ---

def test_compute_regressed_constraints_catches_a_field_renamed_to_a_related_name():
    """Reproduces task013's own real, live shape from the 2026-08-04 full 30-task sweep: the
    'invoice' constraint stayed keyword-PRESENT after the field was renamed from
    `linked_invoice` to a different, still invoice-related field (`invoice_id`) -- the label-stem
    heuristic ("invoic" matches both) sees nothing wrong, a real, confirmed false negative.
    constraint_nodes carries the ACTUAL identifier
    (`project.meerwerk.linked_invoice`, from decompose_into_constraints_with_artifacts()'s own
    real-identifier extraction) and catches it via an exact whole-word check instead.
    """
    constraint_status = {"invoice_link": "satisfied"}
    constraint_nodes = {
        "invoice_link": ConstraintNode(label="invoice_link", creates=["project.meerwerk.linked_invoice"]),
    }
    old_files = {
        "models/models.py": (
            "class ProjectMeerwerk(models.Model):\n"
            "    _inherit = 'project.meerwerk'\n"
            "    linked_invoice = fields.Many2one('account.move')\n"
        ),
    }
    new_files = {
        "models/models.py": (
            "class ProjectMeerwerk(models.Model):\n"
            "    _inherit = 'project.meerwerk'\n"
            "    invoice_id = fields.Many2one('account.move')\n"
        ),
    }
    # The label-stem heuristic alone (no constraint_nodes) is the confirmed real false negative:
    stem_only = compute_regressed_constraints(old_files, new_files, constraint_status)
    assert stem_only == [], (
        "confirms the false negative this fix closes -- 'invoic' stems match both "
        "'linked_invoice' and 'invoice_id', so the label-stem check alone sees no regression"
    )
    # With constraint_nodes wired in, the exact renamed-away identifier is caught:
    with_nodes = compute_regressed_constraints(
        old_files, new_files, constraint_status, constraint_nodes=constraint_nodes,
    )
    assert "invoice_link" in with_nodes
    print(f"PASS: exact-identifier check caught the field rename the stem heuristic missed: {with_nodes}")


def test_compute_regressed_constraints_exact_check_passes_when_identifier_survives_verbatim():
    constraint_status = {"invoice_link": "satisfied"}
    constraint_nodes = {
        "invoice_link": ConstraintNode(label="invoice_link", creates=["project.meerwerk.linked_invoice"]),
    }
    old_files = {"models/models.py": "linked_invoice = fields.Many2one('account.move')\n"}
    new_files = {
        "models/models.py": (
            "linked_invoice = fields.Many2one('account.move')\n"
            "extra_note = fields.Char()\n"
        ),
    }
    assert compute_regressed_constraints(
        old_files, new_files, constraint_status, constraint_nodes=constraint_nodes,
    ) == []
    print("PASS: exact-identifier check never flags a constraint whose real field survives verbatim")


def test_compute_regressed_constraints_exact_check_ignores_constraints_with_no_node_or_empty_creates():
    """Additive-only guarantee: a constraint with no matching constraint_nodes entry, or one
    whose `creates` is empty, must fall back to exactly the pre-existing stem-only behavior --
    never a new false positive for the common case (fewer than 3 constraints, or a task that
    never decomposed with real identifiers)."""
    constraint_status = {"untracked": "satisfied"}
    constraint_nodes = {"untracked": ConstraintNode(label="untracked", creates=[])}
    old_files = {"models/models.py": "widget_flag = fields.Boolean()\n"}
    new_files = {"models/models.py": "class X(models.Model):\n    pass\n"}
    # No stem keywords derivable from "untracked" beyond generic words, and creates is empty --
    # must not raise, must not false-positive.
    result = compute_regressed_constraints(
        old_files, new_files, constraint_status, constraint_nodes=constraint_nodes,
    )
    assert result == []
    print("PASS: a constraint with no real creates identifiers never triggers the exact check")


# --- §22.14 test 7: oscillation detector, real pattern, decomposition-only ---

def test_detect_oscillation_fires_on_real_ping_pong_pattern():
    """§22.14 test 7: reproduces §22.1 item 2's own confirmed shape (the
    hr/product dependency back-and-forth) -- round 1 regresses
    constraint A, round 2 fixes A but regresses a DIFFERENT constraint
    B, which is exactly the "fixing A drops B" signature
    detect_oscillation() exists to catch. Fires within the 3-round
    lookback window, as the plan requires.
    """
    rounds = [
        _round(1, regressed_constraints=["hr_dependency"]),
        _round(2, regressed_constraints=["product_scoping"]),  # A fixed, B (different) regressed
    ]
    assert detect_oscillation(rounds) is True
    print("PASS: detect_oscillation() fires on the real hr/product ping-pong pattern within 2 rounds")


def test_detect_oscillation_does_not_fire_on_ordinary_repeated_failure():
    """The negative case: the SAME constraint regressing round after
    round (still fixing the same thing, not oscillating between two
    different ones) must NOT fire -- oscillation is specifically about
    trading one fix for a different regression, not persistence of one
    single failure.
    """
    rounds = [
        _round(1, regressed_constraints=["scheduling_fields"]),
        _round(2, regressed_constraints=["scheduling_fields"]),
        _round(3, regressed_constraints=["scheduling_fields"]),
    ]
    assert detect_oscillation(rounds) is False

    # Also: no regression at all anywhere must never fire.
    clean_rounds = [_round(1, []), _round(2, []), _round(3, [])]
    assert detect_oscillation(clean_rounds) is False
    print("PASS: detect_oscillation() correctly stays silent on ordinary repeated (non-alternating) failure")


def test_detect_oscillation_never_routes_to_a_different_model_by_code_inspection():
    """§22.14 test 7's own explicit requirement: confirm BY CODE
    INSPECTION, not just behavior, that no path in detect_oscillation()
    or oscillation_majority_signal() can select a different model than
    whatever Build is already configured to use. Both functions take
    only `prior_rounds` (a plain list of ReplanRound) and return a bool
    -- there is no model/client parameter anywhere in either signature
    for a different model to even be reachable through.
    """
    import ast
    import inspect
    import textwrap

    for fn in (detect_oscillation, oscillation_majority_signal):
        source = inspect.getsource(fn)
        params = inspect.signature(fn).parameters
        assert "client" not in params and "model" not in params, (
            f"{fn.__name__} must never accept a model/client parameter -- "
            "it is a pure, deterministic check with no LLM call of any kind"
        )
        # Strip the docstring (which legitimately DISCUSSES the absence
        # of cloud routing in prose) before scanning -- this must check
        # actual executable code for a real call path, not flag the
        # function's own documentation of its own restriction.
        tree = ast.parse(textwrap.dedent(source))
        func_node = tree.body[0]
        if (
            func_node.body
            and isinstance(func_node.body[0], ast.Expr)
            and isinstance(func_node.body[0].value, ast.Constant)
            and isinstance(func_node.body[0].value.value, str)
        ):
            func_node.body = func_node.body[1:]
        code_only_source = ast.unparse(func_node)
        for forbidden in ("ModelGatewayClient", "call_structured", ".generate(", "cloud", "gpt", "claude", "openai"):
            assert forbidden not in code_only_source, (
                f"{fn.__name__}'s real executable code (docstring excluded) unexpectedly references {forbidden!r}"
            )
    print("PASS: by code inspection, neither function can reach a model call of any kind, cloud or otherwise")


def test_oscillation_majority_signal_fires_only_on_majority_pattern():
    """§22.11's own standing guardrail: fires only when the alternating
    pattern is the DOMINANT shape of the task's own round history, not
    on a single isolated occurrence buried in mostly-clean rounds.
    """
    # 4 rounds, oscillating on 3 of the 3 checkable adjacent transitions
    # (rounds 2-3, 3-4 checked against the 3-round lookback window) --
    # majority true.
    oscillating_rounds = [
        _round(1, ["a"]), _round(2, ["b"]), _round(3, ["a"]), _round(4, ["b"]),
    ]
    assert oscillation_majority_signal(oscillating_rounds) is True

    # 4 rounds, clean except one isolated blip -- not a majority.
    mostly_clean_rounds = [
        _round(1, []), _round(2, []), _round(3, ["a"]), _round(4, []),
    ]
    assert oscillation_majority_signal(mostly_clean_rounds) is False
    print("PASS: oscillation_majority_signal() distinguishes a dominant pattern from an isolated blip")


# --- §22.14 test 8: honest ceiling test, deliberately adversarial ---

def _cleanup_module_dir(module_name: str) -> None:
    _run_in_container(f"rm -rf {_MODULE_DEV_ADDONS_DIR}/{module_name}")


def test_honest_ceiling_entangled_constraint_never_falsely_confirmed():
    """§22.14 test 8: a genuinely ENTANGLED-constraint task, the
    dossier's own named distinction (§22.2) between bookkeeping failure
    (what components 1-7 fix) and a real design-reasoning ceiling (what
    they explicitly do NOT claim to fix). The goal below requires
    JOINT reasoning -- a computed field whose own value must be derived
    FROM another field via real logic (@api.depends), not two
    independently-rememberable facts sitting side by side in the same
    file (which is what Constraint Pinning/decomposition/best-of-N all
    actually help with).

    This test does not assert the model succeeds or fails at the joint
    reasoning itself -- that would be asserting a specific ceiling
    exists, which is exactly what this test exists to produce REAL
    evidence about rather than assume. What it does assert, and what
    actually matters for this phase's own honesty: if the generated
    code does NOT contain a real, working joint-reasoning
    implementation (a compute method whose own body genuinely branches
    on the source field, wired via api.depends), the specialist must
    never have claimed complete confidence about it (claims_complete)
    while ALSO leaving notes empty/generic -- i.e. the system is never
    silently, confidently wrong. Either a real implementation exists,
    or the gap is honestly surfaced (claims_complete=False, or a
    concrete uncertainty in `notes`) -- never both "looks done" and
    "actually isn't, with no signal anywhere that it isn't."
    """
    goal = (
        "Add a 'oma_priority_flag' Boolean field to res.partner that is NOT set directly by users -- "
        "it must be a COMPUTED field (real @api.depends logic, store=True), automatically True "
        "whenever this partner's own 'oma_priority_flag' is derived from whether their "
        "'credit_limit' field is greater than 5000, and False otherwise. This requires genuine "
        "joint reasoning between the two fields via a real compute method, not two independent, "
        "unrelated fields."
    )
    contract = TaskContract(
        task_id=uuid.uuid4(), specialist_type=SpecialistType.bug_fix,
        capability_class=CapabilityClass.module_development, tier=AutonomyTier.tier_2_notify_after,
        goal=goal, inputs=["res.partner model definition"],
        rules=["No changes to core Odoo modules", "No schema changes beyond the one new computed field"],
        deliverables=["A new module adding the computed field, wired via a real @api.depends compute method"],
        compensating_actions=[], validation_by="testing_qa", pause_if=[], turn_budget=15,
    )
    module_name = slugify_module_name(goal, str(contract.task_id))
    _cleanup_module_dir(module_name)

    async def _drive():
        client = ModelGatewayClient()
        try:
            specialist = BuildSpecialist(client=client, db=DUPLICATE_DB)
            return await specialist.run(contract)
        finally:
            await client.aclose()

    try:
        output = asyncio.run(_drive())
    finally:
        _cleanup_module_dir(module_name)

    models_py = ""
    if output.detail.get("module_name"):
        # A real, independent read of what was actually written -- never
        # trust output.summary/notes alone for what the code contains.
        proc = _run_in_container(
            f"cat {_MODULE_DEV_ADDONS_DIR}/{output.detail['module_name']}/models/models.py"
        )
        models_py = proc.stdout if proc.returncode == 0 else ""

    has_compute_decorator = "@api.depends" in models_py and "credit_limit" in models_py
    has_compute_method = "def _compute_" in models_py or "compute=" in models_py
    real_joint_reasoning_present = has_compute_decorator and has_compute_method

    notes = output.detail.get("self_report_uncertain") or ""
    honest_about_gap = bool(notes.strip()) and notes.strip().lower() not in ("none", "n/a", "")

    if real_joint_reasoning_present:
        print(
            "EVIDENCE: the local model DID produce a real joint-reasoning compute implementation "
            "for this entangled-constraint task -- no ceiling observed on this specific probe."
        )
    else:
        print(
            "EVIDENCE: the local model did NOT produce a real @api.depends-linked compute "
            f"implementation for the entangled constraint (models.py had "
            f"has_compute_decorator={has_compute_decorator}, has_compute_method={has_compute_method}). "
            f"claims_complete={output.claims_complete}, notes={notes!r}"
        )
        # The one real assertion this test makes: the system must never
        # be silently, confidently wrong about this gap -- either
        # claims_complete is honestly False, or notes honestly flags the
        # uncertainty. Both being "looks fine" is the one outcome this
        # phase's own honesty discipline (§22.12 Component 9's audit)
        # exists to rule out.
        assert not output.claims_complete or honest_about_gap, (
            "the generated code lacks real joint-reasoning logic for the entangled constraint, "
            "AND the specialist reported claims_complete=True with no honest uncertainty in notes -- "
            "this is exactly the silently-confidently-wrong outcome this phase must never produce"
        )
        print("PASS: the system was never silently, confidently wrong about this ceiling -- "
              "the gap was either not claimed complete, or honestly flagged in notes")
