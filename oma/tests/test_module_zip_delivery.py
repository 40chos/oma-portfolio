"""Phase 28B (2026-07-28): `school_student`'s own explicit "Delivery:
Full module as a zip file" requirement. Real, live tests against the
actual Gitea instance (same discipline as
test_phase18_gitea_regression_oscillation.py's own Gitea round-trip
test) for the packaging step itself, plus a lightweight unit test for
the completion-gating logic in manager.dashboard.task_genuinely_passed
(mocked at the Postgres/Redis boundary -- that logic is pure branching
over already-real facts, not itself an infra call worth a live test).
"""

import io
import os
import sys
import uuid
import zipfile
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tools_odoo.module_dev import vcs


def test_zip_is_none_when_nothing_was_ever_committed():
    task_id = str(uuid.uuid4())
    result = vcs.build_task_module_zip_bytes(task_id)
    assert result is None, "a task with no committed round must return None, never an empty archive"
    print("PASS: no committed content -> None, not a silent empty zip")


def test_zip_contains_the_real_committed_module_files_at_the_right_paths():
    task_id = str(uuid.uuid4())
    module_name = f"oma_test_zip_{uuid.uuid4().hex[:8]}"
    files = {
        "__manifest__.py": "{'name': 'zip test'}\n",
        "models/models.py": "# real committed content\n",
        "security/ir.model.access.csv": "id,name\n",
    }
    vcs.commit_validated_round(
        task_id=task_id, module_name=module_name, files=files, round_number=1, summary="round 1",
    )

    zip_bytes = vcs.build_task_module_zip_bytes(task_id)
    assert zip_bytes is not None
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as archive:
        names = set(archive.namelist())
        for relpath in files:
            full_path = f"{module_name}/{relpath}"
            assert full_path in names, f"expected {full_path!r} in the zip, got {sorted(names)!r}"
            assert archive.read(full_path).decode("utf-8") == files[relpath]
    print(f"PASS: a real Gitea-committed module round-trips into a real zip with the exact committed "
          f"content, at the correct installable-module-directory paths ({sorted(names)!r})")


def test_zip_reflects_the_latest_round_not_a_stale_earlier_one():
    task_id = str(uuid.uuid4())
    module_name = f"oma_test_zip_{uuid.uuid4().hex[:8]}"
    vcs.commit_validated_round(
        task_id=task_id, module_name=module_name,
        files={"models/models.py": "# v1\n"}, round_number=1, summary="round 1",
    )
    vcs.commit_validated_round(
        task_id=task_id, module_name=module_name,
        files={"models/models.py": "# v2\n"}, round_number=2, summary="round 2",
    )
    zip_bytes = vcs.build_task_module_zip_bytes(task_id)
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as archive:
        content = archive.read(f"{module_name}/models/models.py").decode("utf-8")
    assert content == "# v2\n", f"zip must reflect the latest committed round, got {content!r}"
    print("PASS: zip always reflects the latest committed round, never a stale earlier one")


def test_a_brand_new_task_branch_never_inherits_stray_content_forked_from_default_branch():
    """Real, live-confirmed bug found during this same phase's own
    verification (2026-07-28): the shared Gitea repo's default branch
    ('main') carries stray leftover content from a historical commit
    ('oma_test_vcs_probe/...'), and every brand-new task branch forks
    from it -- so every task's own module, and therefore every zip this
    same phase's own delivery feature produces, was silently getting
    one alien, unrelated file baked in. commit_validated_round() was
    hardened to always strip anything inherited from the fork point
    that isn't part of THIS round's own files, as part of round 1's own
    commit. This is the real regression test for that fix: proves a
    freshly created branch's own tree, and therefore
    read_last_validated_commit()'s own return value and the zip built
    from it, contains ONLY this task's own real files -- nothing
    forked in from whatever 'main' happens to currently hold.
    """
    task_id = str(uuid.uuid4())
    module_name = f"oma_test_zip_{uuid.uuid4().hex[:8]}"
    vcs.commit_validated_round(
        task_id=task_id, module_name=module_name,
        files={"models/models.py": "# clean round 1\n"}, round_number=1, summary="round 1",
    )
    after = vcs.read_last_validated_commit(task_id)
    assert after is not None
    stray = [path for path in after if not path.startswith(f"{module_name}/")]
    assert not stray, (
        f"a brand-new task branch must never carry inherited content from the fork point, "
        f"found stray path(s): {stray!r}"
    )
    assert set(after.keys()) == {f"{module_name}/models/models.py"}
    print("PASS: a brand-new task branch's own tree contains ONLY this task's own real files, "
          "no inherited stray content from the default branch's own fork point")


def test_task_genuinely_passed_gates_correctly():
    from manager.dashboard import task_genuinely_passed

    # No outcome row at all -- not complete.
    with patch("manager.dashboard._outcome_row_for_task", return_value=None):
        assert task_genuinely_passed("fake-task-1") is False

    # Outcome row exists but detail.passed is False (a real failure/escalation).
    with patch("manager.dashboard._outcome_row_for_task", return_value={"detail": {"passed": False}}):
        assert task_genuinely_passed("fake-task-2") is False

    # Outcome row genuinely passed, but Redis still holds live state
    # (e.g. a later constraint of a decomposed task is still running).
    fake_redis = MagicMock()
    fake_redis.get.return_value = "running"
    with patch("manager.dashboard._outcome_row_for_task", return_value={"detail": {"passed": True}}), \
         patch("manager.dashboard.get_redis_client", return_value=fake_redis):
        assert task_genuinely_passed("fake-task-3") is False

    # Outcome row genuinely passed AND no live Redis state left -- the
    # one real, durable "safe to hand back a zip" case.
    fake_redis_done = MagicMock()
    fake_redis_done.get.return_value = None
    with patch("manager.dashboard._outcome_row_for_task", return_value={"detail": {"passed": True}}), \
         patch("manager.dashboard.get_redis_client", return_value=fake_redis_done):
        assert task_genuinely_passed("fake-task-4") is True

    print("PASS: task_genuinely_passed() correctly gates on both a real passed outcome AND no live Redis state")


if __name__ == "__main__":
    test_zip_is_none_when_nothing_was_ever_committed()
    test_zip_contains_the_real_committed_module_files_at_the_right_paths()
    test_zip_reflects_the_latest_round_not_a_stale_earlier_one()
    test_a_brand_new_task_branch_never_inherits_stray_content_forked_from_default_branch()
    test_task_genuinely_passed_gates_correctly()
    print("\nALL MODULE ZIP DELIVERY TESTS PASSED")
