"""Phase 29B auto-draft (2026-07-29). NOT imported or executed by the live pipeline.
Drafted from cluster: models_py declares a class extending 'X' via `_inherit` that has an entirely EMPTY body -- no `fields.X(...)` 
Real instance row IDs this is meant to close: [22720, 22718, 22726, 22732, 22738, 22744, 22750, 22756, 22762, 22768, 22774, 22780, 22786, 22792, 22798, 22804, 22810, 22816, 22822, 22828, 22834, 22840]
Passed self-test (real behavioral self-test passed: fires on a genuinely new (never-seen) bad example, stays silent on a legitimate one) and the genericity gate on attempt 2.
Requires human review before promotion -- see Phase 29C.
"""

# PHASE29_DRAFT_META: {"target_spec": "build", "cluster_sig": "models_py declares a class extending 'X' via `_inherit` that has an entirely EMPTY body -- no `fields.X(...)` ", "source_row_ids": [22720, 22718, 22726, 22732, 22738, 22744, 22750, 22756, 22762, 22768, 22774, 22780, 22786, 22792, 22798, 22804, 22810, 22816, 22822, 22828, 22834, 22840], "drafted_attempt": 2, "has_real_behavioral_self_test": true}

import re

def _validate_no_empty_inherit_extensions(generated: GeneratedModuleFiles) -> None:
    """
    P11 Tier B item X. Ensures that any model class in `models_py` that uses
    `_inherit` to extend an existing model actually declares at least one
    field or method. An `_inherit`-only class with no body content is a
    broken generation artifact, not a valid Odoo extension pattern.
    """
    models_content = generated.models_py
    if not models_content:
        return

    # Find all class definitions
    class_pattern = re.compile(r'class\s+(\w+)\s*\([^)]*\):')
    matches = list(class_pattern.finditer(models_content))

    for i, match in enumerate(matches):
        class_name = match.group(1)
        # Extract body: from end of current match to start of next match or end of string
        start = match.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(models_content)
        body = models_content[start:end]

        # Check if this class uses _inherit
        has_inherit = bool(re.search(r'_inherit\s*=\s*["\']', body))
        if not has_inherit:
            continue

        # Check if body has any field declarations or method definitions
        has_fields = bool(re.search(r'fields\.', body))
        has_methods = bool(re.search(r'\bdef\s+\w+', body))

        if not has_fields and not has_methods:
            raise ValueError(
                f"Model class '{class_name}' uses `_inherit` but has an empty body "
                f"(no field declarations or methods). An `_inherit` extension must "
                f"add at least one field or method to the target model."
            )
