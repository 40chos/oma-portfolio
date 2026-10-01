"""Phase 29 (2026-07-29): unit test for
_autofix_correct_date_field_string_comparison_in_tests() -- the real,
confirmed live root cause behind 2+ consecutive identical round
failures on the school_student task's own automated_tests round,
despite explicit chat feedback naming the exact fix: `self.assertEqual(
student.date_of_birth, '1990-01-01')` can never pass because a real
Odoo `fields.Date` always reads back as a genuine `datetime.date`
object, never a string.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from specialists.build.specialist import (
    GeneratedModuleFiles,
    ManifestFields,
    _autofix_correct_date_field_string_comparison_in_tests,
)

_MANIFEST = ManifestFields(
    name="x", version="1.0.0", category="Education", summary="x", author="x", depends=["base"], data=[],
)
_MODELS_PY_WITH_DATE_FIELD = (
    "from odoo import fields, models\n\n"
    "class Student(models.Model):\n"
    "    _name = 'school.student'\n"
    "    date_of_birth = fields.Date()\n"
)


def _make_generated(models_py: str, tests_py: dict[str, str]) -> GeneratedModuleFiles:
    return GeneratedModuleFiles(
        manifest_fields=_MANIFEST.model_copy(deep=True), models_py=models_py, security_csv="x", tests_py=tests_py, notes="",
    )


def test_corrects_the_real_live_string_vs_date_comparison():
    test_content = (
        "from odoo.tests.common import TransactionCase\n\n"
        "class TestStudent(TransactionCase):\n"
        "    def test_student_creation(self):\n"
        "        student = self.Student.create({'date_of_birth': '1990-01-01'})\n"
        "        self.assertEqual(student.date_of_birth, '1990-01-01')\n"
    )
    generated = _make_generated(_MODELS_PY_WITH_DATE_FIELD, {"tests/test_student.py": test_content})
    _autofix_correct_date_field_string_comparison_in_tests(generated)
    fixed = generated.tests_py["tests/test_student.py"]
    assert "self.assertEqual(student.date_of_birth, date(1990, 1, 1))" in fixed
    assert "'date_of_birth': '1990-01-01'" in fixed, "the create() input string must be left untouched"
    assert "from datetime import date" in fixed
    compile(fixed, "<fixed>", "exec")
    print("PASS: the real, confirmed live string-vs-date comparison is corrected, create() input "
          "untouched, and the fixed file still compiles cleanly")


def test_never_touches_an_already_correct_date_comparison():
    test_content = (
        "from datetime import date\n\n"
        "class T:\n"
        "    def test_x(self):\n"
        "        self.assertEqual(r.date_of_birth, date(1990, 1, 1))\n"
    )
    generated = _make_generated(_MODELS_PY_WITH_DATE_FIELD, {"tests/test_x.py": test_content})
    before = generated.tests_py["tests/test_x.py"]
    _autofix_correct_date_field_string_comparison_in_tests(generated)
    assert generated.tests_py["tests/test_x.py"] == before
    print("PASS: never touches an already-correct date() comparison")


def test_never_touches_a_char_field_legitimately_compared_to_a_string():
    models_py = "from odoo import fields, models\n\nclass X(models.Model):\n    _name = 'x.y'\n    s = fields.Char()\n"
    test_content = "class T:\n    def test_x(self):\n        self.assertEqual(r.s, '1990-01-01')\n"
    generated = _make_generated(models_py, {"tests/test_x.py": test_content})
    before = generated.tests_py["tests/test_x.py"]
    _autofix_correct_date_field_string_comparison_in_tests(generated)
    assert generated.tests_py["tests/test_x.py"] == before
    print("PASS: never touches a Char field legitimately compared to a string")


def test_no_op_when_there_is_no_date_field_at_all():
    models_py = "from odoo import fields, models\n\nclass X(models.Model):\n    _name = 'x.y'\n    a = fields.Integer()\n"
    generated = _make_generated(models_py, {"tests/test_x.py": "class T:\n    pass\n"})
    before = generated.tests_py["tests/test_x.py"]
    _autofix_correct_date_field_string_comparison_in_tests(generated)
    assert generated.tests_py["tests/test_x.py"] == before
    print("PASS: no-op when the model defines no Date field at all")


if __name__ == "__main__":
    test_corrects_the_real_live_string_vs_date_comparison()
    test_never_touches_an_already_correct_date_comparison()
    test_never_touches_a_char_field_legitimately_compared_to_a_string()
    test_no_op_when_there_is_no_date_field_at_all()
    print("\nALL CORRECT-DATE-FIELD-STRING-COMPARISON TESTS PASSED")
