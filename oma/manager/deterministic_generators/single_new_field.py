"""Phase 35 §15.3 -- the pilot deterministic generator, for the
`single_new_field` direction only.

Renders the field-declaration edit deterministically from typed
parameters instead of asking an LLM to author it -- the same fix
already proven in this codebase for __manifest__.py (see
specialists/build/specialist.py's ManifestFields/_render_manifest_py(),
§15.2's real precedent), extended here to the single most common
real request across every source cited in this roadmap: adding one
field to an existing model.

Schema is deliberately narrower than the full space of Odoo fields --
only the evidence-backed branch (per §14.2's own branch-coverage
discipline): the 13 real single_new_field passes recorded in
state/scope_certification.json as of 2026-08-13 are all Char/Boolean/
Integer/Date/Text/Selection, none group-restricted, none relational.
Anything outside that (a Many2one, a group-restricted field, an
unrecognized module/model shape) is NOT rendered here -- see
StructuralPreconditionError / UnsupportedFieldTypeError below, both of
which signal "fall back to §15.5 path 2/3", never a best-effort guess.

Nothing in this module is wired into the live task pipeline yet.
Building and testing the generator is step one of §15.13's build
order; the shadow-mode validation period (§15.8) and the live gate in
manager/loop.py are separate, later, not-yet-taken steps.
"""

from __future__ import annotations

import ast
import re
from typing import Literal

from pydantic import BaseModel, Field, model_validator

# The evidence-backed field-type set, per §14.2's branch-coverage discipline --
# exactly the types the real 13/13 single_new_field passes actually exercised.
# Expanding this set is a real code change with a real diff and a real review
# (§15.7), never a runtime flag flip, and only once a new type has real,
# certified evidence behind it.
_EVIDENCE_BACKED_FIELD_TYPES = ("Char", "Boolean", "Integer", "Date", "Text", "Selection")

_VALID_PYTHON_IDENTIFIER_RE = re.compile(r"^[a-z][a-z0-9_]*$")


class SingleFieldAdditionParams(BaseModel):
    """§15.3's contract, as a real, validated schema -- not prose."""

    module_name: str
    model_name: str
    field_name: str
    field_type: Literal["Char", "Boolean", "Integer", "Date", "Text", "Selection"]
    default: str | bool | int | None = None
    help_text: str | None = None
    selection_options: list[tuple[str, str]] | None = None
    # None = today's evidence-backed branch. Any non-None value is, by
    # construction, outside the schema this generator is trusted for --
    # see is_in_evidence_backed_space() below, which is the single real
    # gate every caller must check before calling render(), not this
    # validator (field-shape validity is not the same claim as
    # "evidence-backed," per §14.2's own distinction).
    security_group: str | None = None

    @model_validator(mode="after")
    def _field_name_is_a_valid_python_identifier(self) -> "SingleFieldAdditionParams":
        if not _VALID_PYTHON_IDENTIFIER_RE.match(self.field_name):
            raise ValueError(
                f"field_name {self.field_name!r} is not a valid, lowercase Python "
                "identifier -- refuse to render rather than guess."
            )
        return self

    @model_validator(mode="after")
    def _selection_options_required_iff_selection_type(self) -> "SingleFieldAdditionParams":
        if self.field_type == "Selection" and not self.selection_options:
            raise ValueError("field_type='Selection' requires non-empty selection_options.")
        if self.field_type != "Selection" and self.selection_options:
            raise ValueError(f"selection_options given but field_type is {self.field_type!r}, not 'Selection'.")
        return self

    def is_in_evidence_backed_space(self) -> bool:
        """§15.5 step 1's real gate condition, as a checkable function --
        not every schema-VALID combination is evidence-backed. A
        group-restricted field is schema-valid (the field exists, has a
        real type) but is NOT in the 13 real passes' evidence, so it is
        NOT in this generator's trusted space yet, per §14.2's
        branch-coverage discipline. Callers must check this before
        calling render() -- render() itself does not re-check it, so it
        stays a pure function of its own inputs, not a policy engine.
        """
        return self.field_type in _EVIDENCE_BACKED_FIELD_TYPES and self.security_group is None


class UnsupportedFieldTypeError(ValueError):
    """Raised when asked to render outside the evidence-backed space.
    Signals "fall back to §15.5 path 2/3" -- never caught and
    guessed past."""


class StructuralPreconditionError(ValueError):
    """Raised when the target module's real models.py doesn't match a
    structural shape this generator's evidence base has actually seen
    (§15.3's structural-drift precondition) -- e.g. no class found for
    the target model, or more than one class claims the same model.
    Signals "fall back to §15.5 path 2/3", never a best-effort guess
    at which class was meant.
    """


_FIELD_TYPE_PARAMS: dict[str, tuple[str, ...]] = {
    "Char": ("string",),
    "Boolean": ("string",),
    "Integer": ("string",),
    "Date": ("string",),
    "Text": ("string",),
    "Selection": ("selection", "string"),
}


