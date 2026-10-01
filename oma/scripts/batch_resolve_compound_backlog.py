"""Phase 29A/B batch driver (2026-07-29): works through EVERY remaining
compound-narrative backlog cluster in one run, split into two real
passes:

  Pass 1 (fast, cheap, reliable): for each cluster, rank the existing
  validator catalog by keyword overlap. If the top candidate's overlap
  is strong, print it for HUMAN verification against real source before
  anything is superseded -- this script never auto-supersedes anything
  itself; matching still requires a human reading the validator's own
  code, per this project's own established discipline (an earlier
  automated matching pass produced real false negatives/positives).

  Pass 2 (expensive, AI-driven): for clusters with no strong existing-
  code candidate, runs the real draft -> self-test -> genericity/
  relevance gate pipeline (scripts/draft_validator_from_cluster.py).
  Passing drafts are written to contracts/pending_validators/ for
  human review + promotion (scripts/promote_pending_validator.py) --
  never auto-merged.

Writes a full JSON report to the given output path so a human (or a
follow-up script) can process the results without re-running anything.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.draft_validator_from_cluster import (  # noqa: E402
    draft_validator, self_test, genericity_gate, write_pending_validator_files,
)
from scripts.rule_backlog_triage import (  # noqa: E402
    build_validator_catalog, fetch_compound_narrative_rows, decompose_compound_narrative,
    cluster_rows, normalize_summary,
)

_STRONG_MATCH_THRESHOLD = 3  # shared keyword count considered worth a human look


def _keyword_candidates(cluster_sig: str, catalog: list[tuple[str, str, str]]) -> list[tuple[int, str, str, str]]:
    import re
    words = set(re.findall(r"[a-z_]{4,}", cluster_sig.lower()))
    scored = []
    for prefix, name, doc in catalog:
        doc_words = set(re.findall(r"[a-z_]{4,}", (name + " " + doc).lower()))
        score = len(words & doc_words)
        if score > 0:
            scored.append((score, prefix, name, doc))
    scored.sort(key=lambda t: -t[0])
    return scored[:3]


def main() -> None:
    out_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("batch_report.json")

    rows = fetch_compound_narrative_rows()
    atomic_claims = []
    for r in rows:
        for c in decompose_compound_narrative(r["summary"]):
            atomic_claims.append({"id": r["id"], "summary": c})
    clusters_dict = cluster_rows(atomic_claims)
    clusters = [
        {"sig": sig, "count": c["count"], "ids": c["ids"], "sample_summary": c["sample_summary"]}
        for sig, c in sorted(clusters_dict.items(), key=lambda kv: -kv[1]["count"])
    ]
    print(f"Total distinct claim-shape clusters to process: {len(clusters)}", flush=True)

    catalog = build_validator_catalog(Path(__file__).resolve().parent.parent)

    needs_human_verification = []
    drafted_and_passed = []
    gave_up = []

    for i, cluster in enumerate(clusters):
        print(f"\n[{i+1}/{len(clusters)}] {cluster['sig'][:90]} (n={cluster['count']})", flush=True)
        candidates = _keyword_candidates(cluster["sig"], catalog)
        top_score = candidates[0][0] if candidates else 0

        if top_score >= _STRONG_MATCH_THRESHOLD:
            print(f"  Candidate match (score={top_score}): {candidates[0][2]} -- flagging for human verification, not auto-superseding", flush=True)
            needs_human_verification.append({
                "cluster_sig": cluster["sig"], "count": cluster["count"], "ids": cluster["ids"],
                "sample_summary": cluster["sample_summary"],
                "candidates": [{"score": s, "spec": p, "name": n, "doc": d} for s, p, n, d in candidates],
            })
            continue

        print("  No strong existing-code candidate -- running the real draft/test/gate pipeline...", flush=True)
        real_instances = [cluster["sample_summary"]] * min(cluster["count"], 3)
        success = False
        for attempt in range(1, 3):
            code, examples = draft_validator(cluster, catalog, real_instances)
            if not code:
                print(f"    attempt {attempt}: no code extracted", flush=True)
                continue
            test_ok, test_msg, test_code = self_test(code, cluster, real_instances)
            print(f"    attempt {attempt}: self-test {'PASS' if test_ok else 'FAIL'} -- {test_msg[:150]}", flush=True)
            if not test_ok:
                continue
            gate_ok, gate_msg = genericity_gate(code)
            print(f"    attempt {attempt}: genericity gate {'PASS' if gate_ok else 'FAIL'}", flush=True)
            if not gate_ok:
                continue
            drafted_and_passed.append({
                "cluster_sig": cluster["sig"], "count": cluster["count"], "ids": cluster["ids"],
                "code": code, "test_code": test_code,
                "examples": [[p, n, d] for p, n, d in examples],
            })
            # Real, confirmed gap found live (2026-07-30): this used to
            # only append to the in-memory list/JSON report above,
            # despite this module's own docstring promising drafts are
            # "written to contracts/pending_validators/ for human
            # review" -- a real 141-cluster run completed with 10 real,
            # tested drafts and an empty pending_validators/ directory.
            # write_pending_validator_files() is the same real file-
            # writing step scripts/draft_validator_from_cluster.py's own
            # --cluster mode already uses, factored out so both callers
            # produce the exact same durable artifact.
            out_file_path = write_pending_validator_files(
                cluster["sig"], cluster["ids"], code, test_code, examples, test_msg=test_msg, drafted_attempt=attempt,
            )
            print(f"    SUCCESS -- written to {out_file_path}, queued for human review", flush=True)
            success = True
            break
        if not success:
            gave_up.append({"cluster_sig": cluster["sig"], "count": cluster["count"], "ids": cluster["ids"]})
            print("    gave up after real attempts -- flagged for direct human authoring", flush=True)

        out_path.write_text(json.dumps({
            "needs_human_verification": needs_human_verification,
            "drafted_and_passed": drafted_and_passed,
            "gave_up": gave_up,
            "processed_so_far": i + 1,
            "total_clusters": len(clusters),
        }, indent=2))

    print(f"\n=== DONE: {len(needs_human_verification)} need human verification, "
          f"{len(drafted_and_passed)} drafted+tested+gated, {len(gave_up)} gave up ===", flush=True)


if __name__ == "__main__":
    main()
