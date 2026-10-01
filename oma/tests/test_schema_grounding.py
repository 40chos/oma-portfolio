"""Phase 30 (2026-08-06) root-cause pass: regression coverage for the two general fixes to
tools_odoo/schema_grounding.py found live while investigating why several tasks flagged as
"model-incapable" were actually never shown a fact they needed (task034/task049's missing
TransientModel exposure; task030's missing mail.template grounding). The live-DB-backed halves of
these fixes (get_model_is_transient_fast, list_model_mail_templates_fast, the widened related-
model loop in resolve_current_schema_block) were verified directly against the real odoo16_dev
database during the investigation itself -- this file covers the pure-Python rendering logic that
doesn't require a live connection.
"""
from unittest.mock import patch

from tools_odoo.schema_grounding import _render_model_field_summary, resolve_current_schema_block


def test_render_model_field_summary_flags_transient_model():
    rows = [{"name": "bank_account_id", "ttype": "many2one", "relation": "res.partner.bank", "required": False}]
    rendered = _render_model_field_summary("payment.export.wizard", rows, is_transient=True)
    assert "TransientModel" in rendered.splitlines()[0]
    assert "wizard" in rendered.splitlines()[0].lower()


def test_render_model_field_summary_no_transient_note_for_regular_model():
    rows = [{"name": "name", "ttype": "char", "relation": None, "required": True}]
    rendered = _render_model_field_summary("project.project", rows, is_transient=False)
    assert "TransientModel" not in rendered.splitlines()[0]
    rendered_default = _render_model_field_summary("project.project", rows)
    assert "TransientModel" not in rendered_default.splitlines()[0]
    print("PASS: transient models get an explicit warning note, regular models render unchanged")


def test_render_model_field_summary_shows_real_selection_options_and_readonly():
    """Phase 30 (2026-08-06), category-wide info-gap sweep: a Selection field's real, valid
    option keys (e.g. project.meerwerk.state -> draft/sent/accepted/rejected/invoiced/done,
    confirmed live) were never fetched by the shared field reader at all, let alone rendered --
    any task referencing an existing model's own state/status values had zero grounding for the
    real option keys. `readonly` was already fetched but discarded before reaching this render
    step. Using project.meerwerk's own real state field shape.
    """
    rows = [
        {
            "name": "state", "ttype": "selection", "relation": None, "required": True,
            "readonly": False,
            "selection": "[('draft', 'Draft'), ('sent', 'Sent'), ('accepted', 'Accepted')]",
        },
        {"name": "activity_state", "ttype": "selection", "relation": None, "required": False,
         "readonly": True, "selection": "[('overdue', 'Overdue'), ('today', 'Today')]"},
        {"name": "name", "ttype": "char", "relation": None, "required": True, "readonly": False},
    ]
    rendered = _render_model_field_summary("project.meerwerk", rows)
    lines_by_field = {line.strip().split(":")[0]: line for line in rendered.splitlines()}
    assert "[draft, sent, accepted]" in lines_by_field["state"]
    assert "readonly" not in lines_by_field["state"]
    assert "[overdue, today]" in lines_by_field["activity_state"]
    assert "readonly" in lines_by_field["activity_state"]
    assert "[" not in lines_by_field["name"]  # non-selection field renders unaffected

    # A malformed/unparseable selection string must never raise -- just omit the options.
    broken_rows = [{"name": "x", "ttype": "selection", "relation": None, "required": False,
                     "readonly": False, "selection": "not valid python"}]
    rendered_broken = _render_model_field_summary("m", broken_rows)
    assert "x: selection" in rendered_broken  # no crash, no invented content

    # Critic round 4 (2026-08-06), confirmed reproducible: a pathological selection string
    # (a long run of unary minus signs) makes ast.literal_eval raise MemoryError, not caught by
    # (ValueError, SyntaxError, TypeError) -- breaking this whole module's own "never raises"
    # contract. Must never crash regardless of what the live DB happens to contain.
    pathological_rows = [{"name": "y", "ttype": "selection", "relation": None, "required": False,
                           "readonly": False, "selection": "-" * 100000 + "1"}]
    rendered_pathological = _render_model_field_summary("m", pathological_rows)  # must not raise
    assert "y: selection" in rendered_pathological

    # A Selection field with many real options must be capped, not rendered in full -- this
    # module's own repeated "never a full ORM dump" design rule.
    many_options = "[" + ", ".join(f"('opt{i}', 'Option {i}')" for i in range(40)) + "]"
    many_rows = [{"name": "z", "ttype": "selection", "relation": None, "required": False,
                  "readonly": False, "selection": many_options}]
    rendered_many = _render_model_field_summary("m", many_rows)
    assert "opt0" in rendered_many and "opt14" in rendered_many
    assert "opt15" not in rendered_many
    assert "25 more" in rendered_many
    print("PASS: real Selection options and readonly are rendered, malformed/pathological "
          "selection strings never crash the renderer, a large option set is capped not dumped")


