#!/usr/bin/env python3
"""Builds a self-contained, interactive HTML visualization of the real Odoo knowledge
graph (§11.12 Step A of docs/architecture/ODOO_KNOWLEDGE_PIPELINE_PILOT_RESULTS_2026-07-17.md).

Reads the already-assembled final_module_graph.jsonl (no network/LLM calls). Uses
networkx for degree computation and pyvis for rendering -- no graph database, no
server, matches the July-2026 research finding that this scale (~900 nodes) doesn't
warrant heavier tooling.

Design, per the plan and per Operator's stated ask (docs/meetings/07_meetingnotes.md):
  - 2 node colors only: module nodes vs. model/entity nodes (not per-community).
  - Node size = in-degree across the 4 real relation types (EXTENDS/INHERITS/
    VIEWS_EXTEND/SECURITY) -- this is what surfaces hub nodes naturally, without
    manual curation.
  - 4 distinct edge styles + a fixed legend -- not a hover-only explanation.
  - A 5th "DEFINES" edge (module -> its own models) exists only to keep the graph
    connected for layout; it's excluded from degree/hub calculation and rendered
    faint, hidden-by-default via the edge-type filter.
  - Labels hidden by default except on hub nodes; full detail always available via
    hover tooltip on any node.

Usage:
    python3 build_visualization.py <final_module_graph_jsonl> <out_html_path>
"""

import json
import sys
from pathlib import Path

import networkx as nx
from pyvis.network import Network

MODULE_COLOR = "#4C72B0"   # blue
MODEL_COLOR = "#DD8452"    # orange
HUB_LABEL_COUNT = 20        # top-N nodes by real-relation in-degree get a visible label

EDGE_STYLES = {
    "EXTENDS": dict(color="#2ca02c", dashes=False, width=2),        # solid green
    "INHERITS": dict(color="#9467bd", dashes=True, width=2),        # dashed purple
    "VIEWS_EXTEND": dict(color="#d62728", dashes=[2, 4], width=2),  # dotted red
    "SECURITY": dict(color="#8c564b", dashes=False, width=3),       # thick brown
    "DEFINES": dict(color="#cccccc", dashes=False, width=0.5),      # faint gray, structural only
}


def load_records(path: Path) -> list[dict]:
    records = []
    with path.open() as f:
        for line in f:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


def build_graph(records: list[dict]) -> tuple[dict, dict]:
    """Returns (node_kind, edges). `edges` is keyed by (source, target, kind) so every
    real relationship between two nodes is exactly ONE edge, not one per underlying
    fact (e.g. a module adding 5 fields to the same target model is one EXTENDS edge
    with weight=5, not 5 overlapping parallel edges) -- this is what makes node degree
    mean "connected to N other things" (what a hub actually is) rather than "N total
    facts," and keeps the rendered graph legible instead of a hairball of duplicate
    lines between the same two dots.
    """
    node_kind = {}  # name -> "module" | "model"
    edges: dict[tuple[str, str, str], int] = {}  # (u, v, kind) -> weight (fact count)

    def add_edge(u: str, v: str, kind: str) -> None:
        key = (u, v, kind)
        edges[key] = edges.get(key, 0) + 1

    for rec in records:
        node_kind[rec["module"]] = "module"

    for rec in records:
        module = rec["module"]

        for m in rec.get("models", []):
            model_name = m["name"]
            node_kind.setdefault(model_name, "model")
            add_edge(module, model_name, "DEFINES")
            for mixin in m.get("inherits", []) or []:
                node_kind.setdefault(mixin, "model")
                add_edge(model_name, mixin, "INHERITS")

        for e in rec.get("extends", []):
            target_model = e["model"]
            node_kind.setdefault(target_model, "model")
            add_edge(module, target_model, "EXTENDS")

        for v in rec.get("views_extend", []):
            inherit_id = v.get("inherit_id") or ""
            owner_module = inherit_id.split(".")[0] if "." in inherit_id else None
            if owner_module and owner_module in node_kind and owner_module != module:
                add_edge(module, owner_module, "VIEWS_EXTEND")

        for s in rec.get("security", []):
            model_name = s.get("model")
            if not model_name:
                continue
            node_kind.setdefault(model_name, "model")
            add_edge(module, model_name, "SECURITY")

    return node_kind, edges


