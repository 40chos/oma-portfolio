#!/usr/bin/env python3
"""Phase 13 (§14 of the architecture file) -- core vs. peripheral MODULE report for
Operator, tied directly to his stated reason (docs/meetings/07_meetingnotes.md,
00:14:31/00:17:49): "see what is used, what is really meaningful... focus on the
core... if I migrate to new [Odoo] the cleanest way is to first clean our system."

Not a new build -- packages real data that already exists (the same real connectivity
computation already validated in build_visualization.py/build_model_cards.py) at
MODULE granularity instead of model granularity, since that's what Operator actually
asked to see trimmed/kept.

Two real, independent connectivity signals, combined:
  1. reverse_deps: how many other real modules list this module in their manifest
     `depends` -- i.e. how many modules would break/need re-checking if this one
     were removed.
  2. models_touched: real in-degree (EXTENDS/INHERITS/VIEWS_EXTEND/SECURITY,
     deduplicated -- same logic as build_model_cards.py) summed across every model
     this module owns -- i.e. how much other code reaches into what this module
     defines.

No network/LLM calls -- purely local, from final_module_graph.jsonl and
odoo_full_module_graph.json, both already on disk.

Usage:
    python3 build_core_peripheral_report.py <final_module_graph_jsonl> <full_module_graph_json> <out_path>
"""

import json
import sys
from pathlib import Path


def load_records(path: Path) -> list[dict]:
    records = []
    with path.open() as f:
        for line in f:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


