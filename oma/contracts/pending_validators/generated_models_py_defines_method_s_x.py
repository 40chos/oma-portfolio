"""Phase 29B auto-draft (2026-07-29). NOT imported or executed by the live pipeline.
Drafted from cluster: generated models_py defines method(s) [X] more than once -- Python silently keeps only the LAST definition and
Real instance row IDs this is meant to close: [10521, 11737, 11892, 12816, 12818, 13052, 13054, 13085, 13451, 13877, 13879, 14914, 14964]
Passed self-test (real behavioral self-test passed: fires on a genuinely new (never-seen) bad example, stays silent on a legitimate one) and the genericity gate on attempt 2.
Requires human review before promotion -- see Phase 29C.
"""

# PHASE29_DRAFT_META: {"target_spec": "build", "cluster_sig": "generated models_py defines method(s) [X] more than once -- Python silently keeps only the LAST definition and", "source_row_ids": [10521, 11737, 11892, 12816, 12818, 13052, 13054, 13085, 13451, 13877, 13879, 14914, 14964], "drafted_attempt": 2, "has_real_behavioral_self_test": true}

def _validate_no_duplicate_method_definitions_in_models_py(generated: GeneratedModuleFiles) -> None:
       """
       Ensures that no method name is defined more than once in the generated models_py file.
       Python silently overwrites duplicate function definitions, retaining only the last one.
       This causes silent loss of any @api.depends, @api.onchange, or other decorators/logic
       attached to earlier definitions. Each method must be defined exactly once.
       """
       import re
       from collections import Counter

       # Extract all method names defined in the models_py string
       method_names = re.findall(r'\bdef\s+([a-zA-Z_]\w*)\s*\(', generated.models_py)
       counts = Counter(method_names)
       duplicates = [name for name, count in counts.items() if count > 1]

       if duplicates:
           raise ValueError(
               f"generated models_py defines method(s) {sorted(duplicates)!r} more than once -- "
               "Python silently keeps only the LAST definition and discards the rest, which means "
               "any @api.depends/@api.onchange-decorated behavior on an earlier definition is silently "
               "lost entirely. Each method must be defined exactly once; merge the duplicate definitions into one."
           )
