"""Phase 30 (2026-08-06): unit test for
_target_is_a_real_method_not_a_field() -- the real, confirmed live root
cause behind 2 straight identical "Reproduction FAILED for
project.meerwerk.get_summary_data" round failures on task040 (real task_id
942b77ea-6d09-489d-8a63-893b2b87bdb9).

Root cause: `_extract_reproduction_target()` correctly resolves
`field_name` to a real, plain Python method's own name (the task's own
goal explicitly names one: "Add a plain Python method get_summary_data()
..."), but `_check_field_exists_on_model()` can never confirm a method via
`fields_get()` -- a genuinely correct implementation (flat dict,
`self.ensure_one()`, exactly matching this project's own established
rules, verified via direct redis inspection of the round's own real
generated code) was reported "Reproduction FAILED" purely because this
check was verifying the wrong thing entirely for a task shape it was
never designed to cover. Same false-negative class as the existing
button/menu/non-field-constraint reproduction skips, a fourth shape none
of those three cover.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from specialists.testing_qa.specialist import _target_is_a_real_method_not_a_field

_REAL_TASK040_MODELS_PY = (
    "from odoo import models\n\n"
    "class ProjectMeerwerk(models.Model):\n"
    "    _inherit = 'project.meerwerk'\n\n"
    "    def get_summary_data(self):\n"
    "        self.ensure_one()\n"
    "        return {\n"
    "            'id': self.id,\n"
    "            'name': self.name,\n"
    "        }\n"
)


def test_matches_the_real_live_get_summary_data_method():
    assert _target_is_a_real_method_not_a_field("get_summary_data", _REAL_TASK040_MODELS_PY) is True
    print("PASS: the real, confirmed live 'get_summary_data' method-not-field case is detected")


def test_never_matches_a_genuine_real_field_name():
    models_py = (
        "from odoo import models, fields\n\n"
        "class ProjectMeerwerk(models.Model):\n"
        "    _inherit = 'project.meerwerk'\n"
        "    date_of_birth = fields.Date()\n"
    )
    assert _target_is_a_real_method_not_a_field("date_of_birth", models_py) is False
    print("PASS: a genuine, real field name is never treated as a method")


def test_never_matches_a_method_name_that_isnt_actually_defined():
    assert _target_is_a_real_method_not_a_field("some_other_method", _REAL_TASK040_MODELS_PY) is False
    print("PASS: a method name not actually present in this round's own models.py never matches "
          "(never a guess)")


if __name__ == "__main__":
    test_matches_the_real_live_get_summary_data_method()
    test_never_matches_a_genuine_real_field_name()
    test_never_matches_a_method_name_that_isnt_actually_defined()
    print("\nALL METHOD-SHAPE REPRODUCTION SKIP TESTS PASSED")
