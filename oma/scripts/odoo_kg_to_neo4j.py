#!/usr/bin/env python3
"""ETL: Odoo Knowledge Graph (flat JSONL/JSON source files) -> Neo4j.

Implements PHASE36_ODOO_KG_NEO4J_MIGRATION_AND_LIVE_INTEGRATION_2026-08-13.md
S2 (Migration plan) and S3.1 (constraints/indexes). Section numbers in
comments below (S2.1, S2.2 step N, S2.4, S3.1, S3.3a, ...) refer to that
document; read it before changing this file's load order or schema.

DELIVERY NOTE: this file's canonical intended location is
docs/architecture/odoo-knowledge-pipeline/scripts/odoo_kg_to_neo4j.py in the
docs repo (a sibling repo, not the agents repo this branch belongs to). It
was authored inside an agents-repo worktree that could not write outside its
own worktree tree, so it lives here at the identical relative path under the
worktree root and needs to be copied/moved to that location in the docs repo
by whoever merges this work -- see this task's final report for the full
explanation.

Single script, no framework, run manually (or later from cron/routine --
S2.2, "out of scope for this phase"). Source data (read-only inputs) all
live under docs/architecture/odoo-knowledge-pipeline/:
  - final_module_graph.jsonl   (authoritative structural source, S0.3)
  - model_cards.jsonl          (FIELDS:/VIEWS:/EXTENDED_BY:/INHERITS: grammar)
  - module_cards.jsonl         (per-module cards; NOT used for :EXTENDS --
    see the module docstring note below on a documented-vs-real-data
    discrepancy this script deliberately does not paper over)
  - odoo_full_module_graph.json (manifest: author/is_third_party/is_official/
    topo_order/external_deps/classification_note)
  - unresolved_summary.json    (aggregate authority for the still_uncertain
    subset of :UnresolvedItem, S2.5 check 1)

DISCREPANCY NOTE (report this, do not silently resolve it a different way
than documented -- see the task instructions this script was built under):
S2.1's relationship table cites "(:Model)-[:EXTENDS]->(:Model) ... from
module_cards.jsonl `EXTENDS:` and model_cards.jsonl `INHERITS:`". Direct
inspection of the real module_cards.jsonl shows its `EXTENDS:` section is
NOT a model-inheritance list at all -- it is a per-module field-addition
list, structurally identical in meaning to model_cards.jsonl's own
`EXTENDED_BY:` grammar (S2.2 step 9), just grouped by the extending module
instead of by the extended model. There is no base/inherits relationship
encoded there. This script therefore builds :EXTENDS edges from
model_cards.jsonl's `INHERITS:` line only (the genuinely correct source for
that relationship) and does NOT read module_cards.jsonl `EXTENDS:` for this
purpose. See the task's final report for the full explanation of why this
was not silently "fixed" by inventing a different reading of module_cards.jsonl.

SECOND FINDING, ACTED ON 2026-08-13 (originally flagged, not fixed, when this
comment was first written): S2.2 step 9 originally required a two-independent-
implementation stateful-grammar parse of model_cards.jsonl's `EXTENDED_BY:`
text. That design has a real, load-bearing problem beyond the grammar's own
fragility: model_cards.jsonl is produced by a SEPARATE generation script
(build_model_cards.py) that tools_odoo/knowledge_graph/incremental_sync.py's
own per-module refresh never touches -- so EXTENDS_FIELD silently stopped
updating for any field added or edited after the graph's last full rebuild,
even though final_module_graph.jsonl (incremental_sync.py's own target) was
correctly staying fresh the whole time. Found and diagnosed live (2026-08-13,
via a real unattended task) by an independent review of this same work.

Fixed by switching build_extends_field_rows() below to read directly from
final_module_graph.jsonl's own structured `extends` array
(`[{"model", "name", "type", "computed", "depends"}, ...]`, keyed by each
module record's owning `module`) instead of grammar-parsing model_cards.jsonl
text -- the exact fix this comment's own prior revision predicted ("the
structured extends array may be the simpler, more reliable source"). This
array sums to exactly 2,484 entries by trivial JSON parsing, matching the
grammar parsers' own real output count, and -- critically -- is the same
file incremental_sync.py already keeps fresh per-module, so EXTENDS_FIELD
now stays live automatically with no further wiring needed.

The two independent grammar parsers (parse_extended_by_grammar_imperative/
parse_extended_by_grammar_regex) and their own dual-implementation
cross-check are left in this file, still real and still tested -- they are
simply no longer wired into build_import_plan's live path. Kept as a
standalone, callable cross-check tool (e.g. for an offline audit comparing
model_cards.jsonl's text grammar against final_module_graph.jsonl's
structured array on a full rebuild), not deleted, since they were correct,
working code solving a real problem -- just not the one this specific field
turned out to need solved at incremental-sync time.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import logging
import os
import re
import sys
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator

logger = logging.getLogger("odoo_kg_to_neo4j")

# This script lives in scripts/, a direct sibling of infra/ in this repo, so no
# cross-repo path bootstrap is needed -- just make sure the repo root is importable
# regardless of the cwd this script is invoked from.
_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

ODOO_OWNED_LABELS: list[str] = [
    "Module",
    "Model",
    "Field",
    "View",  # S0.9a addition; owned by this migration's schema alongside
    # the seven labels S3.3a's wipe-scoping literally enumerates -- that
    # enumeration predates S0.9a and was not updated to include :View, which
    # this script treats as an omission to fix rather than faithfully
    # reproduce (an unscoped-for-View wipe would leave :View nodes behind on
    # every full reload, which is itself a fidelity bug). Flagged in report.
    "ViewType",
    "UnresolvedItem",
    "ImportMetadata",
    "AccessGroup",
]

BATCH_SIZE = 1000
IMPORT_METADATA_ID = "odoo_full_module_graph"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ==========================================================================
# S2.2 step 0 -- canonicalization / content_hash / provenance
# ==========================================================================
def canonicalize_record(record: dict) -> str:
    """S2.2 step 0's fifth-audit-revision canonicalization rule, applied
    verbatim: deterministic key order, no incidental whitespace, non-ASCII
    preserved literally, no NaN/Infinity tokens."""
    return json.dumps(
        record,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    )


def content_hash(record: dict) -> str:
    """SHA-256 hex digest over the canonicalized record (S2.2 step 0)."""
    return hashlib.sha256(canonicalize_record(record).encode("utf-8")).hexdigest()


def provenance(source_file: str, source_line: int | None, import_batch_id: str) -> dict:
    return {
        "source_file": source_file,
        "source_line": source_line,
        "import_batch_id": import_batch_id,
        "imported_at": utc_now_iso(),
    }


# ==========================================================================
# S2.1 reason_hash -- precise definition, including within-run disambiguation
# ==========================================================================
def normalize_reason(reason: str) -> str:
    s = " ".join(reason.lower().split())
    return s.rstrip(".,;:!?")


def reason_hash_base(reason: str) -> str:
    return hashlib.sha256(normalize_reason(reason).encode("utf-8")).hexdigest()[:16]


class ReasonHashAllocator:
    """S2.1: "the ETL additionally appends a zero-padded occurrence index
    (-00, -01, ...) to reason_hash for the 2nd and subsequent occurrences of
    the same (model, reason_hash) pair within a single import run, in
    source-array order." First occurrence gets the bare hash; 2nd gets
    "-00"; 3rd gets "-01"; etc.
    """

    def __init__(self) -> None:
        self._counts: dict[tuple[str, str], int] = {}

    def allocate(self, model: str, reason: str) -> str:
        base = reason_hash_base(reason)
        key = (model, base)
        n = self._counts.get(key, 0)
        self._counts[key] = n + 1
        if n == 0:
            return base
        return f"{base}-{n - 1:02d}"


# ==========================================================================
# Source loading
# ==========================================================================
@dataclass
class SourceData:
    final_module_graph: list[dict]  # 240 records, one per module
    model_cards: dict[str, dict]  # model -> record (682 entries)
    odoo_full_module_graph: dict  # manifest dict (modules/topo_order/external_deps/...)
    unresolved_summary: dict


def _read_jsonl(path: Path) -> list[dict]:
    records = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


def load_source_data(base_dir: Path) -> SourceData:
    final_module_graph = _read_jsonl(base_dir / "final_module_graph.jsonl")
    model_cards_list = _read_jsonl(base_dir / "model_cards.jsonl")
    model_cards = {rec["model"]: rec for rec in model_cards_list}
    with (base_dir / "odoo_full_module_graph.json").open(encoding="utf-8") as f:
        odoo_full_module_graph = json.load(f)
    with (base_dir / "unresolved_summary.json").open(encoding="utf-8") as f:
        unresolved_summary = json.load(f)
    return SourceData(
        final_module_graph=final_module_graph,
        model_cards=model_cards,
        odoo_full_module_graph=odoo_full_module_graph,
        unresolved_summary=unresolved_summary,
    )


# ==========================================================================
# S2.2 step 1 -- :Module nodes and DEPENDS_ON edges
# ==========================================================================
def build_module_rows(sd: SourceData) -> tuple[list[dict], list[dict]]:
    """Returns (module_rows, depends_on_rows). module_rows carry `name`,
    `notes`, `author`, `is_third_party`, `is_official`, `import_topo_position`,
    `is_external` (bool). Manifest-only fields (author/is_third_party/
    is_official) are merged on by natural key (S2.1's closing paragraph)."""
    manifest_modules: dict = sd.odoo_full_module_graph.get("modules", {})
    topo_order = sd.odoo_full_module_graph.get("topo_order", [])
    topo_position = {name: i for i, name in enumerate(topo_order)}
    external_deps = sd.odoo_full_module_graph.get("external_deps", {})

    module_rows: list[dict] = []
    seen_names: set[str] = set()
    depends_on_rows: list[dict] = []

    for rec in sd.final_module_graph:
        name = rec["module"]
        seen_names.add(name)
        manifest = manifest_modules.get(name, {})
        module_rows.append(
            {
                "name": name,
                "notes": rec.get("notes"),
                "author": manifest.get("author"),
                "is_third_party": manifest.get("is_third_party"),
                "is_official": manifest.get("is_official"),
                "import_topo_position": topo_position.get(name),
                "is_external": False,
            }
        )
        for dep in rec.get("deps", []):
            depends_on_rows.append({"module": name, "dep": dep})

    # S2.6: external_deps become :Module stubs with is_external: true so
    # DEPENDS_ON edges never dangle.
    if isinstance(external_deps, dict):
        external_names: Iterable[str] = external_deps.keys()
    else:
        external_names = external_deps
    for name in external_names:
        if name in seen_names:
            continue
        seen_names.add(name)
        module_rows.append(
            {
                "name": name,
                "notes": None,
                "author": None,
                "is_third_party": None,
                "is_official": None,
                "import_topo_position": topo_position.get(name),
                "is_external": True,
            }
        )

    # A dep target not covered by final_module_graph.jsonl's own 240 records
    # and not already declared in external_deps is still a real DEPENDS_ON
    # target -- create it as an external stub too, so no edge dangles.
    for row in depends_on_rows:
        dep = row["dep"]
        if dep not in seen_names:
            seen_names.add(dep)
            module_rows.append(
                {
                    "name": dep,
                    "notes": None,
                    "author": None,
                    "is_third_party": None,
                    "is_official": None,
                    "import_topo_position": topo_position.get(dep),
                    "is_external": True,
                }
            )

    return module_rows, depends_on_rows


# ==========================================================================
# S2.2 step 3/4 -- :Model nodes and DEFINES edges (union-of-both-sources
# reconciliation, S2.2 step 3's "revised 2026-08-13" logic)
# ==========================================================================
class ModelReconciliationError(RuntimeError):
    """Raised for the two genuine hard-failure conditions S2.2 step 3
    narrows this check to -- not on any 717/682/640 count mismatch."""


def build_model_rows(
    sd: SourceData, incremental_module_scope: str | None = None
) -> tuple[list[dict], list[dict]]:
    """Returns (model_rows, defines_rows). Target: 682 distinct :Model
    nodes (640 owned + 42 foreign), 717 DEFINES edges (raw, not deduped).

    Real gap found and fixed 2026-08-13: incremental (--mode incremental
    --module X) runs only ever refresh final_module_graph.jsonl for that one
    module (see tools_odoo/knowledge_graph/incremental_sync.py) -- nothing
    incrementally regenerates model_cards.jsonl, which is a separate,
    full-build-only artifact (tools_odoo/knowledge_graph/build_model_cards.py).
    So a module that introduces a genuinely NEW model (not just a new field
    on an existing one) always fails the reconciliation check below during
    incremental sync, even though nothing is actually wrong -- confirmed live:
    'oma_i_want_a_full_8c18cc48' adding project.handoff.checklist. When
    incremental_module_scope is set, coverage gaps are scoped to models owned
    by OTHER modules only; a gap caused by the module actually being synced is
    expected and downgraded to a log message instead of a hard failure -- full
    builds (incremental_module_scope=None) keep the original, unscoped hard
    check, since there every model must have a fresh model_cards.jsonl entry.
    """
    owned_pairs: list[tuple[str, str]] = []  # (module, technical_name), 717 raw
    owned_names: set[str] = set()
    scope_owned_names: set[str] = set()
    for rec in sd.final_module_graph:
        module = rec["module"]
        for m in rec.get("models", []):
            technical_name = m["name"]
            owned_pairs.append((module, technical_name))
            owned_names.add(technical_name)
            if module == incremental_module_scope:
                scope_owned_names.add(technical_name)

    model_cards_names = set(sd.model_cards.keys())

    # Hard failure: owner_module-non-null in model_cards.jsonl but absent
    # from final_module_graph.jsonl (genuine ownership disagreement).
    for name, card in sd.model_cards.items():
        if card.get("owner_module") is not None and name not in owned_names:
            raise ModelReconciliationError(
                f"model {name!r} has owner_module={card.get('owner_module')!r} in "
                "model_cards.jsonl but is absent from final_module_graph.jsonl "
                "(ownership disagreement between sources)"
            )
    # Hard failure: name in final_module_graph.jsonl but absent from
    # model_cards.jsonl entirely (genuine coverage gap) -- except gaps
    # entirely explained by the module currently being incrementally
    # synced, which are expected (see docstring) and just logged.
    missing_from_cards = owned_names - model_cards_names
    missing_outside_scope = missing_from_cards - scope_owned_names
    if missing_outside_scope:
        raise ModelReconciliationError(
            f"{len(missing_outside_scope)} model(s) present in final_module_graph.jsonl "
            f"are entirely absent from model_cards.jsonl: {sorted(missing_outside_scope)[:10]}..."
        )
    if missing_from_cards:
        logger.info(
            "build_model_rows: %d model(s) owned by incremental_module_scope=%r missing "
            "from model_cards.jsonl (expected, not regenerated incrementally): %s",
            len(missing_from_cards), incremental_module_scope, sorted(missing_from_cards),
        )

    foreign_names = model_cards_names - owned_names

    model_rows: list[dict] = []
    for name in owned_names:
        model_rows.append(
            {
                "technical_name": name,
                "owner_module": None,  # multiple owners possible; see DEFINES edges for real owners
                "is_foreign": False,
                "has_ungated_access_rule": False,
            }
        )
    for name in foreign_names:
        model_rows.append(
            {
                "technical_name": name,
                "owner_module": None,
                "is_foreign": True,
                "has_ungated_access_rule": False,
            }
        )

    defines_rows = [{"module": module, "model": name} for module, name in owned_pairs]
    return model_rows, defines_rows


# ==========================================================================
# S2.1/S2.2 step 5 -- :EXTENDS edges, from model_cards.jsonl's INHERITS: only
# (see module docstring's "DISCREPANCY NOTE" for why module_cards.jsonl's
# own EXTENDS: section is deliberately NOT used here)
# ==========================================================================
_INHERITS_RE = re.compile(r"^INHERITS:\s*(.+)$", re.MULTILINE)


def build_extends_rows(sd: SourceData) -> list[dict]:
    rows: list[dict] = []
    for name, card in sd.model_cards.items():
        m = _INHERITS_RE.search(card.get("card", ""))
        if not m:
            continue
        targets = [t.strip() for t in m.group(1).split(",") if t.strip()]
        for target in targets:
            rows.append({"child": name, "base": target, "via_module": card.get("owner_module")})
    return rows


# ==========================================================================
# Phase 36 S13.2 gap fix -- (:Module)-[:REOPENS]->(:Model), a NEW edge kind
# distinct from EXTENDS_FIELD (which carries a real, different meaning: "this
# module added field X to this model"). Real, confirmed gap found by direct
# inspection: build_extends_rows above (the only existing Module<->Model-via-
# _inherit builder) sources purely from model_cards.jsonl's per-MODEL
# `INHERITS:` line, which never records the extending MODULE at all beyond
# a single `via_module` property on the (:Model)-[:EXTENDS]->(:Model) edge --
# there is no queryable (:Module)-[...]->(:Model) edge for "this module
# reopened this model via _inherit," including the real case (found live in
# final_module_graph.jsonl, e.g. mass_mailing_crm re-opening crm.lead purely
# for method overrides, zero new fields) where a module both DEFINES a brand
# new model AND separately reopens an existing one via _inherit -- that
# second relationship was previously invisible everywhere in the graph.
#
# Source: each module record's own top-level `reopens` list (parser.py's
# ModuleRecord.reopens -- every model a module's classes re-open via
# `_inherit`, regardless of whether new fields were added, per parser.py's
# own §11.9 fix), UNIONed with each of that module's own `models[].inherits`
# entries (a model this module newly DEFINES that ALSO carries its own
# `_inherit` list, e.g. a model that both `_name`s itself and `_inherit`s a
# mixin like `mail.thread` in the same class body -- `reopens` alone does
# not capture this case, since `reopens` is only populated in parser.py's
# `elif isinstance(inherit, str/list)` branches, which are mutually
# exclusive with the `if name is not None` branch that populates
# `models[].inherits`). Both sources are real, on-disk, and non-overlapping;
# unioning them is the only way to get the complete, real set.
# ==========================================================================
def build_reopens_rows(sd: SourceData) -> list[dict]:
    """Returns deduplicated {"module": str, "model": str} rows for the new
    (:Module)-[:REOPENS]->(:Model) edge. See the block comment above this
    function for why this is a NEW edge kind (not an overload of
    EXTENDS_FIELD or EXTENDS) and why both `reopens` and `models[].inherits`
    are unioned rather than either alone."""
    pairs: set[tuple[str, str]] = set()
    for rec in sd.final_module_graph:
        module = rec["module"]
        for model_name in rec.get("reopens", []) or []:
            pairs.add((module, model_name))
        for model in rec.get("models", []) or []:
            for inherited in model.get("inherits", []) or []:
                pairs.add((module, inherited))
    return [{"module": m, "model": t} for m, t in sorted(pairs)]


# ==========================================================================
# Phase 36 S13.3 gap fix -- (:Module)-[:DECLARES_ACCESS_RULE]->(:Model), a
# NEW edge kind giving module attribution for access-control rules. Real,
# confirmed gap: RESTRICTED_TO (built by build_access_group_rows below) is
# Model->AccessGroup only -- it carries no module attribution at all, so
# "which modules touch this model's access control" was not queryable.
# final_module_graph.jsonl's `security` rows already live inside each
# module's own record (rec["security"]), so the module attribution is real,
# on-disk data -- not derived or guessed -- for both gated (real group) and
# ungated (group is null) rows alike, since both are a real "this module
# declares an access rule scoped to this model" fact.
# ==========================================================================
def build_declares_access_rule_rows(sd: SourceData) -> list[dict]:
    """Returns deduplicated {"module": str, "model": str} rows for the new
    (:Module)-[:DECLARES_ACCESS_RULE]->(:Model) edge -- every module that
    declares at least one security/access CSV row (security.csv) scoped to
    a given model, gated or ungated alike."""
    pairs: set[tuple[str, str]] = set()
    for rec in sd.final_module_graph:
        module = rec["module"]
        for row in rec.get("security", []) or []:
            model = row.get("model")
            if model:
                pairs.add((module, model))
    return [{"module": m, "model": t} for m, t in sorted(pairs)]


# ==========================================================================
# S2.2 step 6 -- :Field nodes, two-source merge
# ==========================================================================
_FIELD_NAME_RE = re.compile(r"^[A-Za-z0-9_.]+")
_FIELD_TARGET_RE = re.compile(r"->([A-Za-z0-9_.]+)")
_FIELD_DEP_RE = re.compile(r"\[dep:([^\]]*)\]")


def _parse_fields_line(fields_line: str) -> dict[str, dict]:
    """Parses model_cards.jsonl's FIELDS: grammar (name* required, name~
    computed, [dep:...] depends-list, ->target.model relational, [] suffix
    collection) into {name: {required, computed, depends, relation_target,
    relation_kind}}.

    The four markers (*, ~, ->target, []) do not appear in a single fixed
    order across the real corpus -- direct inspection shows both
    "currency_id*->res.currency~[dep:move_id.currency_id]" (required, then
    target, then computed) and "account_type*~[dep:code]" (required, then
    computed, no target) and "inbound_...->....line[]~[dep:...]" (target,
    then collection, then computed). This parser therefore scans for each
    marker independently rather than assuming one fixed sequence."""
    result: dict[str, dict] = {}
    if not fields_line.strip():
        return result
    # Split on top-level commas (deps lists use commas inside [dep:...],
    # so split conservatively by tracking bracket depth).
    depth = 0
    tokens: list[str] = []
    current = []
    for ch in fields_line:
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
        if ch == "," and depth == 0:
            tokens.append("".join(current).strip())
            current = []
        else:
            current.append(ch)
    if current:
        tokens.append("".join(current).strip())

    for tok in tokens:
        tok = tok.strip()
        if not tok:
            continue
        name_match = _FIELD_NAME_RE.match(tok)
        if not name_match:
            logger.warning("field_cards_grammar: unparsed token %r", tok)
            continue
        name = name_match.group(0)

        dep_match = _FIELD_DEP_RE.search(tok)
        depends = None
        if dep_match:
            depends = [d.strip() for d in dep_match.group(1).split(",") if d.strip()]

        target_match = _FIELD_TARGET_RE.search(tok)
        target = None
        relation_kind = None
        if target_match:
            target = target_match.group(1)
            after_target = tok[target_match.end():]
            relation_kind = "many2many" if after_target.startswith("[]") else "many2one_or_one2many"

        result[name] = {
            "required": "*" in tok,
            "computed": "~" in tok,
            "depends": depends,
            "relation_target": target,
            "relation_kind": relation_kind,
        }
    return result


_FIELDS_LINE_RE = re.compile(r"^FIELDS:\s*(.*)$", re.MULTILINE)


def build_field_rows(sd: SourceData) -> tuple[list[dict], list[dict]]:
    """Returns (field_rows, field_source_mismatch_warnings). field_rows
    carry key/model/name/ttype/required/computed/depends/relation_target/
    relation_kind. ttype is sourced from final_module_graph.jsonl's `type`
    key (S2.1 fifth-audit correction; the source key is `type`, the graph
    property is named `ttype`)."""
    field_rows: list[dict] = []
    mismatches: list[dict] = []

    # Source A: final_module_graph.jsonl models[].fields[] -- authoritative
    # for ttype.
    ttype_by_model: dict[str, dict[str, str]] = {}
    for rec in sd.final_module_graph:
        for m in rec.get("models", []):
            model_name = m["name"]
            per_model = ttype_by_model.setdefault(model_name, {})
            for f in m.get("fields", []):
                fname = f["name"]
                ftype = f.get("type")
                if not ftype:
                    raise ValueError(
                        f"hard ETL failure: field {model_name}.{fname} has empty/missing "
                        "'type' in final_module_graph.jsonl (S2.2 step 6)"
                    )
                per_model[fname] = ftype

    # Source B: model_cards.jsonl FIELDS: grammar -- required/computed/
    # depends/relation_target/relation_kind.
    cardgrammar_by_model: dict[str, dict[str, dict]] = {}
    for model_name, card in sd.model_cards.items():
        m = _FIELDS_LINE_RE.search(card.get("card", ""))
        cardgrammar_by_model[model_name] = _parse_fields_line(m.group(1)) if m else {}

    all_models = set(ttype_by_model.keys()) | set(cardgrammar_by_model.keys())
    for model_name in all_models:
        a_fields = ttype_by_model.get(model_name, {})
        b_fields = cardgrammar_by_model.get(model_name, {})
        a_names = set(a_fields.keys())
        b_names = set(b_fields.keys())

        for only_a in a_names - b_names:
            mismatches.append({"model": model_name, "name": only_a, "in": "final_module_graph_only"})
        for only_b in b_names - a_names:
            mismatches.append({"model": model_name, "name": only_b, "in": "model_cards_only"})

        for fname in a_names:  # ttype is authoritative and non-nullable -> drives which fields exist
            b = b_fields.get(fname, {})
            field_rows.append(
                {
                    "key": f"{model_name}.{fname}",
                    "model": model_name,
                    "name": fname,
                    "ttype": a_fields[fname],
                    "required": b.get("required", False),
                    "computed": b.get("computed", False),
                    "depends": b.get("depends"),
                    "relation_target": b.get("relation_target"),
                    "relation_kind": b.get("relation_kind"),
                }
            )
    return field_rows, mismatches


def build_has_field_and_relates_to_rows(field_rows: list[dict]) -> tuple[list[dict], list[dict]]:
    has_field_rows = [{"model": r["model"], "field_key": r["key"]} for r in field_rows]
    relates_to_rows = [
        {"field_key": r["key"], "target_model": r["relation_target"], "kind": r["relation_kind"]}
        for r in field_rows
        if r.get("relation_target")
    ]
    return has_field_rows, relates_to_rows


# ==========================================================================
# Phase 36 S13.5 gap fix -- (:Field)-[:DEPENDS_ON]->(:Field), the one real
# gap: `f.depends` (a computed field's @api.depends(...) list, from
# model_cards.jsonl's FIELDS: grammar -- see _parse_fields_line above) is
# already a real property on every :Field node (CYPHER_MERGE_FIELD SETs
# f.depends = row.depends), but no graph EDGE ever existed to traverse it.
#
# Deliberately SAME-MODEL only: a real `@api.depends(...)` entry can be a
# dotted cross-model path (e.g. "line_ids.internal_index", confirmed live in
# model_cards.jsonl's real FIELDS: text for account.bank.statement) --
# resolving those to a real Field node on a DIFFERENT model correctly would
# require walking the relation graph (RELATES_TO) per hop, which is real,
# separate future work, not silently guessed here. Dotted deps are skipped
# and counted (not silently dropped -- S2.4's "nothing silently dropped"
# discipline), logged once as a summary count by the caller.
# ==========================================================================
def build_field_depends_rows(field_rows: list[dict]) -> tuple[list[dict], int]:
    """Takes the already-built `field_rows` (matching this file's own
    established convention -- build_has_field_and_relates_to_rows above also
    takes field_rows, not `sd`, since depends/model/key are only available
    post-build_field_rows). Returns ({"field_key", "depends_on_key"} rows,
    skipped_dotted_dep_count)."""
    rows: list[dict] = []
    skipped_dotted = 0
    for row in field_rows:
        model = row["model"]
        depends = row.get("depends") or []
        for dep in depends:
            if "." in dep:
                skipped_dotted += 1
                continue
            rows.append({"field_key": row["key"], "depends_on_key": f"{model}.{dep}"})
    return rows, skipped_dotted


# ==========================================================================
# S2.2 step 8 -- :ViewType nodes, HAS_VIEW_TYPE / MISSING_VIEW_TYPE edges
# ==========================================================================
def build_view_type_rows(sd: SourceData) -> tuple[list[dict], list[dict], list[dict]]:
    """Real finding (2026-08-13): each module record's `view_types` key in
    final_module_graph.jsonl is NOT a flat list of view-type strings -- it
    is a dict keyed by model technical_name, each value a list of view-type
    strings for that model (confirmed by direct inspection of the real
    file). The original wf-2 implementation iterated it as a flat list,
    which for a dict yields its KEYS -- i.e. model names -- so it was
    silently creating :ViewType nodes named after models and never
    populating a single HAS_VIEW_TYPE edge (per-model `models[].view_types`
    does not exist in the source at all; that lookup was always empty
    too). Fixed to read the dict correctly."""
    view_types: set[str] = set()
    has_rows: list[dict] = []
    missing_rows: list[dict] = []
    for rec in sd.final_module_graph:
        vt_by_model = rec.get("view_types") or {}
        for model_name, vts in vt_by_model.items():
            for vt in vts or []:
                view_types.add(vt)
                has_rows.append({"model": model_name, "view_type": vt})
    view_type_rows = [{"name": vt} for vt in sorted(view_types)]
    return view_type_rows, has_rows, missing_rows


# ==========================================================================
# S2.2 step 9 -- EXTENDS_FIELD, dual-implementation stateful grammar parse
# of model_cards.jsonl's EXTENDED_BY: text.
# ==========================================================================
_EXTENDED_BY_SECTION_RE = re.compile(
    r"^EXTENDED_BY:\n((?:.*\n?)*?)(?=^[A-Z_]+:|\Z)", re.MULTILINE
)


def _extract_extended_by_block(card_text: str) -> str:
    m = _EXTENDED_BY_SECTION_RE.search(card_text + "\n")
    return m.group(1) if m else ""


_ENTRY_SPLIT_RE = re.compile(r"\s*\+=\s*")
_FIELDNAME_END_RE = re.compile(r"[~\[,]")


def _extract_added_field_name(rest: str) -> str:
    m = _FIELDNAME_END_RE.search(rest)
    return (rest[: m.start()] if m else rest).strip()


def _parse_rest_entry(rest: str) -> str:
    """An entry's <rest> is either a bare field expression ("signup_token~",
    from the "<module> += <rest>" form, where <rest> IS the field expr) or
    a full "<technical_name> += <field_expr>" segment (from the
    "<module>: <rest>" form -- confirmed by direct inspection of real
    model_cards.jsonl EXTENDED_BY: text, e.g. "account: res.partner +=
    credit~[dep:...]", where <rest> after the colon still contains its own
    "+="). If a top-level "+=" is present, only the text after it is the
    real field expression."""
    if "+=" in rest:
        rest = rest.split("+=", 1)[1]
    return _extract_added_field_name(rest.strip())


def parse_extended_by_grammar_imperative(block: str) -> list[tuple[str, str]]:
    """Implementation 1: straightforward imperative line-by-line state
    machine, per S2.2 step 9's grammar description."""
    results: list[tuple[str, str]] = []
    via_module: str | None = None
    for raw_line in block.split("\n"):
        if not raw_line.strip():
            continue
        indented = raw_line.startswith("  ")
        line = raw_line.strip()

        if indented and ":" in line and (line.index(":") < (line.index("+=") if "+=" in line else len(line))):
            # "  <module>: <rest>" -- opens new via_module, first entry from rest.
            module_part, rest = line.split(":", 1)
            via_module = module_part.strip()
            rest = rest.strip()
            for entry in _split_entries(rest):
                name = _parse_rest_entry(entry)
                if name:
                    results.append((via_module, name))
        elif indented and "+=" in line:
            # "  <module> += <rest>" -- opens new via_module equal to <module>.
            module_part, rest = line.split("+=", 1)
            via_module = module_part.strip()
            name = _extract_added_field_name(rest.strip())
            if name:
                results.append((via_module, name))
        elif not indented and "+=" in line:
            # continuation of the most recently opened via_module context.
            _, rest = line.split("+=", 1)
            name = _extract_added_field_name(rest.strip())
            if name and via_module:
                results.append((via_module, name))
        # else: unrecognized line shape -- ignored (matches neither entry
        # form; both implementations skip it identically).
    return results


def _split_entries(text: str) -> list[str]:
    """A "<module>: <rest>" line's <rest> can itself contain multiple
    technical_name += ... segments on continuation lines only (per the
    real grammar, a single ": " line has exactly one leading entry); this
    stays a single-entry split for that leading segment."""
    return [text] if text.strip() else []


def parse_extended_by_grammar_regex(block: str) -> list[tuple[str, str]]:
    """Implementation 2: independent, regex/generator-based state machine.
    Deliberately written without deriving from implementation 1 by
    refactoring (S2.2 step 9's own requirement) -- it tokenizes the whole
    block into (is_new_context, module_or_none, rest) triples first, then
    resolves via_module in a second pass."""

    def tokenize() -> Iterator[tuple[bool, str | None, str]]:
        for raw_line in block.split("\n"):
            if not raw_line.strip():
                continue
            colon_ctx = re.match(r"^  ([A-Za-z0-9_.]+):\s*(.*)$", raw_line)
            eq_ctx = re.match(r"^  ([A-Za-z0-9_.]+)\s*\+=\s*(.*)$", raw_line)
            cont = re.match(r"^([A-Za-z0-9_.]+)\s*\+=\s*(.*)$", raw_line)
            if colon_ctx:
                # The colon form's <rest> is itself a full entry, which real
                # data shows can still contain its own "<technical_name> +="
                # prefix (e.g. "account: res.partner += credit~[...]") --
                # strip it via regex substitution (independent technique
                # from implementation 1's str.split) if present.
                rest = re.sub(r"^[A-Za-z0-9_.]+\s*\+=\s*", "", colon_ctx.group(2))
                yield True, colon_ctx.group(1), rest
            elif eq_ctx:
                yield True, eq_ctx.group(1), eq_ctx.group(2)
            elif cont:
                yield False, None, cont.group(2)
            # else: skip unrecognized line

    results: list[tuple[str, str]] = []
    current_module: str | None = None
    for is_new, module, rest in tokenize():
        if is_new:
            current_module = module
        if not rest:
            continue
        # rest here is either "<field_expr>" (colon/eq context first entry)
        # or "<technical_name> += <field_expr>" (continuation, already
        # captured by the `cont` regex group 2 in the caller loop above --
        # for continuation lines `rest` IS the field expr since group(2) is
        # what follows +=).
        name_match = _FIELDNAME_END_RE.search(rest)
        name = (rest[: name_match.start()] if name_match else rest).strip()
        if name and current_module:
            results.append((current_module, name))
    return results


class ExtendsFieldGrammarMismatch(RuntimeError):
    pass


def build_extends_field_rows(sd: SourceData) -> list[dict]:
    """S2.2 step 9, real fix 2026-08-13 (see this file's own module docstring,
    "SECOND FINDING, ACTED ON"): reads final_module_graph.jsonl's own
    structured `extends` array directly -- the same file
    tools_odoo/knowledge_graph/incremental_sync.py keeps fresh per-module --
    instead of grammar-parsing the separately-generated, not-kept-fresh
    model_cards.jsonl. Each module record's own `extends` entries are fields
    THAT module added to another model; `module` here is always the owning
    record's own `rec["module"]`, never inferred or parsed from text.
    Deduplicated (a module can, in principle, appear more than once for the
    same field across independent extraction passes) and sorted for
    deterministic output, matching every other build_*_rows function's own
    convention in this file.
    """
    triples: set[tuple[str, str, str]] = set()
    for rec in sd.final_module_graph:
        module = rec["module"]
        for entry in rec.get("extends", []) or []:
            model = entry.get("model")
            field_name = entry.get("name")
            if model and field_name:
                triples.add((module, model, field_name))
    return [
        {"module": module, "model": model, "added_field_name": field_name}
        for module, model, field_name in sorted(triples)
    ]


# ==========================================================================
# S2.1/S2.2 step 10 -- :UnresolvedItem nodes, HAS_UNRESOLVED edges
# ==========================================================================
def build_unresolved_item_rows(sd: SourceData) -> list[dict]:
    """S2.1/S2.2 step 10. NOTE: direct inspection of the real
    final_module_graph.jsonl shows a small number of needs_llm_review
    entries have no "model" key at all (they flag a module-level fact --
    e.g. an ir.rule domain that is a runtime-evaluated Python expression --
    not tied to any single model). S2.2 step 10 assumes every entry carries
    a model. Rather than silently drop these (violating S2.4's "nothing is
    silently dropped" guarantee) or silently invent a different node shape
    than documented, this uses a `module:<name>` sentinel as the `model`
    value for exactly this case, flagged here and in the final report as a
    genuine, small gap in what S2.2 step 10 as written accounts for."""
    allocator = ReasonHashAllocator()
    rows: list[dict] = []
    for rec in sd.final_module_graph:
        for item in rec.get("needs_llm_review", []):
            model = item.get("model") or f"module:{rec['module']}"
            reason = item["reason"]
            rh = allocator.allocate(model, reason)
            rows.append(
                {
                    "key": f"{model}::{rh}",
                    "model": model,
                    "reason": reason,
                    "snippet": item.get("snippet"),
                    "resolution_status": "still_uncertain",
                    "resolution": None,
                    "llm_note": item.get("llm_note"),
                    "source": "static_analysis",
                }
            )
        for item in rec.get("review_resolved", []):
            model = item.get("model") or f"module:{rec['module']}"
            reason = item["reason"]
            rh = allocator.allocate(model, reason)
            rows.append(
                {
                    "key": f"{model}::{rh}",
                    "model": model,
                    "reason": reason,
                    "snippet": item.get("snippet"),
                    "resolution_status": "resolved",
                    "resolution": item.get("resolution"),
                    "llm_note": item.get("llm_note"),
                    "source": item.get("source", "stage_b_review"),
                }
            )
    return rows


# ==========================================================================
# S2.2 step 11 -- :AccessGroup nodes, RESTRICTED_TO edges,
# has_ungated_access_rule
# ==========================================================================
def build_access_group_rows(
    sd: SourceData, known_model_names: set[str]
) -> tuple[list[dict], list[dict], set[str], list[dict]]:
    """Returns (access_group_rows, restricted_to_rows, ungated_model_names,
    foreign_model_stub_rows). foreign_model_stub_rows are additional
    :Model stubs (owner_module: null, is_foreign: true, no :DEFINES edge)
    for security-row targets not already among the known model set."""
    access_groups: set[str] = set()
    restricted_to: dict[tuple[str, str, str], dict] = {}
    ungated_models: set[str] = set()
    foreign_stub_names: set[str] = set()

    for rec in sd.final_module_graph:
        for row in rec.get("security", []):
            model = row["model"]
            group = row.get("group")
            perms = row.get("perms")
            if model not in known_model_names:
                foreign_stub_names.add(model)
            if group is not None:
                access_groups.add(group)
                key = (model, group, perms)
                restricted_to[key] = {"model": model, "group": group, "perms": perms}
            else:
                ungated_models.add(model)

    access_group_rows = [{"xml_id": g} for g in sorted(access_groups)]
    restricted_to_rows = list(restricted_to.values())
    foreign_model_stub_rows = [
        {"technical_name": name, "owner_module": None, "is_foreign": True, "has_ungated_access_rule": False}
        for name in sorted(foreign_stub_names)
    ]
    return access_group_rows, restricted_to_rows, ungated_models, foreign_model_stub_rows


# ==========================================================================
# S0.9a/S0.9b -- :View nodes, DECLARES_VIEW/INHERITS_VIEW/REFERENCES_FIELD.
# The parser extension (`views_primary`/`field_refs` on each module record)
# is a separate, not-yet-landed task (see this task's own instructions: "DO
# NOT run the actual full ETL ... until the parser_extension task's
# regenerated JSONL is ready"). These builders degrade gracefully to empty
# output against the current source files (which do not yet carry
# `views_primary`/`field_refs`), so the ETL is ready to run the moment that
# data lands without further code changes.
# ==========================================================================
def build_view_rows(
    sd: SourceData, known_model_names: set[str], known_field_pairs: set[tuple[str, str]] = frozenset()
) -> tuple[list[dict], list[dict], list[dict], list[dict], list[dict]]:
    """Returns (view_rows, declares_view_rows, inherits_view_rows,
    references_field_rows, unresolved_view_field_ref_rows).

    Two real findings fixed here (2026-08-13, caught by inspecting the
    actual live post-migration graph, not the mocked test suite):

    1. The unresolved-item rows this function built carried no `key`
    (nor resolution_status/resolution/llm_note/source), but
    CYPHER_MERGE_UNRESOLVED_ITEM's MERGE key is `row.key` -- every one of
    these rows MERGEd on `key: null`, which Neo4j does not reject, so all
    ~3,750 of them silently collapsed onto/overwrote a small number of
    existing :UnresolvedItem nodes instead of erroring or creating their
    own nodes. Fixed to build a real key via ReasonHashAllocator, same as
    build_unresolved_item_rows does.

    2. This function's own docstring/schema intent (S0.9b) is "view
    references a field not found on its declared model" -- but the
    original check only tested `model not in known_model_names`, never
    whether the specific field exists on that model. Since nearly every
    model name referenced by a view ends up known (owned + foreign stubs
    + EXTENDS/RELATES_TO auto-created stubs), this almost never fired for
    the thing it claims to detect. Fixed to accept known_field_pairs (the
    real (model, field name) set from field_rows) and check field
    existence, not just model existence."""
    views: dict[str, dict] = {}
    declares_rows: list[dict] = []
    inherits_rows: list[dict] = []
    references_rows: list[dict] = []
    unresolved_rows: list[dict] = []
    unresolved_allocator = ReasonHashAllocator()

    for rec in sd.final_module_graph:
        module = rec["module"]
        for entry in rec.get("views_primary", []) or []:
            xml_id = entry["view"]
            views.setdefault(
                xml_id, {"xml_id": xml_id, "model": entry.get("model"), "view_type": entry.get("view_type"), "is_external": False}
            )
            declares_rows.append({"module": module, "view": xml_id})
            for field_name in entry.get("field_refs", []) or []:
                references_rows.append({"view": xml_id, "model": entry.get("model"), "field_name": field_name})

        for entry in rec.get("views_extend", []) or []:
            xml_id = entry["view"]
            inherit_id = entry.get("inherit_id")
            views.setdefault(
                xml_id, {"xml_id": xml_id, "model": entry.get("model"), "view_type": entry.get("view_type"), "is_external": False}
            )
            if inherit_id:
                views.setdefault(inherit_id, {"xml_id": inherit_id, "model": None, "view_type": None, "is_external": True})
                inherits_rows.append({"child": xml_id, "base": inherit_id})
            for field_name in entry.get("field_refs", []) or []:
                references_rows.append({"view": xml_id, "model": entry.get("model"), "field_name": field_name})

    for row in references_rows:
        model = row["model"]
        field_name = row["field_name"]
        if model not in known_model_names:
            reason = "view references a field on a model this migration has no record of"
        elif (model, field_name) not in known_field_pairs:
            reason = "view references a field not found on its declared model"
        else:
            continue
        key_model = model or f"unknown_model::{row['view']}"
        rh = unresolved_allocator.allocate(key_model, reason)
        unresolved_rows.append(
            {
                "key": f"{key_model}::{rh}",
                "model": key_model,
                "reason": reason,
                "snippet": f"view={row['view']} field={field_name}",
                "resolution_status": "still_uncertain",
                "resolution": None,
                "llm_note": None,
                "source": "static_analysis",
            }
        )

    return list(views.values()), declares_rows, inherits_rows, references_rows, unresolved_rows


# ==========================================================================
# Neo4j write layer
# ==========================================================================
class AuditLog:
    """S2.2 step 3: per-batch audit log, one JSONL file per import_batch_id."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, **fields: Any) -> None:
        fields.setdefault("logged_at", utc_now_iso())
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(fields, sort_keys=True, ensure_ascii=False) + "\n")


def chunked(rows: list[dict], size: int = BATCH_SIZE) -> Iterator[list[dict]]:
    for i in range(0, len(rows), size):
        yield rows[i : i + size]


class Neo4jWriter:
    """Thin batched-UNWIND-MERGE executor. Every write is `MERGE (n:Label
    {natural_key: $key}) SET n += $props`, never CREATE (S2.2 step 2)."""

    def __init__(self, driver: Any, database: str, audit: AuditLog, step_name: str) -> None:
        self.driver = driver
        self.database = database
        self.audit = audit
        self.step_name = step_name

    def run_batched(self, cypher: str, rows: list[dict], batch_key: str = "batch") -> None:
        with self.driver.session(database=self.database) as session:
            for i, batch in enumerate(chunked(rows)):
                try:
                    session.run(cypher, **{batch_key: batch})
                    self.audit.record(
                        step=self.step_name, batch_index=i, record_count=len(batch), success=True
                    )
                except Exception as exc:  # noqa: BLE001 -- audit-log then re-raise
                    self.audit.record(
                        step=self.step_name,
                        batch_index=i,
                        record_count=len(batch),
                        success=False,
                        error=str(exc),
                    )
                    raise


# ---- Cypher for each load step (S2.1 schema, S2.2 step 2 MERGE semantics) --
CYPHER_MERGE_MODULE = """
UNWIND $batch AS row
MERGE (m:Module {name: row.name})
SET m.notes = row.notes, m.author = row.author, m.is_third_party = row.is_third_party,
    m.is_official = row.is_official, m.import_topo_position = row.import_topo_position,
    m.is_external = row.is_external
"""

CYPHER_MERGE_DEPENDS_ON = """
UNWIND $batch AS row
MATCH (a:Module {name: row.module}), (b:Module {name: row.dep})
MERGE (a)-[:DEPENDS_ON]->(b)
"""

CYPHER_MERGE_MODEL = """
UNWIND $batch AS row
MERGE (m:Model {technical_name: row.technical_name})
SET m.is_foreign = row.is_foreign, m.has_ungated_access_rule = row.has_ungated_access_rule
"""

CYPHER_MERGE_DEFINES = """
UNWIND $batch AS row
MATCH (mod:Module {name: row.module}), (mdl:Model {technical_name: row.model})
CREATE (mod)-[:DEFINES]->(mdl)
"""

CYPHER_MERGE_EXTENDS = """
UNWIND $batch AS row
MATCH (child:Model {technical_name: row.child})
MERGE (base:Model {technical_name: row.base})
ON CREATE SET base.is_foreign = true, base.has_ungated_access_rule = false
MERGE (child)-[e:EXTENDS]->(base)
SET e.via_module = row.via_module
"""

CYPHER_MERGE_FIELD = """
UNWIND $batch AS row
MERGE (f:Field {key: row.key})
SET f.model = row.model, f.name = row.name, f.ttype = row.ttype, f.required = row.required,
    f.computed = row.computed, f.depends = row.depends, f.relation_target = row.relation_target,
    f.relation_kind = row.relation_kind
"""

CYPHER_MERGE_HAS_FIELD = """
UNWIND $batch AS row
MATCH (m:Model {technical_name: row.model}), (f:Field {key: row.field_key})
MERGE (m)-[:HAS_FIELD]->(f)
"""

CYPHER_MERGE_RELATES_TO = """
UNWIND $batch AS row
MATCH (f:Field {key: row.field_key})
MERGE (target:Model {technical_name: row.target_model})
ON CREATE SET target.is_foreign = true, target.has_ungated_access_rule = false
MERGE (f)-[r:RELATES_TO]->(target)
SET r.kind = row.kind
"""

CYPHER_MERGE_VIEWTYPE = """
UNWIND $batch AS row
MERGE (:ViewType {name: row.name})
"""

CYPHER_MERGE_HAS_VIEW_TYPE = """
UNWIND $batch AS row
MATCH (m:Model {technical_name: row.model})
MERGE (vt:ViewType {name: row.view_type})
MERGE (m)-[:HAS_VIEW_TYPE]->(vt)
"""

# Real, confirmed bug found and fixed live (2026-08-13, same day as the
# build_extends_field_rows source-file fix above): a bare `MATCH (f:Field
# {model: row.model, name: row.added_field_name})` requires that exact Field
# node to ALREADY exist -- but a `_inherit`-added field is frequently only
# ever recorded in the EXTENDING module's own `extends` entry, never
# duplicated into the owning module's own `models[].fields[]` list that
# build_field_rows sources :Field nodes from. Confirmed directly: of 2,495
# real rows built from the real corpus, only 64 (2.6%) had a pre-existing
# matching :Field node -- the bare MATCH was silently dropping the other
# 2,431 (97.4%), the exact same "MATCH-as-filter silently drops the whole
# row" bug class as CYPHER_MERGE_UNRESOLVED_ITEM's own earlier fix this same
# day. Fixed the same way every other dangling-reference edge in this file
# already handles it (CYPHER_MERGE_EXTENDS/CYPHER_MERGE_RELATES_TO's own
# established MERGE-with-ON-CREATE-stub pattern): the target :Field is
# MERGEd, not just MATCHed, creating a minimal real stub (key/model/name
# only, ttype left null) when this is the first evidence this field exists
# at all -- an honest partial fact, not a silently dropped one. A LATER
# "fields" step in this same import (if the owning module's own record ever
# does list this field) fills in the full ttype/required/computed/etc. via
# the same key-based MERGE, never creating a duplicate.
CYPHER_MERGE_EXTENDS_FIELD = """
UNWIND $batch AS row
MATCH (mod:Module {name: row.module})
MERGE (f:Field {key: row.model + '.' + row.added_field_name})
ON CREATE SET f.model = row.model, f.name = row.added_field_name
MERGE (mod)-[e:EXTENDS_FIELD]->(f)
SET e.added_field_name = row.added_field_name
"""

CYPHER_MERGE_UNRESOLVED_ITEM = """
UNWIND $batch AS row
OPTIONAL MATCH (m:Model {technical_name: row.model})
MERGE (u:UnresolvedItem {key: row.key})
SET u.model = row.model, u.reason = row.reason, u.snippet = row.snippet,
    u.resolution_status = row.resolution_status, u.resolution = row.resolution,
    u.llm_note = row.llm_note, u.source = row.source
FOREACH (_ IN CASE WHEN m IS NULL THEN [] ELSE [1] END |
  MERGE (m)-[:HAS_UNRESOLVED]->(u)
)
"""

CYPHER_MERGE_ACCESS_GROUP = """
UNWIND $batch AS row
MERGE (:AccessGroup {xml_id: row.xml_id})
"""

CYPHER_MERGE_RESTRICTED_TO = """
UNWIND $batch AS row
MATCH (m:Model {technical_name: row.model}), (g:AccessGroup {xml_id: row.group})
MERGE (m)-[r:RESTRICTED_TO {perms: row.perms}]->(g)
"""

CYPHER_SET_UNGATED = """
UNWIND $batch AS row
MATCH (m:Model {technical_name: row.model})
SET m.has_ungated_access_rule = true
"""

CYPHER_MERGE_VIEW = """
UNWIND $batch AS row
MERGE (v:View {xml_id: row.xml_id})
SET v.model = row.model, v.view_type = row.view_type, v.is_external = row.is_external
"""

CYPHER_MERGE_DECLARES_VIEW = """
UNWIND $batch AS row
MATCH (mod:Module {name: row.module}), (v:View {xml_id: row.view})
MERGE (mod)-[:DECLARES_VIEW]->(v)
"""

CYPHER_MERGE_INHERITS_VIEW = """
UNWIND $batch AS row
MATCH (child:View {xml_id: row.child}), (base:View {xml_id: row.base})
MERGE (child)-[:INHERITS_VIEW]->(base)
"""

CYPHER_MERGE_REFERENCES_FIELD = """
UNWIND $batch AS row
MATCH (v:View {xml_id: row.view}), (f:Field {model: row.model, name: row.field_name})
MERGE (v)-[:REFERENCES_FIELD]->(f)
"""

# Phase 36 S13.5 gap fix -- (:Field)-[:DEPENDS_ON]->(:Field), same-model only
# (see build_field_depends_rows's own docstring for why). Plain MATCH-both-
# sides (not OPTIONAL) is correct here: both field_key and depends_on_key are
# derived from field_rows itself (the field owning the dep, and a
# same-model sibling field name), so the depends_on side is only missing
# from the graph in the rare case model_cards.jsonl's FIELDS: grammar names
# a dependency that final_module_graph.jsonl's own field list doesn't carry
# (the known field_source_mismatches gap already tracked elsewhere in this
# script) -- a real MATCH miss there correctly no-ops that one row rather
# than fabricating a Field node for a name that may not be real.
CYPHER_MERGE_FIELD_DEPENDS_ON = """
UNWIND $batch AS row
MATCH (a:Field {key: row.field_key}), (b:Field {key: row.depends_on_key})
MERGE (a)-[:DEPENDS_ON]->(b)
"""

# Phase 36 S13.2 gap fix -- (:Module)-[:REOPENS]->(:Model). MERGEs the
# :Model side too (ON CREATE, matching CYPHER_MERGE_EXTENDS's own foreign-
# stub discipline) since a module's `reopens`/`models[].inherits` entry can
# legitimately name a model this migration has no other record of yet
# (e.g. a core Odoo model no loaded module's own final_module_graph.jsonl
# record DEFINES, only reopens).
CYPHER_MERGE_REOPENS = """
UNWIND $batch AS row
MATCH (mod:Module {name: row.module})
MERGE (mdl:Model {technical_name: row.model})
ON CREATE SET mdl.is_foreign = true, mdl.has_ungated_access_rule = false
MERGE (mod)-[:REOPENS]->(mdl)
"""

# Phase 36 S13.3 gap fix -- (:Module)-[:DECLARES_ACCESS_RULE]->(:Model),
# module attribution for access rules (get_blast_radius's third leg).
CYPHER_MERGE_DECLARES_ACCESS_RULE = """
UNWIND $batch AS row
MATCH (mod:Module {name: row.module})
MERGE (mdl:Model {technical_name: row.model})
ON CREATE SET mdl.is_foreign = true, mdl.has_ungated_access_rule = false
MERGE (mod)-[:DECLARES_ACCESS_RULE]->(mdl)
"""

# Phase 36 S13.3 gap fix -- (:View)-[:TARGETS]->(:Model), promoting the
# existing `View.model` PROPERTY (set by CYPHER_MERGE_VIEW above) to a real,
# traversable edge -- get_blast_radius's second leg ("a view targeting this
# model"). OPTIONAL MATCH + FOREACH-guarded MERGE, matching
# CYPHER_MERGE_UNRESOLVED_ITEM's own established discipline for a foreign
# key that is not guaranteed to already exist as a real :Model node (a
# view's own `model` field, per parser.py's parse_views_dir, is taken
# verbatim from the XML and is not cross-checked against known_model_names
# at parse time -- only build_view_rows's own unresolved-item detection does
# that, informationally, without blocking the row).
CYPHER_MERGE_VIEW_TARGETS = """
UNWIND $batch AS row
MATCH (v:View {xml_id: row.xml_id})
OPTIONAL MATCH (m:Model {technical_name: row.model})
FOREACH (_ IN CASE WHEN m IS NULL THEN [] ELSE [1] END |
  MERGE (v)-[:TARGETS]->(m)
)
"""

# S3.1 constraints/indexes -- idempotent, safe to run against a shared
# instance (schema-only, no data write, no MATCH). :View's constraint is
# added here even though S3.1's literal Cypher block (written before S0.9a)
# does not list it -- see the ODOO_OWNED_LABELS comment above.
CONSTRAINT_STATEMENTS: list[str] = [
    "CREATE CONSTRAINT module_name_unique IF NOT EXISTS FOR (m:Module) REQUIRE m.name IS UNIQUE",
    "CREATE CONSTRAINT model_technical_name_unique IF NOT EXISTS FOR (m:Model) REQUIRE m.technical_name IS UNIQUE",
    "CREATE CONSTRAINT field_key_unique IF NOT EXISTS FOR (f:Field) REQUIRE f.key IS UNIQUE",
    "CREATE CONSTRAINT viewtype_name_unique IF NOT EXISTS FOR (v:ViewType) REQUIRE v.name IS UNIQUE",
    "CREATE CONSTRAINT unresolved_key_unique IF NOT EXISTS FOR (u:UnresolvedItem) REQUIRE u.key IS UNIQUE",
    "CREATE CONSTRAINT import_metadata_id_unique IF NOT EXISTS FOR (i:ImportMetadata) REQUIRE i.id IS UNIQUE",
    "CREATE CONSTRAINT access_group_xml_id_unique IF NOT EXISTS FOR (a:AccessGroup) REQUIRE a.xml_id IS UNIQUE",
    "CREATE CONSTRAINT view_xml_id_unique IF NOT EXISTS FOR (v:View) REQUIRE v.xml_id IS UNIQUE",
    "CREATE INDEX field_model_lookup IF NOT EXISTS FOR (f:Field) ON (f.model, f.name)",
    "CREATE INDEX field_model_ttype_lookup IF NOT EXISTS FOR (f:Field) ON (f.model, f.ttype)",
    "CREATE INDEX unresolved_model_lookup IF NOT EXISTS FOR (u:UnresolvedItem) ON (u.model)",
]


LABEL_KEY_PROPERTY: dict[str, str] = {
    "Module": "name",
    "Model": "technical_name",
    "Field": "key",
    "ViewType": "name",
    "UnresolvedItem": "key",
    "ImportMetadata": "id",
    "AccessGroup": "xml_id",
    "View": "xml_id",
}


def setup_constraints(driver: Any, database: str) -> list[str]:
    """S3.1. Additive, idempotent (IF NOT EXISTS everywhere) -- never a
    MATCH, never touches existing data, safe to run against a shared
    instance (constraints are per-label; this migration's 8 labels do not
    overlap Nexo's CanonicalEntity/Relation/Episode/FlaggedEdge/
    SameAsCandidate/WrittenNodeKey labels)."""
    applied = []
    with driver.session(database=database) as session:
        for stmt in CONSTRAINT_STATEMENTS:
            session.run(stmt)
            applied.append(stmt)
    return applied


# ==========================================================================
# S2.2 step 4 -- mode orchestration: bootstrap / run-mutex / quiescence flag
# / pre-wipe backup / label-scoped batched wipe with sentinel check
# ==========================================================================
from paths import KNOWLEDGE_GRAPH_DATA_PATH

LOCK_DIR = Path(os.environ.get("OMA_ODOO_KG_LOCK_DIR", str(KNOWLEDGE_GRAPH_DATA_PATH / "locks")))
BACKUP_DIR = Path(
    os.environ.get("OMA_ODOO_KG_BACKUP_DIR", str(KNOWLEDGE_GRAPH_DATA_PATH / "backups"))
)
AUDIT_DIR_DEFAULT = Path(__file__).resolve().parent.parent / "import_audit"

MODE_LOCK_NAMES = {
    "full": "odoo_kg_import.lock",
    "incremental": "odoo_kg_incremental.lock",
    "restore": "odoo_kg_restore.lock",
}


class RunMutexHeld(RuntimeError):
    pass


class ForeignDataDetected(RuntimeError):
    pass


class ModeLock:
    """S2.2 step 4's run-mutex: exclusive, non-blocking OS file lock, held
    for the entire duration of the run, checks all three modes' locks
    (not just its own)."""

    def __init__(self, mode: str, lock_dir: Path = LOCK_DIR) -> None:
        self.mode = mode
        self.lock_dir = lock_dir
        self._fh = None

    def __enter__(self) -> "ModeLock":
        self.lock_dir.mkdir(parents=True, exist_ok=True)
        for other_mode, other_name in MODE_LOCK_NAMES.items():
            path = self.lock_dir / other_name
            if not path.exists():
                continue
            try:
                fh = open(path, "a+")
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
                fh.close()
            except OSError as exc:
                raise RunMutexHeld(
                    f"cannot start --mode {self.mode}: {other_mode} mode's lock "
                    f"({path}) is held by another process"
                ) from exc

        own_path = self.lock_dir / MODE_LOCK_NAMES[self.mode]
        self._fh = open(own_path, "a+")
        try:
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self._fh.close()
            raise RunMutexHeld(
                f"cannot start --mode {self.mode}: lock file {own_path} is already held "
                "(check for a stuck/still-running process before retrying)"
            ) from exc
        self._fh.write(f"{os.getpid()} {utc_now_iso()}\n")
        self._fh.flush()
        return self

    def __exit__(self, *exc_info: Any) -> None:
        if self._fh is not None:
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
            self._fh.close()


def set_import_in_progress(driver: Any, database: str, import_batch_id: str, mode: str) -> None:
    """MERGE, not MATCH -- see clear_import_in_progress's docstring for why
    this node cannot be assumed to already exist."""
    cypher = """
    MERGE (i:ImportMetadata {id: $id})
    ON CREATE SET i.oma_odoo_kg_import = true
    SET i.import_in_progress = true, i.import_started_at = $started_at,
        i.import_batch_id = $batch_id, i.import_mode = $mode
    """
    with driver.session(database=database) as session:
        session.run(
            cypher, id=IMPORT_METADATA_ID, started_at=utc_now_iso(), batch_id=import_batch_id, mode=mode
        )


def clear_import_in_progress(driver: Any, database: str) -> None:
    """MERGE, not MATCH. Real finding (2026-08-13): :ImportMetadata carries
    the label 'ImportMetadata', which IS one of ODOO_OWNED_LABELS -- so
    --mode full's label_scoped_wipe() deletes it along with everything
    else, and nothing in write_import_plan()'s step list recreates it (it
    is not part of ImportPlan at all). A MATCH-only clear silently no-ops
    against a wiped/never-bootstrapped node, permanently breaking the
    quiescence-flag contract every graph_queries.py read relies on -- this
    was caught by inspecting the real post-migration graph, not by the
    (driver-mocked) test suite, which cannot see this interaction."""
    cypher = """
    MERGE (i:ImportMetadata {id: $id})
    ON CREATE SET i.oma_odoo_kg_import = true
    SET i.import_in_progress = false, i.import_completed_at = $completed_at
    """
    with driver.session(database=database) as session:
        session.run(cypher, id=IMPORT_METADATA_ID, completed_at=utc_now_iso())


def bootstrap_if_needed(driver: Any, database: str, allow_bootstrap: bool) -> bool:
    """S2.2 step 4's bootstrap sub-step. Returns True if bootstrap ran.

    "Empty" is scoped to ODOO_OWNED_LABELS, not a bare `MATCH (n)` count --
    this instance is shared with Nexo's own live data (S3.3a), so a global
    node count is never 0 in practice on first run. A bare-count check
    would make bootstrap_if_needed always raise on this shared database,
    permanently blocking the very first import even with
    --bootstrap-empty-db passed, and would also mean :ImportMetadata is
    never created (silently no-op'd by set_import_in_progress's MATCH),
    breaking the quiescence-flag contract every other query in this system
    relies on."""
    with driver.session(database=database) as session:
        count = session.run(
            "MATCH (n) WHERE any(l IN labels(n) WHERE l IN $labels) RETURN count(n) AS c",
            labels=ODOO_OWNED_LABELS,
        ).single()["c"]
        has_metadata = session.run(
            "MATCH (i:ImportMetadata {id: $id}) RETURN count(i) AS c", id=IMPORT_METADATA_ID
        ).single()["c"]
    is_empty = count == 0 and has_metadata == 0
    if not is_empty:
        return False
    if not allow_bootstrap:
        raise RuntimeError(
            "database is empty and untagged; pass --bootstrap-empty-db to explicitly opt in "
            "(bootstrap is never inferred silently)"
        )
    with driver.session(database=database) as session:
        session.run(
            """
            MERGE (i:ImportMetadata {id: $id})
            SET i.oma_odoo_kg_import = true, i.bootstrapped_at = $now, i.import_in_progress = false
            """,
            id=IMPORT_METADATA_ID,
            now=utc_now_iso(),
        )
    return True


def foreign_data_sentinel_check(driver: Any, database: str) -> list[str]:
    """S3.3a mandatory pre-wipe sentinel check. Returns the distinct
    foreign label sets found (empty list == clean)."""
    cypher = """
    MATCH (n) WHERE NOT any(l IN labels(n) WHERE l IN $labels)
    RETURN DISTINCT labels(n) AS labels
    """
    with driver.session(database=database) as session:
        result = session.run(cypher, labels=ODOO_OWNED_LABELS)
        return [row["labels"] for row in result]


def run_export_backup(driver: Any, database: str, out_path: Path) -> None:
    """S2.2 step 4 / S3.3: pre-wipe backup.

    Real finding (2026-08-13, reported rather than silently worked around):
    this Neo4j instance has no APOC plugin installed and no `cypher-shell`
    binary on this host (confirmed via `SHOW PROCEDURES` and `which
    cypher-shell`), so the originally-authored apoc.export.cypher.all() +
    cypher-shell approach cannot run at all against this real deployment.
    Installing APOC would be a consequential, shared-instance infra change
    outside this task's scope, so this instead does a pure Python/driver
    JSON export: every node carrying at least one ODOO_OWNED_LABELS label,
    plus every relationship whose endpoints are both such nodes. Restored
    by restore_from_json_backup() below via the same natural keys the
    unique constraints already enforce (LABEL_KEY_PROPERTY), not elementId
    (which is not stable across a delete+recreate)."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with driver.session(database=database) as session:
        node_rows = [
            {"labels": r["labels"], "props": r["props"]}
            for r in session.run(
                "MATCH (n) WHERE any(l IN labels(n) WHERE l IN $labels) "
                "RETURN labels(n) AS labels, properties(n) AS props",
                labels=ODOO_OWNED_LABELS,
            )
        ]
        rel_rows = [
            {
                "start_labels": r["start_labels"],
                "start_props": r["start_props"],
                "end_labels": r["end_labels"],
                "end_props": r["end_props"],
                "type": r["type"],
                "props": r["props"],
            }
            for r in session.run(
                "MATCH (a)-[rel]->(b) "
                "WHERE any(l IN labels(a) WHERE l IN $labels) "
                "AND any(l IN labels(b) WHERE l IN $labels) "
                "RETURN labels(a) AS start_labels, properties(a) AS start_props, "
                "labels(b) AS end_labels, properties(b) AS end_props, "
                "type(rel) AS type, properties(rel) AS props",
                labels=ODOO_OWNED_LABELS,
            )
        ]
    payload = {
        "exported_at": utc_now_iso(),
        "odoo_owned_labels": ODOO_OWNED_LABELS,
        "nodes": node_rows,
        "relationships": rel_rows,
    }
    out_path.write_text(json.dumps(payload, default=str))
    if out_path.stat().st_size == 0:
        raise RuntimeError(f"pre-wipe export {out_path} is empty after JSON export")


