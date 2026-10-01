"""Phase 32 implementation (2026-08-11): unit tests for
scripts/validator_churn_tracker.py -- the mechanical git-diff branch-
structure churn signal from Phase 32 section 3.3. Builds a real, disposable
git repo under tmp_path and makes real commits, since the actual
agents/services/oma/ repo has almost no commit history yet (see the module's own
docstring) -- this exercises the real git plumbing rather than mocking it.
"""

import os
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from scripts.validator_churn_tracker import compute_function_churn, flag_high_churn_validators


def _init_repo(repo_dir: str) -> None:
    subprocess.run(["git", "init", "-q"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo_dir, check=True)


def _write_and_commit(repo_dir: str, file_path: str, content: str, message: str) -> None:
    full_path = os.path.join(repo_dir, file_path)
    with open(full_path, "w", encoding="utf-8") as f:
        f.write(content)
    subprocess.run(["git", "add", file_path], cwd=repo_dir, check=True)
    subprocess.run(["git", "commit", "-q", "-m", message], cwd=repo_dir, check=True)


_V1 = '''def _filter_findings(findings):
    result = []
    for f in findings:
        result.append(f)
    return result
'''

_V2_ADDS_A_BRANCH = '''def _filter_findings(findings):
    result = []
    for f in findings:
        if f.severity == "blocking":
            result.append(f)
    return result
'''

_V3_ADDS_ANOTHER_BRANCH = '''def _filter_findings(findings):
    result = []
    for f in findings:
        if f.severity == "blocking":
            if "unnecessary" in f.explanation:
                continue
            result.append(f)
    return result
'''

_V4_ADDS_A_THIRD_BRANCH = '''def _filter_findings(findings):
    result = []
    for f in findings:
        if f.severity == "blocking":
            if "unnecessary" in f.explanation or "should be deleted" in f.explanation:
                continue
            result.append(f)
    return result
'''

_V1_WITH_A_COMMENT_NO_BRANCH_CHANGE = '''def _filter_findings(findings):
    # a plain comment, no logic change
    result = []
    for f in findings:
        result.append(f)
    return result
'''


def test_repeated_branch_structure_edits_are_counted_and_flagged(tmp_path):
    repo_dir = str(tmp_path)
    _init_repo(repo_dir)
    _write_and_commit(repo_dir, "specialist.py", _V1, "initial filter")
    _write_and_commit(repo_dir, "specialist.py", _V2_ADDS_A_BRANCH, "narrow to blocking only")
    _write_and_commit(repo_dir, "specialist.py", _V3_ADDS_ANOTHER_BRANCH, "also exclude 'unnecessary' wording")
    _write_and_commit(repo_dir, "specialist.py", _V4_ADDS_A_THIRD_BRANCH, "also exclude 'should be deleted' wording")

    churns = compute_function_churn(repo_dir, "specialist.py")
    assert len(churns) == 1
    churn = churns[0]
    assert churn.function_name == "_filter_findings"
    assert churn.branch_structure_edit_commits == 3, (
        "3 of the 4 commits added/changed a branching line (the initial commit's own diff has no "
        "branch keyword in it since _V1 has no branches yet, so only the 3 later commits count)"
    )

    flagged = flag_high_churn_validators(churns, threshold=3)
    assert len(flagged) == 1
    print("PASS: 3 real branch-structure-changing commits to the same function are counted and flagged")


def test_a_pure_comment_change_does_not_count_as_a_branch_edit(tmp_path):
    repo_dir = str(tmp_path)
    _init_repo(repo_dir)
    _write_and_commit(repo_dir, "specialist.py", _V1, "initial filter, no branches yet")
    _write_and_commit(repo_dir, "specialist.py", _V1_WITH_A_COMMENT_NO_BRANCH_CHANGE, "add explanatory comment only")

    churns = compute_function_churn(repo_dir, "specialist.py")
    churn = next(c for c in churns if c.function_name == "_filter_findings")
    assert churn.branch_structure_edit_commits == 0, (
        "a commit that only adds a comment line must not be counted as a branch-structure edit"
    )
    print("PASS: a comment-only change is not counted as a branch-structure edit")


def test_below_threshold_functions_are_not_flagged(tmp_path):
    repo_dir = str(tmp_path)
    _init_repo(repo_dir)
    _write_and_commit(repo_dir, "specialist.py", _V1, "initial filter")
    _write_and_commit(repo_dir, "specialist.py", _V2_ADDS_A_BRANCH, "one branch edit only")

    churns = compute_function_churn(repo_dir, "specialist.py")
    flagged = flag_high_churn_validators(churns, threshold=3)
    assert flagged == []
    print("PASS: a function with only 1 branch-structure edit (below threshold 3) is not flagged")


def test_a_different_untouched_function_is_never_flagged(tmp_path):
    repo_dir = str(tmp_path)
    _init_repo(repo_dir)
    combined_v1 = _V1 + '\n\ndef _other_stable_helper(x):\n    return x + 1\n'
    combined_v2 = _V2_ADDS_A_BRANCH + '\n\ndef _other_stable_helper(x):\n    return x + 1\n'
    combined_v3 = _V3_ADDS_ANOTHER_BRANCH + '\n\ndef _other_stable_helper(x):\n    return x + 1\n'
    _write_and_commit(repo_dir, "specialist.py", combined_v1, "initial")
    _write_and_commit(repo_dir, "specialist.py", combined_v2, "branch edit 1")
    _write_and_commit(repo_dir, "specialist.py", combined_v3, "branch edit 2")

    churns = compute_function_churn(repo_dir, "specialist.py")
    other = next((c for c in churns if c.function_name == "_other_stable_helper"), None)
    assert other is None or other.branch_structure_edit_commits == 0
    print("PASS: a stable, never-branch-edited function in the same file is never flagged")


if __name__ == "__main__":
    print("(this file requires pytest's tmp_path fixture; run via pytest)")
