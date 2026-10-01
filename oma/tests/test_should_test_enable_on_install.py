"""Phase 35 §9.2 item 2 wiring (2026-08-12): _should_test_enable_on_install() decides whether the
real production install_module() call passes test_enable=True, so Odoo's own --test-enable
actually runs a module's real tests/ directory at the one moment it matters most -- checked
against both this round's own new files AND the module's real on-disk state, since a round that
doesn't touch tests/ itself must still re-run tests/ that already exist from an earlier round or
an earlier, separate task against the same module (the "editing an already-installed module" case
this item exists for).
"""

import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from specialists.build.specialist import CodebaseReadError, _should_test_enable_on_install


def test_true_when_this_rounds_own_new_files_include_tests():
    result = _should_test_enable_on_install(
        "some_module", {"tests/test_x.py": "content", "models/models.py": "content"}
    )
    assert result is True
    print("PASS: this round's own new tests/ file triggers test_enable")


def test_true_when_real_on_disk_module_already_has_tests_even_if_this_round_does_not_touch_them():
    with patch(
        "specialists.build.specialist.read_module_files",
        return_value={"tests/test_existing.py": "content", "models/models.py": "content"},
    ):
        result = _should_test_enable_on_install("some_module", {"models/models.py": "new content"})
    assert result is True
    print("PASS: an existing module's real tests/ dir (from an earlier round/task) still triggers test_enable")


def test_false_when_neither_new_files_nor_real_module_has_tests():
    with patch(
        "specialists.build.specialist.read_module_files",
        return_value={"models/models.py": "content", "__manifest__.py": "content"},
    ):
        result = _should_test_enable_on_install("some_module", {"models/models.py": "new content"})
    assert result is False
    print("PASS: no tests/ anywhere means test_enable stays False")


def test_false_when_module_cannot_be_read_at_all():
    with patch(
        "specialists.build.specialist.read_module_files",
        side_effect=CodebaseReadError("no files found"),
    ):
        result = _should_test_enable_on_install("brand_new_module", {"models/models.py": "content"})
    assert result is False
    print("PASS: a brand-new module (not yet on disk) never raises, just returns False")


if __name__ == "__main__":
    test_true_when_this_rounds_own_new_files_include_tests()
    test_true_when_real_on_disk_module_already_has_tests_even_if_this_round_does_not_touch_them()
    test_false_when_neither_new_files_nor_real_module_has_tests()
    test_false_when_module_cannot_be_read_at_all()
    print("\nALL TESTS PASSED")
