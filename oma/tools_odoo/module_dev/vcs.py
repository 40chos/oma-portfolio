"""Phase 18 (§22.4): a Gitea-backed durable baseline for generated
module code -- the direct fix for four real, confirmed-this-session
problems with treating the odoo16-dev container's own live filesystem
as the only record of a module's prior-round state: (1) race conditions
against a concurrently-writing round, (2) no real undo, (3) no durable
record survives a retry's own overwrite, (4) module-name churn leaves
undiagnosable debris on disk.

This does NOT replace /mnt/extra-addons as the install target -- Odoo
still needs the files physically present to run -i/-u against them.
Gitea is the durable source of truth Build/Code-Review reason from; the
container's filesystem remains written by write_module_file() exactly
as before, immediately before install_module() runs.

One branch per task_id, named `task/<task_id>`, in the dedicated repo
named by DEV_AGENT_GITEA_ORG/DEV_AGENT_GITEA_GENERATED_MODULES_REPO
(SECRETS/dev-agent.env). Every commit uses Gitea's own "change multiple
files" contents API so a round's files land as ONE real commit, not N
racy individual writes. Credentials are read directly from
SECRETS/dev-agent.env via dotenv_values() (never mutating os.environ,
never printed/logged) -- this is the one file this module is allowed to
read, per the standing project rule that credentials live only there.
"""

from __future__ import annotations

import base64
import io
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import httpx
from dotenv import dotenv_values

_SECRETS_ENV_PATH = Path(__file__).resolve().parents[2] / "SECRETS" / "dev-agent.env"

_REQUIRED_KEYS = (
    "DEV_AGENT_GITEA_URL",
    "DEV_AGENT_GITEA_TOKEN",
    "DEV_AGENT_GITEA_ORG",
    "DEV_AGENT_GITEA_GENERATED_MODULES_REPO",
)

_COMMITTER_NAME = "dev-agent"
_COMMITTER_EMAIL = "dev-agent@oma.local"


class VcsError(RuntimeError):
    """Base for structured vcs.py failures -- callers should never parse
    a raw HTTP error string to decide what happened.
    """


def _gitea_config() -> dict[str, str]:
    values = dotenv_values(_SECRETS_ENV_PATH)
    missing = [k for k in _REQUIRED_KEYS if not values.get(k)]
    if missing:
        raise VcsError(
            f"missing required Gitea config key(s) {missing} in {_SECRETS_ENV_PATH} "
            "-- confirm SECRETS/dev-agent.env has real DEV_AGENT_GITEA_* values set"
        )
    return {k: values[k] for k in _REQUIRED_KEYS}  # type: ignore[misc]


_SYSTEM_CA_BUNDLE = "/etc/ssl/certs/ca-certificates.crt"
# A self-hosted Gitea behind a self-signed/internal CA needs its cert trusted
# explicitly -- pointing verify at the system CA bundle (which picks up any CA
# the host trusts) instead of leaving httpx's bundled certifi default in place
# is what makes this connect, not a relaxation of verification. Set
# OMA_GITEA_SKIP_VERIFY=1 only for local dev against a plain-HTTP Gitea.


def _client(cfg: dict[str, str]) -> httpx.Client:
    verify: bool | str = _SYSTEM_CA_BUNDLE if Path(_SYSTEM_CA_BUNDLE).is_file() else True
    return httpx.Client(
        base_url=cfg["DEV_AGENT_GITEA_URL"].rstrip("/"),
        headers={"Authorization": f"token {cfg['DEV_AGENT_GITEA_TOKEN']}"},
        timeout=30.0,
        verify=verify,
    )


def _branch_name(task_id: str, node_label: str | None = None) -> str:
    """Phase 31 §6(a): `node_label`, when given, names a per-node branch
    (`node/<task_id>/<node_label>`) distinct from the task's own continuous branch
    (`task/<task_id>`) -- used for the concurrent-write-safety mechanism's own isolated
    per-node dispatch snapshot. None (the default) preserves today's exact task-level branch
    name, unchanged.

    Deliberately a SEPARATE top-level ref prefix (`node/...`), not `task/<task_id>/node/...` --
    real, live-confirmed finding (2026-08-07): git's own ref storage forbids a ref from being
    both a leaf AND a directory in the same namespace, so a per-node branch nested UNDER the
    already-existing `task/<task_id>` leaf branch is structurally impossible to create (Gitea
    surfaces this as a confusing "branch already exists" 409, not a clearer path-conflict
    error). `node/<task_id>/<node_label>` shares no path prefix with `task/<task_id>`, so no
    such conflict can ever arise.
    """
    if node_label is None:
        return f"task/{task_id}"
    return f"node/{task_id}/{node_label}"