def _primary_owned_label(labels: list[str]) -> str:
    for l in labels:
        if l in LABEL_KEY_PROPERTY:
            return l
    raise RuntimeError(f"no ODOO_OWNED_LABELS label with a known key property in {labels}")


def restore_from_json_backup(driver: Any, database: str, in_path: Path) -> None:
    """Restores a run_export_backup() JSON snapshot via MERGE on each
    node's natural unique key (LABEL_KEY_PROPERTY), never elementId."""
    payload = json.loads(in_path.read_text())
    with driver.session(database=database) as session:
        for n in payload["nodes"]:
            label = _primary_owned_label(n["labels"])
            key_prop = LABEL_KEY_PROPERTY[label]
            session.run(
                f"MERGE (n:{label} {{{key_prop}: $key}}) SET n = $props",
                key=n["props"][key_prop],
                props=n["props"],
            )
        for r in payload["relationships"]:
            start_label = _primary_owned_label(r["start_labels"])
            end_label = _primary_owned_label(r["end_labels"])
            start_key_prop = LABEL_KEY_PROPERTY[start_label]
            end_key_prop = LABEL_KEY_PROPERTY[end_label]
            rel_type = r["type"]
            session.run(
                f"MATCH (a:{start_label} {{{start_key_prop}: $start_key}}), "
                f"(b:{end_label} {{{end_key_prop}: $end_key}}) "
                f"MERGE (a)-[rel:{rel_type}]->(b) SET rel = $props",
                start_key=r["start_props"][start_key_prop],
                end_key=r["end_props"][end_key_prop],
                props=r["props"],
            )


