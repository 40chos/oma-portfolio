"""P12 Tier S item 1 (`docs/planning/PHASE30_ARCH_SYNTHESIS_CRITIQUE_FINAL_2026-07-30.md` §4):
"Give Testing/QA an explicit fourth capability-class handler for `data_change`, or disable the
route until it's real (capability-readiness registry). Resolves Bug #2."

Real, confirmed premise (found independently, twice, this project's own real task history --
tasks 014 and 016 of the 2026-07-30 SITE 50-task benchmark both hit this live):
`BuildSpecialist._run_data_change()` (`specialists/build/specialist.py`) is a permanent stub,
documented in its own docstring as carried forward unimplemented since Phase 6/9 --
"real scope-extraction (turning free text into a concrete model/operation) doesn't exist yet" --
and it unconditionally returns `claims_complete=False`. Every real `data_change`-classified task
burns a full real round (an LLM classification call, contract construction, dispatch) before
failing on a structurally guaranteed dead end, every single time, with no way to know in advance.

This module is the "disable the route until it's real" half of the item -- the safer, more
conservative option per the item's own wording, and the only one buildable without a live model
call during a GPU-access pause window (a real `data_change` implementation was, at the time, a
much larger, separate effort this module deliberately did not attempt). Matches this project's own
established "fail loud before wasting a round" discipline (`manager/governance.py`'s hard
pre-flight gates, `classify_unsupported_domain_touches()`) -- checked once, deterministically,
right after capability classification, before a contract is ever built.

**Update, P14 item 1 (2026-08-01):** the real `data_change` implementation now exists
(`BuildSpecialist._run_data_change()`, `contracts/data_change_extraction.py`) -- but
`CAPABILITY_READY["data_change"]` is still `False` here, deliberately. Building the handler and
flipping this flag live are two separate decisions; the second one enables real, autonomous writes
against real Odoo data for the first time and is left for explicit review, not defaulted into
silently just because the code underneath it is now real and tested.
"""

from __future__ import annotations

# Real, live-confirmed readiness per capability_class label (manager/classify.py's own
# VALID_LABELS: "module_dev", "readonly", "data_change"). `True` means a real, working specialist
# handler exists; `False` means the route is a documented, permanent stub -- gated here rather than
# silently attempted. An unrecognized label is never blocked here (fail open on uncertainty, the
# same discipline every sibling gate in this project already follows) -- classify.py's own
# VALID_LABELS check is the real source of truth for "is this a recognized label at all."
CAPABILITY_READY: dict[str, bool] = {
    "module_dev": True,
    "readonly": True,
    # P14 item 1 (2026-08-01): BuildSpecialist._run_data_change() is no longer a permanent stub --
    # a real, tested, skill-compliant handler now exists (contracts/data_change_extraction.py +
    # the real OdooToolClient/fencing/JIT-key execution path). Left False here DELIBERATELY: this
    # is the "go live" flip, which enables real, autonomous writes against real Odoo data for the
    # first time -- a decision requiring explicit review, not something to default into silently
    # just because the code underneath it is ready. See tests/test_run_data_change_handler.py for
    # what's actually been verified so far (mocked infra, no live writes attempted).
    "data_change": False,
}


def capability_class_is_ready(capability_class_label: str) -> bool:
    """True if a real, working specialist handler exists for this capability class label.
    Fails open (returns True) for any label not in the registry -- this function's job is to
    catch a KNOWN dead route, never to second-guess a label it doesn't recognize.
    """
    return CAPABILITY_READY.get(capability_class_label, True)


def build_capability_not_ready_block(capability_class_label: str) -> dict:
    """Returns the same {"status": "paused", "reason": ..., "message": ...} structured pause
    shape `check_hard_governance_gates()` already returns, so `run_turn()`'s caller (a CLI or the
    real chat UI) renders this identically to every other pre-flight pause -- no new UI/CLI
    handling needed. Callers are expected to add `task_id`/`correction_result` themselves, the
    same convention `check_hard_governance_gates()`'s own call site already follows in
    `manager/loop.py`.
    """
    return {
        "status": "paused",
        "reason": "capability_not_ready",
        "message": (
            f"This task was classified as {capability_class_label!r}, but no real, working "
            f"specialist handler exists for that capability class yet -- it is a documented, "
            f"permanent stub (see manager/capability_readiness.py). Blocked here, before any "
            f"round starts, rather than silently attempting a guaranteed-dead path and burning a "
            f"real round to discover that. If this task can be reframed as a module_dev or "
            f"readonly request instead, please resubmit it that way."
        ),
        "capability_class_label": capability_class_label,
    }
