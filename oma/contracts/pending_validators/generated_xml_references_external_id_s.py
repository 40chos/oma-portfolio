"""Phase 29B auto-draft (2026-07-29). NOT imported or executed by the live pipeline.
Drafted from cluster: generated XML references external id(s) [X] via ref="X" that do not exist in the real Odoo registry -- Odoo's 
Real instance row IDs this is meant to close: [10481, 10483, 10490, 10492, 10722, 10720, 10724, 10726, 10728, 11164, 11166, 11168, 11170, 11172, 12135, 12139, 12141, 13491, 13590, 14295, 14297, 14881, 14885, 15335, 15339, 18357, 20857, 23661, 25093, 25095, 25104, 25106, 25368, 25370, 25459, 25461, 25561, 25563, 26197, 26199, 26339, 26311, 26313, 26332, 26337, 26562, 26564, 26677, 26679, 30620, 30622, 32199]
Passed self-test (real behavioral self-test passed: fires on a genuinely new (never-seen) bad example, stays silent on a legitimate one) and the genericity gate on attempt 3.
Requires human review before promotion -- see Phase 29C.
"""

# PHASE29_DRAFT_META: {"target_spec": "build", "cluster_sig": "generated XML references external id(s) [X] via ref=\"X\" that do not exist in the real Odoo registry -- Odoo's ", "source_row_ids": [10481, 10483, 10490, 10492, 10722, 10720, 10724, 10726, 10728, 11164, 11166, 11168, 11170, 11172, 12135, 12139, 12141, 13491, 13590, 14295, 14297, 14881, 14885, 15335, 15339, 18357, 20857, 23661, 25093, 25095, 25104, 25106, 25368, 25370, 25459, 25461, 25561, 25563, 26197, 26199, 26339, 26311, 26313, 26332, 26337, 26562, 26564, 26677, 26679, 30620, 30622, 32199], "drafted_attempt": 3, "has_real_behavioral_self_test": true}

def _validate_no_hallucinated_external_id_refs(generated: GeneratedModuleFiles) -> None:
         """
         Validates that all `ref="..."` attributes in generated XML files point to
         external IDs that actually exist in the real Odoo registry.
         Rejects plausible-sounding but non-existent external IDs to prevent
         install-time crashes during reference resolution.
         """
         import re
         # Collect all XML content to scan
         xml_contents = []
         if generated.views_xml:
             xml_contents.append(generated.views_xml)
         if generated.extra_data_files:
             xml_contents.extend(generated.extra_data_files.values())

         all_refs = set()
         for xml_content in xml_contents:
             all_refs.update(re.findall(r'ref="([^"]*)"', xml_content))

         invalid_refs = [ref for ref in all_refs if ref not in KNOWN_EXTERNAL_IDS]
         if invalid_refs:
             raise ValueError(
                 f"Generated XML references external id(s) {sorted(invalid_refs)} via ref=\"...\" "
                 f"that do not exist in the real Odoo registry -- Odoo's own install would crash "
                 f"trying to resolve a reference to something that was never real. Only reference "
                 f"REAL, existing external ids (e.g. a real standard menu/action/group), never a "
                 f"plausible-sounding guess."
             )