def label_scoped_wipe(driver: Any, database: str) -> None:
    """S3.3a: batched, label-scoped wipe -- never a bare MATCH (n).

    Real finding (2026-08-13): no APOC on this instance (see
    run_export_backup's docstring), so apoc.periodic.iterate cannot run.
    Replaced with an equivalent plain-Cypher batched loop, driven from
    Python so no server-side procedure is required -- same batch size,
    same label-scoping, same DETACH DELETE semantics."""
    cypher = """
    MATCH (n) WHERE any(l IN labels(n) WHERE l IN $odoo_owned_labels)
    WITH n LIMIT 1000
    DETACH DELETE n
    RETURN count(n) AS deleted
    """
    total = 0
    with driver.session(database=database) as session:
        while True:
            deleted = session.run(cypher, odoo_owned_labels=ODOO_OWNED_LABELS).single()["deleted"]
            total += deleted
            if deleted == 0:
                break
    logger.info("label-scoped wipe deleted %d nodes", total)


# ==========================================================================
# Orchestration
# ==========================================================================
@dataclass
class ImportPlan:
    """All node/edge rows this run will write, pre-built and pure (no I/O)
    so the plan itself is unit-testable without a driver."""

    module_rows: list[dict]
    depends_on_rows: list[dict]
    model_rows: list[dict]
    defines_rows: list[dict]
    extends_rows: list[dict]
    field_rows: list[dict]
    field_source_mismatches: list[dict]
    has_field_rows: list[dict]
    relates_to_rows: list[dict]
    view_type_rows: list[dict]
    has_view_type_rows: list[dict]
    extends_field_rows: list[dict]
    unresolved_item_rows: list[dict]
    access_group_rows: list[dict]
    restricted_to_rows: list[dict]
    ungated_model_names: set[str]
    foreign_model_stub_rows: list[dict]
    view_rows: list[dict]
    declares_view_rows: list[dict]
    inherits_view_rows: list[dict]
    references_field_rows: list[dict]
    unresolved_view_field_ref_rows: list[dict]
    # Phase 36 S13 additions
    reopens_rows: list[dict]
    declares_access_rule_rows: list[dict]
    field_depends_rows: list[dict]
    field_depends_skipped_dotted_count: int
    view_targets_rows: list[dict]


