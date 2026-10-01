"""Real, confirmed bug found live (2026-08-11, task 18fca388, post_init_hook cleanup): a
post_init_hook function genuinely runs exactly once, during the real module install itself, but
tools_odoo.spot_check.run_coverage_and_diff()'s own coverage instrumentation does not reliably
attribute lines executed that early in module loading back to the source file -- a real,
correctly-written, successfully-executed post_init_hook (independently verified via a direct
env.ref()/unlink() check against the live database) was still flagged as an uncovered coverage
gap, permanently failing the round regardless of correctness.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from specialists.testing_qa.specialist import _exempt_verified_install_hook_from_coverage_gap

MANIFEST_WITH_HOOK = repr({
    "name": "x", "post_init_hook": "post_init_hook", "depends": ["base"], "data": [],
})
MODELS_PY = (
    "from odoo import api, SUPERUSER_ID\n\n"
    "def post_init_hook(cr, registry):\n"
    "    env = api.Environment(cr, SUPERUSER_ID, {})\n"
    "    access = env.ref('x.y', raise_if_not_found=False)\n"
    "    if access:\n"
    "        access.unlink()\n"
)


def test_exempts_the_entire_post_init_hook_function_body():
    module_files = {"__manifest__.py": MANIFEST_WITH_HOOK, "models/models.py": MODELS_PY}
    hook_start_line = next(
        i + 1 for i, line in enumerate(MODELS_PY.splitlines()) if line.startswith("def post_init_hook")
    )
    total_lines = len(MODELS_PY.splitlines())
    uncovered = [f"models/models.py:{n}" for n in range(hook_start_line, total_lines + 1)]
    result = _exempt_verified_install_hook_from_coverage_gap(module_files, uncovered)
    assert result == [], (
        f"every line of a manifest-declared post_init_hook function's own body must be "
        f"exempted, since it genuinely only ever runs once at real install time: {result!r}"
    )
    print("PASS: every line of a declared post_init_hook function is exempted from the coverage gap")


def test_works_when_module_files_are_keyed_by_full_absolute_path():
    """Real, confirmed bug found live (2026-08-11, same task, same night): `read_module_files()`
    (the real caller this function is actually used with) keys its dict by the FULL absolute
    path (e.g. '/mnt/extra-addons/<module>/__manifest__.py'), never a bare relative name -- a
    naive `.get("__manifest__.py")` always silently returns "", making the exemption
    permanently, invisibly a no-op regardless of any real manifest content. Confirmed live:
    this exact bug shipped in this function's own first version and was caught only because the
    round it was meant to fix kept failing identically even after the fix landed.
    """
    module_files = {
        "/mnt/extra-addons/oma_x/__manifest__.py": MANIFEST_WITH_HOOK,
        "/mnt/extra-addons/oma_x/models/models.py": MODELS_PY,
    }
    hook_start_line = next(
        i + 1 for i, line in enumerate(MODELS_PY.splitlines()) if line.startswith("def post_init_hook")
    )
    total_lines = len(MODELS_PY.splitlines())
    uncovered = [f"models/models.py:{n}" for n in range(hook_start_line, total_lines + 1)]
    result = _exempt_verified_install_hook_from_coverage_gap(module_files, uncovered)
    assert result == [], f"the exemption must work with full-absolute-path-keyed module_files too: {result!r}"
    print("PASS: the exemption works correctly when module_files is keyed by full absolute paths")


def test_never_exempts_when_manifest_declares_no_hook():
    module_files = {
        "__manifest__.py": repr({"name": "x", "depends": ["base"], "data": []}),
        "models/models.py": MODELS_PY,
    }
    uncovered = [f"models/models.py:{n}" for n in range(1, 7)]
    result = _exempt_verified_install_hook_from_coverage_gap(module_files, uncovered)
    assert result == uncovered, "with no hook declared in the manifest, nothing should ever be exempted"
    print("PASS: no exemption fires when the manifest declares no install hook at all")


def test_never_exempts_unrelated_lines_outside_the_hook_function():
    """Same off-by-one boundary already present in every sibling exemption function in this
    file (`_exempt_method_body_lines()` and friends): the shared `(?=^\\1def\\s|\\Z)` lookahead
    pattern always ends the match on a line boundary, so `end_line = start_line + newline_count`
    includes one trailing separator/blank line past the function's own real content -- harmless
    in practice (coverage tools don't report blank lines as "missing"), so this test asserts the
    REAL, consistent boundary rather than a stricter one that would diverge from every sibling.
    """
    models_py_with_extra = MODELS_PY + "\n\ndef some_other_function():\n    pass\n"
    module_files = {"__manifest__.py": MANIFEST_WITH_HOOK, "models/models.py": models_py_with_extra}
    lines = models_py_with_extra.splitlines()
    pass_line = next(i + 1 for i, line in enumerate(lines) if line.strip() == "pass")
    uncovered = [f"models/models.py:{pass_line}"]
    result = _exempt_verified_install_hook_from_coverage_gap(module_files, uncovered)
    assert result == uncovered, (
        f"a real content line genuinely inside a DIFFERENT function must never be swept up by "
        f"this exemption: {result!r}"
    )
    print("PASS: a real content line inside a different, unrelated function is never touched")


if __name__ == "__main__":
    test_exempts_the_entire_post_init_hook_function_body()
    test_works_when_module_files_are_keyed_by_full_absolute_path()
    test_never_exempts_when_manifest_declares_no_hook()
    test_never_exempts_unrelated_lines_outside_the_hook_function()
    print("\nALL POST_INIT_HOOK COVERAGE EXEMPTION TESTS PASSED")
