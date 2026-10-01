"""Phase 29B auto-draft (2026-07-29). NOT imported or executed by the live pipeline.
Drafted from cluster: tokens are sensitive credentials that should be encrypted or stored in a secure vault, not plaintext in the da
Real instance row IDs this is meant to close: [5262]
Passed self-test () and the genericity gate on attempt 1.
Requires human review before promotion -- see Phase 29C.
"""

# PHASE29_DRAFT_META: {"target_spec": "build", "cluster_sig": "tokens are sensitive credentials that should be encrypted or stored in a secure vault, not plaintext in the da", "source_row_ids": [5262], "drafted_attempt": 1, "has_real_behavioral_self_test": true}

def _validate_no_plaintext_sensitive_fields(generated: GeneratedModuleFiles) -> None:
         """
         Validates that sensitive credentials (e.g., tokens, secrets, passwords, API keys)
         are not defined as plaintext database fields in the generated models.
         Sensitive data should be encrypted, stored in a secure vault, or kept out of the
         database entirely (e.g., via `store=False` or external services).
         """
         import re

         # Generic pattern to detect field definitions with sensitive naming conventions
         # Matches: field_name = fields.Char(...) or fields.Text(...) or fields.Binary(...)
         field_def_pattern = re.compile(
             r'(?P<name>\w+)\s*=\s*fields\.(Char|Text|Binary)\s*\(',
             re.IGNORECASE
         )

         # Generic pattern for sensitive credential identifiers
         sensitive_keywords = re.compile(
             r'(?i)(token|secret|password|passwd|api_key|apikey|credential|private_key|auth_key|access_token|refresh_token|secret_key|private_token|vault_key|encryption_key)',
             re.IGNORECASE
         )

         for match in field_def_pattern.finditer(generated.models_py):
             field_name = match.group('name')
             if sensitive_keywords.search(field_name):
                 raise ValueError(
                     f"Security violation: Field '{field_name}' is defined as a plaintext "
                     f"database field. Sensitive credentials must be encrypted, stored in a "
                     f"secure vault, or excluded from persistent storage (e.g., store=False)."
                 )
