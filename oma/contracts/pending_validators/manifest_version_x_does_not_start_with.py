"""Phase 29B auto-draft (2026-07-29). NOT imported or executed by the live pipeline.
Drafted from cluster: manifest version='X' does not start with 'X' (this project's real target Odoo series) -- breaks Apps-list disp
Real instance row IDs this is meant to close: [11720, 11734, 11775, 11809, 11848, 11822, 11871, 11889, 11908, 11940, 11980, 11988, 11996]
Passed self-test (real behavioral self-test passed: fires on a genuinely new (never-seen) bad example, stays silent on a legitimate one) and the genericity gate on attempt 2.
Requires human review before promotion -- see Phase 29C.
"""

# PHASE29_DRAFT_META: {"target_spec": "build", "cluster_sig": "manifest version='X' does not start with 'X' (this project's real target Odoo series) -- breaks Apps-list disp", "source_row_ids": [11720, 11734, 11775, 11809, 11848, 11822, 11871, 11889, 11908, 11940, 11980, 11988, 11996], "drafted_attempt": 2, "has_real_behavioral_self_test": true}

def _validate_manifest_version_series_prefix(
       generated: GeneratedModuleFiles, expected_series_prefix: str
   ) -> None:
       """
       Validates that the manifest's version string starts with the expected Odoo series prefix.
       Odoo's Apps list and upgrade tooling rely on the version prefix matching the target
       series (e.g., '16.0.', '17.0.'). Mismatches cause display/tooling failures, though
       installation may still proceed.
       """
       version = generated.manifest_fields.version
       if not version.startswith(expected_series_prefix):
           raise ValueError(
               f"Manifest version '{version}' does not start with the expected Odoo series "
               f"prefix '{expected_series_prefix}'. This breaks Apps-list display and upgrade "
               f"tooling, though installation may still proceed."
           )
