"""Phase 29B auto-draft (2026-07-29). NOT imported or executed by the live pipeline.
Drafted from cluster: review response failed to produce valid JSON after N attempts: Expecting 'X' delimiter: line N column N (char 
Real instance row IDs this is meant to close: [12391, 12554, 12560, 20753, 20759]
Passed self-test (real behavioral self-test passed: fires on a genuinely new (never-seen) bad example, stays silent on a legitimate one) and the genericity gate on attempt 1.
Requires human review before promotion -- see Phase 29C.
"""

# PHASE29_DRAFT_META: {"target_spec": "build", "cluster_sig": "review response failed to produce valid JSON after N attempts: Expecting 'X' delimiter: line N column N (char ", "source_row_ids": [12391, 12554, 12560, 20753, 20759], "drafted_attempt": 1, "has_real_behavioral_self_test": true}

def _validate_review_response_json_parsing_integrity(generated: GeneratedModuleFiles) -> None:
       """
       Catches review response payloads that failed JSON parsing due to missing comma delimiters.
       This pattern indicates a malformed review output that leaked into the generation notes,
       typically from a downstream LLM or parser step that truncated or mangled JSON syntax.
       Raises ValueError if the pattern is detected in generated.notes.
       """
       import re
       pattern = re.compile(
           r"review response failed to produce valid JSON after \d+ attempts: "
           r"Expecting ',' delimiter: line \d+ column \d+ \(char \d+\)"
       )
       if pattern.search(generated.notes):
           raise ValueError(
               "Review response JSON parsing failed due to missing comma delimiter. "
               "The generation notes contain a malformed review payload that indicates "
               "a downstream parsing error. Please verify the review step output format."
           )
