"""The abstract interface every specialist implements -- Phase 5, step 2.

Kept as small as it can possibly be: one required method. The fewer
assumptions baked in here, the easier every future specialist (four,
five, ...) is to add without reworking this shared contract.

run() returns SpecialistOutput, NOT VerificationResult -- a specialist
reports what it believes it did; turning that into an actual, trusted
VerificationResult is the Manager's job (Phase 6), combining this
report with a deterministic spot-check and (for Build-specialist work)
the Testing/QA specialist's own independent report. No specialist ever
verifies itself.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from contracts.schema import SpecialistOutput, TaskContract


@runtime_checkable
class Specialist(Protocol):
    async def run(self, contract: TaskContract) -> SpecialistOutput:
        ...