def compute_real_indegree(edges: dict) -> dict:
    """In-degree counting only the 4 real relation types Operator cares about --
    excludes the structural DEFINES edges so every model isn't artificially
    inflated by simply being owned by a module. Counts distinct relationships
    (deduplicated edges), not raw underlying-fact weight -- a hub is a node
    connected to many OTHER nodes, not a node with many total facts."""
    indeg: dict[str, int] = {}
    for (_, v, kind) in edges:
        if kind != "DEFINES":
            indeg[v] = indeg.get(v, 0) + 1
    return indeg


# Edge types visible the moment the page loads -- only the two Operator described as the
# actual "hidden dependency" signal he originally wanted (a module quietly modifying a
# model owned elsewhere, and a model inheriting a mixin). SECURITY, VIEWS_EXTEND, and
# the structural DEFINES scaffolding start OFF, reachable via the legend checkboxes --
# showing all ~4,500 edges at once on a 1,087-node graph is a hairball, not a "concept
# at a glance," which is what was explicitly asked for.
DEFAULT_VISIBLE_KINDS = {"EXTENDS", "INHERITS"}


def build_visualization(records: list[dict], out_path: Path) -> dict:
    node_kind, edges = build_graph(records)
    real_indeg = compute_real_indegree(edges)

    hub_threshold = sorted(real_indeg.values(), reverse=True)[:HUB_LABEL_COUNT]
    min_hub_degree = hub_threshold[-1] if hub_threshold else 0

    # font_color intentionally NOT passed here -- pyvis's Node.__init__ unconditionally
    # overwrites any per-node "font" dict with just {"color": font_color} when the
    # Network-level font_color is set, silently clobbering the hub/non-hub label-size
    # distinction below. Default node/edge font color is set via set_options() instead,
    # where per-node "font" overrides still apply normally (real vis-network behavior).
    net = Network(height="900px", width="100%", directed=True, bgcolor="#111111")
    net.barnes_hut(gravity=-3000, central_gravity=0.3, spring_length=120, spring_strength=0.02, damping=0.09)

    hub_names: set[str] = set()
    for node, kind in node_kind.items():
        deg = real_indeg.get(node, 0)
        size = 8 + min(deg, 60) * 1.5
        is_hub = deg >= min_hub_degree and deg > 0
        if is_hub:
            hub_names.add(node)
        net.add_node(
            node,
            label=node if is_hub else "",
            title=f"{node}<br>type: {kind}<br>real in-degree: {deg}",
            color=MODULE_COLOR if kind == "module" else MODEL_COLOR,
            size=size,
            shape="dot",
            font={"size": 22 if is_hub else 14, "color": "#ffffff" if is_hub else "#cccccc"},
        )

    for (u, v, kind), weight in edges.items():
        style = dict(EDGE_STYLES[kind])
        base_width = style.pop("width")
        detail = f"{kind}: {weight} field(s)" if kind in ("EXTENDS",) and weight > 1 else kind
        net.add_edge(
            u, v,
            title=f"{u} &rarr; {v}<br>{detail}",
            group=kind,
            **style,
            width=base_width + (min(weight, 8) * 0.3 if kind == "EXTENDS" else 0),
            hidden=(kind not in DEFAULT_VISIBLE_KINDS),
            smooth=False,
        )

    net.set_options("""
    {
      "interaction": {"hover": true, "tooltipDelay": 100, "navigationButtons": true, "keyboard": true},
      "physics": {
        "enabled": true,
        "barnesHut": {"gravitationalConstant": -3000, "centralGravity": 0.3, "springLength": 120, "springConstant": 0.02, "damping": 0.09},
        "stabilization": {"enabled": true, "iterations": 300, "fit": true}
      },
      "edges": {"smooth": false},
      "nodes": {"font": {"color": "#cccccc"}}
    }
    """)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    net.write_html(str(out_path), notebook=False, open_browser=False)
    fix_non_hub_labels(out_path, hub_names)
    inject_legend_and_filters(out_path)
    inject_physics_freeze_and_fit(out_path)

    edges_by_kind: dict[str, int] = {}
    for (_, _, kind) in edges:
        edges_by_kind[kind] = edges_by_kind.get(kind, 0) + 1

    return {
        "total_nodes": len(node_kind),
        "module_nodes": sum(1 for k in node_kind.values() if k == "module"),
        "model_nodes": sum(1 for k in node_kind.values() if k == "model"),
        "total_edges": len(edges),
        "edges_by_kind": edges_by_kind,
        "hub_nodes": sorted(
            [(n, real_indeg.get(n, 0)) for n in node_kind if real_indeg.get(n, 0) >= min_hub_degree and real_indeg.get(n, 0) > 0],
            key=lambda x: -x[1],
        ),
    }


