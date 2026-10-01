"""Phase 29B auto-draft (2026-07-29). NOT imported or executed by the live pipeline.
Drafted from cluster: generated models_py calls self.message_post(...), but this model does not inherit 'X' -- Odoo raises an Attrib
Real instance row IDs this is meant to close: [12970, 13064, 13357, 13462, 13662, 13757]
Passed self-test (parses cleanly; real behavioral test-generation failed after 4 tries (draft or its generated test does not even execute at definition time: Indentatio) and the genericity gate on attempt 1.
Requires human review before promotion -- see Phase 29C.
"""

# PHASE29_DRAFT_META: {"target_spec": "build", "cluster_sig": "generated models_py calls self.message_post(...), but this model does not inherit 'X' -- Odoo raises an Attrib", "source_row_ids": [12970, 13064, 13357, 13462, 13662, 13757], "drafted_attempt": 1, "has_real_behavioral_self_test": false}

def _validate_message_post_requires_mail_thread_inherit(generated: GeneratedModuleFiles) -> None:
       """
       Validates that any model calling self.message_post() explicitly inherits 'mail.thread'.
       Odoo's mail.thread mixin provides the message_post method; calling it on a model
       without this inheritance raises an AttributeError at runtime.
       """
       import re

       # Find all class definitions in models_py
       # We'll look for class definitions and their _inherit attributes
       # Simple regex approach to extract class names and their _inherit values
       # This is a common pattern in these validators.
       class_pattern = re.compile(r'class\s+(\w+)\s*\([^)]*\):\s*\n((?:.*\n)*?)(?=\nclass\s|\Z)', re.DOTALL)
       # Actually, parsing Python with regex is fragile. But these validators often use simple regex or AST.
       # Let's use a more robust approach: split by class definitions, or use ast module.
       # Given the constraints and typical Odoo codegen validators, regex is often used for simplicity.
       # I'll use a regex to find classes and their _inherit lines.
