"""Phase U (§25.5 of PHASE30_OMA_SEAM_COVERAGE_AND_COMBINATORIAL_HARDENING_2026-07-29.md) --
category-correlation clustering and low-urgency digest batching for the ambiguity gate's own
escalation queue.

Real, evidenced problem this closes (§25.5): an un-rate-aware approval/escalation gate degrades to
near-100% rubber-stamped noise within months once volume passes ~200/day. OMA is explicitly built
to run hundreds of tasks unattended -- if many parallel tasks hit the SAME underlying category of
ambiguity (the real, observed shape in this codebase's own history: 16 of 18 real historical
`blocking_ambiguity` firings were the literal identical goal, "Route support tickets to the right
team automatically"), each one pinging Operator separately would be exactly this failure mode.

Design, deliberately reusing real, already-existing machinery rather than inventing new
infrastructure:
- Clustering: `scripts.rule_backlog_triage.normalize_summary()` (already imported elsewhere in
  this codebase the same lazy way, see manager/learning.py:173) -- the same shape-normalization
  this codebase already uses to collapse "the same underlying claim, different task-specific
  values" into one signature.
- Persistence: the exact same Redis-backed pending-item pattern `manager/escalations.py` already
  established for PauseForOperator escalations (a TTL'd JSON value keyed by signature, indexed via a
  Redis set) -- not a fourth storage mechanism.
- Urgency: goal-level ambiguity (which is specifically what `contracts/ambiguity_check.py`'s own
  deliberately-high bar catches -- see its own docstring) must be resolved before work starts, per
  the research this phase is built on (value collapses after ~10% of task progress) -- so the
  FIRST occurrence of a new ambiguity category is always surfaced immediately, never batched. What
  IS genuinely low-urgency, per the same research's own "would the human's answer in the next
  15-30 minutes change the outcome" heuristic, is every SUBSEQUENT occurrence of an ALREADY-open
  cluster -- Operator already has that exact question pending; a 10th identical arrival doesn't need a
  fresh ping, it needs its count incremented on the item that's already visible.

§25.6 boundary, load-bearing, do not weaken: this module clusters and batches CURRENTLY-PENDING,
live escalations only. A cluster is a TTL'd, resumable queue item, cleared once resolved -- it is
NEVER a durable, cross-session "learned rule" that makes a FUTURE, different task silently skip
the ambiguity check because a past one resolved similarly. The next task to hit this exact
category, after the cluster clears, starts a fresh cluster and is evaluated by
`extract_ambiguity_report()` exactly as before -- nothing here teaches the gate anything or
changes its future behavior. See `test_ambiguity_digest.py`'s own regression test for this.
"""

from __future__ import annotations

import hashlib
import json
import time

import redis

from infra.redis_client import get_redis_client

_CLUSTER_KEY = "oma:pending_ambiguity_cluster:{signature}"
_CLUSTER_INDEX_KEY = "oma:pending_ambiguity_cluster:index"
# Matches manager/escalations.py's own TTL choice -- a cluster that sits unresolved for a full day
# without a fresh occurrence naturally expires rather than lingering forever; a later, genuinely
# new occurrence of the same category after that starts a fresh cluster, never resurrects a stale
# one (§25.6: no durable persistence past what's needed for the live, in-flight queue).
_CLUSTER_TTL_SEC = 86_400


def _signature_for_questions(blocking_questions: list[str]) -> str:
    """Reuses this codebase's own real, already-tested normalization (scripts/rule_backlog_
    triage.py's normalize_summary(), already imported the same lazy way elsewhere -- see
    manager/learning.py:173) so two occurrences of the same underlying ambiguity category
    (identical or near-identical blocking_questions text) cluster together, the same way
    `cluster_rows()` already clusters proposed-rule rows by normalized shape.
    """
    from scripts.rule_backlog_triage import normalize_summary

    joined = " ".join(blocking_questions)
    normalized = normalize_summary(joined)
    return hashlib.sha256(normalized.encode()).hexdigest()[:16]


def record_ambiguity_pause(
    task_id: str,
    goal: str,
    blocking_questions: list[str],
    reasoning: str,
    client: redis.Redis | None = None,
) -> dict:
    """Records a real `blocking_ambiguity` pause into the category-clustered pending queue.
    Returns the (possibly newly-created) cluster dict, plus `is_new_cluster` -- the caller (manager/
    loop.py) uses this to decide whether this occurrence needs immediate surfacing (a genuinely new
    category) or was absorbed into an already-open cluster (a duplicate arrival, low-urgency by
    the §25.5 heuristic -- Operator already has this question pending).
    """
    r = client or get_redis_client()
    signature = _signature_for_questions(blocking_questions)
    key = _CLUSTER_KEY.format(signature=signature)
    raw = r.get(key)
    now = time.time()

    if raw is None:
        cluster = {
            "signature": signature,
            "blocking_questions": blocking_questions,
            "reasoning": reasoning,
            "sample_goal": goal,
            "task_ids": [task_id],
            "count": 1,
            "first_seen": now,
            "last_seen": now,
        }
        is_new_cluster = True
    else:
        cluster = json.loads(raw)
        if task_id not in cluster["task_ids"]:
            cluster["task_ids"].append(task_id)
        cluster["count"] += 1
        cluster["last_seen"] = now
        is_new_cluster = False

    r.set(key, json.dumps(cluster), ex=_CLUSTER_TTL_SEC)
    r.sadd(_CLUSTER_INDEX_KEY, signature)
    return {**cluster, "is_new_cluster": is_new_cluster}


def list_pending_ambiguity_clusters(client: redis.Redis | None = None) -> list[dict]:
    """Mirrors manager/escalations.py's list_pending_escalations() exactly -- the same read
    shape /api/pending already consumes for its other two pending-item lists (sign-offs,
    escalations), so a caller can fold this in as a third list with no new client-side pattern.
    """
    r = client or get_redis_client()
    signatures = list(r.smembers(_CLUSTER_INDEX_KEY) or [])
    results = []
    for signature in signatures:
        raw = r.get(_CLUSTER_KEY.format(signature=signature))
        if raw is None:
            r.srem(_CLUSTER_INDEX_KEY, signature)
            continue
        results.append(json.loads(raw))
    return results


def clear_ambiguity_cluster(blocking_questions: list[str], client: redis.Redis | None = None) -> None:
    """Clears a resolved cluster -- e.g. once Operator answers it. The NEXT task to hit this exact
    category afterward starts a genuinely fresh cluster (is_new_cluster=True again), per §25.6:
    this is queue hygiene for a live, in-flight escalation, never a durable memory of the answer
    itself.
    """
    r = client or get_redis_client()
    signature = _signature_for_questions(blocking_questions)
    r.delete(_CLUSTER_KEY.format(signature=signature))
    r.srem(_CLUSTER_INDEX_KEY, signature)


def format_ambiguity_digest_line(cluster: dict) -> str:
    """A human-readable one-line digest summary for a cluster -- e.g. for whoever's watching
    /api/pending to see "12 tasks waiting on the same question" instead of scrolling 12 separate
    identical entries. Pure formatting, no side effects.
    """
    n = cluster["count"]
    task_word = "task" if n == 1 else "tasks"
    questions = "; ".join(cluster["blocking_questions"])
    return f"{n} {task_word} waiting on: {questions}"
