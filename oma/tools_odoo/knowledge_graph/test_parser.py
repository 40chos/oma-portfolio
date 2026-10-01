"""Correctness test for Stage A, run entirely against the synthetic fixtures in
fixtures/ -- no real Odoo source, no network access, no model calls.

Run: python3 -m tools_odoo.knowledge_graph.test_parser
(or: python3 tools_odoo/knowledge_graph/test_parser.py from the odoo/ project root)
"""

from __future__ import annotations

import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from tools_odoo.knowledge_graph import driver, lookup
    from tools_odoo.knowledge_graph.parser import parse_module
else:
    from . import driver, lookup
    from .parser import parse_module

FIXTURES_DIR = Path(__file__).parent / "fixtures"

_failures: list[str] = []


def check(condition: bool, description: str) -> None:
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {description}")
    if not condition:
        _failures.append(description)


def find_model(record: dict, name: str) -> dict | None:
    return next((m for m in record["models"] if m["name"] == name), None)


def find_field(model: dict, name: str) -> dict | None:
    return next((f for f in model["fields"] if f["name"] == name), None)


def find_extend(record: dict, model: str, field: str) -> dict | None:
    return next(
        (e for e in record["extends"] if e["model"] == model and e["name"] == field), None
    )


def test_fixture_base() -> None:
    print("\n--- fixture_base ---")
    rec = parse_module(FIXTURES_DIR / "fixture_base").to_dict()
    check(rec["module"] == "fixture_base", "module name is fixture_base")
    check(rec["deps"] == [], "root module has empty deps")

    partner = find_model(rec, "demo.partner")
    check(partner is not None, "demo.partner model found")

    name_field = find_field(partner, "name")
    check(name_field is not None and name_field.get("required") is True, "name field is required")

    company_field = find_field(partner, "company_id")
    check(
        company_field is not None and company_field.get("type") == "Many2one"
        and company_field.get("comodel") == "demo.company",
        "company_id is Many2one -> demo.company",
    )

    display_name = find_field(partner, "display_name")
    check(
        display_name is not None
        and display_name.get("computed") is True
        and set(display_name.get("depends", [])) == {"name", "company_id"},
        "display_name is computed with real @api.depends(name, company_id)",
    )

    company = find_model(rec, "demo.company")
    partner_ids = find_field(company, "partner_ids")
    check(
        partner_ids is not None and partner_ids.get("type") == "One2many"
        and partner_ids.get("comodel") == "demo.partner",
        "demo.company.partner_ids is One2many -> demo.partner",
    )

    check(rec["needs_llm_review"] == [], "fixture_base has zero needs_llm_review flags (all statically resolvable)")


def test_fixture_mail() -> None:
    print("\n--- fixture_mail ---")
    rec = parse_module(FIXTURES_DIR / "fixture_mail").to_dict()
    check(rec["deps"] == ["fixture_base"], "fixture_mail depends on fixture_base")

    thread = find_model(rec, "demo.thread")
    check(thread is not None, "demo.thread model found")
    msg_count = find_field(thread, "message_count")
    check(
        msg_count is not None and msg_count.get("depends") == ["message_ids"],
        "demo.thread.message_count depends on message_ids via real @api.depends",
    )

    ext = find_extend(rec, "demo.partner", "thread_message_count")
    check(
        ext is not None and ext.get("computed") is True and ext.get("depends") == ["display_name"],
        "EXTENDS demo.partner += thread_message_count correctly captured "
        "(class with _inherit only, no _name)",
    )
    check(rec["needs_llm_review"] == [], "fixture_mail has zero needs_llm_review flags")


