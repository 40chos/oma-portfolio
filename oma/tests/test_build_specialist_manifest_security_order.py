"""Phase 28C (2026-07-29): unit tests for
_autofix_manifest_security_xml_before_access_csv_order() -- the real,
confirmed live gap found on the school_student task's own
security_groups round, discovered only AFTER
_validate_security_csv_group_id_refers_to_a_defined_group() started
passing content clean: the EXACT SAME "No matching record found for
external id ... in field 'Group'" Odoo crash recurred byte-for-byte
identical across 5 more rounds, proving the real remaining defect was
manifest data LOAD ORDER, not group existence. Odoo processes a
manifest's own `data` list strictly in list order; if `security/ir.
model.access.csv` (referencing a group) is listed before `security/
security.xml` (defining that group), the group genuinely doesn't
exist yet when the CSV is processed.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from specialists.build.specialist import (
    GeneratedModuleFiles,
    ManifestFields,
    _autofix_manifest_security_xml_before_access_csv_order,
)


def _make_generated(data: list[str]) -> GeneratedModuleFiles:
    manifest = ManifestFields(
        name="x", version="1.0.0", category="Tools", summary="x", author="x",
        depends=["base"], data=data,
    )
    return GeneratedModuleFiles(
        manifest_fields=manifest, models_py="", security_csv="id,name\n",
        security_xml=None, views_xml=None, notes="",
    )


def test_fixes_the_real_live_wrong_order():
    # Real, confirmed follow-on gap found live (2026-08-03, task014's own real sandbox install
    # crash): the original version of this fix only ever reordered security.xml ahead of the
    # access CSV -- 'views/views.xml' can ALSO reference a group defined in security.xml (a menu
    # item's own `groups="..."` attribute), and crashes identically ("External ID not found")
    # when views.xml loads first. security.xml must now correctly move ahead of BOTH dependents,
    # so the old "views.xml stays exactly where it was" assertion is no longer correct -- views.xml
    # genuinely needs to move too, this is a real fix, not a regression.
    generated = _make_generated(["views/views.xml", "security/ir.model.access.csv", "security/security.xml"])
    _autofix_manifest_security_xml_before_access_csv_order(generated)
    data = generated.manifest_fields.data
    assert data.index("security/security.xml") < data.index("security/ir.model.access.csv"), (
        "security.xml (which defines the groups) must load before the CSV that references them"
    )
    assert data.index("security/security.xml") < data.index("views/views.xml"), (
        "security.xml must also load before views.xml, which can reference the same groups via "
        "a menu item's own groups=\"...\" attribute"
    )
    print("PASS: the exact real live wrong order (CSV before security.xml) is corrected, closing "
          "the real gap that survived 5 more rounds after the group-existence validator alone")


def test_fixes_task014_real_views_xml_before_security_xml_order():
    generated = _make_generated(["views/views.xml", "security/security.xml"])
    _autofix_manifest_security_xml_before_access_csv_order(generated)
    data = generated.manifest_fields.data
    assert data.index("security/security.xml") < data.index("views/views.xml"), (
        "security.xml (which defines the group a menu item's groups= attribute references) must "
        "load before views.xml"
    )
    print("PASS: task014's own real 'views.xml before security.xml' order is corrected")


def test_never_touches_views_xml_when_already_correctly_ordered():
    generated = _make_generated(["security/security.xml", "views/views.xml"])
    before = list(generated.manifest_fields.data)
    _autofix_manifest_security_xml_before_access_csv_order(generated)
    assert generated.manifest_fields.data == before
    print("PASS: an already-correct security.xml-before-views.xml order is never touched")


def test_never_touches_an_already_correct_order():
    generated = _make_generated(["security/security.xml", "security/ir.model.access.csv", "views/views.xml"])
    before = list(generated.manifest_fields.data)
    _autofix_manifest_security_xml_before_access_csv_order(generated)
    assert generated.manifest_fields.data == before
    print("PASS: an already-correct order is never touched")


def test_is_a_noop_when_only_one_is_present():
    generated = _make_generated(["security/ir.model.access.csv"])
    before = list(generated.manifest_fields.data)
    _autofix_manifest_security_xml_before_access_csv_order(generated)
    assert generated.manifest_fields.data == before
    print("PASS: a no-op when security.xml isn't even referenced at all")


def test_preserves_other_entries_relative_order_around_the_swap():
    generated = _make_generated([
        "models/models.py", "security/ir.model.access.csv", "views/views.xml", "security/security.xml", "data/demo.xml",
    ])
    _autofix_manifest_security_xml_before_access_csv_order(generated)
    data = generated.manifest_fields.data
    assert data.index("security/security.xml") < data.index("security/ir.model.access.csv")
    assert data.index("models/models.py") < data.index("security/security.xml")
    assert data.index("data/demo.xml") == len(data) - 1
    print("PASS: every other entry's own relative position is preserved around the swap")


if __name__ == "__main__":
    test_fixes_the_real_live_wrong_order()
    test_fixes_task014_real_views_xml_before_security_xml_order()
    test_never_touches_views_xml_when_already_correctly_ordered()
    test_never_touches_an_already_correct_order()
    test_is_a_noop_when_only_one_is_present()
    test_preserves_other_entries_relative_order_around_the_swap()
    print("\nALL MANIFEST-SECURITY-ORDER TESTS PASSED")
