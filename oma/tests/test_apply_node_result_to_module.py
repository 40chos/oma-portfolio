"""Phase 31 §6(b)/(c)/(d) (Phase A.5 items 12-14): tests for
specialists/build/specialist.py's _apply_line_hunks() (pure, unit-testable) and
apply_node_result_to_module() (the real serialized-apply step -- vcs.*/check_fence/
write_module_file all mocked here; a separate live-Gitea test proves the vcs.py side
independently in tests/test_vcs_per_node_branch.py).
"""

import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from specialists.build.specialist import (
    NodeApplyOutcome,
    StaleForwardStepError,
    _apply_line_hunks,
    apply_node_result_to_module,
)


# --- _apply_line_hunks ----------------------------------------------------------------------

def test_no_change_returns_current_unchanged():
    base = "a\nb\nc\n"
    merged, ok = _apply_line_hunks(base, base, "a\nb\nc\n")
    assert ok is True
    assert merged == "a\nb\nc\n"
    print("PASS: identical base/new produces no hunks, current text returned unchanged")


def test_clean_insert_applies_when_current_matches_base():
    base = "line1\nline2\nline3\n"
    new = "line1\nINSERTED\nline2\nline3\n"
    merged, ok = _apply_line_hunks(base, new, base)
    assert ok is True
    assert merged == new
    print("PASS: a clean insert applies verbatim when current == base (no concurrent change)")


def test_hunk_applies_cleanly_when_current_diverges_in_a_disjoint_untouched_region():
    base = "model.py header\nclass Foo:\n    field_a = 1\n\nclass Bar:\n    field_x = 1\n"
    new = "model.py header\nclass Foo:\n    field_a = 1\n    field_b = 2\n\nclass Bar:\n    field_x = 1\n"
    current = "model.py header\nclass Foo:\n    field_a = 1\n\nclass Bar:\n    field_x = 1\n    field_y = 2\n"

    merged, ok = _apply_line_hunks(base, new, current)
    assert ok is True, "a hunk targeting Foo must apply cleanly even though Bar diverged elsewhere"
    assert "field_b = 2" in merged, merged
    assert "field_y = 2" in merged, merged
    print("PASS: a node's own hunk applies cleanly against current content that diverged in a genuinely disjoint region, preserving BOTH changes")


def test_hunk_conflicts_when_current_diverges_at_the_exact_hunk_context():
    base = "class Foo:\n    field_a = 1\n"
    new = "class Foo:\n    field_a = 1\n    field_b = 2\n"
    current = "class Foo:\n    field_a = 999\n"  # someone else changed the SAME line this hunk anchors to

    merged, ok = _apply_line_hunks(base, new, current)
    assert ok is False, "a hunk whose own context line was itself changed by someone else must conflict, never silently overwrite"
    assert merged == current
    print("PASS: a hunk whose own context has genuinely diverged reports a real conflict, current content untouched")


def test_multiple_disjoint_hunks_all_apply_independently():
    base = "a\nb\nc\nd\ne\n"
    new = "a\nb_NEW\nc\nd\ne_NEW\n"
    merged, ok = _apply_line_hunks(base, new, base)
    assert ok is True
    assert merged == new
    print("PASS: multiple disjoint hunks in the same file all apply independently")


# --- apply_node_result_to_module -------------------------------------------------------------

def _mock_vcs():
    """Returns a MagicMock standing in for the `vcs` module name imported into
    specialists.build.specialist -- patched at that import site, not tools_odoo.module_dev.vcs
    itself, so no real Gitea call is ever made.
    """
    from unittest.mock import MagicMock
    return MagicMock()


def test_apply_writes_new_file_directly_with_no_conflict():
    vcs_mock = _mock_vcs()
    vcs_mock.read_last_validated_commit.return_value = {}  # nothing committed yet on the task branch

    written = {}

    def fake_write(module_name, relpath, content):
        written[relpath] = content

    with patch("specialists.build.specialist.vcs", vcs_mock), \
         patch("specialists.build.specialist.check_fence", return_value=True), \
         patch("specialists.build.specialist.write_module_file", side_effect=fake_write):
        outcome = apply_node_result_to_module(
            task_id="t1", module_name="oma_test_mod", node_label="node_a",
            frozen_old_files_by_relpath={},
            node_new_files_by_relpath={"models/models.py": "# new content\n"},
            lock_key="oma_test_mod", fence_token=1, round_number=1, summary="node_a round 1",
        )

    assert outcome.success is True
    assert outcome.applied_relpaths == ["models/models.py"]
    assert written["models/models.py"] == "# new content\n"
    vcs_mock.delete_branch.assert_called_once_with("t1", "node_a")
    vcs_mock.commit_validated_round.assert_called_once()
    print("PASS: a brand-new file with nothing else committed yet applies directly, branch cleaned up")


