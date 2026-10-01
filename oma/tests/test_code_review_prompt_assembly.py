"""Phase 30 §26 follow-up (2026-08-04): regression tests for the SAME schema-injection /
verbatim-failure-text / section-ordering fix Build already received, now applied to Code-Review
after a same-night audit found it had the identical two prompt-assembly gaps (zero live schema
access anywhere in its own prompt construction; the same capped-8 paraphrased rules history with
no verbatim-failure carve-out).

No real network/DB/gateway calls -- generate_checked is patched to capture the assembled prompt
without making a real call, matching tests/test_build_prompt_assembly.py's own convention.
tools_odoo.odoo_schema_client's boundary functions are patched with realistic fixture data, same
"mock exactly the external boundary" discipline used throughout this session.
"""
import asyncio
import uuid
from unittest.mock import patch

import specialists.code_review.specialist as code_review_module
import tools_odoo.odoo_schema_client as schema_client_module
from contracts.schema import AutonomyTier, CapabilityClass, SpecialistType, TaskContract
from specialists.code_review.specialist import CodeReviewSpecialist

_DB = "odoo16_dev"

_MEERWERK_FIELD_ROWS = [
    {"name": "name", "ttype": "char", "relation": None, "required": True},
    {"name": "amount_total", "ttype": "monetary", "relation": None, "required": False},
]


def _make_contract(**overrides) -> TaskContract:
    base = dict(
        task_id=uuid.uuid4(),
        specialist_type=SpecialistType.code_review,
        capability_class=CapabilityClass.readonly_investigation,
        tier=AutonomyTier.tier_1_readonly,
        goal="On the project.meerwerk record, review the total field.",
        inputs=["diff_module:oma_test_module"],
        rules=[],
        deliverables=["A structured list of findings"],
        compensating_actions=[],
        validation_by="code_review",
        pause_if=[],
        turn_budget=10,
    )
    base.update(overrides)
    return TaskContract(**base)


def _capture_prompt(specialist, contract, files):
    captured = {}

    async def _fake_generate_checked(*args, **kwargs):
        messages = kwargs.get("messages") or args[2]
        captured["prompt"] = messages[0]["content"]
        return '{"findings": [], "overall_assessment": "clean"}'

    with patch.object(schema_client_module, "is_fast_path_eligible", return_value=True), \
         patch.object(schema_client_module, "_read_real_field_rows", return_value=_MEERWERK_FIELD_ROWS), \
         patch.object(schema_client_module, "get_relation_fields_fast", return_value={}), \
         patch.object(code_review_module, "generate_checked", _fake_generate_checked):
        asyncio.run(specialist._review(contract, files, mode="diff"))
    return captured["prompt"]


def test_schema_block_injected_for_code_review():
    """Item 1 analog: Code-Review previously had ZERO live schema access anywhere -- confirmed
    the fix now injects the same curated block Build's own prompt gets."""
    specialist = CodeReviewSpecialist(client=None, db=_DB)
    contract = _make_contract(module_identity="project.meerwerk")
    prompt = _capture_prompt(specialist, contract, {"models/models.py": "class X: pass"})
    assert "<current_schema>" in prompt
    assert "project.meerwerk:" in prompt
    assert "amount_total: monetary" in prompt


def test_schema_block_absent_when_no_db_configured():
    """self.db defaults to "" (e.g. no OMA_ODOO_DB_DUPLICATE_FOR_BUILD configured) -- must
    degrade gracefully, never block a review. Deliberately does NOT mock is_fast_path_eligible
    here (unlike _capture_prompt's default) -- the REAL function must itself reject an empty db
    string, which is the actual behavior under test."""
    specialist = CodeReviewSpecialist(client=None)  # db defaults to ""
    contract = _make_contract(module_identity="project.meerwerk")

    captured = {}

    async def _fake_generate_checked(*args, **kwargs):
        captured["prompt"] = args[2][0]["content"]
        return '{"findings": [], "overall_assessment": "clean"}'

    with patch.object(code_review_module, "generate_checked", _fake_generate_checked):
        asyncio.run(specialist._review(contract, {"models/models.py": "class X: pass"}, mode="diff"))

    assert "<current_schema>" not in captured["prompt"]


def test_previous_round_raw_failure_text_appears_verbatim_in_code_review_prompt():
    """Item 3 analog: the exact same field Build now reads, rendered the same way."""
    specialist = CodeReviewSpecialist(client=None, db=_DB)
    raw_text = "Sandbox install failed: ValueError: The _name attribute ProjectMeerwerk is not valid."
    contract = _make_contract(previous_round_raw_failure_text=raw_text)
    prompt = _capture_prompt(specialist, contract, {"models/models.py": "class X: pass"})
    assert raw_text in prompt
    assert "<previous_attempt_errors>" in prompt


def test_previous_attempt_errors_block_absent_on_round_one():
    specialist = CodeReviewSpecialist(client=None, db=_DB)
    contract = _make_contract(previous_round_raw_failure_text=None)
    prompt = _capture_prompt(specialist, contract, {"models/models.py": "class X: pass"})
    assert "<previous_attempt_errors>" not in prompt


def test_code_review_prompt_section_order():
    """Item 4/5 analog: same order as Build's own fixed prompt."""
    specialist = CodeReviewSpecialist(client=None, db=_DB)
    contract = _make_contract(
        module_identity="project.meerwerk",
        rules=["some prior rule"],
        previous_round_raw_failure_text="literal prior failure text",
    )
    prompt = _capture_prompt(specialist, contract, {"models/models.py": "class X: pass"})

    idx_schema = prompt.index("<current_schema>")
    idx_rules = prompt.index("Rules:")
    idx_prev_errors = prompt.index("<previous_attempt_errors>")
    idx_task = prompt.index("<task>")
    idx_output_contract = prompt.index("<output_contract>")

    assert idx_schema < idx_rules < idx_prev_errors < idx_task < idx_output_contract, (
        "prompt sections must appear in the same stable-first, volatile-last order as Build's own fix"
    )


def test_code_review_output_contract_closed_at_true_end_of_prompt():
    specialist = CodeReviewSpecialist(client=None, db=_DB)
    contract = _make_contract()
    prompt = _capture_prompt(specialist, contract, {"models/models.py": "class X: pass"})
    assert prompt.rstrip().endswith("</output_contract>")
