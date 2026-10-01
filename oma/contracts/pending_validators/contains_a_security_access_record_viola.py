"""Phase 29B auto-draft (2026-07-29). NOT imported or executed by the live pipeline.
Drafted from cluster: Contains a security access record, violating the explicit round constraint to exclude security records
Real instance row IDs this is meant to close: [3788]
Passed self-test () and the genericity gate on attempt 1.
Requires human review before promotion -- see Phase 29C.
"""

# PHASE29_DRAFT_META: {"target_spec": "build", "cluster_sig": "Contains a security access record, violating the explicit round constraint to exclude security records", "source_row_ids": [3788], "drafted_attempt": 1, "has_real_behavioral_self_test": true}

def _validate_no_security_records(generated: GeneratedModuleFiles) -> None:
       """
       Validates that the generated module does not contain security access records
       when the current generation round explicitly excludes them.
       Security records are typically defined in security_csv or security_xml.
       Raises ValueError if any non-empty security content is detected.
       """
       has_security_csv = bool(generated.security_csv and generated.security_csv.strip())
       has_security_xml = bool(generated.security_xml and generated.security_xml.strip())

       if has_security_csv or has_security_xml:
           raise ValueError(
               "Generated module contains security access records, violating the "
               "explicit round constraint to exclude security records. "
               "Security files must be empty or None for this generation round."
           )
