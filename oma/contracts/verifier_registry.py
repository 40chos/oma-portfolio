"""Phase 25E (2026-07-26) -- verifier systematization.

Two problems this pass's own history kept re-creating, per the Phase 25
plan doc's own §Phase 25E:

1. Every Code-Review/Testing-QA false-positive ("hallucination") this
   project has ever hit was found the same way: a live task blocked on
   a bogus claim, someone read the real generated code by hand, wrote
   one bespoke regex filter, and moved on -- 9 of them exist now
   (`specialists/code_review/specialist.py`) plus 3 coverage-gap
   exemptions (`specialists/testing_qa/specialist.py`), each discovered
   independently, none tracked anywhere as a set. `HALLUCINATION_FILTER_
   REGISTRY` below is that missing single table -- and
   `tests/test_verifier_registry_completeness.py` fails the moment a
   new filter function is added to either file without a matching
   entry here, so the NEXT one is a one-line registration, not another
   silent gap.

2. Fix 40 (button `groups=` restriction, task 008, 2026-07-25) was a
   goal-target shape -- "restrict this button to a security group" --
   that neither the field-existence check nor any hallucination filter
   could verify at all: the reproduction check only knew how to look
   for ORM fields, so a button name could never satisfy it. That was
   found the same way as every hallucination filter above: a live
   task, five losing rounds, then a human reading the real view XML by
   hand. `REPRODUCTION_SHAPE_REGISTRY` generalizes this: every shape a
   goal is known to target, whether or not it currently has a real
   ground-truth verification path. `unverified_shapes_targeted()` lets
   a caller flag -- non-blocking, informational only -- when a task's
   own goal appears to target a shape with no registered verification,
   so the gap shows up as a flag on that task's own trace instead of
   only being discovered after several losing rounds.

Deliberately NOT a runtime dispatch mechanism -- the real filter/
verification functions stay exactly where they are, called exactly as
they already are, in `code_review/specialist.py`, `testing_qa/
specialist.py`, `manager/replanning.py`. This module is a catalog of
what already exists (or explicitly does not), read by tests for
completeness and by the classifier below for gap-flagging. It does not
own or alter any pass/fail decision.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from contracts.schema import TaskContract

# ---------------------------------------------------------------------
# 1. Hallucination-filter / coverage-exemption registry
# ---------------------------------------------------------------------


@dataclass(frozen=True)
class HallucinationFilterEntry:
    filter_id: str
    module: str                # dotted module path
    function_name: str
    claim_shape: str           # what false-positive claim this catches
    ground_truth_check: str    # what real state it reads before downgrading
    added: str                 # "2026-07-25, task 003" etc.


HALLUCINATION_FILTER_REGISTRY: list[HallucinationFilterEntry] = [
    HallucinationFilterEntry(
        "scope_findings",
        "specialists.code_review.specialist",
        "_filter_hallucinated_scope_findings",
        "invents a scope/round constraint not actually stated in the goal",
        "regex against the goal's own genuine-scope-language markers (no external I/O)",
        "2026-07-24",
    ),
    HallucinationFilterEntry(
        "method_missing",
        "specialists.code_review.specialist",
        "_filter_hallucinated_method_missing_findings",
        "claims a super().method() target is not defined on the base model",
        "check_real_module_defines_method() against the real owner module's source",
        "2026-07-24",
    ),
    HallucinationFilterEntry(
        "xmlid_missing",
        "specialists.code_review.specialist",
        "_filter_hallucinated_xmlid_missing_findings",
        "claims a ref=\"module.xmlid\" might not exist",
        "resolve_xmlids_exist_fast() -- direct ir.model.data lookup",
        "2026-07-24",
    ),
    HallucinationFilterEntry(
        "direct_field_missing",
        "specialists.code_review.specialist",
        "_filter_hallucinated_direct_field_missing_findings",
        "claims object.field / <field name=X> / currency_field=X is not defined on the base model",
        "get_model_fields_fast() -- real fields_get()-equivalent RPC",
        "2026-07-24, widened 2026-07-26 for currency_field kwarg",
    ),
    HallucinationFilterEntry(
        "empty_security_csv",
        "specialists.code_review.specialist",
        "_filter_hallucinated_empty_security_csv_findings",
        "claims a header-only ir.model.access.csv (no new model in this diff) is a bug",
        "regex: no new _name= in models.py AND real CSV genuinely has zero data rows",
        "2026-07-24",
    ),
    HallucinationFilterEntry(
        "sequence_pattern",
        "specialists.code_review.specialist",
        "_filter_hallucinated_sequence_pattern_findings",
        "claims the standard name/default='New'/next_by_code/=='New' idiom is a bug",
        "regex match of the exact known-correct idiom against real models.py",
        "2026-07-25",
    ),
    HallucinationFilterEntry(
        "field_not_instantiated",
        "specialists.code_review.specialist",
        "_filter_hallucinated_field_not_instantiated_findings",
        "claims fields.Text is a class, not an instance (i.e. missing parens)",
        "regex confirms the real = fields.Text(...) call exists in models.py",
        "2026-07-25, widened live for 2 rewordings",
    ),
    HallucinationFilterEntry(
        "module_registration",
        "specialists.code_review.specialist",
        "_filter_hallucinated_module_registration_findings",
        "claims __init__.py doesn't import models correctly / module not installed",
        "regex confirms the real top __init__.py and models/__init__.py both import models",
        "2026-07-26",
    ),
    HallucinationFilterEntry(
        "incomplete_compute_dependency",
        "specialists.code_review.specialist",
        "_filter_hallucinated_incomplete_compute_dependency_findings",
        "invents an extra required compute dependency beyond what the goal actually stated",
        "structural: finding mentions the real field/subfield/relation, verified against real @api.depends + field decl",
        "2026-07-26, rewritten from keyword to structural matching after 6 live rewordings",
    ),
    HallucinationFilterEntry(
        "sequence_idiom_coverage_gap",
        "specialists.testing_qa.specialist",
        "_exempt_verified_sequence_idiom_from_coverage_gap",
        "flags a create() override's body lines as uncovered (install-time coverage never calls create())",
        "regex confirms the real models.py matches the known sequence idiom before exempting",
        "2026-07-25",
    ),
    HallucinationFilterEntry(
        "sum_compute_coverage_gap",
        "specialists.testing_qa.specialist",
        "_exempt_verified_sum_compute_idiom_from_coverage_gap",
        "flags a sum-aggregate compute method's body lines as uncovered",
        "goal states exact field/method/dependency AND real models.py has the matching @api.depends + method",
        "2026-07-26",
    ),
    HallucinationFilterEntry(
        "onchange_coverage_gap",
        "specialists.testing_qa.specialist",
        "_exempt_verified_onchange_idiom_from_coverage_gap",
        "flags an @api.onchange method's body lines as uncovered",
        "goal states method/trigger field AND real models.py has the matching @api.onchange decorator",
        "2026-07-26",
    ),
    HallucinationFilterEntry(
        "own_module_collision",
        "specialists.code_review.specialist",
        "_filter_hallucinated_own_module_collision_findings",
        "claims a new model name collides with an existing model, when the 'existing' model is actually this same task's own module from an earlier round",
        "resolve_model_owner_module_fast / is_fast_path_eligible -- confirms the named model's real owner module is this round's own module",
        "2026-07-28",
    ),
    HallucinationFilterEntry(
        "out_of_scope_field_required",
        "specialists.code_review.specialist",
        "_filter_hallucinated_out_of_scope_field_required_findings",
        "claims a field is missing/required when that field's own constraint is explicitly NOT yet in scope for this round",
        "regex: goal's own not-yet-in-scope marker genuinely names the same field the finding claims is missing",
        "2026-07-28",
    ),
    HallucinationFilterEntry(
        "stale_scope_exclusion",
        "specialists.code_review.specialist",
        "_filter_hallucinated_stale_scope_exclusion_findings",
        "cites a constraint label as 'not in scope' that isn't actually in this round's real current not-yet-in-scope list",
        "regex: cross-checks the finding's cited label against the goal's own live not-yet-in-scope list",
        "2026-07-28",
    ),
    HallucinationFilterEntry(
        "bare_group_id_prefix_mismatch",
        "specialists.code_review.specialist",
        "_filter_hallucinated_bare_group_id_prefix_mismatch_findings",
        "claims a bare <record id=\"X\" model=\"res.groups\"> \"mismatches\" a module-prefixed CSV group_id:id reference to it -- standard, correct Odoo xmlid resolution, never a real mismatch",
        "regex: the bare group id genuinely exists as a <record model=\"res.groups\"> in this generation's own security_xml",
        "2026-07-29",
    ),
    HallucinationFilterEntry(
        "goal_self_contradiction",
        "specialists.code_review.specialist",
        "_filter_hallucinated_goal_self_contradiction_findings",
        "claims the goal is self-contradictory/irreconcilable because it both names a piece in its overall description and lists it as NOT-yet-in-scope for this round -- the round's own goal_text already explains this is the multi-round plan's wording, not a real contradiction",
        "regex: >=2 quoted names in the finding resolve (via substring or significant-token overlap) to this round's own real not-yet-in-scope list",
        "2026-08-07",
    ),
    HallucinationFilterEntry(
        "collision_satisfied_field_missing",
        "specialists.code_review.specialist",
        "_filter_hallucinated_collision_satisfied_field_missing_findings",
        "claims a field is missing/unimplemented when Build's own collision-autofix correctly stripped it because it already exists as a real, live, functioning field on the actual target model",
        "regex: parses the 'IMPORTANT: [...] already exist as REAL, LIVE, FUNCTIONING' marker manager/tools.py appends to goal text, cross-checks the finding's quoted name(s) against it via substring or significant-token overlap",
        "2026-08-07",
    ),
]


def registered_filter_ids() -> set[str]:
    return {entry.filter_id for entry in HALLUCINATION_FILTER_REGISTRY}


# ---------------------------------------------------------------------
# 2. Reproduction-shape registry (generalizes fix 40's own finding)
# ---------------------------------------------------------------------

ShapeVerification = Literal["verified", "unverified"]


@dataclass(frozen=True)
class ReproductionShapeEntry:
    shape_id: str
    description: str
    status: ShapeVerification
    verification_function: str | None   # None when status == "unverified"
    goal_signal_re: str                 # regex used by the classifier below to detect this shape in a goal


REPRODUCTION_SHAPE_REGISTRY: list[ReproductionShapeEntry] = [
    ReproductionShapeEntry(
        "orm_field_existence",
        "a new/changed ORM field on a model",
        "verified",
        "check_field_exists_on_model_fast / get_model_fields_fast",
        r"\bField:\s*\w+",
    ),
    ReproductionShapeEntry(
        "field_group_restriction",
        "an ORM field restricted to a security group (groups= kwarg)",
        "verified",
        "check_field_group_restricted_fast",
        r"\bfield\b.{0,40}\brestrict(ed)?\b.{0,40}\bgroup\b",
    ),
    ReproductionShapeEntry(
        "button_group_restriction",
        "a view button restricted to a security group (groups= XML attribute)",
        "verified",
        "check_button_group_restricted_fast",
        r"\bbutton\b.{0,40}\b(restrict|group|visible only to)\b",
    ),
    ReproductionShapeEntry(
        "security_group_existence",
        "a new security group / access right",
        "verified",
        "check_group_exists_fast / check_group_model_access_fast",
        r"\bsecurity group\b|\bres\.groups\b|\bir\.model\.access\b",
    ),
    ReproductionShapeEntry(
        "xmlid_reference",
        "a ref=\"module.xmlid\" reference that must resolve to a real record",
        "verified",
        "resolve_xmlids_exist_fast",
        r"\bxml ?id\b|\bref=[\"']",
    ),
    ReproductionShapeEntry(
        "method_override",
        "a super().method() override claiming a base-model method exists",
        "verified",
        "check_real_module_defines_method",
        r"\boverride\b.{0,30}\bmethod\b|\bsuper\(\)\.",
    ),
    ReproductionShapeEntry(
        "sequence_assigned_name",
        "an ir.sequence-driven auto-generated reference/name field",
        "verified",
        "pattern match against the known sequence idiom (models.py) + real ir.sequence.next_by_code",
        r"\bsequence\b.{0,30}\b(number|reference|name)\b|\bauto[- ]generat",
    ),
    ReproductionShapeEntry(
        "computed_field",
        "a computed field's numeric/logical correctness beyond its dependency path",
        "unverified",
        None,
        r"\bcompute(d)?\b.{0,20}\bfield\b|@api\.depends",
    ),
    ReproductionShapeEntry(
        "onchange_behavior",
        "an @api.onchange handler's actual runtime behavior",
        "unverified",
        None,
        r"\bonchange\b|\bwhen i select\b|\bauto[- ]fill",
    ),
    ReproductionShapeEntry(
        "selection_options",
        "the real set of Selection field option values",
        "unverified",
        None,
        r"\bselection\b.{0,20}\bfield\b|\bdropdown\b",
    ),
    ReproductionShapeEntry(
        "search_view_filter",
        "a search-view filter's actual domain/behavior",
        "unverified",
        None,
        r"\bfilter\b.{0,20}\b(search|domain)\b|\bView:\s*search\b",
    ),
    ReproductionShapeEntry(
        "ir_cron_schedule",
        "a scheduled action's (ir.cron) content or interval",
        "unverified",
        None,
        r"\bir\.cron\b|\bscheduled action\b|\bcron job\b",
    ),
    ReproductionShapeEntry(
        "menu_action_placement",
        "a menu item / window action's real placement or target",
        "verified",
        "check_menu_exists",
        r"\bmenu item\b|\bir\.actions\.act_window\b|\bnew menu\b|\bmenu structure\b",
    ),
    ReproductionShapeEntry(
        "smart_button_counter",
        "a smart button's real record count / target action",
        "unverified",
        None,
        r"\bsmart button\b",
    ),
]


def known_shape_ids() -> set[str]:
    return {entry.shape_id for entry in REPRODUCTION_SHAPE_REGISTRY}


def classify_targeted_shapes(contract: TaskContract) -> list[str]:
    """Heuristic, deliberately conservative: only used to surface an
    informational gap flag, never to gate pass/fail (that stays exactly
    where it already is -- the real reproduction-target/security-claim
    extraction in testing_qa/specialist.py). Matches the goal text
    against each registered shape's own signal regex.
    """
    goal = contract.goal or ""
    matched: list[str] = []
    for entry in REPRODUCTION_SHAPE_REGISTRY:
        if re.search(entry.goal_signal_re, goal, re.IGNORECASE):
            matched.append(entry.shape_id)
    return matched


def unverified_shapes_targeted(contract: TaskContract) -> list[str]:
    """Shapes this goal appears to target that have no registered
    ground-truth verification path. Non-blocking -- callers attach this
    to SpecialistOutput.detail as an informational flag only.
    """
    unverified_ids = {
        entry.shape_id for entry in REPRODUCTION_SHAPE_REGISTRY if entry.status == "unverified"
    }
    return [shape_id for shape_id in classify_targeted_shapes(contract) if shape_id in unverified_ids]
