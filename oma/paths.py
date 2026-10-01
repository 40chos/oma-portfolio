"""Single source of truth for filesystem locations that used to be found by
walking a fixed number of parent directories up from wherever a given file
happened to live. That approach was calibrated to one specific checkout
depth on the original dev machine and silently breaks (wrong directory, or
`FileNotFoundError`) the moment the repo is laid out differently -- which is
exactly what happened when this system was ported out of its original
multi-repo layout into this standalone one.

Every path here is overridable via an env var, with a default that matches
this repo's own layout (this file lives at the repo root, so `_REPO_ROOT`
needs no parent-walking of its own -- it's just this file's own directory).
"""

from __future__ import annotations

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent

CONSTITUTION_PATH = Path(
    os.environ.get(
        "OMA_CONSTITUTION_PATH",
        str(REPO_ROOT / "registry" / "constitution" / "constitution.md"),
    )
)

SKILLS_BASE_PATH = Path(
    os.environ.get("OMA_SKILLS_BASE_PATH", str(REPO_ROOT / "registry" / "skills"))
)


def skill_path(skill_name: str, filename: str = "SKILL.md") -> Path:
    return SKILLS_BASE_PATH / skill_name / filename


# Combinatorial-coverage evidence artifacts (dimension_table.json, coverage_data.json,
# tier1_evidence.json). Low-confidence signals only -- every reader of these treats a
# missing file as "no evidence yet", not a hard error.
COVERAGE_BASE_PATH = Path(
    os.environ.get("OMA_COVERAGE_BASE_PATH", str(REPO_ROOT / "registry" / "coverage"))
)

# Working directory for the Odoo knowledge-graph pipeline (tools_odoo/knowledge_graph/):
# store.jsonl, final_module_graph.jsonl, model_cards.jsonl, module_cards.jsonl,
# odoo_full_module_graph.json, unresolved_summary.json. Generated/mutable data, not
# checked in -- see .gitignore.
KNOWLEDGE_GRAPH_DATA_PATH = Path(
    os.environ.get("OMA_KNOWLEDGE_GRAPH_DATA_PATH", str(REPO_ROOT / "var" / "knowledge_graph"))
)
