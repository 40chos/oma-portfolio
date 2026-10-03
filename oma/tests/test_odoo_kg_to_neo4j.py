"""Unit tests for docs/architecture/odoo-knowledge-pipeline/scripts/
odoo_kg_to_neo4j.py (PHASE36 S2 migration plan + S3.1 constraints setup).

This script's canonical home is the docs repo; it currently also lives at
the identical relative path under this worktree's `docs/` mirror (see that
file's own module docstring "DELIVERY NOTE" for why). Tests import it from
there via an explicit sys.path insertion, following this test directory's
own established convention (sys.path.insert(0, "..") in every other test
file here) extended one level further to reach the script's directory.

Neo4j driver calls are mocked throughout (FakeDriver/FakeSession) -- these
are logic tests, verifying the right Cypher/parameters get constructed for
a given input, never a real database connection. Real-driver calls are
exercised only by the separate constraint-setup integration check, run
manually against the live instance (see this task's final report), not
by this file.
"""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

_SCRIPTS_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "..", "docs", "architecture", "odoo-knowledge-pipeline", "scripts"
)
sys.path.insert(0, os.path.abspath(_SCRIPTS_DIR))

import pytest

import odoo_kg_to_neo4j as kg


# ==========================================================================
# Canonicalization / content_hash / reason_hash
# ==========================================================================
class TestCanonicalization:
    def test_key_order_is_deterministic(self):
        a = kg.canonicalize_record({"b": 1, "a": 2})
        b = kg.canonicalize_record({"a": 2, "b": 1})
        assert a == b == '{"a":2,"b":1}'

    def test_no_incidental_whitespace(self):
        assert " " not in kg.canonicalize_record({"a": [1, 2], "b": "x"})

    def test_non_ascii_preserved_literally(self):
        s = kg.canonicalize_record({"name": "café"})
        assert "café" in s
        assert "\\u" not in s

    def test_allow_nan_false_rejects_nan(self):
        with pytest.raises(ValueError):
            kg.canonicalize_record({"x": float("nan")})

    def test_content_hash_stable_and_order_independent(self):
        h1 = kg.content_hash({"a": 1, "b": 2})
        h2 = kg.content_hash({"b": 2, "a": 1})
        assert h1 == h2
        assert len(h1) == 64  # full sha256 hex digest, not truncated like reason_hash

    def test_content_hash_changes_with_content(self):
        assert kg.content_hash({"a": 1}) != kg.content_hash({"a": 2})


class TestReasonHash:
    def test_normalization_collapses_whitespace_and_case_and_punctuation(self):
        a = kg.normalize_reason("  Field   Not Found. ")
        b = kg.normalize_reason("field not found")
        assert a == b == "field not found"

    def test_base_hash_is_16_hex_chars(self):
        h = kg.reason_hash_base("some reason")
        assert len(h) == 16
        int(h, 16)  # valid hex

    def test_allocator_first_occurrence_gets_bare_hash(self):
        alloc = kg.ReasonHashAllocator()
        h = alloc.allocate("account.account", "field not found")
        assert h == kg.reason_hash_base("field not found")

    def test_allocator_disambiguates_second_and_third_occurrence(self):
        alloc = kg.ReasonHashAllocator()
        base = kg.reason_hash_base("field not found")
        h1 = alloc.allocate("account.account", "field not found")
        h2 = alloc.allocate("account.account", "field not found")
        h3 = alloc.allocate("account.account", "field not found")
        assert h1 == base
        assert h2 == f"{base}-00"
        assert h3 == f"{base}-01"

    def test_allocator_scopes_by_model_not_globally(self):
        alloc = kg.ReasonHashAllocator()
        h_a = alloc.allocate("account.account", "field not found")
        h_b = alloc.allocate("res.partner", "field not found")
        # Different models -> both get the bare hash, no collision.
        assert h_a == h_b == kg.reason_hash_base("field not found")


# ==========================================================================
# S2.2 step 1 -- module load
# ==========================================================================
def _sd(**overrides) -> kg.SourceData:
    defaults = dict(
        final_module_graph=[],
        model_cards={},
        odoo_full_module_graph={"modules": {}, "topo_order": [], "external_deps": {}},
        unresolved_summary={},
    )
    defaults.update(overrides)
    return kg.SourceData(**defaults)


class TestModuleRows:
    def test_basic_module_and_depends_on(self):
        sd = _sd(
            final_module_graph=[{"module": "account", "deps": ["base"], "notes": "n"}],
            odoo_full_module_graph={
                "modules": {"account": {"author": "Odoo", "is_third_party": False, "is_official": True}},
                "topo_order": ["base", "account"],
                "external_deps": {},
            },
        )
        module_rows, depends_on_rows = kg.build_module_rows(sd)
        names = {r["name"] for r in module_rows}
        assert "account" in names and "base" in names  # base created as dangling-edge stub
        account = next(r for r in module_rows if r["name"] == "account")
        assert account["author"] == "Odoo"
        assert account["is_external"] is False
        base = next(r for r in module_rows if r["name"] == "base")
        assert base["is_external"] is True
        assert depends_on_rows == [{"module": "account", "dep": "base"}]

    def test_external_deps_become_stub_modules(self):
        sd = _sd(
            final_module_graph=[{"module": "account", "deps": [], "notes": None}],
            odoo_full_module_graph={
                "modules": {},
                "topo_order": [],
                "external_deps": {"web": {}},
            },
        )
        module_rows, _ = kg.build_module_rows(sd)
        web = next(r for r in module_rows if r["name"] == "web")
        assert web["is_external"] is True


