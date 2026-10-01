"""Phase 35 §15.6 -- live precondition checks. A deterministic
generator has no awareness of the live Odoo instance unless it's given
one; this module is that awareness, reusing the real, already-proven
fast-path functions in tools_odoo/odoo_schema_client.py rather than
inventing new ones.

Contract, stated once: every check function here returns True only on
a confirmed-good live read. Any False OR None (the real, deliberate
"could not verify" contract check_field_exists_on_model_fast() and
friends already use -- never silently treated as success) means the
precondition is NOT satisfied, and §15.5's caller must fall back to
the LLM path. This module never guesses past an unknown live state.
"""

from __future__ import annotations

from tools_odoo.odoo_schema_client import (
    check_field_exists_on_model_fast,
    get_model_fields_fast,
    get_module_state_fast,
)


class PreconditionResult:
    def __init__(self, ok: bool, reason: str):
        self.ok = ok
        self.reason = reason

    def __bool__(self) -> bool:
        return self.ok

    def __repr__(self) -> str:
        return f"PreconditionResult(ok={self.ok}, reason={self.reason!r})"


def check_single_field_addition_preconditions(
    module_name: str, model_name: str, field_name: str, db: str, login: str = "Admin"
) -> PreconditionResult:
    """§15.6's three real, live checks, run against the actual server --
    not simulated, not assumed. Any ambiguous (None) result from the
    underlying fast-path functions is treated as a failed precondition,
    per those functions' own explicit "None means fall back, never
    guess" contract.
    """
    state = get_module_state_fast(module_name, db, login)
    if state != "installed":
        return PreconditionResult(False, f"module {module_name!r} real state is {state!r}, not 'installed'")

    fields = get_model_fields_fast(model_name, db, login)
    if fields is None:
        return PreconditionResult(False, f"could not read real fields for model {model_name!r} (fast path returned None)")
    if not fields:
        return PreconditionResult(False, f"model {model_name!r} does not appear to exist (zero real fields read)")

    already_exists = check_field_exists_on_model_fast(db, model_name, field_name, login)
    if already_exists is None:
        return PreconditionResult(False, f"could not verify whether field {field_name!r} already exists (fast path returned None)")
    if already_exists:
        return PreconditionResult(False, f"field {field_name!r} already exists on {model_name!r} -- real collision, not a guess")

    return PreconditionResult(True, "all three live preconditions satisfied")
