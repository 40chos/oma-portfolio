"""Phase 29C (2026-07-29): real, end-to-end test of the promotion CLI
built to be the human-confirmation merge step the project owner asked for --
"we don't need you to derive all these validators manually... create a
different AI layer that initially does everything automatically, and
after that, when we only confirm it, it merges with our real system."

Every path (target specialist file, pending/promoted draft directories)
is monkeypatched to tmp_path fixtures -- this test never touches this
repo's own real specialist source files or the real Postgres backlog.
"""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import scripts.promote_pending_validator as promote_module

_DRAFT_CODE = '''def _validate_example_thing(generated):
    """Draft validator for testing."""
    if not generated:
        raise ValueError("example thing missing")
'''


def _write_draft(pending_dir, filename="example_cluster.py", source_row_ids=(101, 102)):
    meta = {
        "target_spec": "build",
        "cluster_sig": "example cluster signature",
        "source_row_ids": list(source_row_ids),
        "drafted_attempt": 1,
    }
    header = (
        '"""Phase 29B auto-draft. NOT imported or executed by the live pipeline."""\n\n'
        f"# PHASE29_DRAFT_META: {json.dumps(meta)}\n\n"
    )
    path = pending_dir / filename
    path.write_text(header + _DRAFT_CODE)
    return path


def test_list_pending_shows_real_metadata_from_the_draft_header(tmp_path, monkeypatch, capsys):
    pending_dir = tmp_path / "pending"
    pending_dir.mkdir()
    _write_draft(pending_dir)
    monkeypatch.setattr(promote_module, "_PENDING_DIR", pending_dir)

    promote_module.list_pending()
    out = capsys.readouterr().out
    assert "_validate_example_thing" in out
    assert "[101, 102]" in out
    assert "example cluster signature" in out
    print("PASS: --list surfaces the real function name, target spec, and source row ids from the draft's own header")


def test_promote_appends_code_and_compiles_and_supersedes_and_archives(tmp_path, monkeypatch, capsys):
    pending_dir = tmp_path / "pending"
    promoted_dir = tmp_path / "promoted"
    pending_dir.mkdir()
    target_file = tmp_path / "fake_specialist.py"
    target_file.write_text("def _existing_validator(x):\n    pass\n")

    draft_path = _write_draft(pending_dir)
    monkeypatch.setattr(promote_module, "_PENDING_DIR", pending_dir)
    monkeypatch.setattr(promote_module, "_PROMOTED_DIR", promoted_dir)
    monkeypatch.setattr(promote_module, "_SPEC_PATHS", {"build": "fake_specialist.py"})
    monkeypatch.setattr(promote_module, "_REPO_ROOT", tmp_path)

    superseded_calls = []
    import manager.correction as correction_module
    monkeypatch.setattr(correction_module, "supersede_proposed_rule", lambda rid, name: superseded_calls.append((rid, name)))
    monkeypatch.setattr(promote_module, "input", lambda _prompt: "y", raising=False)

    promote_module.promote("example_cluster.py", auto_yes=False)

    updated_content = target_file.read_text()
    assert "_validate_example_thing" in updated_content
    assert "_existing_validator" in updated_content, "must append, never overwrite existing content"
    compile(updated_content, "fake_specialist.py", "exec")

    assert superseded_calls == [(101, "_validate_example_thing"), (102, "_validate_example_thing")], (
        f"expected both source rows superseded by the promoted function's real name, got {superseded_calls}"
    )
    assert not draft_path.exists(), "the draft must be moved out of pending_validators/ on successful promotion"
    assert (promoted_dir / "example_cluster.py").exists(), "the draft must be archived, not deleted outright"
    out = capsys.readouterr().out
    assert "REQUIRED MANUAL STEP" in out, "must always remind the human that wiring + tests are still needed"
    print("PASS: promotion appends real code, verifies it compiles, supersedes every source row by the "
          "real promoted function name, and archives (never deletes) the draft")


