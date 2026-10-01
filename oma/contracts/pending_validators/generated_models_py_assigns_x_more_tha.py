"""Phase 29B auto-draft (2026-07-29). NOT imported or executed by the live pipeline.
Drafted from cluster: generated models_py assigns [X] more than once in the same file -- this is always a generation mistake (the la
Real instance row IDs this is meant to close: [10086, 10204, 10206, 10314, 10359, 10361, 10363, 10378, 10381]
Passed self-test (real behavioral self-test passed: fires on a genuinely new (never-seen) bad example, stays silent on a legitimate one) and the genericity gate on attempt 1.
Requires human review before promotion -- see Phase 29C.
"""

# PHASE29_DRAFT_META: {"target_spec": "build", "cluster_sig": "generated models_py assigns [X] more than once in the same file -- this is always a generation mistake (the la", "source_row_ids": [10086, 10204, 10206, 10314, 10359, 10361, 10363, 10378, 10381], "drafted_attempt": 1, "has_real_behavioral_self_test": true}

def _validate_no_duplicate_special_model_attributes(generated: GeneratedModuleFiles) -> None:
       """
       Catches generation errors where Odoo special model attributes (e.g., _inherit,
       _description, _name) are assigned more than once in the same models.py file.
       Duplicate assignments silently shadow earlier values, strongly indicating that
       intended content (such as newly generated fields or methods) was accidentally
       overwritten or omitted during synthesis. Each special attribute must appear
       exactly once per model definition.
       """
       import re
       # Match lines that assign to attributes starting with underscore (Odoo special attrs)
       # e.g., _inherit = 'res.partner', _description = "My Model"
       attr_pattern = re.compile(r'^\s*(_\w+)\s*=', re.MULTILINE)
       assignments = attr_pattern.findall(generated.models_py)

       seen = {}
       for attr in assignments:
           if attr in seen:
               raise ValueError(
                   f"Duplicate assignment of special model attribute '{attr}' detected in models.py. "
                   "Odoo special attributes must be assigned exactly once per model. "
                   "This typically indicates that generated content was accidentally overwritten or omitted."
               )
           seen[attr] = True