# ==========================================================================
# S2.2 step 3 -- model reconciliation
# ==========================================================================
class TestModelRows:
    def test_owned_and_foreign_reconciliation(self):
        sd = _sd(
            final_module_graph=[
                {"module": "account", "models": [{"name": "account.account"}]},
            ],
            model_cards={
                "account.account": {"model": "account.account", "owner_module": "account", "card": ""},
                "mail.compose.message": {"model": "mail.compose.message", "owner_module": None, "card": ""},
            },
        )
        model_rows, defines_rows = kg.build_model_rows(sd)
        by_name = {r["technical_name"]: r for r in model_rows}
        assert by_name["account.account"]["is_foreign"] is False
        assert by_name["mail.compose.message"]["is_foreign"] is True
        assert defines_rows == [{"module": "account", "model": "account.account"}]

    def test_multi_owner_produces_multiple_defines_edges_one_node(self):
        sd = _sd(
            final_module_graph=[
                {"module": "a", "models": [{"name": "res.partner"}]},
                {"module": "b", "models": [{"name": "res.partner"}]},
            ],
            model_cards={"res.partner": {"model": "res.partner", "owner_module": "a", "card": ""}},
        )
        model_rows, defines_rows = kg.build_model_rows(sd)
        assert len([r for r in model_rows if r["technical_name"] == "res.partner"]) == 1
        assert len(defines_rows) == 2

    def test_hard_failure_on_ownership_disagreement(self):
        sd = _sd(
            final_module_graph=[],
            model_cards={"x.model": {"model": "x.model", "owner_module": "some_module", "card": ""}},
        )
        with pytest.raises(kg.ModelReconciliationError):
            kg.build_model_rows(sd)

    def test_hard_failure_on_coverage_gap(self):
        sd = _sd(
            final_module_graph=[{"module": "a", "models": [{"name": "orphan.model"}]}],
            model_cards={},
        )
        with pytest.raises(kg.ModelReconciliationError):
            kg.build_model_rows(sd)


# ==========================================================================
# S2.1/S2.2 step 5 -- EXTENDS (INHERITS: only -- see discrepancy note)
# ==========================================================================
class TestExtendsRows:
    def test_single_inherits_target(self):
        sd = _sd(
            model_cards={
                "account.account": {
                    "model": "account.account",
                    "owner_module": "account",
                    "card": "MODEL: account.account\nOWNER: account\nINHERITS: mail.thread\nFIELDS: name*\n",
                }
            }
        )
        rows = kg.build_extends_rows(sd)
        assert rows == [{"child": "account.account", "base": "mail.thread", "via_module": "account"}]

    def test_multiple_comma_separated_targets(self):
        sd = _sd(
            model_cards={
                "x": {
                    "model": "x",
                    "owner_module": "m",
                    "card": "MODEL: x\nINHERITS: mail.thread, portal.mixin\nFIELDS:\n",
                }
            }
        )
        rows = kg.build_extends_rows(sd)
        bases = {r["base"] for r in rows}
        assert bases == {"mail.thread", "portal.mixin"}

    def test_no_inherits_line_produces_no_rows(self):
        sd = _sd(model_cards={"x": {"model": "x", "owner_module": "m", "card": "MODEL: x\nFIELDS:\n"}})
        assert kg.build_extends_rows(sd) == []


# ==========================================================================
# S2.2 step 6 -- field merge
# ==========================================================================
class TestReopensRows:
    """Phase 36 S13.2 gap fix -- build_reopens_rows."""

    def test_union_of_module_reopens_and_model_level_inherits(self):
        sd = _sd(
            final_module_graph=[
                {
                    "module": "mass_mailing_crm",
                    "reopens": ["crm.lead"],
                    "models": [],
                },
                {
                    "module": "hr",
                    "reopens": [],
                    "models": [{"name": "hr.employee", "inherits": ["mail.thread"], "fields": []}],
                },
            ]
        )
        rows = kg.build_reopens_rows(sd)
        pairs = {(r["module"], r["model"]) for r in rows}
        assert ("mass_mailing_crm", "crm.lead") in pairs
        assert ("hr", "mail.thread") in pairs

    def test_deduplicated_and_sorted(self):
        sd = _sd(
            final_module_graph=[
                {"module": "m", "reopens": ["x", "x"], "models": [{"name": "y", "inherits": ["x"], "fields": []}]},
            ]
        )
        rows = kg.build_reopens_rows(sd)
        assert rows == [{"module": "m", "model": "x"}]

    def test_no_reopens_or_inherits_produces_no_rows(self):
        sd = _sd(final_module_graph=[{"module": "m", "reopens": [], "models": [{"name": "y", "fields": []}]}])
        assert kg.build_reopens_rows(sd) == []


