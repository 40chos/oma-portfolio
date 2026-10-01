"""Phase 29B auto-draft (2026-07-29). NOT imported or executed by the live pipeline.
Drafted from cluster: generated module references method(s) by name that are never actually declared: <button type="X" name='X'> (no
Real instance row IDs this is meant to close: [11968, 12038, 19866, 19868, 22919, 22704, 22870, 23499, 23537, 23565]
Passed self-test (real behavioral self-test passed: fires on a genuinely new (never-seen) bad example, stays silent on a legitimate one) and the genericity gate on attempt 1.
Requires human review before promotion -- see Phase 29C.
"""

# PHASE29_DRAFT_META: {"target_spec": "build", "cluster_sig": "generated module references method(s) by name that are never actually declared: <button type=\"X\" name='X'> (no", "source_row_ids": [11968, 12038, 19866, 19868, 22919, 22704, 22870, 23499, 23537, 23565], "drafted_attempt": 1, "has_real_behavioral_self_test": true}

import re

def _validate_view_button_methods_exist(generated: GeneratedModuleFiles) -> None:
    """
    P11 Tier B item XX (expert design §X.X) -- View XML references to object-type
    buttons must correspond to actual methods declared in the model's Python code.
    Odoo raises an AttributeError at runtime if a button's name attribute points to
    a method that does not exist on the model. This validator extracts all method
    names referenced by <button type="object" name="..."> in the generated views
    and verifies they are defined in the generated Python model code.
    """
    if not generated.views_xml:
        return

    # Extract method names referenced in view buttons
    # Handles both name="..." type="object" and type="object" name="..."
    button_refs = set(re.findall(r'<button[^>]*type=["\']object["\'][^>]*name=["\']([^"\']+)["\'][^>]*>', generated.views_xml))
    button_refs.update(re.findall(r'<button[^>]*name=["\']([^"\']+)["\'][^>]*type=["\']object["\'][^>]*>', generated.views_xml))

    if not button_refs:
        return

    # Extract declared method names from Python code
    # Matches 'def method_name(self, ...):' or 'def method_name(...):'
    declared_methods = set(re.findall(r'def\s+([a-zA-Z_]\w*)\s*\(', generated.models_py))

    # Filter out private methods if desired, but Odoo allows calling any non-private.
    # Actually, it's safer to just check against all declared methods.
    # Let's keep it simple: check against all 'def' names.

    missing = button_refs - declared_methods
    if missing:
        raise ValueError(
            f"View XML references method(s) by name that are never actually declared in the model Python code: "
            f"{', '.join(sorted(missing))}. Odoo will crash with an AttributeError when these buttons are invoked."
        )