LEGEND_HTML = """
<div id="graph-legend" style="
  position:fixed; top:12px; left:12px; z-index:1000;
  background:rgba(20,20,20,0.92); color:#eee; padding:14px 18px;
  border-radius:8px; font-family:sans-serif; font-size:13px; line-height:1.6;
  border:1px solid #444; max-width:280px;">
  <div style="font-weight:bold; margin-bottom:6px;">Odoo Knowledge Graph</div>
  <div><span style="display:inline-block;width:12px;height:12px;border-radius:50%;background:#4C72B0;margin-right:6px;"></span>Module (240)</div>
  <div><span style="display:inline-block;width:12px;height:12px;border-radius:50%;background:#DD8452;margin-right:6px;"></span>Model/entity</div>
  <hr style="border-color:#444;">
  <div><span style="display:inline-block;width:20px;border-top:2px solid #2ca02c;margin-right:6px;"></span>EXTENDS <input type="checkbox" class="edge-toggle" data-kind="EXTENDS" checked></div>
  <div><span style="display:inline-block;width:20px;border-top:2px dashed #9467bd;margin-right:6px;"></span>INHERITS <input type="checkbox" class="edge-toggle" data-kind="INHERITS" checked></div>
  <div><span style="display:inline-block;width:20px;border-top:2px dotted #d62728;margin-right:6px;"></span>VIEWS_EXTEND <input type="checkbox" class="edge-toggle" data-kind="VIEWS_EXTEND"></div>
  <div><span style="display:inline-block;width:20px;border-top:3px solid #8c564b;margin-right:6px;"></span>SECURITY <input type="checkbox" class="edge-toggle" data-kind="SECURITY"></div>
  <div><span style="display:inline-block;width:20px;border-top:1px solid #cccccc;margin-right:6px;"></span>DEFINES (structural) <input type="checkbox" class="edge-toggle" data-kind="DEFINES"></div>
  <hr style="border-color:#444;">
  <div style="font-size:11px; color:#aaa; margin-bottom:8px;">Node size = real in-degree (EXTENDS/INHERITS/VIEWS_EXTEND/SECURITY only). Labels shown for the top 20 hub nodes; hover any node for full detail. EXTENDS/INHERITS shown by default -- the rest are opt-in to avoid a cluttered first view.</div>
  <button id="graph-fit-btn" style="width:100%; padding:6px; background:#333; color:#eee; border:1px solid #555; border-radius:4px; cursor:pointer;">Fit view</button>
  <div id="graph-status" style="font-size:11px; color:#8bd48b; margin-top:6px;"></div>
</div>
<script type="text/javascript">
window.addEventListener("load", function() {
  setTimeout(function() {
    document.querySelectorAll(".edge-toggle").forEach(function(cb) {
      cb.addEventListener("change", function() {
        var kind = cb.getAttribute("data-kind");
        var updates = [];
        edges.forEach(function(e) {
          if (e.group === kind) {
            updates.push({id: e.id, hidden: !cb.checked});
          }
        });
        edges.update(updates);
      });
    });
    var fitBtn = document.getElementById("graph-fit-btn");
    if (fitBtn) {
      fitBtn.addEventListener("click", function() {
        network.fit({animation: {duration: 600, easingFunction: "easeInOutQuad"}});
      });
    }
  }, 500);
});
</script>
"""

