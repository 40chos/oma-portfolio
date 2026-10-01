"""Phase 26B (2026-07-27): the ONE authoritative source for "which real
Odoo model is this task touching" -- the same shared-extraction pattern
`contracts/goal_facts.py` already established for the same reason
(import-linter forbids `manager/` importing anything under `specialists/`
directly, but both already import freely from `contracts/` and `infra/`).

Real, confirmed bug this replaces (audit Finding #2): `manager/loop.py`
derived a fencing-lock key from `anticipated_scope.get("models")` -- a
value the real chat UI never actually populates in production, so the
lock key silently collapsed to `f"task:{task_id}"`, unique to the
CURRENT task by construction, defeating the whole point of the lock
(two different tasks editing the SAME real Odoo model concurrently could
never collide). Separately, `manager/replanning.py`'s own
`_module_identifier()` computed a completely independent 4-word regex
slug of goal PROSE (e.g. "Add a special instructions field..." ->
"add_a_special_instructions") -- a value that can never exact-string-
match the real outcome rows `check_repeated_failures` actually queries
against (stored keyed by the real Odoo model name, or None), silently
making that escalation path an unreachable no-op on every real task.

Every real task goal in this project follows one consistent, already-
established convention -- a `Model: <name>` metadata line (confirmed
across every real task this whole Phase 25/26 investigation has ever
run, e.g. "Model: project.meerwerk" or "Model: res.partner (inherit)")
-- making this a cheap, deterministic, reliable resolution, never a
guess dressed up as one.
"""

from __future__ import annotations

import re

_GOAL_MODEL_LINE_RE = re.compile(
    r"^\s*Model:\s*`?([\w.]+?)`?\s*(?:\(.*\))?\s*$", re.MULTILINE | re.IGNORECASE,
)

# P12 Tier A item 17 (docs/planning/PHASE30_ARCH_SYNTHESIS_CRITIQUE_FINAL_2026-07-30.md §4):
# real, confirmed gap -- this project's own plan text names its flagship example live:
# `resolve_module_identity('Modify the ir.model.access.csv permissions for account.move.')`
# returns None, because that goal (a genuine, real prose-only request, no structured `Model:`
# metadata line at all) has nothing the original regex can match. Confirmed rejected by Operator
# 7+ separate times for exactly this reason. Fallback: a dotted, lowercase identifier matching
# Odoo's own real model-naming convention (e.g. "account.move", "project.meerwerk"), found
# ANYWHERE in the goal prose when no structured `Model:` line exists. Deliberately excludes
# tokens ending in a common file extension (.csv/.py/.xml/etc.) -- the flagship example itself
# contains "ir.model.access.csv" immediately before the real target "account.move", and a
# naive dotted-identifier match would pick the wrong one first.
_FILE_EXTENSION_SUFFIXES = {
    "csv", "py", "xml", "json", "txt", "md", "yml", "yaml", "js", "ts", "html", "css", "po", "pot",
}
_PROSE_MODEL_NAME_RE = re.compile(r"\b[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+\b")


def _resolve_from_prose(goal: str) -> str | None:
    for match in _PROSE_MODEL_NAME_RE.finditer(goal or ""):
        candidate = match.group(0)
        if candidate.rsplit(".", 1)[-1] not in _FILE_EXTENSION_SUFFIXES:
            return candidate
    return None


def resolve_module_identity(goal: str, hint: str | None = None) -> str | None:
    """Returns the real Odoo model name this task's own goal names, or
    None if it can't be determined at all.

    `hint`, when given (e.g. a caller-supplied `anticipated_scope["models"][0]`),
    is an explicit override that always wins over the automatic goal-text
    derivation -- matching this project's own already-established
    "explicit caller intent beats an automatic guess, even a verified
    one" precedent (`manager/loop.py`'s own `detected_existing_module_
    dependency` handling, right next to this call site).

    Deliberately the ONLY place this derivation happens -- every caller
    (fencing-lock key, `check_repeated_failures`, the outcome row's own
    `module=` column, `should_escalate_to_operator()`) must call this
    function (or read the cached `TaskContract.module_identity` it
    populates once), never re-derive its own independent guess. That is
    the entire fix this phase exists to make: ONE authoritative source,
    not two independently-drifting ones.
    """
    if hint:
        return hint
    match = _GOAL_MODEL_LINE_RE.search(goal or "")
    if match:
        return match.group(1)
    return _resolve_from_prose(goal)
