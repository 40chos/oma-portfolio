"""Real, confirmed bug found live (2026-08-10, task e65381cc, a small follow-up correction task
extending the flagship task's own already-installed module, equipment_views_menu node): the
`oma_*` exclusion in `resolve_owning_modules()`/`resolve_owning_modules_fast()`/
`resolve_model_names_from_xmlids()`/`resolve_model_names_from_xmlids_fast()` (tools_odoo, a
"contamination safety" rule meant to stop resolving to some OTHER unrelated past scaffolded
module) had an exemption for `own_module_name` but never for `depends_on_module` -- a task's own
EXPLICIT, already-verified real dependency, which is now routinely `oma_*`-prefixed too since a
separate fix (Bug 40) made `depends_on_module:` correctly reachable for the pipeline's own
previously-scaffolded modules, not just genuine external customer modules. Confirmed live:
`oma.equipment`'s real owning module (`oma_build_a_complete_field_ab52b7f8`) was completely
unresolvable for a module explicitly depending on it, so security_csv's bare `model_oma_equipment`
reference could never be qualified, and Odoo's own CSV loader (which only resolves bare ids
against the installing module's OWN definitions) failed installation every time.
"""

import os
import sys
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tools_odoo.odoo_schema_client import resolve_model_names_from_xmlids_fast, resolve_owning_modules_fast


def _fake_models_proxy(rows):
    proxy = MagicMock()
    proxy.execute_kw.return_value = rows
    return proxy


def test_resolve_owning_modules_fast_resolves_a_depends_on_module_owned_row():
    with patch(
        "tools_odoo.odoo_schema_client._get_or_create_shared_key", return_value=(1, "key"),
    ), patch(
        "tools_odoo.odoo_schema_client._models_proxy",
        return_value=_fake_models_proxy([{"name": "model_oma_equipment", "module": "oma_build_a_complete_field_ab52b7f8"}]),
    ) as mock_proxy_factory:
        result = resolve_owning_modules_fast(
            ["model_oma_equipment"], "db",
            own_module_name="oma_extend_the_existing_field_f2eb1288",
            depends_on_module="oma_build_a_complete_field_ab52b7f8",
        )
    assert result == {"model_oma_equipment": "oma_build_a_complete_field_ab52b7f8"}, (
        f"expected the real depends_on_module owner to resolve despite its own 'oma_' prefix, "
        f"got: {result!r}"
    )
    # Confirm the domain actually sent to Odoo includes the depends_on_module exemption.
    domain_arg = mock_proxy_factory.return_value.execute_kw.call_args[0][5][0]
    assert ("module", "in", ["oma_extend_the_existing_field_f2eb1288", "oma_build_a_complete_field_ab52b7f8"]) in domain_arg
    print("PASS: resolve_owning_modules_fast resolves a depends_on_module-owned row despite its own 'oma_' prefix")


def test_resolve_owning_modules_fast_still_excludes_unrelated_oma_modules():
    with patch(
        "tools_odoo.odoo_schema_client._get_or_create_shared_key", return_value=(1, "key"),
    ), patch(
        "tools_odoo.odoo_schema_client._models_proxy", return_value=_fake_models_proxy([]),
    ):
        result = resolve_owning_modules_fast(
            ["model_oma_equipment"], "db",
            own_module_name="oma_extend_the_existing_field_f2eb1288",
            depends_on_module="oma_build_a_complete_field_ab52b7f8",
        )
    assert result == {"model_oma_equipment": None}
    print("PASS: an unrelated candidate genuinely absent from the (mocked) real registry still resolves to None")


def test_resolve_model_names_from_xmlids_fast_resolves_a_depends_on_module_owned_row():
    with patch(
        "tools_odoo.odoo_schema_client._get_or_create_shared_key", return_value=(1, "key"),
    ), patch(
        "tools_odoo.odoo_schema_client._models_proxy",
    ) as mock_proxy_factory:
        proxy = MagicMock()
        proxy.execute_kw.side_effect = [
            [{"name": "model_oma_equipment", "res_id": 42}],
            [{"id": 42, "model": "oma.equipment"}],
        ]
        mock_proxy_factory.return_value = proxy
        result = resolve_model_names_from_xmlids_fast(
            ["model_oma_equipment"], "db",
            own_module_name="oma_extend_the_existing_field_f2eb1288",
            depends_on_module="oma_build_a_complete_field_ab52b7f8",
        )
    assert result == {"model_oma_equipment": "oma.equipment"}
    print("PASS: resolve_model_names_from_xmlids_fast resolves a depends_on_module-owned model "
          "name despite its own 'oma_' prefix")


if __name__ == "__main__":
    test_resolve_owning_modules_fast_resolves_a_depends_on_module_owned_row()
    test_resolve_owning_modules_fast_still_excludes_unrelated_oma_modules()
    test_resolve_model_names_from_xmlids_fast_resolves_a_depends_on_module_owned_row()
    print("\nALL RESOLVE-OWNING-MODULES-DEPENDS-ON-MODULE-EXEMPTION TESTS PASSED")