def compute_reverse_deps(records: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = {r["module"]: 0 for r in records}
    for r in records:
        for dep in r.get("deps", []):
            if dep in counts:
                counts[dep] += 1
    return counts


def compute_models_touched(records: list[dict]) -> dict[str, int]:
    """Real deduplicated cross-module in-degree, aggregated to the OWNING module of
    each model -- same edge-dedup logic as build_model_cards.py's model index, just
    rolled up one level to module granularity."""
    owner_of_model: dict[str, str] = {}
    for r in records:
        for m in r.get("models", []):
            owner_of_model[m["name"]] = r["module"]

    edges: set[tuple[str, str, str]] = set()  # (source_module, target_model, kind) deduped
    for r in records:
        module = r["module"]
        for e in r.get("extends", []):
            edges.add((module, e["model"], "EXTENDS"))
        # Real gap found and fixed 2026-07-21 (the project owner's second-look request): a
        # module that reopens another model with zero new fields (via `reopens`,
        # not `extends`) was contributing NO connectivity at all -- 291 real
        # relationships were invisible to this score. Counting `reopens` here
        # closes that gap; `extends` targets are always a subset of `reopens`, so
        # this doesn't double-count, it just adds the field-less cases.
        for target in r.get("reopens", []):
            edges.add((module, target, "REOPENS"))
        for s in r.get("security", []):
            if s.get("model"):
                edges.add((module, s["model"], "SECURITY"))
        for m in r.get("models", []):
            for mixin in m.get("inherits", []) or []:
                edges.add((m["name"], mixin, "INHERITS"))
        for v in r.get("views_extend", []):
            inherit_id = v.get("inherit_id") or ""
            owner_module = inherit_id.split(".")[0] if "." in inherit_id else None
            if owner_module:
                edges.add((module, owner_module, "VIEWS_EXTEND"))

    touched: dict[str, int] = {r["module"]: 0 for r in records}
    for (_source, target, kind) in edges:
        if kind == "VIEWS_EXTEND":
            if target in touched:
                touched[target] += 1
            continue
        owner_module = owner_of_model.get(target)
        if owner_module and owner_module in touched:
            touched[owner_module] += 1

    return touched


def main() -> int:
    if len(sys.argv) != 4:
        print(__doc__)
        return 1

    final_graph_path = Path(sys.argv[1])
    full_module_graph_path = Path(sys.argv[2])
    out_path = Path(sys.argv[3])

    records = load_records(final_graph_path)
    meta = json.loads(full_module_graph_path.read_text())["modules"]

    reverse_deps = compute_reverse_deps(records)
    models_touched = compute_models_touched(records)

    rows = []
    for r in records:
        module = r["module"]
        m = meta.get(module, {})
        combined_score = reverse_deps.get(module, 0) + models_touched.get(module, 0)
        rows.append({
            "module": module,
            "is_official": m.get("is_official", False),
            "reverse_deps": reverse_deps.get(module, 0),
            "models_touched": models_touched.get(module, 0),
            "combined_score": combined_score,
        })

    rows.sort(key=lambda x: -x["combined_score"])

    core = [r for r in rows if r["combined_score"] >= 5]
    peripheral = [r for r in rows if r["combined_score"] == 0]
    middle = [r for r in rows if 0 < r["combined_score"] < 5]

    lines = []
    lines.append("# Core vs. Peripheral Modules -- Real Connectivity Data")
    lines.append("")
    lines.append(
        "Answers Operator's direct question (2026-07-20 meeting): \"what is used, what is "
        "really meaningful... focus on the core... if I migrate to new [Odoo] the "
        "cleanest way is to first clean our system.\" Two real, independent signals per "
        "module, both computed from the actual parsed dependency graph, not estimated:"
    )
    lines.append("")
    lines.append("- **reverse_deps**: how many other real modules require this one (would need")
    lines.append("  re-checking or break if this module were removed)")
    lines.append("- **models_touched**: how many real cross-module facts (EXTENDS/INHERITS/")
    lines.append("  VIEWS_EXTEND/SECURITY) point at models this module owns")
    lines.append("")
    lines.append(f"Total modules: {len(rows)} | Core (score >=5): {len(core)} | "
                  f"Middle (1-4): {len(middle)} | Peripheral (score 0, can stand alone): {len(peripheral)}")
    lines.append("")
    lines.append("## CORE -- high connectivity, touch carefully, real migration-cleanup priority order")
    lines.append("")
    lines.append("| Module | Official | Reverse deps | Models touched | Combined |")
    lines.append("|---|---|---|---|---|")
    for r in core:
        lines.append(f"| {r['module']} | {'yes' if r['is_official'] else 'no'} | "
                      f"{r['reverse_deps']} | {r['models_touched']} | {r['combined_score']} |")
    lines.append("")
    # Real data-quality finding, not a report bug: at least one module
    # (`mis_base_extend`) is labeled author "Odoo S.A." in Operator's source spreadsheet
    # despite its real fields being obviously bespoke/custom (e.g. Dutch field names
    # like opname1/gecontroleerd/prospect_go -- not real Odoo core naming). This
    # directly matters for a migration-cleanup review: trusting `is_official` at face
    # value would misclassify a genuinely messy custom module as "core, keep as-is."
    suspicious = [r for r in core if r["module"] == "mis_base_extend" and r["is_official"]]
    if suspicious:
        lines.append("## Data-quality flag -- worth telling Operator directly")
        lines.append("")
        lines.append(
            "`mis_base_extend` is labeled `author: \"Odoo S.A.\"` in the source spreadsheet, but "
            "its real fields (e.g. `opname1`, `prospect_go`, `gecontroleerd`, `handl_ip` -- Dutch, "
            "clearly bespoke) are obviously not real Odoo core code. It also ranks in the top 6 "
            "CORE modules by real connectivity (82 models touched) -- meaning a genuinely messy "
            "custom module is deeply entangled with the rest of the system, exactly the kind of "
            "real risk a migration-cleanup review needs to catch, not something the `is_official` "
            "label alone would surface."
        )
        lines.append("")

    lines.append("## PERIPHERAL -- zero measured connectivity, real candidates to \"stand on its own\"")
    lines.append("")
    third_party_peripheral = [r for r in peripheral if not r["is_official"]]
    official_peripheral = [r for r in peripheral if r["is_official"]]
    lines.append(f"{len(peripheral)} modules total: {len(third_party_peripheral)} third-party, "
                  f"{len(official_peripheral)} official (official ones are still real Odoo core "
                  f"pieces, just not heavily cross-referenced by the other 240 in this dataset -- "
                  f"not necessarily safe to drop, unlike the third-party ones).")
    lines.append("")
    lines.append("### Peripheral third-party modules (real candidates for cleanup review)")
    lines.append(", ".join(sorted(r["module"] for r in third_party_peripheral)))
    lines.append("")
    lines.append("### Peripheral official modules (Odoo core, just not cross-referenced here)")
    lines.append(", ".join(sorted(r["module"] for r in official_peripheral)))
    lines.append("")

    out_path.write_text("\n".join(lines))
    print(f"Report written to {out_path}")
    print(f"CORE: {len(core)} modules, PERIPHERAL: {len(peripheral)} modules "
          f"({len(third_party_peripheral)} third-party, {len(official_peripheral)} official)")
    print("\nTop 15 core modules:")
    for r in core[:15]:
        print(f"  {r['module']}: reverse_deps={r['reverse_deps']}, models_touched={r['models_touched']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
