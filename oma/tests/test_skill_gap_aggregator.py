"""Phase 30, P6 (Phase H, §11): tests for the skill-gap aggregator.
The pure aggregation logic is mocked/tested directly; the real,
current backlog was verified live against the actual production data
instead (see docs/reports/PHASE30_P6_SKILL_STALENESS_2026-07-30.md):
55 real skill_gap events, 25 distinct shape-clusters, all 3 actually-
wired skill files implicated (2+ real incidents each).
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import scripts.skill_gap_aggregator as agg


def test_skills_implicated_by_capability_class_module_dev():
    skills = agg.skills_implicated_by_capability_class("module_dev")
    assert "odoo-module-scaffolding" in skills
    assert "odoo-codebase-audit" in skills
    assert "odoo-verification-and-reproduction" in skills
    print("PASS: a module_dev round implicates its own skill plus both always-read skills")


def test_skills_implicated_by_capability_class_data_change_only_always_read():
    skills = agg.skills_implicated_by_capability_class("data_change")
    assert "odoo-module-scaffolding" not in skills
    assert "odoo-codebase-audit" in skills
    assert "odoo-verification-and-reproduction" in skills
    print("PASS: a data_change round only implicates the two always-read skills, never the "
          "module-scaffolding skill it never actually reads")


def test_skills_implicated_by_capability_class_none_still_gets_always_read(monkeypatch):
    skills = agg.skills_implicated_by_capability_class(None)
    assert set(skills) == set(agg._ALWAYS_READ_SKILLS)
    print("PASS: an unknown/missing capability_class still correctly attributes the always-read skills")


def test_aggregate_surfaces_clusters_at_or_above_the_revision_threshold(monkeypatch):
    fake_rows = [
        {"id": 1, "summary": "Reproduction confirmed for x.y. Code-Review found 1 blocking issue(s): "
                              "Field definition uses fields.Text instead of fields.Text()",
         "capability_class": "module_dev"},
        {"id": 2, "summary": "Reproduction confirmed for a.b. Code-Review found 1 blocking issue(s): "
                              "Field definition uses fields.Text instead of fields.Text()",
         "capability_class": "module_dev"},
        {"id": 3, "summary": "a genuinely different, one-off failure never seen again",
         "capability_class": "module_dev"},
    ]
    monkeypatch.setattr(agg, "fetch_skill_gap_rows", lambda: fake_rows)
    result = agg.aggregate()
    assert result["total_skill_gap_events"] == 3
    assert "odoo-module-scaffolding" in result["skills_needing_revision"]
    assert result["skill_incident_counts"]["odoo-module-scaffolding"] >= 2
    print("PASS: a real 2+-instance cluster correctly surfaces its implicated skill for revision")


if __name__ == "__main__":
    test_skills_implicated_by_capability_class_module_dev()
    test_skills_implicated_by_capability_class_data_change_only_always_read()
    test_skills_implicated_by_capability_class_none_still_gets_always_read(None)
    print("(test_aggregate_surfaces_clusters_at_or_above_the_revision_threshold requires pytest's monkeypatch)")