class TestDeclaresAccessRuleRows:
    """Phase 36 S13.3 gap fix -- build_declares_access_rule_rows."""

    def test_module_attribution_from_security_rows_gated_and_ungated(self):
        sd = _sd(
            final_module_graph=[
                {
                    "module": "account",
                    "security": [
                        {"model": "account.move", "group": "account.group_account_user", "perms": "rwcu"},
                        {"model": "account.account", "group": None, "perms": "rw"},
                    ],
                }
            ]
        )
        rows = kg.build_declares_access_rule_rows(sd)
        pairs = {(r["module"], r["model"]) for r in rows}
        assert ("account", "account.move") in pairs
        assert ("account", "account.account") in pairs

    def test_deduplicates_same_module_model_pair_across_multiple_groups(self):
        sd = _sd(
            final_module_graph=[
                {
                    "module": "account",
                    "security": [
                        {"model": "account.move", "group": "g1", "perms": "r"},
                        {"model": "account.move", "group": "g2", "perms": "w"},
                    ],
                }
            ]
        )
        rows = kg.build_declares_access_rule_rows(sd)
        assert rows == [{"module": "account", "model": "account.move"}]

    def test_no_security_rows_produces_no_rows(self):
        sd = _sd(final_module_graph=[{"module": "m", "security": []}])
        assert kg.build_declares_access_rule_rows(sd) == []


class TestFieldRows:
    def test_ttype_sourced_from_type_key_not_ttype_key(self):
        sd = _sd(
            final_module_graph=[
                {
                    "module": "account",
                    "models": [
                        {
                            "name": "account.account",
                            "fields": [{"name": "code", "type": "Char", "required": True}],
                        }
                    ],
                }
            ],
            model_cards={
                "account.account": {
                    "model": "account.account",
                    "owner_module": "account",
                    "card": "FIELDS: code*\n",
                }
            },
        )
        field_rows, mismatches = kg.build_field_rows(sd)
        assert len(field_rows) == 1
        row = field_rows[0]
        assert row["ttype"] == "Char"
        assert row["key"] == "account.account.code"
        assert row["required"] is True
        assert mismatches == []

    def test_hard_failure_on_missing_type(self):
        sd = _sd(
            final_module_graph=[
                {
                    "module": "m",
                    "models": [{"name": "x", "fields": [{"name": "f", "type": ""}]}],
                }
            ],
            model_cards={},
        )
        with pytest.raises(ValueError):
            kg.build_field_rows(sd)

    def test_field_source_mismatch_detected_both_directions(self):
        sd = _sd(
            final_module_graph=[
                {
                    "module": "m",
                    "models": [
                        {
                            "name": "x",
                            "fields": [
                                {"name": "only_in_final", "type": "Char"},
                            ],
                        }
                    ],
                }
            ],
            model_cards={"x": {"model": "x", "owner_module": "m", "card": "FIELDS: only_in_final, only_in_cards\n"}},
        )
        field_rows, mismatches = kg.build_field_rows(sd)
        reasons = {(m["name"], m["in"]) for m in mismatches}
        assert ("only_in_cards", "model_cards_only") in reasons
        # only_in_final has a type -> it's a real field row, not a mismatch entry for _final_ side
        assert all(m["name"] != "only_in_final" for m in mismatches)

    def test_relational_field_grammar_parsed(self):
        result = kg._parse_fields_line("currency_id->res.currency, tax_ids->account.tax[]")
        assert result["currency_id"]["relation_target"] == "res.currency"
        assert result["tax_ids"]["relation_kind"] == "many2many"

    def test_computed_with_depends_grammar_parsed(self):
        result = kg._parse_fields_line("account_type*~[dep:code]")
        assert result["account_type"]["required"] is True
        assert result["account_type"]["computed"] is True
        assert result["account_type"]["depends"] == ["code"]


class TestHasFieldRelatesTo:
    def test_relates_to_only_for_relational_fields(self):
        field_rows = [
            {"key": "a.b", "model": "a", "name": "b", "relation_target": "c", "relation_kind": "many2one_or_one2many"},
            {"key": "a.d", "model": "a", "name": "d", "relation_target": None, "relation_kind": None},
        ]
        has_field, relates_to = kg.build_has_field_and_relates_to_rows(field_rows)
        assert len(has_field) == 2
        assert len(relates_to) == 1
        assert relates_to[0]["target_model"] == "c"


class TestFieldDependsRows:
    """Phase 36 S13.5 gap fix -- build_field_depends_rows."""

    def test_same_model_dep_produces_an_edge_row(self):
        field_rows = [
            {"key": "a.account_type", "model": "a", "name": "account_type", "depends": ["code"]},
            {"key": "a.code", "model": "a", "name": "code", "depends": None},
        ]
        rows, skipped = kg.build_field_depends_rows(field_rows)
        assert rows == [{"field_key": "a.account_type", "depends_on_key": "a.code"}]
        assert skipped == 0

    def test_dotted_cross_model_dep_is_skipped_and_counted_not_dropped_silently(self):
        field_rows = [
            {"key": "a.date", "model": "a", "name": "date", "depends": ["line_ids.internal_index"]},
        ]
        rows, skipped = kg.build_field_depends_rows(field_rows)
        assert rows == []
        assert skipped == 1

    def test_no_depends_produces_no_rows(self):
        field_rows = [{"key": "a.x", "model": "a", "name": "x", "depends": None}]
        rows, skipped = kg.build_field_depends_rows(field_rows)
        assert rows == [] and skipped == 0

    def test_mixed_same_model_and_dotted_deps_on_one_field(self):
        field_rows = [
            {"key": "a.f", "model": "a", "name": "f", "depends": ["g", "other.h"]},
        ]
        rows, skipped = kg.build_field_depends_rows(field_rows)
        assert rows == [{"field_key": "a.f", "depends_on_key": "a.g"}]
        assert skipped == 1


