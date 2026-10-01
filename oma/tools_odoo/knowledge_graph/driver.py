"""Thin driver: given a directory containing many Odoo module subdirectories, runs
parser.parse_module() over each and writes one JSON file per module plus a combined
JSONL store (the store format `lookup.py` reads).

Usage:
    python3 -m tools_odoo.knowledge_graph.driver <modules_root> <output_dir>

`<modules_root>` is a local directory with one subdirectory per module (each
containing __manifest__.py, models/, views/, security/) -- e.g. a real Odoo addons
checkout pulled down separately. This script never fetches source itself.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .parser import parse_module


def discover_modules(modules_root: Path) -> list[Path]:
    out = []
    for child in sorted(modules_root.iterdir()):
        if child.is_dir() and (child / "__manifest__.py").is_file():
            out.append(child)
    return out


def run(modules_root: Path, output_dir: Path) -> list[dict]:
    output_dir.mkdir(parents=True, exist_ok=True)
    records: list[dict] = []
    for module_dir in discover_modules(modules_root):
        record = parse_module(module_dir).to_dict()
        records.append(record)
        (output_dir / f"{record['module']}.json").write_text(
            json.dumps(record, indent=2, sort_keys=False), encoding="utf-8"
        )

    jsonl_path = output_dir / "store.jsonl"
    with jsonl_path.open("w", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(record, sort_keys=False))
            fh.write("\n")

    return records


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("modules_root", type=Path, help="directory containing one subdirectory per module")
    parser.add_argument("output_dir", type=Path, help="directory to write <module>.json + store.jsonl into")
    args = parser.parse_args(argv)

    if not args.modules_root.is_dir():
        print(f"error: {args.modules_root} is not a directory", file=sys.stderr)
        return 1

    records = run(args.modules_root, args.output_dir)
    review_count = sum(len(r["needs_llm_review"]) for r in records)
    print(f"parsed {len(records)} module(s) -> {args.output_dir}")
    print(f"total needs_llm_review flags: {review_count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
