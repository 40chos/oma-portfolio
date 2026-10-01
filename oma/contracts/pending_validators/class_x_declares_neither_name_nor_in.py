"""Phase 29B auto-draft (2026-07-29). NOT imported or executed by the live pipeline.
Drafted from cluster: class 'X' declares neither _name nor _inherit -- it has no real model identity at all; Odoo raises directly at
Real instance row IDs this is meant to close: [18071, 17605, 17894, 17900, 18105, 18518, 18541, 18579, 18624, 18630]
Passed self-test (real behavioral self-test passed: fires on a genuinely new (never-seen) bad example, stays silent on a legitimate one) and the genericity gate on attempt 2.
Requires human review before promotion -- see Phase 29C.
"""

# PHASE29_DRAFT_META: {"target_spec": "build", "cluster_sig": "class 'X' declares neither _name nor _inherit -- it has no real model identity at all; Odoo raises directly at", "source_row_ids": [18071, 17605, 17894, 17900, 18105, 18518, 18541, 18579, 18624, 18630], "drafted_attempt": 2, "has_real_behavioral_self_test": true}

import re

def _validate_model_has_name_or_inherit(generated: GeneratedModuleFiles) -> None:
    """
    P11 depth-audit item 107. A models.Model subclass with neither _name nor _inherit has no
    real identity at all -- Odoo raises directly at registry-build time.
    """
    if not generated.models_py:
        return

    # Find all class definitions that inherit from models.Model
    # Pattern matches: class ClassName(models.Model): or class ClassName( odoo.models.Model ):
    class_pattern = re.compile(
        r'class\s+(\w+)\s*\(\s*models\.Model\s*\)\s*:',
        re.MULTILINE
    )

    # We need to check the body of each matched class for _name or _inherit
    # A simple approach: split by class definitions and check each block
    # Or use a state machine / regex to find class blocks.
    # Given the deterministic nature, let's use a more robust regex to extract class bodies.
    # Actually, a simpler approach: find all classes inheriting from models.Model,
    # then for each, check if the subsequent lines until the next class or EOF contain _name or _inherit.

    # Let's use a regex that captures the class name and its body up to the next class or end of string.
    # This is tricky with regex. AST is better. But I'll stick to regex for simplicity if possible,
    # or use a straightforward line-by-line parser.

    # Actually, Odoo's generated code is usually well-formatted.
    # Let's use a simple approach: find all matches of `class <Name>(models.Model):`
    # Then scan forward for `_name` or `_inherit`.
    # If not found before the next `class ` or end of file, it's invalid.

    lines = generated.models_py.split('\n')
    i = 0
    while i < len(lines):
        line = lines[i]
        # Check if line defines a class inheriting from models.Model
        if re.match(r'\s*class\s+\w+\s*\(\s*models\.Model\s*\)\s*:', line):
            class_name = re.search(r'class\s+(\w+)', line).group(1)
            # Scan forward for _name or _inherit
            has_name_or_inherit = False
            j = i + 1
            while j < len(lines):
                next_line = lines[j]
                # Stop if we hit another class definition or end of file
                if re.match(r'\s*class\s+\w+', next_line):
                    break
                if re.search(r'\b_name\s*=', next_line) or re.search(r'\b_inherit\s*=', next_line):
                    has_name_or_inherit = True
                    break
                j += 1

            if not has_name_or_inherit:
                raise ValueError(
                    f"class '{class_name}' declares neither _name nor _inherit -- "
                    f"it has no real model identity at all; Odoo raises directly at registry-build time."
                )
        i += 1
