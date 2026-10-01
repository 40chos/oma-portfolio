"""Real, confirmed bug found live (2026-08-10, task e65381cc, ticket_views_menu node): a prior
round wrote `decoration-*` attributes for a not-yet-in-scope 'ticket_status_decoration' constraint
-- once committed as `prior_files`, every subsequent scoped-edit round (`_generate_scoped_edits()`,
which only ever proposes a small, NAMED list of edits and copies everything else forward
byte-for-byte) kept carrying it forward unchanged across 7+ consecutive resumes, independent of
how explicit or narrowly-scoped the resume note asking for its removal became. The exact same
"detected baseline defect forces a full rewrite" shape `_find_empty_inherit_class_defect()`
already closes for models_py's own empty `_inherit` classes, applied to views_xml's decoration-*
attributes instead. See specialists/build/specialist.py's `_find_premature_decoration_defect()`
for the full incident and fix.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from specialists.build.specialist import (
    GeneratedModuleFiles,
    ManifestFields,
    _autofix_strip_empty_inherit_only_extension_classes,
    _autofix_strip_premature_invented_field_on_views_focused_round,
    _find_premature_decoration_defect,
    _find_premature_invented_field_defect,
    _validate_no_invented_field_on_views_focused_round,
)

_VIEWS_WITH_DECORATION = (
    '<odoo><record id="v1" model="ir.ui.view">'
    '<field name="model">oma.service.ticket</field>'
    '<field name="arch" type="xml">'
    '<tree decoration-success="state == \'resolved\'"><field name="state"/></tree>'
    '</field></record></odoo>'
)


def test_detects_decoration_when_the_matching_label_is_not_yet_in_scope():
    defect = _find_premature_decoration_defect(_VIEWS_WITH_DECORATION, ["ticket_status_decoration"])
    assert defect is not None
    assert "ticket_status_decoration" in defect
    assert "decoration-*" in defect
    print("PASS: detects premature decoration-* content when a matching not-yet-in-scope label exists")


def test_no_defect_when_views_xml_has_no_decoration_at_all():
    clean_views = (
        '<odoo><record id="v1" model="ir.ui.view">'
        '<field name="arch" type="xml"><tree><field name="state"/></tree></field>'
        '</record></odoo>'
    )
    assert _find_premature_decoration_defect(clean_views, ["ticket_status_decoration"]) is None
    print("PASS: no false positive when views_xml genuinely has no decoration-* content")


def test_no_defect_when_nothing_is_not_yet_in_scope():
    assert _find_premature_decoration_defect(_VIEWS_WITH_DECORATION, []) is None
    assert _find_premature_decoration_defect(_VIEWS_WITH_DECORATION, None) is None
    print("PASS: decoration content with nothing not-yet-in-scope is never flagged -- it may be "
          "this round's own legitimate, in-scope content")


def test_no_defect_when_not_yet_in_scope_labels_dont_mention_decoration():
    result = _find_premature_decoration_defect(_VIEWS_WITH_DECORATION, ["parts_consumed_relation", "ticket_access_restriction"])
    assert result is None, (
        "must never guess -- only fires when a not-yet-in-scope label textually mentions "
        "'decoration', never for unrelated not-yet-in-scope labels"
    )
    print("PASS: never flags when the not-yet-in-scope labels have nothing to do with decoration")


def test_is_a_noop_on_empty_views_xml():
    assert _find_premature_decoration_defect("", ["ticket_status_decoration"]) is None
    assert _find_premature_decoration_defect(None, ["ticket_status_decoration"]) is None
    print("PASS: a no-op on empty/missing views_xml")


_GOAL_TEXT = (
    "This round's own NEW focus is ONLY: 'ticket_views_menu'. Add ONLY the code this one "
    "constraint strictly requires -- views/menu work only, no fields."
)


def test_invented_field_flagged_on_views_focused_round():
    models_py = (
        "from odoo import models, fields\n\n"
        "class Equipment(models.Model):\n"
        "    _inherit = 'oma.equipment'\n\n"
        "    service_ticket_ids = fields.One2many('oma.service.ticket', 'equipment_id')\n"
    )
    defect = _find_premature_invented_field_defect(models_py, _GOAL_TEXT)
    assert defect is not None
    assert "service_ticket_ids" in defect
    print("PASS: flags a field invented on an _inherit-only class with no basis in the goal text")


def test_field_explicitly_named_in_the_goal_text_is_never_flagged():
    models_py = (
        "from odoo import models, fields\n\n"
        "class ServiceTicket(models.Model):\n"
        "    _inherit = 'oma.service.ticket'\n\n"
        "    parts_consumed_ids = fields.Many2many('product.product')\n"
    )
    goal = _GOAL_TEXT + " (original goal also mentions a many2many relation named parts_consumed_ids)"
    assert _find_premature_invented_field_defect(models_py, goal) is None
    print("PASS: a field genuinely named in the original goal text is never flagged, even on a "
          "views-focused round")


def test_never_flags_a_genuinely_new_models_own_field():
    models_py = (
        "from odoo import models, fields\n\n"
        "class Foo(models.Model):\n"
        "    _name = 'oma.foo'\n"
        "    bar = fields.Char()\n"
    )
    assert _find_premature_invented_field_defect(models_py, _GOAL_TEXT) is None
    print("PASS: a genuinely new model's own fields are never this check's concern")


def test_uses_the_narrowed_round_goal_for_the_focus_check_not_only_original_goal():
    """Real, confirmed follow-up bug in this fix's own first version, found live the same
    night: the focus-detection sentence only ever appears in the per-round NARROWED goal, never
    in the separate, real, never-narrowed `original_goal` -- calling this with only
    `original_goal or round_goal` collapsed (as the very first live call site did) meant the
    focus check always saw the un-narrowed text and silently, permanently never fired in
    production, despite every isolated test (which never separated the two) passing.
    """
    models_py = (
        "from odoo import models, fields\n\n"
        "class Equipment(models.Model):\n"
        "    _inherit = 'oma.equipment'\n\n"
        "    service_ticket_ids = fields.One2many('oma.service.ticket', 'equipment_id')\n"
    )
    real_original_goal = (
        "Extend the existing field-service operations module... add a many2many relation "
        "named parts_consumed_ids... add tree/form views and a menu item for both models..."
    )
    real_round_goal = real_original_goal + " " + _GOAL_TEXT
    defect = _find_premature_invented_field_defect(models_py, real_round_goal, real_original_goal)
    assert defect is not None, (
        "must fire using the narrowed round_goal for the focus check, even though "
        "original_goal (checked separately for field mentions) has no focus sentence at all"
    )
    assert "service_ticket_ids" in defect
    print("PASS: correctly uses the narrowed round_goal for the focus gate, not just original_goal")


def test_never_flags_on_a_non_views_focused_round():
    models_py = (
        "from odoo import models, fields\n\n"
        "class Equipment(models.Model):\n"
        "    _inherit = 'oma.equipment'\n\n"
        "    totally_invented = fields.Char()\n"
    )
    goal = "This round's own NEW focus is ONLY: 'ticket_access_restriction'."
    assert _find_premature_invented_field_defect(models_py, goal) is None
    print("PASS: never fires on a round whose own focus isn't views/menu work")


def test_validator_raises_on_a_fresh_candidate_with_an_invented_field():
    """The hard, pre-Code-Review validator wrapping the same detection logic -- catches the
    exact same defect on THIS round's own fresh candidate output, not just the committed
    baseline, giving a much cheaper in-round retry instead of waiting for an external
    Code-Review round-trip to rediscover it.
    """
    goal = (
        "Extend the module... only add tree/form views and a menu item. "
        "This round's own NEW focus is ONLY: 'ticket_views_menu'."
    )
    generated = GeneratedModuleFiles(
        manifest_fields=ManifestFields(
            name="x", version="1.0", category="Uncategorized", summary="", author="",
            depends=["base"], data=[],
        ),
        models_py=(
            "from odoo import models, fields\n\n"
            "class Equipment(models.Model):\n"
            "    _inherit = 'oma.equipment'\n\n"
            "    service_ticket_ids = fields.One2many('oma.service.ticket', 'equipment_id')\n"
        ),
        security_csv="",
        notes="",
    )
    raised = False
    try:
        _validate_no_invented_field_on_views_focused_round(generated, goal)
    except ValueError as e:
        raised = True
        assert "service_ticket_ids" in str(e)
    assert raised, "expected a ValueError naming the invented field"
    print("PASS: the hard validator raises on a fresh candidate with an invented field")


def test_validator_never_raises_on_a_clean_candidate():
    goal = "This round's own NEW focus is ONLY: 'ticket_views_menu'."
    generated = GeneratedModuleFiles(
        manifest_fields=ManifestFields(
            name="x", version="1.0", category="Uncategorized", summary="", author="",
            depends=["base"], data=[],
        ),
        models_py="from odoo import models, fields\n",
        security_csv="",
        notes="",
    )
    _validate_no_invented_field_on_views_focused_round(generated, goal)  # must not raise
    print("PASS: the hard validator never raises on genuinely clean, empty models.py")


def test_autofix_mechanically_strips_the_invented_field_and_its_view_reference():
    """Real, confirmed bug found live (2026-08-10, task e65381cc, ticket_views_menu node): after
    30+ consecutive resumes (notes, forced full rewrites, a hard validator) all failed to
    converge on the model genuinely omitting an invented field, per the user's own explicit
    instruction to fix this generally rather than keep asking, this deterministic autofix removes
    it unconditionally instead of depending on the model to comply.
    """
    goal = "This round's own NEW focus is ONLY: 'ticket_views_menu'. views/menu work only."
    generated = GeneratedModuleFiles(
        manifest_fields=ManifestFields(
            name="x", version="1.0", category="Uncategorized", summary="", author="",
            depends=["base"], data=[],
        ),
        models_py=(
            "from odoo import models, fields\n\n"
            "class Equipment(models.Model):\n"
            "    _inherit = 'oma.equipment'\n\n"
            "    service_ticket_ids = fields.One2many('oma.service.ticket', 'equipment_id')\n"
        ),
        views_xml=(
            '<odoo><record id="v1" model="ir.ui.view">'
            '<field name="arch" type="xml"><form>'
            '<field name="name"/><field name="service_ticket_ids"/>'
            "</form></field></record></odoo>"
        ),
        security_csv="",
        notes="",
    )
    _autofix_strip_premature_invented_field_on_views_focused_round(generated, goal)
    _autofix_strip_empty_inherit_only_extension_classes(generated)
    assert "service_ticket_ids" not in generated.models_py
    assert "service_ticket_ids" not in generated.views_xml
    assert "class Equipment" not in generated.models_py, (
        "the class becomes entirely empty once the invented field is removed -- the sibling "
        "empty-inherit-class stripper must clean up the resulting empty shell too"
    )
    assert "<field name=\"name\"/>" in generated.views_xml, (
        "a genuinely real, unrelated field reference in the same view must survive untouched"
    )
    _validate_no_invented_field_on_views_focused_round(generated, goal)  # must not raise anymore
    print("PASS: the autofix mechanically strips an invented field and its view reference, "
          "and the round now passes its own sibling hard validator")


def test_autofix_never_touches_a_field_genuinely_named_in_the_goal():
    goal = (
        "This round's own NEW focus is ONLY: 'parts_consumed_relation'. Add a many2many relation "
        "named parts_consumed_ids on oma.service.ticket."
    )
    models_py = (
        "from odoo import models, fields\n\n"
        "class ServiceTicket(models.Model):\n"
        "    _inherit = 'oma.service.ticket'\n\n"
        "    parts_consumed_ids = fields.Many2many('product.product')\n"
    )
    generated = GeneratedModuleFiles(
        manifest_fields=ManifestFields(
            name="x", version="1.0", category="Uncategorized", summary="", author="",
            depends=["base"], data=[],
        ),
        models_py=models_py, security_csv="", notes="",
    )
    _autofix_strip_premature_invented_field_on_views_focused_round(generated, goal)
    assert generated.models_py == models_py, "a genuinely requested field must never be stripped"
    print("PASS: never touches a field genuinely named in the goal text")


def test_autofix_is_a_noop_when_nothing_is_invented():
    generated = GeneratedModuleFiles(
        manifest_fields=ManifestFields(
            name="x", version="1.0", category="Uncategorized", summary="", author="",
            depends=["base"], data=[],
        ),
        models_py="from odoo import models, fields\n",
        security_csv="", notes="",
    )
    original = generated.models_py
    _autofix_strip_premature_invented_field_on_views_focused_round(
        generated, "This round's own NEW focus is ONLY: 'ticket_views_menu'.",
    )
    assert generated.models_py == original
    print("PASS: a no-op on genuinely clean, empty models.py")


if __name__ == "__main__":
    test_detects_decoration_when_the_matching_label_is_not_yet_in_scope()
    test_no_defect_when_views_xml_has_no_decoration_at_all()
    test_no_defect_when_nothing_is_not_yet_in_scope()
    test_no_defect_when_not_yet_in_scope_labels_dont_mention_decoration()
    test_is_a_noop_on_empty_views_xml()
    test_invented_field_flagged_on_views_focused_round()
    test_field_explicitly_named_in_the_goal_text_is_never_flagged()
    test_uses_the_narrowed_round_goal_for_the_focus_check_not_only_original_goal()
    test_never_flags_a_genuinely_new_models_own_field()
    test_never_flags_on_a_non_views_focused_round()
    test_validator_raises_on_a_fresh_candidate_with_an_invented_field()
    test_validator_never_raises_on_a_clean_candidate()
    test_autofix_mechanically_strips_the_invented_field_and_its_view_reference()
    test_autofix_never_touches_a_field_genuinely_named_in_the_goal()
    test_autofix_is_a_noop_when_nothing_is_invented()
    print("\nALL FIND-PREMATURE-DECORATION-DEFECT TESTS PASSED")
