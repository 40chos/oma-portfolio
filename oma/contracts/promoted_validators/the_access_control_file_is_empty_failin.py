"""Phase 29B auto-draft (2026-07-29). NOT imported or executed by the live pipeline.
Drafted from cluster: The access control file is empty, failing to define proper permissions for the new fields, which violates leas
Real instance row IDs this is meant to close: [5266, 5268]
Passed self-test () and the genericity gate on attempt 1.
Requires human review before promotion -- see Phase 29C.
"""

# PHASE29_DRAFT_META: {"target_spec": "build", "cluster_sig": "The access control file is empty, failing to define proper permissions for the new fields, which violates leas", "source_row_ids": [5266, 5268], "drafted_attempt": 1, "has_real_behavioral_self_test": true}

def _validate_security_csv_not_empty(generated: GeneratedModuleFiles) -> None:
         """
         Ensures the generated security CSV file is not empty or whitespace-only.
         An empty access control file means no model/field permissions were defined,
         violating least-privilege principles and leaving sensitive data exposed.
         """
         if not generated.security_csv.strip():
             raise ValueError(
                 "The generated security CSV file is empty. "
                 "Access control records must be defined to enforce least-privilege "
                 "permissions for the module's models and fields."
             )
