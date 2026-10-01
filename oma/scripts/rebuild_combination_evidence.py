"""P7 (Phase 30, Phase D, §7.5): feeds real pass/fail-per-combination
evidence (from mine_tier1_evidence.py's Tier 1 history-mining and
tier2_structural_check.py's Tier 2 static check) back into
coverage_data.json, adding a real `combination_evidence` list to every leaf
node that has one, alongside its existing `confidence` object -- this is
what finally makes `combination_tested` a real field instead of a
permanent `false` for every node (per coverage_data.json's own
`source_note`, generated 2026-07-28: "'combination_tested' is honestly
false for every single node -- no test in this system currently verifies
two+ capabilities working together, a real, systemic, not per-node gap.").

**Honest graduation discipline (§7.2c/§7.5's own revised criterion,
followed exactly, not loosened for convenience):** `combination_tested`
only becomes `true` for a pairing once BOTH a "simple" and a "complex"
deliberately-chosen instantiation have a real, recorded Tier-3 PASS
(§7.2c). Nothing gathered by this run of Tier 1/Tier 2 meets that bar:
- Tier 1 evidence records real historical tasks that passed, but each pair
  is backed by however many real instances happened to exist in history --
  never explicitly one "simple" and one "complex" instantiation by design,
  so it is recorded as real, genuine evidence with an honest instance
  count, never silently promoted to `combination_tested: true`.
- Tier 2 evidence is a structural-only pass (no LLM generation, no real
  running task) -- real and useful (it rules out a whole class of
  structural conflict cheaply), but not behavioral proof, so it is
  recorded as its own distinct, clearly-labeled evidence kind, also never
  promoted to `combination_tested: true`.

`combination_tested: true` is reserved entirely for a future Tier 3 run
(§7.2c/§7.3, not built in this session) that actually submits the 2
deliberately-chosen instantiations per pair through the real task pipeline
and records two real passed task_ids.

Only pairs where at least one side maps to a real coverage_data.json node
(via dimension_table.json's own `node_id` field) can be attached to this
graph at all -- a pair naming two purely-curated dimension values with no
`node_id` on either side (e.g. `packaging_shape` paired with itself) has
nowhere real to attach and is skipped, reported honestly in the summary,
never silently dropped without a trace.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from paths import COVERAGE_BASE_PATH

DIMENSION_TABLE_PATH = COVERAGE_BASE_PATH / "dimension_table.json"
COVERAGE_DATA_PATH = COVERAGE_BASE_PATH / "coverage_data.json"


def _load_node_id_map(dimension_table_path: Path) -> dict[tuple[str, str], str | None]:
    data = json.loads(dimension_table_path.read_text())
    out: dict[tuple[str, str], str | None] = {}
    for dim_name, spec in data["dimensions"].items():
        for v in spec["values"]:
            out[(dim_name, v["value"])] = v.get("node_id")
    return out


def _iter_leaf_nodes(coverage_data: dict):
    """Yields (node_dict, node_id) for every real leaf node in
    coverage_data.json, across every domain/subgroup.
    """
    for domain in coverage_data["domains"]:
        for subgroup in domain["subgroups"]:
            for node in subgroup["nodes"]:
                yield node, node["id"]


def _parse_pair_label(label: str) -> tuple[str, str, str, str]:
    left, right = label.split(" x ")
    dim_a, val_a = left.split(":", 1)
    dim_b, val_b = right.split(":", 1)
    return dim_a, val_a, dim_b, val_b


def build_evidence_entries(
    tier1_path: Path, tier2_path: Path, node_id_map: dict[tuple[str, str], str | None]
) -> tuple[dict[str, list[dict]], dict[str, int]]:
    """Returns {node_id: [combination_evidence entry, ...]} plus a small
    summary-stats dict for honest reporting.
    """
    per_node: dict[str, list[dict]] = {}
    stats = {
        "tier1_pairs_attached": 0, "tier1_pairs_skipped_no_node": 0,
        "tier2_pairs_attached": 0, "tier2_pairs_skipped_no_node": 0,
    }

    def attach(dim_a: str, val_a: str, dim_b: str, val_b: str, entry_for_a: dict, entry_for_b: dict) -> bool:
        node_a = node_id_map.get((dim_a, val_a))
        node_b = node_id_map.get((dim_b, val_b))
        attached = False
        if node_a:
            per_node.setdefault(node_a, []).append(entry_for_a)
            attached = True
        if node_b:
            per_node.setdefault(node_b, []).append(entry_for_b)
            attached = True
        return attached

    tier1 = json.loads(tier1_path.read_text())
    for row in tier1["covered_for_free"]:
        dim_a, val_a, dim_b, val_b = _parse_pair_label(row["pair"])
        task_ids = row["evidence_task_ids"]
        entry_a = {
            "paired_with": f"{dim_b}:{val_b}", "kind": "tier1_real_historical_evidence",
            "n_instances": len(task_ids), "instances": [
                {"result": "pass", "task_id": tid} for tid in task_ids
            ],
            "note": (
                "Real historical task(s) that passed and whose real committed code "
                "evidenced both dimension values. NOT the required simple+complex "
                "A/B pair (§7.2c) -- combination_tested stays false until a real "
                "Tier 3 run records two deliberately-chosen instantiations."
            ),
        }
        entry_b = {**entry_a, "paired_with": f"{dim_a}:{val_a}"}
        if attach(dim_a, val_a, dim_b, val_b, entry_a, entry_b):
            stats["tier1_pairs_attached"] += 1
        else:
            stats["tier1_pairs_skipped_no_node"] += 1

    tier2 = json.loads(tier2_path.read_text())
    for row in tier2["results"]:
        if row["result"] != "pass":
            continue
        dim_a, val_a, dim_b, val_b = _parse_pair_label(row["pair"])
        entry_a = {
            "paired_with": f"{dim_b}:{val_b}", "kind": "tier2_structural_check",
            "n_instances": 0, "instances": [],
            "note": (
                "A minimal synthetic snippet exercising both dimension values passed "
                "the real ~60-function structural validator chain (no LLM call, no "
                "sandbox install) -- rules out a class of structural conflict cheaply, "
                "but is not behavioral proof. combination_tested stays false."
            ),
        }
        entry_b = {**entry_a, "paired_with": f"{dim_a}:{val_a}"}
        if attach(dim_a, val_a, dim_b, val_b, entry_a, entry_b):
            stats["tier2_pairs_attached"] += 1
        else:
            stats["tier2_pairs_skipped_no_node"] += 1

    return per_node, stats


def add_tier3_evidence(
    per_node: dict[str, list[dict]], tier3_path: Path | None,
    node_id_map: dict[tuple[str, str], str | None], stats: dict[str, int],
) -> None:
    """Tier 3 (§7.2c/§7.3): real task-pipeline results, the only tier that
    can ever graduate a pairing to `combination_tested: true` -- and only
    once BOTH a simple and complex instantiation have a real, recorded
    PASS. A real FAIL (either instance, or both) is equally real evidence
    and is recorded honestly -- never omitted just because it's bad news;
    that is exactly the case §7.2c calls "the single most valuable output
    this harness can produce."
    """
    if tier3_path is None:
        return
    data = json.loads(tier3_path.read_text())
    for row in data["pairs"]:
        dim_a, val_a, dim_b, val_b = _parse_pair_label(row["pair"])
        both_passed = all(inst["result"] == "pass" for inst in row["instances"]) and len(row["instances"]) >= 2
        entry_a = {
            "paired_with": f"{dim_b}:{val_b}", "kind": "tier3_real_task_pipeline",
            "n_instances": len(row["instances"]), "instances": row["instances"],
            "divergent": row.get("divergent", False),
            "graduated_combination_tested": both_passed,
            "note": row.get("conclusion", ""),
        }
        entry_b = {**entry_a, "paired_with": f"{dim_a}:{val_a}"}
        node_a = node_id_map.get((dim_a, val_a))
        node_b = node_id_map.get((dim_b, val_b))
        if node_a:
            per_node.setdefault(node_a, []).append(entry_a)
        if node_b:
            per_node.setdefault(node_b, []).append(entry_b)
        if node_a or node_b:
            stats["tier3_pairs_attached"] = stats.get("tier3_pairs_attached", 0) + 1
            if both_passed:
                stats["tier3_pairs_graduated"] = stats.get("tier3_pairs_graduated", 0) + 1
        else:
            stats["tier3_pairs_skipped_no_node"] = stats.get("tier3_pairs_skipped_no_node", 0) + 1


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dimension-table", type=Path, default=DIMENSION_TABLE_PATH)
    parser.add_argument("--coverage-data", type=Path, default=COVERAGE_DATA_PATH)
    parser.add_argument("--tier1-evidence", type=Path, required=True)
    parser.add_argument("--tier2-evidence", type=Path, required=True)
    parser.add_argument("--tier3-evidence", type=Path, default=None)
    parser.add_argument("--write", action="store_true", help="Actually write coverage_data.json (default: dry run, prints a summary only)")
    args = parser.parse_args()

    node_id_map = _load_node_id_map(args.dimension_table)
    per_node, stats = build_evidence_entries(args.tier1_evidence, args.tier2_evidence, node_id_map)
    add_tier3_evidence(per_node, args.tier3_evidence, node_id_map, stats)

    coverage_data = json.loads(args.coverage_data.read_text())
    n_nodes_updated = 0
    n_nodes_graduated = 0
    for node, node_id in _iter_leaf_nodes(coverage_data):
        if node_id in per_node:
            node["combination_evidence"] = per_node[node_id]
            # combination_tested flips true only if at least one real Tier 3
            # pairing for this node graduated (both simple+complex passed,
            # §7.2c/§7.5's own bar) -- Tier 1/Tier 2 evidence alone never
            # sets this, no matter how much of it accumulates. Nothing
            # gathered so far meets this bar (the first real Tier 3 batch,
            # 2 pairs, 4 real tasks, all failed) -- left false, honestly.
            if any(e.get("graduated_combination_tested") for e in per_node[node_id]):
                node["combination_tested"] = True
                n_nodes_graduated += 1
            n_nodes_updated += 1

    print(json.dumps({
        "n_distinct_nodes_with_new_evidence": n_nodes_updated,
        "n_nodes_graduated_to_combination_tested_true": n_nodes_graduated,
        **stats,
    }, indent=2))

    if args.write:
        args.coverage_data.write_text(json.dumps(coverage_data, indent=2) + "\n")
        print(f"Wrote {args.coverage_data}")
    else:
        print("(dry run -- pass --write to actually update coverage_data.json)")


if __name__ == "__main__":
    main()
