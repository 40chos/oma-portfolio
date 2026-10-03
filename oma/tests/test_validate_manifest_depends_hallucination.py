"""P11 finding #7, mechanism 2 (docs/planning/PHASE30_P11_ADDITIONS_FROM_P7_TIER3_2026-07-31.md
§1.7, real task 2194a77b-c620-449a-a983-9a67752f2482): tests for
_validate_references_resolve_against_real_target()'s new depends= hallucination check --
a depends=-specific variant of bug class #1, reusing get_module_state_fast()'s real, distinguishable
"NOT_FOUND" string (vs. None on genuine uncertainty). Mocks the schema-client, no live DB needed.
"""

import asyncio
import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from specialists.build.specialist import (
    GeneratedModuleFiles,
    ManifestFields,
    _validate_references_resolve_against_real_target,
)


def _make_generated(depends: list[str], views_xml: str | None = None) -> GeneratedModuleFiles:
    return GeneratedModuleFiles(
        manifest_fields=ManifestFields(
            name="oma_test", version="16.0.1.0.0", category="Uncategorized", summary="test",
            author="test", depends=depends, data=[],
        ),
        models_py="from odoo import models\n",
        views_xml=views_xml,
        security_csv="id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink\n",
        notes="",
    )


def test_rejects_the_real_confirmed_hallucinated_depends():
    generated = _make_generated(depends=["base", "fieldjob"])
    with patch(
        "specialists.build.specialist.get_module_state_fast",
        side_effect=lambda dep, db: "NOT_FOUND" if dep == "fieldjob" else "installed",
    ):
        try:
            asyncio.run(_validate_references_resolve_against_real_target(
                generated, db="odoo16_dev", module_name="oma_test",
            ))
            raise AssertionError("expected ValueError for a hallucinated depends= entry")
        except ValueError as exc:
            assert "fieldjob" in str(exc)
    print("PASS: a hallucinated depends= entry (the real confirmed 'fieldjob' vs 'project_fieldjob' case) is rejected")


def test_real_module_depends_never_flagged():
    generated = _make_generated(depends=["base", "project_fieldjob"])
    with patch(
        "specialists.build.specialist.get_module_state_fast",
        return_value="installed",
    ):
        asyncio.run(_validate_references_resolve_against_real_target(
            generated, db="odoo16_dev", module_name="oma_test",
        ))  # must not raise
    print("PASS: a real, existing depends= module is never flagged")


def test_base_and_own_module_name_are_never_checked():
    generated = _make_generated(depends=["base", "oma_test"])
    with patch(
        "specialists.build.specialist.get_module_state_fast",
        side_effect=AssertionError("get_module_state_fast must never be called for 'base' or the module's own name"),
    ):
        asyncio.run(_validate_references_resolve_against_real_target(
            generated, db="odoo16_dev", module_name="oma_test",
        ))  # must not raise
    print("PASS: 'base' and the module's own name are never even checked, let alone flagged")


def test_genuine_uncertainty_is_never_treated_as_missing():
    generated = _make_generated(depends=["base", "some_module"])
    with patch(
        "specialists.build.specialist.get_module_state_fast",
        return_value=None,  # genuine uncertainty, e.g. gateway/DB unreachable
    ):
        asyncio.run(_validate_references_resolve_against_real_target(
            generated, db="odoo16_dev", module_name="oma_test",
        ))  # must not raise -- None is never treated as "confirmed missing"
    print("PASS: genuine uncertainty (None) is never treated as a confirmed-missing module")


def test_depends_checked_even_with_no_xml_content_at_all():
    """Real bug caught and fixed before shipping: the function's own pre-existing
    `if not all_xml_blobs: return` early-return would have silently skipped the depends= check
    entirely on a manifest-only/pure-model round with no views/security XML yet.
    """
    generated = _make_generated(depends=["base", "fieldjob"], views_xml=None)
    with patch(
        "specialists.build.specialist.get_module_state_fast",
        side_effect=lambda dep, db: "NOT_FOUND" if dep == "fieldjob" else "installed",
    ):
        try:
            asyncio.run(_validate_references_resolve_against_real_target(
                generated, db="odoo16_dev", module_name="oma_test",
            ))
            raise AssertionError("expected ValueError even with no XML content present")
        except ValueError as exc:
            assert "fieldjob" in str(exc)
    print("PASS: a hallucinated depends= entry is caught even on a round with no XML content at all")


if __name__ == "__main__":
    test_rejects_the_real_confirmed_hallucinated_depends()
    test_real_module_depends_never_flagged()
    test_base_and_own_module_name_are_never_checked()
    test_genuine_uncertainty_is_never_treated_as_missing()
    test_depends_checked_even_with_no_xml_content_at_all()
    print("\nALL MANIFEST-DEPENDS-HALLUCINATION TESTS PASSED")
