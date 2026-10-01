"""Phase 29B auto-draft (2026-07-29). NOT imported or executed by the live pipeline.
Drafted from cluster: generated models_py'X'Lead'X's own actual field/method content is missing.
Real instance row IDs this is meant to close: [11706, 11708, 11710, 11712, 11854, 11856, 11858, 11860, 12004, 12006, 12008, 12010, 12646, 12648, 12826, 12828]
Passed self-test (real behavioral self-test passed: fires on a genuinely new (never-seen) bad example, stays silent on a legitimate one) and the genericity gate on attempt 2.
Requires human review before promotion -- see Phase 29C.
"""

# PHASE29_DRAFT_META: {"target_spec": "build", "cluster_sig": "generated models_py'X'Lead'X's own actual field/method content is missing.", "source_row_ids": [11706, 11708, 11710, 11712, 11854, 11856, 11858, 11860, 12004, 12006, 12008, 12010, 12646, 12648, 12826, 12828], "drafted_attempt": 2, "has_real_behavioral_self_test": true}

import ast

def _validate_models_py_classes_have_real_content(generated: GeneratedModuleFiles) -> None:
    """
    P11 Tier A item 3 (expert design §3.1a). Purely structural, zero-judgment:
    a class with no real statement content beyond its own class header/_name/_inherit/_description
    line is never a correct answer to any goal; the round's own actual field/method content is missing.
    """
    try:
        tree = ast.parse(generated.models_py)
    except SyntaxError as e:
        raise ValueError(f"Failed to parse models_py: {e}") from e

    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            # Filter out docstrings and allowed class attributes
            real_stmts = []
            for stmt in node.body:
                # Skip docstrings
                if (isinstance(stmt, ast.Expr) and
                    isinstance(stmt.value, (ast.Constant, ast.Str)) and
                    isinstance(stmt.value.value if hasattr(stmt.value, 'value') else stmt.value, str)):
                    continue
                # Skip assignments to _name, _inherit, _description
                if isinstance(stmt, ast.Assign):
                    targets = [t.id for t in stmt.targets if isinstance(t, ast.Name)]
                    if all(t in ('_name', '_inherit', '_description') for t in targets):
                        continue
                # Skip AnnAssign (type annotations) if they are just for those attributes?
                # Actually, Odoo models often use `field = fields.Char(...)` which is Assign.
                # We'll keep it simple: if it's not a docstring and not an assignment to those 3, it's real.
                real_stmts.append(stmt)

            if not real_stmts:
                raise ValueError(
                    f"Class '{node.name}' in models_py has no real statement content beyond "
                    "its own class header/_name/_inherit/_description -- no field declarations, "
                    "no method bodies. This is never a correct answer to any goal; the round's "
                    "own actual field/method content is missing."
                )
