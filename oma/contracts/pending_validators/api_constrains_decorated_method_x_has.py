"""Phase 29B auto-draft (2026-07-29). NOT imported or executed by the live pipeline.
Drafted from cluster: @api.constrains-decorated method 'X' has no raise anywhere in its body -- a constraint check that never raises
Real instance row IDs this is meant to close: [13832, 13799, 13810, 13821]
Passed self-test (real behavioral self-test passed: fires on a genuinely new (never-seen) bad example, stays silent on a legitimate one) and the genericity gate on attempt 2.
Requires human review before promotion -- see Phase 29C.
"""

# PHASE29_DRAFT_META: {"target_spec": "build", "cluster_sig": "@api.constrains-decorated method 'X' has no raise anywhere in its body -- a constraint check that never raises", "source_row_ids": [13832, 13799, 13810, 13821], "drafted_attempt": 2, "has_real_behavioral_self_test": true}

import ast

def _validate_api_constrains_method_has_a_raise(generated: GeneratedModuleFiles) -> None:
    """
    P11 depth-audit item 106. Every @api.constrains(...)-decorated method must contain at least
    one raise somewhere in its body -- a pure structural-absence check, orthogonal to Tier A item
    14's field-level validation. Constraints that never raise are structurally inert and will
    silently fail to enforce business rules at runtime.
    """
    try:
        tree = ast.parse(generated.models_py)
    except SyntaxError:
        return # Let other validators handle syntax errors

    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            # Check for @api.constrains decorator
            has_constrains = False
            for dec in node.decorator_list:
                if isinstance(dec, ast.Call):
                    if isinstance(dec.func, ast.Attribute) and dec.func.attr == 'constrains':
                        has_constrains = True
                        break
                elif isinstance(dec, ast.Attribute) and dec.attr == 'constrains':
                    has_constrains = True
                    break

            if has_constrains:
                # Check for raise in body
                has_raise = False
                for child in ast.walk(node):
                    if isinstance(child, ast.Raise):
                        has_raise = True
                        break
                if not has_raise:
                    raise ValueError(
                        f"@api.constrains-decorated method '{node.name}' has no raise anywhere in its body "
                        "-- a constraint check that never raises can never actually enforce anything."
                    )
