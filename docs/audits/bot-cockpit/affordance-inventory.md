# Affordance inventory — cockpit-v2 + deploy-form + start-stop-card

Source artifact for this audit (run-prompt §12). Produced by the inventory-pass Explore agent (transcript not stored) and then triaged against the live code by the audit author.

## Coverage method

For each clickable / keyboard-activatable surface in `Frontend/src/app/components/broker/cockpit-v2/**`, `broker-deploy-form/**`, and (formerly) `broker-start-stop-card/**` (deleted by P3-007), the inventory records: visible label, accessible name, server-side reason-code source, click handler, endpoint hit, tab/location, and any anti-patterns observed.

## Inventory by tab

### Shell (page utility row + identity strip + outer/inner tabs)

| Affordance | File:line | Endpoint | Disabled-reason source | Notes |
|---|---|---|---|---|
| Env chip ("PAPER" / "UNSAFE" / "UNKNOWN") | cockpit-shell.html:6-25 | none | `operator_surface.broker.safety_verdict` (verbatim) | **Fixed by P1-001** (was state-truthiness; now drives from verdict) |
| Outer instance tab (per instance) | cockpit-shell.html:96-122 | route navigation | none | Drives `selectedInstanceId` and route |
| Inner Status / Activity / Audit / Configuration tabs | cockpit-shell.html:332-375 | none | none | Local tab-state reducer |
| Account row toggle | cockpit-shell.html:30-37 | none | `accountAttention().isCollapsible` (server-derived) | Click+enter+role=button |
| Resume | cockpit-shell.html:227-240 | POST /api/live-instances/{id}/desired-state | `actions.resume.disabled_reason_code` → shared copy map | **Tooltip fixed by P2-002** (was raw code; now operator copy) |
| Pause | cockpit-shell.html:241-254 | POST /api/live-instances/{id}/desired-state | `actions.pause.disabled_reason_code` → shared copy map | **Tooltip fixed by P2-002** |
| Flatten and pause | cockpit-shell.html:255-268 | POST /api/live-instances/{id}/flatten-and-pause | `actions.flatten_and_pause.disabled_reason_code` → shared copy map | **Tooltip fixed by P2-002** |
| Stop (overflow menu) | cockpit-shell.html:271-288 | POST /api/live-instances/{id}/desired-state | `actions.stop.disabled_reason_code` → shared copy map | Confirmation via `window.confirm`; tooltip fixed by P2-002 |
| Resume expanded reasons | cockpit-shell.html:292-298 | none | `actions.resume.disabled_reasons[]` | Shows codes as-is in `<span>`; future pass could route through copy map |

### Status & Risk tab

| Affordance | File:line | Endpoint | Disabled-reason source | Notes |
|---|---|---|---|---|
| Gate-suggested-action button (per gate) | status-risk-tab.html:33-42 | depends on `suggested_action.kind` | `gate.suggested_action` (closed enum) | Routes invoke_capability → cockpit-shell methods |
| Passing-gates disclosure | status-risk-tab.html:54-65 | none | none | Native `<details>` |

### Activity tab

| Affordance | File:line | Endpoint | Disabled-reason source | Notes |
|---|---|---|---|---|
| Incident detail toggle | incidents-panel.html:32-42 | none | none | Local toggle |
| Open raw log | incidents-panel.html:48-50 | none | none | Opens raw traceback |
| Original-traceback disclosure | incidents-panel.html:54-57 | none | none | Native `<details>` |

### Audit tab

