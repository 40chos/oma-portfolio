"""Phase 29B (2026-07-29): unit test for
_autofix_missing_validation_error_import() -- the first validator this
project's automated draft -> real behavioral self-test -> genericity
gate -> human-review pipeline (scripts/draft_validator_from_cluster.py,
scripts/promote_pending_validator.py) ever produced, reviewed, and
promoted for real, closing real backlog rows #5354/#5356 ("Missing
import for ValidationError, causing runtime error on validation
failure").

Root cause: `_autofix_models_py_wrong_exception_module()` only fixes
the `models.ValidationError` misuse shape; it does nothing when
generated code correctly writes a bare `raise ValidationError(...)`
but never imports it from `odoo.exceptions` at all -- a real,
structural gap neither existing autofix covered.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from specialists.build.specialist import (
    GeneratedModuleFiles,
    ManifestFields,
    _autofix_missing_validation_error_import,
)

_MANIFEST = ManifestFields(
    name="test_module", version="1.0.0", category="Tools", summary="x", author="x",
    depends=["base"], data=[],
)


def _make_generated(models_py: str) -> GeneratedModuleFiles:
    return GeneratedModuleFiles(
        manifest_fields=_MANIFEST, models_py=models_py,
        security_csv="id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink\n",
        notes="",
    )


def test_adds_missing_import_when_validationerror_is_used_bare():
    generated = _make_generated(
        "from odoo import models\n\n"
        "class TestModel(models.Model):\n"
        "    _name = 'test.model'\n\n"
        "    def _check(self):\n"
        "        raise ValidationError('Invalid')\n"
    )
    _autofix_missing_validation_error_import(generated)
    assert generated.models_py.startswith("from odoo.exceptions import ValidationError\n\n")
    print("PASS: a bare, unimported ValidationError usage gets the missing import added")


def test_never_touches_content_that_already_imports_correctly():
    original = (
        "from odoo import models\n"
        "from odoo.exceptions import ValidationError\n\n"
        "class TestModel(models.Model):\n"
        "    _name = 'test.model'\n\n"
        "    def _check(self):\n"
        "        raise ValidationError('Invalid')\n"
    )
    generated = _make_generated(original)
    _autofix_missing_validation_error_import(generated)
    assert generated.models_py == original
    print("PASS: content that already imports ValidationError correctly is left untouched")


def test_never_touches_content_that_never_uses_validationerror_at_all():
    original = "from odoo import models\n\nclass TestModel(models.Model):\n    _name = 'test.model'\n"
    generated = _make_generated(original)
    _autofix_missing_validation_error_import(generated)
    assert generated.models_py == original
    print("PASS: content with no ValidationError usage at all is left untouched (no unnecessary import added)")


def test_item174_merges_into_an_existing_odoo_exceptions_import_line():
    """P11 fifth pass item 174: previously always PREPENDED a fresh import line, producing two
    separate `from odoo.exceptions import ...` lines when one already existed for a DIFFERENT
    exception (e.g. UserError) -- now merges into the existing line instead.
    """
    original = (
        "from odoo import models\n"
        "from odoo.exceptions import UserError\n\n"
        "class TestModel(models.Model):\n"
        "    _name = 'test.model'\n\n"
        "    def _check(self):\n"
        "        raise ValidationError('Invalid')\n"
    )
    generated = _make_generated(original)
    _autofix_missing_validation_error_import(generated)
    assert generated.models_py.count("from odoo.exceptions import") == 1
    assert "from odoo.exceptions import UserError, ValidationError" in generated.models_py
    print("PASS item174: merges into an existing odoo.exceptions import line instead of adding a second one")


if __name__ == "__main__":
    test_adds_missing_import_when_validationerror_is_used_bare()
    test_never_touches_content_that_already_imports_correctly()
    test_never_touches_content_that_never_uses_validationerror_at_all()
    test_item174_merges_into_an_existing_odoo_exceptions_import_line()
    print("\nALL MISSING-VALIDATIONERROR-IMPORT TESTS PASSED")