# ==========================================================================
# S2.2 step 9 -- EXTENDS_FIELD grammar (dual implementation)
# ==========================================================================
REAL_RES_PARTNER_EXTENDED_BY = """EXTENDED_BY:
  account: res.partner += credit~[dep:account.move.line.debit,account.move.line.credit]
res.partner += debit~[dep:account.move.line.debit,account.move.line.credit]
  account: res.partner += use_partner_credit_limit~[dep:credit_limit]
  auth_signup += signup_token~
  auth_signup += signup_type
  auth_signup: res.partner += signup_token~
  base: res.partner += is_public~[dep:user_ids]
UNRESOLVED:
  some unrelated line
"""


class TestExtendedByGrammar:
    def test_both_parsers_agree_on_real_res_partner_sample(self):
        block = kg._extract_extended_by_block(REAL_RES_PARTNER_EXTENDED_BY)
        v1 = set(kg.parse_extended_by_grammar_imperative(block))
        v2 = set(kg.parse_extended_by_grammar_regex(block))
        assert v1 == v2
        assert ("account", "credit") in v1
        assert ("account", "debit") in v1  # continuation line, still "account" via_module
        assert ("auth_signup", "signup_token") in v1
        assert ("auth_signup", "signup_type") in v1
        assert ("base", "is_public") in v1

    def test_unresolved_section_not_included(self):
        block = kg._extract_extended_by_block(REAL_RES_PARTNER_EXTENDED_BY)
        assert "UNRESOLVED" not in block

    def test_build_extends_field_rows_end_to_end(self):
        sd = _sd(
            model_cards={
                "res.partner": {
                    "model": "res.partner",
                    "owner_module": "base",
                    "card": REAL_RES_PARTNER_EXTENDED_BY,
                }
            }
        )
        rows = kg.build_extends_field_rows(sd)
        triples = {(r["module"], r["model"], r["added_field_name"]) for r in rows}
        assert ("account", "res.partner", "credit") in triples
        assert ("account", "res.partner", "debit") in triples

    def test_mismatch_raises(self, monkeypatch):
        sd = _sd(
            model_cards={
                "x": {"model": "x", "owner_module": "m", "card": "EXTENDED_BY:\n  m: x += f1\n"}
            }
        )
        monkeypatch.setattr(
            kg, "parse_extended_by_grammar_regex", lambda block: [("m", "different_field")]
        )
        with pytest.raises(kg.ExtendsFieldGrammarMismatch):
            kg.build_extends_field_rows(sd)


# ==========================================================================
# S2.4/S2.1 -- UnresolvedItem
# ==========================================================================
class TestUnresolvedItems:
    def test_needs_llm_review_and_review_resolved_both_loaded(self):
        sd = _sd(
            final_module_graph=[
                {
                    "module": "account",
                    "needs_llm_review": [{"model": "account.account", "reason": "r1", "snippet": "s"}],
                    "review_resolved": [
                        {"model": "account.journal", "reason": "r2", "resolution": "fix", "source": "stage_b_review"}
                    ],
                }
            ]
        )
        rows = kg.build_unresolved_item_rows(sd)
        statuses = {r["resolution_status"] for r in rows}
        assert statuses == {"still_uncertain", "resolved"}
        still = next(r for r in rows if r["resolution_status"] == "still_uncertain")
        assert still["source"] == "static_analysis"
        resolved = next(r for r in rows if r["resolution_status"] == "resolved")
        assert resolved["resolution"] == "fix"

    def test_duplicate_reason_same_model_gets_disambiguated_keys(self):
        sd = _sd(
            final_module_graph=[
                {
                    "module": "account",
                    "needs_llm_review": [
                        {"model": "account.account", "reason": "same reason", "snippet": "a"},
                        {"model": "account.account", "reason": "same reason", "snippet": "b"},
                    ],
                    "review_resolved": [],
                }
            ]
        )
        rows = kg.build_unresolved_item_rows(sd)
        keys = [r["key"] for r in rows]
        assert len(set(keys)) == 2  # both kept, no silent MERGE-collapse
        assert keys[0] != keys[1]
        assert keys[1].endswith("-00")

    def test_nothing_dropped_count_matches_source(self):
        sd = _sd(
            final_module_graph=[
                {
                    "module": "m",
                    "needs_llm_review": [{"model": "x", "reason": f"r{i}"} for i in range(5)],
                    "review_resolved": [{"model": "x", "reason": f"rr{i}", "resolution": "z"} for i in range(3)],
                }
            ]
        )
        rows = kg.build_unresolved_item_rows(sd)
        assert len(rows) == 8


