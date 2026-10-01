"""Phase 29B auto-draft (2026-07-29). NOT imported or executed by the live pipeline.
Drafted from cluster: generated models_py declares field(s) [X] more than once in the same class -- the later declaration silently s
Real instance row IDs this is meant to close: [22953, 22955, 27227, 27732, 27734]
Passed self-test (real behavioral self-test passed: fires on a genuinely new (never-seen) bad example, stays silent on a legitimate one) and the genericity gate on attempt 1.
Requires human review before promotion -- see Phase 29C.
"""

# PHASE29_DRAFT_META: {"target_spec": "build", "cluster_sig": "generated models_py declares field(s) [X] more than once in the same class -- the later declaration silently s", "source_row_ids": [22953, 22955, 27227, 27732, 27734], "drafted_attempt": 1, "has_real_behavioral_self_test": true}

import re

def _validate_no_duplicate_field_declarations(generated: GeneratedModuleFiles) -> None:
    """
    Validates that no field is declared more than once within the same model class
    in generated.models_py. Duplicate declarations typically occur when a scoped-edit
    round accidentally re-adds an existing field instead of extending or referencing it,
    causing the later declaration to silently shadow the earlier one.
    """
    models_py = generated.models_py
    if not models_py:
        return

    # Regex to match class definitions
    class_pattern = re.compile(r'^class\s+(\w+)\s*\(', re.MULTILINE)
    # Regex to match field assignments (e.g., field_name = fields.Char(...))
    field_pattern = re.compile(r'^\s+(\w+)\s*=\s*fields\.', re.MULTILINE)

    classes = list(class_pattern.finditer(models_py))
    for i, class_match in enumerate(classes):
        class_name = class_match.group(1)
        # Determine the span of this class
        start = class_match.start()
        end = classes[i + 1].start() if i + 1 < len(classes) else len(models_py)
        class_body = models_py[start:end]

        field_matches = list(field_pattern.finditer(class_body))
        field_names = [m.group(1) for m in field_matches]

        seen = set()
        duplicates = set()
        for name in field_names:
            if name in seen:
                duplicates.add(name)
            seen.add(name)

        if duplicates:
            raise ValueError(
                f"Duplicate field declaration(s) in class '{class_name}': "
                f"{', '.join(sorted(duplicates))}. "
                "A scoped-edit round likely re-added an existing field instead of "
                "extending or referencing it, causing silent shadowing."
            )
