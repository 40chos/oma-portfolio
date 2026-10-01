"""Phase 32 implementation (2026-08-11): the mechanical "verification debt"
signal from docs/planning/PHASE32_RELIABILITY_ROOT_CAUSE_AND_ROLLOUT_STRATEGY_2026-08-11.md
section 3.3.

Deliberately NOT semantic ("is this the same complaint as last time?") --
Phase 32's own self-critique found that a semantic version of this check
would need the exact same judgment call that caused Bugs 1-91 in the first
place. Instead this counts, per function, how many git commits touched
that function's own body with a change to BRANCHING STRUCTURE specifically
(added/removed/modified `if`/`elif`/`and`/`or` lines) -- not whitespace,
not comments, not a rename. A function edited this way repeatedly is
flagged for a cheap human look, never rewritten automatically.

Honest limitation, stated explicitly rather than hidden (per Phase 32's
own acknowledgment): this is not perfectly decidable. A refactor that
splits a validator into two helper functions will under-count (git blame
follows the new function names, not the old one); pure formatting changes
that happen to touch a branching line could over-count. Acceptable because
the ONLY action this ever triggers is "a human looks at this function,"
never an automatic rewrite -- false positives cost a few minutes of
review, not a bad autonomous decision.

As of 2026-08-11, agents/services/oma/ itself has almost no real commit history
(2 skeleton commits; everything else uncommitted) -- this tool has real,
tested logic but nothing meaningful to report yet. It becomes useful the
moment normal commit hygiene starts on this codebase, which this
implementation pass does not retroactively fabricate.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass

_BRANCH_KEYWORDS = re.compile(r"^\s*[+-]\s*(if |elif |else:|and |or |while |except )")
_DEF_RE = re.compile(r"^\s*(async\s+)?def\s+(\w+)\s*\(")
_HUNK_HEADER_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


@dataclass
class FunctionChurn:
    function_name: str
    file_path: str
    branch_structure_edit_commits: int
    total_commits_touching_function: int


def _iter_commit_hashes(repo_dir: str, file_path: str) -> list[str]:
    result = subprocess.run(
        ["git", "log", "--format=%H", "--", file_path],
        cwd=repo_dir, capture_output=True, text=True, check=True,
    )
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def _parse_diff_hunks(diff_text: str) -> list[dict]:
    """Parses a unified diff into hunks of {new_start, new_count, lines}.
    Deliberately does NOT trust git's own hunk-header function-context text
    (the text after the second `@@`) -- that context is produced by a
    best-effort heuristic/per-language driver that isn't reliably present
    for every git version/config, so relying on it under-counts. Instead
    only the line-number ranges are trusted; which FUNCTION those line
    numbers fall inside is determined separately, from the real file
    content (see _function_ranges below)."""
    hunks: list[dict] = []
    current: dict | None = None
    for line in diff_text.splitlines():
        match = _HUNK_HEADER_RE.match(line)
        if match:
            if current is not None:
                hunks.append(current)
            new_start = int(match.group(3))
            new_count = int(match.group(4)) if match.group(4) is not None else 1
            current = {"new_start": new_start, "new_count": new_count, "lines": []}
            continue
        if current is not None:
            if line.startswith("+") or line.startswith("-"):
                if not line.startswith("+++") and not line.startswith("---"):
                    current["lines"].append(line)
    if current is not None:
        hunks.append(current)
    return hunks


def _function_ranges(file_lines: list[str]) -> list[tuple[str, int, int]]:
    """Returns [(function_name, first_line, last_line)] (1-indexed,
    inclusive) for every top-level `def` in `file_lines`, using the REAL
    file content at that commit -- not a guess from diff context. A
    function's range runs until the next `def` line; this deliberately
    does not attempt to track indentation-nested inner functions
    separately (matching this tool's own honest "not perfectly decidable"
    scope from the module docstring: acceptable because the only action
    this ever triggers is a human review, never an automatic rewrite)."""
    ranges: list[tuple[str, int, int]] = []
    current_name: str | None = None
    current_start = 0
    for idx, line in enumerate(file_lines, start=1):
        match = _DEF_RE.match(line)
        if match:
            if current_name is not None:
                ranges.append((current_name, current_start, idx - 1))
            current_name = match.group(2)
            current_start = idx
    if current_name is not None:
        ranges.append((current_name, current_start, len(file_lines)))
    return ranges


def compute_function_churn(repo_dir: str, file_path: str) -> list[FunctionChurn]:
    commit_hashes = _iter_commit_hashes(repo_dir, file_path)
    touched_counts: dict[str, int] = {}
    branch_edit_counts: dict[str, int] = {}

    for commit_hash in commit_hashes:
        diff_result = subprocess.run(
            ["git", "show", commit_hash, "--", file_path],
            cwd=repo_dir, capture_output=True, text=True, check=True,
        )
        hunks = _parse_diff_hunks(diff_result.stdout)
        if not hunks:
            continue

        content_result = subprocess.run(
            ["git", "show", f"{commit_hash}:{file_path}"],
            cwd=repo_dir, capture_output=True, text=True, check=True,
        )
        function_ranges = _function_ranges(content_result.stdout.splitlines())

        functions_touched_this_commit: set[str] = set()
        functions_branch_edited_this_commit: set[str] = set()
        for hunk in hunks:
            hunk_start = hunk["new_start"]
            hunk_end = hunk["new_start"] + max(hunk["new_count"], 1) - 1
            has_branch_line = any(_BRANCH_KEYWORDS.match(line) for line in hunk["lines"])
            for function_name, start, end in function_ranges:
                if hunk_start <= end and hunk_end >= start:
                    functions_touched_this_commit.add(function_name)
                    if has_branch_line:
                        functions_branch_edited_this_commit.add(function_name)

        for function_name in functions_touched_this_commit:
            touched_counts[function_name] = touched_counts.get(function_name, 0) + 1
        for function_name in functions_branch_edited_this_commit:
            branch_edit_counts[function_name] = branch_edit_counts.get(function_name, 0) + 1

    return [
        FunctionChurn(
            function_name=name, file_path=file_path,
            branch_structure_edit_commits=branch_edit_counts.get(name, 0),
            total_commits_touching_function=touched_counts[name],
        )
        for name in touched_counts
    ]


def flag_high_churn_validators(
    churns: list[FunctionChurn], *, threshold: int = 3,
) -> list[FunctionChurn]:
    """Returns functions whose branching structure was edited `threshold`
    or more times -- the review trigger, never an automatic rewrite."""
    return [c for c in churns if c.branch_structure_edit_commits >= threshold]


if __name__ == "__main__":
    import sys

    if len(sys.argv) != 3:
        print("usage: validator_churn_tracker.py <repo_dir> <file_path_relative_to_repo>")
        sys.exit(1)
    churns = compute_function_churn(sys.argv[1], sys.argv[2])
    flagged = flag_high_churn_validators(churns)
    if not flagged:
        print(f"No function in {sys.argv[2]} has 3+ branch-structure-changing commits yet.")
    for churn in sorted(flagged, key=lambda c: -c.branch_structure_edit_commits):
        print(
            f"REVIEW SUGGESTED: {churn.function_name} in {churn.file_path} -- "
            f"{churn.branch_structure_edit_commits} branch-structure edits across "
            f"{churn.total_commits_touching_function} touching commits"
        )
