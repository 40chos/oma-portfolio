"""Phase 29B auto-draft (2026-07-29). NOT imported or executed by the live pipeline.
Drafted from cluster: the goal asks for quick filter button(s) -- Odoo'X's own filter bar -- but the generated views_xml contains no
Real instance row IDs this is meant to close: [18330, 18332, 19641]
Passed self-test (real behavioral self-test passed: fires on a genuinely new (never-seen) bad example, stays silent on a legitimate one) and the genericity gate on attempt 1.
Requires human review before promotion -- see Phase 29C.
"""

# PHASE29_DRAFT_META: {"target_spec": "build", "cluster_sig": "the goal asks for quick filter button(s) -- Odoo'X's own filter bar -- but the generated views_xml contains no", "source_row_ids": [18330, 18332, 19641], "drafted_attempt": 1, "has_real_behavioral_self_test": true}

def _validate_quick_filter_elements_in_search_view(generated: GeneratedModuleFiles) -> None:
       """
       P11 Tier A item X (expert design §Y.Z). Catches cases where the generation goal/notes
       explicitly request quick filter buttons (Odoo's <filter> elements inside a <search> view),
       but the generated views_xml completely omits them. This prevents the LLM from substituting
       visually similar but functionally distinct mechanisms (e.g., tree/list decoration-* attributes)
       for actual search-view filter toggles.
       """
       if not generated.views_xml:
           return

       # Check if the goal/notes indicate a requirement for filter buttons
       notes_lower = generated.notes.lower()
       filter_intent_keywords = ["quick filter", "filter button", "search filter", "toggle filter", "filter by"]
       has_filter_intent = any(kw in notes_lower for kw in filter_intent_keywords)

       if not has_filter_intent:
           return

       # Check if <filter> elements actually exist in the generated XML
       # We look for <filter (with optional attributes) inside the XML string
       import re
       has_filter_elements = bool(re.search(r'<filter\s', generated.views_xml, re.IGNORECASE))

       if not has_filter_elements:
           raise ValueError(
               "Goal/notes request quick filter buttons, but generated views_xml contains no <filter> elements. "
               "Ensure filter toggles are implemented using <filter> tags inside a <search> view, not via "
               "tree/list decoration attributes or other unrelated UI mechanisms."
           )
