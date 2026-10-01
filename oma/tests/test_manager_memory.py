"""Phase 3 tests: ContextSelector/ContextFormatter (fake inputs, no DB
needed) and read_project_memory() (real Postgres, self-cleaning).
"""

import os
import sys
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import psycopg2

from infra.settings import load_postgres_settings
from manager.memory import (
    ContextFormatter,
    ContextSelector,
    MemoryRow,
    read_project_memory,
)


def test_selector_keeps_within_budget_and_flags_exhaustion():
    rows = [
        MemoryRow(event_type="rule", module="account.move", tags=["financial"],
                  actor="operator", summary="x" * 100, verified=True)
        for _ in range(50)
    ]
    # Tiny budget -- only a handful of rows should fit.
    selector = ContextSelector(memory_token_budget=200, history_token_budget=8000)
    ctx = selector.select(candidate_rows=rows, history=[])
    assert len(ctx.memory_rows) < len(rows), "expected budget to cut off before all rows fit"
    assert ctx.budget_exhausted is True
    print(f"PASS: selector kept {len(ctx.memory_rows)}/{len(rows)} rows, flagged budget_exhausted")


def test_selector_no_query_vs_empty_query_are_distinguishable():
    selector = ContextSelector()
    ctx_not_queried = selector.select(candidate_rows=[], history=[], memory_query_ran=False)
    ctx_empty_result = selector.select(candidate_rows=[], history=[], memory_query_ran=True)

    formatter = ContextFormatter()
    text_not_queried = formatter.format_system_block(ctx_not_queried)
    text_empty = formatter.format_system_block(ctx_empty_result)

    assert text_not_queried != text_empty, "these two situations must never render identically"
    assert "not queried" in text_not_queried
    assert "no relevant project memory found" in text_empty
    print("PASS: 'memory not queried' and 'memory queried, found nothing' render distinctly")


def test_history_trimmed_backward_from_most_recent():
    history = [
        {"role": "user", "content": f"message number {i}, padding " + "x" * 50}
        for i in range(30)
    ]
    selector = ContextSelector(memory_token_budget=4000, history_token_budget=300)
    ctx = selector.select(candidate_rows=[], history=history, memory_query_ran=True)
    assert len(ctx.history) < len(history), "expected history to be trimmed"
    # Must keep the MOST RECENT turns, in chronological order.
    kept_contents = [t.content for t in ctx.history]
    assert "message number 29" in kept_contents[-1], "most recent turn must be kept, last in order"
    assert ctx.budget_exhausted is True
    print(f"PASS: history trimmed to {len(ctx.history)}/{len(history)} turns, most-recent-first, "
          f"chronological order restored")


def test_formatter_renders_memory_and_history_distinctly():
    rows = [
        MemoryRow(event_type="rule", module="account.move", tags=["financial"],
                  actor="operator", summary="Never touch discount rounding without asking", verified=True),
    ]
    history = [{"role": "user", "content": "Add a field to contacts"},
               {"role": "assistant", "content": "Done, tier 2, notifying you now."}]
    selector = ContextSelector()
    ctx = selector.select(candidate_rows=rows, history=history, memory_query_ran=True)
    formatter = ContextFormatter()
    text = formatter.format_system_block(ctx)
    assert "Project Memory" in text
    assert "discount rounding" in text
    assert "Conversation History" in text
    assert "Operator:" in text and "Manager:" in text
    print("PASS: formatter renders both memory and history sections, correctly attributed")


def test_read_project_memory_against_real_postgres():
    s = load_postgres_settings()
    conn = psycopg2.connect(host=s.host, port=s.port, dbname=s.db, user=s.user, password=s.password)
    conn.autocommit = True
    cur = conn.cursor()
    marker = f"phase3-test-{uuid.uuid4().hex[:8]}"
    cur.execute(
        "DELETE FROM agent_memory_events WHERE module = %s", (marker,)
    )
    cur.execute(
        """
        INSERT INTO agent_memory_events (event_type, module, tags, actor, summary, verified)
        VALUES ('rule', %s, ARRAY['test-tag'], 'operator', 'A real rule row for phase 3 testing', true)
        """,
        (marker,),
    )
    cur.execute(
        """
        INSERT INTO agent_memory_events (event_type, module, tags, actor, summary, verified, active)
        VALUES ('rule', %s, ARRAY['test-tag'], 'operator', 'An inactive row that must not come back', false, false)
        """,
        (marker,),
    )
    cur.close()
    conn.close()

    rows = read_project_memory(tags=["test-tag"])
    matching = [r for r in rows if r.module == marker]
    assert len(matching) == 1, f"expected exactly 1 active row for {marker}, got {len(matching)}"
    assert matching[0].summary == "A real rule row for phase 3 testing"
    print(f"PASS: read_project_memory() returned exactly the 1 active row for {marker}, "
          f"correctly excluded the inactive one")

    # cleanup
    conn = psycopg2.connect(host=s.host, port=s.port, dbname=s.db, user=s.user, password=s.password)
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("DELETE FROM agent_memory_events WHERE module = %s", (marker,))
    cur.close()
    conn.close()


if __name__ == "__main__":
    test_selector_keeps_within_budget_and_flags_exhaustion()
    test_selector_no_query_vs_empty_query_are_distinguishable()
    test_history_trimmed_backward_from_most_recent()
    test_formatter_renders_memory_and_history_distinctly()
    test_read_project_memory_against_real_postgres()
    print("\nALL MANAGER MEMORY TESTS PASSED")
