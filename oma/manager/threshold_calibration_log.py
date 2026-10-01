"""Phase 35 §18.3: threshold-calibration logging infrastructure -- NOT full calibration itself
(§18.3's own research is explicit: that requires weeks of real shadow-mode data before any
number can be honestly calibrated), but the real, structured LOGGING every step of that
procedure depends on: "For every logged event, record the raw metric value plus a post-hoc
ground-truth label." This module builds the first half (the raw metric value, verdict, and
threshold, logged at decision time); the ground-truth labeling step is a separate, later,
partly-organizational process (§18.3's own recommendation: a human owner approves the cost ratio,
not this code) that reads these logged rows back out, not something this module invents.

Deliberately reuses the existing manager.tools.append_project_memory() store (Postgres
agent_memory_events, already used throughout this codebase for durable structured logging) rather
than building a new log store -- no new infrastructure, matching this project's own
no-architectural-drift rule. Uses the existing "decision" event_type (a gate decision is
semantically a decision) rather than widening append_project_memory()'s own validated event_type
enum, a more conservative choice given how central and heavily-used that function already is.
"""

from __future__ import annotations


def log_gate_decision(
    gate_name: str,
    metric_name: str,
    metric_value: float | int,
    threshold: float | int | None,
    verdict: str,
    task_id: str | None = None,
    extra: dict | None = None,
) -> None:
    """Logs one real gate decision for future §18.3 calibration. Never raises -- a calibration-
    logging failure must never affect the real task outcome it's observing, same fail-open
    discipline as every other gate in this Phase 35 effort.
    """
    try:
        from manager.tools import append_project_memory

        append_project_memory(
            event_type="decision",
            actor="graph_governance",
            task_id=task_id,
            module=None,
            summary=(
                f"[threshold_calibration] gate={gate_name!r} metric={metric_name}="
                f"{metric_value} threshold={threshold} verdict={verdict!r}"
            ),
            tags=["threshold_calibration", gate_name],
            detail={
                "gate_name": gate_name,
                "metric_name": metric_name,
                "metric_value": metric_value,
                "threshold": threshold,
                "verdict": verdict,
                **(extra or {}),
            },
            verified=True,
        )
    except Exception:  # noqa: BLE001 -- calibration logging must never affect the real task
        pass
