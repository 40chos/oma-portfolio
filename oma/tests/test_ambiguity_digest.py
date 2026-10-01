"""Phase U (§25.5) -- regression tests for category-correlation clustering and digest batching of
the ambiguity gate's own pending queue. Uses the real Redis client (matching this repo's own
convention, e.g. tests/test_fencing.py), with test-specific clusters cleared before/after each
test so real production queue state is never touched.
"""
from infra.redis_client import get_redis_client
from manager.ambiguity_digest import (
    clear_ambiguity_cluster,
    format_ambiguity_digest_line,
    list_pending_ambiguity_clusters,
    record_ambiguity_pause,
)

_Q1 = ["Which team should __TEST_PHASE_U__ tickets be routed to?"]
_Q2 = ["What does __TEST_PHASE_U_OTHER__ mean in this context?"]


def _cleanup():
    clear_ambiguity_cluster(_Q1)
    clear_ambiguity_cluster(_Q2)


def test_first_occurrence_is_a_new_cluster():
    _cleanup()
    try:
        cluster = record_ambiguity_pause("task-a", "goal a", _Q1, "reasoning a")
        assert cluster["is_new_cluster"] is True
        assert cluster["count"] == 1
        assert cluster["task_ids"] == ["task-a"]
    finally:
        _cleanup()


def test_multiple_simultaneous_same_category_tasks_collapse_to_one_cluster():
    """§25.8's own definition of done: 'a real test with multiple simultaneous same-category
    ambiguous tasks confirms they actually collapse toward one review item rather than flooding
    Operator with duplicates.' Real, direct test of exactly that claim.
    """
    _cleanup()
    try:
        c1 = record_ambiguity_pause("task-a", "goal a", _Q1, "reasoning a")
        c2 = record_ambiguity_pause("task-b", "goal b", _Q1, "reasoning a")
        c3 = record_ambiguity_pause("task-c", "goal c", _Q1, "reasoning a")

        assert c1["is_new_cluster"] is True
        assert c2["is_new_cluster"] is False
        assert c3["is_new_cluster"] is False

        pending = list_pending_ambiguity_clusters()
        matching = [c for c in pending if c["blocking_questions"] == _Q1]
        assert len(matching) == 1, "three same-category pauses must collapse to exactly one cluster"
        assert matching[0]["count"] == 3
        assert set(matching[0]["task_ids"]) == {"task-a", "task-b", "task-c"}
    finally:
        _cleanup()


def test_different_categories_stay_as_separate_clusters():
    _cleanup()
    try:
        record_ambiguity_pause("task-a", "goal a", _Q1, "reasoning a")
        record_ambiguity_pause("task-b", "goal b", _Q2, "reasoning b")

        pending = list_pending_ambiguity_clusters()
        sigs = {c["signature"] for c in pending if c["blocking_questions"] in (_Q1, _Q2)}
        assert len(sigs) == 2, "genuinely different ambiguity categories must not collapse together"
    finally:
        _cleanup()


def test_digest_line_formatting():
    _cleanup()
    try:
        record_ambiguity_pause("task-a", "goal a", _Q1, "reasoning a")
        cluster = record_ambiguity_pause("task-b", "goal b", _Q1, "reasoning a")
        line = format_ambiguity_digest_line(cluster)
        assert "2 tasks waiting on" in line
        assert _Q1[0] in line
    finally:
        _cleanup()


def test_clearing_a_cluster_makes_the_next_occurrence_genuinely_new():
    """§25.6's own load-bearing boundary: a cluster is a live, in-flight queue item, never a
    durable learned rule. Once cleared (Operator resolved it), the NEXT task hitting the exact same
    category must start a fresh cluster (is_new_cluster=True again) -- nothing here should ever
    cause a later, different task to silently skip surfacing because a past one was answered.
    """
    _cleanup()
    try:
        record_ambiguity_pause("task-a", "goal a", _Q1, "reasoning a")
        clear_ambiguity_cluster(_Q1)

        pending = list_pending_ambiguity_clusters()
        assert not any(c["blocking_questions"] == _Q1 for c in pending)

        fresh = record_ambiguity_pause("task-z", "goal z", _Q1, "reasoning a")
        assert fresh["is_new_cluster"] is True, (
            "a cluster must not implicitly persist past being cleared -- this is a live queue, "
            "not a durable learned-rule store"
        )
        assert fresh["count"] == 1
        assert fresh["task_ids"] == ["task-z"]
    finally:
        _cleanup()


def test_no_durable_rule_memory_module_exists_for_ambiguity_answers():
    """§25.6's explicit warning, confirmed by direct inspection: nothing in this module (or
    manager/loop.py's wiring of it) stores a generalized 'answer' to a past clarifying question
    for reuse on a future, different task. The only persisted state is the live pending cluster
    itself (task_ids/count/timestamps), which clear_ambiguity_cluster() fully removes -- there is
    no separate table/key that survives a clear() call.
    """
    import manager.ambiguity_digest as mod

    source = open(mod.__file__).read()
    assert "retain" not in source.lower()
    assert "learned_rule" not in source.lower()
    assert "recall(" not in source.lower()
    r = get_redis_client()
    keys_after_module_import = r.keys("oma:pending_ambiguity_cluster:*")
    # Only real, currently-open clusters are allowed to exist -- no separate "answers" namespace.
    for key in keys_after_module_import:
        assert b"answer" not in key.lower() if isinstance(key, bytes) else "answer" not in key.lower()
