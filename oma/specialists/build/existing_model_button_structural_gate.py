"""Phase 33 implementation (2026-08-11): the pre-Code-Review structural gate from
docs/planning/PHASE33_CLOSING_THE_GAP_TO_DAY_TO_DAY_WORK_2026-08-11.md §3 item 3 --
a real, scoped fix for two of the concrete defect shapes found in 4 real
`gates_disagree` samples (rounds where Testing/QA's reproduction check passed while
Code-Review independently found a genuine structural defect), for the 192-task
"custom button/method on an existing model" hard core (10% pass, ≈84% of the
workflow/buttons bucket's real volume).

Approved by the CEO-vision review as "a scoped experiment against these two named
defect shapes, not a general claim to have solved buttons" -- it is inferred from
only 4 real samples and may need broadening once more are read. Follows this
codebase's own `_validate_*(generated, ...) -> None` convention (raises ValueError
on a real defect; returns normally when clean) so it plugs into the existing
validator chain in specialists/build/specialist.py the same way every sibling
validator does.
"""

from __future__ import annotations

import re

# Real phrasings observed directly in production goal text signaling "this model
# does not exist yet, build it fresh" -- e.g. "There is no existing container
# concept or button in project_meerwerk to copy -- build this as a self-contained
# NEW model within this task's own scope."
_GOAL_SIGNALS_BRAND_NEW_MODEL_RE = re.compile(
    r"there is no existing .{0,60}? -- build this as a (?:self-contained )?new model"
    r"|build this as a self-contained new model"
    r"|brand new model within this task",
    re.IGNORECASE,
)

_CLASS_DECLARATION_RE = re.compile(
    r"class\s+(\w+)\s*\(models\.Model\):\s*\n((?:[ \t]+.*\n?)*)", re.MULTILINE,
)
_NAME_ATTR_RE = re.compile(r"^\s*_name\s*=\s*['\"]([\w.]+)['\"]", re.MULTILINE)
_INHERIT_ATTR_RE = re.compile(r"^\s*_inherit\s*=\s*['\"]([\w.]+)['\"]", re.MULTILINE)


def _model_name_from_goal_new_model_signal(goal: str) -> str | None:
    """Best-effort: the model name a 'build this as a new model' sentence is
    talking about is whatever dotted model-shaped identifier appears nearest to
    the signal phrase in the goal text -- deliberately conservative, only used to
    scope the check, never to invent a name that isn't actually in the goal."""
    match = _GOAL_SIGNALS_BRAND_NEW_MODEL_RE.search(goal)
    if not match:
        return None
    window = goal[max(0, match.start() - 200): match.end() + 50]
    model_match = re.search(r"\b([a-z][a-z0-9]*\.[a-z][a-z0-9_.]*)\b", window)
    return model_match.group(1) if model_match else None


def validate_new_model_signal_does_not_use_inherit_without_name(generated, goal: str) -> None:
    """Real, confirmed defect (read directly from a real gates_disagree sample,
    2026-08-11): the goal explicitly stated the target model does NOT exist yet
    and must be built fresh ("build this as a self-contained new model"), but
    Build declared a class using `_inherit` targeting that same model name
    without ALSO declaring `_name` -- inheriting from a model that was never
    going to exist, a guaranteed install-time crash Testing/QA's own reproduction
    check happened not to exercise, but Code-Review correctly caught.
    """
    signaled_model = _model_name_from_goal_new_model_signal(goal)
    if not signaled_model:
        return
    models_py = getattr(generated, "models_py", "") or ""
    for class_match in _CLASS_DECLARATION_RE.finditer(models_py):
        class_name, class_body = class_match.group(1), class_match.group(2)
        inherit_match = _INHERIT_ATTR_RE.search(class_body)
        name_match = _NAME_ATTR_RE.search(class_body)
        if (
            inherit_match
            and inherit_match.group(1) == signaled_model
            and not name_match
        ):
            raise ValueError(
                f"the goal explicitly states {signaled_model!r} does not exist yet and must be "
                f"built as a self-contained new model, but class {class_name!r} declares "
                f"`_inherit = {signaled_model!r}` with no `_name` -- this inherits from a model "
                f"that will never exist, a guaranteed install-time crash. Declare `_name = "
                f"{signaled_model!r}` to define it fresh, not `_inherit`."
            )


# Real phrasings signaling the goal enumerates several required pieces, each of
# which must genuinely be present -- generalizes the existing, narrower
# `_validate_goal_named_field_is_declared()` (which only fires on the strict
# "Field: <name> (<Type>)" convention) to also catch a field named only in prose,
# the exact shape a real gates_disagree sample showed: "Required pieces (both are
# needed -- do not omit either): ... project_count ...".
_ENUMERATED_REQUIRED_PIECES_RE = re.compile(
    r"required pieces.{0,40}?(?:both|all).{0,20}?(?:are )?needed.{0,40}?(?:do not omit|none may be omitted)",
    re.IGNORECASE,
)
_SNAKE_CASE_FIELD_LOOKING_IDENTIFIER_RE = re.compile(r"\b([a-z][a-z0-9]*(?:_[a-z0-9]+){1,4})\b")
# Deliberately excludes generic/structural words that are snake_case-shaped but
# never a real field name a goal would be naming as a "required piece."
_EXCLUDED_IDENTIFIER_WORDS = frozenset({
    "ir_model_access", "ir_model", "ir_rule", "res_partner", "res_users",
})


def validate_enumerated_required_pieces_are_all_present(generated, goal: str) -> None:
    """When the goal explicitly enumerates several required pieces and says none
    may be omitted, every snake_case identifier that LOOKS like a field name
    (mentioned in that same sentence's neighborhood) must genuinely appear
    somewhere in the generated models.py or views.xml -- never just the ones
    happening to also match the stricter `Field: <name>` convention.
    """
    match = _ENUMERATED_REQUIRED_PIECES_RE.search(goal)
    if not match:
        return
    window = goal[match.start(): min(len(goal), match.end() + 400)]
    candidates = {
        identifier for identifier in _SNAKE_CASE_FIELD_LOOKING_IDENTIFIER_RE.findall(window)
        if identifier not in _EXCLUDED_IDENTIFIER_WORDS
    }
    if not candidates:
        return
    haystack = (getattr(generated, "models_py", "") or "") + (getattr(generated, "views_xml", "") or "")
    missing = sorted(c for c in candidates if c not in haystack)
    if missing:
        raise ValueError(
            f"the goal explicitly enumerates required pieces that must ALL be present ('do not "
            f"omit either/any'), naming {sorted(candidates)!r}, but {missing!r} does not appear "
            f"anywhere in the generated models.py or views.xml -- a required piece was dropped."
        )
