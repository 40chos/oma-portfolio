"""Phase 34 execution (2026-08-11/12): real, general validators for the
wizard_transient_model shape, built specifically so this domain can be safely
removed from contracts/unsupported_domains.json's permanent block-list.

This codebase's own general infrastructure already recognizes and handles
`models.TransientModel` correctly in several places (see specialists/build/
specialist.py line ~3788, ~14167 -- TransientModel has always been a
recognized real base class, and a real historical task, "task 019," was a
TransientModel wizard the general infrastructure was tuned around). The
domain was later added to the permanent block-list (Phase 30) as a blanket
safety measure, not because wizards are structurally impossible -- but
without any wizard-SPECIFIC check, the two real, deterministic mistakes a
wizard generation could make (using `models.Model` instead of
`models.TransientModel`, creating a real persistent table for what should be
a throwaway dialog; and a window action missing `target="new"`, opening the
wizard as a full page instead of a popup) had no dedicated guard. These two
checks close that gap, matching the same "narrow, deterministic, general"
discipline as every other validator in this pipeline.
"""

from __future__ import annotations

import re

_WIZARD_INTENT_RE = re.compile(
    r"\bpopup\b|\bwizard\b|\bconfirmation dialog\b|\bconfirm(?:ation)? (?:box|screen)\b",
    re.IGNORECASE,
)
_CLASS_DECLARATION_RE = re.compile(
    r"class\s+(\w+)\s*\(models\.(\w+)\):\s*\n((?:[ \t]+.*\n?)*)", re.MULTILINE,
)
_NAME_ATTR_RE = re.compile(r"^\s*_name\s*=\s*['\"]([\w.]+)['\"]", re.MULTILINE)
_TRANSIENT_MODEL_HINT_RE = re.compile(
    r"\bwizard\b|\bconfirm\b|\bpopup\b", re.IGNORECASE,
)


def validate_wizard_intent_uses_transient_model_not_persistent_model(generated, goal: str) -> None:
    """When the goal signals a popup/wizard/confirmation-dialog shape, any NEW
    model this round declares whose own name looks wizard-related (contains
    'wizard'/'confirm'/'popup') must use `models.TransientModel`, never
    `models.Model` -- the latter would create a real, permanent database
    table for what should be a throwaway dialog record, silently leaking
    storage forever. Never fires on a goal with no wizard/popup intent at
    all, and never touches a NEW model whose own name gives no wizard-
    related hint (a genuine new persistent model added in the same round
    stays untouched).
    """
    if not _WIZARD_INTENT_RE.search(goal):
        return
    models_py = getattr(generated, "models_py", "") or ""
    for class_match in _CLASS_DECLARATION_RE.finditer(models_py):
        class_name, base_class, class_body = (
            class_match.group(1), class_match.group(2), class_match.group(3),
        )
        name_match = _NAME_ATTR_RE.search(class_body)
        if not name_match:
            continue
        declared_name = name_match.group(1)
        if base_class == "TransientModel":
            continue  # already correct
        if not _TRANSIENT_MODEL_HINT_RE.search(class_name) and not _TRANSIENT_MODEL_HINT_RE.search(declared_name):
            continue  # this new model's own name gives no wizard-related signal -- leave it alone
        raise ValueError(
            f"the goal describes a popup/wizard/confirmation-dialog shape, and class "
            f"{class_name!r} (_name={declared_name!r}) looks wizard-related by name, but it uses "
            f"`models.{base_class}` instead of `models.TransientModel` -- this would create a "
            f"real, permanent database table for what should be a throwaway dialog record. Use "
            f"`models.TransientModel` for this class."
        )


_WINDOW_ACTION_RECORD_RE = re.compile(
    r'<record[^>]*model="ir\.actions\.act_window"[^>]*>(.*?)</record>', re.DOTALL,
)
_ACTION_RES_MODEL_RE = re.compile(r'<field\s+name="res_model">([\w.]+)</field>')
_ACTION_TARGET_RE = re.compile(r'<field\s+name="target">(\w+)</field>')


def validate_wizard_window_action_opens_as_a_dialog(generated, goal: str, wizard_model_names: set[str]) -> None:
    """When the goal signals a popup/wizard shape, any window action whose
    `res_model` is one of THIS round's own newly-declared TransientModel
    wizards must declare `target="new"` -- without it, Odoo opens the wizard
    as an ordinary full-page view, not a popup/dialog, silently defeating the
    entire point of the goal's own request. Never fires when there's no
    wizard intent, or when no window action targets a wizard model at all
    (a window action for an ordinary persistent model is untouched).
    """
    if not _WIZARD_INTENT_RE.search(goal) or not wizard_model_names:
        return
    views_xml = getattr(generated, "views_xml", "") or ""
    extra_data_files = getattr(generated, "extra_data_files", None) or {}
    for content in (views_xml, *extra_data_files.values()):
        if not content:
            continue
        for action_match in _WINDOW_ACTION_RECORD_RE.finditer(content):
            body = action_match.group(1)
            model_match = _ACTION_RES_MODEL_RE.search(body)
            if not model_match or model_match.group(1) not in wizard_model_names:
                continue
            target_match = _ACTION_TARGET_RE.search(body)
            if not target_match or target_match.group(1) != "new":
                raise ValueError(
                    f"the goal describes a popup/wizard shape, and a window action targets "
                    f"{model_match.group(1)!r} (one of this round's own new TransientModel "
                    f"wizards), but it does not declare `<field name=\"target\">new</field>` -- "
                    f"without it, Odoo opens this as an ordinary full-page view, not a popup/"
                    f"dialog, defeating the goal's own request. Add target=\"new\" to this action."
                )


def find_new_transient_model_names(generated) -> set[str]:
    """Returns the `_name` of every NEW class this round declares using
    `models.TransientModel` -- shared helper so both validators above (and
    any future caller) derive this the same way, once."""
    models_py = getattr(generated, "models_py", "") or ""
    names = set()
    for class_match in _CLASS_DECLARATION_RE.finditer(models_py):
        base_class, class_body = class_match.group(2), class_match.group(3)
        if base_class != "TransientModel":
            continue
        name_match = _NAME_ATTR_RE.search(class_body)
        if name_match:
            names.add(name_match.group(1))
    return names
