"""Phase 26A (2026-07-27): real, live-confirmed false-fail bug in
`tools_odoo.odoo_schema_client` -- a more severe sibling of the
Phase 25F button-groups bug, sitting on the pipeline's own mandatory,
unconditional reproduction-check hot path.

Root cause, confirmed live with a real, disposable test module
(`oma_phase26a_test_fixture`, installed against the real `odoo16_dev`
then removed): Odoo's real `fields_get()` OMITS a field ENTIRELY (not
just its `groups` attribute -- the whole field, unlike the view-arch
case) the moment the calling user (every function in this file
defaults to `login="Admin"`) lacks the group that field's own
`groups=` kwarg requires. Confirmed even after a full warm-worker
restart (ruling out any caching explanation): a field declared
`fields.Char(groups="mis_base_extend.group_user_developer_access_
fields")`, a real group Admin genuinely does not hold, vanished
completely from `fields_get()`'s own returned dict.

Also confirmed live: `ir.model.fields.groups` (the many2many column
Studio-authored fields use) stays genuinely EMPTY for a Python-declared
`groups=` kwarg -- it is a pure runtime attribute, never mirrored into
that column. So `ir.model.fields.search_read()` correctly answers
EXISTENCE (unaffected by the omission bug) but cannot answer WHICH
group a field is restricted to -- that required a second, different
fix: reading the real generated Python source directly (see
test_check_field_group_restricted_fast_reads_real_source.py).

Fixed: `get_model_fields_fast()`, `check_field_exists_on_model_fast()`,
and `get_relation_fields_fast()` all now route through the new
`_read_real_field_rows()` (ir.model.fields), never `fields_get()`
directly, for the existence/type/relation question.
"""

import os
import sys
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tools_odoo.odoo_schema_client import (
    check_field_exists_on_model_fast,
    get_model_fields_fast,
    get_relation_fields_fast,
)


def _fake_models_proxy(rows):
    proxy = MagicMock()

    def execute_kw(db, uid, key, model, method, args, kwargs=None):
        assert model == "ir.model.fields", f"expected the unprivileged ir.model.fields read, got model={model!r}"
        assert method == "search_read"
        return rows

    proxy.execute_kw.side_effect = execute_kw
    return proxy


def test_get_model_fields_fast_includes_a_group_restricted_field():
    """The exact live bug: a field genuinely restricted to a group the
    calling identity lacks must still be reported as existing."""
    rows = [
        {"name": "id", "ttype": "integer", "relation": False, "required": False, "readonly": True},
        {"name": "name", "ttype": "char", "relation": False, "required": True, "readonly": False},
        {"name": "phase26a_restricted_field", "ttype": "char", "relation": False, "required": False, "readonly": False},
    ]
    with patch("tools_odoo.odoo_schema_client._get_or_create_shared_key", return_value=(2, "fake-key")), \
         patch("tools_odoo.odoo_schema_client._models_proxy", return_value=_fake_models_proxy(rows)):
        result = get_model_fields_fast("res.partner", "odoo16_dev")
    assert result is not None
    assert "phase26a_restricted_field" in result, (
        f"a group-restricted field must still be reported as existing -- got {result!r}"
    )
    print("PASS: get_model_fields_fast() includes a group-restricted field")


def test_get_model_fields_fast_returns_none_for_nonexistent_model():
    with patch("tools_odoo.odoo_schema_client._get_or_create_shared_key", return_value=(2, "fake-key")), \
         patch("tools_odoo.odoo_schema_client._models_proxy", return_value=_fake_models_proxy([])):
        result = get_model_fields_fast("does.not.exist", "odoo16_dev")
    assert result is None, "a genuinely nonexistent model must still correctly return None"
    print("PASS: get_model_fields_fast() still correctly returns None for a nonexistent model")


def test_check_field_exists_on_model_fast_true_for_group_restricted_field():
    rows = [
        {"name": "id", "ttype": "integer", "relation": False, "required": False, "readonly": True},
        {"name": "phase26a_restricted_field", "ttype": "char", "relation": False, "required": False, "readonly": False},
    ]
    with patch("tools_odoo.odoo_schema_client._get_or_create_shared_key", return_value=(2, "fake-key")), \
         patch("tools_odoo.odoo_schema_client._models_proxy", return_value=_fake_models_proxy(rows)):
        result = check_field_exists_on_model_fast("odoo16_dev", "res.partner", "phase26a_restricted_field")
    assert result is True, (
        f"a group-restricted field must be reported as existing, not missing -- got {result!r}"
    )
    print("PASS: check_field_exists_on_model_fast() correctly returns True for a group-restricted field")


def test_check_field_exists_on_model_fast_false_for_genuinely_missing_field():
    rows = [{"name": "id", "ttype": "integer", "relation": False, "required": False, "readonly": True}]
    with patch("tools_odoo.odoo_schema_client._get_or_create_shared_key", return_value=(2, "fake-key")), \
         patch("tools_odoo.odoo_schema_client._models_proxy", return_value=_fake_models_proxy(rows)):
        result = check_field_exists_on_model_fast("odoo16_dev", "res.partner", "genuinely_invented_field")
    assert result is False, "a genuinely nonexistent field must still correctly return False, not True"
    print("PASS: check_field_exists_on_model_fast() still correctly returns False for an invented field")


def test_get_relation_fields_fast_includes_a_group_restricted_relation():
    rows = [
        {"name": "id", "ttype": "integer", "relation": False, "required": False, "readonly": True},
        {"name": "partner_id", "ttype": "many2one", "relation": "res.partner", "required": False, "readonly": False},
        {"name": "restricted_line_ids", "ttype": "one2many", "relation": "res.partner", "required": False, "readonly": False},
    ]
    with patch("tools_odoo.odoo_schema_client._get_or_create_shared_key", return_value=(2, "fake-key")), \
         patch("tools_odoo.odoo_schema_client._models_proxy", return_value=_fake_models_proxy(rows)):
        result = get_relation_fields_fast("project.meerwerk", "odoo16_dev")
    assert result == {"partner_id": "res.partner", "restricted_line_ids": "res.partner"}, (
        f"a group-restricted relational field must still be included -- got {result!r}"
    )
    print("PASS: get_relation_fields_fast() includes a group-restricted relational field")


if __name__ == "__main__":
    test_get_model_fields_fast_includes_a_group_restricted_field()
    test_get_model_fields_fast_returns_none_for_nonexistent_model()
    test_check_field_exists_on_model_fast_true_for_group_restricted_field()
    test_check_field_exists_on_model_fast_false_for_genuinely_missing_field()
    test_get_relation_fields_fast_includes_a_group_restricted_relation()
    print("\nALL PHASE 26A FIELD-EXISTENCE TESTS PASSED")
