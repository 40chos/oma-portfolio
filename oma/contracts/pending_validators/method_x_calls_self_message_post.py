"""Phase 29B auto-draft (2026-07-29). NOT imported or executed by the live pipeline.
Drafted from cluster: method 'X' calls self.message_post(...) with no partner_ids= argument and no message_subscribe(...) call anywh
Real instance row IDs this is meant to close: [25322, 25334, 25801, 25803, 26642, 27073, 27075, 27105, 27124, 27126, 27135]
Passed self-test (real behavioral self-test passed: fires on a genuinely new (never-seen) bad example, stays silent on a legitimate one) and the genericity gate on attempt 2.
Requires human review before promotion -- see Phase 29C.
"""

# PHASE29_DRAFT_META: {"target_spec": "build", "cluster_sig": "method 'X' calls self.message_post(...) with no partner_ids= argument and no message_subscribe(...) call anywh", "source_row_ids": [25322, 25334, 25801, 25803, 26642, 27073, 27075, 27105, 27124, 27126, 27135], "drafted_attempt": 2, "has_real_behavioral_self_test": true}

import ast

def _validate_message_post_requires_partner_ids_or_subscribe(generated: GeneratedModuleFiles) -> None:
    """
    P11 depth-audit item XXX. Catches methods that call `self.message_post(...)`
    without explicitly passing `partner_ids=` and without calling `self.message_subscribe(...)`
    in the same method body. Odoo's `message_post` only emails existing followers or
    explicitly passed partners; omitting both results in a silent internal chatter note
    with no actual email delivery.
    """
    try:
        tree = ast.parse(generated.models_py)
    except SyntaxError:
        return # or raise, but usually validators assume valid syntax from generator

    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue

        method_name = node.name
        has_message_post_without_partner_ids = False
        has_message_subscribe = False

        for child in ast.walk(node):
            if not isinstance(child, ast.Call):
                continue
            if not isinstance(child.func, ast.Attribute):
                continue

            attr_name = child.func.attr
            if attr_name == 'message_post':
                # Check if it's self.message_post
                if isinstance(child.func.value, ast.Name) and child.func.value.id == 'self':
                    has_partner_ids = any(kw.arg == 'partner_ids' for kw in child.keywords)
                    if not has_partner_ids:
                        has_message_post_without_partner_ids = True
            elif attr_name == 'message_subscribe':
                has_message_subscribe = True

        if has_message_post_without_partner_ids and not has_message_subscribe:
            raise ValueError(
                f"Method '{method_name}' calls `self.message_post(...)` without "
                f"`partner_ids=` and lacks a `self.message_subscribe(...)` call. "
                f"This will silently post an internal chatter note without sending "
                f"any emails. Either pass `partner_ids=` to `message_post` or call "
                f"`self.message_subscribe()` earlier in the method."
            )