def build_import_plan(sd: SourceData, incremental_module_scope: str | None = None) -> ImportPlan:
    module_rows, depends_on_rows = build_module_rows(sd)
    model_rows, defines_rows = build_model_rows(sd, incremental_module_scope=incremental_module_scope)
    known_model_names = {r["technical_name"] for r in model_rows}
    extends_rows = build_extends_rows(sd)
    reopens_rows = build_reopens_rows(sd)
    declares_access_rule_rows = build_declares_access_rule_rows(sd)
    field_rows, mismatches = build_field_rows(sd)
    has_field_rows, relates_to_rows = build_has_field_and_relates_to_rows(field_rows)
    field_depends_rows, field_depends_skipped = build_field_depends_rows(field_rows)
    view_type_rows, has_view_type_rows, _ = build_view_type_rows(sd)
    extends_field_rows = build_extends_field_rows(sd)
    unresolved_item_rows = build_unresolved_item_rows(sd)
    access_group_rows, restricted_to_rows, ungated_names, foreign_stub_rows = build_access_group_rows(
        sd, known_model_names
    )
    known_model_names |= {r["technical_name"] for r in foreign_stub_rows}
    known_field_pairs = {(r["model"], r["name"]) for r in field_rows}
    view_rows, declares_view_rows, inherits_view_rows, references_field_rows, unresolved_view_rows = (
        build_view_rows(sd, known_model_names, known_field_pairs)
    )
    view_targets_rows = [
        {"xml_id": v["xml_id"], "model": v["model"]} for v in view_rows if v.get("model")
    ]
    if field_depends_skipped:
        logger.info(
            "build_field_depends_rows: skipped %d dotted (cross-model) @api.depends() "
            "entries -- same-model DEPENDS_ON edges only, see function docstring",
            field_depends_skipped,
        )
    return ImportPlan(
        module_rows=module_rows,
        depends_on_rows=depends_on_rows,
        model_rows=model_rows,
        defines_rows=defines_rows,
        extends_rows=extends_rows,
        field_rows=field_rows,
        field_source_mismatches=mismatches,
        has_field_rows=has_field_rows,
        relates_to_rows=relates_to_rows,
        view_type_rows=view_type_rows,
        has_view_type_rows=has_view_type_rows,
        extends_field_rows=extends_field_rows,
        unresolved_item_rows=unresolved_item_rows,
        access_group_rows=access_group_rows,
        restricted_to_rows=restricted_to_rows,
        ungated_model_names=ungated_names,
        foreign_model_stub_rows=foreign_stub_rows,
        view_rows=view_rows,
        declares_view_rows=declares_view_rows,
        inherits_view_rows=inherits_view_rows,
        references_field_rows=references_field_rows,
        unresolved_view_field_ref_rows=unresolved_view_rows,
        reopens_rows=reopens_rows,
        declares_access_rule_rows=declares_access_rule_rows,
        field_depends_rows=field_depends_rows,
        field_depends_skipped_dotted_count=field_depends_skipped,
        view_targets_rows=view_targets_rows,
    )