# ==========================================================================
# S2.2 step 11 -- AccessGroup / RESTRICTED_TO / has_ungated_access_rule
# ==========================================================================
class TestAccessGroups:
    def test_grouped_rows_create_access_group_and_restricted_to(self):
        sd = _sd(
            final_module_graph=[
                {
                    "module": "account",
                    "security": [
                        {"model": "account.account", "group": "account.group_account_manager", "perms": "rwcu"},
                    ],
                }
            ]
        )
        ag_rows, rt_rows, ungated, foreign = kg.build_access_group_rows(sd, {"account.account"})
        assert ag_rows == [{"xml_id": "account.group_account_manager"}]
        assert rt_rows == [{"model": "account.account", "group": "account.group_account_manager", "perms": "rwcu"}]
        assert ungated == set()
        assert foreign == []

    def test_null_group_sets_ungated_flag_not_an_edge(self):
        sd = _sd(
            final_module_graph=[
                {"module": "m", "security": [{"model": "res.partner", "group": None, "perms": "r"}]}
            ]
        )
        ag_rows, rt_rows, ungated, foreign = kg.build_access_group_rows(sd, {"res.partner"})
        assert ag_rows == []
        assert rt_rows == []
        assert ungated == {"res.partner"}

    def test_unknown_model_becomes_foreign_stub(self):
        sd = _sd(
            final_module_graph=[
                {"module": "m", "security": [{"model": "totally.unknown", "group": "g", "perms": "r"}]}
            ]
        )
        _, _, _, foreign = kg.build_access_group_rows(sd, set())
        assert foreign == [
            {"technical_name": "totally.unknown", "owner_module": None, "is_foreign": True, "has_ungated_access_rule": False}
        ]

    def test_duplicate_rule_from_two_modules_collapses_to_one_edge(self):
        sd = _sd(
            final_module_graph=[
                {"module": "a", "security": [{"model": "x", "group": "g", "perms": "r"}]},
                {"module": "b", "security": [{"model": "x", "group": "g", "perms": "r"}]},
            ]
        )
        _, rt_rows, _, _ = kg.build_access_group_rows(sd, {"x"})
        assert len(rt_rows) == 1


# ==========================================================================
# S2.2 step 8 -- :ViewType nodes / HAS_VIEW_TYPE edges
# ==========================================================================
class TestViewTypeRows:
    def test_view_types_is_a_dict_keyed_by_model_not_a_flat_list(self):
        # Real shape confirmed against final_module_graph.jsonl 2026-08-13:
        # {"account.account": ["form", "kanban"], "res.partner": ["form"]}
        sd = _sd(
            final_module_graph=[
                {
                    "module": "account",
                    "view_types": {
                        "account.account": ["form", "kanban"],
                        "res.partner": ["form"],
                    },
                }
            ]
        )
        view_type_rows, has_rows, missing_rows = kg.build_view_type_rows(sd)
        names = {r["name"] for r in view_type_rows}
        assert names == {"form", "kanban"}  # never model names leaking in as ViewType.name
        assert {"model": "account.account", "view_type": "form"} in has_rows
        assert {"model": "account.account", "view_type": "kanban"} in has_rows
        assert {"model": "res.partner", "view_type": "form"} in has_rows
        assert len(has_rows) == 3

    def test_absent_view_types_key_produces_empty_output(self):
        sd = _sd(final_module_graph=[{"module": "account"}])
        view_type_rows, has_rows, missing_rows = kg.build_view_type_rows(sd)
        assert view_type_rows == []
        assert has_rows == []


# ==========================================================================
# S0.9a/S0.9b -- View nodes (graceful degradation when source keys absent)
# ==========================================================================
class TestViewRows:
    def test_absent_views_primary_and_field_refs_produce_empty_output(self):
        sd = _sd(final_module_graph=[{"module": "account", "views_extend": [{"view": "x", "inherit_id": "y", "model": "account.account"}]}])
        views, declares, inherits, refs, unresolved = kg.build_view_rows(sd, {"account.account"}, set())
        assert refs == []
        assert unresolved == []
        assert inherits == [{"child": "x", "base": "y"}]

    def test_declares_view_from_views_primary(self):
        sd = _sd(
            final_module_graph=[
                {
                    "module": "account",
                    "views_primary": [{"view": "account.view_account_form", "model": "account.account", "view_type": "form"}],
                }
            ]
        )
        views, declares, inherits, refs, unresolved = kg.build_view_rows(sd, {"account.account"}, set())
        assert declares == [{"module": "account", "view": "account.view_account_form"}]
        assert views[0]["xml_id"] == "account.view_account_form"

    def test_field_ref_to_unknown_model_becomes_unresolved_item(self):
        sd = _sd(
            final_module_graph=[
                {
                    "module": "m",
                    "views_primary": [
                        {
                            "view": "v1",
                            "model": "unknown.model",
                            "view_type": "form",
                            "field_refs": ["some_field"],
                        }
                    ],
                }
            ]
        )
        views, declares, inherits, refs, unresolved = kg.build_view_rows(sd, {"account.account"}, set())
        assert len(unresolved) == 1
        assert unresolved[0]["model"] == "unknown.model"
        assert unresolved[0]["key"]  # never null -- see the function's own docstring finding 1
        assert unresolved[0]["source"] == "static_analysis"
        assert unresolved[0]["resolution_status"] == "still_uncertain"

    def test_field_ref_to_known_model_but_unknown_field_becomes_unresolved_item(self):
        # This is the actual S0.9b case (a view references a field genuinely
        # absent from its own model) -- distinct from the model-unknown case
        # above, and the thing the original (pre-fix) model-only check could
        # never detect.
        sd = _sd(
            final_module_graph=[
                {
                    "module": "m",
                    "views_primary": [
                        {
                            "view": "v1",
                            "model": "account.account",
                            "view_type": "form",
                            "field_refs": ["nonexistent_field"],
                        }
                    ],
                }
            ]
        )
        views, declares, inherits, refs, unresolved = kg.build_view_rows(
            sd, {"account.account"}, {("account.account", "code")}
        )
        assert len(unresolved) == 1
        assert unresolved[0]["model"] == "account.account"
        assert "not found on its declared model" in unresolved[0]["reason"]

    def test_field_ref_to_known_model_and_known_field_is_not_unresolved(self):
        sd = _sd(
            final_module_graph=[
                {
                    "module": "m",
                    "views_primary": [
                        {
                            "view": "v1",
                            "model": "account.account",
                            "view_type": "form",
                            "field_refs": ["code"],
                        }
                    ],
                }
            ]
        )
        views, declares, inherits, refs, unresolved = kg.build_view_rows(
            sd, {"account.account"}, {("account.account", "code")}
        )
        assert unresolved == []
        assert refs == [{"view": "v1", "model": "account.account", "field_name": "code"}]