def _render_field_declaration(params: SingleFieldAdditionParams) -> str:
    """Pure, deterministic: same params always produce the same source
    line-for-line. No LLM call anywhere in this function."""
    if not params.is_in_evidence_backed_space():
        raise UnsupportedFieldTypeError(
            f"{params.field_type!r} (security_group={params.security_group!r}) is outside "
            "this generator's evidence-backed space -- route to LLM generation instead."
        )

    kwargs: list[str] = []
    # string= is always included; it is Odoo's own convention and every
    # real single_new_field pass this generator's evidence is drawn from
    # includes one.
    label = params.field_name.replace("_", " ").strip().capitalize()
    kwargs.append(f"string={label!r}")
    if params.help_text:
        kwargs.append(f"help={params.help_text!r}")
    if params.default is not None:
        kwargs.append(f"default={params.default!r}")

    if params.field_type == "Selection":
        options_repr = ", ".join(f"({k!r}, {v!r})" for k, v in params.selection_options or [])
        args = f"[{options_repr}], " + ", ".join(kwargs)
    else:
        args = ", ".join(kwargs)

    return f"    {params.field_name} = fields.{params.field_type}({args})\n"


def _find_target_class(models_py: str, model_name: str) -> ast.ClassDef:
    """§15.3's structural-drift precondition, made concrete: locate the
    single class declaring or reopening `model_name`, via a real AST
    parse (the same class of parsing §13's knowledge-graph parser
    already uses, not a regex guess). Exactly one match is required --
    zero or more than one both signal a structural shape this
    generator's evidence base hasn't seen.
    """
    try:
        tree = ast.parse(models_py)
    except SyntaxError as exc:
        raise StructuralPreconditionError(f"models.py does not parse as valid Python: {exc}") from exc

    matches: list[ast.ClassDef] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        for stmt in node.body:
            if not isinstance(stmt, ast.Assign):
                continue
            targets = [t.id for t in stmt.targets if isinstance(t, ast.Name)]
            if ("_name" in targets or "_inherit" in targets) and isinstance(stmt.value, ast.Constant):
                if stmt.value.value == model_name:
                    matches.append(node)
                    break
            # _inherit = ['a', 'b'] (list form) -- real mixin shape §13.2's
            # own EXTENDS-union fix already had to account for.
            if "_inherit" in targets and isinstance(stmt.value, (ast.List, ast.Tuple)):
                values = [e.value for e in stmt.value.elts if isinstance(e, ast.Constant)]
                if model_name in values:
                    matches.append(node)
                    break

    if len(matches) != 1:
        raise StructuralPreconditionError(
            f"Expected exactly one class declaring/reopening {model_name!r}, found {len(matches)} -- "
            "structural shape not covered by this generator's evidence base."
        )
    return matches[0]


def render_single_field_addition(
    params: SingleFieldAdditionParams, current_models_py: str
) -> dict:
    """§15.3's renderer. Returns a dict matching
    specialists/build/specialist.py's real GeneratedModuleEdit shape
    (file, operation="search_replace", target, content) -- deliberately
    a plain dict here, not importing the Pydantic model directly, to
    keep this module's only dependency on the Build specialist being a
    documented CONTRACT (the object shape), not a live import coupling
    two packages that should stay independently testable. The caller
    (manager/loop.py, once §15's live gate is built -- not yet) is
    responsible for constructing the real GeneratedModuleEdit from this.

    Raises UnsupportedFieldTypeError / StructuralPreconditionError
    rather than ever guessing -- both signal "fall back to §15.5 path
    2/3", per this module's own docstring.
    """
    if not params.is_in_evidence_backed_space():
        raise UnsupportedFieldTypeError(
            f"{params.field_type!r} (security_group={params.security_group!r}) is outside "
            "this generator's evidence-backed space -- route to LLM generation instead."
        )

    target_class = _find_target_class(current_models_py, params.model_name)

    # Anchor: insert immediately after the class's own header line block
    # (the _name/_inherit assignment(s) and any _description/_inherit
    # chain right after it) -- the same "insert after an anchor,
    # everything else survives untouched" discipline
    # GeneratedModuleEdit.search_replace's own docstring requires.
    lines = current_models_py.splitlines(keepends=True)
    class_line_no = target_class.lineno - 1  # 0-indexed
    anchor_end_line = class_line_no
    for stmt in target_class.body:
        if isinstance(stmt, ast.Assign):
            targets = [t.id for t in stmt.targets if isinstance(t, ast.Name)]
            if any(t in ("_name", "_inherit", "_description") for t in targets):
                anchor_end_line = max(anchor_end_line, stmt.end_lineno or stmt.lineno)
                continue
        break  # first non-header statement -- stop extending the anchor

    anchor_text = "".join(lines[class_line_no:anchor_end_line + 1])
    field_line = _render_field_declaration(params)

    return {
        "file": "models/models.py",
        "operation": "search_replace",
        "target": anchor_text,
        "content": anchor_text + field_line,
    }
