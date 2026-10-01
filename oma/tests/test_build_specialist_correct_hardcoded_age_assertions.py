"""Phase 29 (2026-07-29): unit test for
_autofix_correct_hardcoded_wrong_age_assertions_in_tests() -- the real,
confirmed live root cause behind 4+ consecutive identical round
failures on the school_student task's own automated_tests round: a
test asserted `student.age == 26` for `date_of_birth='2000-12-31'`, but
the real, correct age (the December birthday hasn't occurred yet as of
any date before Dec 31 this year) is 25 -- the compute logic (fixed
earlier this same session by adding the missing @api.depends
decorator) was correct; the TEST's own hardcoded expected value was
wrong, and Build never self-corrected it despite explicit, repeated,
precise chat feedback naming the exact right number.

Phase 30, P5 (§6, item 6, 2026-07-30): the SAME task, still running
after this autofix had already shipped, showed correcting the literal
to today's right number was not enough -- Code-Review's real objection
was never "the number is wrong," it was "this value will become wrong
over time." The autofix now rewrites the assertion to compute the
expected value dynamically (`relativedelta`) instead of patching in a
fresh literal -- these tests were updated to match that real,
intentional behavior change, not preserved against it.
"""

import datetime
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from specialists.build.specialist import (
    GeneratedModuleFiles,
    ManifestFields,
    _autofix_correct_hardcoded_wrong_age_assertions_in_tests,
)

_MANIFEST = ManifestFields(
    name="x", version="1.0.0", category="Education", summary="x", author="x", depends=["base"], data=[],
)


def _make_generated(tests_py: dict[str, str]) -> GeneratedModuleFiles:
    return GeneratedModuleFiles(
        manifest_fields=_MANIFEST.model_copy(deep=True), models_py="", security_csv="x", tests_py=tests_py, notes="",
    )


def test_corrects_the_real_live_wrong_december_birthday_assertion():
    today = datetime.date.today()
    real_age_for_dec_31_2000 = (today.year - 2000) - (1 if (today.month, today.day) < (12, 31) else 0)
    wrong_age = real_age_for_dec_31_2000 + 1  # deliberately wrong, matching the real live bug shape
    test_content = (
        "from odoo.tests.common import TransactionCase\n\n"
        "class TestStudent(TransactionCase):\n"
        "    def test_student_age_computation_edge_case_december_31st(self):\n"
        "        student = self.Student.create({\n"
        "            'name': 'Edge Case Dec 31st',\n"
        "            'student_id': 'EDGEDec31001',\n"
        "            'date_of_birth': '2000-12-31',\n"
        "        })\n"
        f"        self.assertEqual(student.age, {wrong_age})\n"
    )
    generated = _make_generated({"tests/test_student.py": test_content})
    _autofix_correct_hardcoded_wrong_age_assertions_in_tests(generated)
    fixed = generated.tests_py["tests/test_student.py"]
    assert "from dateutil.relativedelta import relativedelta" in fixed
    assert "expected_age = relativedelta(date.today(), date(2000, 12, 31)).years" in fixed
    assert "self.assertEqual(student.age, expected_age)" in fixed
    assert f"self.assertEqual(student.age, {wrong_age})" not in fixed
    compile(fixed, "<fixed>", "exec")
    print("PASS: the wrong hardcoded literal is replaced with a dynamically-computed expected "
          "value (not just corrected to today's number), and the fixed file still compiles cleanly")


def test_converts_an_already_numerically_correct_but_still_hardcoded_assertion_too():
    """Real, intentional behavior change (Phase 30, P5, item 6): even a
    literal that happens to be numerically correct TODAY is still
    time-dependent and will become wrong later -- Code-Review's real
    objection is to the pattern, not to today's specific number, so
    this must convert it too, not skip it as "already fine."
    """
    today = datetime.date.today()
    real_age = (today.year - 2000) - (1 if (today.month, today.day) < (6, 15) else 0)
    test_content = (
        "class TestStudent:\n"
        "    def test_student_age_computation_with_different_dates(self):\n"
        "        student = self.Student.create({'date_of_birth': '2000-06-15'})\n"
        f"        self.assertEqual(student.age, {real_age})\n"
    )
    generated = _make_generated({"tests/test_student.py": test_content})
    _autofix_correct_hardcoded_wrong_age_assertions_in_tests(generated)
    fixed = generated.tests_py["tests/test_student.py"]
    assert "expected_age = relativedelta(date.today(), date(2000, 6, 15)).years" in fixed
    assert "self.assertEqual(student.age, expected_age)" in fixed
    compile(fixed, "<fixed>", "exec")
    print("PASS: an already-numerically-correct-today literal is still converted to a dynamic "
          "computation, since it would drift wrong again next year otherwise")


def test_never_touches_an_assertion_already_converted_to_dynamic_computation():
    test_content = (
        "class TestStudent:\n"
        "    def test_student_age_already_dynamic(self):\n"
        "        from dateutil.relativedelta import relativedelta\n"
        "        from datetime import date\n"
        "        student = self.Student.create({'date_of_birth': '2000-06-15'})\n"
        "        expected_age = relativedelta(date.today(), date(2000, 6, 15)).years\n"
        "        self.assertEqual(student.age, expected_age)\n"
    )
    generated = _make_generated({"tests/test_student.py": test_content})
    before = generated.tests_py["tests/test_student.py"]
    _autofix_correct_hardcoded_wrong_age_assertions_in_tests(generated)
    assert generated.tests_py["tests/test_student.py"] == before
    print("PASS: an assertion already using the dynamic relativedelta computation is left untouched")


def test_never_touches_a_method_missing_either_half():
    test_content = (
        "class TestStudent:\n"
        "    def test_student_age_zero_when_no_birth_date(self):\n"
        "        student = self.Student.create({'name': 'X'})\n"
        "        self.assertEqual(student.age, 0)\n\n"
        "    def test_something_unrelated(self):\n"
        "        self.assertEqual(1, 1)\n"
    )
    generated = _make_generated({"tests/test_student.py": test_content})
    before = generated.tests_py["tests/test_student.py"]
    _autofix_correct_hardcoded_wrong_age_assertions_in_tests(generated)
    assert generated.tests_py["tests/test_student.py"] == before
    print("PASS: never touches a method with no date_of_birth literal or no age assertion at all")


def test_no_op_when_tests_py_is_empty_or_none():
    generated = _make_generated({})
    _autofix_correct_hardcoded_wrong_age_assertions_in_tests(generated)
    assert generated.tests_py == {}
    print("PASS: no-op when tests_py is empty")


if __name__ == "__main__":
    test_corrects_the_real_live_wrong_december_birthday_assertion()
    test_converts_an_already_numerically_correct_but_still_hardcoded_assertion_too()
    test_never_touches_an_assertion_already_converted_to_dynamic_computation()
    test_never_touches_a_method_missing_either_half()
    test_no_op_when_tests_py_is_empty_or_none()
    print("\nALL CORRECT-HARDCODED-AGE-ASSERTIONS TESTS PASSED")
