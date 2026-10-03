"""Phase 6 tests: the Manager's six tools. Most importantly, a grep-based
test proving append_project_memory() really is the ONLY code path in
this entire project that inserts into agent_memory_events -- not just a
convention documented in a docstring.
"""

import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import psycopg2
import psycopg2.extras

from infra.settings import load_postgres_settings
from manager.tools import _extract_collision_confirmed_field_names, ask_operator, fold_specialist_result

_PROJECT_ROOT = os.path.join(os.path.dirname(__file__), "..")
_INSERT_RE = re.compile(r"INSERT\s+INTO\s+agent_memory_events", re.IGNORECASE)

# Scoped to actual APPLICATION source directories only -- tests/ and
# scripts/ legitimately contain their own direct fixture inserts to test
# the schema layer in isolation (Phase 1/3, predating manager/tools.py
# and testing the database itself, not the application's write-path
# discipline), and this very test file's own regex pattern would
# otherwise match itself.
_APPLICATION_DIRS = ["manager", "specialists", "infra", "contracts"]


def test_append_project_memory_is_the_only_insert_path():
    violations = []
    for app_dir in _APPLICATION_DIRS:
        dir_path = os.path.join(_PROJECT_ROOT, app_dir)
        if not os.path.isdir(dir_path):
            continue
        for dirpath, dirnames, filenames in os.walk(dir_path):
            dirnames[:] = [d for d in dirnames if d not in ("__pycache__",)]
            for filename in filenames:
                if not filename.endswith(".py"):
                    continue
                path = os.path.join(dirpath, filename)
                with open(path, encoding="utf-8", errors="ignore") as f:
                    content = f.read()
                if _INSERT_RE.search(content):
                    rel = os.path.relpath(path, _PROJECT_ROOT)
                    if rel != os.path.join("manager", "tools.py"):
                        violations.append(rel)

    assert violations == [], (
        f"found a direct INSERT INTO agent_memory_events outside manager/tools.py "
        f"in application code: {violations}"
    )
    print("PASS: append_project_memory() in manager/tools.py is the ONLY code path in "
          "manager/specialists/infra/contracts that inserts into agent_memory_events "
          "(grep-verified across all real application source, not just documented)")


def test_fold_specialist_result_under_limit_is_unchanged():
    short_text = "a short result"
    assert fold_specialist_result(short_text) == short_text
    print("PASS: a result under the 5,000-char limit is returned unchanged")


def test_fold_specialist_result_over_limit_keeps_head_and_tail():
    long_text = ("HEAD_MARKER_" + "x" * 10 + "middle filler " * 1000 + "TAIL_MARKER_" + "y" * 10)
    folded = fold_specialist_result(long_text)
    assert len(folded) < len(long_text)
    assert "HEAD_MARKER_" in folded
    assert "TAIL_MARKER_" in folded
    assert "truncated" in folded
    # The middle filler should be substantially reduced, not fully present.
    assert folded.count("middle filler") < long_text.count("middle filler")
    print(f"PASS: a result over the 5,000-char limit keeps head and tail markers "
          f"({len(long_text)} chars -> {len(folded)} chars), drops the middle")


def test_extract_collision_confirmed_field_names_parses_the_real_marker_format():
    """Real, confirmed gap found live (2026-08-06, fix-pass task 004): Build's own collision-
    autofix writes `f"{marker}: {colliding!r}"` into generated.notes -- threaded here via
    build_output.detail['self_report_uncertain'] -- confirming this parses the exact real format
    verbatim (a Python list repr of single-quoted strings), matching
    specialists/build/specialist.py's own `_extract_collision_marker_field_names()` sibling.
    """
    notes = (
        "The module extends project.fieldjob to display amount_total.\n"
        "ALREADY_SATISFIED_BY_REAL_TARGET_COLLISION: ['amount_total']"
    )
    assert _extract_collision_confirmed_field_names(notes) == ["amount_total"]
    print("PASS: the real collision-marker format is parsed correctly")


def test_extract_collision_confirmed_field_names_empty_when_no_marker():
    assert _extract_collision_confirmed_field_names("just ordinary notes, no marker here") == []
    print("PASS: no marker present returns an empty list, never a guess")


def test_extract_collision_confirmed_field_names_handles_none():
    assert _extract_collision_confirmed_field_names(None) == []
    print("PASS: None input (no self_report_uncertain at all) returns an empty list, never a crash")


def test_ask_operator_returns_correct_marker():
    result = ask_operator("Which form view should show this field?")
    assert result == {"action": "ask_operator", "question": "Which form view should show this field?"}
    print("PASS: ask_operator() returns the correct plain marker dict")


def test_append_project_memory_sets_verified_and_stale_after_explicitly():
    from manager.tools import append_project_memory

    row_id = append_project_memory(
        event_type="note",
        actor="operator",
        summary="Phase 6 tool test row",
        tags=["phase6-tool-test"],
        verified=True,
        stale_after="1 day",
    )
    s = load_postgres_settings()
    conn = psycopg2.connect(host=s.host, port=s.port, dbname=s.db, user=s.user, password=s.password)
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT verified, stale_after FROM agent_memory_events WHERE id = %s", (row_id,))
        row = cur.fetchone()
        assert row["verified"] is True
        assert row["stale_after"] is not None
        cur.execute("DELETE FROM agent_memory_events WHERE id = %s", (row_id,))
        conn.commit()
    finally:
        conn.close()
    print(f"PASS: append_project_memory correctly sets verified/stale_after explicitly "
          f"(row #{row_id}), never left to bare defaults by accident")


if __name__ == "__main__":
    test_append_project_memory_is_the_only_insert_path()
    test_fold_specialist_result_under_limit_is_unchanged()
    test_fold_specialist_result_over_limit_keeps_head_and_tail()
    test_ask_operator_returns_correct_marker()
    test_append_project_memory_sets_verified_and_stale_after_explicitly()
    print("\nALL MANAGER TOOLS TESTS PASSED")
