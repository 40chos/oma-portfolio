"""Real fix (2026-08-12, day-to-day directions sweep): build_deterministic_search_view_xml()
now verifies every extracted filter's field against the live model schema before committing to
it, with a safe 'active'/'inactive' -> the real boolean `active` field substitution on a miss,
and bails (returns None) on any filter that still can't be confirmed real -- confirmed live gap:
fleet.vehicle has no 'state' field, so a naive field='state' guess crashed with "field(s) ['state']
... not declared anywhere on this model."
"""

import asyncio
import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import specialists.build.specialist as spec


def test_active_label_substitutes_real_active_field_when_no_state_field():
    goal = ("On the fleet.vehicle list, add a quick filter button called 'Active' that shows "
            "only active records.")
    with patch("tools_odoo.odoo_schema_client.get_primary_search_view_xmlid_fast",
               return_value="fleet.fleet_vehicle_view_search"), \
         patch("tools_odoo.odoo_schema_client.get_model_fields_fast",
               return_value=["id", "name", "active", "driver_id"]), \
         patch.object(spec, "is_fast_path_eligible", return_value=True):
        result = asyncio.run(spec.build_deterministic_search_view_xml(
            "fleet.vehicle", goal, "odoo16_dev", task_id="test-task",
        ))
    assert result is not None
    assert "('active','=',True)" in result
    assert "'state'" not in result
    print(f"PASS: 'Active' label substitutes the real active field when no state field exists:\n{result}")


def test_bails_entirely_when_guessed_field_cannot_be_verified_at_all():
    goal = "Add a filter to the sale order list called 'Weird' that shows only weird records."
    with patch("tools_odoo.odoo_schema_client.get_primary_search_view_xmlid_fast",
               return_value="sale.sale_order_view_search"), \
         patch("tools_odoo.odoo_schema_client.get_model_fields_fast",
               return_value=["id", "name", "partner_id"]), \
         patch.object(spec, "is_fast_path_eligible", return_value=True):
        result = asyncio.run(spec.build_deterministic_search_view_xml(
            "sale.order", goal, "odoo16_dev", task_id="test-task",
        ))
    assert result is None
    print("PASS: bails entirely (falls back to LLM) when a guessed field can't be verified real "
          "and has no known safe substitution, never a half-guessed result")


def test_field_already_real_is_used_unchanged():
    goal = "Add a filter to the sale order list: 'Accepted' (state=accepted)."
    with patch("tools_odoo.odoo_schema_client.get_primary_search_view_xmlid_fast",
               return_value="sale.sale_order_view_search"), \
         patch("tools_odoo.odoo_schema_client.get_model_fields_fast",
               return_value=["id", "name", "state"]), \
         patch.object(spec, "is_fast_path_eligible", return_value=True):
        result = asyncio.run(spec.build_deterministic_search_view_xml(
            "sale.order", goal, "odoo16_dev", task_id="test-task",
        ))
    assert result is not None
    assert "('state','=','accepted')" in result
    print(f"PASS: an explicitly-stated field that's genuinely real on the model is used as-is:\n{result}")


if __name__ == "__main__":
    test_active_label_substitutes_real_active_field_when_no_state_field()
    test_bails_entirely_when_guessed_field_cannot_be_verified_at_all()
    test_field_already_real_is_used_unchanged()
    print("\nALL TESTS PASSED")
