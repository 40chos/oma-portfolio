"""Shared specialist-registration logic for anything that runs the
Manager's loop against the real registry -- the CLI harness (Phase 6)
and the chat UI server (Phase 12) both need exactly this, so it lives
here once rather than copy-pasted into each entry point.

Deliberately lives under scripts/, NOT manager/ -- import-linter's
"Manager must route to specialists only through the registry" contract
(pyproject.toml) forbids anything under manager/ from importing a
specialist module directly; only specialists.registry is exempted.
scripts/ (like tests/) sits outside that contract's root_packages
entirely, which is exactly why cli_harness.py -- the same pattern this
factors out of -- already lived there rather than under manager/.
"""

from __future__ import annotations

import os

from contracts.schema import SpecialistOutput, SpecialistType, TaskContract
from infra.gateway_client import ModelGatewayClient
from specialists import registry
from specialists.build.specialist import BuildSpecialist
from specialists.code_review.specialist import CodeReviewSpecialist
from specialists.testing_qa.specialist import TestingQASpecialist


class DemoFakeSpecialist:
    """Stands in for a real specialist when no target duplicate
    database is configured -- always "succeeds," clearly labeled.
    """
    async def run(self, contract: TaskContract) -> SpecialistOutput:
        return SpecialistOutput(
            task_id=contract.task_id,
            specialist_type=contract.specialist_type,
            summary=f"[DEMO FAKE SPECIALIST -- no OMA_ODOO_DB_DUPLICATE_FOR_BUILD set] "
                    f"echoed goal: {contract.goal}",
            detail={"uncovered_paths": [], "coverage_diff": "N/A (demo fake)"},
            claims_complete=True,
            artifacts=[],
        )


def register_default_specialists(client: ModelGatewayClient) -> bool:
    """Registers bug_fix, code_review, and testing_qa. Returns True if
    the real Odoo-backed specialists were registered, False if the demo
    fake had to stand in for bug_fix/testing_qa (no target database
    configured). code_review is always registered for real -- it doesn't
    STRICTLY require a target duplicate database to run at all (unlike
    bug_fix/testing_qa, which have no fallback), only the SSH/container
    access already required for everything else. It IS given the same db
    (Phase 30 §26 follow-up, 2026-08-04) so its own live-schema-grounded
    review can engage when available -- an empty db degrades that one
    feature gracefully, never blocks the review itself.
    """
    db = os.environ.get("OMA_ODOO_DB_DUPLICATE_FOR_BUILD", "")
    registry.register(SpecialistType.code_review, CodeReviewSpecialist(client=client, db=db))
    if db:
        registry.register(SpecialistType.bug_fix, BuildSpecialist(client=client, db=db))
        registry.register(SpecialistType.testing_qa, TestingQASpecialist(client=client))
        return True

    registry.register(SpecialistType.bug_fix, DemoFakeSpecialist())
    registry.register(SpecialistType.testing_qa, DemoFakeSpecialist())
    return False
