"""Phase 29B auto-draft (2026-07-29). NOT imported or executed by the live pipeline.
Drafted from cluster: data_change branch: would use OdooToolClient directly per the odoo-xmlrpc-operations skill, with no module-dev
Real instance row IDs this is meant to close: [10123, 10125, 10162, 10164, 10179, 10181, 11044, 11046]
Passed self-test (real behavioral self-test passed: fires on a genuinely new (never-seen) bad example, stays silent on a legitimate one) and the genericity gate on attempt 3.
Requires human review before promotion -- see Phase 29C.
"""

# PHASE29_DRAFT_META: {"target_spec": "build", "cluster_sig": "data_change branch: would use OdooToolClient directly per the odoo-xmlrpc-operations skill, with no module-dev", "source_row_ids": [10123, 10125, 10162, 10164, 10179, 10181, 11044, 11046], "drafted_attempt": 3, "has_real_behavioral_self_test": true}

def _validate_notes_external_tool_dependency(generated: GeneratedModuleFiles) -> None:
         """
         Validates that the generated module's notes do not indicate reliance on
         external XML-RPC tools or non-module-development skills (e.g., OdooToolClient).
         Raises ValueError if the notes contain a pattern indicating the task branch
         bypasses standard module-development tools in favor of direct external tool usage.
         """
         import re
         pattern = re.compile(
             r".*branch: would use .* directly per the .* skill, with no module-development tools handed to it.",
             re.IGNORECASE
         )
         if pattern.search(generated.notes):
             raise ValueError(
                 "Generated module notes indicate reliance on an external tool/skill "
                 "bypassing standard module-development tools. This task type is incompatible "
                 "with standard Odoo module generation and must be handled via the appropriate "
                 "external execution pipeline."
             )