class TestImportPlanS13Wiring:
    """Phase 36 S13 additions -- confirms build_import_plan/write_import_plan
    actually wire reopens_rows/declares_access_rule_rows/field_depends_rows/
    view_targets_rows through end to end, not just that the standalone
    builder functions work in isolation."""

    def test_view_targets_rows_derived_from_view_rows_with_a_model(self):
        sd = _sd(
            final_module_graph=[
                {
                    "module": "account",
                    "reopens": [],
                    "models": [{"name": "account.account", "fields": [{"name": "code", "type": "Char"}]}],
                    "views_primary": [
                        {"view": "account.view_account_form", "model": "account.account", "field_refs": []}
                    ],
                }
            ],
            model_cards={
                "account.account": {"model": "account.account", "owner_module": "account", "card": "FIELDS: code*\n"}
            },
        )
        plan = kg.build_import_plan(sd)
        assert {"xml_id": "account.view_account_form", "model": "account.account"} in plan.view_targets_rows

    def test_reopens_and_declares_access_rule_rows_present_on_plan(self):
        sd = _sd(
            final_module_graph=[
                {
                    "module": "mass_mailing_crm",
                    "reopens": ["crm.lead"],
                    "models": [],
                    "security": [{"model": "crm.lead", "group": None, "perms": "rw"}],
                }
            ]
        )
        plan = kg.build_import_plan(sd)
        assert {"module": "mass_mailing_crm", "model": "crm.lead"} in plan.reopens_rows
        assert {"module": "mass_mailing_crm", "model": "crm.lead"} in plan.declares_access_rule_rows

    def test_write_import_plan_runs_the_four_new_steps_when_rows_present(self):
        recorder = []

        class _RecSession:
            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *exc):
                return False

            def run(self_inner, cypher, **kwargs):
                recorder.append(cypher)

        class _RecDriver:
            def session(self_inner, **kwargs):
                return _RecSession()

        sd = _sd(
            final_module_graph=[
                {
                    "module": "m",
                    "reopens": ["x"],
                    "models": [{"name": "y", "fields": [{"name": "f", "type": "Char"}]}],
                    "security": [{"model": "x", "group": None, "perms": "r"}],
                    "views_primary": [{"view": "m.v1", "model": "y", "field_refs": []}],
                }
            ],
            model_cards={"y": {"model": "y", "owner_module": "m", "card": "FIELDS: f*[dep:f]\n"}},
        )
        plan = kg.build_import_plan(sd)
        audit = kg.AuditLog(Path("/tmp") / "test_import_plan_audit_scratch.jsonl")
        kg.write_import_plan(_RecDriver(), "neo4j", plan, audit)

        joined = "\n".join(recorder)
        assert "REOPENS" in joined
        assert "DECLARES_ACCESS_RULE" in joined
        assert "TARGETS" in joined


# ==========================================================================
# S3.1 -- constraint setup (mocked driver)
# ==========================================================================
class FakeSession:
    def __init__(self, recorder):
        self.recorder = recorder

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def run(self, cypher, **params):
        self.recorder.append((cypher, params))
        return FakeResult()


class FakeResult:
    def __init__(self, single_row=None):
        self._single_row = single_row if single_row is not None else {"c": 0}

    def single(self):
        return self._single_row

    def __iter__(self):
        return iter([])


class FakeDriver:
    def __init__(self):
        self.calls = []
        self.session_kwargs = []

    def session(self, **kwargs):
        self.session_kwargs.append(kwargs)
        return FakeSession(self.calls)


