"""Phase 29A (2026-07-29): the real, checked-in backlog-triage tool --
clears the 637-item `proposed_rule` backlog honestly, not by rejecting,
by recognizing that most of it is ALREADY covered by existing
`_validate_*`/`_autofix_*` code and was simply never marked closed.

Real, confirmed finding this tool is built to act on: `agent_memory_
events` had 637 `proposed` rules, 109 `rejected`, only 2 ever
`active` -- and even an `active` rule never becomes code, it only
ever gets re-injected as prose into a future prompt
(`manager/memory.py:read_project_memory()`). Cross-referencing a
normalized-and-clustered version of the 637 rows against the ~73
existing validator functions' own docstrings found the large majority
of the backlog's TOP shapes are already, independently covered --
built the same way every fix in this codebase is built, by someone
investigating one real failure by hand. The proposed-rule pipeline and
the hand-built-validator pipeline never talked to each other; this
tool is that missing connection.

Three real steps, run in this order:
  1. normalize + cluster the backlog by structural shape (task-specific
     identifiers stripped), separating compound round-outcome
     narratives ("Reproduction confirmed for X... Code-Review found N
     blocking issues") from single atomic claims -- forcing the two
     into the same matching question produced unreliable, low-
     confidence guesses during this tool's own prototyping.
  2. classify each atomic-claim cluster against the real, current
     validator catalog (extracted live from source, never hand-
     maintained prose that could drift out of sync).
  3. NEVER trust step 2's raw answer alone -- every proposed supersede
     match must be independently verified (by direct reasoning against
     the validator's own real code and several real historical
     instances from the cluster) before `supersede_proposed_rule()` is
     ever called. Per this project's own "never trust ungrounded LLM
     output blindly" discipline, applied throughout Phase 28C.

Usage:
    python3 scripts/rule_backlog_triage.py --dry-run     # cluster + classify only, write nothing
    python3 scripts/rule_backlog_triage.py --supersede <cluster_index> <validator_name>
        # after manual verification, mark one cluster's rows superseded
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_QUOTED_RE = re.compile(r"'[^']*'")
_DQUOTED_RE = re.compile(r'"[^"]*"')
_BRACKETED_RE = re.compile(r"\[.*?\]")
_UUID_RE = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b")
_NUMBER_RE = re.compile(r"\b\d+\b")
_WHITESPACE_RE = re.compile(r"\s+")

_COMPOUND_NARRATIVE_PREFIXES = ("Reproduction confirmed", "Reproduction FAILED", "Sandbox install failed")

_VALIDATOR_FN_RE = re.compile(
    r'^(?:async )?def (_(?:validate|autofix)_\w+)\(.*?\)(?: -> [\w\[\], .|"\']+)?:\s*\n\s*"""(.*?)"""',
    re.MULTILINE | re.DOTALL,
)

_CATALOG_SOURCE_FILES = [
    ("build", "specialists/build/specialist.py"),
    ("code_review", "specialists/code_review/specialist.py"),
    ("testing_qa", "specialists/testing_qa/specialist.py"),
    ("toolchain", "tools_odoo/module_dev/toolchain.py"),
]


def normalize_summary(summary: str) -> str:
    """Strips this project's own real proposed-rule prefix and every
    task-specific identifier (quoted strings, bracketed lists, UUIDs,
    bare numbers), collapsing whitespace -- so two instances of the
    same underlying claim shape ('model X already exists' for two
    different X values) normalize to the identical string and cluster
    together.
    """
    s = (summary or "").replace("[self-detected, pending confirmation] ", "")
    s = _BRACKETED_RE.sub("[X]", s)
    s = _QUOTED_RE.sub("'X'", s)
    s = _DQUOTED_RE.sub('"X"', s)
    s = _UUID_RE.sub("UUID", s)
    s = _NUMBER_RE.sub("N", s)
    return _WHITESPACE_RE.sub(" ", s).strip()


def is_compound_narrative(summary: str) -> bool:
    """Real, confirmed finding from this tool's own prototyping: a
    round-outcome summary (citing reproduction + spot-check + possibly
    several Code-Review findings all at once) is not one atomic claim,
    and forcing it through the same single-validator matching question
    as a genuinely atomic claim produced unreliable, forced-guess
    answers. Handled as its own bucket (§29A Step 4), never matched
    1:1 against a single validator.
    """
    s = (summary or "").replace("[self-detected, pending confirmation] ", "").lstrip()
    return s.startswith(_COMPOUND_NARRATIVE_PREFIXES)


_COMPOUND_CLAIM_MARKER_RE = re.compile(
    r"Code-Review found \d+ blocking issue\(s\):\s*(.*)$|"
    r"Code-Review finding:\s*(.*)$",
    re.DOTALL,
)


def decompose_compound_narrative(summary: str) -> list[str]:
    """Real, generic structural extraction (2026-07-29) -- closes the
    gap this tool's own docstring left open at Phase 29A time: compound
    round-outcome narratives were bucketed separately and never actually
    resolved, just permanently deferred. Every real compound-narrative
    instance sampled shares the SAME structural shape: a reproduction/
    spot-check preamble, then either "Code-Review found N blocking
    issue(s): claim1; claim2; ..." or "Code-Review finding: claim" --
    the semicolon-separated tail after that marker is the actual list of
    atomic, individually-matchable claims. Purely structural (a marker
    string + a split), never keyed to any one task's own identifiers, so
    it generalizes to every compound row regardless of which task or
    model/field names it mentions.

    Returns [] (not a crash) for a narrative that doesn't contain either
    marker -- e.g. a bare "Sandbox install failed: ..." with no
    Code-Review section at all has no sub-claim to extract, and callers
    must treat an empty list as "nothing here was resolvable this way,"
    not as an error.
    """
    m = _COMPOUND_CLAIM_MARKER_RE.search(summary)
    if not m:
        return []
    tail = (m.group(1) or m.group(2) or "").strip()
    if not tail:
        return []
    claims = [c.strip().rstrip(".") for c in tail.split(";")]
    return [c for c in claims if c]


def cluster_rows(rows: list[dict], signature_chars: int = 110) -> dict[str, dict]:
    """Groups rows by normalized-shape signature. Returns
    {signature: {"ids": [...], "count": N, "sample_summary": str,
    "is_compound_narrative": bool}}, sorted by count descending is the
    caller's own choice (this returns an unordered dict; callers that
    want top-first order should sort by `count`).
    """
    clusters: dict[str, dict] = defaultdict(lambda: {"ids": [], "count": 0, "sample_summary": None})
    for r in rows:
        summary = r.get("summary") or ""
        sig = normalize_summary(summary)[:signature_chars]
        c = clusters[sig]
        c["ids"].append(r["id"])
        c["count"] += 1
        if c["sample_summary"] is None:
            c["sample_summary"] = summary
        c["is_compound_narrative"] = is_compound_narrative(summary)
    return dict(clusters)


def build_validator_catalog(repo_root: Path) -> list[tuple[str, str, str]]:
    """Extracts every real `_validate_*`/`_autofix_*` function's name
    and first-paragraph docstring directly from source -- never a
    hand-maintained list that could silently drift out of sync with
    what code actually exists. Returns [(source_prefix, function_name,
    first_paragraph), ...].
    """
    catalog: list[tuple[str, str, str]] = []
    for prefix, relpath in _CATALOG_SOURCE_FILES:
        path = repo_root / relpath
        if not path.exists():
            continue
        content = path.read_text()
        for m in _VALIDATOR_FN_RE.finditer(content):
            name, doc = m.group(1), m.group(2)
            first_para = doc.strip().split("\n\n")[0].replace("\n", " ").strip()
            catalog.append((prefix, name, first_para[:200]))
    return catalog


def fetch_proposed_rules() -> list[dict]:
    import psycopg2
    import psycopg2.extras

    from infra.settings import load_postgres_settings

    s = load_postgres_settings()
    conn = psycopg2.connect(host=s.host, port=s.port, dbname=s.db, user=s.user, password=s.password)
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        # Real, confirmed bug found live (2026-07-30, Phase 29 recheck):
        # the `summary` column is hard-truncated to 300 chars at write
        # time (manager/learning.py's append_project_memory() call,
        # `failure_summary[:300]`) -- for any real Code-Review finding
        # list longer than that, `summary` ends mid-word (confirmed
        # live: row 8531's own summary literally ends "...; Missi",
        # silently dropping two of its four real blocking findings).
        # `detail->>'directive'` is the SAME text, written unconditionally
        # (never truncated) alongside it -- COALESCE prefers it and only
        # falls back to `summary` for the rare row that somehow has
        # neither (never actually observed, but conservative).
        cur.execute(
            "SELECT id, COALESCE(NULLIF(detail->>'directive', ''), summary) AS summary, "
            "module, task_id FROM agent_memory_events "
            "WHERE event_type='rule' AND detail->>'status'='proposed'"
        )
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def fetch_compound_narrative_rows() -> list[dict]:
    """Same query as fetch_proposed_rules(), filtered to only the
    compound-narrative subset -- the 92-row bucket the original Phase
    29A pass deliberately deferred rather than resolved (see this
    module's own docstring history / PHASE29 planning doc).
    """
    return [r for r in fetch_proposed_rules() if is_compound_narrative(r["summary"])]


def supersede_compound_rows_with_fully_matched_claims(
    claim_to_validator: dict[str, str], repo_root: Path,
) -> tuple[list[int], list[dict]]:
    """Phase 29A follow-up (2026-07-29): closes the real gap the
    original compound-narrative bucket left open -- those 92 rows sat
    as permanently `proposed`, never triaged, because forcing a WHOLE
    multi-claim narrative through the same single-validator matching
    question as an atomic claim produced unreliable guesses. This
    decomposes each row into its real, individually-verifiable
    sub-claims (decompose_compound_narrative()) and only marks the
    WHOLE row `superseded` when EVERY extracted sub-claim has a
    caller-provided, independently-verified match in `claim_to_validator`
    (keyed by the same normalize_summary() signature used everywhere
    else in this tool). A row with even one unmatched or unextractable
    claim is left untouched -- conservative on purpose, matching this
    tool's own "never force a match" discipline.

    `claim_to_validator` must be built by a human directly reading the
    validator's own source against several real instances first -- this
    function performs no matching itself, only the safe "is this row
    now fully covered" bookkeeping and the actual supersede calls.

    Returns (row_ids_superseded, still_open_rows) where still_open_rows
    is [{"id":, "unmatched_claims": [...]}] for whatever remains.
    """
    from manager.correction import supersede_proposed_rule

    rows = fetch_compound_narrative_rows()
    superseded_ids: list[int] = []
    still_open: list[dict] = []
    for row in rows:
        claims = decompose_compound_narrative(row["summary"])
        if not claims:
            still_open.append({"id": row["id"], "unmatched_claims": ["<no extractable claim>"]})
            continue
        matched_validators: set[str] = set()
        unmatched: list[str] = []
        for claim in claims:
            sig = normalize_summary(claim)
            validator = claim_to_validator.get(sig)
            if validator:
                matched_validators.add(validator)
            else:
                unmatched.append(claim)
        if unmatched:
            still_open.append({"id": row["id"], "unmatched_claims": unmatched})
            continue
        supersede_proposed_rule(row["id"], ", ".join(sorted(matched_validators)))
        superseded_ids.append(row["id"])
    return superseded_ids, still_open


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="cluster + report only, write nothing")
    parser.add_argument("--supersede", nargs=2, metavar=("SIGNATURE_PREFIX", "VALIDATOR_NAME"),
                         help="after manual verification, mark all rows in the cluster matching this "
                              "normalized-signature prefix as superseded by the given validator name")
    parser.add_argument("--decompose-compound", action="store_true",
                         help="decompose all compound-narrative rows into atomic sub-claims and print "
                              "the resulting claim-shape clusters (read-only, same as --dry-run)")
    args = parser.parse_args()

    if args.decompose_compound:
        rows = fetch_compound_narrative_rows()
        atomic_claims = []
        no_claims = 0
        for r in rows:
            claims = decompose_compound_narrative(r["summary"])
            if not claims:
                no_claims += 1
                continue
            atomic_claims.extend({"id": r["id"], "summary": c} for c in claims)
        clusters = cluster_rows(atomic_claims)
        print(f"Compound-narrative rows: {len(rows)} ({no_claims} with no extractable claim)")
        print(f"Extracted atomic claims: {len(atomic_claims)} in {len(clusters)} distinct shape-clusters\n")
        for sig, c in sorted(clusters.items(), key=lambda kv: -kv[1]["count"]):
            print(f"  n={c['count']:3d}  {sig[:100]}")
        return

    repo_root = Path(__file__).resolve().parent.parent
    rows = fetch_proposed_rules()
    clusters = cluster_rows(rows)
    sorted_sigs = sorted(clusters, key=lambda sig: -clusters[sig]["count"])

    if args.supersede:
        sig_prefix, validator_name = args.supersede
        matches = [sig for sig in clusters if sig.startswith(sig_prefix)]
        if not matches:
            print(f"No cluster found starting with {sig_prefix!r}", file=sys.stderr)
            sys.exit(1)
        if len(matches) > 1:
            print(f"Ambiguous prefix {sig_prefix!r} matches {len(matches)} clusters -- be more specific",
                  file=sys.stderr)
            sys.exit(1)
        from manager.correction import supersede_proposed_rule

        ids = clusters[matches[0]]["ids"]
        for row_id in ids:
            supersede_proposed_rule(row_id, validator_name)
        print(f"Marked {len(ids)} row(s) superseded by {validator_name!r}: {ids}")
        return

    print(f"Total proposed rules: {len(rows)}")
    print(f"Distinct shape-clusters: {len(clusters)}")
    compound = sum(c["count"] for c in clusters.values() if c["is_compound_narrative"])
    print(f"Compound round-outcome narratives (handled separately, not auto-matched): {compound} rows")
    print()
    catalog = build_validator_catalog(repo_root)
    print(f"Validator catalog size (extracted live from source): {len(catalog)}")
    print()
    print("Top clusters by count:")
    for sig in sorted_sigs[:30]:
        c = clusters[sig]
        tag = " [COMPOUND]" if c["is_compound_narrative"] else ""
        print(f"  {c['count']:4d}{tag}  {sig[:90]}")

    if not args.dry_run:
        print()
        print("Run with --dry-run to only print this report, or --supersede <sig_prefix> <validator_name> "
              "to mark a cluster's rows superseded after manually verifying the match.")


if __name__ == "__main__":
    main()
