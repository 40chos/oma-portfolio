"""Phase 35 §17.11: the staged rollout mechanism for every new graph-consultation gate this
document adds (task-intake dedup, decomposition-level blast radius, post-install diff, etc).

Every new gate ships defaulting to `log_only` -- it runs for real, queries the real graph, computes
a real verdict, and publishes a real trace event and a real calibration-log row (§18.3), but it
NEVER blocks, escalates, or mutates a task's outcome. A gate only reaches `enforced` (where its
verdict can actually pause/reject a task) via an explicit entry in this file's state, never a code
default -- matching this project's own established pattern for `manager/scope_certification.py`'s
default-off `n_required` gating and `manager/deterministic_generators/registry.py`'s file-backed
kill-switch.

This is deliberately a flat, file-backed registry (same pattern as `scope_certification.json`), not
a database table or a new service -- no new infrastructure, consistent with this project's
no-architectural-drift rule.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum

_STATE_DIR = os.path.join(os.path.dirname(__file__), "..", "state")
_STATE_PATH = os.path.join(_STATE_DIR, "graph_governance_flags.json")


class GateMode(str, Enum):
    """`disabled`: the gate's own code never even runs (for a genuine kill-switch, e.g. rolling
    back a gate that's misbehaving). `log_only`: the gate runs for real, computes a real verdict,
    logs it, but never changes what the task does. `enforced`: the gate's verdict can actually
    auto-fix/escalate/block, per its own design section."""

    DISABLED = "disabled"
    LOG_ONLY = "log_only"
    ENFORCED = "enforced"


# Every gate this document defines gets an entry here, defaulting to log_only per §17.11 -- adding
# a new gate to the pipeline means adding its name here, not silently defaulting to enforced.
_KNOWN_GATES = frozenset({
    "intake_grounding_gate",       # §17.2.1
    "decomposition_blast_radius",  # §17.3.1
    "concurrent_claim_admission",  # §17.3.2
    "post_install_graph_diff",     # §17.6.1
    "rollback_dry_run_probe",      # §18.4
})

_DEFAULT_MODE = GateMode.LOG_ONLY


@dataclass
class GateState:
    mode: GateMode = _DEFAULT_MODE
    log_only_since: str | None = None
    enforced_since: str | None = None
    # §17.11's graduation criteria: a human-set flag, never self-promoted by the gate itself --
    # this module only ever reads it, promotion is a deliberate, separate, out-of-band action.
    graduation_approved_by: str | None = None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_state() -> dict:
    if not os.path.exists(_STATE_PATH):
        return {}
    with open(_STATE_PATH) as f:
        return json.load(f)


def _save_state(state: dict) -> None:
    os.makedirs(_STATE_DIR, exist_ok=True)
    tmp_path = _STATE_PATH + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(state, f, indent=2, sort_keys=True)
    os.replace(tmp_path, _STATE_PATH)


def get_gate_mode(gate_name: str) -> GateMode:
    """Fail-open to the safest option (log_only, never enforced) on any read error -- a corrupt
    or missing state file must never silently escalate a gate to enforced by accident."""
    if gate_name not in _KNOWN_GATES:
        raise ValueError(
            f"{gate_name!r} is not a registered graph-governance gate -- add it to "
            f"_KNOWN_GATES in manager/graph_governance_flags.py before wiring it into the "
            f"pipeline, so its rollout stage is always explicit, never assumed."
        )
    try:
        state = _load_state()
        raw_mode = state.get(gate_name, {}).get("mode")
        if raw_mode is None:
            return _DEFAULT_MODE
        return GateMode(raw_mode)
    except Exception:  # noqa: BLE001 -- fail-open to the safest mode, never crash a real task turn
        return GateMode.LOG_ONLY


def set_gate_mode(gate_name: str, mode: GateMode, approved_by: str) -> None:
    """The only way a gate ever reaches `enforced` -- an explicit, out-of-band, human-attributed
    call. Never invoked from inside a gate's own verdict logic."""
    if gate_name not in _KNOWN_GATES:
        raise ValueError(f"{gate_name!r} is not a registered graph-governance gate.")
    state = _load_state()
    entry = state.get(gate_name, {})
    entry["mode"] = mode.value
    if mode == GateMode.LOG_ONLY and "log_only_since" not in entry:
        entry["log_only_since"] = _now_iso()
    if mode == GateMode.ENFORCED:
        entry["enforced_since"] = _now_iso()
        entry["graduation_approved_by"] = approved_by
    state[gate_name] = entry
    _save_state(state)


def all_gate_states() -> dict[str, GateState]:
    state = _load_state()
    return {
        name: GateState(
            mode=GateMode(state.get(name, {}).get("mode", _DEFAULT_MODE.value)),
            log_only_since=state.get(name, {}).get("log_only_since"),
            enforced_since=state.get(name, {}).get("enforced_since"),
            graduation_approved_by=state.get(name, {}).get("graduation_approved_by"),
        )
        for name in sorted(_KNOWN_GATES)
    }
