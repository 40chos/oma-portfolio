"""The Manager-side companion to a specialist's own compensating_actions
bookkeeping -- Phase 9 step 4. A Build specialist fills in
`CompensatingAction(forward_step, undo_action)` for each real-state-
changing step as it actually executes it (not left generic or empty);
this module is what the Manager calls to actually run those undo
actions when a task gets cut off mid-sequence -- by turn_budget, by the
Phase 6 gateway-outage handling, or by a pause_if trigger.

`undo_action` follows a small, explicit convention rather than being
free-form prose the Manager has to interpret: `"<kind>:<arg1>:<arg2>..."`.
Only two kinds are needed for task 1's shape (scaffold-then-install);
more kinds get added here, deliberately, as later tasks' own
compensating actions need them -- never invented ahead of an actual
need, same spirit as odoo_tool_allowlist.yaml.
"""

from __future__ import annotations

from dataclasses import dataclass

from contracts.schema import CompensatingAction, TaskContract
from manager.task_state import PAUSE_TASK_CUT_OFF, set_task_state
from tools_odoo.module_dev.toolchain import remove_scaffolded_module, uninstall_module

_KIND_REMOVE_SCAFFOLDED_MODULE = "remove_scaffolded_module"
_KIND_UNINSTALL_MODULE = "uninstall_module"


class TaskCutOffPause(Exception):
    """Raised by manager.tools.delegate_to_specialist() after a
    specialist's PartialTaskFailure has been caught and its completed
    steps compensated -- worded distinctly from an ambiguity pause, a
    tier-3/4 sign-off pause, and a gateway-outage pause, per the
    established pattern (manager.gateway_orchestration.GatewayOutagePause).
    """

    def __init__(self, task_id: str, message: str, compensation_results: list["CompensationResult"]):
        super().__init__(message)
        self.task_id = task_id
        self.message = message
        self.compensation_results = compensation_results


def handle_partial_task_failure(
    task_id: str, contract: TaskContract, completed_steps: list[CompensatingAction],
    original: Exception | None = None,
) -> "TaskCutOffPause":
    """Runs the real compensations for a cut-off task and builds the
    TaskCutOffPause to raise -- factored out so manager.tools.py's
    delegate_to_specialist can call this in one line from its own
    except clause.

    Real, confirmed bug found live (2026-07-24, task 026, reproduced
    identically on 2 separate submissions): the message built here was
    FULLY GENERIC -- "cut off mid-sequence... N succeeded" -- with no
    trace of WHAT actually broke, even though the real exception was
    sitting right there the whole time (`PartialTaskFailure.original`,
    already threaded from Build's own except clause). This is strictly
    worse than every other error path in this codebase, which all give
    Build/Operator the concrete failure text to act on -- here it was
    silently thrown away, so a real crash (whatever it was) could never
    self-correct across retries and Operator had nothing to go on either.
    `original` is optional only so existing callers/tests that don't
    have it yet keep working unchanged; every real caller should pass it.
    """
    set_task_state(task_id, PAUSE_TASK_CUT_OFF)
    results = run_compensations(contract, completed_steps)
    ran_ok = [r for r in results if r.ran]
    ran_failed = [r for r in results if not r.ran]
    message = (
        f"This task was cut off mid-sequence after {len(completed_steps)} real step(s) had "
        f"already executed. I ran the recorded compensating (undo) actions for all of them, "
        f"walking backward: {len(ran_ok)} succeeded"
        + (f", {len(ran_failed)} did NOT clean up automatically and may need manual attention"
           if ran_failed else "")
        + "."
    )
    if original is not None:
        message += f" Real error that caused the cut-off: {type(original).__name__}: {original}"
    return TaskCutOffPause(task_id, message, results)


@dataclass
class CompensationResult:
    forward_step: str
    undo_action: str
    ran: bool
    detail: str


def run_compensations(
    contract: TaskContract, completed_steps: list[CompensatingAction]
) -> list[CompensationResult]:
    """Walks BACKWARD through completed_steps (the ones that actually
    executed before cutoff -- never contract.compensating_actions in
    full, since some of those may never have run) and runs each one's
    undo_action for real. Returns one CompensationResult per step,
    including any step whose undo_action kind isn't recognized -- that
    case is reported, never silently skipped, since an unrecognized
    kind means real state may be left behind uncleaned.
    """
    results: list[CompensationResult] = []
    for step in reversed(completed_steps):
        results.append(_run_one(step, task_id=str(contract.task_id)))
    return results


def _run_one(step: CompensatingAction, task_id: str | None = None) -> CompensationResult:
    parts = step.undo_action.split(":")
    kind = parts[0]

    if kind == _KIND_REMOVE_SCAFFOLDED_MODULE:
        module_name = parts[1]
        remove_scaffolded_module(module_name)
        return CompensationResult(
            forward_step=step.forward_step,
            undo_action=step.undo_action,
            ran=True,
            detail=f"removed scaffolded module {module_name!r} from /mnt/extra-addons",
        )

    if kind == _KIND_UNINSTALL_MODULE:
        module_name, db = parts[1], parts[2]
        result = uninstall_module(module_name, db, task_id=task_id)
        return CompensationResult(
            forward_step=step.forward_step,
            undo_action=step.undo_action,
            ran=result.success,
            detail=result.message if result.success else f"uninstall failed: {result.message}",
        )

    return CompensationResult(
        forward_step=step.forward_step,
        undo_action=step.undo_action,
        ran=False,
        detail=(
            f"unrecognized undo_action kind {kind!r} -- no automatic cleanup ran for this "
            f"step; real state may still need manual attention."
        ),
    )