def _repo_is_empty(client: httpx.Client, owner: str, repo: str) -> bool:
    repo_resp = client.get(f"/api/v1/repos/{owner}/{repo}")
    if repo_resp.status_code != 200:
        raise VcsError(f"Gitea repo lookup failed ({repo_resp.status_code}): {repo_resp.text[:500]}")
    return bool(repo_resp.json().get("empty"))


def _ensure_branch(
    client: httpx.Client, owner: str, repo: str, branch: str, fork_from_ref: str | None = None,
) -> bool:
    """Returns True if the branch already existed, False if it was just
    created (a genuine round-1 case -- there's nothing to conflict with
    yet, so every file in this round's commit is a `create`, never an
    `update`). Callers must check `_repo_is_empty()` first -- a
    genuinely empty repo (no commits at all, e.g. right after manual
    creation) has no default_branch to fork a task branch from at all;
    that case is handled directly in commit_validated_round() by
    committing straight onto the task branch name, which Gitea accepts
    as the very first commit of an empty repo and adopts as its default
    branch.

    `fork_from_ref` (Phase 31 §6(a)): when given, a brand-new branch forks from this ref instead
    of the repo's own `default_branch`. MUST be a real, existing branch NAME -- confirmed live
    (2026-08-07) that Gitea's branch-create API rejects a raw commit SHA here ("The old branch
    does not exist", HTTP 404); it forks from whatever branch you name's own CURRENT head at
    creation time, so passing a branch name is sufficient and correct (no need to resolve/pass a
    SHA at all). Used
    by commit_validated_round()'s own per-node dispatch path so a per-node branch forks from the
    TASK's own current branch tip, not `main` -- forking from `main` would silently drop every
    conditional (views/security-xml/extra-data/tests) file the task has accumulated across
    earlier rounds, since only manifest/models/security-csv are unconditionally rewritten every
    round (specialists/build/specialist.py:15264-15278). None (the default) preserves today's
    exact fork-from-default_branch behavior, unchanged.
    """
    resp = client.get(f"/api/v1/repos/{owner}/{repo}/branches/{branch}")
    if resp.status_code == 200:
        return True
    if resp.status_code != 404:
        raise VcsError(f"Gitea branch lookup failed ({resp.status_code}): {resp.text[:500]}")

    if fork_from_ref is not None:
        old_branch_name = fork_from_ref
    else:
        repo_resp = client.get(f"/api/v1/repos/{owner}/{repo}")
        if repo_resp.status_code != 200:
            raise VcsError(f"Gitea repo lookup failed ({repo_resp.status_code}): {repo_resp.text[:500]}")
        default_branch = repo_resp.json().get("default_branch")
        if not default_branch:
            raise VcsError(f"repo {owner}/{repo} has no default_branch and is not reported empty -- unexpected state")
        old_branch_name = default_branch
    create_resp = client.post(
        f"/api/v1/repos/{owner}/{repo}/branches",
        json={"new_branch_name": branch, "old_branch_name": old_branch_name},
    )
    if create_resp.status_code not in (200, 201):
        raise VcsError(f"Gitea branch create failed ({create_resp.status_code}): {create_resp.text[:500]}")
    return False


def _branch_commit_sha(client: httpx.Client, owner: str, repo: str, branch: str) -> str | None:
    resp = client.get(f"/api/v1/repos/{owner}/{repo}/branches/{branch}")
    if resp.status_code == 404:
        return None
    if resp.status_code != 200:
        raise VcsError(f"Gitea branch lookup failed ({resp.status_code}): {resp.text[:500]}")
    return resp.json()["commit"]["id"]


def _existing_paths(client: httpx.Client, owner: str, repo: str, branch: str) -> set[str]:
    # Gitea's git/trees endpoint takes a real commit (or tree) SHA, NOT
    # a branch name -- confirmed live, a branch name here 404s even
    # though the branch itself resolves fine everywhere else in this
    # module's own API calls.
    commit_sha = _branch_commit_sha(client, owner, repo, branch)
    if commit_sha is None:
        return set()
    resp = client.get(f"/api/v1/repos/{owner}/{repo}/git/trees/{commit_sha}", params={"recursive": "true"})
    if resp.status_code == 404:
        return set()
    if resp.status_code != 200:
        raise VcsError(f"Gitea tree lookup failed ({resp.status_code}): {resp.text[:500]}")
    return {entry["path"] for entry in resp.json().get("tree", []) if entry.get("type") == "blob"}


