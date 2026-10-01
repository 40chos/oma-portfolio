"""Phase 32 implementation (2026-08-11): a lightweight version of the
"headless UI/E2E rendering check" recommended by docs/planning/
PHASE32_RELIABILITY_ROOT_CAUSE_AND_ROLLOUT_STRATEGY_2026-08-11.md section
3.1 item 1 -- deliberately NOT a full headless-browser harness (the
document itself flags that as costly/flaky and says to validate a cheaper
version first). This is that cheaper version: given the task's own goal
text and the actual rendered form-view arch (fetched via fields_view_get,
the exact technique used live tonight to diagnose the real bug this
closes), check whether every workflow action the goal explicitly promises
by name is really present in the arch Odoo will render.

Real, confirmed gap this closes: the flagship ticket task's goal text
promised two named buttons ("Start" and "Resolve") and a chatter/activity
log; the underlying Python action methods (action_start_work(),
action_resolve()) were fully correct, but the form view XML was never
updated to add <header> buttons or an mail.thread chatter div -- every
existing artifact-level and backend check passed, because they never
looked at the rendered view at all. Confirmed live via a direct
fields_view_get(view_type="form") call in an odoo-bin shell script.

Only runs when the goal text names a concrete UI interaction (per Phase
32's own scoping: "only run when goal names a concrete UI interaction").
Detect-and-report only -- this never fails a task on its own; it appends
a note a human (or a future stricter gate) can act on.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_BUTTON_LABEL_RE = re.compile(r"['\"]([A-Z][A-Za-z0-9 _/-]{1,40})['\"]\s*(?:button|buttons)?", re.IGNORECASE)
# Real, confirmed false positive found live (2026-08-15, quick_filter_search_construction
# certification): "quick filter button"/"filter button" is Odoo's own extremely common,
# natural way to describe a <filter> element (a clickable toggle in the SEARCH view's own
# filter bar) -- structurally NOT a `<button>` form-view widget at all. The bare `\bbuttons?\b`
# below matched this phrasing every single time, causing _extract_named_buttons() to treat the
# filter's own quoted label (e.g. "Low Stock", "No Description") as an EXPECTED `<button
# string="...">` in the FORM view -- which of course never exists, guaranteeing a false
# UI-action-gap warning (and a real bake-in-certification qualifying failure) on every quick-
# filter task regardless of whether the actual deliverable was built correctly. Confirmed live:
# 3 of 3 recent quick-filter tasks tonight hit this, including one with a fully correct,
# verified <filter> element and a single clean round. Same root-cause SHAPE (and same fix
# approach) as the earlier "tracking"/chatter false positive documented below -- an overly
# broad word match catching Odoo's own colloquial terminology for a DIFFERENT UI mechanism.
_BUTTON_MENTION_RE = re.compile(r"(?<!filter )(?<!quick filter )\bbuttons?\b", re.IGNORECASE)
# Real, confirmed bug found live overnight (2026-08-14 direction-certification run): the
# bare `\btracked\b|\btracking\b` alternatives matched the word "tracking" in its ordinary
# English sense ("a model for TRACKING supply requests", "TRACKING monthly book club
# picks") -- nothing to do with Odoo's own chatter/mail.thread feature at all, but
# "tracking X" is an extremely common, natural way to describe ANY new record-keeping
# model. Confirmed live: 3 of 3 real, otherwise-clean "new self-contained module" tasks in
# one overnight batch got a false chatter_expected=True purely from this word, each one
# incorrectly recorded as a qualifying failure and escalating that direction's real
# certification bar (29 -> 232 across three occurrences of this same false positive) --
# none of the three goals asked for a chatter widget at all. Narrowed to the phrases that
# actually signal Odoo's own chatter/activity-log feature specifically; a goal simply
# describing what a new model is FOR ("tracking X") no longer false-triggers this check.
_CHATTER_MENTION_RE = re.compile(
    r"\bchatter\b|\bactivity log\b|\bmessage log\b|\bactivity tracking\b|\bfield tracking\b",
    re.IGNORECASE,
)
_STATUSBAR_MENTION_RE = re.compile(r"\bstatus\s*bar\b|\bstate\s+widget\b", re.IGNORECASE)


@dataclass
class UiActionPresenceResult:
    applicable: bool
    named_buttons_expected: list[str] = field(default_factory=list)
    named_buttons_missing: list[str] = field(default_factory=list)
    chatter_expected: bool = False
    chatter_present: bool = False
    statusbar_expected: bool = False
    statusbar_present: bool = False

    @property
    def has_gap(self) -> bool:
        if not self.applicable:
            return False
        return bool(self.named_buttons_missing) or (self.chatter_expected and not self.chatter_present) or (
            self.statusbar_expected and not self.statusbar_present
        )


def _extract_named_buttons(goal_text: str) -> list[str]:
    if not _BUTTON_MENTION_RE.search(goal_text):
        return []
    names = []
    for match in _BUTTON_LABEL_RE.finditer(goal_text):
        label = match.group(1).strip()
        # Reject obvious false positives: quoted model/technical names rarely
        # look like a short, capitalized action label a real button would use.
        if 1 <= len(label.split()) <= 4 and label[0].isupper():
            names.append(label)
    # De-duplicate, preserve order.
    seen = set()
    unique = []
    for name in names:
        if name not in seen:
            seen.add(name)
            unique.append(name)
    return unique


def _button_present_in_arch(button_label: str, view_arch_xml: str) -> bool:
    escaped = re.escape(button_label)
    # A <button> element whose string=/attribute mentions the label, anywhere
    # in the tag (order of attributes is not guaranteed).
    pattern = re.compile(rf"<button[^>]*\bstring=[\"']{escaped}[\"'][^>]*>", re.IGNORECASE)
    return bool(pattern.search(view_arch_xml))


def check_goal_named_ui_actions_present(goal_text: str, view_arch_xml: str) -> UiActionPresenceResult:
    named_buttons = _extract_named_buttons(goal_text)
    chatter_expected = bool(_CHATTER_MENTION_RE.search(goal_text))
    statusbar_expected = bool(_STATUSBAR_MENTION_RE.search(goal_text))
    applicable = bool(named_buttons) or chatter_expected or statusbar_expected
    if not applicable:
        return UiActionPresenceResult(applicable=False)

    missing = [b for b in named_buttons if not _button_present_in_arch(b, view_arch_xml)]
    chatter_present = bool(re.search(r"oe_chatter|mail_thread|message_ids", view_arch_xml))
    statusbar_present = bool(re.search(r"widget=[\"']statusbar[\"']", view_arch_xml, re.IGNORECASE))

    return UiActionPresenceResult(
        applicable=True,
        named_buttons_expected=named_buttons,
        named_buttons_missing=missing,
        chatter_expected=chatter_expected,
        chatter_present=chatter_present,
        statusbar_expected=statusbar_expected,
        statusbar_present=statusbar_present,
    )


def format_ui_action_gap_warning(result: UiActionPresenceResult) -> str:
    parts = []
    if result.named_buttons_missing:
        parts.append(
            f"named button(s) the goal promised are missing from the rendered form view: "
            f"{', '.join(result.named_buttons_missing)}"
        )
    if result.chatter_expected and not result.chatter_present:
        parts.append("the goal expects a chatter/activity log, but no chatter widget is in the rendered view")
    if result.statusbar_expected and not result.statusbar_present:
        parts.append("the goal expects a status bar, but no widget=\"statusbar\" is in the rendered view")
    return (
        "⚠ UI ACTION PRESENCE CHECK: the underlying logic may be correct, but a human opening this "
        "record will not see what the goal promised -- " + "; ".join(parts) + "."
    )