def test_apply_still_writes_unchanged_files_to_disk_but_never_commits_them():
    """Real, confirmed bug found live (2026-08-09, task 07141af5-9a4e-41b6-93ea-8b7af04fea9c's
    flagship run, service_ticket_model node): this used to skip BOTH the disk write and the git
    commit whenever frozen_old_files_by_relpath == node_new_files_by_relpath for a relpath --
    but a REJECTED prior round can already have written a BROKEN version of that same file
    straight to the live disk before its content was ultimately rejected, leaving disk stale
    forever afterward (git's own "last validated commit" was never wrong, but nothing ever
    re-synced disk to match it). Confirmed live via direct SSH comparison: git had a shared
    data file's full, correct content; live disk still had an earlier, broken partial version.
    Disk must always be written to reflect this round's own real, intended final state, even
    when that state happens to equal the frozen baseline -- but the git commit itself still
    only fires for a GENUINE change, avoiding wasteful, empty commits every round.
    """
    vcs_mock = _mock_vcs()
    vcs_mock.read_last_validated_commit.return_value = {}
    written = {}

    with patch("specialists.build.specialist.vcs", vcs_mock), \
         patch("specialists.build.specialist.check_fence", return_value=True), \
         patch("specialists.build.specialist.write_module_file", side_effect=lambda m, r, c: written.setdefault(r, c)):
        outcome = apply_node_result_to_module(
            task_id="t1", module_name="oma_test_mod", node_label="node_a",
            frozen_old_files_by_relpath={"unchanged.py": "same\n"},
            node_new_files_by_relpath={"unchanged.py": "same\n"},
            lock_key="k", fence_token=1, round_number=1, summary="s",
        )

    assert outcome.applied_relpaths == ["unchanged.py"], (
        "a file whose new content equals the frozen baseline must still be reported as applied "
        "-- disk correctness for it is still guaranteed, closing the real live gap found on "
        "task 07141af5's service_ticket_model node"
    )
    assert written == {"unchanged.py": "same\n"}, (
        f"disk must still receive the write even when frozen == new, in case an earlier "
        f"rejected round left disk stale -- got {written!r}"
    )
    vcs_mock.commit_validated_round.assert_not_called()
    print("PASS: a file the node never actually changed (frozen == new) is still written to "
          "disk (guarding against a stale write from an earlier rejected round), but never "
          "committed to git (avoiding a wasted, empty commit)")


def test_apply_merges_a_clean_hunk_against_current_when_current_diverged_elsewhere():
    vcs_mock = _mock_vcs()
    vcs_mock.read_last_validated_commit.return_value = {
        "oma_test_mod/models/models.py": (
            "class Foo:\n    field_a = 1\n\nclass Bar:\n    field_x = 1\n    field_y = 2\n"
        ),
    }
    written = {}

    with patch("specialists.build.specialist.vcs", vcs_mock), \
         patch("specialists.build.specialist.check_fence", return_value=True), \
         patch("specialists.build.specialist.write_module_file", side_effect=lambda m, r, c: written.setdefault(r, c)):
        outcome = apply_node_result_to_module(
            task_id="t1", module_name="oma_test_mod", node_label="node_a",
            frozen_old_files_by_relpath={
                "models/models.py": "class Foo:\n    field_a = 1\n\nclass Bar:\n    field_x = 1\n",
            },
            node_new_files_by_relpath={
                "models/models.py": "class Foo:\n    field_a = 1\n    field_b = 2\n\nclass Bar:\n    field_x = 1\n",
            },
            lock_key="k", fence_token=1, round_number=2, summary="node_a round 2",
        )

    assert outcome.success is True
    assert "field_b = 2" in written["models/models.py"]
    assert "field_y = 2" in written["models/models.py"], "the concurrently-added field_y from CURRENT must survive the merge"
    print("PASS: apply_node_result_to_module() merges a clean hunk against real diverged CURRENT content, preserving both changes")


def test_apply_reports_conflict_and_does_not_write_the_conflicting_file():
    vcs_mock = _mock_vcs()
    vcs_mock.read_last_validated_commit.return_value = {
        "oma_test_mod/models/models.py": "class Foo:\n    field_a = 999\n",
    }
    written = {}

    with patch("specialists.build.specialist.vcs", vcs_mock), \
         patch("specialists.build.specialist.check_fence", return_value=True), \
         patch("specialists.build.specialist.write_module_file", side_effect=lambda m, r, c: written.setdefault(r, c)):
        outcome = apply_node_result_to_module(
            task_id="t1", module_name="oma_test_mod", node_label="node_a",
            frozen_old_files_by_relpath={"models/models.py": "class Foo:\n    field_a = 1\n"},
            node_new_files_by_relpath={"models/models.py": "class Foo:\n    field_a = 1\n    field_b = 2\n"},
            lock_key="k", fence_token=1, round_number=2, summary="s",
        )

    assert outcome.success is False
    assert outcome.conflicted_relpaths == ["models/models.py"]
    assert "models/models.py" not in written, "a genuinely conflicting file must never be written"
    vcs_mock.delete_branch.assert_called_once_with("t1", "node_a")
    print("PASS: a genuine conflict is reported and the conflicting file is never written; branch still cleaned up")


