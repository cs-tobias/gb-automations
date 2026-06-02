# Toggl integration — remaining work

Snapshot taken 2026-06-01 after first dev turn-on (Tobias's personal Toggl + Notion). Project mirror, hours aggregation, and historical backfill are all live and verified end-to-end against `GB Automations`. What follows is the gap between the current behavior and the client brief.

The brief lives in [docs/reference/client-brief.md](../reference/client-brief.md) (and a near-identical [client-brief-2.md](../reference/client-brief-2.md)) — Toggl section starts around line 70.

## Status by brief requirement

| Brief requirement | Status | Where |
|---|---|---|
| Notion → Toggl project mirror | ✅ done | [sync/sync_toggl_project.py](../../src/gb_automations/sync/sync_toggl_project.py) |
| Adopt existing same-name Toggl project (no duplicates) | ✅ done | `_find_workspace_project_by_name` in same file |
| Notion rename → Toggl rename | ✅ done | `update_project(name=…)` branch in `sync_toggl_project` |
| Daily hours sync at 02:00 Oslo | ✅ done | [jobs/scheduler.py:64-70](../../src/gb_automations/jobs/scheduler.py#L64-L70) |
| Aggregate hours per (user, project, day) | ✅ done | `_aggregate` in [sync/sync_toggl_hours.py](../../src/gb_automations/sync/sync_toggl_hours.py) — verified: 17 Toggl entries → 10 day-rows |
| Year-partitioned `Timer YYYY` DB, auto-created | ✅ done | [clients/notion_timer_db.py](../../src/gb_automations/clients/notion_timer_db.py) |
| Toggl→Notion user attribution by email | ✅ done | `_build_notion_user_index` + email match in `sync_toggl_hours`. Dev override available via `TOGGL_DEV_EMAIL_OVERRIDES` |
| Handle retroactive Toggl edits / deletions | ✅ done (32-day window) | replace-not-merge reconciliation in `_reconcile_year`. Edits >32 days back don't propagate — documented trade-off |
| Historical backfill on first turn-on | ✅ done | `POST /debug/toggl/backfill?from=YYYY-MM-DD&to=YYYY-MM-DD`, defaults to Jan 1 → today |

## Gaps

### 1. Project status gate — only mirror when project is "aktivt"

**Brief quote:** *"Det er ikke aktivt når det bare er i tilbudsfase. Men så fort tilbudet er godkjent blir det et aktivt prosjekt."*

Today the Sync-Toggl button (and the Initialize fan-out) fires on any project regardless of status. The brief says a project in tilbudsfase should NOT yet have a Toggl mirror — only once the tilbud is godkjent does it become "aktivt" and earn a Toggl project.

**What's needed:**

- Read the Notion project's status property at the top of `sync_toggl_project`.
- If status is not in the "active set", return a `skipped` result with a clear note.
- Decide which statuses count as active. Likely candidates from the Goldbox workflow: `Aktivt`, `Pågående`. Statuses that should NOT mirror: `Tilbud`, `Tilbudsfase`, `Avslag`, `Arkivert`. **Confirm with Goldbox before coding.**

**Open question:** does the same gate apply to hours sync? If a project is mirrored, then later moves out of active, should Toggl entries on it still flow to Notion? Most likely yes (they're billable hours, status changes don't retroactively un-bill them) but worth a one-line check.

### 2. Project becomes inactive → archive in Toggl (`active=false`)

Symmetric to gap #1. The brief says *"toggl speiler notion"*. If a Notion project moves out of the active set (won → done, or arkivert), the Toggl project should become inactive so it stops cluttering the team's timer dropdowns.

The Toggl client already has the method: `toggl_client.update_project(active=False)` in [clients/toggl.py:357](../../src/gb_automations/clients/toggl.py#L357). Just not wired up.

**What's needed:**

- In `sync_toggl_project`, after the active-status check: if a `TogglProject` cache row EXISTS but the Notion status is no longer active, call `update_project(active=False)` instead of `update_project(name=…)`. Don't delete the cache row — re-activation should re-flip it to true, not create a new Toggl project.
- The trigger: this needs to fire when status changes. Options:
  - **Sync-Toggl button**: works manually (operator presses it after changing status), simplest.
  - **Notion automation on status change**: cleaner, no operator step. Same shape as the Frame "Ferdig" automation (`POST /webhooks/notion/oppgave-done`) — a Notion automation hits a webhook on the status field changing, the webhook enqueues a `toggl_project_sync`.

   Recommend starting with the button (zero new infrastructure) and only adding the automation if the team finds the manual step annoying.

### 3. Monthly totals for payroll (nice-to-have)

**Brief quote:** *"Timer fra toggl skal også brukes til grunnlag for lønninger, overtid og undertid. For dette så trenger vi egentlig bare totale timene ila mnd."*

The brief explicitly marks this as "ting som hadde vært nice", not required. The daily rows we already write are sufficient raw material — a Notion rollup or formula can sum a month per `Ansatt`.

**Recommendation:** skip in code. After Goldbox is live with the daily rows, ask the team how they want to view monthly totals (a Notion view with month-grouping? a separate `Lønnsperiode` DB rolled up from `Timer YYYY`?). Their answer will be specific to whatever their payroll workflow needs, and we shouldn't pre-build the wrong thing.

## Goldbox first-turn-on

See [docs/misc/toggl-setup.md](toggl-setup.md) for the full step-by-step guide.

## Memory pointers

- The Ansatte DB was retired in late May 2026 — replaced by direct email match against native Notion users. Don't reintroduce a manual mapping DB; see [notion-db-shape-oppgaver-korreksjoner](../../../../../.claude/projects/c--Users-tobia-Documents-Code-gb-automations/memory/) for the parallel DB-shape simplification.
- Dev creds live on Tobias's personal Toggl + Notion (see memory `dev-on-tobias-accounts-prod-on-goldbox`). Never reuse Tobias's Toggl token in Goldbox prod.
