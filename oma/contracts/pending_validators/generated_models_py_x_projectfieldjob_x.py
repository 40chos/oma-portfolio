"""Phase 29B auto-draft (2026-07-29). NOT imported or executed by the live pipeline.
Drafted from cluster: generated models_py'X'ProjectFieldjob'X's own actual field/method content is missing.
Real instance row IDs this is meant to close: [11690, 11692, 11694, 11696, 12856, 12858, 12950, 12952, 14287, 14854, 15808, 16237, 16578, 17742, 18346, 19445, 19633, 19631, 19644]
Passed self-test (real behavioral self-test passed: fires on a genuinely new (never-seen) bad example, stays silent on a legitimate one) and the genericity gate on attempt 1.
Requires human review before promotion -- see Phase 29C.
"""

# PHASE29_DRAFT_META: {"target_spec": "build", "cluster_sig": "generated models_py'X'ProjectFieldjob'X's own actual field/method content is missing.", "source_row_ids": [11690, 11692, 11694, 11696, 12856, 12858, 12950, 12952, 14287, 14854, 15808, 16237, 16578, 17742, 18346, 19445, 19633, 19631, 19644], "drafted_attempt": 1, "has_real_behavioral_self_test": true}

import ast

def _validate_models_py_classes_have_real_content(generated: GeneratedModuleFiles) -> None:
    """
    P11 Tier A item 3 (expert design §3.1a). Purely structural, zero-judgment:
    a class with no real statement content beyond its own class header/`_name`/
    `_inherit`/`_description` line is never a correct answer to any goal; the
    round's own actual field/method content is missing.
    """
    try:
        tree = ast.parse(generated.models_py)
    except SyntaxError as e:
        raise ValueError(f"models_py contains invalid Python syntax: {e}") from e

    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue

        # Filter out allowed/ignored statements
        real_statements = []
        for stmt in node.body:
            # Ignore docstrings
            if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant) and isinstance(stmt.value.value, str):
                continue
            # Ignore pass
            if isinstance(stmt, ast.Pass):
                continue
            # Ignore _name, _inherit, _description assignments
            if isinstance(stmt, ast.Assign):
                target_names = {t.id for t in stmt.targets if isinstance(t, ast.Name)}
                if target_names <= {'_name', '_inherit', '_description'}:
                    continue
            # If it's not filtered out, it's real content
            real_statements.append(stmt)

        if not real_statements:
            raise ValueError(
                f"generated models_py's class '{node.name}' has no real statement content "
                f"beyond its own class header/_name/_inherit/_description -- no field declarations, "
                f"no method bodies. This is never a correct answer to any goal; the round's own "
                f"actual field/method content is missing."
            )
