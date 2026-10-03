"""Phase 26A (2026-07-27): check_field_group_restricted_fast()'s own
rewrite -- real, live-confirmed finding: a Python-declared field's
`groups="module.xmlid"` kwarg is a pure runtime attribute, checked
entirely inside Odoo's own fields_get() execution -- it is NEVER
mirrored into `ir.model.fields.groups` (confirmed live: that column
stayed genuinely empty, `[]`, for a real, confirmed-active `groups=`
restriction). This means there is no reliable way to read the real
declared group restriction for a Python-declared field via plain
XML-RPC metadata introspection at all -- fixed by reading the real
generated Python source directly (SSH, via the same `find_module_
defining_model()`/`read_module_files()` mechanism this project's own
Code-Review hallucination filters already use elsewhere) and
paren-balance extracting the field's own declaration to read its real
`groups=` kwarg value.
"""

import os
import sys
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tools_odoo.odoo_schema_client import (
    _extract_field_declaration_source,
    check_field_group_restricted_fast,
)


def test_extract_field_declaration_source_handles_nested_parens():
    """The whole reason this is paren-balanced, not a naive regex: a
    real field declaration commonly has its own nested parens (e.g.
    Selection options) BEFORE the groups= kwarg -- a naive
    non-greedy-to-first-close-paren regex would truncate mid-declaration."""
    source = (
        "from odoo import fields, models\n\n"
        "class ProjectFieldjob(models.Model):\n"
        "    _inherit = 'project.fieldjob'\n\n"
        "    status = fields.Selection([('draft', 'Draft'), ('done', 'Done')], "
        "string='Status', groups='base.group_system')\n\n"
        "    other_field = fields.Char()\n"
    )
    decl = _extract_field_declaration_source(source, "status")
    assert decl is not None
    assert "groups='base.group_system'" in decl
    assert "other_field" not in decl, f"must not overshoot into the NEXT field's own declaration -- got {decl!r}"
    print(f"PASS: paren-balanced extraction correctly handles nested parens:\n{decl}")


def test_extract_field_declaration_source_returns_none_when_not_found():
    source = "from odoo import fields, models\n\nclass X(models.Model):\n    _inherit = 'x'\n"
    decl = _extract_field_declaration_source(source, "nonexistent_field")
    assert decl is None
    print("PASS: returns None (never guesses) when the field's own declaration can't be found")


def _fake_field_rows_proxy(field_exists: bool):
    proxy = MagicMock()

    def execute_kw(db, uid, key, model, method, args, kwargs=None):
        if model == "ir.model.fields" and method == "search_read":
            if not field_exists:
                return []
            return [{"name": "phase26a_restricted_field", "ttype": "char", "relation": False}]
        if model == "ir.model.data" and method == "search_read":
            # domain is a list of tuples: [("module","=",X), ("name","=",Y), ("model","=","res.groups")]
            domain = args[0]
            module_val = domain[0][2]
            name_val = domain[1][2]
            if module_val == "mis_base_extend" and name_val == "group_user_developer_access_fields":
                return [{"res_id": 148}]
            return []
        if model == "res.groups" and method == "read":
            return [{"id": 148, "name": "Developer Access Fields"}]
        raise AssertionError(f"unexpected call: model={model!r} method={method!r} args={args!r}")

    proxy.execute_kw.side_effect = execute_kw
    return proxy


def test_check_field_group_restricted_fast_reads_the_real_declared_group():
    """The exact live bug: fields_get()-based reading always returned
    None (field 'does not exist') for a genuinely-restricted field.
    The fix reads the real source instead."""
    py_source = (
        "from odoo import fields, models\n\n"
        "class ResPartnerPhase26ATest(models.Model):\n"
        "    _inherit = 'res.partner'\n\n"
        "    phase26a_restricted_field = fields.Char(string='Phase26A Restricted Field', "
        "groups='mis_base_extend.group_user_developer_access_fields')\n"
    )
    with patch(
        "tools_odoo.odoo_schema_client._read_real_field_rows",
        return_value=[{"name": "phase26a_restricted_field", "modules": "oma_phase26a_test_fixture"}],
    ), \
         patch("tools_odoo.odoo_schema_client.read_module_files", return_value={"models/models.py": py_source}), \
         patch("tools_odoo.odoo_schema_client._get_or_create_shared_key", return_value=(2, "fake-key")), \
         patch("tools_odoo.odoo_schema_client._models_proxy", return_value=_fake_field_rows_proxy(True)):
        result = check_field_group_restricted_fast(
            "odoo16_dev", "res.partner", "phase26a_restricted_field", "Developer Access Fields",
        )
    assert result is not None, "must not report 'field does not exist' for a genuinely group-restricted field"
    passed, notes = result
    assert passed is True, f"expected the real declared group to be found and matched -- got {passed}, {notes!r}"
    print(f"PASS: {notes}")