def test_resolve_current_schema_block_excludes_mixin_noise_and_prioritizes_goal_named_relations():
    """Phase 30 (2026-08-06), critic round 1 finding: the widened related-model loop (added to fix
    task041/task045's missing relation grounding) must not let ORM-automatic fields
    (create_uid/write_uid -> res.users) or mail.thread mixin fields (message_ids/
    message_follower_ids/activity_ids -> mail.message/mail.followers/mail.activity) crowd out the
    genuinely business-relevant relation (project.container, this task's own real shape) out of
    the capped slot list -- and a relation the goal text DOES literally name must still come first.
    """
    from contracts.schema import AutonomyTier, CapabilityClass, SpecialistType, TaskContract
    import uuid

    contract = TaskContract(
        task_id=uuid.uuid4(), specialist_type=SpecialistType.bug_fix,
        capability_class=CapabilityClass.module_development, tier=AutonomyTier.tier_1_readonly,
        goal="Add a smart button counting related project.container records.",
        inputs=[], rules=[], deliverables=[], compensating_actions=[],
        validation_by="testing_qa", pause_if=[], turn_budget=10,
        module_identity="project.project",
    )
    relations = {
        "create_uid": "res.users", "write_uid": "res.users",
        "message_follower_ids": "mail.followers", "message_ids": "mail.message",
        "activity_ids": "mail.activity",
        "container_ids": "project.container",
        "aaa_early_alpha_model": "aaa.early.alpha.model",  # sorts before project.container
    }

    def fake_read_rows(model_name, db, login):
        return [{"name": "id", "ttype": "integer", "relation": None, "required": True}]

    with (
        patch("tools_odoo.odoo_schema_client.is_fast_path_eligible", return_value=True),
        patch("tools_odoo.odoo_schema_client._read_real_field_rows", side_effect=fake_read_rows),
        patch("tools_odoo.odoo_schema_client.get_relation_fields_fast", return_value=relations),
        patch("tools_odoo.odoo_schema_client.get_model_is_transient_fast", return_value=False),
    ):
        block = resolve_current_schema_block(contract, "irrelevant_db")

    assert "res.users" not in block, "ORM-automatic create_uid/write_uid noise must be excluded"
    assert "mail.followers" not in block and "mail.message" not in block and "mail.activity" not in block, (
        "mail.thread mixin noise must be excluded"
    )
    assert "project.container" in block, "the goal-named, genuinely relevant relation must survive the cap"
    container_pos = block.index("project.container")
    alpha_pos = block.index("aaa.early.alpha.model") if "aaa.early.alpha.model" in block else 10**9
    assert container_pos < alpha_pos, "a goal-text-named relation must be prioritized ahead of alphabetical fill"
    print("PASS: noise relations excluded, goal-named relation prioritized ahead of alphabetical fill")


if __name__ == "__main__":
    test_render_model_field_summary_flags_transient_model()
    test_render_model_field_summary_no_transient_note_for_regular_model()
    test_resolve_current_schema_block_excludes_mixin_noise_and_prioritizes_goal_named_relations()
