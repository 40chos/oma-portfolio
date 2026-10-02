#!/usr/bin/env bash
# Stage 2: seeds the real Neo4j knowledge graph from Odoo CE's own bundled
# addons. Run once after `docker compose up -d` (odoo must be healthy).
# Safe to re-run -- each step is idempotent or wipes-and-rebuilds its own
# scope only.
set -euo pipefail

cd "$(dirname "$0")/.."
OMA_DIR="oma"
KG_DIR="${OMA_DIR}/var/knowledge_graph"
PY="${OMA_VENV_PYTHON:-python3}"
ODOO_CONTAINER="${OMA_ODOO_CONTAINER:-oma-odoo-1}"

echo "[seed-kg] copying Odoo CE's own addons out of the running container..."
rm -rf "${KG_DIR}/addons_src"
mkdir -p "${KG_DIR}/addons_src"
docker cp "${ODOO_CONTAINER}:/usr/lib/python3/dist-packages/odoo/addons" "${KG_DIR}/addons_src/odoo_addons"
rm -rf "${KG_DIR}/addons_src/odoo_addons/__pycache__"

cd "${OMA_DIR}"
set -a; source .env; set +a

echo "[seed-kg] Stage A: parsing real module source..."
"${PY}" -m tools_odoo.knowledge_graph.driver \
  var/knowledge_graph/addons_src/odoo_addons var/knowledge_graph

echo "[seed-kg] merging (no Stage B notes/review in this port -- see DECISIONS.md)..."
mkdir -p var/knowledge_graph/empty_review_output
: > var/knowledge_graph/empty_notes.jsonl
"${PY}" tools_odoo/knowledge_graph/merge_final_graph.py \
  var/knowledge_graph/store.jsonl \
  var/knowledge_graph/empty_notes.jsonl \
  var/knowledge_graph/empty_review_output \
  var/knowledge_graph

echo "[seed-kg] deriving model_cards.jsonl + odoo_full_module_graph.json..."
"${PY}" tools_odoo/knowledge_graph/build_stage2_derived_artifacts.py \
  var/knowledge_graph/store.jsonl \
  var/knowledge_graph/addons_src/odoo_addons \
  var/knowledge_graph

echo "[seed-kg] applying Neo4j constraints..."
"${PY}" scripts/odoo_kg_to_neo4j.py --setup-constraints

echo "[seed-kg] running the real ETL import..."
"${PY}" scripts/odoo_kg_to_neo4j.py --mode full --bootstrap-empty-db --base-dir var/knowledge_graph

echo "[seed-kg] done."
