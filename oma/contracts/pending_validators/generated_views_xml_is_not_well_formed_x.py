"""Phase 29B auto-draft (2026-07-29). NOT imported or executed by the live pipeline.
Drafted from cluster: generated views_xml is not well-formed XML -- not well-formed (invalid token): line N, column N -- Odoo's own 
Real instance row IDs this is meant to close: [12754, 14143, 15904]
Passed self-test (real behavioral self-test passed: fires on a genuinely new (never-seen) bad example, stays silent on a legitimate one) and the genericity gate on attempt 3.
Requires human review before promotion -- see Phase 29C.
"""

# PHASE29_DRAFT_META: {"target_spec": "build", "cluster_sig": "generated views_xml is not well-formed XML -- not well-formed (invalid token): line N, column N -- Odoo's own ", "source_row_ids": [12754, 14143, 15904], "drafted_attempt": 3, "has_real_behavioral_self_test": true}

import xml.etree.ElementTree as ET

def _validate_views_xml_is_well_formed(generated: GeneratedModuleFiles) -> None:
    """
    Validates that the generated views_xml contains strictly well-formed XML.
    Catches truncated, incomplete, or syntactically invalid XML fragments that
    would cause Odoo's XML loader to raise a hard parse failure. Ensures the
    content can be parsed as a complete XML document before module generation.
    """
    if generated.views_xml is None:
        return

    try:
        ET.fromstring(generated.views_xml)
    except ET.ParseError as e:
        raise ValueError(
            f"generated views_xml is not well-formed XML -- {e} -- "
            "Odoo's own XML loader requires strictly valid XML; a stray, incomplete, "
            "or truncated fragment anywhere in the file is a hard parse failure. "
            "Regenerate this file as a single, complete, well-formed <odoo>...</odoo> document."
        )
