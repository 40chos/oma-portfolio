"""P7 (Phase 30, Phase D, §7.2): generates the minimum set of synthetic task
goals such that every pair of values across any two dimensions in
docs/architecture/oma-coverage-graph/dimension_table.json appears together in
at least one generated task -- pairwise coverage, not full enumeration.

Standard greedy pairwise algorithm (orthogonal-array-style), implemented
directly rather than shelling out to an external binary (PICT), per the
plan's own stated preference when an external dependency is undesirable.
Not novel research -- well understood: repeatedly build one candidate
assignment (one value per dimension) that covers as many still-uncovered
pairs as possible, record it, mark its pairs covered, repeat until every
pair is covered.

Each generated task is tagged with the exact dimension-pairs it specifically
covers (not just "some pairs, unspecified") and rendered as a plain-English
goal string, mechanically assembled from the dimension values -- matching
this project's own real `agent_memory_events.summary` goal-text convention,
never hand-written per task.
"""

from __future__ import annotations

import argparse
import json
import random
from dataclasses import dataclass, field
import sys
from itertools import combinations
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from paths import COVERAGE_BASE_PATH  # noqa: E402

DIMENSION_TABLE_PATH = COVERAGE_BASE_PATH / "dimension_table.json"

# Deterministic tie-breaking: reproducible output across runs given the same
# dimension_table.json, so re-running this script doesn't silently reshuffle
# an already-reviewed task set.
_RNG_SEED = 30729


@dataclass
class GeneratedTask:
    index: int
    assignment: dict[str, str]
    covers_pairs: list[tuple[str, str, str, str]] = field(default_factory=list)
    goal_text: str = ""
    any_unsupported_value: bool = False


def load_dimensions(path: Path = DIMENSION_TABLE_PATH) -> dict[str, list[str]]:
    """Returns {dimension_name: [value, ...]} -- just the value strings,
    dropping node_id/generation_supported/real_usage metadata, which the
    generator doesn't need (unsupported-value routing is handled separately,
    see `any_unsupported_value` below, sourced from the same file).
    """
    data = json.loads(path.read_text())
    dims: dict[str, list[str]] = {}
    for dim_name, spec in data["dimensions"].items():
        dims[dim_name] = [v["value"] for v in spec["values"]]
    return dims


def load_unsupported_lookup(path: Path = DIMENSION_TABLE_PATH) -> dict[tuple[str, str], bool]:
    """{(dimension_name, value): generation_supported} -- used only to flag
    (never to exclude) generated tasks that name a generation_supported:false
    value, per §7.1's own "included anyway, visible, routes to Phase K"
    discipline. The generator still produces the pair; nothing here silently
    drops it.
    """
    data = json.loads(path.read_text())
    lookup: dict[tuple[str, str], bool] = {}
    for dim_name, spec in data["dimensions"].items():
        for v in spec["values"]:
            lookup[(dim_name, v["value"])] = bool(v["generation_supported"])
    return lookup


def all_pairs_to_cover(dims: dict[str, list[str]]) -> set[tuple[str, str, str, str]]:
    """Every (dim_a, val_a, dim_b, val_b) combination across every distinct
    pair of dimensions -- the real target set pairwise coverage must reach.
    Canonicalized so (dim_a, val_a, dim_b, val_b) and the swapped form are
    never both present.
    """
    pairs: set[tuple[str, str, str, str]] = set()
    dim_names = sorted(dims.keys())
    for dim_a, dim_b in combinations(dim_names, 2):
        for val_a in dims[dim_a]:
            for val_b in dims[dim_b]:
                pairs.add((dim_a, val_a, dim_b, val_b))
    return pairs


def _pairs_covered_by_assignment(
    assignment: dict[str, str],
) -> set[tuple[str, str, str, str]]:
    covered: set[tuple[str, str, str, str]] = set()
    dim_names = sorted(assignment.keys())
    for dim_a, dim_b in combinations(dim_names, 2):
        covered.add((dim_a, assignment[dim_a], dim_b, assignment[dim_b]))
    return covered