def test_check_field_group_restricted_fast_matches_by_group_xmlid():
    """Phase 26A follow-up (2026-07-27): real, live-confirmed gap found
    testing the fields_get() fix itself -- xmlid-preference matching
    (added to the BUTTON-restriction check in Phase 25F/fix 40) was
    never propagated to this, its field-restriction sibling. A goal
    stating "Restrict to group: mis_base_extend.group_user_developer_
    access_fields" (this project's own xmlid-literal goal convention)
    correctly found the real restriction but reported it as NOT
    matching, because only a resolved-display-name comparison existed.
    This test confirms group_xmlid, when given, matches directly
    against the real declared xmlid -- no res.groups resolution RPC
    call needed at all (asserted via the mock's own call tracking)."""
    py_source = (
        "from odoo import fields, models\n\n"
        "class X(models.Model):\n    _inherit = 'res.partner'\n\n"
        "    phase26a_restricted_field = fields.Char(groups='mis_base_extend.group_user_developer_access_fields')\n"
    )
    proxy = MagicMock()

    def execute_kw(db, uid, key, model, method, args, kwargs=None):
        raise AssertionError(
            f"group_xmlid match should short-circuit before any RPC lookup -- "
            f"unexpected call: model={model!r} method={method!r}"
        )

    proxy.execute_kw.side_effect = execute_kw
    with patch(
        "tools_odoo.odoo_schema_client._read_real_field_rows",
        return_value=[{"name": "phase26a_restricted_field", "modules": "oma_phase26a_test_fixture"}],
    ), \
         patch("tools_odoo.odoo_schema_client.read_module_files", return_value={"models/models.py": py_source}), \
         patch("tools_odoo.odoo_schema_client._get_or_create_shared_key", return_value=(2, "fake-key")), \
         patch("tools_odoo.odoo_schema_client._models_proxy", return_value=proxy):
        result = check_field_group_restricted_fast(
            "odoo16_dev", "res.partner", "phase26a_restricted_field",
            group_xmlid="mis_base_extend.group_user_developer_access_fields",
        )
    assert result is not None
    passed, notes = result
    assert passed is True, f"expected a direct xmlid match -- got {passed}, {notes!r}"
    print(f"PASS: {notes}")


def test_check_field_group_restricted_fast_still_returns_none_for_missing_field():
    with patch("tools_odoo.odoo_schema_client._read_real_field_rows", return_value=[{"name": "some_other_field"}]):
        result = check_field_group_restricted_fast(
            "odoo16_dev", "res.partner", "genuinely_invented_field", "Some Group",
        )
    assert result is None
    print("PASS: still correctly returns None for a genuinely nonexistent field")


def test_check_field_group_restricted_fast_correctly_fails_for_wrong_group():
    py_source = (
        "from odoo import fields, models\n\n"
        "class X(models.Model):\n    _inherit = 'res.partner'\n\n"
        "    phase26a_restricted_field = fields.Char(groups='mis_base_extend.group_user_developer_access_fields')\n"
    )
    with patch(
        "tools_odoo.odoo_schema_client._read_real_field_rows",
        return_value=[{"name": "phase26a_restricted_field", "modules": "oma_phase26a_test_fixture"}],
    ), \
         patch("tools_odoo.odoo_schema_client.read_module_files", return_value={"models/models.py": py_source}), \
         patch("tools_odoo.odoo_schema_client._get_or_create_shared_key", return_value=(2, "fake-key")), \
         patch("tools_odoo.odoo_schema_client._models_proxy", return_value=_fake_field_rows_proxy(True)):
        result = check_field_group_restricted_fast(
            "odoo16_dev", "res.partner", "phase26a_restricted_field", "base.group_system",
        )
    assert result is not None
    passed, notes = result
    assert passed is False, f"expected a mismatch against the WRONG claimed group -- got {passed}, {notes!r}"
    print(f"PASS: {notes}")


if __name__ == "__main__":
    test_extract_field_declaration_source_handles_nested_parens()
    test_extract_field_declaration_source_returns_none_when_not_found()
    test_check_field_group_restricted_fast_reads_the_real_declared_group()
    test_check_field_group_restricted_fast_matches_by_group_xmlid()
    test_check_field_group_restricted_fast_still_returns_none_for_missing_field()
    test_check_field_group_restricted_fast_correctly_fails_for_wrong_group()
    print("\nALL PHASE 26A check_field_group_restricted_fast TESTS PASSED")
