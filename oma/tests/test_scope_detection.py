"""Isolated + live tests for manager.scope_detection
(detect_existing_custom_module_target) -- Phase 22 follow-up,
2026-07-23. The isolated tests mock nothing about the real network
calls (this module has no logic worth testing without them); they
verify the SAFETY GATES using fakes for the three real-world lookups
(list_custom_site_module_names, is_custom_site_module,
get_module_state_fast) plus a fake LLM response, so the pure
decision logic is provable without live infra. Live-verification of
the LLM's own real accuracy on real goal text is exercised separately,
directly against the live gateway, not here.
"""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from manager.scope_detection import (
    _fuzzy_match_existing_custom_module,
    _ModulePick,
    detect_existing_custom_module_target,
)


def _run(coro):
    return asyncio.run(coro)


@patch("manager.scope_detection.list_custom_site_module_names", return_value=["project_fieldjob", "mis_base_extend"])
@patch("manager.scope_detection.list_own_scaffolded_module_names", return_value=[])
@patch("manager.scope_detection.call_structured", new_callable=AsyncMock)
@patch("manager.scope_detection.is_fast_path_eligible", return_value=True)
@patch("manager.scope_detection.is_custom_site_module", return_value=True)
@patch("manager.scope_detection.get_module_state_fast", return_value="installed")
def test_valid_pick_from_real_candidates_returns_the_module(
    _state, _custom, _fast, mock_llm, _own_scaffolded, _candidates,
):
    mock_llm.return_value = _ModulePick(module_name="project_fieldjob")
    result = _run(detect_existing_custom_module_target("goal text", "db", client=object(), model="m"))
    assert result == "project_fieldjob"


@patch("manager.scope_detection.list_custom_site_module_names", return_value=["project_fieldjob"])
@patch("manager.scope_detection.list_own_scaffolded_module_names", return_value=[])
@patch("manager.scope_detection.call_structured", new_callable=AsyncMock)
def test_llm_pick_outside_the_real_candidate_list_is_rejected(mock_llm, _own_scaffolded, _candidates):
    """Real safety check: even though _ModulePick has no enum constraint
    at the pydantic level (a plain str field), a name NOT in the real,
    live candidate list must never be trusted -- structured output can
    still occasionally drift outside its own intended range.
    """
    mock_llm.return_value = _ModulePick(module_name="totally_made_up_module")
    result = _run(detect_existing_custom_module_target("goal text", "db", client=object(), model="m"))
    assert result is None


@patch("manager.scope_detection.list_custom_site_module_names", return_value=["project_fieldjob"])
@patch("manager.scope_detection.list_own_scaffolded_module_names", return_value=[])
@patch("manager.scope_detection.call_structured", new_callable=AsyncMock)
def test_llm_returns_none_for_genuinely_new_work(mock_llm, _own_scaffolded, _candidates):
    mock_llm.return_value = _ModulePick(module_name=None)
    result = _run(detect_existing_custom_module_target("build something new", "db", client=object(), model="m"))
    assert result is None


def test_empty_candidate_list_short_circuits_without_any_llm_call():
    """No custom modules found at all (e.g. the SSH lookup itself
    failed) -- must never even attempt an LLM call, let alone guess.
    """
    with patch("manager.scope_detection.list_custom_site_module_names", return_value=[]) as _candidates, \
         patch("manager.scope_detection.list_own_scaffolded_module_names", return_value=[]), \
         patch("manager.scope_detection.call_structured", new_callable=AsyncMock) as mock_llm:
        result = _run(detect_existing_custom_module_target("goal", "db", client=object(), model="m"))
        assert result is None
        mock_llm.assert_not_called()


@patch("manager.scope_detection.list_custom_site_module_names", return_value=["project_fieldjob"])
@patch("manager.scope_detection.list_own_scaffolded_module_names", return_value=[])
@patch("manager.scope_detection.call_structured", new_callable=AsyncMock)
@patch("manager.scope_detection.is_fast_path_eligible", return_value=True)
@patch("manager.scope_detection.is_custom_site_module", return_value=True)
@patch("manager.scope_detection.get_module_state_fast", return_value="uninstalled")
def test_module_on_disk_but_not_installed_is_rejected(
    _state, _custom, _fast, mock_llm, _own_scaffolded, _candidates,
):
    """A module directory existing under the custom addons root
    doesn't mean it's actually live right now -- must check real
    installed state, not just filesystem presence.
    """
    mock_llm.return_value = _ModulePick(module_name="project_fieldjob")
    result = _run(detect_existing_custom_module_target("goal", "db", client=object(), model="m"))
    assert result is None


@patch("manager.scope_detection.list_custom_site_module_names", return_value=["project_fieldjob"])
@patch("manager.scope_detection.list_own_scaffolded_module_names", return_value=[])
@patch("manager.scope_detection.call_structured", new_callable=AsyncMock)
def test_llm_call_exception_degrades_to_none_not_a_crash(mock_llm, _own_scaffolded, _candidates):
    mock_llm.side_effect = ValueError("gateway hiccup")
    result = _run(detect_existing_custom_module_target("goal", "db", client=object(), model="m"))
    assert result is None


# ---------------------------------------------------------------------------
# Phase 30 §26 follow-up (2026-08-04): the deterministic fallback for when the LLM path
# confidently returns null despite a real, obvious candidate already being in the list it was
# shown -- confirmed live, the exact goal shape below (task008 from a real 24-task sweep).
# ---------------------------------------------------------------------------

