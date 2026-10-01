"""Phase 35 §17.4.3 / §18.6: tests for manager/gate_tamper_protection.py -- the local hash-pin
detection half. Uses real temp files (real SHA-256 over real bytes), never mocked hashing.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import manager.gate_tamper_protection as tamper_module
from manager.gate_tamper_protection import (
    check_for_tampering,
    compute_manifest,
    load_manifest,
    write_manifest,
)


def _use_temp_repo(tmp_path, monkeypatch):
    monkeypatch.setattr(tamper_module, "_REPO_ROOT", str(tmp_path))
    manifest_path = os.path.join(str(tmp_path), "state", "gate_source_manifest.json")
    return manifest_path


def _write(tmp_path, relpath, content):
    full = os.path.join(str(tmp_path), relpath)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "w") as f:
        f.write(content)


def test_compute_manifest_produces_real_distinct_hashes(tmp_path, monkeypatch):
    _use_temp_repo(tmp_path, monkeypatch)
    _write(tmp_path, "a.py", "content A")
    _write(tmp_path, "b.py", "content B")
    manifest = compute_manifest(frozenset({"a.py", "b.py"}))
    assert manifest["a.py"] != manifest["b.py"]
    assert len(manifest["a.py"]) == 64  # real sha256 hex digest length


def test_compute_manifest_missing_file_is_none():
    manifest = compute_manifest(frozenset({"does/not/exist.py"}))
    assert manifest["does/not/exist.py"] is None


def test_write_and_load_manifest_roundtrip(tmp_path, monkeypatch):
    manifest_path = _use_temp_repo(tmp_path, monkeypatch)
    _write(tmp_path, "a.py", "original content")
    manifest = compute_manifest(frozenset({"a.py"}))
    write_manifest(manifest, manifest_path)
    loaded = load_manifest(manifest_path)
    assert loaded == manifest


def test_no_tampering_when_files_match_the_pinned_manifest(tmp_path, monkeypatch):
    manifest_path = _use_temp_repo(tmp_path, monkeypatch)
    _write(tmp_path, "a.py", "safe content")
    write_manifest(compute_manifest(frozenset({"a.py"})), manifest_path)

    findings = check_for_tampering(frozenset({"a.py"}), manifest_path)
    assert findings == []


def test_hash_mismatch_detected_after_real_edit(tmp_path, monkeypatch):
    manifest_path = _use_temp_repo(tmp_path, monkeypatch)
    _write(tmp_path, "a.py", "original safe content")
    write_manifest(compute_manifest(frozenset({"a.py"})), manifest_path)

    # A real, tampering-style edit to the protected file after it was pinned.
    _write(tmp_path, "a.py", "raise ValueError('x')  # neutered by an edit")

    findings = check_for_tampering(frozenset({"a.py"}), manifest_path)
    assert len(findings) == 1
    assert findings[0].relpath == "a.py"
    assert findings[0].kind == "hash_mismatch"


def test_missing_file_that_was_previously_pinned_is_detected(tmp_path, monkeypatch):
    manifest_path = _use_temp_repo(tmp_path, monkeypatch)
    _write(tmp_path, "a.py", "content")
    write_manifest(compute_manifest(frozenset({"a.py"})), manifest_path)

    os.remove(os.path.join(str(tmp_path), "a.py"))

    findings = check_for_tampering(frozenset({"a.py"}), manifest_path)
    assert len(findings) == 1
    assert findings[0].kind == "missing_file"


def test_protected_file_never_pinned_is_detected(tmp_path, monkeypatch):
    manifest_path = _use_temp_repo(tmp_path, monkeypatch)
    _write(tmp_path, "a.py", "content")
    write_manifest({}, manifest_path)  # empty manifest -- a.py was never pinned

    findings = check_for_tampering(frozenset({"a.py"}), manifest_path)
    assert len(findings) == 1
    assert findings[0].kind == "unpinned_protected_file"


def test_missing_manifest_file_reports_every_protected_file_as_unpinned(tmp_path, monkeypatch):
    manifest_path = _use_temp_repo(tmp_path, monkeypatch)
    _write(tmp_path, "a.py", "content")

    findings = check_for_tampering(frozenset({"a.py"}), manifest_path)
    assert len(findings) == 1
    assert findings[0].kind == "unpinned_protected_file"


def test_corrupt_manifest_never_raises(tmp_path, monkeypatch):
    manifest_path = _use_temp_repo(tmp_path, monkeypatch)
    _write(tmp_path, "a.py", "content")
    os.makedirs(os.path.dirname(manifest_path), exist_ok=True)
    with open(manifest_path, "w") as f:
        f.write("{ not valid json")

    findings = check_for_tampering(frozenset({"a.py"}), manifest_path)  # must not raise
    assert len(findings) == 1
    assert findings[0].kind == "manifest_unreadable"


def test_multiple_files_only_the_changed_one_is_flagged(tmp_path, monkeypatch):
    manifest_path = _use_temp_repo(tmp_path, monkeypatch)
    _write(tmp_path, "a.py", "content A")
    _write(tmp_path, "b.py", "content B")
    write_manifest(compute_manifest(frozenset({"a.py", "b.py"})), manifest_path)

    _write(tmp_path, "b.py", "TAMPERED content B")

    findings = check_for_tampering(frozenset({"a.py", "b.py"}), manifest_path)
    assert len(findings) == 1
    assert findings[0].relpath == "b.py"
