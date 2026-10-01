"""Phase 29B auto-draft (2026-07-29). NOT imported or executed by the live pipeline.
Drafted from cluster: the goal unambiguously states new-model intent ('X'/'X'), but generated models_py has no _name= assignment dec
Real instance row IDs this is meant to close: [21750, 28436, 28612, 28782, 28881]
Passed self-test (real behavioral self-test passed: fires on a genuinely new (never-seen) bad example, stays silent on a legitimate one) and the genericity gate on attempt 3.
Requires human review before promotion -- see Phase 29C.
"""

# PHASE29_DRAFT_META: {"target_spec": "build", "cluster_sig": "the goal unambiguously states new-model intent ('X'/'X'), but generated models_py has no _name= assignment dec", "source_row_ids": [21750, 28436, 28612, 28782, 28881], "drafted_attempt": 3, "has_real_behavioral_self_test": true}

import re

def _validate_goal_new_model_declared(generated: GeneratedModuleFiles) -> None:
    """
    Validates that when the generation goal explicitly requests a new model
    (e.g., 'create a new model', 'add a new model'), the generated models_py
    actually contains a _name= declaration for a genuinely new model, rather
    than only containing _inherit= extensions.

    Catches structural mismatches where the pipeline inherits from existing
    models but fails to declare a new _name, violating the explicit goal.
    """
    # Check if the goal/notes indicate a new model should be created
    goal_text = generated.notes or ""
    new_model_intent = bool(re.search(r'(?i)(create|add|define|build|implement)\s+(a\s+)?new\s+model', goal_text))

    if not new_model_intent:
        return

    # Check if models_py actually declares a new model via _name
    # We look for _name = '...' or _name="..." assignments
    has_new_model_declaration = bool(re.search(r'_name\s*=\s*["\']', generated.models_py))

    if not has_new_model_declaration:
        raise ValueError(
            "Goal explicitly requests a new model, but generated models_py "
            "lacks a '_name=' declaration. Only '_inherit=' extensions or "
            "other code were generated. A genuinely new model must be declared."
        )
