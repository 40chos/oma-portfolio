"""Real, confirmed bug found live (2026-08-10, task 07141af5's flagship run, 9-constraint
field-service task, daily_escalation_cron node): the sibling fix
`_autofix_restore_dropped_model_classes_in_shared_models_py` restores a whole missing model
class when its `_name` disappears -- but deliberately excludes pure `_inherit`-only extension
classes, since whether a whole such class is still needed is a real judgment call. That leaves a
real, distinct gap: a round can regenerate models.py with the SAME `_inherit`-only class still
present, but missing some of its own previously-declared fields/methods. Confirmed live: 3
consecutive rounds of the daily_escalation_cron node regenerated `project.project`'s own
`_inherit`-only extension class without its already-satisfied `open_ticket_count`/
`overdue_ticket_count` fields (plus their compute method and two action methods), while the
round's own unchanged views.xml still referenced them via the existing smart buttons --
`_validate_view_fields_exist_on_model` correctly caught the crash-causing mismatch every time,
but nothing existed to fix it, so all 3 rounds failed identically.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from specialists.build.specialist import (
    GeneratedModuleFiles,
    ManifestFields,
    _autofix_restore_dropped_inherit_extension_members_when_already_satisfied,
)

# Exact real shape from the live bug's own already-committed round.
_OLD_MODELS_PY = (
    "import datetime\n"
    "from odoo import models, fields, api\n\n"
    "class ProjectProject(models.Model):\n"
    "    _inherit = 'project.project'\n\n"
    "    service_ticket_ids = fields.One2many('oma.service.ticket', 'project_id', string='Service Tickets')\n\n"
    "    open_ticket_count = fields.Integer(string='Open Tickets', compute='_compute_ticket_counts')\n"
    "    overdue_ticket_count = fields.Integer(string='Overdue Tickets', compute='_compute_ticket_counts')\n\n"
    "    @api.depends('service_ticket_ids.state', 'service_ticket_ids.create_date')\n"
    "    def _compute_ticket_counts(self):\n"
    "        for project in self:\n"
    "            project.open_ticket_count = 0\n\n"
    "    def action_open_tickets(self):\n"
    "        self.ensure_one()\n"
    "        return {}\n\n"
    "    def action_overdue_tickets(self):\n"
    "        self.ensure_one()\n"
    "        return {}\n\n\n"
    "class ServiceTicket(models.Model):\n"
    "    _name = 'oma.service.ticket'\n"
    "    _description = 'Service Ticket'\n\n"
    "    name = fields.Char(string='Ticket Number', required=True)\n"
)
# Exact real broken shape: ProjectProject kept, but its computed fields/methods dropped while
# adding new cron-related content elsewhere.
_NEW_MODELS_PY_MISSING_FIELDS = (
    "import datetime\n"
    "from odoo import models, fields, api\n\n"
    "class ProjectProject(models.Model):\n"
    "    _inherit = 'project.project'\n\n"
    "    service_ticket_ids = fields.One2many('oma.service.ticket', 'project_id', string='Service Tickets')\n\n\n"
    "class ServiceTicket(models.Model):\n"
    "    _name = 'oma.service.ticket'\n"
    "    _description = 'Service Ticket'\n\n"
    "    name = fields.Char(string='Ticket Number', required=True)\n\n"
    "    def action_check_overdue_and_escalate(self):\n"
    "        pass\n"
)


def _make_generated(models_py: str) -> GeneratedModuleFiles:
    manifest = ManifestFields(
        name="x", version="1.0.0", category="Tools", summary="x", author="x",
        depends=["base"], data=[],
    )
    return GeneratedModuleFiles(
        manifest_fields=manifest, models_py=models_py, security_csv="x",
        security_xml=None, views_xml=None, notes="",
    )


def test_restores_dropped_fields_and_methods_closing_the_live_gap():
    generated = _make_generated(_NEW_MODELS_PY_MISSING_FIELDS)
    old_files_by_relpath = {"models/models.py": _OLD_MODELS_PY}
    constraint_status = {"project_ticket_counts": "satisfied", "daily_escalation_cron": "pending"}

    _autofix_restore_dropped_inherit_extension_members_when_already_satisfied(
        generated, old_files_by_relpath, constraint_status,
    )

    for expected in (
        "open_ticket_count = fields.Integer(string='Open Tickets', compute='_compute_ticket_counts')",
        "overdue_ticket_count = fields.Integer(string='Overdue Tickets', compute='_compute_ticket_counts')",
        "@api.depends('service_ticket_ids.state', 'service_ticket_ids.create_date')",
        "def _compute_ticket_counts(self):",
        "def action_open_tickets(self):",
        "def action_overdue_tickets(self):",
    ):
        assert expected in generated.models_py, f"expected restored content missing: {expected!r}"
    assert "def action_check_overdue_and_escalate(self):" in generated.models_py, (
        "the round's own new method must still be present, unchanged"
    )
    print("PASS: dropped fields, their decorator, and their methods are all restored verbatim")


def test_is_a_noop_when_nothing_is_satisfied_yet():
    """A fresh, non-decomposed first round (or a decomposed round where nothing has been
    satisfied yet) has nothing to protect -- must never fire.
    """
    generated = _make_generated(_NEW_MODELS_PY_MISSING_FIELDS)
    original = generated.models_py
    old_files_by_relpath = {"models/models.py": _OLD_MODELS_PY}

    _autofix_restore_dropped_inherit_extension_members_when_already_satisfied(
        generated, old_files_by_relpath, constraint_status=None,
    )
    assert generated.models_py == original

    _autofix_restore_dropped_inherit_extension_members_when_already_satisfied(
        generated, old_files_by_relpath, constraint_status={"daily_escalation_cron": "pending"},
    )
    assert generated.models_py == original
    print("PASS: a no-op when nothing is genuinely satisfied yet")


def test_is_a_noop_when_nothing_is_actually_missing():
    generated = _make_generated(_OLD_MODELS_PY)
    original = generated.models_py
    old_files_by_relpath = {"models/models.py": _OLD_MODELS_PY}
    constraint_status = {"project_ticket_counts": "satisfied"}

    _autofix_restore_dropped_inherit_extension_members_when_already_satisfied(
        generated, old_files_by_relpath, constraint_status,
    )
    assert generated.models_py == original, "must never duplicate content that's already present"
    print("PASS: a no-op when the round's own content already carries every old member forward")


def test_is_a_noop_when_the_extension_class_disappeared_entirely():
    """A class that vanished COMPLETELY (not just missing some members) is a different,
    deliberately separate judgment call -- this function must leave it alone.
    """
    new_without_class_at_all = (
        "from odoo import models, fields, api\n\n"
        "class ServiceTicket(models.Model):\n"
        "    _name = 'oma.service.ticket'\n"
        "    name = fields.Char()\n"
    )
    generated = _make_generated(new_without_class_at_all)
    original = generated.models_py
    old_files_by_relpath = {"models/models.py": _OLD_MODELS_PY}
    constraint_status = {"project_ticket_counts": "satisfied"}

    _autofix_restore_dropped_inherit_extension_members_when_already_satisfied(
        generated, old_files_by_relpath, constraint_status,
    )
    assert generated.models_py == original, (
        "an entirely-disappeared _inherit-only class must be left alone by this function"
    )
    print("PASS: an entirely-disappeared _inherit-only extension class is left untouched")


def test_never_restores_a_name_declared_model_leaves_that_to_its_own_sibling():
    """A _name-declared model dropping a field is a different (not yet handled) shape this
    function deliberately does not touch -- only pure _inherit-only extension classes.
    """
    old = (
        "from odoo import models, fields\n\n"
        "class Equipment(models.Model):\n"
        "    _name = 'oma.equipment'\n\n"
        "    tracking_number = fields.Char()\n"
    )
    new = (
        "from odoo import models, fields\n\n"
        "class Equipment(models.Model):\n"
        "    _name = 'oma.equipment'\n"
    )
    generated = _make_generated(new)
    original = generated.models_py
    old_files_by_relpath = {"models/models.py": old}
    constraint_status = {"equipment_registry": "satisfied"}

    _autofix_restore_dropped_inherit_extension_members_when_already_satisfied(
        generated, old_files_by_relpath, constraint_status,
    )
    assert generated.models_py == original, (
        "a _name-declared model's own dropped field is out of scope for this function"
    )
    print("PASS: a _name-declared model is left entirely to its own separate handling")


if __name__ == "__main__":
    test_restores_dropped_fields_and_methods_closing_the_live_gap()
    test_is_a_noop_when_nothing_is_satisfied_yet()
    test_is_a_noop_when_nothing_is_actually_missing()
    test_is_a_noop_when_the_extension_class_disappeared_entirely()
    test_never_restores_a_name_declared_model_leaves_that_to_its_own_sibling()
    print("\nALL RESTORE-DROPPED-INHERIT-EXTENSION-MEMBERS TESTS PASSED")
