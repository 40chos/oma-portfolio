"""Phase 35 §17.4.3 / §18.6: gate/validator/orchestrator source tamper protection -- the LOCAL
half only. §18.6's research (anthropics/claude-code#11226) confirmed a real, reproducible finding:
`permissions.deny`-style rules do NOT stop an agent's own Edit/Write calls against protected
files -- the only real enforcement point is one the agent's own tool calls cannot reach. This
module builds the hash-pin/detection mechanism (§18.6 step 1-2: a manifest, a load-time check
against it); it deliberately does NOT build the external-authority half (§18.6 step 3-4: a CI
check enforced via branch protection, a re-signing identity distinct from the agent's own session
credential) -- that requires real changes to this repo's git hosting / branch protection
configuration, a decision for a human to make and configure, not something to invent and wire in
unsupervised. Ships as a real, working detection mechanism (compute a manifest, detect drift
against it) that a human can layer real CI enforcement on top of later, per §18.6's own
recommendation, rather than a fake placeholder.

Real, honest limitation stated plainly: WITHOUT the external-authority half, this module can only
ever DETECT and LOG that a protected file changed -- since the agent itself has full filesystem
write access to both the protected files AND this module's own manifest file, a sufficiently
determined self-modification could update both together, which is exactly the gap §18.6's own
research says only an out-of-band authority closes. This is disclosed here, not hidden -- see
§17.4.3/§18.6 in the roadmap doc for the full reasoning and the explicit follow-up this leaves.
"""

from __future__ import annotations

import hashlib
import json
import os

_STATE_DIR = os.path.join(os.path.dirname(__file__), "..", "state")
_MANIFEST_PATH = os.path.join(_STATE_DIR, "gate_source_manifest.json")
_REPO_ROOT = os.path.join(os.path.dirname(__file__), "..")

# The real gate/validator/orchestrator source files this session built and the pre-existing
# pre-write validator chain -- the actual safety-critical surface §17.4.3/§4.2's own
# "suppression zero-tolerance" requirement is about protecting. Adding a new gate anywhere in
# this pipeline means adding its file here too, same discipline as
# manager/graph_governance_flags.py's own _KNOWN_GATES registry.
PROTECTED_RELPATHS = frozenset({
    "manager/graph_governance_flags.py",
    "manager/intake_grounding.py",
    "manager/concurrent_claim.py",
    "manager/gate_tamper_protection.py",
    "tools_odoo/knowledge_graph/build_safety_grounding.py",
    "tools_odoo/graph_queries.py",
    "specialists/build/graph_structural_gate.py",
    "infra/fencing.py",
    "manager/scope_certification.py",
})


def _sha256_of_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def compute_manifest(relpaths: frozenset[str] = PROTECTED_RELPATHS) -> dict[str, str]:
    """Real SHA-256 per protected file, keyed by its relpath from the repo root. A missing file
    is recorded as None rather than raising -- a protected file that's been DELETED is itself a
    real, detectable tamper signal, not something this function should crash on."""
    manifest: dict[str, str | None] = {}
    for relpath in sorted(relpaths):
        full_path = os.path.join(_REPO_ROOT, relpath)
        manifest[relpath] = _sha256_of_file(full_path) if os.path.exists(full_path) else None
    return manifest


def write_manifest(manifest: dict[str, str | None], path: str = _MANIFEST_PATH) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp_path = path + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(manifest, f, indent=2, sort_keys=True)
    os.replace(tmp_path, path)


def load_manifest(path: str = _MANIFEST_PATH) -> dict[str, str | None]:
    if not os.path.exists(path):
        return {}
    with open(path) as f:
        return json.load(f)


class TamperFinding:
    __slots__ = ("relpath", "kind", "detail")

    def __init__(self, relpath: str, kind: str, detail: str):
        self.relpath = relpath
        self.kind = kind  # "hash_mismatch" | "missing_file" | "unpinned_protected_file"
        self.detail = detail

    def __repr__(self) -> str:  # pragma: no cover -- debug/log convenience only
        return f"TamperFinding(relpath={self.relpath!r}, kind={self.kind!r})"


def check_for_tampering(
    relpaths: frozenset[str] = PROTECTED_RELPATHS, manifest_path: str = _MANIFEST_PATH,
) -> list[TamperFinding]:
    """Compares the CURRENT real hash of every protected file against the pinned manifest.
    Never raises -- a check that can crash the process it's protecting would be worse than no
    check at all. Real, honest limitation stated in this module's own docstring: without the
    external-authority half (§18.6 steps 3-4, not built here), this detects drift but cannot
    prevent a self-consistent tamper that updates both the protected file and the manifest
    together -- flagged, not hidden.
    """
    findings: list[TamperFinding] = []
    try:
        pinned = load_manifest(manifest_path)
    except Exception as exc:  # noqa: BLE001 -- a corrupt manifest must never crash the caller
        return [TamperFinding("<manifest>", "manifest_unreadable", str(exc))]

    for relpath in sorted(relpaths):
        full_path = os.path.join(_REPO_ROOT, relpath)
        current_hash = _sha256_of_file(full_path) if os.path.exists(full_path) else None
        pinned_hash = pinned.get(relpath, "__NOT_IN_MANIFEST__")
        if pinned_hash == "__NOT_IN_MANIFEST__":
            findings.append(TamperFinding(
                relpath, "unpinned_protected_file",
                "this file is in PROTECTED_RELPATHS but was never pinned in the manifest -- "
                "run compute_manifest()/write_manifest() to pin it.",
            ))
        elif current_hash is None and pinned_hash is not None:
            findings.append(TamperFinding(
                relpath, "missing_file", f"pinned hash {pinned_hash[:12]}... but file no longer exists.",
            ))
        elif current_hash != pinned_hash:
            findings.append(TamperFinding(
                relpath, "hash_mismatch",
                f"pinned {str(pinned_hash)[:12] if pinned_hash else 'None'}..., "
                f"current {str(current_hash)[:12] if current_hash else 'None'}...",
            ))
    return findings