def generate_pairwise_set(
    dims: dict[str, list[str]], seed: int = _RNG_SEED
) -> list[GeneratedTask]:
    """The core greedy algorithm. Repeatedly builds one full assignment
    (one value per dimension) chosen to cover as many currently-uncovered
    pairs as possible, greedily, one dimension at a time in a fixed
    deterministic order; records it; removes its covered pairs from the
    remaining set; repeats until nothing is left uncovered.
    """
    rng = random.Random(seed)
    dim_names = sorted(dims.keys())
    remaining = all_pairs_to_cover(dims)
    tasks: list[GeneratedTask] = []

    while remaining:
        assignment: dict[str, str] = {}
        # Fixed dimension order each round, but shuffle each dimension's own
        # value order per round so ties don't always resolve the same way
        # across the whole run -- still fully deterministic given `seed`.
        for dim_name in dim_names:
            candidate_values = list(dims[dim_name])
            rng.shuffle(candidate_values)
            best_value = None
            best_new_coverage = -1
            for value in candidate_values:
                trial = dict(assignment)
                trial[dim_name] = value
                # Only pairs between already-assigned dimensions and this
                # one are decidable at this point; later dimensions aren't
                # assigned yet so can't be scored here.
                new_pairs = {
                    p
                    for p in _pairs_covered_by_assignment(trial)
                    if dim_name in (p[0], p[2])
                }
                new_coverage = len(new_pairs & remaining)
                if new_coverage > best_new_coverage:
                    best_new_coverage = new_coverage
                    best_value = value
            assignment[dim_name] = best_value

        covered_now = _pairs_covered_by_assignment(assignment) & remaining
        if not covered_now:
            # Every remaining pair is already covered by pairs this exact
            # assignment produces having been seen before -- can happen once
            # coverage is nearly complete. Force progress by picking one
            # arbitrary still-uncovered pair and building an assignment
            # around it directly, rather than looping forever.
            forced = next(iter(remaining))
            assignment = {forced[0]: forced[1], forced[2]: forced[3]}
            for dim_name in dim_names:
                if dim_name not in assignment:
                    assignment[dim_name] = rng.choice(dims[dim_name])
            covered_now = _pairs_covered_by_assignment(assignment) & remaining

        remaining -= covered_now
        tasks.append(
            GeneratedTask(
                index=len(tasks) + 1,
                assignment=assignment,
                covers_pairs=sorted(covered_now),
            )
        )

    return tasks


_FIELD_TYPE_NOUN = {
    "char": "a Char field",
    "text": "a Text field",
    "boolean": "a Boolean field",
    "integer": "an Integer field",
    "float": "a Float field",
    "selection": "a Selection field",
    "binary": "a Binary (attachment) field",
    "html": "an Html field",
    "image": "an Image field",
    "monetary": "a Monetary field",
    "date": "a Date field",
    "datetime": "a Datetime field",
    "many2one": "a Many2one field",
    "one2many": "a One2many field",
    "many2many": "a Many2many field",
    "json_field": "a Json field",
    "reference_field": "a Reference field",
    "properties_field": "a Properties field",
    "many2one_reference": "a Many2oneReference field",
    "serialized_field": "a Serialized field",
}

_MODEL_KIND_CLAUSE = {
    "new_model": "on a brand-new model",
    "inherit_extend": "on an _inherit-extended existing model",
    "abstract_mixin": "on a new AbstractModel mixin",
    "transient_model_wizard": "on a TransientModel wizard",
    "inherits_delegation": "on a model using _inherits delegation",
}

_VIEW_TYPE_CLAUSE = {
    "form": "shown on a form view",
    "list": "shown on a list view",
    "kanban": "shown on a kanban view",
    "calendar": "shown on a calendar view",
    "pivot_graph": "shown on a pivot/graph view",
    "search": "shown on a search view",
    "activity_gantt_grid_map": "shown on an activity/gantt/grid/map view",
    "qweb_web_template": "shown via a QWeb web template",
}

_SECURITY_SCOPE_CLAUSE = {
    "crud_only": "with plain model-level CRUD access only",
    "field_group_restriction": "with a field-level groups= restriction",
    "button_group_restriction": "with a button-level groups= restriction",
    "record_rule": "with a record-level ir.rule restriction",
}

_AUTOMATION_TYPE_CLAUSE = {
    "none": "with no automation trigger",
    "cron": "triggered by a scheduled ir.cron job",
    "base_automation_trigger": "triggered by a base.automation rule",
}

_PACKAGING_SHAPE_CLAUSE = {
    "single_new_module": "packaged as a single new module",
    "extend_existing_module": "packaged as an extension of an existing module",
    "cross_module_dependency": "packaged with a real cross-module dependency",
}

_MIXIN_TYPE_CLAUSE = {
    "none": "with no mixin",
    "mail_thread": "inheriting mail.thread",
    "mail_activity_mixin": "inheriting mail.activity.mixin",
    "portal_mixin": "inheriting portal.mixin",
}

_OUTPUT_SURFACE_CLAUSE = {
    "backend_view_only": "surfaced only in the backend view",
    "qweb_report": "surfaced as a printable QWeb report",
    "translated_string": "surfaced as a translated string",
    "external_http": "surfaced via an external HTTP route",
    "one_off_templated_email": "surfaced via a one-off templated email",
}


