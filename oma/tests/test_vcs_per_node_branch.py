"""Phase 31 §6(a) (Phase A.5 items 10-11): real, LIVE tests against the actual Gitea instance
(same discipline as tests/test_module_zip_delivery.py) for the per-node branch mechanism --
_branch_name()'s node_label param, commit_validated_round()'s own node_label param (forking a
per-node branch from the TASK branch's own current tip, never `main`), and delete_branch().
"""

import os
import sys
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tools_odoo.module_dev import vcs


def test_branch_name_with_node_label_is_distinct_from_task_branch():
    task_id = str(uuid.uuid4())
    assert vcs._branch_name(task_id) == f"task/{task_id}"
    assert vcs._branch_name(task_id, "my_label") == f"node/{task_id}/my_label"
    print("PASS: node_label produces a distinct, correctly-namespaced per-node branch name "
          "(a separate top-level ref prefix, never nested under the task/<id> leaf branch)")


def test_per_node_branch_forks_from_the_current_task_branch_tip_not_main():
    """The real, found-and-fixed bug this item exists for: a per-node branch forking from
    `main` (an earlier draft's wrong assumption) would silently drop every conditional file
    (views/security-xml/extra-data/tests) the task has accumulated across earlier rounds, since
    only manifest/models/security-csv are unconditionally rewritten every round. This proves the
    per-node branch's own tree, right after creation, ALREADY contains a file from an EARLIER
    task-level round that the per-node round itself never wrote.
    """
    task_id = str(uuid.uuid4())
    module_name = f"oma_test_pernode_{uuid.uuid4().hex[:8]}"

    # Round 1: task-level branch, real accumulated content (a conditional file: views.xml).
    vcs.commit_validated_round(
        task_id=task_id, module_name=module_name,
        files={
            "models/models.py": "# base models\n",
            "views/views.xml": "<odoo><!-- real accumulated view content --></odoo>\n",
        },
        round_number=1, summary="round 1 (task-level)",
    )

    # Round 2: a per-node branch for constraint 'add_computed_field' that ONLY writes
    # models/models.py -- never mentions views.xml at all.
    node_commit_sha = vcs.commit_validated_round(
        task_id=task_id, module_name=module_name,
        files={"models/models.py": "# base models + computed field\n"},
        round_number=2, summary="round 2 (per-node)", node_label="add_computed_field",
    )
    assert node_commit_sha

    node_branch = vcs._branch_name(task_id, "add_computed_field")
    cfg = vcs._gitea_config()
    owner, repo = cfg["DEV_AGENT_GITEA_ORG"], cfg["DEV_AGENT_GITEA_GENERATED_MODULES_REPO"]
    with vcs._client(cfg) as client:
        node_branch_paths = vcs._existing_paths(client, owner, repo, node_branch)

    views_path = f"{module_name}/views/views.xml"
    assert views_path in node_branch_paths, (
        f"the per-node branch must have forked from the TASK branch's own current tip -- "
        f"round 1's own views.xml (a conditional file the node's own round never wrote) must "
        f"still be present. Got: {sorted(node_branch_paths)!r}"
    )
    print(
        f"PASS: the per-node branch's own tree includes {views_path!r}, a file from an EARLIER "
        f"task-level round the node's own commit never wrote -- confirms it forked from the "
        f"task branch's current tip, not `main`"
    )

    vcs.delete_branch(task_id, "add_computed_field")


def test_per_node_branch_commit_does_not_pollute_the_task_level_branch():
    """A per-node commit must land ONLY on the per-node branch -- the task-level branch's own
    HEAD must stay exactly what it was before the node ever dispatched.
    """
    task_id = str(uuid.uuid4())
    module_name = f"oma_test_pernode_{uuid.uuid4().hex[:8]}"

    vcs.commit_validated_round(
        task_id=task_id, module_name=module_name,
        files={"models/models.py": "# v1\n"}, round_number=1, summary="round 1",
    )
    task_level_before = vcs.read_last_validated_commit(task_id)

    vcs.commit_validated_round(
        task_id=task_id, module_name=module_name,
        files={"models/models.py": "# v1 + node-only change, should NOT reach task branch\n"},
        round_number=2, summary="round 2 (per-node)", node_label="isolated_node",
    )

    task_level_after = vcs.read_last_validated_commit(task_id)
    assert task_level_after == task_level_before, (
        "a per-node commit must never change the task-level branch's own HEAD content"
    )
    print("PASS: a per-node branch commit leaves the task-level branch's own HEAD completely untouched")

    vcs.delete_branch(task_id, "isolated_node")


def test_delete_branch_removes_a_real_per_node_branch():
    task_id = str(uuid.uuid4())
    module_name = f"oma_test_pernode_{uuid.uuid4().hex[:8]}"
    vcs.commit_validated_round(
        task_id=task_id, module_name=module_name,
        files={"models/models.py": "# v1\n"}, round_number=1, summary="round 1",
    )
    vcs.commit_validated_round(
        task_id=task_id, module_name=module_name,
        files={"models/models.py": "# node change\n"},
        round_number=2, summary="round 2 (per-node)", node_label="to_delete",
    )

    cfg = vcs._gitea_config()
    owner, repo = cfg["DEV_AGENT_GITEA_ORG"], cfg["DEV_AGENT_GITEA_GENERATED_MODULES_REPO"]
    node_branch = vcs._branch_name(task_id, "to_delete")
    with vcs._client(cfg) as client:
        exists_before = client.get(f"/api/v1/repos/{owner}/{repo}/branches/{node_branch}").status_code == 200
    assert exists_before, "the per-node branch must genuinely exist right after committing to it"

    deleted = vcs.delete_branch(task_id, "to_delete")
    assert deleted is True

    with vcs._client(cfg) as client:
        exists_after = client.get(f"/api/v1/repos/{owner}/{repo}/branches/{node_branch}").status_code == 200
    assert not exists_after, "delete_branch() must genuinely remove the branch from Gitea"
    print("PASS: delete_branch() genuinely removes a real per-node branch from Gitea")


def test_delete_branch_on_an_already_gone_branch_never_raises():
    """Real, live-confirmed finding (2026-08-07): Gitea's own DELETE branch endpoint returns 204
    even when the branch never existed at all -- idempotent, not an error. delete_branch() must
    never raise for this case; its own True/False here reflects Gitea's response code, not
    literally "a branch was removed."
    """
    task_id = str(uuid.uuid4())
    result = vcs.delete_branch(task_id, "never_existed")
    assert result is True, f"Gitea's own DELETE is idempotent (204) for a non-existent branch: got {result!r}"
    print("PASS: delete_branch() on a non-existent branch never raises (Gitea's DELETE is idempotent)")


if __name__ == "__main__":
    test_branch_name_with_node_label_is_distinct_from_task_branch()
    test_per_node_branch_forks_from_the_current_task_branch_tip_not_main()
    test_per_node_branch_commit_does_not_pollute_the_task_level_branch()
    test_delete_branch_removes_a_real_per_node_branch()
    test_delete_branch_on_an_already_gone_branch_never_raises()
    print("\nALL VCS PER-NODE-BRANCH TESTS PASSED")
