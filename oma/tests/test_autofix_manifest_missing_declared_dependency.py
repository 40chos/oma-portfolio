"""Real, confirmed recurring gap found live (2026-08-12, day-to-day directions sweep, Batch A
re-verification): a views-only round (no field/model touch, e.g. a pure quick filter) repeatedly
forgot to add the real, already-known depends_on_module to the manifest's own depends list,
burning a full round's retry budget on a mechanical omission the caller already knew the answer
to. _autofix_manifest_missing_declared_dependency() closes this deterministically.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from specialists.build.specialist import (
    GeneratedModuleFiles,
    ManifestFields,
    _autofix_manifest_missing_declared_dependency,
    _validate_manifest_declares_dependency,
)

_MANIFEST = ManifestFields(
    name="x", version="1.0.0", category="Tools", summary="x", author="x", depends=["base"], data=[],
)


def _gen(depends=None):
    manifest = _MANIFEST if depends is None else ManifestFields(
        name="x", version="1.0.0", category="Tools", summary="x", author="x", depends=depends, data=[],
    )
    return GeneratedModuleFiles(manifest_fields=manifest, models_py="", security_csv="", notes="")


def test_autofix_adds_missing_dependency():
    generated = _gen(depends=["base"])
    _autofix_manifest_missing_declared_dependency(generated, "mis_base_extend")
    assert "mis_base_extend" in generated.manifest_fields.depends
    _validate_manifest_declares_dependency(generated, "mis_base_extend")  # must never raise now
    print("PASS: missing real dependency is added deterministically, validator never fires after")


def test_autofix_is_a_noop_when_already_present():
    generated = _gen(depends=["base", "mis_base_extend"])
    before = list(generated.manifest_fields.depends)
    _autofix_manifest_missing_declared_dependency(generated, "mis_base_extend")
    assert generated.manifest_fields.depends == before
    print("PASS: no-op when the dependency is already declared")


def test_validator_still_raises_without_the_autofix():
    generated = _gen(depends=["base"])
    raised = False
    try:
        _validate_manifest_declares_dependency(generated, "mis_base_extend")
    except ValueError:
        raised = True
    assert raised
    print("PASS: the validator itself still correctly raises when nothing adds the dependency")


if __name__ == "__main__":
    test_autofix_adds_missing_dependency()
    test_autofix_is_a_noop_when_already_present()
    test_validator_still_raises_without_the_autofix()
    print("\nALL TESTS PASSED")
