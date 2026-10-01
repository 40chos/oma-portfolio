"""Small query layer over the combined JSONL store produced by driver.py.

Per docs/architecture/ODOO_KNOWLEDGE_PIPELINE_PILOT_RESULTS_2026-07-17.md §11.5: a
coding agent should call a bounded lookup function and get a bounded answer, not be
handed the whole graph as context. This module is the callable surface -- ready to be
wrapped as MCP tools later without redesign.
"""

from __future__ import annotations

import json
from pathlib import Path


class KnowledgeStore:
    def __init__(self, records: list[dict]):
        self._by_module: dict[str, dict] = {r["module"]: r for r in records}

    @classmethod
    def from_jsonl(cls, path: Path) -> "KnowledgeStore":
        records = []
        with Path(path).open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    records.append(json.loads(line))
        return cls(records)

    def get_module(self, name: str) -> dict | None:
        """Full parsed record for one module, or None if not in the store."""
        return self._by_module.get(name)

    def get_dependents(self, model: str, field: str) -> list[dict]:
        """Every (module, model, field) whose computed/related `depends` list
        references `<model>.<field>` (cross-model form, as written in EXTENDS lines)
        or bare `<field>` on a field belonging to `model` itself.
        """
        cross_token = f"{model}.{field}"
        results: list[dict] = []
        for record in self._by_module.values():
            for m in record.get("models", []):
                same_model = m["name"] == model
                for f in m.get("fields", []):
                    deps = f.get("depends") or []
                    if cross_token in deps or (same_model and field in deps):
                        results.append(
                            {"module": record["module"], "model": m["name"], "field": f["name"]}
                        )
            for e in record.get("extends", []):
                deps = e.get("depends") or []
                same_model = e.get("model") == model
                if cross_token in deps or (same_model and field in deps):
                    results.append(
                        {"module": record["module"], "model": e["model"], "field": e["name"]}
                    )
        return results

    def get_hub_modules(self, min_indegree: int = 1) -> list[dict]:
        """Modules depended on by at least `min_indegree` other modules in the
        store, per each module's manifest `deps` list -- sorted highest-first.
        """
        indegree: dict[str, int] = {name: 0 for name in self._by_module}
        for record in self._by_module.values():
            for dep in record.get("deps", []):
                if dep in indegree:
                    indegree[dep] += 1
        hubs = [
            {"module": name, "indegree": count}
            for name, count in indegree.items()
            if count >= min_indegree
        ]
        hubs.sort(key=lambda h: h["indegree"], reverse=True)
        return hubs

    def all_modules(self) -> list[str]:
        return sorted(self._by_module)
