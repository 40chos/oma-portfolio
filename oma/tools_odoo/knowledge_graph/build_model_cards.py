#!/usr/bin/env python3
"""Real fix for the token-budget problem found in build_module_cards.py: 61/240
per-MODULE cards blew past the ~300-400 token target because some modules are
genuinely huge (mis_base_extend: 86 models, 908 cross-module field additions;
account: similarly large). Forcing those under budget would mean dropping real
facts, which contradicts this whole pipeline's "never fabricate, never silently
drop" design.

The real fix: render at MODEL granularity instead, consolidating every field ever
added to a given model -- regardless of which module added it -- onto that model's
own card. This is a natural fit with the existing bounded-query design
(GraphStore.get_dependents(), §11.5) and is what an agent actually wants when working
on "crm.lead": every real field/relationship on that model in one place, not a
module-centric view that splits the same model's facts across dozens of module cards.

Also emits a small per-module index card (DEPS + NOTES + which models it defines/
touches) so "what does module X do" is still a fast, tiny lookup -- kept from
build_module_cards.py's already-working design, just no longer the only artifact.

No network/LLM calls -- purely local rendering of data already on disk.

Usage:
    python3 build_model_cards.py <final_module_graph_jsonl> <out_dir>
"""

import json
import sys
from pathlib import Path

CORE_VIEW_TYPES = ["form", "list", "kanban", "calendar"]
TOKEN_BUDGET_WARN = 400


def render_field(f: dict) -> str:
    s = f["name"]
    if f.get("required"):
        s += "*"
    if f.get("comodel"):
        s += f"->{f['comodel']}"
        if f.get("type") in ("One2many", "Many2many"):
            s += "[]"
    if f.get("computed"):
        deps = f.get("depends") or []
        s += f"~[dep:{','.join(deps)}]" if deps else "~"
    return s


def render_views_facet(types: set) -> str | None:
    if not types:
        return None
    core_present = [t for t in CORE_VIEW_TYPES if t in types]
    core_absent = [t for t in CORE_VIEW_TYPES if t not in types]
    extra = sorted(types - set(CORE_VIEW_TYPES))
    facet = f"VIEWS: {','.join(core_present) if core_present else '(none of form/list/kanban/calendar)'}"
    if core_absent:
        facet += f" | NOT: {','.join(core_absent)}"
    if extra:
        facet += f" | +extra: {','.join(extra)}"
    return facet


def estimate_tokens(text: str) -> int:
    return len(text) // 4


def build_model_index(records: list[dict]) -> dict:
    """One entry per real model, consolidating facts from every module that
    defines, extends, or reviews-resolves it."""
    models: dict[str, dict] = {}

    def get_or_create(name: str) -> dict:
        return models.setdefault(name, {
            "name": name, "owner_module": None, "own_fields": [], "inherits": [],
            "extended_by": [], "views": set(), "unresolved": [],
        })

    for rec in records:
        module = rec["module"]
        view_types_map = rec.get("view_types", {})

        for m in rec.get("models", []):
            entry = get_or_create(m["name"])
            entry["owner_module"] = module
            entry["own_fields"] = m.get("fields", [])
            entry["inherits"] = m.get("inherits", []) or []
            entry["views"] |= set(view_types_map.get(m["name"], []))

        for e in rec.get("extends", []):
            entry = get_or_create(e["model"])
            field_name = e.get("field", e.get("name"))
            entry["extended_by"].append({"module": module, "field": field_name, "raw": e})
            entry["views"] |= set(view_types_map.get(e["model"], []))

        for r in rec.get("review_resolved", []):
            model = r.get("model")
            if model:
                entry = get_or_create(model)
                entry["extended_by"].append({"module": module, "resolution": r["resolution"]})

        for u in rec.get("needs_llm_review", []):
            model = u.get("model")
            if model:
                entry = get_or_create(model)
                entry["unresolved"].append({"module": module, "note": u.get("llm_note") or u.get("reason", "")})

    return models


