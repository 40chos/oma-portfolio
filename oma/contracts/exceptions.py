"""Shared exception types that both manager/ and specialists/ need to
reference -- living here (a root package neither the import-linter
contract nor anything else restricts) specifically so a specialist can
raise PartialTaskFailure and manager/tools.py can catch it without
manager/ ever importing a specialist module directly (only
specialists.registry is allowed -- see pyproject.toml's importlinter
contract, "Manager must route to specialists only through the registry").
"""

from __future__ import annotations

from contracts.schema import CompensatingAction


class PartialTaskFailure(Exception):
    """Raised by a specialist when a multi-step task fails after at
    least one real, undo-able step already executed. Carries exactly
    the steps that really happened, in order, so
    manager.compensations.run_compensations() can walk them backward.
    """

    def __init__(self, original: Exception, completed_steps: list[CompensatingAction]):
        super().__init__(str(original))
        self.original = original
        self.completed_steps = completed_steps
