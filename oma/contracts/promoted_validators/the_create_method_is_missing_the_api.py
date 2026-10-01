"""Phase 29B auto-draft (2026-07-29). NOT imported or executed by the live pipeline.
Drafted from cluster: The create() method is missing the @api.model_create_multi decorator, which is required for proper multi-recor
Real instance row IDs this is meant to close: [6844]
Passed self-test () and the genericity gate on attempt 1.
Requires human review before promotion -- see Phase 29C.
"""

# PHASE29_DRAFT_META: {"target_spec": "build", "cluster_sig": "The create() method is missing the @api.model_create_multi decorator, which is required for proper multi-recor", "source_row_ids": [6844], "drafted_attempt": 1, "has_real_behavioral_self_test": true}

def _autofix_missing_model_create_multi_decorator(generated: GeneratedModuleFiles) -> None:
       """
       Detects and fixes missing `@api.model_create_multi` decorators on overridden
       `create()` methods in generated model files. Odoo 16+ requires this decorator
       for proper multi-record creation handling when overriding the ORM create method.
       """
       if not generated.models_py:
           return

       lines = generated.models_py.split('\n')
       fixed_lines = []
       i = 0
       while i < len(lines):
           line = lines[i]
           # Check if this line defines a create method
           stripped = line.lstrip()
           if stripped.startswith('def create('):
               # Check if the previous line has the required decorator
               has_decorator = False
               if i > 0:
                   prev_stripped = lines[i-1].lstrip()
                   if prev_stripped.startswith('@api.model_create_multi'):
                       has_decorator = True

               if not has_decorator:
                   # Insert decorator with matching indentation
                   indent = line[:len(line) - len(stripped)]
                   fixed_lines.append(f'{indent}@api.model_create_multi')
               fixed_lines.append(line)
           else:
               fixed_lines.append(line)
           i += 1

       generated.models_py = '\n'.join(fixed_lines)
