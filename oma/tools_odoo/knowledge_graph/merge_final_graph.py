#!/usr/bin/env python3
"""Merges Stage A's structural facts + Stage B's NOTES + Stage B's review resolutions
into one final structured store per module (§11.8 step 9), plus an explicit
"unresolved -- needs manual research" list (§11.1 point 3, §11.3).

Inputs (all already on disk, no network/LLM calls):
  - stage_a_store_jsonl: the FIXED Stage A output (structural facts + needs_llm_review,
    with the `model` label fix applied) -- /tmp/odoo_all_parsed_v2/store.jsonl. NOT the
    combined_graph.jsonl from the notes run, which predates the model-label fix and would
    silently drop `model` from every review item if used as the structural source.
  - notes_graph_jsonl: Stage B's NOTES output, used ONLY for the `notes` field (real,
    already-paid-for LLM output that doesn't need regenerating) --
    /tmp/stage_b_notes_output/combined_graph.jsonl
  - review_output_dir: per-item review resolution JSON files
    (/tmp/stage_b_review_output_v2/)

Output:
  - <out_dir>/final_module_graph.jsonl -- one record per module: everything from the fixed
    Stage A + Stage B's NOTES, with each needs_llm_review item replaced by either its real
    resolution (folded into `review_resolved`) or kept as an honest unresolved entry.
  - <out_dir>/unresolved_summary.json -- every real STILL_UNCERTAIN item, grouped by
    module, with the model/reason/snippet -- the explicit gap list Operator asked for.

Usage:
    python3 merge_final_graph.py <stage_a_store_jsonl> <notes_graph_jsonl> <review_output_dir> <out_dir>
"""

import hashlib
import json
import sys
from pathlib import Path


def review_item_id(module: str, reason: str, snippet: str) -> str:
    h = hashlib.sha256((module + reason + snippet).encode()).hexdigest()[:12]
    return f"{module}_{h}"


def main() -> int:
    if len(sys.argv) != 5:
        print(__doc__)
        return 1

    stage_a_path = Path(sys.argv[1])
    notes_graph_path = Path(sys.argv[2])
    review_output_dir = Path(sys.argv[3])
    out_dir = Path(sys.argv[4])
    out_dir.mkdir(parents=True, exist_ok=True)

    modules = {}
    with stage_a_path.open() as f:
        for line in f:
            line = line.strip()
            if line:
                rec = json.loads(line)
                modules[rec["module"]] = rec

    with notes_graph_path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            notes_rec = json.loads(line)
            target = modules.get(notes_rec["module"])
            if target is not None:
                target["notes"] = notes_rec.get("notes")

    resolutions_by_id = {}
    for path in review_output_dir.glob("*.json"):
        if path.name.endswith(".log.jsonl"):
            continue
        d = json.loads(path.read_text())
        item_id = review_item_id(d["module"], d["reason"], d["snippet"])
        resolutions_by_id[item_id] = d

    unresolved_by_module = {}
    matched = 0
    unmatched = 0

    for module, rec in modules.items():
        original_flags = rec.get("needs_llm_review", [])
        resolved_extends_additions = []
        remaining_unresolved = []

        for flag in original_flags:
            item_id = review_item_id(module, flag["reason"], flag.get("snippet", ""))
            res = resolutions_by_id.get(item_id)
            if res is None:
                # Flag had no matching review call (e.g. a model-less flag that was
                # still sent, or a genuine gap) -- keep as unresolved, honestly.
                unmatched += 1
                remaining_unresolved.append({**flag, "resolution_status": "not_reviewed"})
                continue

            matched += 1
            resolution_text = res.get("resolution", "")
            if resolution_text.startswith("STILL_UNCERTAIN"):
                remaining_unresolved.append({
                    **flag,
                    "resolution_status": "still_uncertain",
                    "llm_note": resolution_text[len("STILL_UNCERTAIN:"):].strip(),
                })
            else:
                resolved_extends_additions.append({
                    "source": "stage_b_review",
                    "model": flag.get("model"),
                    "reason": flag["reason"],
                    "resolution": resolution_text,
                })

        rec["needs_llm_review"] = remaining_unresolved
        rec["review_resolved"] = resolved_extends_additions

        if remaining_unresolved:
            unresolved_by_module[module] = remaining_unresolved

    final_path = out_dir / "final_module_graph.jsonl"
    with final_path.open("w") as f:
        for module in sorted(modules):
            f.write(json.dumps(modules[module]))
            f.write("\n")

    total_unresolved = sum(len(v) for v in unresolved_by_module.values())
    summary_path = out_dir / "unresolved_summary.json"
    summary_path.write_text(json.dumps({
        "total_unresolved_items": total_unresolved,
        "modules_with_unresolved_items": len(unresolved_by_module),
        "by_module": unresolved_by_module,
    }, indent=2))

    print(f"modules merged: {len(modules)}")
    print(f"review resolutions matched: {matched}, unmatched (kept unresolved): {unmatched}")
    print(f"final graph -> {final_path}")
    print(f"unresolved summary ({total_unresolved} items across {len(unresolved_by_module)} modules) -> {summary_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
