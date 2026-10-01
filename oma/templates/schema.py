"""Template shape for the Phase 20 template library (build plan §24.5,
extended by §24.12.4's Component 13 hierarchy fields). A template is
not free text -- every field here is either something the classifier
narrows on, something the execution path fills in verbatim, or
something the harvest/maintenance jobs need to decide promotion/retire.

This module is pure data + validation -- no DB access (that's
templates/store.py) and no harvesting logic (that's templates/harvest.py).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum


class TemplateLevel(int, Enum):
    """§24.12.1's four hierarchy levels. Level 0 is the smallest
    mechanically-checkable unit; Level 3 is a named group, assembled
    automatically from Level-2 clustering, never hand-declared.
    """
    ATOMIC_SKILL = 0
    PIPELINE = 1
    FEATURE_TEMPLATE = 2
    GROUP = 3


class TemplateStatus(str, Enum):
    """§24.7 / §24.11.5: a harvested template starts NEEDS_REVIEW, never
    live immediately. It only becomes ACTIVE (eligible for the
    classifier to match against, once Component 2/3 exist) via the
    margin-based auto-promote rule or an explicit human confirmation.
    RETIRED is Component 5 (merge/prune)'s terminal state for a
    template whose fallback_override_count stays high relative to
    success_count -- not implemented yet (§24.10 step 5), but the
    status value exists now so store.py's schema doesn't need a later
    migration just to add it.
    """
    NEEDS_REVIEW = "needs_review"
    ACTIVE = "active"
    RETIRED = "retired"


@dataclass(frozen=True)
class ApplicabilitySignature:
    """The features the two-stage classifier (§24.6) narrows on. Kept
    as a small, explicit set of fields rather than a free-form dict --
    every field here is something harvest.py can extract mechanically
    from a real task's own contract/outcome, not something that needs
    an LLM call to invent.
    """
    task_shape: str          # e.g. "add_field_to_model" -- the harvested
                              # goal's structural category, not its
                              # literal text
    odoo_module_area: str    # e.g. "res.partner", "hr.employee" -- the
                              # target model/module this template touches
    scope_class: str = "single_file"  # single_file / multi_file / migration,
                              # per §24.4 row 1's own definition

    def as_dict(self) -> dict:
        return {
            "task_shape": self.task_shape,
            "odoo_module_area": self.odoo_module_area,
            "scope_class": self.scope_class,
        }


@dataclass(frozen=True)
class ConstraintSlot:
    """One named, fillable slot in a constraint_template -- e.g.
    {"name": "field_name", "example_value": "badge_expiry_date"}. Real
    tasks fill these in before the slot-filled constraint list is
    handed to contracts/schema.py's TaskContract.inputs, per §24.3's
    wiring point.
    """
    name: str
    example_value: str


@dataclass(frozen=True)
class CodeExample:
    """One real, verified working code snippet -- added 2026-07-15 after
    inspecting the first real harvested batch and finding two concrete
    gaps (see build plan §24.14.7 for the full research/critique/decision
    trail): a template previously stored only abstract constraint labels,
    never actual code a future agent could start from, and kept only a
    single example parameter set even when multiple distinct,
    independently-verified variations existed (e.g. Char, Boolean, Date,
    Many2one field additions all separately verified for the same
    model). One CodeExample per distinct variation actually seen and
    verified -- not invented, not templated from a guess -- pulled
    directly from the real source task's own last validated Gitea
    commit (templates/harvest.py's job, via
    tools_odoo/module_dev/vcs.py's existing read_last_validated_commit()).
    Deliberately plain fields in the same Postgres row as everything
    else (JSONB), not a separate git-backed content store -- that
    alternative was researched and explicitly rejected, see §24.14.7.
    """
    field_name: str
    field_type: str
    source_task_id: str
    models_py: str
    views_xml: str | None = None


@dataclass
class Template:
    template_id: str          # uuid4 hex, assigned at harvest/creation time
    version: int
    created_from: str         # source task_id, or "hand_authored"
    level: TemplateLevel
    applicability_signature: ApplicabilitySignature
    constraint_template: list[str]     # parameterized constraint labels,
                                        # e.g. "add_it_{model_name}"
    slots: list[ConstraintSlot] = field(default_factory=list)
    known_pitfalls: list[str] = field(default_factory=list)
    code_examples: list[CodeExample] = field(default_factory=list)
    composed_from: list[str] = field(default_factory=list)  # Level>=1 only:
                                        # ordered list of child template_ids
    status: TemplateStatus = TemplateStatus.NEEDS_REVIEW
    success_count: int = 0
    fallback_override_count: int = 0
    last_used_at: datetime | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self):
        if self.level != TemplateLevel.ATOMIC_SKILL and not self.composed_from:
            raise ValueError(
                f"template {self.template_id!r} is level {self.level.name} "
                "but has no composed_from -- only Level 0 (atomic skill) "
                "templates may be built from scratch rather than assembled "
                "from already-verified child templates, per §24.12.2"
            )
        if self.level == TemplateLevel.ATOMIC_SKILL and self.composed_from:
            raise ValueError(
                f"template {self.template_id!r} is Level 0 (atomic skill) "
                "but has composed_from set -- Level 0 templates are the "
                "base case, they cannot themselves be composed of other "
                "templates"
            )

    @property
    def override_rate(self) -> float:
        """§24.7's retire signal: a template that keeps getting
        overridden despite matching is actively harmful to keep
        serving. Returns 0.0 for a never-used template (nothing to
        judge yet), not a divide-by-zero.
        """
        total = self.success_count + self.fallback_override_count
        if total == 0:
            return 0.0
        return self.fallback_override_count / total
