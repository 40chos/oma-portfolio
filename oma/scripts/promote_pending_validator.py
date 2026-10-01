"""Phase 29C (2026-07-29): the human-confirmation promotion step for a
Phase 29B auto-draft. the project owner's own framing: draft + self-test are fully
automated; promotion into the real system happens "only when we confirm
it." This script IS that confirmation step -- not another prose plan,
real code that does the mechanical merge.

What this automates:
  - Appending the drafted function's real code to the correct specialist
    file (read from the draft's own PHASE29_DRAFT_META header, written
    by draft_validator_from_cluster.py at draft time).
  - Verifying the target file still compiles cleanly after the append.
  - Marking every source proposed-rule row this draft was grounded in as
    `superseded` (via the same supersede_proposed_rule() used by the
    backlog triage), so the pipeline's own bookkeeping stays accurate.
  - Archiving the promoted draft file (moved, not deleted, so there is
    always a real record of what was promoted and when).

What this deliberately does NOT automate, by design:
  - Wiring the new function into the actual validation call chain
    (deciding call order relative to other autofixes/validators, which
    parameters to thread through). This session found real bugs caused
    by call-order mistakes (e.g. the manifest security_xml-before-
    access_csv ordering bug) -- that judgment call stays human, every
    time, no exception.
  - Writing the function's own unit test. A promoted function with zero
    test coverage is a regression waiting to happen; the promotion
    output explicitly reminds the human of this as a required next step,
    never silently skips it.

Usage:
    python3 scripts/promote_pending_validator.py --list
    python3 scripts/promote_pending_validator.py --promote <filename> [--yes]
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.rule_backlog_triage import _CATALOG_SOURCE_FILES  # noqa: E402

_REPO_ROOT = Path(__file__).resolve().parent.parent
_PENDING_DIR = _REPO_ROOT / "contracts" / "pending_validators"
_PROMOTED_DIR = _REPO_ROOT / "contracts" / "promoted_validators"
_META_RE = re.compile(r"^# PHASE29_DRAFT_META: (\{.*\})\s*$", re.MULTILINE)
_FUNC_NAME_RE = re.compile(r"^(?:async )?def (_(?:validate|autofix)_\w+)\(", re.MULTILINE)

_SPEC_PATHS = dict(_CATALOG_SOURCE_FILES)


def _parse_draft(path: Path) -> tuple[dict, str]:
    text = path.read_text()
    m = _META_RE.search(text)
    if not m:
        raise ValueError(f"{path} has no PHASE29_DRAFT_META header -- not a real Phase 29B draft, refusing to promote")
    meta = json.loads(m.group(1))
    code = text[m.end():].strip("\n")
    return meta, code


def _validate_code_is_a_single_function(code: str) -> str:
    """Real safety check: the draft must parse as valid Python AND
    contain exactly one top-level function definition, matching this
    codebase's own `_validate_*`/`_autofix_*` naming convention -- never
    silently append something the draft pipeline didn't actually intend
    (an incomplete snippet, a stray import, multiple functions bundled
    together). Returns the function's real name.
    """
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        raise ValueError(f"draft does not parse as valid Python: {exc}") from exc
    top_level_funcs = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    if len(top_level_funcs) != 1:
        raise ValueError(f"expected exactly one top-level function in the draft, found {len(top_level_funcs)}")
    name = top_level_funcs[0].name
    if not _FUNC_NAME_RE.search(code):
        raise ValueError(f"function name {name!r} does not match the required _validate_*/_autofix_* convention")
    return name


def list_pending() -> None:
    if not _PENDING_DIR.exists() or not any(_PENDING_DIR.glob("*.py")):
        print("No pending drafts in contracts/pending_validators/.")
        return
    for path in sorted(_PENDING_DIR.glob("*.py")):
        if path.name.startswith("test_"):
            continue  # companion test file for a sibling draft, not itself a promotable draft
        try:
            meta, code = _parse_draft(path)
            name = _validate_code_is_a_single_function(code)
        except ValueError as exc:
            print(f"{path.name}: INVALID -- {exc}")
            continue
        print(f"{path.name}")
        print(f"  function:        {name}")
        print(f"  target_spec:     {meta.get('target_spec')} -> {_SPEC_PATHS.get(meta.get('target_spec'), '?')}")
        print(f"  cluster:         {meta.get('cluster_sig')}")
        print(f"  source_row_ids:  {meta.get('source_row_ids')}")


def _install_and_run_companion_test(test_path: Path, func_name: str) -> None:
    """Final proof step: installs the drafted validator's own generated
    test into the REAL `tests/` directory and actually runs it against
    the just-merged, real specialist file -- not the in-memory namespace
    trick draft_validator_from_cluster.py's self_test() uses while
    drafting (necessary then, since the function didn't exist on disk
    yet). This is the genuinely final, strongest proof available: the
    same test, run for real, against the real merged code.

    Never blocks or reverts the promotion on failure (the validator
    already compiles and was already reviewed/confirmed by the human
    running this command) -- but prints a loud, impossible-to-miss
    warning if the real run doesn't match what drafting-time reported,
    since that would itself be a real, actionable finding.
    """
    import subprocess

    raw = test_path.read_text()
    # Real, confirmed bug found live (2026-07-30, human-grade review of
    # the first two real promotions): the draft's own header (written by
    # draft_validator_from_cluster.py for contracts/pending_validators/,
    # TWO directories below the repo root) uses `sys.path.insert(0,
    # os.path.join(os.path.dirname(__file__), '..', '..'))` -- correct
    # for that location, but ONE level too many once installed here in
    # tests/, which is only one directory below the repo root. Left
    # uncorrected, the installed test only ever ran via THIS script's
    # own special `sys.path.insert(0, '.')` invocation below, not via
    # the standard `pytest tests/` every other real test in this suite
    # uses -- confirmed live: both of the first two real promotions
    # produced a test that errored with `ModuleNotFoundError: No module
    # named 'specialists'` under a normal pytest run.
    raw = raw.replace(
        "sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))",
        "sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))",
    )
    if f"import {func_name}" not in raw and f", {func_name}" not in raw:
        raw = raw.replace(
            "def test_draft_catches_the_pattern():",
            f"from specialists.build.specialist import {func_name}\n\n\ndef test_draft_catches_the_pattern():",
            1,
        )
    tests_dir = _REPO_ROOT / "tests"
    installed_path = tests_dir / f"test_promoted_{func_name}.py"
    installed_path.write_text(raw)

    result = subprocess.run(
        [sys.executable, "-c", f"import sys; sys.path.insert(0, '.'); from tests.test_promoted_{func_name} import test_draft_catches_the_pattern; test_draft_catches_the_pattern(); print('REAL RUN PASSED')"],
        cwd=str(_REPO_ROOT), capture_output=True, text=True, timeout=30,
    )
    if "REAL RUN PASSED" in result.stdout:
        print(f"Installed real test at tests/test_promoted_{func_name}.py -- ran it for real against the merged code: PASSED.")
    else:
        print(
            f"WARNING: installed tests/test_promoted_{func_name}.py but the REAL run (against the "
            f"now-merged code) did not pass -- this needs human attention before relying on this "
            f"validator:\n{result.stdout}\n{result.stderr}"
        )


def promote(filename: str, auto_yes: bool) -> None:
    path = _PENDING_DIR / filename
    if not path.exists():
        raise SystemExit(f"no such pending draft: {path}")
    try:
        meta, code = _parse_draft(path)
        func_name = _validate_code_is_a_single_function(code)
    except ValueError as exc:
        raise SystemExit(f"refusing to promote {filename}: {exc}") from exc

    target_spec = meta.get("target_spec")
    target_relpath = _SPEC_PATHS.get(target_spec)
    if not target_relpath:
        raise SystemExit(f"unknown target_spec {target_spec!r} -- known specs: {list(_SPEC_PATHS)}")
    target_path = _REPO_ROOT / target_relpath

    print(f"=== Promoting {filename} ===")
    print(f"Function:    {func_name}")
    print(f"Target file: {target_relpath}")
    print(f"Source rows: {meta.get('source_row_ids')}")
    print("\n--- Code to be appended ---")
    print(code)
    print("--- end code ---\n")
    print(
        "REQUIRED MANUAL STEP after this promotion: this function is appended to the file but is "
        "NOT wired into the live validation call chain yet, and has NO unit test yet. Both must be "
        "added by a human before this validator takes effect -- call-order and parameter-threading "
        "decisions are deliberately never automated (see this script's own module docstring)."
    )

    if not auto_yes:
        answer = input("\nConfirm promotion? [y/N] ").strip().lower()
        if answer != "y":
            print("Aborted -- draft left untouched in contracts/pending_validators/.")
            return

    original = target_path.read_text()
    updated = original.rstrip("\n") + "\n\n\n" + code + "\n"
    target_path.write_text(updated)

    try:
        compile(updated, str(target_path), "exec")
    except SyntaxError as exc:
        target_path.write_text(original)
        raise SystemExit(f"promoted code broke compilation of {target_relpath}, reverted: {exc}")
    print(f"Appended to {target_relpath} -- compiles cleanly.")

    test_path = _PENDING_DIR / f"test_{filename}"
    if test_path.exists():
        _install_and_run_companion_test(test_path, func_name)

    from manager.correction import supersede_proposed_rule

    for row_id in meta.get("source_row_ids", []):
        supersede_proposed_rule(row_id, func_name)
    print(f"Marked {len(meta.get('source_row_ids', []))} source row(s) as superseded by {func_name!r}.")

    _PROMOTED_DIR.mkdir(exist_ok=True)
    shutil.move(str(path), str(_PROMOTED_DIR / filename))
    print(f"Archived draft to contracts/promoted_validators/{filename}.")
    if test_path.exists():
        shutil.move(str(test_path), str(_PROMOTED_DIR / test_path.name))
        print(f"Archived its companion test to contracts/promoted_validators/{test_path.name}.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--promote", metavar="FILENAME")
    parser.add_argument("--yes", action="store_true", help="skip the interactive confirmation prompt")
    args = parser.parse_args()

    if args.promote:
        promote(args.promote, args.yes)
    else:
        list_pending()


if __name__ == "__main__":
    main()