def render_model_card(entry: dict) -> str:
    lines = [f"MODEL: {entry['name']}"]
    if entry["owner_module"]:
        lines.append(f"OWNER: {entry['owner_module']}")
    if entry["inherits"]:
        lines.append(f"INHERITS: {', '.join(entry['inherits'])}")
    if entry["own_fields"]:
        fields = ", ".join(render_field(f) for f in entry["own_fields"])
        lines.append(f"FIELDS: {fields}")
    views_facet = render_views_facet(entry["views"])
    if views_facet:
        lines.append(views_facet)
    if entry["extended_by"]:
        lines.append("EXTENDED_BY:")
        for e in entry["extended_by"]:
            if "field" in e:
                raw = e["raw"]
                s = f"  {e['module']} += {e['field']}"
                if raw.get("computed"):
                    deps = raw.get("depends") or []
                    s += f"~[dep:{','.join(deps)}]" if deps else "~"
                lines.append(s)
            else:
                lines.append(f"  {e['module']}: {e['resolution']}")
    if entry["unresolved"]:
        lines.append("UNRESOLVED:")
        for u in entry["unresolved"]:
            lines.append(f"  {u['module']}: {u['note']}")
    return "\n".join(lines)


def render_module_index_card(rec: dict) -> str:
    lines = [f"MODULE: {rec['module']}"]
    if rec.get("deps"):
        lines.append(f"DEPS: {', '.join(rec['deps'])}")
    model_names = [m["name"] for m in rec.get("models", [])]
    if model_names:
        lines.append(f"DEFINES_MODELS: {', '.join(model_names)}")
    extends_targets = sorted({e["model"] for e in rec.get("extends", [])})
    if extends_targets:
        lines.append(f"EXTENDS_MODELS: {', '.join(extends_targets)}")
    if rec.get("notes"):
        lines.append(f"NOTES: {rec['notes']}")
    unresolved_count = len(rec.get("needs_llm_review", []))
    if unresolved_count:
        lines.append(f"UNRESOLVED_COUNT: {unresolved_count} (see per-model cards for detail)")
    return "\n".join(lines)


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__)
        return 1

    in_path = Path(sys.argv[1])
    out_dir = Path(sys.argv[2])
    out_dir.mkdir(parents=True, exist_ok=True)

    records = []
    with in_path.open() as f:
        for line in f:
            line = line.strip()
            if line:
                records.append(json.loads(line))

    # Module index cards -- small, always well under budget by construction.
    module_cards = []
    for rec in records:
        text = render_module_index_card(rec)
        module_cards.append({"module": rec["module"], "card": text, "estimated_tokens": estimate_tokens(text)})
    module_path = out_dir / "module_index_cards.jsonl"
    with module_path.open("w") as f:
        for c in module_cards:
            f.write(json.dumps(c))
            f.write("\n")

    # Model cards -- the real per-entity granularity, this is what fixes the budget problem.
    model_index = build_model_index(records)
    model_cards = []
    for name, entry in model_index.items():
        text = render_model_card(entry)
        model_cards.append({"model": name, "owner_module": entry["owner_module"], "card": text, "estimated_tokens": estimate_tokens(text)})
    model_path = out_dir / "model_cards.jsonl"
    with model_path.open("w") as f:
        for c in model_cards:
            f.write(json.dumps(c))
            f.write("\n")

    token_counts = [c["estimated_tokens"] for c in model_cards]
    over_budget = [c for c in model_cards if c["estimated_tokens"] > TOKEN_BUDGET_WARN]

    print(f"{len(module_cards)} module index cards -> {module_path}")
    print(f"{len(model_cards)} model cards -> {model_path}")
    print(f"model card tokens: min={min(token_counts)}, median={sorted(token_counts)[len(token_counts)//2]}, "
          f"max={max(token_counts)}, mean={sum(token_counts)/len(token_counts):.0f}")
    print(f"model cards still over the {TOKEN_BUDGET_WARN}-token target: {len(over_budget)} of {len(model_cards)}")
    for c in sorted(over_budget, key=lambda x: -x["estimated_tokens"])[:10]:
        print(f"  {c['model']}: {c['estimated_tokens']} tokens")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