def write_import_plan(driver: Any, database: str, plan: ImportPlan, audit: AuditLog) -> None:
    """S2.2 step 1's strict dependency load order."""
    steps: list[tuple[str, str, list[dict]]] = [
        ("modules", CYPHER_MERGE_MODULE, plan.module_rows),
        ("depends_on", CYPHER_MERGE_DEPENDS_ON, plan.depends_on_rows),
        ("models", CYPHER_MERGE_MODEL, plan.model_rows),
        ("foreign_model_stubs", CYPHER_MERGE_MODEL, plan.foreign_model_stub_rows),
        ("defines", CYPHER_MERGE_DEFINES, plan.defines_rows),
        ("extends", CYPHER_MERGE_EXTENDS, plan.extends_rows),
        ("reopens", CYPHER_MERGE_REOPENS, plan.reopens_rows),
        ("declares_access_rule", CYPHER_MERGE_DECLARES_ACCESS_RULE, plan.declares_access_rule_rows),
        ("fields", CYPHER_MERGE_FIELD, plan.field_rows),
        ("has_field", CYPHER_MERGE_HAS_FIELD, plan.has_field_rows),
        ("relates_to", CYPHER_MERGE_RELATES_TO, plan.relates_to_rows),
        ("field_depends_on", CYPHER_MERGE_FIELD_DEPENDS_ON, plan.field_depends_rows),
        ("view_types", CYPHER_MERGE_VIEWTYPE, plan.view_type_rows),
        ("has_view_type", CYPHER_MERGE_HAS_VIEW_TYPE, plan.has_view_type_rows),
        ("extends_field", CYPHER_MERGE_EXTENDS_FIELD, plan.extends_field_rows),
        ("unresolved_items", CYPHER_MERGE_UNRESOLVED_ITEM, plan.unresolved_item_rows),
        ("access_groups", CYPHER_MERGE_ACCESS_GROUP, plan.access_group_rows),
        ("restricted_to", CYPHER_MERGE_RESTRICTED_TO, plan.restricted_to_rows),
        (
            "ungated_access",
            CYPHER_SET_UNGATED,
            [{"model": m} for m in sorted(plan.ungated_model_names)],
        ),
        ("views", CYPHER_MERGE_VIEW, plan.view_rows),
        ("view_targets", CYPHER_MERGE_VIEW_TARGETS, plan.view_targets_rows),
        ("declares_view", CYPHER_MERGE_DECLARES_VIEW, plan.declares_view_rows),
        ("inherits_view", CYPHER_MERGE_INHERITS_VIEW, plan.inherits_view_rows),
        ("references_field", CYPHER_MERGE_REFERENCES_FIELD, plan.references_field_rows),
        ("unresolved_view_refs", CYPHER_MERGE_UNRESOLVED_ITEM, plan.unresolved_view_field_ref_rows),
    ]
    for name, cypher, rows in steps:
        if not rows:
            continue
        writer = Neo4jWriter(driver, database, audit, name)
        writer.run_batched(cypher, rows)

    if plan.field_source_mismatches:
        pct = len(plan.field_source_mismatches) / max(len(plan.field_rows), 1) * 100
        for m in plan.field_source_mismatches:
            audit.record(step="field_source_mismatch", **m)
        if pct > 1.0:
            audit.record(
                step="field_source_mismatch_summary",
                count=len(plan.field_source_mismatches),
                pct=pct,
                status="ESCALATED",
            )


