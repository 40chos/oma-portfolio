"""Phase 29B auto-draft (2026-07-29). NOT imported or executed by the live pipeline.
Drafted from cluster: create()'X'for vals in vals_list:'X's own original dict in place, a real, surprising side effect; rebind a loc
Real instance row IDs this is meant to close: [11762, 11764, 11766, 11916, 11918, 11920, 12050, 12052, 12879, 12881, 12917, 13274, 13276, 13609, 13613]
Passed self-test (real behavioral self-test passed: fires on a genuinely new (never-seen) bad example, stays silent on a legitimate one) and the genericity gate on attempt 1.
Requires human review before promotion -- see Phase 29C.
"""

# PHASE29_DRAFT_META: {"target_spec": "build", "cluster_sig": "create()'X'for vals in vals_list:'X's own original dict in place, a real, surprising side effect; rebind a loc", "source_row_ids": [11762, 11764, 11766, 11916, 11918, 11920, 12050, 12052, 12879, 12881, 12917, 13274, 13276, 13609, 13613], "drafted_attempt": 1, "has_real_behavioral_self_test": true}

import re

def _autofix_no_in_place_vals_mutation_in_batch_create(generated: GeneratedModuleFiles) -> None:
    """
    Ensures that any `for vals in vals_list:` loop inside a `create()` method
    rebinds `vals` to a shallow copy before mutating it. Mutating the original
    dict in place causes surprising side-effects for the caller.
    """
    if not generated.models_py:
        return

    # Pattern to match the for loop line
    for_loop_pattern = re.compile(r'^(\s*)for\s+vals\s+in\s+vals_list\s*:', re.MULTILINE)

    # We'll process the file line by line to handle indentation correctly
    lines = generated.models_py.split('\n')
    fixed_lines = []
    i = 0
    while i < len(lines):
        line = lines[i]
        match = for_loop_pattern.match(line)
        if match:
            indent = match.group(1)
            # Look ahead to see if there's a mutation of vals in this block
            # and if a copy/rebind is already present
            has_mutation = False
            has_copy = False
            block_indent = len(indent)
            j = i + 1
            while j < len(lines):
                next_line = lines[j]
                # Check if we've left the block (line is not empty and indent <= block_indent)
                if next_line.strip() and len(next_line) - len(next_line.lstrip()) <= block_indent:
                    break
                # Check for copy/rebind
                if re.search(r'\bvals\s*=\s*(dict\(vals\)|\{[\s*]*vals[\s*]*\}|\w+\.copy\(\))', next_line):
                    has_copy = True
                # Check for mutation
                if re.search(r'\bvals\s*\[', next_line) and '=' in next_line:
                    has_mutation = True
                j += 1

            if has_mutation and not has_copy:
                # Insert the copy line right after the for loop
                fixed_lines.append(line)
                fixed_lines.append(f'{indent}    vals = dict(vals)')
                i += 1
                continue

        fixed_lines.append(line)
        i += 1

    if fixed_lines != lines:
        generated.models_py = '\n'.join(fixed_lines)
