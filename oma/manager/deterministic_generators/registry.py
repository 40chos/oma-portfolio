"""Phase 35 §15.9's kill-switch, as a real, persisted mechanism -- not
a comment. Matches state/scope_certification.json's own existing
pattern (a small, atomic-write JSON file, no database dependency) for
consistency with how this codebase already tracks this class of state.

§15.9's real requirement: disabling a generator is an explicit,
logged, human (adjudicator) decision, never automatic -- this module
provides the mechanism; it does not itself decide when to call
disable_generator(). That decision belongs to the adjudicator role
§11.1 already established, exactly as §15.9 specifies.
"""

from __future__ import annotations

import datetime
import json
import os

_STATE_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "state")
_STATE_PATH = os.path.join(_STATE_DIR, "deterministic_generators.json")


def _load() -> dict:
    if not os.path.exists(_STATE_PATH):
        return {}
    with open(_STATE_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _save(state: dict) -> None:
    os.makedirs(_STATE_DIR, exist_ok=True)
    tmp_path = _STATE_PATH + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, sort_keys=True)
    os.replace(tmp_path, _STATE_PATH)


def is_generator_live(scope: str) -> bool:
    """§15.5 step 1's real gate check. Defaults to False -- a generator
    is opt-in live, never live by default just because its code exists.
    This is the deliberate, explicit difference between "built and
    tested" (true for single_new_field as of this revision) and "live"
    (requires an explicit enable_generator() call, itself requiring
    §15.8's shadow-mode evidence to justify)."""
    state = _load()
    entry = state.get(scope)
    return bool(entry and entry.get("live") is True)


def enable_generator(scope: str, *, reason: str, actor: str) -> None:
    """§15.8's real "flip live" action -- explicit, logged, never automatic."""
    state = _load()
    state[scope] = {
        "live": True,
        "enabled_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "enabled_by": actor,
        "reason": reason,
    }
    _save(state)


def disable_generator(scope: str, *, reason: str, actor: str) -> None:
    """§15.9's kill-switch itself. Explicit, logged, human (adjudicator)
    decision -- this function does not decide WHEN to call itself; that
    judgment belongs to the adjudicator role per §15.9/§11.1."""
    state = _load()
    state[scope] = {
        "live": False,
        "disabled_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "disabled_by": actor,
        "reason": reason,
    }
    _save(state)


def get_generator_state(scope: str) -> dict | None:
    return _load().get(scope)
