"""Phase 29B auto-draft (2026-07-29). NOT imported or executed by the live pipeline.
Drafted from cluster: generated security_csv contains only a header row (or is effectively empty), but this module declares at least
Real instance row IDs this is meant to close: [11675, 11787, 11838, 12991, 18134, 18731, 18983, 19778, 21557, 21662, 28847]
Passed self-test (real behavioral self-test passed: fires on a genuinely new (never-seen) bad example, stays silent on a legitimate one) and the genericity gate on attempt 2.
Requires human review before promotion -- see Phase 29C.
"""

# PHASE29_DRAFT_META: {"target_spec": "build", "cluster_sig": "generated security_csv contains only a header row (or is effectively empty), but this module declares at least", "source_row_ids": [11675, 11787, 11838, 12991, 18134, 18731, 18983, 19778, 21557, 21662, 28847], "drafted_attempt": 2, "has_real_behavioral_self_test": true}

import re

def _validate_security_csv_not_empty_when_models_declared(generated: GeneratedModuleFiles) -> None:
    """
    Ensures the generated security CSV contains at least one data row whenever
    the module declares new models in models_py. An empty or header-only access
    control file combined with new model definitions means Odoo will grant zero
    permissions on install, rendering the module's own models completely inaccessible.
    """
    # Check if security_csv is effectively empty or header-only
    csv_lines = [line.strip() for line in generated.security_csv.strip().splitlines() if line.strip()]
    is_csv_empty_or_header_only = len(csv_lines) <= 1

    # Check if models_py declares at least one new Odoo model
    has_new_models = bool(re.search(r'class\s+\w+\s*\(\s*models\.(Model|AbstractModel)\s*\)', generated.models_py))

    if is_csv_empty_or_header_only and has_new_models:
        raise ValueError(
            "Generated security CSV contains only a header row (or is empty), but "
            "models_py declares at least one new model. At least one real access-control "
            "data row is required, otherwise Odoo will grant zero permissions and the "
            "module's new models will be completely inaccessible on install."
        )
