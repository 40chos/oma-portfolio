"""Phase 29B auto-draft (2026-07-29). NOT imported or executed by the live pipeline.
Drafted from cluster: Contains only a header row with no data, violating the strict constraint to add ONLY the code strictly require
Real instance row IDs this is meant to close: [4525]
Passed self-test () and the genericity gate on attempt 1.
Requires human review before promotion -- see Phase 29C.
"""

# PHASE29_DRAFT_META: {"target_spec": "build", "cluster_sig": "Contains only a header row with no data, violating the strict constraint to add ONLY the code strictly require", "source_row_ids": [4525], "drafted_attempt": 1, "has_real_behavioral_self_test": true}

def _validate_security_csv_not_header_only(generated: GeneratedModuleFiles) -> None:
         """
         Validates that the generated security CSV contains actual access control entries
         and is not limited to just the standard header row. Odoo requires at least one
         access rule for newly generated models; a header-only CSV indicates missing
         mandatory access definitions, violating the constraint to include only strictly
         required code.
         """
         lines = [line.strip() for line in generated.security_csv.splitlines() if line.strip()]
         if len(lines) == 1:
             raise ValueError(
                 "Security CSV contains only the header row with no data entries. "
                 "At least one access control rule must be defined for the generated model."
             )
