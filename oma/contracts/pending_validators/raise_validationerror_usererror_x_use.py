"""Phase 29B auto-draft (2026-07-29). NOT imported or executed by the live pipeline.
Drafted from cluster: raise ValidationError/UserError("X") uses a plain string literal, never wrapped in Odoo's own _(...) translati
Real instance row IDs this is meant to close: [11795, 11952, 12090, 13032, 13431, 13630, 13728]
Passed self-test (real behavioral self-test passed: fires on a genuinely new (never-seen) bad example, stays silent on a legitimate one) and the genericity gate on attempt 2.
Requires human review before promotion -- see Phase 29C.
"""

# PHASE29_DRAFT_META: {"target_spec": "build", "cluster_sig": "raise ValidationError/UserError(\"X\") uses a plain string literal, never wrapped in Odoo's own _(...) translati", "source_row_ids": [11795, 11952, 12090, 13032, 13431, 13630, 13728], "drafted_attempt": 2, "has_real_behavioral_self_test": true}

import re

def _validate_raise_error_strings_use_translate_call(generated: GeneratedModuleFiles) -> None:
    """
    P11 Tier B item XX (expert design §X.X). Flag only, deliberately NOT autofix:
    a raise ValidationError(...)/UserError(...) call whose argument is a plain string
    literal not wrapped in Odoo's _() translation call. Such strings will never be
    translated. Wrap the literal template in _(), never the already-interpolated
    result of an f-string.
    """
    pattern = re.compile(r"raise\s+(?:ValidationError|UserError)\s*\(\s*(?!_)\s*['\"]")

    sources_to_check = [
        generated.models_py,
     ]
    if generated.tests_py:
        sources_to_check.extend(generated.tests_py.values())

    for source in sources_to_check:
        if not source:
            continue
        matches = pattern.findall(source)
        if matches:
            raise ValueError(
                f"Found {len(matches)} raise ValidationError/UserError call(s) with "
                f"untranslated string literal(s). Wrap the literal template in _(), "
                f"e.g., raise ValidationError(_('message')), not raise ValidationError('message')."
            )