def commit_count_for_task(task_id: str, node_label: str | None = None) -> int:
    """How many commits already exist on task_id's own branch -- used by
    the Build specialist to derive an honest round number for a commit
    message without needing manager/loop.py's own round_number threaded
    through the SpecialistType.run(contract)-only interface (a wider
    change than this phase's own scope justifies). Returns 0 for a
    branch that doesn't exist yet (genuine round 1).

    `node_label` (Phase 31 §6(a)): when given, counts commits on the PER-NODE branch instead --
    a per-node branch is always brand-new at first commit (forked fresh from the task branch's
    own current tip each time apply_node_result_to_module() cleans it up), so this almost always
    returns 0/1, never the task branch's own accumulated total.
    """
    cfg = _gitea_config()
    owner, repo = cfg["DEV_AGENT_GITEA_ORG"], cfg["DEV_AGENT_GITEA_GENERATED_MODULES_REPO"]
    branch = _branch_name(task_id, node_label)
    with _client(cfg) as client:
        commit_sha = _branch_commit_sha(client, owner, repo, branch)
        if commit_sha is None:
            return 0
        resp = client.get(
            f"/api/v1/repos/{owner}/{repo}/commits",
            params={"sha": branch, "limit": 250, "path": "", "stat": "false", "verification": "false"},
        )
        if resp.status_code != 200:
            raise VcsError(f"Gitea commit-history lookup failed ({resp.status_code}): {resp.text[:500]}")
        return len(resp.json())


