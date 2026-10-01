"""Phase 34 execution (2026-08-11): deterministic construction for "quick filter" /
search-domain requests -- real, confirmed gap found live in three separate,
identically-worded Batch A survey failures: Build reliably NEVER emits the actual
`<filter>` XML element a goal asks for, even though an existing validator catches
the omission every single time (a hint-not-enforcement gap, the exact same class
already fixed tonight for ACL). Mirrors the proven
tools_odoo/schema_grounding.py / build_deterministic_view_xml() pattern: resolve
the REAL base view via the live registry, never guess, and construct the
insertion XML directly in Python when extraction is unambiguous -- fall back to
the existing LLM-generation path on any uncertainty.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_HAS_FILTER_WORD_RE = re.compile(r"\bfilter\b", re.IGNORECASE)

# Two real, independently-observed phrasing families, not one -- this session's own
# lesson that a single narrow pattern is always eventually dodged by a rephrasing:
# (a) "...filter ... called/named 'Label' ..." (label named explicitly relative to
#     the word "filter", arbitrary text in between);
# (b) "'Label' (field=value)" -- a bare quoted label immediately followed by an
#     explicit inline domain, used when the goal enumerates several filters at once
#     without repeating the word "filter" for each one.
_FILTER_LABEL_NAMED_RE = re.compile(
    r"['\"]([^'\"]+)['\"]\s*(?:\(([^)]+)\))?", re.IGNORECASE,
)

# Common named conditions observed in real goal text, used only when no explicit
# "(field=value)" domain is given inline -- never a guess beyond these well-
# understood, unambiguous Odoo idioms.
_NAMED_CONDITION_TO_DOMAIN = [
    (re.compile(r"assigned to (?:me|the current user)|\bmine\b", re.IGNORECASE), "user_id", "uid"),
    (re.compile(r"created by (?:me|the current user)", re.IGNORECASE), "create_uid", "uid"),
]

_EXPLICIT_DOMAIN_RE = re.compile(r"^\s*([\w.]+)\s*=\s*([\w.]+)\s*$")


@dataclass
class ExtractedQuickFilter:
    label: str
    field: str
    value: str  # either a literal ("accepted") or the special token "uid"


def extract_quick_filters(goal_text: str) -> list[ExtractedQuickFilter] | None:
    """Deterministic extraction only. Returns None if the goal doesn't mention
    "filter" at all, doesn't name any quick filter by label, or if ANY named
    filter's condition can't be resolved unambiguously (never partial-guesses
    some filters and drops others silently -- an all-or-nothing signal keeps
    the fallback-to-LLM contract clean)."""
    if not _HAS_FILTER_WORD_RE.search(goal_text):
        return None
    matches = list(_FILTER_LABEL_NAMED_RE.finditer(goal_text))
    if not matches:
        return None

    results = []
    for match in matches:
        label = match.group(1).strip()
        explicit = match.group(2)
        if explicit:
            domain_match = _EXPLICIT_DOMAIN_RE.match(explicit)
            if not domain_match:
                return None  # explicit domain given but not in a form we can trust -- bail entirely
            field, value = domain_match.group(1), domain_match.group(2)
            results.append(ExtractedQuickFilter(label=label, field=field, value=value))
            continue

        # No explicit domain -- look for a known named condition in the text
        # immediately around this filter's own mention (never globally, so two
        # different filters in the same goal don't cross-contaminate).
        window_start = max(0, match.start() - 20)
        window_end = min(len(goal_text), match.end() + 80)
        window = goal_text[window_start:window_end]
        resolved = None
        for pattern, field, value in _NAMED_CONDITION_TO_DOMAIN:
            if pattern.search(window):
                resolved = ExtractedQuickFilter(label=label, field=field, value=value)
                break
        if resolved is None and len(label.split()) == 1:
            # Real, confirmed recurring gap found live (2026-08-12, day-to-day directions sweep,
            # Batch A): a label like "Accepted" naming a state value directly (goal text: "...quick
            # filter button called 'Accepted' that shows only accepted records...") has no idiom in
            # _NAMED_CONDITION_TO_DOMAIN above, so the whole extraction used to bail even though
            # this filter's own label is a literal, unambiguous echo of the state word right there
            # in the same sentence -- never a guess, the goal text says it twice. Deliberately
            # narrow: only fires when the label's own lowercased word reappears verbatim right
            # after "only" in the same window (single-word labels only -- a multi-word label like
            # "My records" is never a bare state value and is left to the idiom table above).
            echo_re = re.compile(rf"\bonly\s+{re.escape(label.lower())}\b", re.IGNORECASE)
            if echo_re.search(window):
                resolved = ExtractedQuickFilter(label=label, field="state", value=label.lower())
        if resolved is None:
            return None  # a named filter with no resolvable condition -- never guess
        results.append(resolved)

    return results or None


def render_filter_domain(f: ExtractedQuickFilter) -> str:
    if f.value == "uid":
        return f"[('{f.field}','=',uid)]"
    if f.value in ("True", "False"):
        # Real fix (2026-08-12, day-to-day directions sweep): a live-schema-verified boolean
        # field substitution (e.g. 'active'/'inactive' -> the real active field) must render as
        # a real Python bool, never a quoted string -- Odoo would otherwise compare the column
        # against the literal string 'True', which never matches a real boolean column.
        return f"[('{f.field}','=',{f.value})]"
    return f"[('{f.field}','=','{f.value}')]"


def build_deterministic_search_view_snippet(filters: list[ExtractedQuickFilter]) -> str:
    """Renders the <filter> elements (plus a <separator/> before them, matching
    this project's own established real-goal convention) to insert into an
    inherited search view's own xpath target."""
    lines = ["            <separator/>"]
    for f in filters:
        name = re.sub(r"[^a-z0-9_]", "_", f.label.lower())
        lines.append(f'            <filter name="{name}" string="{f.label}" domain="{render_filter_domain(f)}"/>')
    return "\n".join(lines)
