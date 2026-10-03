"""P12 Tier A item 12 (docs/planning/PHASE30_ARCH_SYNTHESIS_CRITIQUE_FINAL_2026-07-30.md §4):
tests for check_field_exists_on_model()'s new try/except (mirroring its sibling
_run_odoo_shell_script()'s discipline) and the "None is genuine uncertainty, never a
guessed False" fix at both real call sites in specialists/testing_qa/specialist.py.
Zero live SSH/DB/gateway calls -- subprocess.run and check_field_exists_on_model are mocked
directly, same pattern as tests/test_testing_qa_reproduction_target_autocorrect.py.
"""

import asyncio
import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import specialists.testing_qa.specialist as testing_qa_module
from specialists.testing_qa.specialist import ReproductionTarget, TestingQASpecialist
from tools_odoo.spot_check import check_field_exists_on_model


def _make_specialist():
    return TestingQASpecialist(client=None, routine_model="fake-model")


# --- check_field_exists_on_model() itself -----------------------------------

def test_returns_none_on_a_genuine_subprocess_exception():
    with patch("subprocess.run", side_effect=TimeoutError("ssh hung")):
        with patch.dict(os.environ, {
            "OMA_ODOO_SSH_HOST": "x", "OMA_ODOO_SSH_USER": "x", "OMA_ODOO_SSH_CONTAINER": "x",
        }):
            result = check_field_exists_on_model("odoo16_dev", "res.partner", "x")
    assert result is None
    print("PASS: check_field_exists_on_model() returns None (never raises) on a genuine subprocess exception")


def test_returns_none_on_nonzero_exit_code():
    class _FakeProc:
        returncode = 1
        stdout = ""
    with patch("subprocess.run", return_value=_FakeProc()):
        with patch.dict(os.environ, {
            "OMA_ODOO_SSH_HOST": "x", "OMA_ODOO_SSH_USER": "x", "OMA_ODOO_SSH_CONTAINER": "x",
        }):
            result = check_field_exists_on_model("odoo16_dev", "res.partner", "x")
    assert result is None
    print("PASS: check_field_exists_on_model() returns None on a non-zero exit code, same as its sibling")


def test_returns_real_bool_on_genuine_success():
    class _FakeProc:
        returncode = 0
        stdout = "FIELD_CHECK exists=True\n"
    with patch("subprocess.run", return_value=_FakeProc()):
        with patch.dict(os.environ, {
            "OMA_ODOO_SSH_HOST": "x", "OMA_ODOO_SSH_USER": "x", "OMA_ODOO_SSH_CONTAINER": "x",
        }):
            result = check_field_exists_on_model("odoo16_dev", "res.partner", "x")
    assert result is True
    print("PASS: a genuine successful check still returns a real bool, unchanged")


# --- _autocorrect_hallucinated_reproduction_target()'s None handling -------

def test_autocorrect_never_guesses_a_correction_when_existence_check_is_uncertain():
    def fake_check_field_exists(db, model, field_name):
        return None  # genuine uncertainty, e.g. an SSH failure

    with patch.object(testing_qa_module, "check_field_exists_on_model", fake_check_field_exists):
        specialist = _make_specialist()
        target = ReproductionTarget(model="project.fieldjob", field_name="user_id")

        async def run():
            return await specialist._autocorrect_hallucinated_reproduction_target(
                target, "some_module", "odoo16_dev", task_id=None,
            )

        result = asyncio.run(run())
    assert result is target
    print("PASS: _autocorrect_hallucinated_reproduction_target() leaves target unchanged (never guesses a "
          "correction) when the existence check itself is genuinely uncertain (None), not just when the field is confirmed real")


if __name__ == "__main__":
    test_returns_none_on_a_genuine_subprocess_exception()
    test_returns_none_on_nonzero_exit_code()
    test_returns_real_bool_on_genuine_success()
    test_autocorrect_never_guesses_a_correction_when_existence_check_is_uncertain()
    print("\nALL CHECK-FIELD-EXISTS NONE-UNCERTAINTY TESTS PASSED")