def commit_validated_round(
    task_id: str, module_name: str, files: dict[str, str], round_number: int, summary: str,
    node_label: str | None = None,
) -> str:
    """Called immediately after a round's generated files pass every
    pre-write validator, before write_module_file() writes them onto
    the live container. Commits to task_id's own branch in
    oma/oma-generated-modules over the same HTTPS credential already
    confirmed working (SECRETS/dev-agent.env). Returns the real commit
    SHA, which gets recorded on the round's own ReplanRound.commit_sha
    (§22.13) so a later round -- or a human, reading the Gitea history
    directly -- can always answer "what did round N actually look like"
    with certainty, not a reconstruction.

    `files` is keyed exactly like `read_module_files()`'s own return
    shape (relative paths inside the module, e.g. "manifest.py",
    "models/models.py") -- module_name is used only to namespace those
    paths inside the branch, since a future task could in principle
    touch more than one module across its own history.

    `node_label` (Phase 31 §6(a)): when given, commits to a per-node branch
    (`node/<task_id>/<node_label>`) instead of the task's own continuous branch -- a
    brand-new per-node branch forks from the TASK branch's own CURRENT tip (never `main`/
    default_branch, per `_ensure_branch()`'s own `fork_from_ref` docstring). None (the default)
    preserves today's exact task-level-branch behavior, byte-for-byte.
    """
    cfg = _gitea_config()
    owner, repo = cfg["DEV_AGENT_GITEA_ORG"], cfg["DEV_AGENT_GITEA_GENERATED_MODULES_REPO"]
    branch = _branch_name(task_id, node_label)
    message = f"round {round_number} ({module_name}): {summary}"

    with _client(cfg) as client:
        inherited: set[str] = set()
        if _repo_is_empty(client, owner, repo):
            # Nothing to branch from yet -- committing directly onto the
            # task branch name is what actually initializes the repo on
            # Gitea; there is no existing content anywhere to conflict
            # with, so every file below is a `create`.
            existing: set[str] = set()
        else:
            if node_label is not None:
                # Real, live-confirmed finding (2026-08-07): Gitea's branch-create API rejects a
                # raw commit SHA for `old_branch_name` ("The old branch does not exist", HTTP
                # 404) -- it must be an actual branch NAME, which Gitea then forks from that
                # branch's own current HEAD. The task branch's own NAME is already exactly the
                # right, stable reference -- no need to resolve/pass its SHA at all.
                fork_from_ref = _branch_name(task_id)
            else:
                fork_from_ref = None
            branch_existed = _ensure_branch(client, owner, repo, branch, fork_from_ref=fork_from_ref)
            if branch_existed:
                existing = _existing_paths(client, owner, repo, branch)
            else:
                # Real, confirmed bug found live (2026-07-28, Phase 28B):
                # a brand-new branch created by _ensure_branch() above is
                # a real git fork of the repo's own default branch, not
                # an empty ref -- Gitea's branch-create API has no
                # "orphan" option. Confirmed directly against the live
                # Gitea instance: this repo's own default branch ('main')
                # still carried a stray leftover file tree
                # ('oma_test_vcs_probe/...') from whichever long-past
                # commit happened to be the very first one ever made
                # while the repo was still genuinely empty (the branch
                # above) -- which then permanently became 'main'. Every
                # brand-new task branch created since has silently
                # forked from it, inheriting that same stray content
                # forever, invisible until Phase 28B's own zip-delivery
                # feature made "the branch's own full tree" a real,
                # user-facing artifact for the first time: every
                # delivered zip would have silently included this one
                # alien, unrelated file. Fixed at the root: a genuinely
                # brand-new branch now always deletes everything it
                # inherited from the fork point as part of its own round
                # 1 commit, so the branch ends up containing ONLY what
                # this task itself ever wrote -- exactly matching this
                # function's own long-standing stated assumption above
                # ("there's nothing to conflict with yet").
                inherited = _existing_paths(client, owner, repo, branch)
                if node_label is not None:
                    # Phase 31 §6(a): a per-node branch forks from the TASK's own real,
                    # accumulated branch tip (fork_from_ref above), NOT `main` -- everything
                    # inherited here is legitimate task history (e.g. an earlier round's own
                    # views.xml), never accidental stray content the way forking from `main`
                    # could produce. Treat it as `existing` (so a path this round's own files
                    # also touch becomes an `update`, not a rejected duplicate `create`) and
                    # never schedule any of it for deletion -- the Phase 28B stray-content
                    # cleanup above is deliberately NOT applied to a per-node branch's own
                    # real, wanted inherited content.
                    existing = inherited
                    inherited = set()
                else:
                    existing = set()

        file_ops = []
        for relative_path, content in sorted(files.items()):
            full_path = f"{module_name}/{relative_path}"
            file_ops.append(
                {
                    "operation": "update" if full_path in existing else "create",
                    "path": full_path,
                    "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
                }
            )
        written_paths = {op["path"] for op in file_ops}
        for stray_path in sorted(inherited - written_paths):
            file_ops.append({"operation": "delete", "path": stray_path})

        # Real, live-confirmed finding (2026-08-07, Phase 31 §6): every commit made through this
        # payload with no explicit `date` field on author/committer gets a fixed, bogus
        # "2001-01-01T00:00:00Z" commit timestamp from this Gitea instance -- confirmed directly
        # against the real repo (both the branches API's own `commit.timestamp` and the git
        # commits API's own `commit.author.date`). This makes any AGE-based check (e.g.
        # infra/scripts/cleanup_orphaned_gitea_branches.py's own "older than 24h" rule) silently
        # see every commit as ancient. Fixed by setting a real, current UTC timestamp explicitly
        # on every commit going forward -- commits made before this fix still carry the bogus
        # timestamp, a real, disclosed limitation the cleanup script's own docstring names.
        commit_timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        payload = {
            "branch": branch,
            "message": message,
            "author": {"name": _COMMITTER_NAME, "email": _COMMITTER_EMAIL, "date": commit_timestamp},
            "committer": {"name": _COMMITTER_NAME, "email": _COMMITTER_EMAIL, "date": commit_timestamp},
            "files": file_ops,
        }
        resp = client.post(f"/api/v1/repos/{owner}/{repo}/contents", json=payload)
        if resp.status_code not in (200, 201):
            raise VcsError(f"Gitea batch commit failed ({resp.status_code}): {resp.text[:1000]}")
        data = resp.json()
        commit_sha = (data.get("commit") or {}).get("sha")
        if not commit_sha:
            raise VcsError(f"Gitea batch commit response had no commit sha: {data}")
        return commit_sha


