"""Phase 29B auto-draft (2026-07-29). NOT imported or executed by the live pipeline.
Drafted from cluster: the goal explicitly asks for the new custom security group to be applied to fields and/or buttons, but no <fie
Real instance row IDs this is meant to close: [19460, 19466, 19472, 19474]
Passed self-test (parses cleanly; real behavioral test-generation failed after 4 tries (draft or its generated test does not even execute at definition time: Indentatio) and the genericity gate on attempt 2.
Requires human review before promotion -- see Phase 29C.
"""

# PHASE29_DRAFT_META: {"target_spec": "build", "cluster_sig": "the goal explicitly asks for the new custom security group to be applied to fields and/or buttons, but no <fie", "source_row_ids": [19460, 19466, 19472, 19474], "drafted_attempt": 2, "has_real_behavioral_self_test": false}

def _validate_custom_security_group_applied_in_views(generated: GeneratedModuleFiles) -> None:
       """
       Validates that when a custom security group is defined, it is actually
       applied to at least one <field> or <button> element in the generated
       views XML via a `groups=` attribute. Catches cases where the group is
       created in security_xml/security_csv but never referenced in the UI,
       rendering the access control ineffective.
       """
       if generated.views_xml is None:
           return

       # Check for <field ... groups=...> or <button ... groups=...>
       # Using a regex that matches the tag name, allows arbitrary attributes,
       # and specifically looks for the groups= attribute.
       pattern = r'<(?:field|button)\b[^>]*groups\s*='
       if not re.search(pattern, generated.views_xml):
           raise ValueError(
               "Custom security group defined but not applied: "
               "No <field> or <button> element in views_xml contains a 'groups=' attribute. "
               "The security group must be explicitly assigned to UI elements to enforce access control."
           )