class TestUnresolvedItemCypher:
    def test_uses_optional_match_not_match_for_model(self):
        # Real finding (2026-08-13): a bare MATCH here silently drops the
        # ENTIRE row (node included) whenever `row.model` isn't a genuine
        # existing :Model -- which is exactly the case for the
        # `module:<name>` / `unknown_model::<view>` sentinels this ETL
        # deliberately uses for modelless facts (S2.4's "nothing silently
        # dropped" guarantee). Caught by inspecting the real live graph
        # (6,610 rows sent, only 2,838 nodes landed), not by any mocked
        # test, since a FakeSession never models Cypher's actual
        # MATCH-as-filter semantics.
        assert "OPTIONAL MATCH (m:Model {technical_name: row.model})" in kg.CYPHER_MERGE_UNRESOLVED_ITEM
        assert "\nMATCH (m:Model {technical_name: row.model})" not in kg.CYPHER_MERGE_UNRESOLVED_ITEM


class TestSetupConstraints:
    def test_all_statements_run_with_if_not_exists(self):
        driver = FakeDriver()
        applied = kg.setup_constraints(driver, "neo4j")
        assert applied == kg.CONSTRAINT_STATEMENTS
        assert len(driver.calls) == len(kg.CONSTRAINT_STATEMENTS)
        for cypher, params in driver.calls:
            assert "IF NOT EXISTS" in cypher
            assert not cypher.strip().upper().startswith("MATCH")

    def test_database_passed_explicitly_never_implicit_default(self):
        driver = FakeDriver()
        kg.setup_constraints(driver, "neo4j")
        assert all(kw.get("database") == "neo4j" for kw in driver.session_kwargs)

    def test_view_constraint_present_despite_predating_s0_9a(self):
        assert any("View" in s and "xml_id" in s for s in kg.CONSTRAINT_STATEMENTS)

    def test_no_statement_touches_labels_outside_this_migration(self):
        # Defense-in-depth sanity check: constraint statements are additive
        # schema only, never reference Pulsar's labels.
        pulsar_labels = {"CanonicalEntity", "Relation", "Episode", "FlaggedEdge", "SameAsCandidate", "WrittenNodeKey"}
        for s in kg.CONSTRAINT_STATEMENTS:
            assert not any(label in s for label in pulsar_labels)


# ==========================================================================
# S3.3a -- label-scoped wipe / sentinel check
# ==========================================================================
class TestLabelScopedWipe:
    def test_wipe_query_uses_odoo_owned_labels_param_not_bare_match(self):
        driver = FakeDriver()

        class Session(FakeSession):
            def run(self, cypher, **params):
                self.recorder.append((cypher, params))
                return FakeResult(single_row={"deleted": 0})

        driver.session = lambda **kw: Session(driver.calls)
        kg.label_scoped_wipe(driver, "neo4j")
        cypher, params = driver.calls[0]
        assert "MATCH (n) DETACH DELETE n" not in cypher  # never a bare, unscoped delete literal
        assert params["odoo_owned_labels"] == kg.ODOO_OWNED_LABELS
        assert "View" in params["odoo_owned_labels"]

    def test_wipe_loops_in_batches_until_a_round_deletes_zero(self):
        driver = FakeDriver()
        counts = iter([1000, 1000, 3, 0])

        class Session(FakeSession):
            def run(self, cypher, **params):
                self.recorder.append((cypher, params))
                return FakeResult(single_row={"deleted": next(counts)})

        driver.session = lambda **kw: Session(driver.calls)
        kg.label_scoped_wipe(driver, "neo4j")
        assert len(driver.calls) == 4  # stops the round after a 0-deleted batch, not before

    def test_sentinel_check_scopes_to_odoo_owned_labels(self):
        driver = FakeDriver()

        class Session(FakeSession):
            def run(self, cypher, **params):
                self.recorder.append((cypher, params))
                return iter([{"labels": ["CanonicalEntity"]}])

        driver.session = lambda **kw: Session(driver.calls)
        foreign = kg.foreign_data_sentinel_check(driver, "neo4j")
        assert foreign == [["CanonicalEntity"]]
        cypher, params = driver.calls[0]
        assert params["labels"] == kg.ODOO_OWNED_LABELS


# ==========================================================================
# S2.2 step 4 -- import_in_progress lifecycle (mocked driver)
# ==========================================================================
class TestImportInProgressLifecycle:
    def test_set_writes_all_five_fields(self):
        driver = FakeDriver()
        kg.set_import_in_progress(driver, "neo4j", "batch-123", "full")
        cypher, params = driver.calls[0]
        assert params["batch_id"] == "batch-123"
        assert params["mode"] == "full"
        assert "import_in_progress = true" in cypher

    def test_clear_sets_completed_at(self):
        driver = FakeDriver()
        kg.clear_import_in_progress(driver, "neo4j")
        cypher, params = driver.calls[0]
        assert "import_in_progress = false" in cypher
        assert params["completed_at"]

    def test_bootstrap_refuses_silently_without_flag(self):
        driver = FakeDriver()

        class Session(FakeSession):
            def run(self, cypher, **params):
                return FakeResult()  # count() = 0 always -> looks empty

        driver.session = lambda **kw: Session(driver.calls)
        with pytest.raises(RuntimeError):
            kg.bootstrap_if_needed(driver, "neo4j", allow_bootstrap=False)

    def test_bootstrap_proceeds_when_flag_set_and_db_empty(self):
        driver = FakeDriver()

        class Session(FakeSession):
            def run(self, cypher, **params):
                self.recorder.append((cypher, params))
                return FakeResult()

        driver.session = lambda **kw: Session(driver.calls)
        ran = kg.bootstrap_if_needed(driver, "neo4j", allow_bootstrap=True)
        assert ran is True
        assert any("bootstrapped_at" in c for c, _ in driver.calls)