# Freezes the physics simulation once the layout has settled and frames the whole
# graph in view -- without this, a graph this size (1,087 nodes / thousands of edges)
# keeps re-simulating forever, which makes dragging/zooming feel laggy and the layout
# never truly "rest." This is the single biggest interaction-quality fix for a graph
# at this scale.
PHYSICS_FREEZE_JS = """
<script type="text/javascript">
window.addEventListener("load", function() {
  setTimeout(function() {
    var statusEl = document.getElementById("graph-status");
    if (statusEl) statusEl.innerText = "Stabilizing layout...";
    network.on("stabilizationProgress", function(params) {
      if (statusEl) {
        var pct = Math.round((params.iterations / params.total) * 100);
        statusEl.innerText = "Stabilizing layout... " + pct + "%";
      }
    });
    network.once("stabilizationIterationsDone", function() {
      network.setOptions({physics: {enabled: false}});
      network.fit({animation: {duration: 800, easingFunction: "easeInOutQuad"}});
      if (statusEl) {
        statusEl.innerText = "Layout settled -- drag/zoom freely.";
        setTimeout(function() { statusEl.innerText = ""; }, 3000);
      }
    });
  }, 300);
});
</script>
"""


def fix_non_hub_labels(out_path: Path, hub_names: set[str]) -> None:
    """pyvis's add_node() treats an empty-string label as falsy and silently falls
    back to using the node's id as the label (`if label: ... else: node_label = n_id`
    in its own source) -- meaning every non-hub node's label was rendering anyway,
    defeating the hide-detail-except-hubs design. Fixed here by directly rewriting the
    embedded node dataset after pyvis has already written the file, forcing label=""
    for every node NOT in the real hub set."""
    import re

    html = out_path.read_text(encoding="utf-8")
    m = re.search(r"nodes = new vis\.DataSet\((\[.*?\])\);", html, re.DOTALL)
    if not m:
        raise RuntimeError("Could not locate the node dataset in the generated HTML -- pyvis output format may have changed.")
    nodes = json.loads(m.group(1))
    for n in nodes:
        if n["id"] not in hub_names:
            n["label"] = ""
    new_nodes_json = json.dumps(nodes)
    html = html[: m.start(1)] + new_nodes_json + html[m.end(1):]
    out_path.write_text(html, encoding="utf-8")


def inject_legend_and_filters(out_path: Path) -> None:
    html = out_path.read_text(encoding="utf-8")
    html = html.replace("</body>", LEGEND_HTML + "</body>")
    out_path.write_text(html, encoding="utf-8")


def inject_physics_freeze_and_fit(out_path: Path) -> None:
    html = out_path.read_text(encoding="utf-8")
    html = html.replace("</body>", PHYSICS_FREEZE_JS + "</body>")
    out_path.write_text(html, encoding="utf-8")


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__)
        return 1

    in_path = Path(sys.argv[1])
    out_path = Path(sys.argv[2])

    records = load_records(in_path)
    stats = build_visualization(records, out_path)

    print(f"Visualization written to {out_path}")
    print(f"Total nodes: {stats['total_nodes']} ({stats['module_nodes']} modules, {stats['model_nodes']} models)")
    print(f"Total edges: {stats['total_edges']}")
    print("Edges by type:", stats["edges_by_kind"])
    print(f"\nTop hub nodes ({len(stats['hub_nodes'])}):")
    for name, deg in stats["hub_nodes"][:20]:
        print(f"  {name}: {deg}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
