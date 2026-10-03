"""Phase 30 §26 (Build Prompt-Assembly Correctness) items 1/2 -- the curated, live-schema-excerpt
builder, factored out to a shared, lightweight module (2026-08-04) specifically so both
specialists that construct an LLM-facing prompt from a TaskContract (Build, and now Code-Review --
audited the same night and found to have the identical schema-grounding gap Build had before its
own §26 fix) can reuse ONE real implementation, never two independently-drifting copies. Lives
under tools_odoo/, not specialists/, so importing it never pulls in either specialist's own heavy
module (~14,700 lines for Build's own file) just to reuse this one function, and isn't governed by
the import-linter's manager->specialists contract (root_packages = manager/specialists/contracts/
infra; tools_odoo isn't listed).
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from contracts.schema import TaskContract


def _render_model_field_summary(model_name: str, field_rows: list[dict], is_transient: bool | None = None) -> str:
    """Curated rendering for one model's fields (item 2): name + type + required only --
    deliberately never help-text/method bodies/full ir.model.fields row content, which is exactly
    the "full ORM dump" §26.6 names as this section's own most important non-goal. Deterministic
    ordering (sorted by field name) -- item 7's own concern (byte-identical stable content across
    repeated calls for the same task, so prefix caching can actually engage).

    Real, confirmed gap found live (2026-08-06, Phase 30 Step 0 + root-cause pass, task034/task049):
    the header line never said whether this model is a TransientModel (a wizard) -- both tasks had
    Build correctly resolve the right model NAME, then `_inherit`/extend it as a regular
    `models.Model`, crashing install with "transforms the transient model ... into a non-transient
    model". `is_transient` (from `get_model_is_transient_fast()`) is rendered explicitly, only when
    known True, so an ordinary regular model's header stays unchanged (no added noise for the
    overwhelmingly common case).
    Real, confirmed gap found live (2026-08-06, Phase 30 category-wide info-gap sweep): a
    Selection field's real, valid option keys (e.g. project.fieldjob.state ->
    draft/sent/accepted/rejected/invoiced/done, live-queried) were never fetched by the shared
    reader this function's caller routes through, let alone rendered -- ANY task referencing an
    existing model's own state/status values (task020/030/042/046 and others touching
    project.fieldjob.state alone) had zero grounding for what the real option keys actually are,
    the exact same "the model was never shown this fact" shape as the TransientModel/mail.template
    gaps. Separately, `readonly` was already fetched by `_read_real_field_rows()` but discarded
    before ever reaching this rendering step -- now shown too (a readonly/computed field being
    written to directly is a real, recurring mistake shape elsewhere in this codebase).
    """
    import ast

    kind_note = " (TransientModel -- a wizard, NOT a persisted record; inherit with TransientModel, never Model)" if is_transient else ""
    lines = [f"{model_name}:{kind_note}"]
    for row in sorted(field_rows, key=lambda r: r["name"]):
        req = ", required" if row.get("required") else ""
        ro = ", readonly" if row.get("readonly") else ""
        rel = f" -> {row['relation']}" if row.get("relation") else ""
        sel = ""
        # Critic round 4 (2026-08-06), confirmed live via direct reproduction: a pathological
        # selection string (e.g. a long run of unary minus signs) makes ast.literal_eval raise
        # MemoryError, not caught by the original (ValueError, SyntaxError, TypeError) tuple --
        # breaking this whole module's own explicit "never raises" contract. A length cap before
        # ever calling literal_eval closes this at the source (no legitimate Odoo Selection
        # definition is anywhere near this long) rather than trying to enumerate every exception
        # type literal_eval can theoretically raise.
        selection_raw = row.get("selection") or ""
        if row.get("ttype") == "selection" and selection_raw and len(selection_raw) <= 2000:
            try:
                options = ast.literal_eval(selection_raw)
                keys = [str(k) for k, _label in options]
                # Critic round 4: uncapped rendering violates this file's own repeated "never a
                # full ORM dump" design rule for a Selection field with many real options (custom
                # country/status/document-type lists are common in real Odoo deployments).
                if len(keys) > 15:
                    shown = keys[:15]
                    sel = f" [{', '.join(shown)}, ... {len(keys) - 15} more]"
                else:
                    sel = f" [{', '.join(keys)}]"
            except (ValueError, SyntaxError, TypeError):
                pass  # malformed/unparseable selection string -- never guess, just omit
        lines.append(f"  {row['name']}: {row['ttype']}{rel}{sel}{req}{ro}")
    return "\n".join(lines)


def resolve_current_schema_block(contract: "TaskContract", db: str) -> str:
    """Items 1/2: a curated, live schema excerpt for the goal's real target model
    (`contract.module_identity`, resolved once by the Manager via
    `contracts.module_identity.resolve_module_identity()` -- reused here, never re-derived), plus
    one-hop related models the goal text itself names by their real technical name -- never a
    full ORM/database dump (§26.6's own explicit non-goal).

    Returns "" (never raises) when: no target model could be resolved at all (a genuinely new-
    concept task -- nothing existing to ground against); the live-schema fast path isn't
    available for this database (`is_fast_path_eligible()` -- a fresh/duplicate/sandbox db, same
    allow-list every other fast-path caller in this codebase already respects); or the resolved
    model doesn't actually exist yet. Every caller treats "" as "nothing to render," never an
    error -- this must never be able to block or fail a round or a review.
    """
    from tools_odoo.odoo_schema_client import (
        _read_real_field_rows, get_model_is_transient_fast, get_relation_fields_fast,
        is_fast_path_eligible,
    )

    if not contract.module_identity or not is_fast_path_eligible(db):
        return ""

    target_model = contract.module_identity
    target_rows = _read_real_field_rows(target_model, db, "Admin")
    if not target_rows:
        return ""

    blocks = [_render_model_field_summary(
        target_model, target_rows, get_model_is_transient_fast(target_model, db),
    )]

    # Real, confirmed gap found live (2026-08-06, Phase 30 root-cause pass, task041/task045): this
    # loop previously only included a related model when the goal text happened to literally name
    # it -- but a goal naming a HUMAN-READABLE concept ("tag"/"category", "container") rather than
    # the real technical model name (`project.tags`, `project.container`) got NO related-model
    # grounding at all, and Build then hallucinated a field/relation on the unnamed model (task041:
    # `container_id.partner_id` on project.fieldjob, a field that does not exist there; task045:
    # wrong relation direction on project.tags). Every real, structural one-hop relation the target
    # model actually has is now considered -- still never a full ORM dump (capped, and each related
    # block is itself just name/type/required, same curated shape as the target's own), just no
    # longer gated on the goal text happening to spell out the technical name.
    #
    # Adversarial review finding (2026-08-06, critic round 1): a plain alphabetical top-8 cap has
    # no relevance signal, and `_read_real_field_rows()`/`get_relation_fields_fast()` never filter
    # ORM-automatic fields (create_uid/write_uid -> res.users) or mail.thread-mixin fields
    # (message_ids/message_follower_ids/activity_ids/message_main_attachment_id ->
    # mail.message/mail.followers/mail.activity/ir.attachment) -- on any target model inheriting
    # mail.thread, these sort ahead of most business model names and can consume most/all of the
    # cap before a load-bearing relation is even considered, defeating this fix's own purpose.
    # Fixed by (a) dropping the known-noise relation targets outright, and (b) still prioritizing
    # any relation the goal text literally names (the original, narrower behavior) ahead of the
    # remaining alphabetical fill -- so the common case (goal names the real model) is unaffected,
    # and the new case (goal only names a human concept) now actually reaches business-relevant
    # relations instead of mixin boilerplate.
    # Real, live-tested finding (2026-08-06, critic round 2): a structural (module-ownership-
    # based) alternative to this hardcoded list was tried and confirmed NOT viable -- see the
    # comment above `get_relation_fields_with_owning_modules_fast`'s removal in
    # tools_odoo/odoo_schema_client.py for the live evidence (Odoo attributes a mixin-added
    # field's `ir.model.fields` row to the EXTENDING model's own module, not to `mail`/`base`
    # where the mixin lives -- there's no usable structural signal at this granularity). This
    # list stays hardcoded; kept as complete as real testing found (also now covers
    # `mail.activity.type`, missed on the first pass -- confirmed live via
    # `get_relation_fields_fast('project.project', ...)`).
    _NOISE_RELATION_TARGETS = {
        "res.users", "mail.message", "mail.followers", "mail.activity", "mail.activity.type",
        "ir.attachment", "mail.tracking.value", "mail.notification", "mail.message.subtype",
        "rating.rating",
    }
    relations = get_relation_fields_fast(target_model, db) or {}
    candidates = sorted(
        m for m in set(relations.values())
        if m != target_model and m not in _NOISE_RELATION_TARGETS
    )
    goal_lower = (contract.goal or "").lower()
    goal_words = set(goal_lower.split())

    # Critic round 3 (2026-08-06), live-tested finding: the literal-substring `goal_named` check
    # above only ever matches when the goal spells out the FULL dotted technical name -- but Gap
    # 3's own motivating case (task045: goal says "tag"/"category", never "project.tags") never
    # does that. Live-verified against the real database: `project.project` has 38 real,
    # non-noise related models; `project.tags` sorts to position 20 alphabetically -- outside the
    # 5-slot cap even after the noise fix, so the plain-substring check alone still doesn't
    # surface it. Widened to a per-segment stem match: split each candidate's dotted name into
    # segments (`project.tags` -> `project`, `tags`) and match a segment against the goal if
    # either contains the other as a substring, for segments of at least 4 characters (skips
    # generic/noisy segments like `id`/`line`/`res` that would false-positive on nearly anything).
    # This is intentionally a looser signal than the (still-checked-first) exact full-name match,
    # not a replacement for it.
    def _stem_matches_goal(model_name: str) -> bool:
        if model_name.lower() in goal_lower:
            return True
        for segment in model_name.lower().split("."):
            if len(segment) < 4:
                continue
            if any(segment in w or w in segment for w in goal_words if len(w) >= 3):
                return True
        return False

    goal_named = [m for m in candidates if _stem_matches_goal(m)]
    rest = [m for m in candidates if m not in goal_named]
    # Critic round 2 (2026-08-06): now that noise no longer competes for slots, 5 is plenty of
    # headroom for genuine business relations without the earlier 8-slot cap's larger unconditional
    # per-call RPC cost (each slot costs 2 extra XML-RPC round trips).
    ordered = (goal_named + rest)[:5]

    seen_related: set[str] = set()
    for related_model in ordered:
        if related_model in seen_related:
            continue
        related_rows = _read_real_field_rows(related_model, db, "Admin")
        if related_rows:
            blocks.append(_render_model_field_summary(
                related_model, related_rows, get_model_is_transient_fast(related_model, db),
            ))
            seen_related.add(related_model)

    return "\n\n".join(blocks)


def resolve_current_views_block(contract: "TaskContract", db: str) -> str:
    """Real gap found live (2026-08-06, task038 of the SITE 50-task fix-pass): the sibling of
    `resolve_current_schema_block()` above, but for view xmlids instead of fields. Build has no
    live listing of a model's real existing views when scoped-editing an existing module, so an
    `inherit_id`/xpath target it needs to reference gets guessed at (confirmed live: the same
    wrong guess, `sale_room_management.view_sale_room_line_form`, repeated identically across 2
    rounds even after the deterministic external-id validator correctly rejected it both times --
    the validator worked, but had nothing better to offer as a correction). Uses
    `list_model_view_xmlids_fast()` (tools_odoo/odoo_schema_client.py), the same live-registry
    mechanism this file's own field-grounding block already relies on for `_read_real_field_rows`.

    Returns "" (never raises) under the exact same conditions as `resolve_current_schema_block()`
    -- no target model resolved, fast path unavailable, or the model has no real views at all.
    """
    from tools_odoo.odoo_schema_client import is_fast_path_eligible, list_model_view_xmlids_fast

    if not contract.module_identity or not is_fast_path_eligible(db):
        return ""

    target_model = contract.module_identity
    view_rows = list_model_view_xmlids_fast(target_model, db)
    if not view_rows:
        return ""

    lines = [f"Real, existing views for {target_model} (only ever inherit_id/xpath against one of these -- never invent a plausible-sounding id):"]
    for xmlid, view_type in view_rows:
        lines.append(f"  {xmlid} ({view_type})")
    return "\n".join(lines)


_HEADER_TAG_RE = re.compile(r"<header\b", re.IGNORECASE)
_BUTTON_TAG_RE = re.compile(r"<button\b([^>]*)/?>", re.IGNORECASE)
_BUTTON_NAME_ATTR_RE = re.compile(r"\bname=[\"']([^\"']+)[\"']")
_BUTTON_STRING_ATTR_RE = re.compile(r"\bstring=[\"']([^\"']+)[\"']")


def resolve_current_view_arch_block(contract: "TaskContract", db: str) -> str:
    """Real, confirmed gap found live (2026-08-17, overnight `workflow_with_custom_buttons_or_
    cron` certification run, root-caused after the project owner directly asked whether the live system was
    actually being consulted for this direction's recurring failures): `resolve_current_views_
    block()` above tells Build WHICH real views exist for a model, but nothing tells it what's
    actually INSIDE the one it's about to inherit -- so a goal asking to add a button left Build
    guessing whether a `<header>` element exists, or whether a specific existing button name
    (used as an xpath anchor, e.g. `position="after"` relative to a named button) is really
    there. Confirmed live: 4 of 5 real button-adding attempts that night guessed wrong about the
    target parent view's own real structure, correctly caught by Code-Review's own xpath-
    resolution check every time, but only after a wasted round each time.

    Uses `get_model_form_view_arch_fast()` (tools_odoo/odoo_schema_client.py) -- the exact same
    `fields_view_get(view_type="form")` technique already proven live and safe in the UI-action-
    presence check -- to fetch Odoo's own FINAL, already-inheritance-resolved form-view arch, then
    extracts a curated summary (never the full raw XML -- same "curated excerpt, not a full dump"
    discipline every sibling block in this file follows): whether a `<header>` element exists, and
    the real name/label of every existing `<button>` already in the view. This is deterministic
    regex extraction, not an LLM call or a second live query -- cheap, and the same lightweight-
    parsing style `specialists/testing_qa/ui_action_presence_check.py` already uses successfully
    on this exact kind of content.

    Returns "" (never raises) under the same conditions as the sibling blocks above -- no target
    model resolved, fast path unavailable, or no form view/arch could be fetched.
    """
    from tools_odoo.odoo_schema_client import get_model_form_view_arch_fast, is_fast_path_eligible

    if not contract.module_identity or not is_fast_path_eligible(db):
        return ""

    target_model = contract.module_identity
    arch = get_model_form_view_arch_fast(target_model, db)
    if not arch:
        return ""

    has_header = bool(_HEADER_TAG_RE.search(arch))
    existing_buttons: list[str] = []
    for match in _BUTTON_TAG_RE.finditer(arch):
        attrs = match.group(1)
        name_match = _BUTTON_NAME_ATTR_RE.search(attrs)
        string_match = _BUTTON_STRING_ATTR_RE.search(attrs)
        label = name_match.group(1) if name_match else None
        display = string_match.group(1) if string_match else None
        if label and display:
            existing_buttons.append(f"{label!r} (string={display!r})")
        elif label:
            existing_buttons.append(repr(label))
        elif display:
            existing_buttons.append(f"(string={display!r})")

    lines = [
        f"Real, current 'form' view structure for {target_model} -- Odoo's own already-"
        f"inheritance-resolved arch, not a guess. Only ever add a NEW <button> via an xpath "
        f"target that ACTUALLY EXISTS below -- never assume a <header> or another button is "
        f"present if it isn't listed here:",
        f"  <header> element: {'present' if has_header else 'ABSENT -- do not xpath into //header, it does not exist'}",
    ]
    if existing_buttons:
        lines.append("  existing <button> elements already in this view:")
        for b in existing_buttons:
            lines.append(f"    {b}")
    else:
        lines.append("  existing <button> elements already in this view: none")
    return "\n".join(lines)


def resolve_current_mail_templates_block(contract: "TaskContract", db: str) -> str:
    """Real, confirmed gap found live (2026-08-06, Phase 30 root-cause pass on task030): the
    mail.template sibling of `resolve_current_views_block()` above. A goal naming a real template
    by its human label ("our 09.A template") gave Build nothing to resolve that to a real external
    id, so it fabricated one (`your_module.email_template_customer`, confirmed live to not exist)
    that crashed at runtime with `MissingError`. Uses `list_model_mail_templates_fast()`
    (tools_odoo/odoo_schema_client.py), the same live-registry mechanism the view/field grounding
    blocks already rely on.

    Returns "" (never raises) under the exact same conditions as the sibling blocks above -- no
    target model resolved, fast path unavailable, or the model has no real templates at all.
    """
    from tools_odoo.odoo_schema_client import is_fast_path_eligible, list_model_mail_templates_fast

    if not contract.module_identity or not is_fast_path_eligible(db):
        return ""

    target_model = contract.module_identity
    template_rows = list_model_mail_templates_fast(target_model, db)
    if not template_rows:
        return ""

    lines = [
        f"Real, existing mail.template records for {target_model} (only ever self.env.ref() one "
        f"of these when the goal names an existing template -- never invent a plausible-sounding id):"
    ]
    for xmlid, name in template_rows:
        lines.append(f"  {xmlid} ({name!r})")
    return "\n".join(lines)
