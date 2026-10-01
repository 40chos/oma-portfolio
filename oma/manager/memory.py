"""ContextSelector + ContextFormatter, and read_project_memory() --
Phase 3 of the build plan, mirroring Nexo's real, verified split
(agents/src/nexo/context/assembler.py + context/formatter.py) rather
than an invented shape.

Nexo's assembler decides *what fits* in a token budget (greedy fill,
sorted by priority, a per-source diversity cap, history trimmed by
walking backward from the most recent turn) and returns a plain
dataclass; a separate formatter turns that dataclass into the actual
system-prompt text. We keep that same two-piece split, adapted to our
own inputs: agent_memory_events rows (from read_project_memory())
instead of Nexo's RAG evidence, and a plain conversation-history list
instead of Nexo's retrieval-evidence-and-RAG-history.

Per §2.1's correction: Nexo's own `is_partial`/`gap_description` fields
report whether upstream *retrieval* was incomplete, not whether the
selector itself dropped something for budget reasons. Nexo does not
give that signal for free -- so `budget_exhausted` below is a genuine
addition, not something copied from Nexo, built because the Manager
does need to know whether "no memory found" and "memory was found but
didn't fit" are different situations.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import psycopg2
import psycopg2.extras

from infra.settings import load_postgres_settings

# Nexo's own real token-estimation heuristic: UTF-8 byte length // 3,
# not char count // 4 -- accurate to within ~15% for mixed-language
# text without needing a real tokenizer call, per
# agents/src/nexo/context/assembler.py's own docstring.
def estimate_tokens(text: str) -> int:
    return len(text.encode("utf-8")) // 3 + 1


@dataclass(frozen=True)
class MemoryRow:
    """One row read back from agent_memory_events (or the
    active_agent_rules view). Only the fields the selector/formatter
    actually need -- not the full table schema.
    """
    event_type: str
    module: str | None
    tags: list[str]
    actor: str
    summary: str
    verified: bool


@dataclass(frozen=True)
class ConversationTurn:
    role: str  # "user" (Operator) or "assistant" (Manager)
    content: str
    token_estimate: int


@dataclass
class AssembledContext:
    memory_rows: list[MemoryRow] = field(default_factory=list)
    memory_tokens_used: int = 0
    history: list[ConversationTurn] = field(default_factory=list)
    history_tokens_used: int = 0
    # True if a memory query actually ran and came back with zero rows --
    # distinct from "no query ran at all" so the Manager never has to
    # guess whether "no memory found" and "memory wasn't checked" look
    # the same. They must never look the same.
    memory_query_ran: bool = False
    # A genuine addition beyond Nexo's own signal (§2.1's correction):
    # true if there were more candidate memory rows or history turns
    # than fit in budget, i.e. something was actually dropped here, not
    # just "retrieval came back incomplete upstream."
    budget_exhausted: bool = False

    @property
    def total_tokens_estimated(self) -> int:
        return self.memory_tokens_used + self.history_tokens_used


class ContextSelector:
    """Decides what fits in the token budget. Does not touch text
    formatting at all -- that's ContextFormatter's job, kept separate
    so each half is independently testable, mirroring Nexo's real split.
    """

    def __init__(self, memory_token_budget: int = 4000, history_token_budget: int = 8000):
        self.memory_token_budget = memory_token_budget
        self.history_token_budget = history_token_budget

    def select(
        self,
        candidate_rows: list[MemoryRow],
        history: list[dict],
        memory_query_ran: bool = True,
    ) -> AssembledContext:
        budget_exhausted = False

        selected_rows: list[MemoryRow] = []
        used = 0
        for row in candidate_rows:
            row_tokens = estimate_tokens(row.summary)
            if used + row_tokens > self.memory_token_budget:
                budget_exhausted = True
                break
            used += row_tokens
            selected_rows.append(row)

        # Walk backward from the most recent turn, same as Nexo's real
        # _trim_history -- keep the most recent turns first, then
        # restore chronological order.
        kept_turns: list[ConversationTurn] = []
        history_used = 0
        for turn in reversed(history):
            content = str(turn.get("content", ""))
            role = str(turn.get("role", "user"))
            tokens = estimate_tokens(content)
            if history_used + tokens > self.history_token_budget:
                if history:  # there was at least one candidate turn we couldn't fit
                    budget_exhausted = True
                break
            history_used += tokens
            kept_turns.append(ConversationTurn(role=role, content=content, token_estimate=tokens))
        kept_turns.reverse()

        return AssembledContext(
            memory_rows=selected_rows,
            memory_tokens_used=used,
            history=kept_turns,
            history_tokens_used=history_used,
            memory_query_ran=memory_query_ran,
            budget_exhausted=budget_exhausted,
        )


_ROLE_LABEL = {"user": "Operator", "assistant": "Manager"}


class ContextFormatter:
    """Stateless -- pure functions of an AssembledContext, mirroring
    Nexo's real ContextFormatter.
    """

    def format_system_block(self, ctx: AssembledContext) -> str:
        parts = []
        memory_block = self._format_memory(ctx)
        if memory_block:
            parts.append(memory_block)
        history_block = self._format_history(ctx.history)
        if history_block:
            parts.append(history_block)
        return "\n\n---\n\n".join(parts)

    @staticmethod
    def _format_memory(ctx: AssembledContext) -> str:
        if not ctx.memory_rows:
            if not ctx.memory_query_ran:
                # Must never look the same as "queried, found nothing."
                return (
                    "## Project Memory\n\n"
                    "[NOTE: project memory was not queried this turn.]"
                )
            note = (
                "\n\n⚠ Some relevant memory may not have fit in the context budget."
                if ctx.budget_exhausted
                else ""
            )
            return (
                "## Project Memory\n\n"
                "[NOTE: no relevant project memory found for this query.]" + note
            )

        lines = ["## Project Memory\n"]
        for row in ctx.memory_rows:
            verified_flag = " ✓" if row.verified else ""
            module_part = f" [{row.module}]" if row.module else ""
            lines.append(f"- ({row.event_type}{module_part}, {row.actor}{verified_flag}) {row.summary}")
        if ctx.budget_exhausted:
            lines.append(
                "\n⚠ Additional relevant memory exists but did not fit in this turn's budget."
            )
        return "\n".join(lines)

    @staticmethod
    def _format_history(history: list[ConversationTurn]) -> str:
        if not history:
            return ""
        lines = ["## Conversation History\n"]
        for turn in history:
            label = _ROLE_LABEL.get(turn.role, turn.role.capitalize())
            content = turn.content.strip()
            if "\n" in content:
                indented = "\n  ".join(content.splitlines())
                lines.append(f"**{label}:** {indented}\n")
            else:
                lines.append(f"**{label}:** {content}\n")
        return "\n".join(lines)


def read_project_memory(tags: list[str] | None = None, limit: int = 50) -> list[MemoryRow]:
    """Plain, parameterized SQL query against active_agent_rules and the
    base agent_memory_events table (active=true), no LLM call involved
    at all -- exactly per the technical document's §6.
    """
    s = load_postgres_settings()
    conn = psycopg2.connect(
        host=s.host, port=s.port, dbname=s.db, user=s.user, password=s.password
    )
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        if tags:
            cur.execute(
                """
                SELECT event_type, module, tags, actor, summary, verified
                FROM agent_memory_events
                WHERE active = true
                  AND tags && %s
                ORDER BY created_at DESC
                LIMIT %s
                """,
                (tags, limit),
            )
        else:
            cur.execute(
                """
                SELECT event_type, module, tags, actor, summary, verified
                FROM agent_memory_events
                WHERE active = true
                ORDER BY created_at DESC
                LIMIT %s
                """,
                (limit,),
            )
        rows = cur.fetchall()
        cur.close()
    finally:
        conn.close()

    return [
        MemoryRow(
            event_type=r["event_type"],
            module=r["module"],
            tags=list(r["tags"] or []),
            actor=r["actor"],
            summary=r["summary"],
            verified=r["verified"],
        )
        for r in rows
    ]


# Real, general bug found live (2026-08-07, HUMAN_DECISION deep-push, confirmed on both task019
# and task039): matches the exact `f"...Code-Review found {n} blocking issue(s): {explanation}"`
# shape manager/tools.py's own await_verification()/fold_code_review_findings() writes into a
# round's own outcome summary -- the ONE clause in an otherwise-often-reliable summary that is
# Code-Review's own unverified LLM judgment, not a deterministic fact. Runs to the end of the
# string (greedy) since any trailing "[Arbitration: ...]" boilerplate that follows it is about the
# arbitration PROCESS, not a real finding of its own, and isn't independently useful either.
_UNVERIFIED_CODE_REVIEW_VERDICT_CLAUSE_RE = re.compile(
    r"\s*Code-Review found \d+ blocking issue\(s\):.*$", re.DOTALL,
)


def _strip_unverified_code_review_verdict_clause(summary: str | None) -> str:
    """Removes just the Code-Review-verdict clause from a summary, keeping any real,
    deterministic signal that came before it (e.g. Testing/QA's own "Reproduction FAILED ...
    Spot-check found a real, unclaimed gap" -- confirmed live this is a genuinely different,
    reliable check against real Postgres/schema state, not the same class of unverified judgment).
    Returns "" (never None) so callers can filter on truthiness directly.
    """
    if not summary:
        return ""
    return _UNVERIFIED_CODE_REVIEW_VERDICT_CLAUSE_RE.sub("", summary).strip()


def derive_known_risk_hint(
    memory_rows: list[MemoryRow], module_identity: str | None, limit: int = 2,
) -> str | None:
    """P13 item 6 (docs/planning/PHASE30_OMA_SEAM_COVERAGE_AND_COMBINATORIAL_HARDENING_2026-07-29.md
    §22.2): the deterministic, zero-LLM half of the new "why is this task likely to go wrong"
    task-spec-enrichment element. Reuses `memory_rows` already fetched once by the caller (no new
    DB query -- this is a pure, synchronous filter over data already in hand) and the exact same
    `event_type == "outcome" and "failed" in tags` shape `manager/learning.py`'s
    `check_repeated_failures()` already queries for real, confirmed prior-failure rows -- not a
    second, competing definition of "a real failure record."

    Returns None (never a guessed/invented hint) when `module_identity` is unset or no matching
    failed-outcome row exists for it -- every consumer treats that identically to "no hint
    available." The exact retrieval mechanism beyond this exact-module-match filter (e.g. matching
    by pattern/constraint shape rather than only module identity) is explicitly NOT designed here
    -- flagged open by item 6's own source (P13 item 21) -- this is deliberately the narrow, safe
    subset: real matches only, never a fuzzy/inferred one.
    """
    if not module_identity:
        return None
    matches = [
        row for row in memory_rows
        if row.module == module_identity and row.event_type == "outcome" and "failed" in row.tags
    ]
    if not matches:
        return None
    # Real, general bug found live (2026-08-07, HUMAN_DECISION deep-push, confirmed independently
    # on BOTH task039 and task019, real task_ids d15e3e66-... and d7e7f077-...): every "outcome"
    # row tagged "failed" is written with verified=False (manager/loop.py's own escalation-outcome
    # write, the only code path producing this shape) -- it is, by construction, never more than a
    # SPECIALIST'S OWN self-report, not an independently-confirmed fact. This mechanism was
    # already shown, live, to be wrong: Code-Review flagged "field/method X is missing" as
    # blocking on rounds where the field/method was later independently reproduced. When that
    # exact wrong verdict text (`f"...Code-Review found {n} blocking issue(s): {explanation}"`,
    # manager/tools.py's own await_verification() shape) gets folded back into a LATER round's own
    # goal_text as "risk history worth being aware of" -- confirmed live via redis, on a round
    # where the flagged constraint wasn't even in scope yet -- it reads as settled fact repeating
    # the SAME specialist's own past mistake, not a caveat.
    #
    # Deliberately surgical, not a blanket row-drop: a real summary is often a COMBINED string --
    # e.g. "Reproduction FAILED for X. Spot-check found a real, unclaimed gap. Code-Review found 3
    # blocking issue(s): ..." -- confirmed live in agent_memory_events (182-row sweep, 2026-08-07).
    # The FIRST part (Testing/QA's own deterministic reproduction/spot-check against real
    # Postgres/schema state) is exactly the reliable signal this mechanism was built to surface;
    # only the Code-Review-verdict clause appended after it is the unreliable, unverified-judgment
    # part. Stripping just that clause (rather than discarding the whole row) keeps the real
    # signal while still removing the specific, twice-confirmed poisoning shape.
    stripped_summaries = [
        _strip_unverified_code_review_verdict_clause(row.summary)
        for row in matches[:limit]
    ]
    summaries = [s for s in stripped_summaries if s]
    if not summaries:
        return None
    joined = "; ".join(summaries)
    return (
        f"This module ({module_identity}) has a real prior-failure history worth being aware of "
        f"(these are past, unverified specialist self-reports -- possibly incorrect, not settled "
        f"facts -- weigh them as context, not as a conclusion to defer to): {joined}"
    )
