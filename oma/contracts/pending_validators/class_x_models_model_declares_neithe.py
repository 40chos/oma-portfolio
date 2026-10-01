"""Phase 29B auto-draft (2026-07-29). NOT imported or executed by the live pipeline.
Drafted from cluster: class 'X' (models.Model) declares neither _name nor _inherit -- it has no real model identity at all; Odoo rai
Real instance row IDs this is meant to close: [14450, 15211, 15445, 15649, 16116, 18693]
Passed self-test (real behavioral self-test passed: fires on a genuinely new (never-seen) bad example, stays silent on a legitimate one) and the genericity gate on attempt 3.
Requires human review before promotion -- see Phase 29C.
"""

# PHASE29_DRAFT_META: {"target_spec": "build", "cluster_sig": "class 'X' (models.Model) declares neither _name nor _inherit -- it has no real model identity at all; Odoo rai", "source_row_ids": [14450, 15211, 15445, 15649, 16116, 18693], "drafted_attempt": 3, "has_real_behavioral_self_test": true}

import ast

def _validate_persisted_model_declares_name_or_inherit(generated: GeneratedModuleFiles) -> None:
    """
    Validates that every models.Model (or AbstractModel/TransientModel) subclass
    in the generated Python code declares either _name or _inherit.
    Odoo's registry build strictly requires at least one of these attributes
    to establish model identity or inheritance chain. Missing both causes an
    immediate fatal error at runtime.
    """
    try:
        tree = ast.parse(generated.models_py)
    except SyntaxError:
        # If it's not valid Python, other validators will catch it, or we skip.
        return

    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue

        # Check if it inherits from an Odoo model base class
        is_odoo_model = False
        for base in node.bases:
            base_name = ast.unparse(base) if hasattr(ast, 'unparse') else ast.dump(base)
            # Check for models.Model, models.AbstractModel, models.TransientModel
            if 'models.Model' in base_name or 'models.AbstractModel' in base_name or 'models.TransientModel' in base_name:
                is_odoo_model = True
                break

        if not is_odoo_model:
            continue

        # Check for _name or _inherit assignments in the class body
        has_name = False
        has_inherit = False
        for item in node.body:
            if isinstance(item, ast.Assign):
                for target in item.targets:
                    if isinstance(target, ast.Name):
                        if target.id == '_name':
                            has_name = True
                        elif target.id == '_inherit':
                            has_inherit = True
            # Also handle AnnAssign if they use type hints, though rare for these
            elif isinstance(item, ast.AnnAssign):
                if isinstance(item.target, ast.Name):
                    if item.target.id == '_name':
                        has_name = True
                    elif item.target.id == '_inherit':
                        has_inherit = True

        if not has_name and not has_inherit:
            raise ValueError(
                f"Model class '{node.name}' inherits from an Odoo model base but "
                "declares neither '_name' nor '_inherit'. Odoo requires at least one "
                "to establish model identity or inheritance chain at registry-build time."
            )