| Affordance | File:line | Endpoint | Disabled-reason source | Notes |
|---|---|---|---|---|
| Copy run_id | audit-tab.html:9 | clipboard | none | One of 7 copy buttons |
| Copy code_sha | audit-tab.html:19 | clipboard | none | Conditional on field |
| Copy spec_path | audit-tab.html:27 | clipboard | none | Conditional |
| Copy spec_sha256 | audit-tab.html:35 | clipboard | none | Conditional |
| Copy audit_copy_path | audit-tab.html:43 | clipboard | none | Conditional |
| Copy audit_sha256 | audit-tab.html:51 | clipboard | none | Conditional |
| Copy backtest_id | audit-tab.html:59 | clipboard | none | Conditional |
| Mark POISONED trigger | audit-tab.html:89-101 | (via shell) POST /api/live-instances/{id}/commands {verb:MARK_POISONED} | `actions.mark_poisoned.disabled_reason_code` → shared copy map | **Fixed by P2-002** (was raw code; now operator copy) |
| Mark POISONED confirm (typed-halt dialog) | typed-halt-confirm.html:42-50 | as above | local: typed input === 'HALT' | Server gate re-checked on the canonical render path (shell verifies `mark_poisoned.enabled` before opening) |

### Configuration tab

| Affordance | File:line | Endpoint | Disabled-reason source | Notes |
|---|---|---|---|---|
| Redeploy link | configuration-tab.html:115-122 | route nav | none | Pre-fills deploy form with current run provenance |

### Broker deploy form

| Affordance | File:line | Endpoint | Disabled-reason source | Notes |
|---|---|---|---|---|
| Instances back link | broker-deploy-form.html:7 | route nav | none | |
| View deployment (post-deploy) | broker-deploy-form.html:28 | route nav | none | Conditional on `deployed()` |
| Enter path manually | broker-deploy-form.html:64 | none | none | Toggles input mode |
| Deploy / Deploy & start | broker-deploy-form.html:285-287 | POST /api/live-instances [+start] | `canSubmit()` (local readiness) | Deploy form pre-creation — no `operator_surface.actions` available; consult is local |
| Deploy & start live (modal confirm) | broker-deploy-form.html:302 | as above | none | Modal confirmation |
| Cancel (modal) | broker-deploy-form.html:303 | none | none | Modal cancel |

### Typed-halt confirm dialog

| Affordance | File:line | Endpoint | Disabled-reason source | Notes |
|---|---|---|---|---|
| Cancel | typed-halt-confirm.html:34-40 | none | none | ESC also cancels |
| Mark POISONED (confirm) | typed-halt-confirm.html:42-50 | (parent) POST /api/live-instances/{id}/commands {verb:MARK_POISONED} | local: typed input | Server gate enforced by parent before opening |

## High-priority flags (from inventory triage)

See:
- `findings/P1-001-paper-chip-is-state-truthiness-not-safety-verdict.md`
- `findings/P2-002-no-frontend-copy-map-for-disabled-reason-codes.md`
- `findings/P3-007-legacy-broker-start-stop-card-orphaned.md`
- `findings/P3-008-broker-paper-run-kept-for-reference.md`

## Out of audit scope (recorded only)

- `broker-deploy-form.canSubmit()` derives from local readiness checks; the create-instance endpoint has no `operator_surface.actions` entry yet (per the action-conflict matrix in `operator_capability.py:85-89`, `MUTATION_UNRESOLVED_START` is reserved but the router-level enforcement is a follow-up). Recording this as a known gap, not a finding for this audit.
- `dispatchResume` / `dispatchPause` do not consult `actions.{resume,pause}.enabled` before calling `_setIntent`. The server endpoint re-evaluates per ADR-0010 §A3 (the canonical resolver runs again immediately before the durable write), so this is defense-in-depth asymmetry, not a safety hole. Logged in this inventory but not opened as a finding.
- The expanded reasons block in `cockpit-shell.html:292-298` only exists for Resume. Pause / Stop / Flatten / Mark POISONED don't show their multi-reason list expanded. Cosmetic; deferred.
- `dispatchStop` uses `window.confirm` rather than the typed-halt-confirm dialog. Consistency gap (the existing dialog is reserved for Mark POISONED's typed-HALT acknowledgment); deferred.
