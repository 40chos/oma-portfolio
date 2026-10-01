---
name: odoo-module-scaffolding
version: 1.4.0
last_validated: 2026-08-14
description: How to scaffold, write, lint, and install a new or
  extended Odoo module using the Phase 8 toolchain. Use whenever a
  task's capability_class is module_dev — anything that requires
  actual model/view/security code, not just a data edit.
---

# Odoo Module Scaffolding

## When to use this
Any task whose `capability_class` is `module_dev` — adding a field,
model, view, or business logic that requires writing and installing
real module code, as opposed to editing an existing record
(`odoo-xmlrpc-operations` covers that simpler case instead).

## Rules specific to this domain
- **Foundational, non-negotiable, stated explicitly by Operator (2026-07-09):
  never write schema or data directly into Postgres for the Odoo
  database, under any circumstances, even when a real, working
  Postgres credential is available (dev-agent's own Postgres-dev access
  is for other things -- see the odoo-xmlrpc-operations skill and
  dev-agent's own scope notes -- never for the odoo16_dev database
  Odoo itself owns).** This is not a missing permission to work around
  -- it is Odoo's own architecture, on purpose: Odoo's ORM tracks
  schema through its own module/field definitions, and a table changed
  outside that path silently desyncs from what Odoo itself believes
  exists, corrupting upgrades and migrations later even if the
  immediate change appears to work. The only correct path for any
  schema or model change is: change the module's own code (models.py,
  the manifest, security/views), then let Odoo's own upgrade mechanism
  (`odoo-bin -i`/`-u`, i.e. `install_module()`/`lint_module()` in this
  toolchain) apply it. If that upgrade path is ever blocked (e.g. the
  real, confirmed Postgres table-ownership error found live 2026-07-09),
  the fix is fixing what blocks Odoo's own upgrade mechanism -- never
  reaching around it with a direct write, no matter how available or
  tempting a working database credential might be at the time.