def test_apply_conflict_on_one_file_does_not_block_a_non_conflicting_sibling_file():
    vcs_mock = _mock_vcs()
    vcs_mock.read_last_validated_commit.return_value = {
        "oma_test_mod/models/models.py": "class Foo:\n    field_a = 999\n",
        "oma_test_mod/security/ir.model.access.csv": "id,name\n",
    }
    written = {}

    with patch("specialists.build.specialist.vcs", vcs_mock), \
         patch("specialists.build.specialist.check_fence", return_value=True), \
         patch("specialists.build.specialist.write_module_file", side_effect=lambda m, r, c: written.setdefault(r, c)):
        outcome = apply_node_result_to_module(
            task_id="t1", module_name="oma_test_mod", node_label="node_a",
            frozen_old_files_by_relpath={
                "models/models.py": "class Foo:\n    field_a = 1\n",
                "security/ir.model.access.csv": "id,name\n",
            },
            node_new_files_by_relpath={
                "models/models.py": "class Foo:\n    field_a = 1\n    field_b = 2\n",
                "security/ir.model.access.csv": "id,name\naccess_foo,Foo access\n",
            },
            lock_key="k", fence_token=1, round_number=2, summary="s",
        )

    assert outcome.conflicted_relpaths == ["models/models.py"]
    assert outcome.applied_relpaths == ["security/ir.model.access.csv"]
    assert "security/ir.model.access.csv" in written
    assert "models/models.py" not in written
    print("PASS: a conflict on one file never blocks a non-conflicting sibling file in the SAME node's own diff (§6(c) scoped fallback)")


def test_apply_rechecks_fence_before_each_write_and_stops_on_a_stale_token():
    vcs_mock = _mock_vcs()
    vcs_mock.read_last_validated_commit.return_value = {}
    written = {}
    fence_calls = {"n": 0}

    def fake_check_fence(lock_key, fence_token):
        fence_calls["n"] += 1
        return fence_calls["n"] == 1  # first write's own check passes, second's fails

    try:
        with patch("specialists.build.specialist.vcs", vcs_mock), \
             patch("specialists.build.specialist.check_fence", side_effect=fake_check_fence), \
             patch("specialists.build.specialist.write_module_file", side_effect=lambda m, r, c: written.setdefault(r, c)):
            apply_node_result_to_module(
                task_id="t1", module_name="oma_test_mod", node_label="node_a",
                frozen_old_files_by_relpath={},
                node_new_files_by_relpath={"a_file.py": "a\n", "b_file.py": "b\n"},
                lock_key="k", fence_token=1, round_number=1, summary="s",
            )
        assert False, "a stale fence token on the SECOND write must raise, never silently continue"
    except StaleForwardStepError:
        pass

    assert fence_calls["n"] == 2, f"check_fence must be called once per write, got {fence_calls['n']} calls"
    assert len(written) == 1, "only the write whose OWN fence check passed must have actually happened"
    vcs_mock.delete_branch.assert_called_once_with("t1", "node_a")
    print("PASS: check_fence() is re-checked before EVERY individual write; a stale token mid-batch stops further writes, branch still cleaned up")


def test_apply_deletes_the_per_node_branch_even_when_an_exception_is_raised():
    vcs_mock = _mock_vcs()
    vcs_mock.read_last_validated_commit.return_value = {}

    with patch("specialists.build.specialist.vcs", vcs_mock), \
         patch("specialists.build.specialist.check_fence", return_value=False), \
         patch("specialists.build.specialist.write_module_file"):
        try:
            apply_node_result_to_module(
                task_id="t1", module_name="oma_test_mod", node_label="node_a",
                frozen_old_files_by_relpath={},
                node_new_files_by_relpath={"a_file.py": "a\n"},
                lock_key="k", fence_token=1, round_number=1, summary="s",
            )
        except StaleForwardStepError:
            pass

    vcs_mock.delete_branch.assert_called_once_with("t1", "node_a")
    print("PASS: the per-node branch is deleted in a `finally` even when the fence check raises")


if __name__ == "__main__":
    test_no_change_returns_current_unchanged()
    test_clean_insert_applies_when_current_matches_base()
    test_hunk_applies_cleanly_when_current_diverges_in_a_disjoint_untouched_region()
    test_hunk_conflicts_when_current_diverges_at_the_exact_hunk_context()
    test_multiple_disjoint_hunks_all_apply_independently()
    test_apply_writes_new_file_directly_with_no_conflict()
    test_apply_still_writes_unchanged_files_to_disk_but_never_commits_them()
    test_apply_merges_a_clean_hunk_against_current_when_current_diverged_elsewhere()
    test_apply_reports_conflict_and_does_not_write_the_conflicting_file()
    test_apply_conflict_on_one_file_does_not_block_a_non_conflicting_sibling_file()
    test_apply_rechecks_fence_before_each_write_and_stops_on_a_stale_token()
    test_apply_deletes_the_per_node_branch_even_when_an_exception_is_raised()
    print("\nALL apply_node_result_to_module / _apply_line_hunks TESTS PASSED")
