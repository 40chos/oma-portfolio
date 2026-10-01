"""Phase 29B auto-draft (2026-07-29). NOT imported or executed by the live pipeline.
Drafted from cluster: generated models_py defines compute method(s) [X] that no field'X's compute= kwarg, or remove the orphaned met
Real instance row IDs this is meant to close: [11966, 11970, 11972, 12096, 12098, 12100, 12102, 12503, 12675, 12677, 12757, 12799, 12807]
Passed self-test (real behavioral self-test passed: fires on a genuinely new (never-seen) bad example, stays silent on a legitimate one) and the genericity gate on attempt 2.
Requires human review before promotion -- see Phase 29C.
"""

# PHASE29_DRAFT_META: {"target_spec": "build", "cluster_sig": "generated models_py defines compute method(s) [X] that no field'X's compute= kwarg, or remove the orphaned met", "source_row_ids": [11966, 11970, 11972, 12096, 12098, 12100, 12102, 12503, 12675, 12677, 12757, 12799, 12807], "drafted_attempt": 2, "has_real_behavioral_self_test": true}

import re

def _validate_no_orphaned_compute_methods(generated: GeneratedModuleFiles) -> None:
    """
    P11 Tier A item 6 (expert design §3.3a). Every `_compute_*` method defined in models_py
    must be referenced by at least one field's `compute=` kwarg somewhere in the file -- an
    unreferenced compute method is dead code that Odoo will never invoke.
    """
    code = generated.models_py
    defined = set(re.findall(r'\bdef\s+(_compute_\w+)\s*\(', code))
    referenced = set(re.findall(r"compute\s*=\s*['\"]?(_compute_\w+)['\"]?", code))
    orphaned = defined - referenced
    if orphaned:
        raise ValueError(
            f"generated models_py defines compute method(s) {sorted(orphaned)} that no field's "
            f"own compute= kwarg actually references -- Odoo never calls a method just because "
            f"its name starts with _compute_; either wire it to the intended field's compute= "
            f"kwarg, or remove the orphaned method entirely."
        )