def test_fuzzy_match_finds_the_single_obvious_candidate():
    goal = (
        "The 'Send to customer' button on the fieldjob form should only be visible to System "
        "Administrators. Normal users and managers should not see it."
    )
    result = _fuzzy_match_existing_custom_module(goal, ["project_fieldjob", "mis_base_extend"])
    assert result == "project_fieldjob"


def test_fuzzy_match_declines_when_two_candidates_share_the_same_distinctive_token():
    goal = "Something about the fieldjob form."
    result = _fuzzy_match_existing_custom_module(goal, ["project_fieldjob", "billing_fieldjob"])
    assert result is None, "two candidates matching the same token is ambiguous -- must not guess"


def test_fuzzy_match_returns_none_for_genuinely_new_work():
    result = _fuzzy_match_existing_custom_module("build something brand new", ["project_fieldjob"])
    assert result is None


def test_fuzzy_match_requires_a_whole_word_not_a_partial_substring():
    """"fieldjob2" (project_fieldjob2's own distinctive token) must not match a goal that only
    contains the substring "fieldjob" without the trailing "2" -- word-boundary match, never a
    naive substring check."""
    result = _fuzzy_match_existing_custom_module("the fieldjob form", ["project_fieldjob2"])
    assert result is None


@patch("manager.scope_detection.list_custom_site_module_names", return_value=["project_fieldjob", "mis_base_extend"])
@patch("manager.scope_detection.list_own_scaffolded_module_names", return_value=[])
@patch("manager.scope_detection.call_structured", new_callable=AsyncMock)
@patch("manager.scope_detection.is_fast_path_eligible", return_value=True)
@patch("manager.scope_detection.is_custom_site_module", return_value=True)
@patch("manager.scope_detection.get_module_state_fast", return_value="installed")
def test_task008_exact_goal_shape_now_resolves_via_fallback_when_llm_returns_null(
    _state, _custom, _fast, mock_llm, _own_scaffolded, _candidates,
):
    """Reproduces the exact real-world failure confirmed live, 2026-08-04: the LLM path returned
    {"module_name": null} for this precise goal, despite "project_fieldjob" being a real,
    correct, unambiguous candidate already in the list it was shown. The deterministic fallback
    must now catch this case."""
    mock_llm.return_value = _ModulePick(module_name=None)
    goal = (
        "The 'Send to customer' button on the fieldjob form should only be visible to System "
        "Administrators. Normal users and managers should not see it."
    )
    result = _run(detect_existing_custom_module_target(goal, "db", client=object(), model="m"))
    assert result == "project_fieldjob"


@patch("manager.scope_detection.list_custom_site_module_names", return_value=["project_fieldjob"])
@patch("manager.scope_detection.list_own_scaffolded_module_names", return_value=[])
@patch("manager.scope_detection.call_structured", new_callable=AsyncMock)
@patch("manager.scope_detection.is_fast_path_eligible", return_value=True)
@patch("manager.scope_detection.is_custom_site_module", return_value=True)
@patch("manager.scope_detection.get_module_state_fast", return_value="uninstalled")
def test_fallback_pick_still_goes_through_the_same_installed_state_gate(
    _state, _custom, _fast, mock_llm, _own_scaffolded, _candidates,
):
    """The fallback path must never bypass the exact same real-world re-verification the LLM
    path already requires -- a fallback-sourced pick for a module that's on disk but not
    installed must still be rejected."""
    mock_llm.return_value = _ModulePick(module_name=None)
    result = _run(detect_existing_custom_module_target("the fieldjob form", "db", client=object(), model="m"))
    assert result is None


@patch("manager.scope_detection.list_custom_site_module_names", return_value=["project_fieldjob", "mis_base_extend"])
@patch("manager.scope_detection.list_own_scaffolded_module_names", return_value=[])
@patch("manager.scope_detection.call_structured", new_callable=AsyncMock)
def test_deterministic_fuzzy_match_wins_when_it_finds_an_answer_llm_never_called(mock_llm, _own_scaffolded, _candidates):
    """Phase 35 fix (2026-08-13 overnight, single_new_field bake-in, 4+ real live occurrences
    the same night): this test used to assert the OPPOSITE priority (the LLM's pick always wins
    over the fallback) -- confirmed live, repeatedly, to be the wrong priority: the LLM pick
    prompt embeds the ENTIRE real candidate list (confirmed live at ~1,816 real entries) in one
    call, and returned `module_name: null` for goals naming their real target module VERBATIM,
    on 4+ separate real occurrences the same night, while the cheap, deterministic fuzzy/exact
    match (tested directly, in isolation, against those exact real failing goals) answered
    correctly every time. The real, corrected priority: the cheap, reliable, zero-network-cost
    deterministic check now runs FIRST and, when it finds an unambiguous answer, the expensive,
    unreliable-at-this-scale LLM call is never even reached -- confirmed here via `mock_llm`
    never being awaited.
    """
    mock_llm.return_value = _ModulePick(module_name="mis_base_extend")
    with patch("manager.scope_detection.is_fast_path_eligible", return_value=True), \
         patch("manager.scope_detection.is_custom_site_module", return_value=True), \
         patch("manager.scope_detection.get_module_state_fast", return_value="installed"):
        result = _run(detect_existing_custom_module_target("the fieldjob form", "db", client=object(), model="m"))
    assert result == "project_fieldjob", (
        "the deterministic fuzzy match's own unambiguous answer must win -- it is the more "
        "reliable signal, confirmed live"
    )
    mock_llm.assert_not_awaited()
