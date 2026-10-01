"""Phase 29B auto-draft (2026-07-29). NOT imported or executed by the live pipeline.
Drafted from cluster: The module does not implement the required _onchange_project_id method to auto-fill user_id from project_id.us
Real instance row IDs this is meant to close: [7865]
Passed self-test () and the genericity gate on attempt 1.
Requires human review before promotion -- see Phase 29C.
"""

# PHASE29_DRAFT_META: {"target_spec": "build", "cluster_sig": "The module does not implement the required _onchange_project_id method to auto-fill user_id from project_id.us", "source_row_ids": [7865], "drafted_attempt": 1, "has_real_behavioral_self_test": true}

def _validate_missing_onchange_method(generated: GeneratedModuleFiles) -> None:
         """
         Validates that any @api.onchange method explicitly requested in the task notes
         is actually implemented in the generated Python models code.
         Catches cases where the generation pipeline omits required onchange handlers
         (e.g., auto-filling fields based on relational triggers), leaving the task goal unfulfilled.
         """
         # Extract requested onchange method names from notes
         # Pattern matches _onchange_<field> or mentions of onchange for a field
         import re
         requested_methods = set(re.findall(r'_onchange_(\w+)', generated.notes))
         # Also check for natural language mentions like "onchange <field>"
         requested_methods.update(re.findall(r'onchange\s+(\w+)', generated.notes))

         for method_name in requested_methods:
             full_method_name = f"_onchange_{method_name}"
             # Check if the method is defined in models_py
             # Look for def _onchange_<name>(self): or similar
             if not re.search(rf'def\s+{re.escape(full_method_name)}\s*\(', generated.models_py):
                 raise ValueError(
                     f"Required @api.onchange method '{full_method_name}' is missing from models.py. "
                     f"The task notes request this method to handle field auto-population, but it was not generated."
                 )
