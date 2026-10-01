#!/usr/bin/env python3
"""Stage 2 port: derives the two artifacts odoo_kg_to_neo4j.py's
load_source_data() needs beyond final_module_graph.jsonl --
`model_cards.jsonl` and `odoo_full_module_graph.json` -- directly and
deterministically from Stage A's own real parsed output (store.jsonl) and
each module's real __manifest__.py, with zero LLM calls.

The original system produced these via a separate LLM-driven annotation
pipeline (build_model_cards.py, not included in this handoff) that wrote
human-readable architectural notes alongside the structural grammar. That
narrative layer is out of scope here -- but direct inspection of
odoo_kg_to_neo4j.py shows its actual ETL only ever regex-parses two lines
out of model_cards.jsonl's "card" text (FIELDS: and INHERITS: -- see
_FIELDS_LINE_RE/_INHERITS_RE), and reads `extends` directly off
final_module_graph.jsonl rather than model_cards.jsonl's EXTENDED_BY: text
(build_extends_field_rows()'s own docstring: fixed 2026-08-13 for exactly
this reason). So the structural facts Stage A already captured are
sufficient to produce a model_cards.jsonl the real, unmodified ETL parses
correctly -- this script writes that text mechanically, not narratively.

Usage:
    python3 build_stage2_derived_artifacts.py <store_jsonl> <addons_src_dir> <out_dir>
"""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path


def _parse_manifest(module_dir: Path) -> dict:
    manifest_path = module_dir / "__manifest__.py"
    if not manifest_path.is_file():
        return {}
    try:
        return ast.literal_eval(manifest_path.read_text())
    except (SyntaxError, ValueError):
        return {}


def _fields_line(fields: list[dict]) -> str:
    tokens = []
    for f in fields:
        tok = f["name"]
        if f.get("required"):
            tok += "*"
        if f.get("computed"):
            tok += "~"
        if f.get("comodel"):
            tok += f"->{f['comodel']}"
            if f["type"] == "Many2many":
                tok += "[]"
        if f.get("depends"):
            tok += f"[dep:{','.join(f['depends'])}]"
        tokens.append(tok)
    return ", ".join(tokens)


def build_model_cards(modules: dict) -> list[dict]:
    rows = []
    for module_name, rec in modules.items():
        for m in rec.get("models", []):
            lines = [f"FIELDS: {_fields_line(m.get('fields', []))}"]
            if m.get("inherits"):
                inherits = m["inherits"]
                inherits = [inherits] if isinstance(inherits, str) else inherits
                lines.append(f"INHERITS: {', '.join(inherits)}")
            rows.append({
                "model": m["name"],
                "owner_module": module_name,
                "card": "\n".join(lines),
            })
    return rows


def _topo_order(modules: dict) -> list[str]:
    """Kahn's algorithm over the real `deps` field each module record
    already carries -- standard-library topological sort, no new
    dependency-resolution mechanism invented."""
    deps = {name: [d for d in rec.get("deps", []) if d in modules] for name, rec in modules.items()}
    indegree = {name: 0 for name in modules}
    for name, dlist in deps.items():
        for _ in dlist:
            indegree[name] += 1
    dependents: dict[str, list[str]] = {name: [] for name in modules}
    for name, dlist in deps.items():
        for d in dlist:
            dependents[d].append(name)

    ready = sorted(name for name, deg in indegree.items() if deg == 0)
    order = []
    while ready:
        ready.sort()
        name = ready.pop(0)
        order.append(name)
        for dependent in dependents[name]:
            indegree[dependent] -= 1
            if indegree[dependent] == 0:
                ready.append(dependent)
    remaining = set(modules) - set(order)
    order.extend(sorted(remaining))  # cycle fallback: stable, alphabetical
    return order


def build_manifest(modules: dict, addons_src: Path) -> dict:
    manifest_modules = {}
    external_deps: set[str] = set()
    for name, rec in modules.items():
        manifest = _parse_manifest(addons_src / name)
        manifest_modules[name] = {
            "author": manifest.get("author", "Odoo S.A."),
            "is_third_party": False,
            "is_official": True,
            "classification_note": "bundled with Odoo CE; parsed directly from the demo container's own addons path",
        }
        for dep in rec.get("deps", []):
            if dep not in modules:
                external_deps.add(dep)

    return {
        "modules": manifest_modules,
        "topo_order": _topo_order(modules),
        "external_deps": sorted(external_deps),
    }


def main(argv: list[str]) -> int:
    if len(argv) != 4:
        print(__doc__)
        return 1
    store_jsonl, addons_src, out_dir = Path(argv[1]), Path(argv[2]), Path(argv[3])
    out_dir.mkdir(parents=True, exist_ok=True)

    modules = {}
    with store_jsonl.open() as f:
        for line in f:
            line = line.strip()
            if line:
                rec = json.loads(line)
                modules[rec["module"]] = rec

    model_cards = build_model_cards(modules)
    with (out_dir / "model_cards.jsonl").open("w") as f:
        for row in model_cards:
            f.write(json.dumps(row))
            f.write("\n")

    manifest = build_manifest(modules, addons_src)
    (out_dir / "odoo_full_module_graph.json").write_text(json.dumps(manifest, indent=2))

    print(f"model_cards.jsonl: {len(model_cards)} model cards -> {out_dir}")
    print(f"odoo_full_module_graph.json: {len(manifest['modules'])} modules, "
          f"{len(manifest['topo_order'])} in topo order, "
          f"{len(manifest['external_deps'])} external deps -> {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