def test_fixture_note() -> None:
    print("\n--- fixture_note ---")
    rec = parse_module(FIXTURES_DIR / "fixture_note").to_dict()
    check(rec["deps"] == ["fixture_mail"], "fixture_note depends on fixture_mail")

    note = find_model(rec, "demo.note")
    check(note is not None, "demo.note model found")
    check(
        note is not None and note.get("inherits") == ["demo.thread"],
        "demo.note captured as _name + _inherit=['demo.thread'] (mixin), not misclassified as an extension",
    )
    summary = find_field(note, "summary")
    check(
        summary is not None and summary.get("depends") == ["memo"],
        "demo.note.summary computed field depends captured",
    )

    ext = find_extend(rec, "demo.partner", "note_count")
    check(
        ext is not None and ext.get("depends") == ["demo.note.partner_id"],
        "EXTENDS demo.partner += note_count carries the cross-module dotted dependency "
        "'demo.note.partner_id' verbatim",
    )

    check(
        rec["views_extend"] == [
            {
                "view": "demo_partner_form_note_inherit",
                "inherit_id": "fixture_base.demo_partner_form",
                "field_refs": ["note_count"],
            }
        ],
        "views_extend captures the one <record> with inherit_id set (plus its §0.9b "
        "field_refs), ignores the plain new views",
    )

    check(
        rec["views_primary"] == [
            {"view": "demo_note_form", "model": "demo.note", "field_refs": ["memo"]},
            {"view": "demo_note_kanban", "model": "demo.note", "field_refs": ["memo"]},
        ],
        f"§0.9a views_primary captures both non-inherited <record model=\"ir.ui.view\"> "
        f"declarations (demo_note_form, demo_note_kanban) with their §0.9b field_refs, "
        f"excludes the inherited one -- got {rec['views_primary']}",
    )

    check(
        rec["view_types"] == {"demo.note": ["form", "kanban"]},
        "view_types captures both real primary view types for demo.note (form, kanban), "
        "correctly excludes demo.partner since its only view record here is an "
        "inherit_id-based extension, not a primary view definition",
    )

    check(
        rec["security"] == [
            {"model": "demo.note", "group": "base.group_user", "perms": "rwc"},
            {"model": "demo.note", "group": "base.group_system", "perms": "rwcu"},
        ],
        "security rows parsed from ir.model.access.csv with correct model-name decoding and perms letters",
    )
    check(rec["needs_llm_review"] == [], "fixture_note has zero needs_llm_review flags")


def test_fixture_dynamic() -> None:
    print("\n--- fixture_dynamic (must flag, never guess) ---")
    rec = parse_module(FIXTURES_DIR / "fixture_dynamic").to_dict()
    reasons = " | ".join(r["reason"] for r in rec["needs_llm_review"])

    check(
        any("dynamic" in r["reason"] and "_inherit" in r["reason"] for r in rec["needs_llm_review"]),
        "dynamic _inherit (variable-based) flagged, not silently resolved",
    )
    check(
        any("getattr" in r["reason"] for r in rec["needs_llm_review"]),
        "dynamic getattr() call flagged",
    )
    check(
        any("_compute_missing" in r["reason"] for r in rec["needs_llm_review"]),
        "compute= referencing a missing method flagged",
    )
    check(
        any("comodel" in r["reason"] for r in rec["needs_llm_review"]),
        "non-literal relational comodel flagged",
    )
    check(
        any("ir.rule" in r["reason"] for r in rec["needs_llm_review"]),
        "ir.rule domain always flagged (runtime-evaluated, never a static fact)",
    )
    print(f"    (all reasons: {reasons})")

    # The one thing this file DOES declare unambiguously should still be captured correctly.
    comodel_model = find_model(rec, "demo.dynamic_comodel")
    check(comodel_model is not None, "demo.dynamic_comodel (unambiguous _name) still captured despite other flags in the file")


def test_fixture_behavioral_reopen() -> None:
    print("\n--- fixture_behavioral_reopen (§11.9 real gap: zero-field _inherit) ---")
    rec = parse_module(FIXTURES_DIR / "fixture_behavioral_reopen").to_dict()
    check(
        rec["extends"] == [],
        "zero-field behavioral reopen correctly produces NO extends entry (matches the "
        "pre-fix behavior -- extends is field-specific by design)",
    )
    check(
        rec["reopens"] == ["demo.note"],
        "but the relationship itself IS captured in reopens, even with zero new fields -- "
        f"this is the real fix, got {rec['reopens']}",
    )


def test_fixture_singular_model_dir() -> None:
    print("\n--- fixture_singular_model_dir (§11.9 real gap: model/ not models/) ---")
    rec = parse_module(FIXTURES_DIR / "fixture_singular_model_dir").to_dict()
    check(
        len(rec["models"]) == 1 and rec["models"][0]["name"] == "demo.thing",
        f"module using a singular model/ directory is no longer silently invisible -- "
        f"got models={rec['models']}",
    )


