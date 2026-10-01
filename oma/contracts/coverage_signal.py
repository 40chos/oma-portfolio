"""P10 (docs/planning/PHASE30_OMA_SEAM_COVERAGE_AND_COMBINATORIAL_HARDENING_2026-07-29.md §18,
Phase O, items 2/3): a real, evidence-backed low-confidence signal derived from
`docs/architecture/oma-coverage-graph/coverage_data.json` -- whether a given field type has been
independently confirmed generation-capable and pairwise combination-tested, or is still a real,
open, unproven gap.

Lives in `contracts/`, matching `contracts/goal_facts.py`'s own placement rationale: both
`manager/` and `specialists/` already import freely from `contracts/`, and this module has no
manager/specialists-specific logic of its own -- it's a pure, static-artifact lookup.

Deliberately scoped to `field_type` ONLY, not the full multi-dimension `complexity_signal` §18
item 1 describes (how many distinct dimension values across all 8 real dimensions -- field_type,
model_kind, view_type, security_scope, automation_type, packaging_shape, mixin_type,
output_surface -- a goal touches). `field_type` is the one dimension already reliably extracted
from goal text without a new LLM call (`contracts/goal_facts.py`'s existing
`extract_goal_field_facts()`, populated once per contract); mapping free text to the other 7
dimensions would need new, unvalidated keyword-matching heuristics or a new extraction call --
real, unscoped scope-extraction work, the same open gap flagged elsewhere in this codebase, not
attempted here as a guess.
"""

from __future__ import annotations

import json

from paths import COVERAGE_BASE_PATH

# This is a low-confidence evidence signal that fails soft (see _load below) if the
# file is absent, so there's no hard requirement that it exist out of the box.
_COVERAGE_DATA_PATH = COVERAGE_BASE_PATH / "coverage_data.json"


def _flatten_nodes(coverage_data: dict) -> dict[str, dict]:
    """Every real node across every domain/subgroup, indexed by its own `id` -- coverage_data.json
    has no single flat lookup table, so this is built fresh from its real, nested `domains ->
    subgroups -> nodes` shape.
    """
    nodes: dict[str, dict] = {}
    for domain in coverage_data.get("domains", []):
        for subgroup in domain.get("subgroups", []):
            for node in subgroup.get("nodes", []):
                node_id = node.get("id")
                if node_id:
                    nodes[node_id] = node
    return nodes


def load_coverage_data(path: Path | None = None) -> dict | None:
    """Never raises -- a missing/unreadable/malformed coverage_data.json returns None, and every
    real caller treats that identically to "no coverage signal available," falling back to the
    existing flat-default behavior unchanged. This is a real, evidence-backed ENHANCEMENT when the
    file is readable, never a new failure mode when it isn't.
    """
    try:
        return json.loads((path or _COVERAGE_DATA_PATH).read_text())
    except Exception:
        return None


def field_type_coverage_signal(field_type: str | None, coverage_data: dict | None = None) -> dict | None:
    """Returns `{"combination_tested": bool, "generation_supported": bool}` for the real coverage
    node matching `field_type` (lowercased, matching this codebase's own real node-id convention,
    e.g. `GoalFieldFacts.field_type` "Char" -> node id "char") -- or None whenever `field_type` is
    unset, `coverage_data` couldn't be loaded, or no matching node exists. None is never treated as
    "low confidence" by callers -- it means "no real signal available," not "assume the worst."
    """
    if not field_type:
        return None
    data = coverage_data if coverage_data is not None else load_coverage_data()
    if not data:
        return None
    node = _flatten_nodes(data).get(field_type.strip().lower())
    if node is None:
        return None
    return {
        "combination_tested": bool(node.get("combination_tested", False)),
        "generation_supported": bool((node.get("confidence") or {}).get("generation", False)),
    }


def field_type_has_low_coverage_confidence(field_type: str | None, coverage_data: dict | None = None) -> bool:
    """P10 item 3's real trigger condition: True only when a real coverage node was found for
    `field_type` AND `generation_supported` is False -- NOT `combination_tested` alone.

    P14 item 4 (docs/planning/PHASE30_OMA_SEAM_COVERAGE_AND_COMBINATORIAL_HARDENING_2026-07-29.md
    §18c.4b): real, confirmed bug found live in this same session, before this shipped to real
    traffic -- the original version used `not combination_tested OR not generation_supported`.
    Pulled real numbers from the live coverage_data.json (2026-08-01): 19 of 20 real field_type
    nodes (95%) have `combination_tested: false` right now, simply because P7 Tier 3 has only
    graduated 1 real pair total out of ~1,070 so far -- `combination_tested` is not yet a
    meaningful per-field-type signal at this stage of that effort, it is close to universally
    false. Under the OR version, this would have force-triggered best-of-N on ~95% of field-type-
    bearing tasks, not just genuinely unproven ones -- defeating the whole point of a targeted,
    evidence-based cost/quality gate (this is the exact risk P14 item 4 itself named for P10's
    mechanism, discovered live rather than left hypothetical).

    Fixed: trigger on `generation_supported: false` alone -- a real, specific, much less
    universal signal (13/20 = 65% of real field types today: binary, html, image, monetary, date,
    datetime, one2many, many2many, json_field, reference_field, properties_field,
    many2one_reference, serialized_field -- genuinely NOT confirmed generation-capable, as opposed
    to char/text/boolean/integer/float/selection/many2one, which are). `combination_tested`
    remains a real, tracked field in the returned signal dict (`field_type_coverage_signal()`) for
    any caller that wants it directly -- just no longer folded into THIS function's own trigger
    condition until P7 Tier 3 has graduated enough real pairs for it to carry real, differentiated
    information again (re-evaluate this decision once that changes, not before).
    """
    signal = field_type_coverage_signal(field_type, coverage_data)
    if signal is None:
        return False
    return not signal["generation_supported"]
