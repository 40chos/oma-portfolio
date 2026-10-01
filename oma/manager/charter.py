"""Loads MANAGER_CONSTITUTION.md and sensitive_paths.yaml, and implements
check_sensitive_paths() -- Phase 4 of the build plan, steps 1-3.

check_sensitive_paths() is, per the build plan's own words, "the single
most important piece of code in the whole Manager" -- every escalation
guarantee in both the vision and technical documents rests on this
staying a plain, deterministic script and never becoming "ask the model
if this feels risky." No LLM call happens anywhere in this file.

sensitive_paths.yaml is read fresh on every call, not cached at import
time -- it's meant to grow over time as more of Odoo's model surface
gets encountered, and a builder editing it shouldn't have to also
restart the process for the edit to take effect.
"""

from __future__ import annotations

import fnmatch
from pathlib import Path

import yaml

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONSTITUTION_PATH = _PROJECT_ROOT / "MANAGER_CONSTITUTION.md"
SENSITIVE_PATHS_PATH = _PROJECT_ROOT / "sensitive_paths.yaml"

TIER_READONLY = 1
TIER_NOTIFY_AFTER = 2
TIER_PAUSE_BEFORE = 3
TIER_PRESIGN_OFF = 4


def load_manager_constitution() -> str:
    """Read the Manager's own charter in full, every time -- never
    summarized or cached, per the technical document's §3 instruction
    that the Manager's system prompt loads this in full every turn.
    """
    return CONSTITUTION_PATH.read_text()


def load_sensitive_paths() -> list[dict]:
    """Read and parse sensitive_paths.yaml fresh on every call. A
    living file that gets edited over the life of the project, not
    compiled in once at import time.
    """
    data = yaml.safe_load(SENSITIVE_PATHS_PATH.read_text())
    return data.get("sensitive_paths", []) if data else []


def check_sensitive_paths(
    models: list[str] | None = None,
    fields: list[str] | None = None,
    concerns: list[str] | None = None,
    files: list[str] | None = None,
    is_write: bool = False,
    touches_schema_or_permissions: bool = False,
) -> int:
    """The mechanical, deterministic tier check. Nothing fuzzy, nothing
    model-based -- a plain set-intersection / glob-pattern check against
    sensitive_paths.yaml.

    Per MANAGER_CONSTITUTION.md: if `touches_schema_or_permissions` is
    true, the tier is 4 regardless of anything else. Otherwise, if the
    anticipated scope matches any sensitive_paths.yaml entry, the tier
    is whatever that entry specifies (defaulting to 3 if unspecified).
    Otherwise the tier is 2 if this is a write, 1 if it's read-only.
    """
    models = models or []
    fields = fields or []
    concerns = concerns or []
    files = files or []

    if touches_schema_or_permissions:
        return TIER_PRESIGN_OFF

    rules = load_sensitive_paths()
    matched_tier: int | None = None

    for rule in rules:
        hit = False
        if "model" in rule and rule["model"] in models:
            hit = True
        if "field_pattern" in rule and any(
            fnmatch.fnmatch(f, rule["field_pattern"]) for f in fields
        ):
            hit = True
        if "concern" in rule and rule["concern"] in concerns:
            hit = True
        if "file" in rule and rule["file"] in files:
            hit = True
        if hit:
            rule_tier = rule.get("tier", TIER_PAUSE_BEFORE)
            if matched_tier is None or rule_tier > matched_tier:
                matched_tier = rule_tier

    if matched_tier is not None:
        return matched_tier

    return TIER_NOTIFY_AFTER if is_write else TIER_READONLY