def test_fixture_view_nested() -> None:
    print("\n--- fixture_view_nested (§0.9a/§0.9b: primary views + deep field_refs) ---")
    rec = parse_module(FIXTURES_DIR / "fixture_view_nested").to_dict()

    check(
        rec["views_primary"] == [
            {
                "view": "demo_widget_form",
                "model": "demo.widget",
                "field_refs": ["code", "description", "line_ids", "qty"],
            }
        ],
        "§0.9a views_primary captures the one primary view (demo_widget_form) with "
        "§0.9b field_refs collected at every nesting depth (group > notebook > page "
        "> tree), in document order, and correctly excludes the inherited view and "
        f"the model-less orphan record -- got {rec['views_primary']}",
    )

    check(
        rec["views_extend"] == [
            {
                "view": "demo_widget_inherit",
                "inherit_id": "fixture_view_nested.demo_widget_form",
                "field_refs": ["extra_note"],
            }
        ],
        f"views_extend also carries field_refs for the inherited view -- got {rec['views_extend']}",
    )

    check(
        rec["needs_llm_review"] == [],
        "fixture_view_nested has zero needs_llm_review flags (the model-less record "
        "is silently excluded from views_primary, per the no-fabrication rule, not "
        "flagged -- it simply carries no resolvable model)",
    )


def test_driver_and_lookup(tmp_out: Path) -> None:
    print("\n--- driver + lookup layer ---")
    records = driver.run(FIXTURES_DIR, tmp_out)
    check(len(records) == 7, f"driver discovered all 7 fixture modules (got {len(records)})")
    check((tmp_out / "fixture_note.json").is_file(), "driver wrote fixture_note.json")
    check((tmp_out / "store.jsonl").is_file(), "driver wrote store.jsonl")

    store = lookup.KnowledgeStore.from_jsonl(tmp_out / "store.jsonl")
    check(
        set(store.all_modules()) == {
            "fixture_base", "fixture_mail", "fixture_note", "fixture_dynamic",
            "fixture_behavioral_reopen", "fixture_singular_model_dir", "fixture_view_nested",
        },
        "KnowledgeStore loaded all 7 modules from the JSONL store",
    )

    mod = store.get_module("fixture_note")
    check(mod is not None and mod["module"] == "fixture_note", "get_module('fixture_note') returns the right record")
    check(store.get_module("does.not.exist") is None, "get_module() returns None for an unknown module")

    dependents = store.get_dependents("demo.note", "partner_id")
    check(
        any(d["module"] == "fixture_note" and d["field"] == "note_count" for d in dependents),
        "get_dependents('demo.note', 'partner_id') finds fixture_note's note_count "
        "(cross-module 'demo.note.partner_id' EXTENDS dependency)",
    )

    same_model_dependents = store.get_dependents("demo.thread", "message_ids")
    check(
        any(d["field"] == "message_count" for d in same_model_dependents),
        "get_dependents('demo.thread', 'message_ids') finds the same-model message_count computed field",
    )

    # Manifest `depends` is direct-only (like real Odoo), not transitive:
    # fixture_base <- fixture_mail, fixture_dynamic (indegree 2)
    # fixture_mail <- fixture_note (indegree 1)
    hubs = store.get_hub_modules(min_indegree=1)
    hub_names = [h["module"] for h in hubs]
    check(
        hub_names and hub_names[0] == "fixture_base",
        f"get_hub_modules() ranks fixture_base first (most direct dependents), got {hub_names}",
    )
    fixture_base_hub = next(h for h in hubs if h["module"] == "fixture_base")
    check(fixture_base_hub["indegree"] == 2, f"fixture_base indegree == 2, got {fixture_base_hub['indegree']}")


def main() -> int:
    import shutil
    import tempfile

    test_fixture_base()
    test_fixture_mail()
    test_fixture_note()
    test_fixture_dynamic()
    test_fixture_behavioral_reopen()
    test_fixture_singular_model_dir()
    test_fixture_view_nested()

    tmp = Path(tempfile.mkdtemp(prefix="stage_a_test_"))
    try:
        test_driver_and_lookup(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n{'=' * 60}")
    if _failures:
        print(f"{len(_failures)} FAILURE(S):")
        for f in _failures:
            print(f"  - {f}")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