def run_import(
    mode: str,
    base_dir: Path,
    driver: Any,
    database: str,
    bootstrap_empty_db: bool = False,
    i_acknowledge_shared_database: bool = False,
    restore_file: Path | None = None,
    module_scope: str | None = None,
    audit_dir: Path = AUDIT_DIR_DEFAULT,
) -> str:
    """Top-level orchestration implementing S2.2 step 4's mutex / bootstrap
    / quiescence-flag / pre-wipe-backup / label-scoped-wipe sequence for
    --mode full|incremental|restore. Returns the import_batch_id."""
    if mode not in MODE_LOCK_NAMES:
        raise ValueError(f"unknown mode {mode!r}")

    import_batch_id = str(uuid.uuid4())
    audit = AuditLog(audit_dir / f"{import_batch_id}.jsonl")

    with ModeLock(mode):
        bootstrapped = bootstrap_if_needed(driver, database, bootstrap_empty_db)

        if not bootstrapped:
            set_import_in_progress(driver, database, import_batch_id, mode)

        try:
            if mode in ("full", "restore"):
                foreign = foreign_data_sentinel_check(driver, database)
                if foreign and not i_acknowledge_shared_database:
                    raise ForeignDataDetected(
                        f"foreign (non-Odoo-KG) labels found on this database: {foreign}. "
                        "Refusing to wipe. Pass --i-acknowledge-shared-database to override "
                        "after reviewing this is a genuinely reviewed, benign exception."
                    )
                backup_name = "pre_restore" if mode == "restore" else "pre_full_reload"
                out_path = BACKUP_DIR / f"{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}_{backup_name}.json"
                run_export_backup(driver, database, out_path)
                audit.record(step="pre_wipe_backup", path=str(out_path))
                label_scoped_wipe(driver, database)

            if mode == "restore":
                if restore_file is None:
                    raise ValueError("--mode restore requires a file path")
                restore_from_json_backup(driver, database, restore_file)
            else:
                sd = load_source_data(base_dir)
                plan = build_import_plan(
                    sd, incremental_module_scope=module_scope if mode == "incremental" else None
                )
                write_import_plan(driver, database, plan, audit)

            clear_import_in_progress(driver, database)
            audit.record(step="run_complete", mode=mode, import_batch_id=import_batch_id)
        except Exception:
            audit.record(step="run_failed", mode=mode, import_batch_id=import_batch_id)
            # Real bug found and fixed live (2026-08-13): this except block
            # re-raised without ever clearing import_in_progress, so any
            # incremental-sync failure (e.g. ModelReconciliationError from a
            # module that just introduced a new model) permanently stuck the
            # whole graph in "import in progress", silently blocking every
            # downstream query in graph_queries.py that guards on
            # `NOT in_progress` -- confirmed live via
            # oma_i_want_a_full_8c18cc48's real ModelReconciliationError.
            clear_import_in_progress(driver, database)
            raise

    return import_batch_id


