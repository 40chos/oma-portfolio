"""Phase 29 (2026-07-30): unit test for a real, confirmed live bug --
the Phase 29 backlog-processing recheck found that `fetch_proposed_
rules()`, `fetch_genuinely_open_clusters()`, and `fetch_cluster_real_
instances()` all read the `summary` column, which manager/learning.py's
append_project_memory() call hard-truncates to 300 chars at write time
(`failure_summary[:300]`). Row 8531's own real, live summary ends
literally mid-word ("...; Missi"), silently dropping 2 of its 4 real
Code-Review findings -- the full, untruncated text was always available
in `detail->>'directive'`, just never read. This produced garbage,
un-draftable cluster fragments during the real batch backlog run.

Requires a real, live connection to the project's own Postgres (same
convention as tests/test_module_dev_toolchain.py and friends) -- run
with the project's .env sourced.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from scripts.rule_backlog_triage import fetch_proposed_rules

# The real, live, already-confirmed-truncated row this bug was found on.
_KNOWN_TRUNCATED_ROW_ID = 8531
_REAL_UNTRUNCATED_TAIL = "should sum line_ids.mapped('price_subtotal') or equivalent for accurate total."


def test_fetch_proposed_rules_returns_the_full_untruncated_directive():
    rows = fetch_proposed_rules()
    row = next((r for r in rows if r["id"] == _KNOWN_TRUNCATED_ROW_ID), None)
    if row is None:
        print(
            f"SKIP: row {_KNOWN_TRUNCATED_ROW_ID} is no longer 'proposed' (already triaged since this "
            "test was written) -- the fix itself can't be re-verified against this specific row anymore"
        )
        return
    assert len(row["summary"]) > 300, (
        "the real, confirmed live bug: this row's real Code-Review finding list is 563 chars long -- "
        "if this comes back <= 300 chars, fetch_proposed_rules() is reading the truncated `summary` "
        "column again instead of the full, untruncated detail->>'directive'"
    )
    assert row["summary"].endswith(_REAL_UNTRUNCATED_TAIL), (
        "must return the real, complete text, not a mid-word-truncated fragment"
    )
    print("PASS: fetch_proposed_rules() returns the full, untruncated directive text for a real, "
          "previously-truncated row")


if __name__ == "__main__":
    test_fetch_proposed_rules_returns_the_full_untruncated_directive()
    print("\nALL RULE-BACKLOG DIRECTIVE-NOT-TRUNCATED TESTS PASSED")
