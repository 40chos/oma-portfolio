#!/usr/bin/env python3
"""Builds the final, agent-facing compact cards (§11.12 Step B) from
final_module_graph.jsonl -- one card per module, in the same dense notation the whole
pipeline has used throughout, with two real additions per the July-2026 research pass:

  - A `VIEWS:` facet per model, listing BOTH present and explicitly absent core view
    types (form/list/kanban/calendar) -- directly answers Operator's own example ("this
    model mostly only has form views, not list views") without the agent having to
    infer absence from omission. Non-core types (search/graph/pivot/activity/etc,
    largely Odoo boilerplate present on nearly every model) are appended as `+extra`
    only when present, kept out of the present/absent framing to avoid noise.
  - An inline `UNRESOLVED:` tag per module for anything Stage B's targeted review
    still couldn't resolve -- a reserved, typed field in the same compact grammar
    (2026 confidence-aware-RAG practice: make abstention first-class, not a prose
    caveat), not just buried in the separate unresolved_summary.json file.

No network/LLM calls -- purely local rendering of data already on disk.

Usage:
    python3 build_module_cards.py <final_module_graph_jsonl> <out_dir>
"""

import json
import sys
from pathlib import Path

CORE_VIEW_TYPES = ["form", "list", "kanban", "calendar"]
TOKEN_BUDGET_WARN = 400  # §11.12 Step B target ceiling per card


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


def render_views_facet(model_name: str, view_types_map: dict) -> str | None:
    types = set(view_types_map.get(model_name, []))
    if not types:
        return None
    core_present = [t for t in CORE_VIEW_TYPES if t in types]
    core_absent = [t for t in CORE_VIEW_TYPES if t not in types]
    extra = sorted(types - set(CORE_VIEW_TYPES))
    parts = []
    if core_present:
        parts.append(",".join(core_present))
    facet = f"VIEWS: {parts[0] if parts else '(none of form/list/kanban/calendar)'}"
    if core_absent:
        facet += f" | NOT: {','.join(core_absent)}"
    if extra:
        facet += f" | +extra: {','.join(extra)}"
    return facet


def render_card(rec: dict) -> str:
    lines = [f"MODULE: {rec['module']}"]
    if rec.get("deps"):
        lines.append(f"DEPS: {', '.join(rec['deps'])}")

    view_types_map = rec.get("view_types", {})
    if rec.get("models"):
        lines.append("MODELS:")
        for m in rec["models"]:
            fields = ", ".join(render_field(f) for f in m.get("fields", []))
            lines.append(f"  {m['name']}({fields})")
            views_facet = render_views_facet(m["name"], view_types_map)
            if views_facet:
                lines.append(f"    {views_facet}")

    if rec.get("extends"):
        lines.append("EXTENDS:")
        for e in rec["extends"]:
            field_name = e.get("field", e.get("name"))
            s = f"  {e['model']} += {field_name}"
            if e.get("computed"):
                deps = e.get("depends") or []
                s += f"~[dep:{','.join(deps)}]" if deps else "~"
            lines.append(s)
            views_facet = render_views_facet(e["model"], view_types_map)
            if views_facet:
                lines.append(f"    {views_facet}")

    if rec.get("review_resolved"):
        lines.append("EXTENDS (resolved by targeted review):")
        for r in rec["review_resolved"]:
            model = r.get("model") or "?"
            lines.append(f"  {model}: {r['resolution']}")

    if rec.get("views_extend"):
        vs = ", ".join(f"{v['view']}(+{v.get('inherit_id', '')})" for v in rec["views_extend"])
        lines.append(f"VIEWS_EXTEND: {vs}")

    if rec.get("security"):
        ss = ", ".join(f"{s['model']}[{s['group']} {s['perms']}]" for s in rec["security"])
        lines.append(f"SECURITY: {ss}")

    if rec.get("notes"):
        lines.append(f"NOTES: {rec['notes']}")

    if rec.get("needs_llm_review"):
        lines.append("UNRESOLVED:")
        for u in rec["needs_llm_review"]:
            model = u.get("model") or "?"
            note = u.get("llm_note") or u.get("reason", "")
            lines.append(f"  {model}: {note}")

    return "\n".join(lines)


def estimate_tokens(text: str) -> int:
    return len(text) // 4  # standard rough estimate, consistent with the rest of this pipeline


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

    cards = []
    over_budget = []
    for rec in records:
        card_text = render_card(rec)
        tokens = estimate_tokens(card_text)
        cards.append({"module": rec["module"], "card": card_text, "estimated_tokens": tokens})
        if tokens > TOKEN_BUDGET_WARN:
            over_budget.append((rec["module"], tokens))

    jsonl_path = out_dir / "module_cards.jsonl"
    with jsonl_path.open("w") as f:
        for c in cards:
            f.write(json.dumps(c))
            f.write("\n")

    txt_path = out_dir / "module_cards.txt"
    with txt_path.open("w") as f:
        for c in cards:
            f.write(c["card"])
            f.write("\n\n" + ("-" * 60) + "\n\n")

    token_counts = [c["estimated_tokens"] for c in cards]
    print(f"{len(cards)} cards written -> {jsonl_path}, {txt_path}")
    print(f"token estimate: min={min(token_counts)}, median={sorted(token_counts)[len(token_counts)//2]}, "
          f"max={max(token_counts)}, mean={sum(token_counts)/len(token_counts):.0f}")
    print(f"cards over the {TOKEN_BUDGET_WARN}-token target: {len(over_budget)}")
    for mod, tok in sorted(over_budget, key=lambda x: -x[1])[:10]:
        print(f"  {mod}: {tok} tokens")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
