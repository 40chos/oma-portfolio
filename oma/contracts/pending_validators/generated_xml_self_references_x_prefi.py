"""Phase 29B auto-draft (2026-07-29). NOT imported or executed by the live pipeline.
Drafted from cluster: generated XML self-references [X] (prefixed with this module'X'oma_build_a_complete_field_ab52b7f8'X's own ins
Real instance row IDs this is meant to close: [23511, 23545, 23573, 23579, 24554, 24625]
Passed self-test (real behavioral self-test passed: fires on a genuinely new (never-seen) bad example, stays silent on a legitimate one) and the genericity gate on attempt 1.
Requires human review before promotion -- see Phase 29C.
"""

# PHASE29_DRAFT_META: {"target_spec": "build", "cluster_sig": "generated XML self-references [X] (prefixed with this module'X'oma_build_a_complete_field_ab52b7f8'X's own ins", "source_row_ids": [23511, 23545, 23573, 23579, 24554, 24625], "drafted_attempt": 1, "has_real_behavioral_self_test": true}

import re

def _validate_xml_local_ids_are_defined(generated: GeneratedModuleFiles) -> None:
    """
    Validates that all local XML references prefixed with the module's own name
    (e.g., module_name.some_id) correspond to an actually defined <record id="some_id">
    within the generated views_xml or security_xml. Odoo's XML parser will crash
    during installation if a local ID is referenced but never declared in the same module.
    """
    module_name = generated.manifest_fields.name
    xml_content = (generated.views_xml or "") + "\n" + (generated.security_xml or "")

    # Extract all defined local IDs from <record id="...">
    defined_ids = set(re.findall(r'<record\s+id=["\']([^"\']+)["\']', xml_content))

    # Extract all references prefixed with the module name
    # Matches module_name.id, ignoring cases where it's part of a larger string or attribute value incorrectly
    ref_pattern = rf'{re.escape(module_name)}\.([a-zA-Z0-9_]+)'
    referenced_ids = set(re.findall(ref_pattern, xml_content))

    missing_ids = referenced_ids - defined_ids

    if missing_ids:
        raise ValueError(
            f"Generated XML references local IDs prefixed with '{module_name}.' that are "
            f"never defined in the module's XML files. Missing definitions for: "
            f"{', '.join(sorted(missing_ids))}. Odoo will crash during installation "
            f"when trying to resolve these local references. Ensure a corresponding "
            f"<record id=\"...\"> exists in views_xml or security_xml."
        )