- Decide **new module vs. extend an existing one** before scaffolding
  anything. A single new field on an existing model (task 1's shape)
  usually belongs in one small, purpose-named new module — scaffolding
  a second module for the same purpose later, instead of extending the
  first, creates confusing duplication. Check what already exists in
  `/mnt/extra-addons` before assuming a new module is needed.
- Generated modules live in `/mnt/extra-addons` on `odoo16-dev` — never
  `/opt/site`, which is a **read-only** bind mount shared with the host
  image. `tools_odoo.module_dev.toolchain.scaffold_module()` already
  points at the right place; don't hand-construct a different path.
- Always run `lint_module()` after writing real code into a scaffolded
  module, before requesting install. A fresh, unmodified scaffold
  legitimately fails several checks — that's expected — but a module
  with real hand-written code should be re-checked and any genuine
  finding addressed (or consciously accepted with a reason) before
  moving on.
- Only request `install_module()` after lint has actually been run —
  never skip straight from writing code to installing it.
- **Call `infra.fencing.check_fence()` immediately before requesting
  install** (the actual Odoo write) — same non-optional rule as the
  data-operations skill. Acquire the module's lock at the start of the
  task and release it at the end, success or failure.
- **Fill in `compensating_actions` for every forward step that changes
  real state**, not just at the end: before creating a module directory,
  record that undo means removing it; before requesting install, record
  that undo means uninstalling via the same mechanism, never a manual
  database edit. The Manager's `run_compensations()` depends on this
  being filled in accurately as the task proceeds, not reconstructed
  after the fact.
- **Real, confirmed, recurring mistake found live across many real
  tasks**: guessing a plausible-sounding field name instead of the real
  one. The most common: writing `customer_id` when a model's real field
  for "the customer/contact" is `partner_id` — this is true across most
  of Odoo core (`project.project`, `sale.order`, `account.move`, etc.).
  Similarly, `res.partner` does NOT have a `product_ids` field in
  standard Odoo — don't assume a relation exists because the goal
  implies one; if you need it, add it explicitly to YOUR module's own
  model, or use the real, correct field on the actual model that has
  it. Before referencing any field on a model you did not just define
  yourself in this same module, verify the real field name — read the
  target model's real source if it's available in `/mnt/extra-addons`,
  or use `partner_id` as the default guess for "customer/contact"
  relations rather than inventing a name, since that is what most of
  real Odoo core actually uses.
- **Real, confirmed, recurring failure found live across ~20 real
  rounds of the SAME task**: when a goal says "scope X to the
  customer's project" (or any similar "restrict selection to what's
  related to Y" requirement), a plausible-sounding but WRONG approach
  keeps recurring — an empty `pass` stub, filtering by a generic
  category/tag name instead of a real relation, or inventing a field on
  a model that doesn't have it. This is a real *relational* filter, not
  a name lookup: it means restricting a Many2many/Many2one's selectable
  records to those actually linked to another field on the SAME record,
  via a real field that connects them (most commonly through a shared
  `project_id`/`partner_id`, not a category or tag). The concrete
  pattern that is almost always correct — a domain on the field itself,
  computed from the record's own other real field, e.g.:
  ```python
  project_id = fields.Many2one('project.project', required=True)
  product_ids = fields.Many2many('product.product', string='Selectable Products')

  @api.onchange('project_id')
  def _onchange_project_id(self):
      # Real relational scoping: restrict the selection to products
      # actually linked to this project (however that link exists on
      # your own module's own models — e.g. via product.product's own
      # project_id if you added one, or via an intermediate model) --
      # NOT a category/tag name, and NEVER an empty 'pass'.
      if self.project_id:
          return {'domain': {'product_ids': [('id', 'in', self.project_id.product_ids.ids)]}}
      return {'domain': {'product_ids': []}}

  @api.constrains('project_id', 'product_ids')
  def _check_product_scope(self):
      for record in self:
          if record.project_id and record.product_ids:
              allowed = record.project_id.product_ids
              invalid = record.product_ids - allowed
              if invalid:
                  raise ValidationError(
                      f"These products are not part of the selected project: {invalid.mapped('name')}"
                  )
  ```
  The real relation ("how are products linked to a project") depends on
  what your own module actually models — if `project.project` doesn't
  already have a `product_ids` (or equivalent) field, you likely need
  to add it yourself as part of this same task, not assume it exists.
  Both the `onchange` (UI-side filtering) AND the `constrains` (real,
  enforced validation) are required — one alone is not "scoping," it's
  half of it.
- A known, current limitation (as of Phase 8): installing any module
  that adds a column to an existing model currently fails on this
  deployment's duplicate databases with a Postgres table-ownership
  error, entirely outside this project's access boundary to fix. If
  `install_module()` returns `error_kind="postgres_ownership_blocked"`,
  report this honestly as a real, external blocker — do not retry
  blindly, and do not attempt any Postgres-side workaround.
- **Real, confirmed, recurring mistake (Phase 30, P6, §11, 2026-07-30
  — 9 real round instances, one real cluster):** always instantiate a
  field TYPE with parentheses — `name = fields.Text(string='X')`, never
  the bare class reference `name = fields.Text`. A field declared
  without calling it is not a real field at all; it silently fails or
  is rejected by Code-Review, and (unlike a wrong-casing mistake, which
  an existing autofix already corrects) a genuinely missing `()` has no
  automatic fix — get this right the first time.
- **Real, confirmed, recurring mistake (Phase 30, P6, §11, 2026-07-30
  — 9 real round instances):** when a goal mentions view-layer detail
  for a SPECIFIC view type (e.g. "tree view... badge widget,
  decoration-danger", "kanban card should show..."), that is a real,
  separate requirement from the form view — read the goal for every
  view type it actually names, and inherit/modify EACH one it
  describes, not just the form view by default. Silently only touching
  the form view when the goal explicitly describes tree/kanban/other
  view behavior is a real, recurring miss.
- **Real, confirmed platform fact (Phase 30, P6, §11, 2026-07-30, found
  live on the real `school_student` task's own 6-hour, 29-round
  stretch):** Odoo's own test framework does **not** load a module's
  demo data during automated tests by default — a generated test that
  asserts demo data exists/was-loaded will structurally fail every
  time, no matter how many times it's retried, because the platform
  itself skips that data unless a test explicitly opts in. Never
  generate a test that depends on demo data being present unless the
  goal explicitly asks for demo-data-dependent test behavior.
- **Real, confirmed instruction gap (Phase 30, P6, §11, 2026-07-30):**
  when a goal names something by reference to another existing piece of
  work instead of describing it directly ("use the same webhook URL as
  the low-stock alert," "handle it the same way we did for the
  newsletter module"), verify that cross-reference is real BEFORE
  treating it as established fact — read the actual referenced
  module/pattern via the same codebase-read tools already available
  (e.g. checking `/mnt/extra-addons` for the named module), the same
  way any other real field/model name must be verified rather than
  guessed. Proceeding on an unverified cross-reference is the same
  class of mistake as guessing a plausible-sounding field name.
- **Real, confirmed, recurring mistake (overnight direction-certification
  run, 2026-08-13/14 — 3 real occurrences, same shape, across unrelated
  goals):** `security/ir.model.access.csv` for a brand-new model must
  ALWAYS include a row granting `base.group_user` (or an equally broad,
  non-restrictive group) real read/write/create access — never a file
  whose ONLY row scopes access to one narrow, invented group. This
  happened even when the goal never asked for any access restriction at
  all (a plain 3-field model for an "expense receipt index" got a
  single row scoped to an invented `group_expense_managers`, confirmed
  live: `access_oma_expense_receipt_index,...,group_expense_managers,
  1,1,1,0` was the file's only row). The pattern seems triggered by the
  model's own SUBJECT MATTER sounding even mildly sensitive (assets,
  expenses, deliveries, visitor logs) — Build appears to infer an
  access-restriction requirement that was never actually stated. This
  violates Odoo's own hard requirement that the backend stay usable by
  ordinary admins/internal users, and Code-Review correctly blocks it
  every time (confirmed live, all 3 occurrences caught before reaching
  a real install). The correct default, unless the goal explicitly asks
  for restricted access:
  ```csv
  id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink
  access_oma_expense_receipt_index,oma.expense.receipt.index,model_oma_expense_receipt_index,base.group_user,1,1,1,1
  ```
  If the goal DOES explicitly ask to restrict a model/field to a named
  role or group, ADD a second, more specific row for that group — never
  REMOVE or replace the `base.group_user` row to make room for it. A
  restricted row narrows who ALSO gets access; it should never be the
  only row unless the goal explicitly says "only X should have access"
  or equivalent, in which case that explicit instruction — not the
  model's own subject matter — is what justifies scoping it down.
- **Real, confirmed follow-up (same overnight run, 2026-08-14): the
  "ADD a second row" instruction above is not enough on its own —
  confirmed live that the SAME drop-the-base-row mistake still happens
  even when the goal explicitly asks for BOTH normal access AND a named
  group in the same sentence** ("with full CRUD access for regular
  users (base.group_user) and an additional dedicated security group
  for X"). The generated file still had only one row, scoped to the
  named group, with no `base.group_user` row at all — confirmed live:
  `access_oma_visitor_badge,...,group_visitor_badge_frontdesk,1,1,1,0`
  was the file's only row, caught and blocked by Code-Review. When a
  goal asks for both, the correct output is TWO rows, not one:
  ```csv
  id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink
  access_oma_visitor_badge_user,oma.visitor.badge,model_oma_visitor_badge,base.group_user,1,1,1,0
  access_oma_visitor_badge_frontdesk,oma.visitor.badge,model_oma_visitor_badge,oma_module_name.group_visitor_badge_frontdesk,1,1,1,1
  ```
  (perm columns per row can differ deliberately — e.g. the broader row
  read-only, the narrower row full CRUD — but BOTH rows must exist
  whenever the goal asks for both; one row is only ever correct when
  the goal asks for exactly one access level, restricted or not.)
- **Real, confirmed, recurring mistake (overnight direction-certification
  run, 2026-08-14 — 2 real occurrences, identical shape, in one batch):**
  a goal asking for a "quick filter" or "filter button" means a real
  `<filter>` element inside the view's `<search>` arch — Odoo's own
  clickable toggle in the list/kanban view's filter bar, e.g.:
  ```xml
  <record id="view_my_model_search_inherit" model="ir.ui.view">
      <field name="name">my.model.search.inherit</field>
      <field name="model">my.model</field>
      <field name="inherit_id" ref="module.view_my_model_search"/>
      <field name="arch" type="xml">
          <xpath expr="//search" position="inside">
              <filter string="My Tasks" name="filter_my_tasks"
                      domain="[('user_id', '=', uid)]"/>
          </xpath>
      </field>
  </record>
  ```
  Confirmed live, twice, same session: Build instead added a
  `decoration-*` attribute to the `<tree>`/`<list>` view — a real Odoo
  mechanism, but a completely different one (colors existing rows based
  on a condition; adds no clickable control, no `<search>` involvement
  at all). This is a plausible-sounding but wrong substitution, the same
  class of mistake as the field-name-guessing rule above — Code-Review
  correctly caught it both times ("no `<filter>` element at all") before
  either reached a real install. If a goal names a specific `<search>`-
  view element (quick filter, group-by option, default filter), it must
  produce a real `<search>`-arch change, never a `<tree>`/`<list>`/
  `<kanban>` view attribute instead, no matter how visually similar the
  end result might sound in plain English.
- **Real, confirmed, recurring mistake (overnight `record_rule_row_level_
  security` certification run, 2026-08-17 — 3 real occurrences in one
  night, three DIFFERENT wrong mechanisms, same underlying gap): a
  goal asking to restrict regular users to their own records (an
  `ir.rule` row-level-security rule) needs the correct Odoo idiom for
  keeping the rest of the system — admins, other privileged groups,
  pre-existing standard rules — working exactly as before. Every one of
  these was caught correctly by Code-Review before reaching a real
  install, never guessed or worked around:
  1. The new rule's restrictive domain was attached to the WRONG group —
     the privileged "sees everything" group ended up MORE restricted
     than ordinary users, the exact opposite of the goal.
  2. The new rule's domain was technically correct
     (`[('user_ids', 'in', uid)]`-style) but never accounted for a
     PRE-EXISTING standard Odoo rule on the same model (e.g. `project`'s
     own built-in access rule) that already grants broader visibility —
     Odoo combines multiple rules for the SAME group via OR, so a new
     restrictive rule alone does not narrow access already granted
     elsewhere; it must be scoped to the specific group being
     restricted, not left to "compete" with a rule that already applies
     more broadly.
  3. The new rule used `groups_id` = empty (a GLOBAL rule, applying to
     literally everyone, `<field name="groups_id" eval="[]"/>` or the
     field omitted) — a global restrictive rule blocks ordinary admins
     too, since normal admin users are NOT automatically exempt from
     record rules in Odoo (only the actual superuser/`SUPERUSER_ID` is).
  The correct, general pattern: scope the restrictive rule to a SPECIFIC
  group (never leave `groups_id` empty/global unless the goal explicitly
  asks for a universal restriction with no exceptions), and when the
  goal also names a privileged group that should see everything, give
  THAT group its own separate rule with an unrestricted domain
  (`[(1, '=', 1)]`) rather than relying on the restrictive rule's own
  absence-of-domain to cover it:
  ```xml
  <record id="rule_my_model_own_records" model="ir.rule">
      <field name="name">my.model: own records only</field>
      <field name="model_id" ref="model_my_model"/>
      <field name="domain_force">[('user_id', '=', user.id)]</field>
      <field name="groups_id" eval="[(4, ref('base.group_user'))]"/>
  </record>
  <record id="rule_my_model_all_records" model="ir.rule">
      <field name="name">my.model: full access for reviewers</field>
      <field name="model_id" ref="model_my_model"/>
      <field name="domain_force">[(1, '=', 1)]</field>
      <field name="groups_id" eval="[(4, ref('module_name.group_my_reviewer'))]"/>
  </record>
  ```
  Note: unlike ACL rows (`ir.model.access.csv`), record rules are NOT
  automatically bypassed by ordinary admin users (`base.group_system`)
  — only the actual superuser (`SUPERUSER_ID`) skips `ir.rule` checks
  entirely. Do not assume admins are covered "for free"; if the goal's
  intent implies admins should also see everything (most goals of this
  shape do, even when not stated explicitly), give admins their own
  unrestricted rule too, the same second-rule pattern shown above,
  scoped to `base.group_system` — never invent a bypass that isn't
  real Odoo behavior. The two-rule pattern above (restrictive rule
  scoped to a specific group + separate unrestricted rule for whichever
  group(s) the goal says should see everything) is the correct default
  shape for "regular users see only their own, a named privileged
  group sees all."
- **Real, confirmed 4th occurrence (same run, first real attempt after the
  fix above landed): a fresh anti-pattern, not the two-rule pattern being
  followed correctly.** Rather than write the two separate rules shown
  above, Build wrote ONE rule with the restrictive domain and used
  `groups_id` to try to EXCLUDE the privileged group from it. This is not
  real Odoo semantics: `groups_id` on `ir.rule` is an INCLUSION list (the
  rule applies TO members of the listed groups, or to everyone if the
  list is empty) — there is no "apply to everyone except this group"
  mechanism. The generated rule ended up applying the restrictive domain
  to literally everyone who wasn't in the privileged group, including
  admins and any other real group needing broader access, and Code-Review
  correctly caught it before install. If tempted to "exclude" a group
  from a restriction, that is always a sign the two-rule pattern above is
  needed instead — never try to encode an exclusion through `groups_id`.
- **Real, confirmed 5th occurrence (2026-08-17, same run): the group-inversion
  mistake from the FIRST bullet above (restrictive domain attached to the
  privileged group) recurred a second time, on a different model, even after
  the fixes above had already landed** — confirming this specific habit was
  not reliably correctable via documentation alone. A deterministic pre-write
  validator (`_validate_record_rule_privileged_group_not_also_restricted`,
  `specialists/build/specialist.py`) now backstops the two most-recurring
  concrete mechanisms directly (privileged-group-in-the-restrictive-rule, and
  a missing separate unrestricted rule for the privileged group) and raises a
  corrective error mid-round instead of relying on Code-Review's own later
  catch — this skill text stays as the primary guidance for getting it right
  the first time; the validator is the safety net for when it doesn't.
## How
1. Confirm `capability_class == module_dev`. Check whether an existing
   module in `/mnt/extra-addons` already serves this purpose; extend it
   if so.
2. If a new module is needed: `acquire_module_lock()` on the module
   name, then `scaffold_module(name)`.
3. Write the real model/view/security code into the scaffolded files —
   this is the actual authoring step; there is no shortcut for it.
4. `lint_module(name)` — review every finding; fix what's reasonable to
   fix now, note anything consciously deferred.
5. `check_fence()`, then `install_module(name, db)` against the
   **duplicate** database, never the instance directly.
6. Record `compensating_actions` for each step above as it happens.
7. On completion (success or failure): `release_module_lock()`.
8. Hand back a `SpecialistOutput` with an honest `claims_complete` —
   never claim success on a blocked or partially-completed install.

## Revision Log
- **2026-08-14 (v1.4.0, overnight direction-certification run, breadth push):**
  added the quick-filter rule above — 2 real, live occurrences (same
  session) of Build substituting a `decoration-*` tree-view attribute for
  a real `<filter>` search-view element, both caught by Code-Review.
- **2026-08-14 (v1.3.0, overnight direction-certification run, follow-up):**
  the v1.2.0 rule's "add a second row" instruction wasn't concrete enough
  on its own — the same base.group_user-dropping mistake recurred even
  when the goal explicitly asked for both normal AND restricted access in
  one sentence. Added a concrete two-row example for this exact case.
- **2026-08-14 (v1.2.0, overnight direction-certification run):** added
  the over-restrictive-ACL rule above — 3 real, live occurrences of the
  same shape in one night (a new model's `ir.model.access.csv` scoped to
  ONLY an invented restricted group, with no `base.group_user` row,
  even when the goal never asked for any restriction), each independently
  caught and blocked by Code-Review before reaching a real install.
- **2026-07-30 (v1.1.0, Phase 30 P6, §11):** added 4 real, documented
  revisions, each backed by real `skill_gap` incidents found via
  `scripts/skill_gap_aggregator.py` (a 9-instance cluster on missing
  field-instantiation parentheses; a 9-instance cluster on
  goal-named-tree-view detail being silently skipped; the real,
  live-confirmed demo-data-skipped-by-default platform fact from the
  `school_student` task; and the unverified-cross-reference instruction
  gap). Added `last_validated:` to the frontmatter, next to the
  already-existing `version:` field.
