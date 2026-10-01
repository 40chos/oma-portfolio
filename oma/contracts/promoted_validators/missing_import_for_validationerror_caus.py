"""Phase 29B auto-draft (2026-07-29). NOT imported or executed by the live pipeline.
Drafted from cluster: Missing import for ValidationError, causing runtime error on validation failure
Real instance row IDs this is meant to close: [5354, 5356]
Passed a REAL behavioral self-test (not just compile-check) and the genericity gate on attempt 2.
Requires human review before promotion -- see Phase 29C.
"""

# PHASE29_DRAFT_META: {"target_spec": "build", "cluster_sig": "Missing import for ValidationError, causing runtime error on validation failure", "source_row_ids": [5354, 5356], "drafted_attempt": 2, "has_real_behavioral_self_test": true}

import re

def _autofix_missing_validation_error_import(generated: GeneratedModuleFiles) -> None:
    """
    Detects and fixes missing imports for `ValidationError` in generated model files.
    When model methods raise `ValidationError` without importing it from `odoo.exceptions`,
    runtime NameErrors occur. This validator checks for any usage of `ValidationError`
    in `models_py` and ensures the appropriate import is present, adding it generically
    if absent.
    """
    if not generated.models_py:
        return

    # Detect usage of ValidationError anywhere in the models file
    if not re.search(r'\bValidationError\b', generated.models_py):
        return

    # Check if it's already imported via standard odoo.exceptions patterns
    has_import = (
        re.search(r'^from\s+odoo\.exceptions\s+import\s+.*\bValidationError\b', generated.models_py, re.MULTILINE) or
        re.search(r'^from\s+odoo\s+import\s+.*\bexceptions\b', generated.models_py, re.MULTILINE) or
        re.search(r'^import\s+odoo\.exceptions', generated.models_py, re.MULTILINE)
    )

    if has_import:
        return

    # Autofix: prepend the standard import
    import_line = "from odoo.exceptions import ValidationError\n\n"
    generated.models_py = import_line + generated.models_py