def delete_branch(task_id: str, node_label: str) -> bool:
    """Phase 31 §6(a): deletes a per-node branch (`node/<task_id>/<node_label>`) once its
    own apply-or-conflict outcome has been finalized -- called from the concurrent-write-safety
    mechanism's own `apply_node_result_to_module()`, in a `finally`, so the branch is cleaned up
    regardless of outcome. `node_label` is required -- this function must never be used to
    delete a task's own continuous branch (`task/<task_id>`), which has no node_label.

    Returns True on 200/204 -- confirmed live (2026-08-07) that Gitea's own DELETE branch
    endpoint returns 204 even when the branch never existed at all (idempotent, not an error),
    so True here means "Gitea's response was successful," not "a branch was genuinely removed."
    Returns False only on a real non-2xx Gitea error -- never raises. A failed delete here is a
    non-fatal warning for the caller to log, never something that should fail the round:
    `infra/scripts/cleanup_orphaned_gitea_branches.py` is the real safety net for whatever this
    misses.
    """
    cfg = _gitea_config()
    owner, repo = cfg["DEV_AGENT_GITEA_ORG"], cfg["DEV_AGENT_GITEA_GENERATED_MODULES_REPO"]
    branch = _branch_name(task_id, node_label)
    with _client(cfg) as client:
        resp = client.delete(f"/api/v1/repos/{owner}/{repo}/branches/{branch}")
        return resp.status_code in (200, 204)


def read_last_validated_commit(task_id: str) -> dict[str, str] | None:
    """The read side Build's `prior_files` parameter (§22.6) actually
    calls -- reads the task's own branch HEAD from Gitea, never the
    live container's filesystem, structurally eliminating the race
    condition a live SSH read has against a concurrently-writing round.

    Returns None (not an empty dict) if this is genuinely round 1 with
    nothing committed yet -- same conservative, never-guess posture as
    every _validate_* function already in this codebase. Returned keys
    are the same module-namespaced paths commit_validated_round() wrote
    (`<module_name>/<relative_path>`), so a caller that already knows
    the module_name can strip that prefix itself; this function doesn't
    assume a single-module task to stay honest about what's actually on
    the branch.
    """
    cfg = _gitea_config()
    owner, repo = cfg["DEV_AGENT_GITEA_ORG"], cfg["DEV_AGENT_GITEA_GENERATED_MODULES_REPO"]
    branch = _branch_name(task_id)

    with _client(cfg) as client:
        branch_resp = client.get(f"/api/v1/repos/{owner}/{repo}/branches/{branch}")
        if branch_resp.status_code == 404:
            return None
        if branch_resp.status_code != 200:
            raise VcsError(f"Gitea branch lookup failed ({branch_resp.status_code}): {branch_resp.text[:500]}")

        paths = _existing_paths(client, owner, repo, branch)
        if not paths:
            return None

        result: dict[str, str] = {}
        for path in sorted(paths):
            file_resp = client.get(
                f"/api/v1/repos/{owner}/{repo}/contents/{path}",
                params={"ref": branch},
            )
            if file_resp.status_code != 200:
                raise VcsError(f"Gitea file read failed for {path!r} ({file_resp.status_code}): {file_resp.text[:500]}")
            body = file_resp.json()
            encoded_content = body.get("content", "")
            result[path] = base64.b64decode(encoded_content).decode("utf-8")
        return result


def build_task_module_zip_bytes(task_id: str) -> bytes | None:
    """Phase 28B (2026-07-28): the real "Delivery: Full module as a zip
    file" requirement (`school_student`'s own §0 text). Built directly
    on `read_last_validated_commit()` above -- Gitea's own branch HEAD
    is already this project's one durable, race-free source of truth
    for "what does this task's own module genuinely, finally look like"
    (see this file's own module docstring, problem #1: the live
    container's filesystem is never used for this exact reason). This
    is purely a packaging step over already-committed, already-real
    content -- no new generation, no live container read, no new race
    exposure.

    Paths are already namespaced `<module_name>/<relative_path>` by
    `commit_validated_round()`, which is exactly the directory layout a
    real `unzip` needs to produce an installable module directory --
    written into the archive as-is, never re-derived.

    Returns None (never an empty zip) if nothing has ever been
    committed for this task -- same conservative, never-guess posture
    as `read_last_validated_commit()` itself; a caller must treat this
    as "no deliverable exists yet," not silently hand back an empty
    archive that looks like a real, empty delivery.
    """
    files = read_last_validated_commit(task_id)
    if not files:
        return None
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path, content in sorted(files.items()):
            archive.writestr(path, content)
    return buffer.getvalue()
