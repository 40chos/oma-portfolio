#!/usr/bin/env python3
"""Fetches a handful of real Odoo module source trees from the dev container over SSH
into a local directory, so Stage A's parser (driver.py) can be run against them.

This script contains no Odoo source itself -- it only pipes a `docker exec ... tar`
stream from the remote container into a local `tar` extraction. Real source only ever
lands on disk locally, in the output directory you choose; it is never printed,
embedded, or sent anywhere else.

Run this yourself, in your own terminal (same pattern as the pilot's run_full_chain.py).

Usage:
    python3 fetch_modules_via_ssh.py <local_output_dir> [module1 module2 ...]
    python3 fetch_modules_via_ssh.py <local_output_dir> --all-241

    Defaults to the pilot's 6 verified modules (base, web, base_setup, mail, note, crm)
    if no module names are given -- this lets you diff Stage A's real output against the
    pilot's known-correct LLM output for the same modules.

    --all-241 reads the real module list from
    docs/architecture/odoo-knowledge-pipeline/odoo_full_module_graph.json (all 241 modules,
    official modules first per Operator's "clean Odoo first" requirement) instead of the default 6.

    Already-fetched modules (a subdirectory that already exists in the output dir) are skipped
    automatically -- safe to re-run after a partial failure or an interrupted run.
"""

import json
import subprocess
import sys
from pathlib import Path

SSH_HOST = "dev-agent@odoo-dev.int"
SSH_KEY = "/home/andrew/projects/agents/services/oma/SECRETS/dev-agent_ed25519"
DOCKER_CONTAINER = "odoo16-dev"

# Real, authoritative addons_path pulled directly from the container's own Odoo config
# (`cat odoo*.conf | grep addons_path`, 2026-07-19) -- not a guess. 157/241 modules were
# found under just the first two entries; the remaining 84 (mostly OCA/third-party:
# helpdesk, mis-builder, account-financial-reporting, bank-statement-import, odoo-llm,
# etc.) live under the OCA-style extra_addons subdirectories below.
CANDIDATE_ADDONS_PATHS = [
    "/opt/site/16/addons",
    "/opt/site/16/odoo/addons",
    "/opt/site/site16",
    "/opt/site/extra_addons/account-analytic",
    "/opt/site/extra_addons/account-financial-reporting",
    "/opt/site/extra_addons/account-reconcile",
    "/opt/site/extra_addons/bank-statement-import",
    "/opt/site/extra_addons/currency",
    "/opt/site/extra_addons/extra",
    "/opt/site/extra_addons/extra_account",
    "/opt/site/extra_addons/helpdesk",
    "/opt/site/extra_addons/mis-builder",
    "/opt/site/extra_addons/odoo-llm",
    "/opt/site/extra_addons/reporting-engine",
    "/opt/site/extra_addons/report-print-send",
    "/opt/site/extra_addons/server-tools",
    "/opt/site/extra_addons/server-ux",
    "/opt/site/extra_addons/web",
    "/mnt/extra-addons",
]

DEFAULT_MODULES = ["base", "web", "base_setup", "mail", "note", "crm"]

GRAPH_JSON_PATH = Path(
    "/home/andrew/projects/docs/architecture/odoo-knowledge-pipeline/odoo_full_module_graph.json"
)


def load_all_241_modules() -> list[str]:
    """Official modules first (Operator's "clean Odoo first" requirement), then third-party,
    each group in the file's real computed topological order (shallowest/least-dependent
    modules first, per odoo_full_module_graph.json's `topo_order`)."""
    data = json.loads(GRAPH_JSON_PATH.read_text())
    modules = data["modules"]
    topo_position = {name: i for i, name in enumerate(data["topo_order"])}
    official = sorted((n for n in modules if modules[n].get("is_official")), key=lambda n: topo_position[n])
    third_party = sorted((n for n in modules if not modules[n].get("is_official")), key=lambda n: topo_position[n])
    return official + third_party


