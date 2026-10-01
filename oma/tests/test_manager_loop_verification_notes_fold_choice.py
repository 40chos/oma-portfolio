"""Phase 30 §26 follow-up (2026-08-04): regression test for a real, confirmed gap -- the
sandbox_failed/real_install_genuinely_failed short-circuit branch in manager/loop.py's own
VerificationResult construction was still using fold_specialist_result() (the plain, naive
middle-drop truncation) for VerificationResult.notes, while every sibling call site
(manager/tools.py's own two, for the readonly_investigation early-return and the real
await_verification() path) was already patched to fold_verification_notes() (the structure-aware
fold that preserves diagnostic lines -- P12 Tier B/C item 28). VerificationResult.notes is exactly
the field build_verification_notes_with_log_tail() attaches a real install log_tail to just before
this call -- folding it with the naive fold risked silently dropping that same diagnostic content
back out on a long combined string.

Source-level check (same discipline as test_no_durable_rule_memory_module_exists_for_ambiguity_
answers in tests/test_ambiguity_digest.py) rather than a full run_turn() invocation -- run_turn()
is a large, deeply-dependency-heavy function; this directly and durably prevents a regression back
to the plain fold without needing to mock the entire turn pipeline.
"""
import inspect

import manager.loop as loop_module


def test_sandbox_and_install_failure_short_circuit_uses_the_structure_aware_fold():
    source = inspect.getsource(loop_module._execute_contract)
    # Find the specific VerificationResult(...) construction inside the sandbox_failed / lock_
    # rejected / real_install_genuinely_failed short-circuit branch -- identified by the
    # build_verification_notes_with_log_tail(...) call immediately nested inside its own notes=.
    idx = source.find("build_verification_notes_with_log_tail(build_output.summary, build_output.detail)")
    assert idx != -1, "the sandbox/install-failure short-circuit's own notes= construction must still exist"
    # The wrapping fold call immediately precedes this on the same notes= assignment.
    preceding = source[:idx]
    last_notes_assignment = preceding.rfind("notes=")
    fold_call_text = preceding[last_notes_assignment:idx]
    assert "fold_verification_notes(" in fold_call_text, (
        "the sandbox/install-failure short-circuit must fold VerificationResult.notes with "
        "fold_verification_notes() (structure-aware, preserves diagnostic lines), never the "
        "plain fold_specialist_result() -- this is the exact gap found live 2026-08-04"
    )
    assert "fold_specialist_result(" not in fold_call_text


def test_fold_specialist_result_no_longer_imported_unused_in_loop_module():
    """Confirms the stale import was cleaned up, not just the call site -- an unused import of
    the wrong function left behind would be a real, if silent, invitation to reintroduce it."""
    assert not hasattr(loop_module, "fold_specialist_result"), (
        "fold_specialist_result should no longer be imported into manager/loop.py at all -- "
        "its only real call site here was the one just fixed to use fold_verification_notes()"
    )
