"""Phase 25F (2026-07-26): real, live-confirmed false-negative bug in
`tools_odoo.odoo_schema_client.check_button_group_restricted_fast()`.

Found live during task 008's own first run under the full Phase 25A-25E
architecture: the check reported "button 'action_send' on
'project.meerwerk' has NO group restriction at all" identically across
5 rounds, even though the real generated view genuinely, correctly
declared `groups="base.group_system"` -- confirmed directly by reading
the view's raw `arch_db` via `ir.ui.view.search_read` (present) and by
comparing against `get_views()`'s own resolved arch for the SAME view,
moments later (absent). Root cause: `get_views()`'s returned arch is
POST-PROCESSED for the calling user's own real permissions -- Odoo
strips a `groups=` attribute from any element the calling user already
has access to. Every function in this module defaults to `login=
"Admin"`, and Admin belongs to `base.group_system` on the real dev
target, so the attribute being checked is NEVER present in `get_views()`'s
own arch, regardless of whether the view correctly declares it. Fixed
by resolving the view id via `get_views()` (unaffected -- ids aren't
stripped) then reading the RAW combined arch via `ir.ui.view.
read_combined()`, which returns the fully inherited/merged arch WITHOUT
the per-viewer ACL postprocessing.
"""

import os
import sys
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tools_odoo.odoo_schema_client import check_button_group_restricted_fast


def _fake_models_proxy_acl_stripped_get_views():
    """Simulates the real live bug: get_views() returns a resolved view
    id but an arch with the groups= attribute stripped (as Odoo does for
    a privileged caller); read_combined() on that same view id returns
    the real, raw, unstripped arch."""
    proxy = MagicMock()

    def execute_kw(db, uid, key, model, method, args, kwargs=None):
        if method == "get_views":
            return {
                "views": {
                    "form": {
                        "id": 3968,
                        "arch": (
                            '<form><header>'
                            '<button name="action_send" type="object" string="Send to customer" '
                            'class="btn-primary"/>'
                            '</header></form>'
                        ),
                    }
                }
            }
        if method == "read_combined":
            assert args == [3968], f"expected read_combined to target the resolved view id, got {args!r}"
            return {
                "arch": (
                    '<form><header>'
                    '<button name="action_send" type="object" string="Send to customer" '
                    'class="btn-primary" groups="base.group_system"/>'
                    '</header></form>'
                )
            }
        raise AssertionError(f"unexpected method {method!r}")

    proxy.execute_kw.side_effect = execute_kw
    return proxy


def test_reads_the_real_declared_groups_attribute_not_the_acl_stripped_one():
    with patch(
        "tools_odoo.odoo_schema_client._get_or_create_shared_key", return_value=(2, "fake-key"),
    ), patch(
        "tools_odoo.odoo_schema_client._models_proxy", return_value=_fake_models_proxy_acl_stripped_get_views(),
    ):
        result = check_button_group_restricted_fast(
            "odoo16_dev", "project.meerwerk", "action_send", group_xmlid="base.group_system",
        )
    assert result is not None, "must find the button via the resolved view id"
    passed, notes = result
    assert passed is True, (
        f"expected the real, declared groups= attribute (visible only via read_combined(), "
        f"not get_views()'s own ACL-stripped arch) to be found -- got passed={passed}, notes={notes!r}"
    )
    assert "correctly restricted" in notes
    print(f"PASS: {notes}")


def _fake_models_proxy_genuinely_unrestricted():
    proxy = MagicMock()

    def execute_kw(db, uid, key, model, method, args, kwargs=None):
        if method == "get_views":
            return {"views": {"form": {"id": 1234, "arch": "<form/>"}}}
        if method == "read_combined":
            return {
                "arch": (
                    '<form><header>'
                    '<button name="action_send" type="object" string="Send to customer"/>'
                    '</header></form>'
                )
            }
        raise AssertionError(f"unexpected method {method!r}")

    proxy.execute_kw.side_effect = execute_kw
    return proxy


def test_still_correctly_reports_a_genuinely_unrestricted_button():
    """The fix must not just always report True -- a button genuinely
    missing groups= entirely (per read_combined()'s own raw arch) must
    still correctly fail."""
    with patch(
        "tools_odoo.odoo_schema_client._get_or_create_shared_key", return_value=(2, "fake-key"),
    ), patch(
        "tools_odoo.odoo_schema_client._models_proxy", return_value=_fake_models_proxy_genuinely_unrestricted(),
    ):
        result = check_button_group_restricted_fast(
            "odoo16_dev", "project.meerwerk", "action_send", group_xmlid="base.group_system",
        )
    assert result is not None
    passed, notes = result
    assert passed is False
    assert "NO group restriction at all" in notes
    print(f"PASS: {notes}")


if __name__ == "__main__":
    test_reads_the_real_declared_groups_attribute_not_the_acl_stripped_one()
    test_still_correctly_reports_a_genuinely_unrestricted_button()
    print("\nALL check_button_group_restricted_fast TESTS PASSED")
