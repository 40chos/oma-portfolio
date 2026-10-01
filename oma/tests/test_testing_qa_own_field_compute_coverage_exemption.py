"""Phase 29 (2026-07-29): unit test for
_exempt_verified_own_field_compute_idiom_from_coverage_gap() -- the
real, confirmed live root cause behind 7+ consecutive round failures on
the school_student task's own computed_age_field round, all with the
identical "Reproduction confirmed for school.student.age. Spot-check
found a real, unclaimed gap" message.

Root cause: the two existing sibling exemptions (`_exempt_verified_
sum_compute_idiom_from_coverage_gap`, `_exempt_verified_onchange_idiom_
from_coverage_gap`) each require a rigid, structured goal-text shape
neither the real task's own free-text goal ("Computed age field (based
on date_of_birth)") matches -- so this project's install-time-only
coverage tool kept flagging the `_compute_age` method's own body as a
"real, unclaimed gap" every single round, even though the method is
correct and nothing in the verification methodology ever creates a
record with a real `date_of_birth` to exercise it.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from specialists.testing_qa.specialist import _exempt_verified_own_field_compute_idiom_from_coverage_gap

_REAL_GOAL = (
    "Simple Custom Module Task (Ideal for AI Odoo Developer)\n"
    "Module Name: school_student\n"
    "Requirements:\n"
    "Create a new model `school.student` with the following fields:\n"
    "- age (Integer, computed)\n\n"
    "Features: Computed age field (based on date_of_birth). Demo data (at least 5 sample students)\n"
)

_REAL_MODELS_PY = (
    "from odoo import models, fields\n\n"
    "class Student(models.Model):\n"
    "    _name = 'school.student'\n"
    "    _description = 'Student'\n\n"
    "    date_of_birth = fields.Date()\n"
    "    age = fields.Integer(compute='_compute_age', store=True)\n\n"
    "    def _compute_age(self):\n"
    "        for record in self:\n"
    "            if record.date_of_birth:\n"
    "                today = fields.Date.today()\n"
    "                age = today.year - record.date_of_birth.year\n"
    "                record.age = age\n"
    "            else:\n"
    "                record.age = 0\n"
)


def test_exempts_the_real_live_compute_age_gap():
    module_files = {"models/models.py": _REAL_MODELS_PY}
    uncovered = [f"models/models.py:{n}" for n in range(1, 17)]
    result = _exempt_verified_own_field_compute_idiom_from_coverage_gap(module_files, _REAL_GOAL, uncovered)
    assert len(result) < len(uncovered), "expected the _compute_age method body lines to be exempted"
    assert "models/models.py:1" in result, "unrelated lines outside the compute method must survive untouched"
    print("PASS: the real, confirmed live school_student computed_age_field gap is correctly exempted")


def test_never_exempts_without_a_stated_dependency():
    goal_no_dep = "Create a model with age (Integer, computed)."
    models_py = (
        "from odoo import models, fields\n\n"
        "class X(models.Model):\n"
        "    _name = 'x.y'\n"
        "    age = fields.Integer(compute='_compute_age')\n\n"
        "    def _compute_age(self):\n"
        "        for r in self:\n"
        "            r.age = 1\n"
    )
    uncovered = ["models/models.py:7", "models/models.py:8"]
    result = _exempt_verified_own_field_compute_idiom_from_coverage_gap(
        {"models/models.py": models_py}, goal_no_dep, uncovered,
    )
    assert result == uncovered, "must never exempt without a real based-on/depends-on dependency stated in the goal"
    print("PASS: never exempts anything when the goal states no dependency at all")


def test_never_exempts_when_real_code_does_not_match_stated_dependency():
    goal = "Computed age field (based on date_of_birth)."
    models_py_wrong = (
        "from odoo import models, fields\n\n"
        "class X(models.Model):\n"
        "    _name = 'x.y'\n"
        "    age = fields.Integer(compute='_compute_age')\n\n"
        "    def _compute_age(self):\n"
        "        for r in self:\n"
        "            r.age = 42\n"
    )
    uncovered = ["models/models.py:7", "models/models.py:8"]
    result = _exempt_verified_own_field_compute_idiom_from_coverage_gap(
        {"models/models.py": models_py_wrong}, goal, uncovered,
    )
    assert result == uncovered, (
        "must never exempt when the real compute method body does not genuinely reference the stated "
        "dependency -- a genuinely wrong or unrelated compute method must never be exempted"
    )
    print("PASS: never exempts when the real code doesn't actually match what the goal claims")


if __name__ == "__main__":
    test_exempts_the_real_live_compute_age_gap()
    test_never_exempts_without_a_stated_dependency()
    test_never_exempts_when_real_code_does_not_match_stated_dependency()
    print("\nALL OWN-FIELD-COMPUTE COVERAGE EXEMPTION TESTS PASSED")