def test_promote_reverts_and_refuses_if_the_appended_code_would_break_compilation(tmp_path, monkeypatch, capsys):
    pending_dir = tmp_path / "pending"
    promoted_dir = tmp_path / "promoted"
    pending_dir.mkdir()
    target_file = tmp_path / "fake_specialist.py"
    original_content = "def _existing_validator(x):\n    pass\n"
    target_file.write_text(original_content)

    meta = {"target_spec": "build", "cluster_sig": "broken draft", "source_row_ids": [999], "drafted_attempt": 1}
    header = f'"""draft"""\n\n# PHASE29_DRAFT_META: {json.dumps(meta)}\n\n'
    broken_code = "def _validate_broken(generated)\n    pass\n"  # missing colon -- deliberately invalid
    (pending_dir / "broken.py").write_text(header + broken_code)

    monkeypatch.setattr(promote_module, "_PENDING_DIR", pending_dir)
    monkeypatch.setattr(promote_module, "_PROMOTED_DIR", promoted_dir)
    monkeypatch.setattr(promote_module, "_SPEC_PATHS", {"build": "fake_specialist.py"})
    monkeypatch.setattr(promote_module, "_REPO_ROOT", tmp_path)

    try:
        promote_module.promote("broken.py", auto_yes=True)
        raised = False
    except SystemExit:
        raised = True

    assert raised, "a draft that fails the single-function AST check must refuse to promote"
    assert target_file.read_text() == original_content, "target file must be left untouched when the draft is invalid"
    print("PASS: an invalid draft is refused before ever touching the target specialist file")


def test_installed_companion_test_uses_the_correct_relative_import_path(tmp_path, monkeypatch, capsys):
    """Real, confirmed bug found live (2026-07-30, human-grade review of
    the first two real promotions): the companion test's own header,
    written by draft_validator_from_cluster.py for contracts/pending_
    validators/ (two directories below the repo root), used `sys.path.
    insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))` --
    correct THERE, but one level too many once installed into tests/
    (only one directory below the repo root). Confirmed live: both real
    installed tests errored with `ModuleNotFoundError: No module named
    'specialists'` under a normal `pytest tests/` run, even though
    promotion's own special-invocation self-check reported PASSED.
    """
    pending_dir = tmp_path / "pending"
    promoted_dir = tmp_path / "promoted"
    tests_dir = tmp_path / "tests"
    pending_dir.mkdir()
    tests_dir.mkdir()
    target_file = tmp_path / "fake_specialist.py"
    target_file.write_text("")

    draft_path = _write_draft(pending_dir)
    test_code = (
        "import sys, os\n"
        "sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))\n\n"
        "def test_draft_catches_the_pattern():\n"
        "    _validate_example_thing(None)\n"
    )
    (pending_dir / "test_example_cluster.py").write_text(test_code)

    monkeypatch.setattr(promote_module, "_PENDING_DIR", pending_dir)
    monkeypatch.setattr(promote_module, "_PROMOTED_DIR", promoted_dir)
    monkeypatch.setattr(promote_module, "_SPEC_PATHS", {"build": "fake_specialist.py"})
    monkeypatch.setattr(promote_module, "_REPO_ROOT", tmp_path)
    monkeypatch.setattr(promote_module, "input", lambda _prompt: "y", raising=False)

    import manager.correction as correction_module
    monkeypatch.setattr(correction_module, "supersede_proposed_rule", lambda rid, name: None)

    promote_module.promote("example_cluster.py", auto_yes=False)

    installed = tests_dir / "test_promoted__validate_example_thing.py"
    assert installed.exists(), "the companion test must be installed into tests/"
    installed_content = installed.read_text()
    assert "os.path.join(os.path.dirname(__file__), '..', '..')" not in installed_content, (
        "the wrong (2-levels-up) relative path must never survive into the installed test"
    )
    assert "os.path.join(os.path.dirname(__file__), '..')" in installed_content, (
        "the installed test must use the correct (1-level-up) relative path for its real location in tests/"
    )
    print("PASS: the installed companion test's relative import path is corrected for its real location in tests/")


if __name__ == "__main__":
    print("(this file requires pytest's monkeypatch/tmp_path/capsys fixtures; run via pytest)")