def ssh_run(remote_cmd: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["ssh", "-i", SSH_KEY, SSH_HOST, remote_cmd],
        capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL,
    )


def build_path_index() -> dict[str, str]:
    """One SSH round-trip per candidate addons path (not per module) -- lists every
    subdirectory once and builds a local module -> addons_path map, instead of doing a
    `test -f` round-trip per module (which would be ~2x241 SSH calls for the full run)."""
    index: dict[str, str] = {}
    for base_path in CANDIDATE_ADDONS_PATHS:
        r = ssh_run(f"sudo docker exec {DOCKER_CONTAINER} ls {base_path}")
        if r.returncode != 0:
            print(f"warning: could not list {base_path}: {r.stderr.strip()}", file=sys.stderr)
            continue
        for name in r.stdout.split():
            index.setdefault(name, base_path)
    return index


def fetch_module(module: str, base_path: str | None, output_dir: Path) -> bool:
    print(f"Fetching {module} ...", end=" ", flush=True)

    if base_path is None:
        print(f"FAILED (not found under any of {CANDIDATE_ADDONS_PATHS})")
        return False

    remote_tar_cmd = f"sudo docker exec {DOCKER_CONTAINER} tar cz -C {base_path} {module}"
    ssh_cmd = ["ssh", "-i", SSH_KEY, SSH_HOST, remote_tar_cmd]
    local_tar_cmd = ["tar", "xz", "-C", str(output_dir)]

    ssh_proc = subprocess.Popen(ssh_cmd, stdout=subprocess.PIPE, stdin=subprocess.DEVNULL)
    tar_proc = subprocess.run(local_tar_cmd, stdin=ssh_proc.stdout)
    ssh_proc.stdout.close()
    ssh_proc.wait()

    if ssh_proc.returncode != 0 or tar_proc.returncode != 0:
        print(f"FAILED (ssh={ssh_proc.returncode}, tar={tar_proc.returncode})")
        return False

    module_dir = output_dir / module
    if not module_dir.is_dir():
        print("FAILED (no output directory produced)")
        return False

    print(f"ok ({base_path}) -> {module_dir}")
    return True


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 1

    output_dir = Path(sys.argv[1])
    output_dir.mkdir(parents=True, exist_ok=True)

    if len(sys.argv) > 2 and sys.argv[2] == "--all-241":
        modules = load_all_241_modules()
        print(f"Loaded {len(modules)} real modules from {GRAPH_JSON_PATH.name} "
              f"(official first, by dependency depth).")
    else:
        modules = sys.argv[2:] or DEFAULT_MODULES

    already_present = [m for m in modules if (output_dir / m).is_dir()]
    to_fetch = [m for m in modules if m not in already_present]
    if already_present:
        print(f"Skipping {len(already_present)} already-fetched module(s) (resume).")

    print(f"Indexing {len(CANDIDATE_ADDONS_PATHS)} remote addons paths "
          f"({len(CANDIDATE_ADDONS_PATHS)} SSH calls total, not one per module)...")
    path_index = build_path_index()

    ok = len(already_present)
    failed = []
    for i, module in enumerate(to_fetch, 1):
        print(f"[{i}/{len(to_fetch)}] ", end="")
        if fetch_module(module, path_index.get(module), output_dir):
            ok += 1
        else:
            failed.append(module)

    print(f"\n{ok}/{len(modules)} modules present in {output_dir}")
    if failed:
        print(f"{len(failed)} module(s) failed: {failed}")
        print("Re-run the same command to retry only the missing ones (already-fetched ones are skipped).")
        return 1

    print("\nNext step -- run Stage A's parser against this real source:")
    print(f"  cd /home/andrew/projects/agents/services/oma")
    print(f"  python3 -m tools_odoo.knowledge_graph.driver {output_dir} <parsed_output_dir>")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
