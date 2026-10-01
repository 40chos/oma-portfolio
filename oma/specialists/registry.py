"""A simple mapping from SpecialistType to a concrete Specialist
implementation -- Phase 5, step 3. Deliberately almost boring: the
value isn't in this registry's own complexity, it's in the discipline
of the Manager only ever calling registry.get(...), never importing a
specialist module directly by name anywhere in its own decision logic.
That discipline is enforced mechanically in this same phase (step 4,
see import_linter_contracts.cfg + README) -- a good rule with no
enforcement erodes the first time someone's in a hurry.
"""

from __future__ import annotations

from contracts.schema import SpecialistType
from specialists.base import Specialist

_registry: dict[SpecialistType, Specialist] = {}


class SpecialistNotAvailableError(RuntimeError):
    """Raised when the Manager tries to route to a SpecialistType that
    isn't registered. Deliberately a clear, loud error rather than a
    hollow stub that looks like it does something -- per step 5's own
    instruction: migration/gui_productionization aren't registered yet,
    and shouldn't pretend to be.
    """


def register(specialist_type: SpecialistType, implementation: Specialist) -> None:
    _registry[specialist_type] = implementation


def get(specialist_type: SpecialistType) -> Specialist:
    if specialist_type not in _registry:
        raise SpecialistNotAvailableError(
            f"No specialist registered for {specialist_type!r}. "
            f"Registered: {sorted(t.value for t in _registry)}"
        )
    return _registry[specialist_type]


def registered_types() -> list[SpecialistType]:
    return list(_registry.keys())


def clear() -> None:
    """Test-only helper -- resets the registry between test runs so one
    test's fake registration can't leak into another's.
    """
    _registry.clear()
