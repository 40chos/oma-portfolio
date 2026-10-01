"""Phase 29B auto-draft (2026-07-29). NOT imported or executed by the live pipeline.
Drafted from cluster: BuildSpecialist does not handle capability_class=<CapabilityClass.readonly_investigation: 'X'> -- readonly_inv
Real instance row IDs this is meant to close: [22504, 22502, 22506, 22508]
Passed self-test (real behavioral self-test passed: fires on a genuinely new (never-seen) bad example, stays silent on a legitimate one) and the genericity gate on attempt 2.
Requires human review before promotion -- see Phase 29C.
"""

# PHASE29_DRAFT_META: {"target_spec": "build", "cluster_sig": "BuildSpecialist does not handle capability_class=<CapabilityClass.readonly_investigation: 'X'> -- readonly_inv", "source_row_ids": [22504, 22502, 22506, 22508], "drafted_attempt": 2, "has_real_behavioral_self_test": true}

def _validate_no_out_of_scope_capabilities(generated: GeneratedModuleFiles) -> None:
         """
         Validates that the generated module files do not contain capability references
         that belong to other pipeline phases or components (e.g., Code-Review, Testing).
         The build specialist should only generate code for capabilities explicitly scoped
         to the current build phase. Any occurrence of capability_class=<CapabilityClass.*>
         in the output indicates a routing or filtering failure in the generation pipeline.
         """
         import re
         pattern = re.compile(r'capability_class\s*=\s*<CapabilityClass\.[^>]+>')
         files_to_check = [
             generated.models_py,
             generated.views_xml,
             generated.security_xml,
         ]
         if generated.extra_data_files:
             files_to_check.extend(generated.extra_data_files.values())
         if generated.tests_py:
             files_to_check.extend(generated.tests_py.values())

         for content in files_to_check:
             if content and pattern.search(content):
                 raise ValueError(
                     "Generated files contain out-of-scope capability references "
                     "(capability_class=<CapabilityClass.*>). These belong to other "
                     "pipeline phases/components and must not be included in the build output."
                 )
