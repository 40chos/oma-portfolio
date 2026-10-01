"""Phase 35 §15 -- deterministic code generators for certified directions.

Each module here replaces LLM code-authorship with structured parameter
extraction plus deterministic emission, for the narrow, evidence-backed
slice of a certified direction's real parameter space. See
docs/planning/PHASE35_DIRECTION_CERTIFICATION_ROADMAP_2026-08-12.md §15
for the full design, the shadow-mode rollout requirement (§15.8), and
the kill-switch/adjudicator-review discipline (§15.9) every generator
here must go through before it is ever trusted for a real submission.

Nothing in this package is wired into the live task pipeline yet --
§15.8 requires a shadow-mode validation period (computed and compared,
never submitted) before any generator here is live-gated in
manager/loop.py. Building the generator and its tests is a safe, purely
additive first step; flipping it live is a separate, later step.
"""
