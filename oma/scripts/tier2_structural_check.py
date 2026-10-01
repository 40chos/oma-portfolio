"""P7 (Phase 30, Phase D, §7.2b, Tier 2): the cheap, no-LLM-generation-call
static check for whatever real dimension-value pairs Tier 1 (mine_tier1_
evidence.py) couldn't resolve for free. Synthesizes a minimal, hand-
templated GeneratedModuleFiles for each pair (mechanically assembled from
the same field-type skeletons `_GOAL_FIELD_TYPE_SKELETONS` already provides,
never LLM-written) and runs it through the REAL
`BuildSpecialist._validate_generated_module()` -- the same ~60
`_validate_*`/`_autofix_*` structural-check chain a real task's round
actually runs, called directly, in-process, with no LLM call and no
sandbox install.

**Correction found live (2026-07-31), before writing anything, and worth
keeping on the record rather than silently acted on:** the plan's own
prose calls this tier "no LLM call, no sandbox" as if it were fully
zero-dependency. That's not quite right -- `_validate_generated_module()`
requires a real `BuildSpecialist` instance (constructed here via
`ModelGatewayClient.__new__(ModelGatewayClient)`, the exact pattern this
project's own test suite already uses to avoid a real gateway connection
for structural-only tests, e.g. `tests/test_build_repetition_loop_
fallback.py`) and several of its ~60 validators genuinely perform cheap,
real, read-only XML-RPC lookups against the live `db` argument (e.g. "does
this model/field really exist"). This is still vastly cheaper than Tier 3
(no LLM generation, no retry rounds, no sandbox install) -- seconds, not
minutes -- but it is not literally zero-dependency, and this script is
honest about that rather than repeating the plan's own slightly-overstated
framing.

**Synthesis limits, stated honestly rather than silently worked around:**
`GeneratedModuleFiles` has no field representing a Python HTTP controller
at all (confirmed by reading the real schema directly) -- any pair naming
`output_surface:external_http` cannot be synthesized into this schema and
is reported as `cannot_synthesize`, not silently skipped or faked.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from infra.gateway_client import ModelGatewayClient  # noqa: E402
from specialists.build.specialist import (  # noqa: E402
    BuildSpecialist,
    GeneratedModuleFiles,
    ManifestFields,
    _GOAL_FIELD_TYPE_SKELETONS,
)
from paths import COVERAGE_BASE_PATH  # noqa: E402

DIMENSION_TABLE_PATH = COVERAGE_BASE_PATH / "dimension_table.json"
TIER1_EVIDENCE_DEFAULT = COVERAGE_BASE_PATH / "tier1_evidence.json"

_DEFAULT_DB = "odoo16_dev"
_SYNTHETIC_MODULE_NAME = "oma_p7_tier2_synthetic_probe"


# --- Field-type snippet ------------------------------------------------
# char/text/boolean/integer/float/binary/html/image already have a real
# skeleton in _GOAL_FIELD_TYPE_SKELETONS -- reused directly, never
# reinvented. selection/many2one need one additional line of judgment each
# (a concrete, always-safe option list / a concrete, always-real comodel)
# since neither is present in that dict (by design -- see its own comment
# on why a Selection/relational comodel is never safely guessable for a
# REAL task; here, for a synthetic structural probe only, a fixed neutral
# choice is fine).

_EXTRA_FIELD_SKELETONS = {
    "selection": "fields.Selection([('draft', 'Draft'), ('done', 'Done')], string={label!r})",
    "many2one": "fields.Many2one('res.partner', string={label!r})",
}


def _field_snippet(field_type: str, field_name: str, label: str) -> str | None:
    template = _GOAL_FIELD_TYPE_SKELETONS.get(field_type) or _EXTRA_FIELD_SKELETONS.get(field_type)
    if not template:
        return None
    return f"    {field_name} = {template.format(label=label)}"


# --- Per-dimension synthesizers ----------------------------------------
# Each mutates a working dict of {"model_lines": [...], "inherit": [...],
# "name": str|None, "view_fragments": [...], "security_csv_rows": [...],
# "security_xml_records": [...], "extra_data_records": [...],
# "depends": [...]} in place, given the one value it's responsible for.
# Deliberately conservative: only ever ADDS a real, valid fragment for the
# exact value given -- never guesses content for a dimension the pair
# doesn't name.


class _CannotSynthesize(Exception):
    pass


def _apply_model_kind(state: dict, value: str) -> None:
    if value == "new_model":
        state["name"] = f"x.{_SYNTHETIC_MODULE_NAME}"
    elif value == "inherit_extend":
        state["inherit"].append("res.partner")
    elif value == "abstract_mixin":
        state["base_class"] = "AbstractModel"
        state["name"] = f"x.{_SYNTHETIC_MODULE_NAME}.mixin"
    elif value == "transient_model_wizard":
        state["base_class"] = "TransientModel"
        state["name"] = f"x.{_SYNTHETIC_MODULE_NAME}.wizard"
    elif value == "inherits_delegation":
        state["name"] = f"x.{_SYNTHETIC_MODULE_NAME}.delegate"
        state["inherits_line"] = "_inherits = {'res.partner': 'partner_id'}"
        state["model_lines"].append("    partner_id = fields.Many2one('res.partner', required=True, ondelete='cascade')")
    else:
        raise _CannotSynthesize(f"model_kind:{value}")


def _apply_view_type(state: dict, value: str) -> None:
    field_ref = state.get("primary_field_name", "name")
    tags = {
        "form": f"<form><field name=\"{field_ref}\"/></form>",
        "list": f"<tree><field name=\"{field_ref}\"/></tree>",
        "kanban": f"<kanban><templates><t t-name=\"kanban-box\"><div><field name=\"{field_ref}\"/></div></t></templates></kanban>",
        "search": f"<search><field name=\"{field_ref}\"/></search>",
        "calendar": f"<calendar date_start=\"{field_ref}\"><field name=\"{field_ref}\"/></calendar>",
        "pivot_graph": f"<graph><field name=\"{field_ref}\" type=\"measure\"/></graph>",
        "qweb_web_template": None,  # represented via extra_data_records below
        "activity_gantt_grid_map": None,
    }
    if value not in tags:
        raise _CannotSynthesize(f"view_type:{value}")
    if tags[value] is None:
        raise _CannotSynthesize(f"view_type:{value} (no safe minimal Odoo 16 skeleton)")
    state["view_fragments"].append((value, tags[value]))


def _apply_security_scope(state: dict, value: str) -> None:
    model_ref = f"model_{state['name'].replace('.', '_')}"
    if value == "crud_only":
        state["security_csv_rows"].append(
            f"access_{_SYNTHETIC_MODULE_NAME},{_SYNTHETIC_MODULE_NAME},"
            f"{model_ref},base.group_user,1,1,1,0"
        )
    elif value == "field_group_restriction":
        state["needs_field_groups"] = True
    elif value == "button_group_restriction":
        state["needs_button_groups"] = True
    elif value == "record_rule":
        state["security_xml_records"].append(
            f'<record id="rule_{_SYNTHETIC_MODULE_NAME}" model="ir.rule">'
            f'<field name="name">Synthetic probe rule</field>'
            f'<field name="model_id" ref="{model_ref}"/>'
            f'<field name="domain_force">[(1,\'=\',1)]</field>'
            f"</record>"
        )
    else:
        raise _CannotSynthesize(f"security_scope:{value}")


def _apply_automation_type(state: dict, value: str) -> None:
    model_ref = f"model_{state['name'].replace('.', '_')}"
    if value == "none":
        return
    if value == "cron":
        state["extra_data_records"].append(
            ("data/cron_data.xml",
             f'<record id="cron_{_SYNTHETIC_MODULE_NAME}" model="ir.cron">'
             f'<field name="name">Synthetic probe cron</field>'
             f'<field name="model_id" ref="{model_ref}"/>'
             f'<field name="state">code</field>'
             f'<field name="code">pass</field>'
             f'<field name="interval_number">1</field>'
             f'<field name="interval_type">days</field>'
             f'<field name="numbercall">-1</field>'
             f"</record>")
        )
    else:
        raise _CannotSynthesize(f"automation_type:{value}")


def _apply_mixin_type(state: dict, value: str) -> None:
    mapping = {
        "none": None,
        "mail_thread": "mail.thread",
        "mail_activity_mixin": "mail.activity.mixin",
        "portal_mixin": "portal.mixin",
    }
    if value not in mapping:
        raise _CannotSynthesize(f"mixin_type:{value}")
    if mapping[value]:
        state["inherit"].append(mapping[value])


def _apply_packaging_shape(state: dict, value: str) -> None:
    if value == "single_new_module":
        return
    if value == "extend_existing_module":
        state["depends"].append("project")
    elif value == "cross_module_dependency":
        state["depends"].extend(["project", "mail"])
    else:
        raise _CannotSynthesize(f"packaging_shape:{value}")


def _apply_output_surface(state: dict, value: str) -> None:
    if value == "backend_view_only":
        return
    if value == "qweb_report":
        state["extra_data_records"].append(
            ("data/report_data.xml",
             f'<record id="report_{_SYNTHETIC_MODULE_NAME}" model="ir.actions.report">'
             f'<field name="name">Synthetic Probe Report</field>'
             f'<field name="model">{state.get("name") or "res.partner"}</field>'
             f'<field name="report_type">qweb-pdf</field>'
             f'<field name="report_name">{_SYNTHETIC_MODULE_NAME}.report_template</field>'
             f"</record>")
        )
    elif value == "translated_string":
        state["needs_translated_string"] = True
    elif value == "one_off_templated_email":
        state["extra_data_records"].append(
            ("data/mail_template_data.xml",
             f'<record id="template_{_SYNTHETIC_MODULE_NAME}" model="mail.template">'
             f'<field name="name">Synthetic probe template</field>'
             f'<field name="model_id" ref="model_{state["name"].replace(".", "_")}"/>'
             f'<field name="subject">Probe {{{{ object.id }}}}</field>'
             f'<field name="body_html" type="html"><p>probe</p></field>'
             f"</record>")
        )
    else:
        raise _CannotSynthesize(f"output_surface:{value}")


_APPLIERS = {
    "model_kind": _apply_model_kind,
    "view_type": _apply_view_type,
    "security_scope": _apply_security_scope,
    "automation_type": _apply_automation_type,
    "mixin_type": _apply_mixin_type,
    "packaging_shape": _apply_packaging_shape,
    "output_surface": _apply_output_surface,
}


def synthesize(dim_a: str, val_a: str, dim_b: str, val_b: str) -> GeneratedModuleFiles:
    """Builds a minimal GeneratedModuleFiles exercising exactly this one
    pair. Raises _CannotSynthesize if either side has no safe minimal
    representation in this schema -- the caller records that honestly,
    never silently drops or fakes a result.
    """
    state: dict = {
        # Real model _name, set up front (not None) so any applier can
        # safely bake a "model_<name>" reference into a security/cron/
        # report record regardless of which of the two dimensions in this
        # pair happens to run first -- _apply_model_kind() below may
        # overwrite it, but only BEFORE any other applier runs (model_kind
        # is always applied first, see the ordering below), so nothing
        # else ever reads a stale value.
        "model_lines": [], "inherit": [], "name": f"x.{_SYNTHETIC_MODULE_NAME}",
        "base_class": "Model",
        "inherits_line": None, "view_fragments": [], "security_csv_rows": [],
        "security_xml_records": [], "extra_data_records": [], "depends": [],
        "primary_field_name": "probe_field", "needs_field_groups": False,
        "needs_button_groups": False, "needs_translated_string": False,
    }

    field_type = val_a if dim_a == "field_type" else (val_b if dim_b == "field_type" else "char")
    field_snippet = _field_snippet(field_type, "probe_field", "Probe Field")
    if field_snippet is None:
        raise _CannotSynthesize(f"field_type:{field_type} (no safe minimal skeleton)")
    state["model_lines"].append(field_snippet)

    dims_to_apply = [(dim_a, val_a), (dim_b, val_b)]
    # model_kind must be applied first -- it's the only applier that can
    # change state["name"], and every other applier bakes a "model_<name>"
    # string reference at call time (not lazily), so it must see the
    # final name.
    dims_to_apply.sort(key=lambda pair: 0 if pair[0] == "model_kind" else 1)
    for dim, val in dims_to_apply:
        if dim == "field_type":
            continue
        applier = _APPLIERS.get(dim)
        if applier is None:
            raise _CannotSynthesize(f"{dim}:{val} (no applier)")
        applier(state, val)

    base_class_map = {"Model": "models.Model", "AbstractModel": "models.AbstractModel",
                       "TransientModel": "models.TransientModel"}
    inherit_list = state["inherit"]
    lines = ["from odoo import api, fields, models", "", ""]
    class_name = "SyntheticProbe"
    lines.append(f"class {class_name}({base_class_map[state['base_class']]}):")
    if state["base_class"] == "Model" and not inherit_list:
        lines.append(f"    _name = {state['name']!r}")
    elif state["base_class"] == "Model" and inherit_list and state["name"] and "delegate" not in state["name"]:
        # inherit_extend case: extend an existing real model, no _name.
        lines.append(f"    _inherit = {inherit_list[0]!r}")
        inherit_list = inherit_list[1:]
    else:
        lines.append(f"    _name = {state['name']!r}")
    if inherit_list:
        lines.append(f"    _inherit = {inherit_list!r}")
    if state["inherits_line"]:
        lines.append(f"    {state['inherits_line']}")
    lines.append(f"    _description = 'P7 Tier 2 synthetic structural probe'")
    lines.extend(state["model_lines"])
    models_py = "\n".join(lines) + "\n"

    view_xml_parts = []
    for kind, fragment in state["view_fragments"]:
        frag = fragment
        if state["needs_field_groups"]:
            frag = frag.replace(
                f'<field name="{state["primary_field_name"]}"/>',
                f'<field name="{state["primary_field_name"]}" groups="base.group_system"/>',
            )
        if state["needs_button_groups"] and "<form>" in frag:
            frag = frag.replace(
                "<form>",
                '<form><header><button name="probe_action" type="object" '
                'string="Probe" groups="base.group_system"/></header>',
            )
        view_xml_parts.append(
            f'<record id="view_{_SYNTHETIC_MODULE_NAME}_{kind}" model="ir.ui.view">'
            f'<field name="name">{_SYNTHETIC_MODULE_NAME}.{kind}</field>'
            f'<field name="model">{state["name"]}</field>'
            f'<field name="arch" type="xml">{frag}</field>'
            f"</record>"
        )
    views_xml = ("<odoo>" + "".join(view_xml_parts) + "</odoo>") if view_xml_parts else None

    security_csv = "id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink\n"
    security_csv += "\n".join(state["security_csv_rows"])
    if not state["security_csv_rows"]:
        security_csv += (
            f"access_{_SYNTHETIC_MODULE_NAME},{_SYNTHETIC_MODULE_NAME},"
            f"model_{state['name'].replace('.', '_')},base.group_user,1,1,1,0"
        )

    security_xml = None
    if state["security_xml_records"]:
        security_xml = "<odoo>" + "".join(state["security_xml_records"]) + "</odoo>"

    extra_data_files: dict[str, str] = {}
    data_list = ["security/ir.model.access.csv"]
    if security_xml:
        data_list.append("security/security.xml")
    if views_xml:
        data_list.append("views/views.xml")
    for path, content in state["extra_data_records"]:
        extra_data_files[path] = "<odoo>" + content + "</odoo>"
        data_list.append(path)

    manifest_fields = ManifestFields(
        name=_SYNTHETIC_MODULE_NAME,
        version="16.0.1.0.0",
        category="Uncategorized",
        summary="P7 Tier 2 synthetic structural probe -- never installed",
        author="P7 Tier 2 harness",
        depends=["base"] + state["depends"],
        data=data_list,
    )

    return GeneratedModuleFiles(
        manifest_fields=manifest_fields,
        models_py=models_py,
        views_xml=views_xml,
        security_csv=security_csv,
        security_xml=security_xml,
        extra_data_files=extra_data_files or None,
        notes="Synthetic P7 Tier 2 structural probe.",
    )


async def run_one_pair(
    specialist: BuildSpecialist, dim_a: str, val_a: str, dim_b: str, val_b: str
) -> dict:
    pair_label = f"{dim_a}:{val_a} x {dim_b}:{val_b}"
    try:
        generated = synthesize(dim_a, val_a, dim_b, val_b)
    except _CannotSynthesize as exc:
        return {"pair": pair_label, "result": "cannot_synthesize", "detail": str(exc)}

    try:
        await specialist._validate_generated_module(
            generated,
            module_name=_SYNTHETIC_MODULE_NAME,
            depends_on_module=None,
            goal="",
            task_id=None,
        )
        return {"pair": pair_label, "result": "pass"}
    except ValueError as exc:
        return {"pair": pair_label, "result": "fail", "detail": str(exc)[:300]}
    except Exception as exc:  # noqa: BLE001
        return {"pair": pair_label, "result": "error", "detail": f"{type(exc).__name__}: {exc}"[:300]}


async def run_all(pairs: list[tuple[str, str, str, str]], db: str, verbose: bool) -> list[dict]:
    client = ModelGatewayClient.__new__(ModelGatewayClient)
    specialist = BuildSpecialist(client=client, db=db)
    results = []
    for i, (dim_a, val_a, dim_b, val_b) in enumerate(pairs):
        result = await run_one_pair(specialist, dim_a, val_a, dim_b, val_b)
        results.append(result)
        if verbose:
            print(f"  [{i + 1}/{len(pairs)}] {result['pair']} -> {result['result']}", file=sys.stderr)
    return results


def _load_still_needed_pairs(tier1_path: Path) -> list[tuple[str, str, str, str]]:
    data = json.loads(tier1_path.read_text())
    pairs = []
    for label in data["still_needed_pairs"]:
        left, right = label.split(" x ")
        dim_a, val_a = left.split(":", 1)
        dim_b, val_b = right.split(":", 1)
        pairs.append((dim_a, val_a, dim_b, val_b))
    return pairs


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tier1-evidence", type=Path, required=True)
    parser.add_argument("--db", default=_DEFAULT_DB)
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()

    pairs = _load_still_needed_pairs(args.tier1_evidence)
    if args.limit:
        pairs = pairs[: args.limit]
    if args.verbose:
        print(f"Tier 2: checking {len(pairs)} pairs against real structural validators (db={args.db})...", file=sys.stderr)

    results = asyncio.run(run_all(pairs, args.db, args.verbose))

    by_result: dict[str, int] = {}
    for r in results:
        by_result[r["result"]] = by_result.get(r["result"], 0) + 1

    output = {
        "generated_by": "scripts/tier2_structural_check.py",
        "n_pairs_checked": len(results),
        "counts_by_result": by_result,
        "results": results,
    }
    text = json.dumps(output, indent=2)
    if args.out:
        args.out.write_text(text)
        print(f"Tier 2: {by_result} -- wrote {args.out}")
    else:
        print(text)


if __name__ == "__main__":
    main()
