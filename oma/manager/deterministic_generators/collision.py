"""Phase 35 §15.11 -- the multi-direction collision pre-check.

A deterministic-path edit's `target` must still exist, verbatim, in
the file state AFTER every edit ordered before it in the same round
has been applied -- a dry-run match, never a live mutation. If it
doesn't, this specific task's deterministic-path edit is discarded and
that direction falls back to LLM authorship for this round only, per
§15.11's own real design (never treated as a kill-switch trigger --
a collision is an unlucky ordering, not evidence the generator itself
is wrong).
"""

from __future__ import annotations


def deterministic_edit_survives_prior_edits(
    deterministic_target: str, file_content_before_round: str, prior_edits_in_round: list[dict]
) -> bool:
    """Dry-run applies every prior edit (in order) to a scratch copy of
    the file, then checks whether `deterministic_target` still appears
    EXACTLY ONCE -- the same "target must match exactly once" contract
    GeneratedModuleEdit.search_replace itself requires. Only considers
    prior edits touching the SAME file the deterministic edit targets;
    edits to other files can't collide by construction.
    """
    scratch = file_content_before_round
    for edit in prior_edits_in_round:
        if edit.get("operation") != "search_replace":
            continue
        target = edit.get("target")
        if target is None or target not in scratch:
            continue  # a prior edit that doesn't apply to this file/state -- not this check's concern
        if scratch.count(target) != 1:
            continue  # ambiguous prior edit is itself a separate problem, not this check's concern
        scratch = scratch.replace(target, edit.get("content", ""), 1)

    return scratch.count(deterministic_target) == 1
