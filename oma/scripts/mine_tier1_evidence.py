"""P7 (Phase 30, Phase D, §7.2b, Tier 1): "mine what's already been run" --
before generating or running a single new synthetic pairwise task, walk the
real historical task record and extract which dimension-value PAIRS (from
dimension_table.json, §7.1) already co-occurred in a real, PASSED historical
task. Those pairs are covered for free, at zero additional
generation/sandbox cost.

Two extraction sources, in order of trustworthiness (2026-07-31, corrected
after a first pass proved the first alone is not enough -- see below):

1. **Real generated code** (primary, added 2026-07-31): via
   `tools_odoo.module_dev.vcs.read_last_validated_commit(task_id)`, the
   same, already-existing, already-proven function Build's own
   `prior_files` reads from -- the real final committed module content on
   the task's own Gitea branch HEAD. Dimension values are detected by
   regex over the ACTUAL models.py/views.xml/security.xml/manifest.py
   content, not the human request text.
2. **Goal text** (fallback only, kept from the first pass): `task_created`
   events' `summary` field -- the clean, original, human-authored request.

**Why code, not just goal text, is the real source of truth (found live,
2026-07-31, before committing to a text-only miner):** a first-pass,
text-only version of this script found dimension_evidence for `mixin_type`
in ZERO of 341 real passed tasks -- not because none of them used a mixin,
but because Operator's plain-English requests describe business outcomes
("show this in the chatter"), not implementation mechanisms
("_inherit=['mail.thread']") -- that choice is the specialist's, and it is
real, verifiable, and present in the actual committed code every time it
was used. Mining only request text systematically undercounts true
historical coverage; this is why §7.2b's own text says "walk the real
commit history," not "read the goal text."

Every extractor below only fires on an unambiguous, explicit signal in the
actual source it reads -- no dimension value is ever assumed by default
(no silent "none" for automation_type, no silent "crud_only" for
security_scope just because nothing else matched) -- an undetected
dimension for a given task simply contributes no evidence for that
dimension. False coverage claims are far worse for this harness's purpose
than an honest under-count.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from itertools import combinations
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from infra.settings import load_postgres_settings  # noqa: E402
from tools_odoo.module_dev.vcs import VcsError, read_last_validated_commit  # noqa: E402

import psycopg2  # noqa: E402
import psycopg2.extras  # noqa: E402

from paths import COVERAGE_BASE_PATH

DIMENSION_TABLE_PATH = COVERAGE_BASE_PATH / "dimension_table.json"

# --- Real-code extractors (primary source) --------------------------------
# Operate on the concatenated real content of every file in a task's last
# validated Gitea commit. Grounded directly in patterns confirmed present
# in this project's own real generated modules throughout this session
# (e.g. `fields.Many2one(...)`, `_inherit = ['mail.thread']`,
# `model="ir.cron"`, `groups="...group..."` on a <button>/<field>).

_FIELD_TYPE_CODE_RE = re.compile(
    r"\bfields\.(Char|Text|Boolean|Integer|Float|Selection|Binary|Html|Image"
    r"|Monetary|Date|Datetime|Many2one|One2many|Many2many|Json|Reference)\("
)
_FIELD_TYPE_VALUE_MAP = {
    "char": "char", "text": "text", "boolean": "boolean", "integer": "integer",
    "float": "float", "selection": "selection", "binary": "binary",
    "html": "html", "image": "image", "monetary": "monetary", "date": "date",
    "datetime": "datetime", "many2one": "many2one", "one2many": "one2many",
    "many2many": "many2many", "json": "json_field", "reference": "reference_field",
}


def extract_field_type_from_code(content: str) -> set[str]:
    found: set[str] = set()
    for m in _FIELD_TYPE_CODE_RE.finditer(content):
        key = m.group(1).lower()
        if key in _FIELD_TYPE_VALUE_MAP:
            found.add(_FIELD_TYPE_VALUE_MAP[key])
    return found


def extract_model_kind_from_code(content: str) -> set[str]:
    found: set[str] = set()
    has_name = bool(re.search(r"^\s*_name\s*=", content, re.MULTILINE))
    has_inherit = bool(re.search(r"^\s*_inherit\s*=", content, re.MULTILINE))
    has_inherits = bool(re.search(r"^\s*_inherits\s*=", content, re.MULTILINE))
    if re.search(r"models\.AbstractModel\b", content):
        found.add("abstract_mixin")
    if re.search(r"models\.TransientModel\b", content):
        found.add("transient_model_wizard")
    if has_inherits:
        found.add("inherits_delegation")
    if has_inherit and not has_name:
        found.add("inherit_extend")
    if has_name:
        found.add("new_model")
    return found


def extract_view_type_from_code(content: str) -> set[str]:
    found: set[str] = set()
    if re.search(r"<kanban\b", content):
        found.add("kanban")
    if re.search(r"<tree\b|<list\b", content):
        found.add("list")
    if re.search(r"<form\b", content):
        found.add("form")
    if re.search(r"<search\b", content):
        found.add("search")
    if re.search(r"<calendar\b", content):
        found.add("calendar")
    if re.search(r"<graph\b|<pivot\b", content):
        found.add("pivot_graph")
    if re.search(r'type="qweb"', content):
        found.add("qweb_web_template")
    return found


def extract_security_scope_from_code(content: str) -> set[str]:
    found: set[str] = set()
    if re.search(r'model="ir\.rule"', content):
        found.add("record_rule")
    if re.search(r"<button\b[^>]*\bgroups=", content):
        found.add("button_group_restriction")
    if re.search(r"<field\b[^>]*\bgroups=", content):
        found.add("field_group_restriction")
    if re.search(r"id,name,model_id.*perm_read", content) or re.search(
        r"perm_read,perm_write,perm_create,perm_unlink", content
    ):
        found.add("crud_only")
    return found


def extract_automation_type_from_code(content: str) -> set[str]:
    found: set[str] = set()
    if re.search(r'model="ir\.cron"', content):
        found.add("cron")
    if re.search(r'model="base\.automation"', content):
        found.add("base_automation_trigger")
    return found


def extract_mixin_type_from_code(content: str) -> set[str]:
    found: set[str] = set()
    if "mail.thread" in content:
        found.add("mail_thread")
    if "mail.activity.mixin" in content:
        found.add("mail_activity_mixin")
    if "portal.mixin" in content:
        found.add("portal_mixin")
    return found


def extract_packaging_shape_from_code(content: str, manifest_depends: list[str]) -> set[str]:
    found: set[str] = set()
    non_core = [d for d in manifest_depends if d not in ("base", "web", "mail")]
    if len(non_core) == 0:
        found.add("single_new_module")
    elif len(non_core) == 1:
        found.add("extend_existing_module")
    else:
        found.add("cross_module_dependency")
    return found


def extract_output_surface_from_code(content: str) -> set[str]:
    found: set[str] = set()
    if re.search(r'model="ir\.actions\.report"', content):
        found.add("qweb_report")
    if re.search(r"\{\{\s*object\.\w+\.lang\s*\}\}", content):
        found.add("translated_string")
    if re.search(r"@http\.route\b|http\.route\(", content):
        found.add("external_http")
    if "mail.template" in content and "mail.thread" not in content:
        found.add("one_off_templated_email")
    return found


def _extract_manifest_depends(files: dict[str, str]) -> list[str]:
    for path, content in files.items():
        if path.endswith("__manifest__.py"):
            m = re.search(r"['\"]depends['\"]\s*:\s*\[([^\]]*)\]", content)
            if m:
                return re.findall(r"['\"]([\w.]+)['\"]", m.group(1))
    return []


def evidence_from_code(files: dict[str, str]) -> dict[str, set[str]]:
    all_content = "\n".join(files.values())
    depends = _extract_manifest_depends(files)
    return {
        "field_type": extract_field_type_from_code(all_content),
        "model_kind": extract_model_kind_from_code(all_content),
        "view_type": extract_view_type_from_code(all_content),
        "security_scope": extract_security_scope_from_code(all_content),
        "automation_type": extract_automation_type_from_code(all_content),
        "mixin_type": extract_mixin_type_from_code(all_content),
        "packaging_shape": extract_packaging_shape_from_code(all_content, depends),
        "output_surface": extract_output_surface_from_code(all_content),
    }


# --- Goal-text extractors (fallback only, kept from the first pass) -------

_FIELD_LINE_RE = re.compile(
    r"(?im)^\s*Fields?(?:\s*name)?:\s*.*?\(([A-Za-z0-9_]+)\)"
)


def extract_field_type_from_goal(goal: str) -> set[str]:
    found: set[str] = set()
    for m in _FIELD_LINE_RE.finditer(goal):
        key = m.group(1).lower()
        if key in _FIELD_TYPE_VALUE_MAP:
            found.add(_FIELD_TYPE_VALUE_MAP[key])
    return found


def extract_model_kind_from_goal(goal: str) -> set[str]:
    found: set[str] = set()
    low = goal.lower()
    if re.search(r"\(inherit\)|_inherit\b|inherit and extend|inherit_id", low):
        found.add("inherit_extend")
    if re.search(r"\bnew model\b|\(new model\)", low):
        found.add("new_model")
    return found


def evidence_from_goal(goal: str) -> dict[str, set[str]]:
    return {
        "field_type": extract_field_type_from_goal(goal),
        "model_kind": extract_model_kind_from_goal(goal),
        "view_type": set(),
        "security_scope": set(),
        "automation_type": set(),
        "mixin_type": set(),
        "packaging_shape": set(),
        "output_surface": set(),
    }


def load_supported_pairs(path: Path) -> set[tuple[str, str, str, str]]:
    data = json.loads(path.read_text())
    supported: dict[tuple[str, str], bool] = {}
    for dim_name, spec in data["dimensions"].items():
        for v in spec["values"]:
            supported[(dim_name, v["value"])] = bool(v["generation_supported"])
    dim_names = sorted(data["dimensions"].keys())
    pairs: set[tuple[str, str, str, str]] = set()
    for dim_a, dim_b in combinations(dim_names, 2):
        for (da, va), sa in supported.items():
            if da != dim_a or not sa:
                continue
            for (db, vb), sb in supported.items():
                if db != dim_b or not sb:
                    continue
                pairs.add((dim_a, va, dim_b, vb))
    return pairs


def fetch_real_task_records() -> list[dict]:
    pg = load_postgres_settings()
    conn = psycopg2.connect(pg.dsn)
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(
        "SELECT DISTINCT ON (task_id) task_id, summary AS goal "
        "FROM agent_memory_events WHERE event_type = 'task_created' "
        "ORDER BY task_id, id ASC"
    )
    goals = {row["task_id"]: row["goal"] for row in cur.fetchall() if row["task_id"]}

    cur.execute(
        "SELECT task_id, bool_or((detail->>'passed')::boolean) AS passed "
        "FROM agent_memory_events WHERE event_type = 'outcome' AND task_id IS NOT NULL "
        "GROUP BY task_id"
    )
    passed_by_task = {row["task_id"]: bool(row["passed"]) for row in cur.fetchall()}

    records = []
    for task_id, goal in goals.items():
        if task_id in passed_by_task:
            records.append(
                {"task_id": str(task_id), "goal": goal, "passed": passed_by_task[task_id]}
            )
    return records


def mine(records: list[dict], verbose: bool = False) -> tuple[
    dict[tuple[str, str, str, str], list[str]], dict[str, int]
]:
    """{pair: [task_id, ...]} for every pair evidenced by a real PASSED
    historical task, preferring real committed code over goal text.
    Also returns a small stats dict (n_code_fetched / n_code_failed /
    n_fallback_to_goal) for honest reporting on which source each task
    actually used.
    """
    evidence: dict[tuple[str, str, str, str], list[str]] = defaultdict(list)
    stats = {"n_code_fetched": 0, "n_code_failed": 0, "n_code_empty": 0}

    for i, rec in enumerate(records):
        if not rec["passed"]:
            continue
        task_id = rec["task_id"]
        evidenced: dict[str, set[str]] | None = None
        try:
            files = read_last_validated_commit(task_id)
        except VcsError as exc:
            files = None
            stats["n_code_failed"] += 1
            if verbose:
                print(f"  [warn] Gitea fetch failed for {task_id}: {exc}", file=sys.stderr)

        if files:
            evidenced = evidence_from_code(files)
            stats["n_code_fetched"] += 1
        else:
            # files is either None (read_last_validated_commit's own
            # documented "genuinely nothing committed yet" case -- a clean
            # return, not an exception) or an empty dict -- both real,
            # legitimate "no code to mine" outcomes, not failures.
            stats["n_code_empty"] += 1
            evidenced = evidence_from_goal(rec["goal"] or "")

        if verbose and (i + 1) % 25 == 0:
            print(f"  ...mined {i + 1}/{len(records)} tasks", file=sys.stderr)

        dim_value_pairs = [
            (dim, val) for dim, values in evidenced.items() for val in values
        ]
        for (dim_a, val_a), (dim_b, val_b) in combinations(dim_value_pairs, 2):
            if dim_a == dim_b:
                continue
            if dim_a > dim_b:
                dim_a, val_a, dim_b, val_b = dim_b, val_b, dim_a, val_a
            evidence[(dim_a, val_a, dim_b, val_b)].append(task_id)

    return evidence, stats


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dimension-table", type=Path, default=DIMENSION_TABLE_PATH)
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument(
        "--limit", type=int, default=None,
        help="Only mine the first N passed tasks (for a quick smoke test before a full run)",
    )
    args = parser.parse_args()

    supported_pairs = load_supported_pairs(args.dimension_table)
    records = fetch_real_task_records()
    passed_records = [r for r in records if r["passed"]]
    if args.limit:
        passed_records = passed_records[: args.limit]
    if args.verbose:
        print(
            f"Mining {len(passed_records)} real passed tasks (Gitea content, "
            "goal-text fallback)...",
            file=sys.stderr,
        )

    evidence, stats = mine(passed_records, verbose=args.verbose)

    covered_for_free = {p: tids for p, tids in evidence.items() if p in supported_pairs}
    still_needed = supported_pairs - set(covered_for_free.keys())

    result = {
        "generated_by": "scripts/mine_tier1_evidence.py",
        "extraction_source": "real Gitea-committed code (primary), goal text (fallback)",
        "n_real_tasks_considered": len(records),
        "n_real_tasks_passed": len(passed_records),
        "n_passed_tasks_code_fetched": stats["n_code_fetched"],
        "n_passed_tasks_code_fetch_failed": stats["n_code_failed"],
        "n_passed_tasks_code_empty_fell_back_to_goal": stats["n_code_empty"],
        "n_target_pairs_both_supported": len(supported_pairs),
        "n_pairs_covered_for_free_by_history": len(covered_for_free),
        "n_pairs_still_needing_tier2_or_3": len(still_needed),
        "covered_for_free": [
            {"pair": f"{a}:{av} x {b}:{bv}", "evidence_task_ids": sorted(set(tids))}
            for (a, av, b, bv), tids in sorted(covered_for_free.items())
        ],
        "still_needed_pairs": [
            f"{a}:{av} x {b}:{bv}" for a, av, b, bv in sorted(still_needed)
        ],
    }

    text = json.dumps(result, indent=2)
    if args.out:
        args.out.write_text(text)
        print(
            f"Tier 1: {len(covered_for_free)}/{len(supported_pairs)} generation-supported "
            f"pairs covered for free by {len(passed_records)} real passed historical tasks "
            f"({stats['n_code_fetched']} via real code, "
            f"{stats['n_code_failed'] + stats['n_code_empty']} fell back to goal text). "
            f"{len(still_needed)} pairs still need Tier 2/3. Wrote {args.out}"
        )
    else:
        print(text)


if __name__ == "__main__":
    main()