def render_goal_text(task: GeneratedTask) -> str:
    a = task.assignment
    parts = [
        f"Add {_FIELD_TYPE_NOUN[a['field_type']]}",
        _MODEL_KIND_CLAUSE[a["model_kind"]],
        _VIEW_TYPE_CLAUSE[a["view_type"]],
        _SECURITY_SCOPE_CLAUSE[a["security_scope"]],
        _AUTOMATION_TYPE_CLAUSE[a["automation_type"]],
        _MIXIN_TYPE_CLAUSE[a["mixin_type"]],
        _PACKAGING_SHAPE_CLAUSE[a["packaging_shape"]],
        _OUTPUT_SURFACE_CLAUSE[a["output_surface"]],
    ]
    return ", ".join(parts) + "."


def annotate_unsupported(
    tasks: list[GeneratedTask], unsupported_lookup: dict[tuple[str, str], bool]
) -> None:
    for task in tasks:
        task.any_unsupported_value = any(
            not unsupported_lookup.get((dim, val), True)
            for dim, val in task.assignment.items()
        )


def _pair_both_supported(
    pair: tuple[str, str, str, str], unsupported_lookup: dict[tuple[str, str], bool]
) -> bool:
    dim_a, val_a, dim_b, val_b = pair
    return unsupported_lookup.get((dim_a, val_a), True) and unsupported_lookup.get(
        (dim_b, val_b), True
    )


def tasks_to_json(
    tasks: list[GeneratedTask], unsupported_lookup: dict[tuple[str, str], bool]
) -> list[dict]:
    # Whole-task any_unsupported_value is close to useless as a filter on
    # its own: each generated task spans all 8 dimensions at once, and most
    # dimensions are majority generation_supported:false (confirmed live,
    # 2026-07-31: 161/161 generated tasks touch at least one unsupported
    # value) -- almost any full 8-dimension assignment hits one. The 3-tier
    # funnel (§7.2b) selects at the PAIR level, not the whole-task level, so
    # each covered pair is annotated with its own both-sides-supported flag
    # here -- a task can (and usually does) contain a genuine mix of
    # currently-runnable pairs and Phase-K-routed pairs within the same
    # assignment.
    out = []
    for t in tasks:
        pair_details = [
            {
                "pair": f"{a}:{av} x {b}:{bv}",
                "both_generation_supported": _pair_both_supported(
                    (a, av, b, bv), unsupported_lookup
                ),
            }
            for a, av, b, bv in t.covers_pairs
        ]
        out.append(
            {
                "index": t.index,
                "goal_text": t.goal_text,
                "assignment": t.assignment,
                "covers_pairs": pair_details,
                "n_pairs_covered": len(t.covers_pairs),
                "n_pairs_both_supported": sum(
                    1 for p in pair_details if p["both_generation_supported"]
                ),
                "any_unsupported_value": t.any_unsupported_value,
            }
        )
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dimension-table", type=Path, default=DIMENSION_TABLE_PATH,
        help="Path to dimension_table.json",
    )
    parser.add_argument(
        "--out", type=Path, default=None,
        help="Write the generated task set as JSON to this path (default: stdout)",
    )
    parser.add_argument("--seed", type=int, default=_RNG_SEED)
    args = parser.parse_args()

    dims = load_dimensions(args.dimension_table)
    unsupported_lookup = load_unsupported_lookup(args.dimension_table)
    total_pairs = len(all_pairs_to_cover(dims))

    tasks = generate_pairwise_set(dims, seed=args.seed)
    for t in tasks:
        t.goal_text = render_goal_text(t)
    annotate_unsupported(tasks, unsupported_lookup)

    covered_check = set()
    for t in tasks:
        covered_check.update(t.covers_pairs)
    all_pairs = all_pairs_to_cover(dims)
    assert covered_check == all_pairs, (
        f"generator bug: {len(all_pairs - covered_check)} pairs "
        "never covered by the generated set"
    )

    n_pairs_both_supported = sum(
        1 for p in all_pairs if _pair_both_supported(p, unsupported_lookup)
    )

    result = {
        "generated_by": "scripts/pairwise_task_generator.py",
        "dimension_table_source": str(args.dimension_table),
        "total_pairs_to_cover": total_pairs,
        "n_pairs_both_generation_supported": n_pairs_both_supported,
        "n_pairs_at_least_one_unsupported": total_pairs - n_pairs_both_supported,
        "n_generated_tasks": len(tasks),
        "n_tasks_touching_unsupported_value": sum(
            1 for t in tasks if t.any_unsupported_value
        ),
        "tasks": tasks_to_json(tasks, unsupported_lookup),
    }

    text = json.dumps(result, indent=2)
    if args.out:
        args.out.write_text(text)
        print(
            f"Wrote {len(tasks)} pairwise tasks covering {total_pairs} pairs "
            f"to {args.out}"
        )
    else:
        print(text)


if __name__ == "__main__":
    main()