# ==========================================================================
# CLI
# ==========================================================================
def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["full", "incremental", "restore"])
    parser.add_argument("--setup-constraints", action="store_true")
    parser.add_argument("--bootstrap-empty-db", action="store_true")
    parser.add_argument("--i-acknowledge-shared-database", action="store_true")
    parser.add_argument("--restore-file", type=Path)
    parser.add_argument("--module", help="scope for --mode incremental (S0.9c)")
    parser.add_argument(
        "--base-dir",
        type=Path,
        default=KNOWLEDGE_GRAPH_DATA_PATH,
        help="directory containing final_module_graph.jsonl etc.",
    )
    args = parser.parse_args(argv)

    if not args.setup_constraints and not args.mode:
        parser.error("pass --setup-constraints and/or --mode {full,incremental,restore}")

    from infra.neo4j_client import get_neo4j_driver  # noqa: E402 -- see sys.path bootstrap above
    from infra.settings import load_neo4j_settings

    settings = load_neo4j_settings()
    driver = get_neo4j_driver()

    if args.setup_constraints:
        applied = setup_constraints(driver, settings.database)
        logger.info("applied %d constraint/index statements", len(applied))

    if args.mode:
        batch_id = run_import(
            mode=args.mode,
            base_dir=args.base_dir,
            driver=driver,
            database=settings.database,
            bootstrap_empty_db=args.bootstrap_empty_db,
            i_acknowledge_shared_database=args.i_acknowledge_shared_database,
            restore_file=args.restore_file,
            module_scope=args.module,
        )
        logger.info("import complete, batch_id=%s", batch_id)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
