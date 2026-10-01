"""Phase 29B auto-draft (2026-07-29). NOT imported or executed by the live pipeline.
Drafted from cluster: models_py defines `_name = 'X'`, which is not a valid Odoo model identifier -- Odoo requires a lowercase, dot-
Real instance row IDs this is meant to close: [14618, 15047, 18641, 18643, 18673, 18676]
Passed self-test (real behavioral self-test passed: fires on a genuinely new (never-seen) bad example, stays silent on a legitimate one) and the genericity gate on attempt 1.
Requires human review before promotion -- see Phase 29C.
"""

# PHASE29_DRAFT_META: {"target_spec": "build", "cluster_sig": "models_py defines `_name = 'X'`, which is not a valid Odoo model identifier -- Odoo requires a lowercase, dot-", "source_row_ids": [14618, 15047, 18641, 18643, 18673, 18676], "drafted_attempt": 1, "has_real_behavioral_self_test": true}

import re

def _validate_model_name_is_valid_odoo_identifier(generated: GeneratedModuleFiles) -> None:
    """
    Ensures that all _name assignments in models.py follow Odoo's strict
    naming convention: lowercase, dot-separated identifiers (e.g., 'module.model').
    Rejects bare identifiers, PascalCase/camelCase class names, or any string
    lacking a module prefix, which causes runtime registry crashes.
    """
    # Find all _name = '...' or _name = "..." assignments
    pattern = re.compile(r"_name\s*=\s*['\"]([^'\"]+)['\"]")
    matches = pattern.findall(generated.models_py)

    for name in matches:
        # Odoo requires a dotted identifier (module.model) and strictly lowercase
        if not re.match(r"^[a-z0-9_]+\.[a-z0-9_]+$", name):
            raise ValueError(
                f"Invalid Odoo model identifier '_name = '{name}''. "
                "Odoo requires a lowercase, dot-separated name (e.g., 'module.model'). "
                "Bare identifiers or PascalCase/camelCase class names will crash the registry."
            )
