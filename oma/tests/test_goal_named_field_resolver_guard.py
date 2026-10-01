"""Phase 26C (2026-07-27, audit Finding #3): a real, repo-local guard
against the exact recurrence class this phase exists to fix.

Real history this protects against: `_resolve_current_round_named_field()`
(specialists/build/specialist.py) was built in Phase 25F specifically to
replace an old, confirmed-wrong guard (`if len(named_fields) != 1: return`,
which bails for the WHOLE task, every round, the moment a decomposed
goal names more than one field anywhere in its text) -- but nothing
enforced every function sharing this shape actually migrated to it.
Phase 26C's own audit found the two originally-known-broken siblings
(`_autofix_goal_named_sum_compute_field_missing`, `_autofix_goal_named_
sequence_field_missing`) still using the old guard -- AND two MORE
instances that had crept in the very same day, in two brand-new
functions Phase 26A itself added (`_autofix_goal_named_stat_button_
missing`, `_autofix_goal_named_computed_field_missing_declaration`),
neither one part of the original audit's own written evidence. Four
real instances of the same bug, found by hand, twice, in one week --
exactly the kind of "fixed in one place, not propagated to the next"
pattern this project's own Phase 26 investigation exists to close
generally, not just patch the two currently-known cases.

Introspects the REAL function objects (inspect.getsource()), not a
separate hand-maintained list -- so this fails the moment ANY future
`_autofix_goal_named_*`/`_validate_goal_named_*` function is added
using the old guard shape, without anyone needing to remember to check.
"""

import inspect
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import specialists.build.specialist as build_module

_TARGET_NAME_RE = re.compile(r"^_(autofix|validate)_goal_named_.*$")
_OLD_GUARD_RE = re.compile(r"len\(\s*named_fields\s*\)\s*!=\s*1")


def _goal_named_functions() -> dict[str, object]:
    return {
        name: obj for name, obj in inspect.getmembers(build_module)
        if (inspect.isfunction(obj) or inspect.iscoroutinefunction(obj)) and _TARGET_NAME_RE.match(name)
    }


def _code_only(source: str) -> str:
    """Strips full-line and trailing '#' comments -- this guard must
    check real CODE, not prose explaining the old pattern by name
    (which every fixed function's own docstring/comment now does)."""
    lines = []
    for line in source.splitlines():
        stripped = line.split("#", 1)[0]
        lines.append(stripped)
    return "\n".join(lines)


def test_every_goal_named_field_function_uses_the_shared_resolver_not_the_old_guard():
    functions = _goal_named_functions()
    assert len(functions) >= 6, (
        f"expected to find at least the 6 known _autofix_goal_named_*/_validate_goal_named_* "
        f"functions confirmed present as of Phase 26C -- got {sorted(functions)}. If this is a "
        f"real, deliberate removal, update this count; if it's fewer than expected, something's wrong."
    )
    offenders = []
    for name, func in functions.items():
        source = _code_only(inspect.getsource(func))
        if _OLD_GUARD_RE.search(source):
            offenders.append(name)
    assert not offenders, (
        f"found function(s) still using the OLD, confirmed-wrong "
        f"'len(named_fields) != 1: return' guard instead of "
        f"_resolve_current_round_named_field(): {offenders} -- this silently bails for the WHOLE "
        f"task, every round, the moment a decomposed goal names more than one field anywhere in "
        f"its text, even when the round's own explicit focus marker unambiguously names THIS "
        f"round's field. Route through _resolve_current_round_named_field(goal, named_fields) "
        f"instead, matching every other sibling in this file."
    )
    print(f"PASS: all {len(functions)} _autofix_goal_named_*/_validate_goal_named_* functions "
          f"({sorted(functions)}) correctly route through the shared resolver, none use the old guard")


def test_functions_that_resolve_a_named_field_actually_call_the_resolver():
    """A stricter, positive check: any function whose source references
    `_GOAL_NAMED_FIELD_RE` (i.e. it's resolving a field name from goal
    text at all) must also reference `_resolve_current_round_named_field`
    somewhere in its own body -- catches a hypothetical future variant
    of the same mistake that doesn't literally spell `!= 1` (e.g. `== 1`
    inverted, or a differently-worded equivalent bail-out).
    """
    functions = _goal_named_functions()
    offenders = []
    for name, func in functions.items():
        source = inspect.getsource(func)
        if "_GOAL_NAMED_FIELD_RE" in source and "_resolve_current_round_named_field" not in source:
            offenders.append(name)
    assert not offenders, (
        f"function(s) resolve a field name from goal text (reference _GOAL_NAMED_FIELD_RE) but "
        f"never call _resolve_current_round_named_field() anywhere in their own body: {offenders} "
        f"-- likely a hand-rolled equivalent of the same old, wrong single-field-only assumption."
    )
    print(f"PASS: every function referencing _GOAL_NAMED_FIELD_RE also correctly calls "
          f"_resolve_current_round_named_field()")


if __name__ == "__main__":
    test_every_goal_named_field_function_uses_the_shared_resolver_not_the_old_guard()
    test_functions_that_resolve_a_named_field_actually_call_the_resolver()
    print("\nALL PHASE 26C GOAL-NAMED-FIELD RESOLVER GUARD TESTS PASSED")