# ==========================================================================
# S2.2 step 4 -- run-mutex (real fcntl against a tmp lock dir)
# ==========================================================================
class TestModeLock:
    def test_same_mode_lock_blocks_second_acquire(self, tmp_path):
        lock1 = kg.ModeLock("full", lock_dir=tmp_path)
        lock1.__enter__()
        try:
            lock2 = kg.ModeLock("full", lock_dir=tmp_path)
            with pytest.raises(kg.RunMutexHeld):
                lock2.__enter__()
        finally:
            lock1.__exit__(None, None, None)

    def test_full_blocks_while_incremental_lock_held(self, tmp_path):
        inc = kg.ModeLock("incremental", lock_dir=tmp_path)
        inc.__enter__()
        try:
            full = kg.ModeLock("full", lock_dir=tmp_path)
            with pytest.raises(kg.RunMutexHeld):
                full.__enter__()
        finally:
            inc.__exit__(None, None, None)

    def test_lock_released_on_exit_allows_reacquire(self, tmp_path):
        with kg.ModeLock("full", lock_dir=tmp_path):
            pass
        with kg.ModeLock("full", lock_dir=tmp_path):
            pass  # no exception -> released correctly


# ==========================================================================
# Pure-driver backup/restore (2026-08-13 rewrite -- no APOC/cypher-shell on
# the real instance, see run_export_backup's module docstring)
# ==========================================================================
class TestJsonBackupRestore:
    def test_export_writes_nodes_and_relationships_scoped_to_odoo_labels(self, tmp_path):
        driver = FakeDriver()
        node_rows = [
            {"labels": ["Module"], "props": {"name": "sale"}},
            {"labels": ["Model"], "props": {"technical_name": "sale.order"}},
        ]
        rel_rows = [
            {
                "start_labels": ["Module"],
                "start_props": {"name": "sale"},
                "end_labels": ["Model"],
                "end_props": {"technical_name": "sale.order"},
                "type": "DEFINES",
                "props": {},
            }
        ]

        class Session(FakeSession):
            def run(self, cypher, **params):
                self.recorder.append((cypher, params))
                if "RETURN labels(n)" in cypher:
                    return node_rows
                return rel_rows

        driver.session = lambda **kw: Session(driver.calls)
        out_path = tmp_path / "backup.json"
        kg.run_export_backup(driver, "neo4j", out_path)

        payload = json.loads(out_path.read_text())
        assert payload["nodes"] == node_rows
        assert payload["relationships"] == rel_rows
        assert "View" in payload["odoo_owned_labels"]

    def test_export_raises_if_output_file_ends_up_empty(self, tmp_path, monkeypatch):
        driver = FakeDriver()

        class Session(FakeSession):
            def run(self, cypher, **params):
                return []

        driver.session = lambda **kw: Session(driver.calls)
        out_path = tmp_path / "backup.json"
        # json.dumps() of the real payload can never be empty by construction;
        # force the write itself to simulate an empty/truncated file on disk
        # (the failure mode the size check exists to catch).
        original_write_text = Path.write_text
        monkeypatch.setattr(Path, "write_text", lambda self, content: original_write_text(self, ""))
        with pytest.raises(RuntimeError):
            kg.run_export_backup(driver, "neo4j", out_path)

    def test_restore_merges_nodes_by_natural_key_not_elementid(self, tmp_path):
        payload = {
            "exported_at": "2026-08-13T00:00:00Z",
            "odoo_owned_labels": kg.ODOO_OWNED_LABELS,
            "nodes": [{"labels": ["Module"], "props": {"name": "sale"}}],
            "relationships": [],
        }
        in_path = tmp_path / "backup.json"
        in_path.write_text(json.dumps(payload))

        driver = FakeDriver()
        driver.session = lambda **kw: FakeSession(driver.calls)
        kg.restore_from_json_backup(driver, "neo4j", in_path)

        cypher, params = driver.calls[0]
        assert "MERGE (n:Module {name: $key})" in cypher
        assert params["key"] == "sale"

    def test_restore_relationship_matches_both_endpoints_by_natural_key(self, tmp_path):
        payload = {
            "exported_at": "2026-08-13T00:00:00Z",
            "odoo_owned_labels": kg.ODOO_OWNED_LABELS,
            "nodes": [],
            "relationships": [
                {
                    "start_labels": ["Module"],
                    "start_props": {"name": "sale"},
                    "end_labels": ["Model"],
                    "end_props": {"technical_name": "sale.order"},
                    "type": "DEFINES",
                    "props": {},
                }
            ],
        }
        in_path = tmp_path / "backup.json"
        in_path.write_text(json.dumps(payload))

        driver = FakeDriver()
        driver.session = lambda **kw: FakeSession(driver.calls)
        kg.restore_from_json_backup(driver, "neo4j", in_path)

        cypher, params = driver.calls[0]
        assert "MATCH (a:Module {name: $start_key}), (b:Model {technical_name: $end_key})" in cypher
        assert "MERGE (a)-[rel:DEFINES]->(b)" in cypher
        assert params["start_key"] == "sale"
        assert params["end_key"] == "sale.order"
