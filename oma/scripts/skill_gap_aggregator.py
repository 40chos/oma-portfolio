"""Phase 30, P6 (Phase H, §11): aggregates real `root_cause='skill_gap'`
events the same way `rule_backlog_triage.py` aggregates proposed-rule
rows -- cluster by normalized shape, surface the skill files most
frequently implicated -- so a real, recurring documentation gap in a
skill file is discoverable systematically instead of only by a human
happening to notice the same failure shape repeating by hand.

Real, confirmed mapping (verified live by reading each specialist's own
source before writing this, not assumed): only `module_dev` tasks
currently read a skill file (`specialists/build/specialist.py` reads
`odoo-module-scaffolding/SKILL.md`); Code-Review reads `odoo-codebase-
audit/SKILL.md` unconditionally (not gated by capability_class); Testing-
QA reads `odoo-verification-and-reproduction/SKILL.md` unconditionally.
A `skill_gap` event's own `capability_class` (from its round's
`new_contract`) picks the Build-side skill; every `skill_gap` event is
ALSO potentially a Code-Review/Testing-QA skill gap, since those two
skills are read on every round regardless of capability_class -- this
tool surfaces clusters under all applicable skill files rather than
guessing which one specific specialist's own skill was really at fault
from the failure text alone (a genuine ambiguity a human reviewing the
cluster resolves, not this script).

Usage:
    python3 scripts/skill_gap_aggregator.py [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.rule_backlog_triage import cluster_rows  # noqa: E402

_REPO_ROOT = Path(__file__).resolve().parent.parent
_MIN_INCIDENTS_FOR_REVISION = 2

# Real, confirmed live (see this module's own docstring): which skill
# file(s) are actually read for a round with this capability_class.
_ALWAYS_READ_SKILLS = [
    "odoo-codebase-audit",              # specialists/code_review/specialist.py, unconditional
    "odoo-verification-and-reproduction",  # specialists/testing_qa/specialist.py, unconditional
]
_CAPABILITY_CLASS_SKILLS = {
    "module_dev": ["odoo-module-scaffolding"],
}


def fetch_skill_gap_rows() -> list[dict]:
    """Every real replan_round row whose verification_result.root_cause
    is 'skill_gap', with its round's own real capability_class.
    """
    import psycopg2
    import psycopg2.extras

    from infra.settings import load_postgres_settings

    s = load_postgres_settings()
    conn = psycopg2.connect(host=s.host, port=s.port, dbname=s.db, user=s.user, password=s.password)
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            """
            SELECT id,
                   detail->'verification_result'->>'notes' AS summary,
                   detail->'new_contract'->>'capability_class' AS capability_class
            FROM agent_memory_events
            WHERE event_type = 'replan_round'
              AND detail->'verification_result'->>'root_cause' = 'skill_gap'
            """
        )
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def skills_implicated_by_capability_class(capability_class: str | None) -> list[str]:
    return _ALWAYS_READ_SKILLS + _CAPABILITY_CLASS_SKILLS.get(capability_class or "", [])


def aggregate() -> dict:
    rows = fetch_skill_gap_rows()
    clusters = cluster_rows(rows)

    skill_incident_counts: dict[str, int] = {}
    for c in clusters.values():
        # Real capability_class per real row in this cluster (a cluster
        # can legitimately mix capability_classes if the same failure
        # SHAPE recurred across different task types).
        cluster_row_ids = set(c["ids"])
        cluster_rows_full = [r for r in rows if r["id"] in cluster_row_ids]
        skills_this_cluster: set[str] = set()
        for r in cluster_rows_full:
            skills_this_cluster.update(skills_implicated_by_capability_class(r.get("capability_class")))
        for skill in skills_this_cluster:
            skill_incident_counts[skill] = skill_incident_counts.get(skill, 0) + c["count"]

    top_clusters = sorted(clusters.items(), key=lambda kv: -kv[1]["count"])[:10]
    return {
        "total_skill_gap_events": len(rows),
        "distinct_shape_clusters": len(clusters),
        "skill_incident_counts": skill_incident_counts,
        "skills_needing_revision": sorted(
            skill for skill, n in skill_incident_counts.items() if n >= _MIN_INCIDENTS_FOR_REVISION
        ),
        "top_clusters": [
            {"count": c["count"], "sample_summary": c["sample_summary"][:200]} for _, c in top_clusters
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.parse_args()
    result = aggregate()
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
