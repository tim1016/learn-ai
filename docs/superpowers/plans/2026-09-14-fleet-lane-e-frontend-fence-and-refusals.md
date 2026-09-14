# Lane E: frontend fence and refusal vocabulary (#2068, #2067, #2076 frontend) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Four PRs. Freeze the binding-generation fence at action open on every command surface and surface drift as a conflict (#2068). Give the fleet's refusal reason codes a committed snapshot, backfill `next_step` on the families that have none, and wrap the bare-detail 403/503 refusals into the flat body (#2067 backend). Teach the frontend the vocabulary and render `authority_state` (#2067 frontend, #2076). Derive the clerk-scoped URL builders from the operation catalog and delete the dead `listOrderGroups` (#2076 frontend).

**Architecture:** See the lane report below; every task carries its own files, failing test, implementation and commit.

**Tech Stack:** Python 3.12 / FastAPI / pytest (PythonDataService), Angular 22 / Vitest + Angular Testing Library (Frontend), Podman Compose, GitHub Actions.

**Spec:** GitHub issues #2068, #2067, #2076 (frontend half), plus `docs/broker-clerk-fleet-authority.md` §6 (trust register). Master sequencing, owner decisions, deploy windows and the delivery protocol live in `2026-09-14-fleet-trust-register-fixes.md`, and the current state of the remaining ten issues lives in `2026-09-14-fleet-trust-register-closeout.md` — read both first; the master's Global Constraints apply to every task here.

## Global Constraints

Inherited verbatim from `2026-09-14-fleet-trust-register-fixes.md` § Global Constraints. In particular:

- **Never `git checkout` / `switch` / `stash` in `/Users/inkant/learn-ai`.** Both live clerks bind-mount `./PythonDataService/app` from that checkout. Feature work happens in a worktree under `/Users/inkant/learn-ai-worktrees/` (Podman cannot see `/tmp`).
- **Never disable `IBKR_BROKER_ENABLED`.** The IB Gateway is the live decision-bar source for the Alpaca lanes.
- Every `./restart.sh` is an unconditional `podman compose down`; deploy only in a no-bot window.
- Python: `ruff check PythonDataService/app/ PythonDataService/tests/` at project scope; no `print()`; no silent excepts; structured logging with `extra={"action": ...}`.
- Frontend: scoped runs use `npx ng test --watch=false --include='src/app/path/exact.spec.ts'` — **never a directory glob** — and a **full** `npx ng test --watch=false` is required before every frontend push, because parent specs pin child component copy.
- Angular 22 only: signals, `input()`/`output()` functions, `inject()`, `ChangeDetectionStrategy.OnPush`, standalone (never write `standalone: true`), `@if`/`@for` with `track`, class/style bindings never `ngClass`/`ngStyle`, no `mutate()`, prefer `resource()`/`rxResource()`.
- Raw backend identifiers in receipt/evidence UI (`reason_code`, `gate_id`, `source`, code-like receipt values) render through the shared `receiptLabel` pipe. Opaque audit tokens (intent/order IDs, paths, hashes, refs, URLs) are preserved exactly. Backend-authored trader/operator prose is **never** piped.
- Time is `int64 ms UTC` on every wire and stored field; new timestamp fields are named `*_ms`; display goes through the shared timestamp display component with an explicit mode.
- Any Pydantic schema or route change regenerates `contracts/openapi/python-data-service.openapi.json` (`python scripts/export_openapi_contract.py`) and `Frontend/src/app/api/broker.types.ts` (`npm run codegen:openapi` in `Frontend/`) in the same commit. **Two PRs that both regenerate must be sequential.**
- Docs edits must pass `DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest tests/contracts -q`.
- `tests/broker/fleet` binds sockets and **FAILS** under the Bash sandbox — run those with `dangerouslyDisableSandbox: true`.

## Corrections applied by the integrating session (do not re-derive)

- **#2068's freeze-at-open is a correctness requirement, not UX.** The backend fence can only refuse a stale generation the *client sends*; a click-time read always sends the current one. The bug is dormant only because `refresh()` has no caller. **No task may add a `refresh()` caller before the freeze lands** — Task 6 is deliberately last.
- **#2068's cold-load retry half is already fixed and merged** (#2081, `fleet-directory.service.ts:69-93`).
- **#2067's parser half is already fixed and merged** (#2081, `Frontend/src/app/shared/errors/refusal-body.ts`). What remains is the vocabulary, the `next_step` backfill, and wrapping the bare-detail 403/503 responses.
- **Decision 9 (#2067): do NOT export refusal reasons through OpenAPI.** Use committed-snapshot parity, following the `test_vocabulary_snapshot.py` pattern already in this repo.
- **Decision 10 (#2068):** freeze the binding generation at open everywhere, one shared helper, and surface drift as a conflict.
- **Decision 13 (#2076d):** DELETE the dead `listOrderGroups`. Task 11 bridges `order-groups` only if the owner flips that decision; it is written to stand if flipped and is marked conditional.
- **#2076's coordinator heartbeat warning is already fixed and merged** (#2081). Lane D owns the seven backend cleanups; this lane owns only the frontend ones.

---

*This report was produced by an Opus planning agent on 2026-09-14 against `origin/master` `cec92fcf` (= main-checkout HEAD, deployed 12:17 CDT). Every `file:line` claim below was verified against that tree by that agent, by opening the file and reading the line. Counts were measured, not repeated from the issues.*

---

## 0. Baseline

| Fact | Measured value |
|---|---|
| `origin/master` | `cec92fcf` |
| **Frontend suite** (`podman exec my-frontend npx ng test --watch=false`) | **281 test files, 2545 tests, 281/281 files passed, 2545/2545 tests passed, 20.74 s** |
| **Python fleet + contracts** (`DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest tests/broker/fleet tests/contracts -q`, unsandboxed) | **342 passed, 0 failed, 86.83 s** |
| `tests/broker/v2panel` on the host venv | **5 pre-existing failures**, all `CatalogUnavailableError: POSTGRES_URL is empty` (`app/data_lake/catalog_client.py:73`), raised from `tests/broker/v2panel/test_panel_router.py:488`. Failing tests: `test_reads_of_a_stopped_bot_never_invoke_the_broker_port[1]`, `[50]`, `test_repeated_reads_never_advance_the_control_revision[1]`, `[50]`, `test_shadow_operator_surfaces.py::test_the_deploy_view_is_reachable_over_http_and_offers_shadow`. **Inherited, environment-only, not Lane E's.** Surface them in the PR body if a run includes that directory. |
| Catalog size | **77** operations, all `alpaca` (`app/broker/alpaca/clerk/fleet_adapter.py`, enumerated via `production_provider_adapters()`) |
| Frontend `laneUrl`/`accountUrl` call sites (non-spec, excluding the builder itself) | **44** — `brokers.service.ts` 22, `broker-v2-panel.service.ts` 19, `gallery-live-store.service.ts` 2, `broker-configuration.service.ts` 1 |
| Frontend HTTP calls those 44 sites actually address | **61** — 24 / 19 / 1 / 17. `broker-configuration.service.ts` makes **17** HTTP calls from **one** `laneUrl` call (`:56-57` builds a prefix; `:71,80,85,95,105,209,217,224,235,250` concatenate onto it) |
| Spec files providing the fleet directory double | **28** (`grep -rl provideFleetDirectory src/` = 29 including the seam file itself) |

The master plan's "Baseline on master today: 315 passed (325 with #2081)" and the close-out plan's "Target: 321 passed" are both **stale**; the measured number today is **342**.

---

## 1. Verified facts

### 1.1 #2068 — the fence

| # | Claim | Verdict | Evidence (verified at `cec92fcf`) |
|---|---|---|---|
| 1 | The fence is a live directory read at click time on the panel | **CONFIRMED** | `Frontend/src/app/components/broker/v2-panel/panel-shell/bot-panel-shell.component.ts:135-143` — `target = computed(() => { const lane = this.fleetDirectory.lane(...); return resourceTarget(..., { bindingGeneration: lane?.effective_binding_generation ?? null, routingEpoch: lane?.routing_epoch ?? null }); })`. Read at click at **`:294`** (`const target = this.target();` inside `onActionRequested`), minted at **`:378-380`** (`withCommand(target, 'bot_action', crypto.randomUUID())`). |
| 2 | …and on the roster | **CONFIRMED** | `bots-list-page.component.ts:99-108` (same computed shape), read at click at **`:353`** (`withEntity(this.target(), sid)`), minted at **`:354`**. The comment at `:350-352` says "Freeze the lane before any await" — true of the *await*, false of the *open*. |
| 3 | It is genuinely frozen-at-open only on the Deploy drawer | **CONFIRMED** | `alpaca-deploy-drawer.component.ts:44` `frozenTarget = signal<ResourceTarget \| null>(null)`; `:96-102` an effect that sets it **only on the invisible→visible edge** (`if (visible && !this.wasVisible)`) and nulls it on close. `alpaca-deploy-workflow.component.ts:148` takes it as `input.required<ResourceTarget>()` with the comment "Frozen when the drawer opens; never reconstructed from the directory." |
| 4 | `FleetDirectoryService` loads once per session | **CONFIRMED** | `fleet-directory.service.ts:42-47` — the constructor calls `void this.load()`; `load()` (`:96-116`) is guarded by `this.inFlight`. |
| 5 | `refresh()` has no caller | **CONFIRMED** | `refresh()` at `fleet-directory.service.ts:65-67`. `grep -rn "fleetDirectory\.refresh\|fleet\.refresh\|directory\.refresh" src/` → **zero hits**, spec files included. |
| 6 | A failed cold load rejects forever | **FIXED, no longer true** | `ensureLoaded()` (`:81-93`) now retries past a 3 s `RETRY_COOLDOWN_MS` (`:33`). This half shipped in #2081. |
| 7 | The backend fence | **CONFIRMED, two checks, both null-gated** | `app/broker/fleet/service.py:1283-1292` (`resolve_route`) and `app/broker/fleet/routing.py:328-343` (the configuration-access path). Both begin `expected_binding_generation is not None and …`. The refusal is `ClerkBindingGenerationConflict`, `reason="clerk_binding_generation_conflict"`, **409** (`app/broker/fleet/errors.py:121-125`). |
| 8 | The client omits the field when the generation is null | **CONFIRMED** | `Frontend/src/app/fleet/resource-target.ts:139-141` — `if (target.bindingGeneration !== null) { context.expected_effective_binding_generation = target.bindingGeneration; }`. The envelope parser accepts its absence (`routing.py:103-110`, `generation is not None and …`). |

### 1.2 #2067 — the vocabulary (all counts measured by AST walk over `PythonDataService/app/`)

| # | Fact | Measured |
|---|---|---|
| 9 | Classes declared in `app/broker/fleet/errors.py` | **22** (`grep -c "^class "`): `FleetControlError` at `:24` (base, `reason="fleet_control_error"`, 409) + 21 subclasses at `:44,51,58,65,72,79,86,93,100,107,114,121,128,135,142,151,158,171,183,195,208` |
| 10 | `FleetControlError` subclasses defined **outside** `errors.py` | **3** — `FleetPresenceError` (`app/broker/fleet/presence.py:51`, `fleet_presence_unavailable`, 503); `ConfirmationEvidenceError` (`app/broker/fleet/confirmation.py:38`, `confirmation_evidence_invalid`, 409); `FleetBootRefused` (`app/broker/alpaca/clerk/fleet_boot.py:64`, `fleet_boot_refused`, 409). Subclass closure size = **25**. |
| 11 | Reason codes minted outside the closure | **5 net-new** — `fleet_lane_capacity_exhausted` (`app/broker/fleet/lane_runtime.py:43,46`, a class that subclasses **`Exception`**, not `FleetControlError`); `compatibility_retirement_state_invalid` (`lane_runtime.py:545`, 503); `compatibility_read_retired` (`lane_runtime.py:573`, 410); `command_envelope_invalid` (`app/routers/broker_clerks.py:87`, 422); `qualification_market_status_unavailable` (`app/routers/fleet_qualification.py:210,219`, 503). Plus one **duplicate** mint: `clerk_identity_mismatch` is re-minted by string concatenation at `app/broker/fleet/agent_identity.py:236`. |
| 12 | **Distinct reason codes** | **30** (25 closure + 5 net-new) |
| 13 | **Raise sites** for the 25 closure classes | **187** — counted by `ast.Raise` whose `.exc` is a `Call` resolving to a closure class. Concentration: `service.py` 62, `recovery.py` 41, `routing.py` 14, `store.py` 12, `volume.py` 12, `fleet_boot.py` 10, `presence.py` 10, `confirmation.py` 9, `provider.py` 3, `broker_clerks.py` 2, `main.py` 1. |
| 14 | Raise sites carrying `next_step=` | **90 (48.1 %)**. Without: **97 (51.9 %)**. |
| 15 | **Zero-coverage families** | **6**, covering **31** raise sites: `ClerkUnreachable` 13 (`routing.py:215,220,280,285,702,709`; `service.py:893,1225,1230,1234,1253,1264,1276`), `ClerkNotFound` 7 (`service.py:459,554,669,734,1223,1649,1652`), `ClerkBrokerMismatch` 4 (`service.py:728,872,1096,1218`), `BrokerAndClerkRequired` 3 (`service.py:221,725,1213`), `ClerkAccountMismatch` 2 (`service.py:745,1297`), `FleetControlError` raised directly 2 (`presence.py:276,316`). |
| 16 | The flat body builder | `FleetControlError.detail()` at `errors.py:36-41` — `{"reason", "message"}` plus `"next_step"` **only when not None** (`:39-40`). |
| 17 | The flat response **writer** | `app/routers/broker_clerks.py:78-80` — `JSONResponse(status_code=error.status_code, content=error.detail())`. |
| 18 | Bare-detail 403/503 on the fleet surface | **9** — `app/security/data_plane_control.py:66-68` (503), `:73-75` (503), `:79-81` (403); `app/routers/broker_clerks.py:58-61` (503), `:71-74` (503); `app/routers/internal_fleet.py:84` (503), `:95` (503), `:97` (503), `:108` (403). All `HTTPException(detail="<bare string>")`. |
| 19 | Hand-built refusal bodies bypassing `detail()` | **4** — `lane_runtime.py:543-556` (503, `{reason,message}`, no `next_step`), `:571-584` (410, same), `:605-620` (503, full three keys but hand-assembled), `agent_identity.py:234-253` (409, built by **string concatenation** of JSON). |
| 20 | The existing snapshot-parity pattern | `PythonDataService/tests/broker/v2panel/test_vocabulary_snapshot.py` (paths at `:44-61`; freshness assertion at `:91-107`; byte-identity at `:71-88`; exact-copy at `:110-119`). Generator `PythonDataService/scripts/regenerate_broker_v2_vocabulary_snapshot.py` exposing `build_snapshot()` (`:86-104`) and writing two copies (`:114-119`). CI job `broker-v2-vocabulary-contract` at `.github/workflows/ci.yml:203-236`, regenerate + `git diff --exit-code` (`:223-225`) + `git status --porcelain` (`:229-236`). Frontend half: `Frontend/src/app/components/broker/v2-panel/lib/broker-v2-copy-contract.spec.ts` reading `./broker-v2-vocabulary.snapshot.json` (`:29`) against `BROKER_V2_EMERGENCY_COPY` (`:30`). |

### 1.3 #2076 — the three frontend bullets

| Bullet | Verdict | Evidence |
|---|---|---|
| `getLiveVerdict` stays a lane-implicit shell poll | **CONFIRMED** | `Frontend/src/app/services/brokers.service.ts:129-134` — `this.polls.get(\`${this.base}/alpaca/live-verdict\`)`, hardcoded `alpaca`, no clerk. Consumers: `alpaca-live-verdict.service.ts:47` → `app.component.ts:9` / `shell/alpaca-live-banner.component.ts:3`. Backend mount is coordinator-only (`app/routers/fleet_compatibility_reads.py`). |
| `authority_state` carried and never rendered | **CONFIRMED** | Typed at `Frontend/src/app/fleet/fleet-directory.types.ts:14`; fixture value `'real_paper'` at `fleet-directory-testing.ts:38`. `grep -rn "authority_state\|authorityState" src/` outside `broker.types.ts` returns **only those two lines** — zero template or component read. |
| Path builders don't derive from the catalog | **CONFIRMED** | `Frontend/src/app/fleet/clerk-scoped-url.ts` — `type UrlSuffix = '' \| \`/${string}\`` and both `laneUrl` / `accountUrl` append it verbatim. No contract test ties a suffix to a declared operation. |
| `GET .../order-groups` is stranded | **CONFIRMED, and the caller is dead** | `brokers.service.ts:187-201` `listOrderGroups`. `grep -rn listOrderGroups src/` → the definition plus **one spec** (`brokers.service.spec.ts:103,107`). **Zero production callers.** |

### 1.4 Facts none of the three issues contained

- **A cold or failed directory fails OPEN, not closed.** `lane?.effective_binding_generation ?? null` (`bot-panel-shell.component.ts:140`, `bots-list-page.component.ts:103-105`, `bot-gallery-page.component.ts:63-66`) yields `null`; `commandContextOf` then omits `expected_effective_binding_generation` entirely (`resource-target.ts:139-141`); and **both** backend fence checks are `is not None`-gated (`service.py:1283-1284`, `routing.py:328-329`). The command routes with **no generation fence at all**. #2068's "it fails closed, but silently" is the exact opposite of what the code does.
- **There are six command-target origins reading the directory, not two.** Enumerated by `grep -rn "withCommand(" src/app/ | grep -v spec` (13 hits) traced back to their target source:
  1. `bot-panel-shell.component.ts:135-143` → live read at click (`:294`) — **unfrozen**
  2. `bots-list-page.component.ts:99-108` → live read at click (`:353`) — **unfrozen**
  3. `bot-gallery-page.component.ts:59-67` → `galleryTarget` is a **function**, evaluated at click (`:118`) — **unfrozen**. The issue does not name this surface at all.
  4. `cohort-archive-drawer.component.ts:101-110` → frozen on open (`:117-135`) but **silently re-mints** on drift (`:130-134` calls `clearDestructiveState()` and a fresh `crypto.randomUUID()`)
  5. `alpaca-deploy-workflow.component.ts:846-856` → frozen, but the effect at `:586-593` **silently nulls** `frozenCommand` on route-key drift
  6. `alpaca-desk-account-data.service.ts:21-32` → a page-level computed passed as `input()` into four command components (`alpaca-order-entry.component.ts:64`, `alpaca-sqlite-custody.component.ts:94`, `account-desk-transaction-history.component.ts:128`, `deploy-paper-access.component.ts:52`), each of which reads `this.target()` at interaction time (`:379`, `:353`, `:241`, `:112`)
- **`provideFleetDirectory` is a fake that cannot change.** `Frontend/src/app/fleet/fleet-directory-testing.ts:45-73` closes over one fixed `response`; `lane()` (`:58-61`) resolves against it and `refresh: () => undefined` (`:69`) is a no-op. **No freeze test written against this double can fail**, because the double cannot rebind. Repairing the double is a precondition for a falsifiable Task 3/4/5, not an optional nicety.
- **Only one nested-only parser remains, and `toOperationError` is dead.** #2067 says "all three frontend parsers read `error.error.detail.*`". Two were migrated to `refusalBody` by #2081 (`panel-action-outcome.ts:2,23`; `broker-configuration-refusal.ts:15,54`). The third, `Frontend/src/app/components/broker/operation-error.ts`, was **not** — `extractServerMessage` at `:109-114` still reads `error['error']['detail']` only. It has exactly **one** production importer (`account-desk-transaction-history-store.service.ts:11`, used at `:137`), and that path is clerk-scoped (`laneKey(...)` at `:173-177`). `toOperationError` (`:477-536`) has **zero** production callers — `grep -rn toOperationError src/` returns the definition plus 13 spec references only.
- **Every fleet 409 renders as "Unknown".** `deriveActionRejection` (`panel-action-outcome.ts:33-61`) takes `outcome` from `detail['outcome']` — a field the panel's own `PanelActionErrorResponse` carries and the fleet's flat `{reason, message, next_step}` body does not. So `clerk_binding_generation_conflict` (409) falls to `'unknown'` at `:39-40`, and `actionOutcomeToast` (`:72-83`) renders `severity: 'error'`, `summary: formatReceiptLabel('unknown')` = **"Unknown"**. The fence fires correctly on the wire and reads as an unexplained error in the browser.
- **The same exception type has two wire shapes.** `app/routers/broker_clerks.py:78-80` returns `error.detail()` **flat**; `app/routers/internal_fleet.py:111-113` returns `HTTPException(detail=control_error.detail())`, which FastAPI serialises **nested** as `{"detail": {...}}`. `refusalBody` survives this only because #2081 taught it both shapes.
- **`authority_state` is not a closed vocabulary.** `app/broker/fleet/records.py:139-141` accepts any bounded snake-case token (`_AUTHORITY_STATE_PATTERN`). The only producer today is `app/main.py:607-612`, emitting exactly five values: `real_paper`, `real_live`, `shadow`, `synthetic`, `unavailable`. A frontend closed union would be wrong; render through `receiptLabel` with an open-token fallback.
- **`broker-configuration.service.ts` cannot be migrated by a per-operation builder without rewriting its prefix pattern.** One `laneUrl` call at `:56-57` produces a base that ten sites concatenate onto (`:71,80,85,95,105,209,217,224,235,250`), driving 17 HTTP calls. A strict `operationUrl(operationId, params)` builder has no expression for "the prefix".
- **The catalog declares no `/configuration` operation.** The 77 operations include `/configuration/profiles`, `/configuration/owner`, `/configuration/selection` and so on — never a bare `/configuration`. The prefix `broker-configuration.service.ts:57` builds is not itself an operation path.
- **One catalog path carries a Starlette converter.** `manual_order_cancel` declares `/accounts/{account_id}/manual-orders/{order_ref:path}/cancel`. A generated TypeScript builder must not URL-encode a `:path` segment's slashes; `pathIdentity` in `clerk-scoped-url.ts:6-11` calls `encodeURIComponent` unconditionally.
- **`alpaca-deploy-workflow.component.ts` is 957 lines**, inside the thermo-nuclear review's ~1k file-size alarm. Task 5 must not grow it; extract if it needs more than a handful of lines.

---

## 2. Corrections to the issue text and the master plan

| Source | Claim | Truth |
|---|---|---|
| #2068 | "a boot blip disables every execution command for the whole session (**it fails closed**, but silently)" | It fails **open**. A null generation omits the envelope field and both backend checks are `is not None`-gated (`service.py:1283-1284`, `routing.py:328-329`). Commands dispatch **unfenced**. |
| #2068 | Two affected surfaces (panel, roster) | **Three** unfrozen (panel, roster, **gallery** — `bot-gallery-page.component.ts:59-67,118`), plus two frozen-but-silently-re-minting drawers, plus one shared service origin feeding four more command components. Six origins total. |
| #2067 | "**28** refusal reason codes exist in the backend" | **30** distinct codes. |
| #2067 | "48 % of refusal sites (**85/176**) carry no `next_step`" | **97 of 187** (51.9 %) carry none. |
| #2067 | "`clerk_unreachable` (13 raise sites, zero)" | **CONFIRMED exactly** — 13 sites, zero `next_step`. The only claim in #2067 whose numbers survived. |
| #2067 | "**All three** frontend parsers read `error.error.detail.*`" | Two were fixed by #2081. The third (`operation-error.ts:109-114`) remains, and its other half (`toOperationError`) is dead code with no production caller. |
| #2067 | "Auth/infra refusals … return a bare `{"detail": …}`" | **CONFIRMED and enumerated: 9 sites** (§1.2 row 18), plus **4 hand-built** bodies that bypass `detail()` entirely (row 19), one of which builds JSON by string concatenation. |
| Master §"What changed", item 5 | "The closed refusal set is **29** codes (22 declared, 3 minted inline, 4 more subclasses outside `errors.py`)" | **30** codes. The shape is 22 declared in `errors.py` + **3** subclasses outside it + **5** net-new inline mints. There are three, not four, `FleetControlError` subclasses outside `errors.py`; the fourth family (`FleetLaneCapacityExhausted`) is outside the closure because it subclasses `Exception`. |
| Master §"What changed", item 5 | "the `next_step` gap is **95 of 184** sites across **five** zero-coverage families" | **97 of 187** across **six** zero-coverage families (31 raise sites). The sixth is `FleetControlError` raised directly at `presence.py:276,316`. |
| Master PR table, E-D | "44 call-site migration" | 44 is the count of `laneUrl`/`accountUrl` **invocations**; those address **61** HTTP calls, 17 of them through one prefix builder. |
| Close-out plan §3 | "Target: `pytest tests/broker/fleet tests/contracts -q` → **321 passed**" | **342 passed** today on `cec92fcf`. |
| Master §Verification | "Baseline on master today: 315 passed, 0 failed (325 with #2081)" | Stale; 342. |

---

## 3. Task breakdown

Environment for every Python command: `cd /Users/inkant/learn-ai-worktrees/<branch>/PythonDataService`. Frontend commands run through the container: `podman exec my-frontend npx ng test …` for the main checkout, or `npx ng test` inside the worktree's `Frontend/`. Tests under `tests/broker/fleet/` bind sockets — run with `dangerouslyDisableSandbox: true`.

---

### Task 1 — Make the fleet directory test double able to rebind

**Why this is first.** `provideFleetDirectory` (`fleet-directory-testing.ts:45-73`) closes over one immutable `response`. A test that asserts "the frozen generation survives a rebinding" cannot fail against it, because the double cannot rebind. Shipping Tasks 3–5 against the current double would produce three green tests that prove nothing — the exact shape the `fakes that collapse facts` note warns about, and the shape Lane G spent a whole PR undoing.

**Files**
- modify `Frontend/src/app/fleet/fleet-directory-testing.ts`
- create `Frontend/src/app/fleet/fleet-directory-testing.spec.ts`

**Failing regression test** — new file:

```ts
import { describe, expect, it } from 'vitest';

import { provideFleetDirectory, testLane, TEST_CLERK_ID } from './fleet-directory-testing';
import type { FleetDirectoryService } from './fleet-directory.service';

/** The double is evidence for the binding-generation fence, so it must be able
 * to do the one thing the fence exists to survive: rebind mid-session. A double
 * that cannot rebind makes every freeze-at-open test unfalsifiable. */
describe('the fleet directory double', () => {
  it('reports a rebinding after the directory is replaced', () => {
    const directory = provideFleetDirectory();
    const service = directory.useValue as unknown as FleetDirectoryService;

    expect(service.lane('alpaca', TEST_CLERK_ID)?.effective_binding_generation).toBe(3);

    directory.rebind({
      observed_at_ms: 1_757_000_000_001,
      clerks: [testLane({ effective_binding_generation: 4, routing_epoch: 5 })],
    });

    expect(service.lane('alpaca', TEST_CLERK_ID)?.effective_binding_generation).toBe(4);
    expect(service.lane('alpaca', TEST_CLERK_ID)?.routing_epoch).toBe(5);
    expect(service.value()?.observed_at_ms).toBe(1_757_000_000_001);
  });

  it('keeps the default single-ready-lane response for callers that never rebind', () => {
    const service = provideFleetDirectory().useValue as unknown as FleetDirectoryService;
    expect(service.lanesOf('alpaca')).toHaveLength(1);
    expect(service.lanesOf('tradier')).toHaveLength(0);
  });
});
```

**Run / expected failure**
```
podman exec my-frontend npx ng test --watch=false --include='src/app/fleet/fleet-directory-testing.spec.ts'
```
Expected: `TypeError: directory.rebind is not a function` on the first test. The second test passes immediately (it pins the unchanged default so the refactor cannot silently alter the 28 existing consumers).

**Minimal implementation** — in `fleet-directory-testing.ts`, replace the closed-over constant with a mutable holder and widen the return type. The existing call signature (`provideFleetDirectory()` and `provideFleetDirectory(response)`) is unchanged, so **none of the 28 consumer specs need editing**:

```ts
export interface FleetDirectoryDouble {
  readonly provide: typeof FleetDirectoryService;
  readonly useValue: Partial<FleetDirectoryService>;
  /** Replace the served directory, as a coordinator rebinding would. */
  rebind(next: FleetDirectoryResponse): void;
}

export function provideFleetDirectory(
  initial: FleetDirectoryResponse = { observed_at_ms: 1_757_000_000_000, clerks: [testLane()] },
): FleetDirectoryDouble {
  let response = initial;
  return {
    provide: FleetDirectoryService,
    useValue: {
      value: () => response,
      error: () => undefined,
      isLoading: () => false,
      lanesOf: (broker: string) => response.clerks.filter((lane) => lane.broker === broker),
      lane: (broker: string, clerkId: string) => /* unchanged body, reading `response` */,
      laneForAccount: (broker: string, accountId: string) => /* unchanged body */,
      refresh: () => Promise.resolve(response),
      ensureLoaded: () => Promise.resolve(response),
    } as Partial<FleetDirectoryService>,
    rebind(next: FleetDirectoryResponse) { response = next; },
  };
}
```

`value()` must read the live `response`, not the captured `initial` — that is the whole point. Note `refresh` changes from `() => undefined` to a resolved promise so it matches the real signature (`fleet-directory.service.ts:65-67`); the current stub returns the wrong type and would let a `refresh().then(...)` caller crash only in production.

**Run / expected pass**
```
podman exec my-frontend npx ng test --watch=false --include='src/app/fleet/fleet-directory-testing.spec.ts'
podman exec my-frontend npx ng test --watch=false
```
Expected: the new spec passes; the full suite stays at **281 files / 2545 + 2 = 2547 tests**, 0 failures. If any of the 28 consumers breaks, the signature change was not backwards compatible — fix that rather than editing the consumer.

**Commit**
```
test(fleet): let the directory double rebind, so the fence is falsifiable

provideFleetDirectory closed over one immutable response, so `lane()` could
never report a different binding generation than it did at provision time.
Every freeze-at-open test written against it would have passed whether or not
the component froze anything.

The double now holds the response in a rebindable slot and exposes rebind().
The existing call signature is unchanged; all 28 consumer specs are untouched.
`refresh` now returns a resolved promise, matching the real service.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
```

---

### Task 2 — `laneFence`: one shared helper for freezing and for detecting drift

**Where it lives and why not `resource-target.ts`.** The helper needs `LaneDescriptor`, which lives in `fleet-directory.types.ts` — and `fleet-directory.types.ts` already imports `FleetCapability` **from** `resource-target.ts` (`fleet-directory.types.ts:1`). Putting `laneFence` in `resource-target.ts` creates an import cycle. New file `Frontend/src/app/fleet/lane-fence.ts`, which may import from both.

**Files**
- create `Frontend/src/app/fleet/lane-fence.ts`
- create `Frontend/src/app/fleet/lane-fence.spec.ts`

**Failing regression test** — new file:

```ts
import { describe, expect, it } from 'vitest';

import { freezeLaneFence, laneFenceDrifted, type LaneFence } from './lane-fence';
import { testLane } from './fleet-directory-testing';

describe('laneFence', () => {
  it('freezes the generation and epoch the operator was shown', () => {
    const fence = freezeLaneFence(testLane({ effective_binding_generation: 3, routing_epoch: 4 }));
    expect(fence).toEqual({ bindingGeneration: 3, routingEpoch: 4 });
    expect(Object.isFrozen(fence)).toBe(true);
  });

  it('reports drift when the lane rebinds under a frozen fence', () => {
    const frozen = freezeLaneFence(testLane({ effective_binding_generation: 3, routing_epoch: 4 }));
    expect(laneFenceDrifted(frozen, testLane({ effective_binding_generation: 3, routing_epoch: 4 }))).toBe(false);
    expect(laneFenceDrifted(frozen, testLane({ effective_binding_generation: 4, routing_epoch: 4 }))).toBe(true);
    expect(laneFenceDrifted(frozen, testLane({ effective_binding_generation: 3, routing_epoch: 5 }))).toBe(true);
  });

  it('treats a lane that vanished from the directory as drift', () => {
    const frozen = freezeLaneFence(testLane({ effective_binding_generation: 3, routing_epoch: 4 }));
    expect(laneFenceDrifted(frozen, undefined)).toBe(true);
  });

  it('does not call an unfenced command frozen', () => {
    // A cold directory yields nulls. The backend fence is `is not None`-gated
    // on both paths, so a null generation dispatches UNFENCED — the caller must
    // be able to tell that apart from a real fence.
    const fence = freezeLaneFence(undefined);
    expect(fence).toEqual({ bindingGeneration: null, routingEpoch: null });
    expect(laneFenceIsEnforceable(fence)).toBe(false);
    expect(laneFenceIsEnforceable({ bindingGeneration: 3, routingEpoch: 4 } as LaneFence)).toBe(true);
  });
});
```

(The last test also imports `laneFenceIsEnforceable`; add it to the import list.)

**Run / expected failure**
```
podman exec my-frontend npx ng test --watch=false --include='src/app/fleet/lane-fence.spec.ts'
```
Expected: the whole file fails to resolve — `Failed to resolve import "./lane-fence"`.

**Minimal implementation** — `Frontend/src/app/fleet/lane-fence.ts`:

```ts
/** The binding-generation fence, frozen at action open (#2068, decision 10).
 *
 * The coordinator can only refuse a generation the client *sends*
 * (`service.py:1283-1292`, `routing.py:328-343` — both `is not None`-gated). A
 * click-time read always sends the current one, so the fence is structurally
 * unable to fire: it protects nothing unless the value is captured when the
 * operator was shown the lane and carried unchanged to submission.
 *
 * A null fence is not a safe default. `commandContextOf` omits
 * `expected_effective_binding_generation` when the generation is null
 * (`resource-target.ts:139-141`), so an unfenced command routes with no
 * generation check at all. `laneFenceIsEnforceable` names that state so a
 * surface can refuse rather than dispatch blind.
 */

import type { LaneDescriptor } from './fleet-directory.types';
import type { ResourceTarget } from './resource-target';

export interface LaneFence {
  readonly bindingGeneration: number | null;
  readonly routingEpoch: number | null;
}

/** Capture what the rendered lane said, at the moment it was rendered. */
export function freezeLaneFence(lane: LaneDescriptor | undefined): LaneFence {
  return Object.freeze({
    bindingGeneration: lane?.effective_binding_generation ?? null,
    routingEpoch: lane?.routing_epoch ?? null,
  });
}

/** Whether this fence can actually be enforced by the coordinator. */
export function laneFenceIsEnforceable(fence: LaneFence): boolean {
  return fence.bindingGeneration !== null;
}

/** Whether the lane has moved out from under a frozen fence. A lane that
 * vanished from the directory counts as drift: it cannot be proven unchanged. */
export function laneFenceDrifted(frozen: LaneFence, lane: LaneDescriptor | undefined): boolean {
  if (lane === undefined) return true;
  const current = freezeLaneFence(lane);
  return (
    current.bindingGeneration !== frozen.bindingGeneration ||
    current.routingEpoch !== frozen.routingEpoch
  );
}

/** The one sentence a surface shows when a frozen command met a rebound lane. */
export const LANE_FENCE_CONFLICT_MESSAGE =
  'This clerk lane was rebound while the action was open, so the command was not sent. ' +
  'Reopen the action to reissue it against the lane as it stands now.';

/** Whether a target still matches the fence it was frozen against. */
export function targetMatchesFence(target: ResourceTarget, fence: LaneFence): boolean {
  return (
    target.bindingGeneration === fence.bindingGeneration &&
    target.routingEpoch === fence.routingEpoch
  );
}
```

`LANE_FENCE_CONFLICT_MESSAGE` is client-authored prose about the *request*, not about the trading domain — the same carve-out `broker-configuration-refusal.ts:40-45` documents for `clientRefusal`. It is not piped through `receiptLabel`.

**Run / expected pass**
```
podman exec my-frontend npx ng test --watch=false --include='src/app/fleet/lane-fence.spec.ts'
npx eslint Frontend/src/ --max-warnings 0
```
Expected: 4 tests pass.

**Commit**
```
feat(fleet): one shared lane fence, frozen at open (#2068)

The coordinator can only refuse a binding generation the client sends, and
both fence checks are `is not None`-gated (service.py:1283-1292,
routing.py:328-343). A click-time directory read always sends the current
generation, so the fence could never fire — and a cold directory sends none at
all, dispatching UNFENCED rather than failing closed as the issue assumed.

freezeLaneFence captures what the operator was shown; laneFenceDrifted says
whether the lane moved; laneFenceIsEnforceable names the unfenced state so a
surface can refuse instead of dispatching blind.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
```

---

### Task 3 — The bot panel freezes its fence at action open

**Files**
- modify `Frontend/src/app/components/broker/v2-panel/panel-shell/bot-panel-shell.component.ts`
- modify `Frontend/src/app/components/broker/v2-panel/panel-shell/bot-panel-shell.component.spec.ts`

**Failing regression test** — append to `bot-panel-shell.component.spec.ts`, using the rebindable double from Task 1:

```ts
it('refuses a panel action whose lane rebound while the action was open', async () => {
  const directory = provideFleetDirectory();
  const runBotAction = vi.fn().mockResolvedValue(fakeActionResult());
  await renderShell({ directory, runBotAction });

  // The operator is shown generation 3, then the coordinator rebinds to 4
  // before they press the button — exactly what refresh() will start doing.
  directory.rebind({
    observed_at_ms: 1_757_000_000_001,
    clerks: [testLane({ effective_binding_generation: 4 })],
  });

  await userEvent.click(screen.getByRole('button', { name: /stop/i }));

  expect(runBotAction).not.toHaveBeenCalled();
  expect(await screen.findByText(/rebound while the action was open/i)).toBeVisible();
});

it('sends the generation the operator was shown, not the one current at click', async () => {
  const directory = provideFleetDirectory();
  const runBotAction = vi.fn().mockResolvedValue(fakeActionResult());
  await renderShell({ directory, runBotAction });

  await userEvent.click(screen.getByRole('button', { name: /stop/i }));

  expect(runBotAction).toHaveBeenCalledWith(
    expect.objectContaining({ bindingGeneration: 3, routingEpoch: 4 }),
    expect.anything(), expect.anything(), expect.anything(),
  );
});
```

`renderShell` is the file's existing render helper; give it a `directory` override that replaces the hardcoded `provideFleetDirectory()` in its providers list, and reuse whatever action fixture the file already builds rather than minting a second one.

**Run / expected failure**
```
podman exec my-frontend npx ng test --watch=false --include='src/app/components/broker/v2-panel/panel-shell/bot-panel-shell.component.spec.ts'
```
Expected: the first test fails with `expected "runBotAction" to not be called, but it was called once` — the click-time read at `:294` re-adopts generation 4 and dispatches. The second test passes already (it is the regression guard for the happy path, and must stay green through the change).

**Minimal implementation**

1. Add a frozen fence signal beside the existing `target` computed (`:135-143`). Keep `target` as the **read** address — it is legitimately reactive for `resource()` params and the live store — and introduce a separate frozen fence for **commands**:

```ts
  /** The fence the operator was shown. Captured when the shell renders the
   * lane and again only when the route identity changes; never at click time.
   * The backend can refuse only a generation we send, so re-reading it here
   * would make the fence structurally unable to fire (#2068). */
  private readonly openFence = linkedSignal({
    source: () => `${this.broker()}::${this.clerkId()}`,
    computation: (): LaneFence =>
      freezeLaneFence(this.fleetDirectory.lane(this.broker(), this.clerkId())),
  });
```

2. In `onActionRequested` (`:290-339`), replace `const target = this.target();` at `:294` with a fence check before any branch:

```ts
    const fence = this.openFence();
    if (laneFenceDrifted(fence, this.fleetDirectory.lane(this.broker(), this.clerkId()))) {
      this.actionReceipt.set(this.conflictReceipt(action, LANE_FENCE_CONFLICT_MESSAGE));
      this.messageService.add(actionOutcomeToast('conflict', LANE_FENCE_CONFLICT_MESSAGE));
      return;
    }
    const target = fencedTarget(this.target(), fence);
```

3. `commandTarget` (`:378-380`) is unchanged — it already derives from the passed target.

4. Add `fencedTarget(target, fence)` to `lane-fence.ts`: a one-liner returning `resourceTarget(target.broker, target.clerkId, {…target dimensions, bindingGeneration: fence.bindingGeneration, routingEpoch: fence.routingEpoch})`. It exists so three surfaces do not each re-spell the spread.

5. `conflictReceipt` is a three-line sibling of the file's existing `errorReceipt` (`:328`) returning an `ActionReceiptView` with `outcome: 'conflict'`. Do not grow the file with a fourth receipt builder if `errorReceipt` can take an override — the file is 518 lines and must not drift toward the 1k alarm.

**Run / expected pass**
```
podman exec my-frontend npx ng test --watch=false --include='src/app/components/broker/v2-panel/panel-shell/bot-panel-shell.component.spec.ts'
podman exec my-frontend npx ng test --watch=false --include='src/app/components/broker/v2-panel/panel-shell/bot-panel-shell.correlation.spec.ts'
```
Expected: both new tests pass; the two existing panel-shell spec files stay fully green. `bot-panel-shell.correlation.spec.ts` is included because it also provides the directory double and pins correlation identity across the same command path.

**Commit**
```
fix(panel): freeze the binding-generation fence at action open (#2068)

bot-panel-shell.component.ts:294 read the fence from a live directory signal
at click time, so the value sent was always the coordinator's current one and
service.py:1283-1292 could never refuse it. The bug was dormant only because
FleetDirectoryService.refresh() has no caller.

The shell now captures the fence when it renders the lane, sends that, and
refuses with a conflict when the lane rebound underneath an open action
instead of silently re-adopting the new generation.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
```

---

### Task 4 — The roster and the gallery freeze their fences at action open

Same defect, same helper, two files; one commit because the shape and the test are identical and splitting them would leave the gallery — which #2068 never names — unfixed behind a green PR.

**Files**
- modify `Frontend/src/app/components/broker/v2-panel/bots-list-page/bots-list-page.component.ts`
- modify `Frontend/src/app/components/broker/v2-panel/bots-list-page/bots-list-page.component.spec.ts`
- modify `Frontend/src/app/components/broker/v2-panel/gallery/bot-gallery-page/bot-gallery-page.component.ts`
- modify `Frontend/src/app/components/broker/v2-panel/gallery/bot-gallery-page/bot-gallery-page.component.spec.ts`

**Failing regression test** — the roster case (mirror it for the gallery, driving `onAction` through its tile button):

```ts
it('refuses a roster action whose lane rebound while the row was on screen', async () => {
  const directory = provideFleetDirectory();
  const runBotAction = vi.fn().mockResolvedValue({ message: 'stopped' });
  await renderPage([fakeCatalogBot()], { directory, runBotAction });

  directory.rebind({
    observed_at_ms: 1_757_000_000_001,
    clerks: [testLane({ effective_binding_generation: 4 })],
  });

  await userEvent.click(within(screen.getByRole('row', { name: /bot-1/ })).getByRole('button', { name: /stop/i }));

  expect(runBotAction).not.toHaveBeenCalled();
  expect(await screen.findByText(/rebound while the action was open/i)).toBeVisible();
});
```

**Run / expected failure**
```
podman exec my-frontend npx ng test --watch=false --include='src/app/components/broker/v2-panel/bots-list-page/bots-list-page.component.spec.ts'
podman exec my-frontend npx ng test --watch=false --include='src/app/components/broker/v2-panel/gallery/bot-gallery-page/bot-gallery-page.component.spec.ts'
```
Expected: one failure in each file — `expected "runBotAction" to not be called, but it was called once`.

**Minimal implementation**

- **Roster** (`bots-list-page.component.ts`): add the same `openFence` linked signal keyed on `${broker}::${clerkId}`; in `runAction` (`:346-355`) replace `const laneTarget = withEntity(this.target(), sid);` with the drift check plus `withEntity(fencedTarget(this.target(), fence), sid)`. The conflict path reuses the file's own `actionNotice` + `actionOutcomeToast('conflict', …)` pair, which `:361-366` already uses for the "no longer available" case — no new notice mechanism.
- **Gallery** (`bot-gallery-page.component.ts`): `galleryTarget` (`:59-67`) is a function evaluated at click (`:118`). Turn its two directory reads into the frozen fence and take the drift check at the top of `onAction` (`:114-119`), before `pendingSids` is touched. The `store.start(...)` effect at `:100-110` keeps reading the live lane — the stream address is a read, not a command, and must follow a rebinding.
- Neither file gains a new import beyond `lane-fence`.

**Run / expected pass**
```
podman exec my-frontend npx ng test --watch=false --include='src/app/components/broker/v2-panel/bots-list-page/bots-list-page.component.spec.ts'
podman exec my-frontend npx ng test --watch=false --include='src/app/components/broker/v2-panel/gallery/bot-gallery-page/bot-gallery-page.component.spec.ts'
podman exec my-frontend npx ng test --watch=false --include='src/app/components/broker/v2-panel/gallery/lib/gallery-live-store.service.spec.ts'
```

**Commit**
```
fix(panel): freeze the roster's and the gallery's fences at action open (#2068)

bots-list-page.component.ts:353 and bot-gallery-page.component.ts:118 both
built their command target from a live directory read at click time. The
roster's own comment claimed the lane was frozen "before any await" — true of
the await, false of the open, which is where the fence has to be taken.

The gallery is not named in #2068 at all; it has the same defect and the same
fix. Both now refuse with a conflict when the lane rebound, and the gallery's
live stream keeps following the current lane, because a stream address is a
read and must not be fenced.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
```

---

### Task 5 — The two drawers surface drift as a conflict instead of a silent re-mint

Both drawers already freeze at open. Neither tells the operator when the lane moves: one nulls its frozen command, the other clears the typed confirmation and mints a fresh idempotency key. Both are "silently re-adopted", which decision 10 names as the thing to stop.

**Files**
- modify `Frontend/src/app/components/broker/broker-deploy-page/alpaca-deploy-workflow.component.ts`
- modify `Frontend/src/app/components/broker/broker-deploy-page/alpaca-deploy-workflow.component.spec.ts`
- modify `Frontend/src/app/components/broker/v2-panel/cohort-archive/cohort-archive-drawer.component.ts`
- modify `Frontend/src/app/components/broker/v2-panel/cohort-archive/cohort-archive-drawer.component.spec.ts`

**Failing regression test** — the cohort-archive case, which is the sharper one because a typed destructive confirmation is involved:

```ts
it('states the conflict when the lane rebinds under an open archive confirmation', async () => {
  const directory = provideFleetDirectory();
  const runBotAction = vi.fn();
  const { rerender } = await renderDrawer({ directory, visible: true, runBotAction });

  await userEvent.type(screen.getByLabelText(/type ARCHIVE/i), 'ARCHIVE');

  directory.rebind({
    observed_at_ms: 1_757_000_000_001,
    clerks: [testLane({ effective_binding_generation: 4 })],
  });
  await rerender({ visible: true });

  expect(await screen.findByText(/rebound while the action was open/i)).toBeVisible();
  expect(screen.getByRole('button', { name: /archive/i })).toBeDisabled();
  expect(runBotAction).not.toHaveBeenCalled();
});
```

**Run / expected failure**
```
podman exec my-frontend npx ng test --watch=false --include='src/app/components/broker/v2-panel/cohort-archive/cohort-archive-drawer.component.spec.ts'
```
Expected: `Unable to find an element with the text: /rebound while the action was open/i` — today `:130-134` silently calls `clearDestructiveState()` and mints a new target, so the confirmation box empties with no explanation and the button re-enables as soon as the operator retypes.

**Minimal implementation**

- **`cohort-archive-drawer.component.ts:117-135`**: keep the freeze-on-open, but on `openedTarget !== target` while `visible` is true, set a `laneConflict` signal instead of re-minting. The template renders `LANE_FENCE_CONFLICT_MESSAGE` in the existing refusal slot with `role="alert"`, and the submit button's existing disabled binding gains `|| laneConflict()`. Clear `laneConflict` only on the visible→false edge, where `presentedTarget` is already nulled (`:125-128`).
- **`alpaca-deploy-workflow.component.ts:586-593`**: the effect nulls `frozenCommand` on route-key drift. Replace the bare `this.frozenCommand.set(null)` with setting the same conflict state the file already has for a refused deploy (`submitError`), carrying `LANE_FENCE_CONFLICT_MESSAGE`. **Do not add net lines to this file beyond the branch** — it is 957 lines and inside the thermo file-size alarm; if the conflict needs more than the branch, extract the effect body into a named private method in the same commit.
- Note that the deploy drawer's own target genuinely cannot drift while open (`alpaca-deploy-drawer.component.ts:96-102` sets `frozenTarget` only on the false→true edge), so this drift comes from the account or the ticket, not the directory. Say so in the code comment; a future reader will otherwise assume the freeze is redundant and delete it.

**Run / expected pass**
```
podman exec my-frontend npx ng test --watch=false --include='src/app/components/broker/v2-panel/cohort-archive/cohort-archive-drawer.component.spec.ts'
podman exec my-frontend npx ng test --watch=false --include='src/app/components/broker/broker-deploy-page/alpaca-deploy-workflow.component.spec.ts'
podman exec my-frontend npx ng test --watch=false --include='src/app/components/broker/broker-deploy-page/alpaca-deploy-workflow.symbol-scope.spec.ts'
podman exec my-frontend npx ng test --watch=false --include='src/app/components/broker/broker-deploy-page/alpaca-deploy-drawer.component.spec.ts'
```

**Commit**
```
fix(deploy): a lane that rebinds under an open drawer is a conflict (#2068)

Both drawers froze correctly and then re-adopted silently. cohort-archive
cleared the operator's typed ARCHIVE confirmation and minted a fresh
idempotency key with no message; the deploy workflow nulled its frozen command
so the next submit carried a new durable identity against a lane the operator
never saw.

Drift now states itself and blocks submission until the action is reopened,
which is what decision 10 means by "surface drift as a conflict".

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
```

---

### Task 5a — A command with no enforceable fence is refused, not dispatched (decision 15)

**This task exists because of decision 15, answered by the owner on 2026-09-14: refuse, loudly, client-side.** Tasks 3–5 freeze the fence and surface *drift*. None of them handles the state Task 2's `laneFenceIsEnforceable` names: a fence whose `bindingGeneration` is `null`, which `commandContextOf` omits from the envelope (`resource-target.ts:139-141`) and which both backend checks then skip (`service.py:1283-1284`, `routing.py:328-329`). Today that command dispatches **unfenced**. After this task it does not dispatch at all.

This is the correction to #2068's premise. The issue says the fence "fails closed, but silently"; it fails **open**. Freezing alone does not close the hole — it only names it.

**Accepted cost, from the decision:** a cold directory now refuses commands until it warms. That is a self-announcing failure with a stated remedy, chosen deliberately over an invisible unfenced dispatch.

**Files**
- modify `Frontend/src/app/fleet/lane-fence.ts`
- modify `Frontend/src/app/fleet/lane-fence.spec.ts`
- modify the five command seams Tasks 3–5 touched, so each routes its check through the shared verdict rather than calling `laneFenceDrifted` directly:
  - `Frontend/src/app/components/broker/v2-panel/panel-shell/bot-panel-shell.component.ts`
  - `Frontend/src/app/components/broker/v2-panel/bots-list-page/bots-list-page.component.ts`
  - `Frontend/src/app/components/broker/bot-gallery/bot-gallery-page.component.ts`
  - `Frontend/src/app/components/broker/broker-deploy-page/alpaca-deploy-workflow.component.ts`
  - `Frontend/src/app/components/broker/v2-panel/cohort-archive/cohort-archive-drawer.component.ts`
- modify each of their `.spec.ts` siblings

**Sequencing.** Task 5a lands **after Task 5** (all five seams must already freeze) and **before Task 6** (which adds the first `refresh()` caller — an unfenced dispatch racing a refresh is the exact pair decision 15 forecloses).

**One verdict, not five checks.** Each surface asking two questions in its own order is five chances to get the order wrong. One function answers both, and the surfaces consume a verdict:

```ts
export type LaneFenceVerdict =
  | { readonly ok: true }
  | { readonly ok: false; readonly message: string };

/** The one sentence a surface shows when the lane was never fenceable. */
export const LANE_FENCE_UNENFORCEABLE_MESSAGE =
  'This clerk lane had no known binding when the action was opened, so the command ' +
  'was not sent — it would have dispatched with no binding check at all. Reopen the ' +
  'action once the fleet directory has loaded.';

/**
 * Whether a frozen fence may still be acted on.
 *
 * Enforceability is checked BEFORE drift, and the order is load-bearing. A
 * fence frozen against a cold directory holds `bindingGeneration: null`; if the
 * directory has since warmed, `laneFenceDrifted` also reports true (null !== 3).
 * Both statements are accurate, but only one is actionable: the operator was
 * shown a lane whose binding was unknown, and the remedy is to reopen now that
 * it is known — not to reason about a lane that "moved".
 */
export function laneFenceVerdict(frozen: LaneFence, lane: LaneDescriptor | undefined): LaneFenceVerdict {
  if (!laneFenceIsEnforceable(frozen)) {
    return { ok: false, message: LANE_FENCE_UNENFORCEABLE_MESSAGE };
  }
  if (laneFenceDrifted(frozen, lane)) {
    return { ok: false, message: LANE_FENCE_CONFLICT_MESSAGE };
  }
  return { ok: true };
}
```

`LANE_FENCE_UNENFORCEABLE_MESSAGE` is client-authored prose about the *request*, like `LANE_FENCE_CONFLICT_MESSAGE` in Task 2 — the `broker-configuration-refusal.ts:40-45` carve-out. Not piped through `receiptLabel`.

**Failing regression test — the one that proves the hole is closed.** It must assert the command was *not dispatched*, not merely that a message rendered. A test that only checks copy passes against a build that shows the banner and submits anyway.

```ts
it('refuses the command when the directory was cold at open, and dispatches nothing', async () => {
  const directory = provideFleetDirectory({ lanes: [] }); // cold: no lane, so no generation
  const runBotAction = vi.fn();
  await renderPanel({ directory, runBotAction });

  await userEvent.click(screen.getByRole('button', { name: /stop bot/i }));

  expect(runBotAction).not.toHaveBeenCalled();
  expect(screen.getByText(/no known binding when the action was opened/i)).toBeVisible();
});
```

**Prove it can fail.** Before committing, delete the `laneFenceIsEnforceable` branch from `laneFenceVerdict` and confirm this test goes red on the `not.toHaveBeenCalled()` line — not on the copy assertion. A test that only reddens on the copy is pinning a string, not the refusal. Revert the mutation.

**Unit tests on the verdict itself** — add to `lane-fence.spec.ts`:

```ts
it('refuses an unenforceable fence before it reports drift', () => {
  const frozen = freezeLaneFence(undefined); // bindingGeneration: null
  const warmLane = { effective_binding_generation: 3, routing_epoch: 4 } as LaneDescriptor;

  // Both statements are true of this pair; the unenforceable one is the one shown.
  expect(laneFenceDrifted(frozen, warmLane)).toBe(true);
  expect(laneFenceVerdict(frozen, warmLane)).toEqual({
    ok: false,
    message: LANE_FENCE_UNENFORCEABLE_MESSAGE,
  });
});

it('reports drift when the fence was enforceable and the lane moved', () => {
  const frozen = { bindingGeneration: 3, routingEpoch: 4 } as LaneFence;
  const moved = { effective_binding_generation: 4, routing_epoch: 4 } as LaneDescriptor;

  expect(laneFenceVerdict(frozen, moved)).toEqual({
    ok: false,
    message: LANE_FENCE_CONFLICT_MESSAGE,
  });
});

it('passes an enforceable fence against an unmoved lane', () => {
  const frozen = { bindingGeneration: 3, routingEpoch: 4 } as LaneFence;
  const same = { effective_binding_generation: 3, routing_epoch: 4 } as LaneDescriptor;

  expect(laneFenceVerdict(frozen, same)).toEqual({ ok: true });
});
```

**What this task must NOT do.** Do not add a sentinel to `commandContextOf`, and do not relax the `is not None` gates in `service.py` / `routing.py`. Decision 15 rejected the backend-sentinel route explicitly: the client already knows it has nothing to send, so the refusal belongs where the knowledge is. Backend changes here would widen the wire contract for an outcome the client can reach on its own.

**Run / expected pass**
```
podman exec my-frontend npx ng test --watch=false --include='src/app/fleet/lane-fence.spec.ts'
podman exec my-frontend npx ng test --watch=false --include='src/app/components/broker/v2-panel/panel-shell/bot-panel-shell.component.spec.ts'
npx eslint Frontend/src/ --max-warnings 0
```
Expected: `lane-fence.spec.ts` 7 tests pass (4 from Task 2 + 3 here).

**Commit**
```
fix(fleet): refuse a command whose fence cannot be enforced (#2068)

#2068 says the binding fence "fails closed, but silently". It fails OPEN. A
cold directory yields a null generation, commandContextOf omits
expected_effective_binding_generation entirely (resource-target.ts:139-141),
and both backend checks are `is not None`-gated (service.py:1283-1284,
routing.py:328-329) — so the command routed with no binding check at all.

Tasks 3-5 froze the fence and surfaced drift; none of them handled the
unfenceable state laneFenceIsEnforceable names. laneFenceVerdict now answers
both questions in one place, enforceability first, and the five command seams
consume the verdict instead of asking about drift alone.

Per the owner's decision 15: refuse, loudly, client-side. A cold directory
refuses commands until it warms — a self-announcing failure with a remedy,
chosen over an invisible unfenced dispatch.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
```

---

### Task 6 — `refresh()` gains its first caller, behind the landed freeze

**This task is last, and it is the only task that may add a `refresh()` caller.** Adding one before Tasks 3–5 would activate the dormant bug on every unfrozen surface.

**Files**
- modify `Frontend/src/app/components/broker/v2-panel/lib/panel-action-outcome.ts` (or wherever Task 8 lands the reason-code read — see the sequencing note below)
- modify `Frontend/src/app/components/broker/v2-panel/panel-shell/bot-panel-shell.component.ts`
- modify `Frontend/src/app/components/broker/v2-panel/bots-list-page/bots-list-page.component.ts`
- create `Frontend/src/app/fleet/lane-fence-freeze.contract.spec.ts`

**Failing regression test** — two parts. The behavioural half:

```ts
it('refreshes the directory after the coordinator refuses a stale generation', async () => {
  const directory = provideFleetDirectory();
  const refresh = vi.spyOn(directory.useValue as never, 'refresh');
  const runBotAction = vi.fn().mockRejectedValue(
    new HttpErrorResponse({
      status: 409,
      error: { reason: 'clerk_binding_generation_conflict', message: 'Expected 3 is not 4.' },
    }),
  );
  await renderPage([fakeCatalogBot()], { directory, runBotAction });

  await userEvent.click(within(screen.getByRole('row', { name: /bot-1/ })).getByRole('button', { name: /stop/i }));

  expect(refresh).toHaveBeenCalledTimes(1);
});
```

…and the structural half, a contract spec that keeps the freeze from regressing once `refresh()` is live:

```ts
/** Once the directory can refresh mid-session, a command target rebuilt from a
 * live directory read is a live defect, not a latent one. This spec is the
 * guard: no non-spec source may mint a command from a directory lookup. */
import { readFileSync, readdirSync } from 'node:fs';
import { join } from 'node:path';
import { describe, expect, it } from 'vitest';

const APP_ROOT = join(__dirname, '..');
const ALLOWED = new Set(['fleet/lane-fence.ts', 'fleet/fleet-directory.service.ts']);

function sources(dir: string): string[] { /* recursive .ts walk, skipping *.spec.ts and api/broker.types.ts */ }

describe('the lane fence is frozen, never read at command time', () => {
  it('no component derives a command target from a directory lookup', () => {
    const offenders: string[] = [];
    for (const path of sources(APP_ROOT)) {
      const text = readFileSync(path, 'utf8');
      if (!text.includes('withCommand(')) continue;
      if (!/fleetDirectory\.lane\(|fleet\.lane\(/.test(text)) continue;
      if (text.includes('freezeLaneFence(')) continue;
      offenders.push(path.slice(APP_ROOT.length + 1));
    }
    expect(offenders.filter((p) => !ALLOWED.has(p))).toEqual([]);
  });
});
```

**Run / expected failure**
```
podman exec my-frontend npx ng test --watch=false --include='src/app/fleet/lane-fence-freeze.contract.spec.ts'
podman exec my-frontend npx ng test --watch=false --include='src/app/components/broker/v2-panel/bots-list-page/bots-list-page.component.spec.ts'
```
Expected: the behavioural test fails with `expected "refresh" to be called 1 times, but got 0 times`. The contract spec is expected to **pass** the moment Tasks 3–5 have landed — run it on the branch point before Task 3 to prove it is falsifiable: it must list `components/broker/v2-panel/panel-shell/bot-panel-shell.component.ts`, `components/broker/v2-panel/bots-list-page/bots-list-page.component.ts` and `components/broker/v2-panel/gallery/bot-gallery-page/bot-gallery-page.component.ts` there. Record that output in the PR body; a contract spec nobody has seen fail is a canary that cannot fire.

**Minimal implementation.** On a rejected command whose refusal `reason` is `clerk_binding_generation_conflict`, call `void this.fleetDirectory.refresh()` in the roster's and the panel's catch blocks, after the notice is set. The frozen fence means the next action is minted against a lane the operator has actually been shown; without Tasks 3–5, this same call would arm the stale-fence defect on every surface.

**Run / expected pass**
```
podman exec my-frontend npx ng test --watch=false --include='src/app/fleet/lane-fence-freeze.contract.spec.ts'
podman exec my-frontend npx ng test --watch=false --include='src/app/fleet/fleet-directory.service.spec.ts'
podman exec my-frontend npx ng test --watch=false
npx eslint Frontend/src/ --max-warnings 0
```
Expected: the full suite green. `2545` baseline + the tests added by Tasks 1–6.

**Commit**
```
feat(fleet): refresh the directory on a stale-generation refusal (#2068)

FleetDirectoryService.refresh() had no caller, which is the only reason the
click-time fence read was harmless. Now that every command surface freezes its
fence at open, a 409 clerk_binding_generation_conflict refreshes the directory
so the operator's next action is minted against a lane they have seen.

A contract spec pins the invariant: no non-spec source may mint a command from
a directory lookup without going through freezeLaneFence. Verified falsifiable
— on the branch point it lists the panel, the roster and the gallery.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
```

**Sequencing note within E-A.** If PR E-C has not landed, the 409's `reason` is read straight from `refusalBody(error)?.['reason']` here; Task 8 later routes the same read through the copy map. Do not block E-A on E-C.

---

### Task 7 — The refusal vocabulary: snapshot, `next_step` backfill, and the bare-detail wrap

One PR, five commits, in this order. **E-B lands after Lane D's PR D-D**: both touch `app/broker/fleet/routing.py` and `errors.py`, and D-D also deletes `bots_deploy_apply` and bumps the adapter version.

#### 7a — `refusal_vocabulary.py` + the two committed snapshots + the generator

**Files**
- create `PythonDataService/app/broker/fleet/refusal_vocabulary.py`
- create `PythonDataService/app/broker/fleet/refusal_vocabulary.snapshot.json`
- create `Frontend/src/app/fleet/fleet-refusal-vocabulary.snapshot.json`
- create `PythonDataService/scripts/regenerate_fleet_refusal_vocabulary_snapshot.py`
- create `PythonDataService/tests/broker/fleet/test_refusal_vocabulary_snapshot.py`

The module declares `FLEET_REFUSAL_REASONS` — the name is pinned by the master plan's type-consistency list — as a mapping from reason code to a frozen record of `status_code` and the family's one-line meaning, covering **all 30** codes: the 25-class closure plus the five minted outside it. The five outsiders are declared explicitly with their mint site in a comment, so the module is a complete census rather than a convenient subset.

**Failing regression test** — new file, modelled on `tests/broker/v2panel/test_vocabulary_snapshot.py:64-130`:

```python
"""Committed-snapshot parity for the fleet's closed refusal vocabulary (#2067).

Decision 9: the reasons do NOT enter the exported OpenAPI contract. The
frontend's copy map locks against the committed snapshot instead, exactly as
the broker-v2 panel vocabulary does — see
scripts/regenerate_broker_v2_vocabulary_snapshot.py and
.github/workflows/ci.yml:203-236 for the pattern this mirrors.

The live set is derived, not hand-listed: walking FleetControlError's subclass
closure means a new refusal family cannot be added without this test noticing.
"""

def test_the_declared_set_equals_the_live_subclass_closure() -> None:
    """Every FleetControlError subclass in app/ is declared, and vice versa."""
    live = {cls.reason for cls in _subclass_closure(FleetControlError)}
    declared = set(FLEET_REFUSAL_REASONS) - _MINTED_OUTSIDE_THE_CLOSURE
    assert live == declared


def test_every_minted_reason_literal_is_declared() -> None:
    """The five codes produced without a FleetControlError are declared too."""
    assert _MINTED_OUTSIDE_THE_CLOSURE == {
        "command_envelope_invalid",
        "compatibility_read_retired",
        "compatibility_retirement_state_invalid",
        "fleet_lane_capacity_exhausted",
        "qualification_market_status_unavailable",
    }
    assert _MINTED_OUTSIDE_THE_CLOSURE <= set(FLEET_REFUSAL_REASONS)


def test_committed_snapshots_match_freshly_generated_output() -> None:
    from scripts.regenerate_fleet_refusal_vocabulary_snapshot import build_snapshot

    fresh = json.dumps(build_snapshot(), indent=2, sort_keys=False) + "\n"
    assert _SNAPSHOT_PATH.read_text(encoding="utf-8") == fresh


def test_python_and_frontend_snapshots_are_byte_identical() -> None:
    if not _FRONTEND_SNAPSHOT_PATH.exists():
        pytest.skip(f"Frontend/ not present in this checkout ({_FRONTEND_SNAPSHOT_PATH})")
    assert _SNAPSHOT_PATH.read_text(encoding="utf-8") == _FRONTEND_SNAPSHOT_PATH.read_text(encoding="utf-8")


def test_the_closure_walk_would_notice_a_new_family() -> None:
    """The parity check is vacuous unless the walk actually finds subclasses."""
    class _Probe(FleetControlError):
        reason = "probe_only_never_raised"

    try:
        assert "probe_only_never_raised" in {c.reason for c in _subclass_closure(FleetControlError)}
    finally:
        FleetControlError.__subclasses__()  # the probe is GC'd with the test
```

The last test is the falsifiability guard: without it, a broken `_subclass_closure` would return an empty set and the parity assertion would pass against an empty `declared`.

**Run / expected failure**
```
DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest tests/broker/fleet/test_refusal_vocabulary_snapshot.py -q
```
(`dangerouslyDisableSandbox: true`)
Expected: collection error — `ModuleNotFoundError: No module named 'app.broker.fleet.refusal_vocabulary'`.

**Implementation.** `build_snapshot()` returns `{"$comment", "generated_by", "source_files", "reasons": {code: {"status_code": …, "meaning": …}}}` with `reasons` sorted by code; `_write` uses `json.dumps(snapshot, indent=2, sort_keys=False) + "\n"`, copying `regenerate_broker_v2_vocabulary_snapshot.py:107-119` exactly. The importing module for the closure walk must not import any provider package — `tests/broker/fleet/test_import_isolation.py` fences that.

#### 7b — `next_step` on the six zero-coverage families

**Files**: `app/broker/fleet/service.py` (25 sites), `app/broker/fleet/routing.py` (6 sites), `app/broker/fleet/presence.py` (2 sites).

**Failing regression test** — append to the new snapshot test file:

```python
_NEXT_STEP_REQUIRED = frozenset({
    "clerk_unreachable", "clerk_not_found", "clerk_broker_mismatch",
    "broker_and_clerk_required", "clerk_account_mismatch", "fleet_control_error",
})


def test_no_refusal_family_raises_without_a_next_step_everywhere() -> None:
    """Six families had zero next_step across 31 raise sites; clerk_unreachable
    is a 503 the operator can act on, and carried none at any of its 13."""
    bare = _raise_sites_without_next_step(REPOSITORY_APP_ROOT)
    offending = {reason for reason in bare if reason in _NEXT_STEP_REQUIRED}
    assert offending == set(), (
        "these families still have a raise site with no next_step: " f"{sorted(offending)}"
    )
```

`_raise_sites_without_next_step` is an `ast` walk in the test module — the same technique that measured the baseline. **Expected before:** `AssertionError: these families still have a raise site with no next_step: ['broker_and_clerk_required', 'clerk_account_mismatch', 'clerk_broker_mismatch', 'clerk_not_found', 'clerk_unreachable', 'fleet_control_error']`. **Expected after:** empty set, and the measured totals move from 90/187 to **121/187** carrying `next_step`.

The two `FleetControlError` base raises at `presence.py:276,316` should become a **named** family rather than gain a `next_step` on the base — raising the base is what produces the useless `fleet_control_error` code in the first place. If a suitable existing family fits, use it and note the code change in the commit; if not, declare one in `errors.py` and regenerate the 7a snapshot in the same commit.

#### 7c — The bare-detail 403/503 responses take the flat refusal body

**Files**: `app/security/data_plane_control.py` (`:66-68`, `:73-75`, `:79-81`), `app/routers/broker_clerks.py` (`:58-61`, `:71-74`), `app/routers/internal_fleet.py` (`:84`, `:95`, `:97`, `:108`).

**Failing regression test** — new `tests/broker/fleet/test_refusal_body_shape.py`, asserting over a real ASGI app that every refusal from the fleet surface parses as `{reason, message}` with a `reason` in `FLEET_REFUSAL_REASONS`, and in particular:

```python
async def test_a_missing_control_secret_refuses_in_the_contract_shape() -> None:
    ...
    assert response.status_code == 403
    body = response.json()
    assert set(body) >= {"reason", "message"}
    assert body["reason"] in FLEET_REFUSAL_REASONS
    assert "detail" not in body
```

**Expected before:** `AssertionError: assert {'detail'} >= {'reason', 'message'}`.

**Implementation.** Three new families in `errors.py` — a control-secret refusal (403), a not-installed refusal (503) and an agent-token refusal (403) — each raised in place of the bare `HTTPException`, and each with a `next_step`. `_refuse` (`broker_clerks.py:78-80`) already writes the flat shape; add the same writer to `internal_fleet.py` so its `_refuse` (`:111-113`) stops nesting the body under FastAPI's `detail`. Regenerate the 7a snapshot.

**Explicitly out of scope:** the four hand-built bodies at `lane_runtime.py:543-556`, `:571-584`, `:605-620` and `agent_identity.py:234-253`. They are raw-ASGI writers on the middleware path where no `Response` object exists, and `lane_runtime.py` is a file Lane D's PR D-A has already edited. File them; do not fold them into this PR.

#### 7d — CI extends the vocabulary job

Add a second regenerate-and-diff block to `.github/workflows/ci.yml`'s existing `broker-v2-vocabulary-contract` job (currently `:203-236`), running `python -m scripts.regenerate_fleet_refusal_vocabulary_snapshot` and diffing both new snapshot paths with the same `git diff --exit-code` plus `git status --porcelain` pair. Reuse the job rather than adding a fourth; it already installs the same requirements. **Rebase order** for `ci.yml` is #2078 → Lane F A3 → E-B.

#### 7e — Regenerate the contracts

New `errors.py` families change no route or schema, but 7c's refusal bodies replace `HTTPException` responses whose shape FastAPI documents. Run `python scripts/export_openapi_contract.py` and, from `Frontend/`, `npm run codegen:openapi`, in the same commit.

**Run / expected pass for Task 7**
```
ruff check PythonDataService/app/ PythonDataService/tests/
DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest tests/broker/fleet tests/contracts -q
DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest tests/test_data_plane_control_security.py tests/routers/test_broker_clerks.py -q
python scripts/export_openapi_contract.py --check
DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m scripts.run_fast_tests
```
Expected: **342 baseline + the new tests**, 0 failures, ruff clean, `--check` clean.

**Commit** (the PR's first; 7b–7e each get their own)
```
feat(fleet): give the refusal vocabulary a committed snapshot (#2067)

Thirty distinct reason codes exist on the fleet surface — 22 declared in
errors.py, 3 more FleetControlError subclasses outside it (presence.py:51,
confirmation.py:38, fleet_boot.py:64) and 5 minted without a
FleetControlError at all — and none of them appeared anywhere in Frontend/src.

Per decision 9 they do not enter the exported OpenAPI contract. They get the
pattern this repo already uses for the broker-v2 panel vocabulary: a live set
derived by walking the subclass closure, a generator, two byte-identical
committed snapshots, and an in-process freshness assertion, so a new refusal
family cannot ship without the frontend's copy map noticing.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
```

---

### Task 8 — The frontend learns the vocabulary, and a fleet 409 stops reading as "Unknown"

**Files**
- create `Frontend/src/app/fleet/fleet-refusal-copy.ts`
- create `Frontend/src/app/fleet/fleet-refusal-copy.spec.ts`
- modify `Frontend/src/app/components/broker/v2-panel/lib/panel-action-outcome.ts`
- modify `Frontend/src/app/components/broker/v2-panel/lib/panel-action-outcome.spec.ts`
- modify `Frontend/src/app/components/broker/operation-error.ts`
- modify `Frontend/src/app/components/broker/operation-error.spec.ts`
- modify `Frontend/src/app/components/broker/account-desk/account-desk-transaction-history-store.service.spec.ts`

**Failing regression test** — three parts.

Parity, modelled on `broker-v2-copy-contract.spec.ts:35-85`:

```ts
import snapshot from './fleet-refusal-vocabulary.snapshot.json';
import { FLEET_REFUSAL_COPY, fleetRefusalOutcome } from './fleet-refusal-copy';

describe('fleet refusal copy contract', () => {
  it('covers every reason code in the committed snapshot', () => {
    expect(Object.keys(snapshot.reasons).filter((code) => !(code in FLEET_REFUSAL_COPY))).toEqual([]);
  });

  it('carries no orphan entries', () => {
    expect(Object.keys(FLEET_REFUSAL_COPY).filter((code) => !(code in snapshot.reasons))).toEqual([]);
  });

  it('maps each code to the outcome its status implies', () => {
    for (const [code, { status_code }] of Object.entries(snapshot.reasons)) {
      const expected = status_code === 409 ? 'conflict' : status_code >= 500 ? 'failure' : 'failure';
      expect(fleetRefusalOutcome(code)).toBe(expected);
    }
  });
});
```

Outcome mapping, appended to `panel-action-outcome.spec.ts`:

```ts
it('reads a fleet 409 as a conflict, not as Unknown', () => {
  const rejection = deriveActionRejection(
    new HttpErrorResponse({
      status: 409,
      error: { reason: 'clerk_binding_generation_conflict', message: 'Expected 3 is not 4.' },
    }),
    'fallback',
  );
  expect(rejection.outcome).toBe('conflict');
  expect(rejection.message).toBe('Expected 3 is not 4.');
});
```

And the last nested-only parser, appended to `operation-error.spec.ts`:

```ts
it('reads a flat fleet refusal, which is the shape a clerk-scoped route returns', () => {
  const error = new HttpErrorResponse({
    status: 503,
    error: { reason: 'clerk_unreachable', message: 'The clerk did not answer.' },
  });
  expect(extractServerMessage(error, 'fallback')).toBe('The clerk did not answer.');
});
```

**Run / expected failure**
```
podman exec my-frontend npx ng test --watch=false --include='src/app/fleet/fleet-refusal-copy.spec.ts'
podman exec my-frontend npx ng test --watch=false --include='src/app/components/broker/v2-panel/lib/panel-action-outcome.spec.ts'
podman exec my-frontend npx ng test --watch=false --include='src/app/components/broker/operation-error.spec.ts'
```
Expected: the copy spec fails to resolve `./fleet-refusal-copy`; the outcome test fails `expected 'unknown' to be 'conflict'`; the parser test fails `expected 'fallback' to be 'The clerk did not answer.'`.

**Minimal implementation**

1. `fleet-refusal-copy.ts` holds `FLEET_REFUSAL_COPY` — the emergency fallback map, one short **client-authored** sentence per code for the case where the backend sends no `message`, mirroring `broker-v2-emergency-copy.ts`'s role — and `fleetRefusalOutcome(reason)`, deriving `'conflict' | 'failure'` from the snapshot's `status_code`. Backend-authored `message` and `next_step` always win; the map is a last resort, never a rewrite.
2. `deriveActionRejection` (`panel-action-outcome.ts:33-61`) gains one branch: when `detail['outcome']` is absent and `detail['reason']` is a known fleet code, take the outcome from `fleetRefusalOutcome`. Do not touch the `why` precedence chain at `:51-60` — it is already correct, and `next_step` is backend prose that must not be piped.
3. `extractServerMessage` (`operation-error.ts:109-114`) reads `refusalBody(error)` first and falls back to its current nested read. This is the third parser #2067 named; #2081 migrated the other two.
4. **Delete `toOperationError` and its now-orphaned helpers** — it has zero production callers (`grep -rn toOperationError src/` returns the definition and 13 spec references only) and is 60 lines of parser that cannot see a fleet refusal. Delete its spec block with it. If the thermo review prefers keeping it, say so in the PR body with the reason; do not leave it undiscussed.

**Run / expected pass**
```
podman exec my-frontend npx ng test --watch=false --include='src/app/fleet/fleet-refusal-copy.spec.ts'
podman exec my-frontend npx ng test --watch=false --include='src/app/components/broker/v2-panel/lib/panel-action-outcome.spec.ts'
podman exec my-frontend npx ng test --watch=false --include='src/app/components/broker/operation-error.spec.ts'
podman exec my-frontend npx ng test --watch=false --include='src/app/components/broker/account-desk/account-desk-transaction-history-store.service.spec.ts'
podman exec my-frontend npx ng test --watch=false
```

**Commit**
```
feat(fleet): render the refusal vocabulary, and stop calling a 409 "Unknown"

Thirty backend reason codes appeared nowhere in Frontend/src. The copy map now
locks against E-B's committed snapshot, so adding a family without frontend
copy fails a contract test.

deriveActionRejection took `outcome` from a field only the panel's own error
body carries, so every fleet refusal — including the binding-generation fence
at 409 — rendered as severity error, summary "Unknown". It now derives the
outcome from the reason's pinned status.

operation-error.ts was the third parser #2067 named and the one #2081 did not
migrate; extractServerMessage now accepts the flat body. Its dead sibling
toOperationError, which had no production caller, is deleted.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
```

---

### Task 9 — Render `authority_state`

**Files**
- modify `Frontend/src/app/components/brokers/alpaca-desk/lane-directory/alpaca-lane-directory.component.html`
- modify `Frontend/src/app/components/brokers/alpaca-desk/lane-directory/alpaca-lane-directory.component.spec.ts`

**Failing regression test** — appended to the lane-directory spec:

```ts
it('names each lane authority state, as the backend reported it', async () => {
  await render(AlpacaLaneDirectoryComponent, {
    providers: [provideFleetDirectory({
      observed_at_ms: 1_757_000_000_000,
      clerks: [
        testLane({ clerk_id: 'clrk_paper', provider_summary: { authority_state: 'real_paper' } }),
        testLane({ clerk_id: 'clrk_shadow', provider_summary: { authority_state: 'shadow' } }),
      ],
    })],
  });

  expect(await screen.findByText('Real paper')).toBeVisible();
  expect(screen.getByText('Shadow')).toBeVisible();
});

it('says a lane reported no authority state rather than inventing one', async () => {
  await render(AlpacaLaneDirectoryComponent, {
    providers: [provideFleetDirectory({
      observed_at_ms: 1_757_000_000_000,
      clerks: [testLane({ provider_summary: { authority_state: null } })],
    })],
  });
  expect(await screen.findByText('Not reported')).toBeVisible();
});
```

**Run / expected failure**
```
podman exec my-frontend npx ng test --watch=false --include='src/app/components/brokers/alpaca-desk/lane-directory/alpaca-lane-directory.component.spec.ts'
```
Expected: `Unable to find an element with the text: Real paper`.

**Minimal implementation.** Add one `lane-card__fact` block to the template's `<dl>` (after the endpoint-mode block at `:35-38`), matching the existing pattern exactly:

```html
          <div class="lane-card__fact">
            <dt>Authority</dt>
            <dd>{{ lane.provider_summary?.authority_state ?? 'not_reported' | receiptLabel }}</dd>
          </div>
```

`authority_state` is a raw backend identifier — a code-like receipt value — so it goes through `receiptLabel`, which the component already imports (`:11,21`). It is **not** a closed union: `app/broker/fleet/records.py:139-141` accepts any bounded snake-case token, and `app/main.py:607-612` is the only producer today (`real_paper`, `real_live`, `shadow`, `synthetic`, `unavailable`). Do not add a TypeScript union for it; the pipe handles an unknown token correctly and a union would have to be widened every time the backend adds an authority kind.

**Run / expected pass**
```
podman exec my-frontend npx ng test --watch=false --include='src/app/components/brokers/alpaca-desk/lane-directory/alpaca-lane-directory.component.spec.ts'
podman exec my-frontend npx ng test --watch=false --include='src/app/components/brokers/alpaca-desk/alpaca-desk.component.spec.ts'
```
(the desk spec is included because a parent spec pins child copy)

**Commit**
```
feat(desk): render each lane's authority state (#2076)

authority_state crossed the wire into LaneProviderSummary and was read by
nothing — the operator could see a lane's endpoint mode and binding generation
but not whether its authority was real, shadowed or synthetic.

It renders through receiptLabel as the code-like receipt value it is. No
TypeScript union: records.py:139-141 accepts any bounded snake-case token, so
a closed union would be a lie the next authority kind breaks.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
```

---

### Task 10 — A catalog snapshot and an `operationUrl` builder

**The catalog snapshot must be generated AFTER Lane D's PR D-D lands**, because D-D deletes `bots_deploy_apply` and bumps `_ADAPTER_VERSION` from `alpaca-fleet.4` to `alpaca-fleet.5` (`fleet_adapter.py:25`). Generating it earlier commits a snapshot that D-D immediately invalidates.

**Files**
- create `PythonDataService/scripts/regenerate_fleet_operation_catalog_snapshot.py`
- create `PythonDataService/app/broker/fleet/operation_catalog.snapshot.json`
- create `Frontend/src/app/fleet/fleet-operation-catalog.snapshot.json`
- create `PythonDataService/tests/broker/fleet/test_operation_catalog_snapshot.py`
- create `Frontend/src/app/fleet/operation-url.ts`
- create `Frontend/src/app/fleet/operation-url.spec.ts`
- modify `Frontend/src/app/services/brokers.service.ts`, `Frontend/src/app/components/broker/v2-panel/lib/broker-v2-panel.service.ts`, `Frontend/src/app/components/broker/v2-panel/gallery/lib/gallery-live-store.service.ts`
- modify `.github/workflows/ci.yml` (extend the same job E-B extended)

**Failing regression test** — the frontend half:

```ts
import { describe, expect, it } from 'vitest';

import catalog from './fleet-operation-catalog.snapshot.json';
import { operationUrl } from './operation-url';

const TARGET = { broker: 'alpaca', clerkId: 'clrk_1', accountId: 'PA9' };

describe('operationUrl', () => {
  it('builds the declared path for a lane-scoped read', () => {
    expect(operationUrl('account_read', TARGET)).toBe('/api/brokers/alpaca/clerks/clrk_1/account');
  });

  it('builds the declared path for an account-scoped read', () => {
    expect(operationUrl('bots_catalog_read', TARGET)).toBe(
      '/api/brokers/alpaca/clerks/clrk_1/accounts/PA9/bots/catalog',
    );
  });

  it('refuses an operation the catalog does not declare', () => {
    expect(() => operationUrl('order_groups_read' as never, TARGET)).toThrow(/not a declared/i);
  });

  it('refuses an account-scoped operation with no account', () => {
    expect(() => operationUrl('bots_catalog_read', { broker: 'alpaca', clerkId: 'clrk_1' })).toThrow(
      /account/i,
    );
  });

  it('does not percent-encode a :path parameter', () => {
    // manual_order_cancel declares {order_ref:path}; encoding its slashes would
    // under-match the route the coordinator mounts.
    expect(operationUrl('manual_order_cancel', { ...TARGET, orderRef: 'alpaca/abc-123' })).toBe(
      '/api/brokers/alpaca/clerks/clrk_1/accounts/PA9/manual-orders/alpaca/abc-123/cancel',
    );
  });

  it('declares exactly the operations the coordinator serves', () => {
    expect(Object.keys(catalog.operations)).toHaveLength(76);
  });
});
```

The `76` is `77 − 1` after D-D deletes `bots_deploy_apply`. **Measure it at implementation time and correct this number if D-D landed differently** — do not copy it forward blind.

**Run / expected failure**
```
podman exec my-frontend npx ng test --watch=false --include='src/app/fleet/operation-url.spec.ts'
```
Expected: `Failed to resolve import "./operation-url"`.

**Implementation.** The generator emits `{"$comment", "generated_by", "source_files", "adapter_version", "operations": {operation_id: {"method", "path_template", "requires_account"}}}`, sorted, into the two byte-identical snapshot files, exactly as `regenerate_broker_v2_vocabulary_snapshot.py` does. The pytest side asserts freshness, byte-identity and set parity against `production_provider_adapters()`. `operationUrl` composes `clerkScope` from `clerk-scoped-url.ts` (do not duplicate it) and substitutes path parameters, calling `encodeURIComponent` on every segment **except** a `:path` parameter.

**Migration scope, honestly stated.** The 44 `laneUrl`/`accountUrl` invocations address **61** HTTP calls. Migrate the three services whose calls map one-to-one onto declared operations: `brokers.service.ts` (22 sites / 24 calls — the two non-lane calls are `getLiveVerdict` at `:129-134` and `listOrderGroups` at `:187-201`, which Task 11 deletes), `broker-v2-panel.service.ts` (19/19) and `gallery-live-store.service.ts` (2/1).

**`broker-configuration.service.ts` is deliberately NOT migrated in this task.** One `laneUrl` call at `:56-57` builds a prefix that ten sites concatenate onto, driving 17 HTTP calls, and the catalog declares no bare `/configuration` operation to anchor that prefix. Migrating it means rewriting all ten concatenations into per-operation calls — a change of comparable size to everything else in this PR, on the desk-configuration write path. File it as a follow-up and say so in the PR body; do not quietly leave it unmentioned.

**Run / expected pass**
```
DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest tests/broker/fleet/test_operation_catalog_snapshot.py -q
ruff check PythonDataService/app/ PythonDataService/tests/
podman exec my-frontend npx ng test --watch=false --include='src/app/fleet/operation-url.spec.ts'
podman exec my-frontend npx ng test --watch=false
python scripts/export_openapi_contract.py --check
```

**Commit**
```
feat(fleet): derive the frontend's clerk-scoped URLs from the catalog (#2076)

clerk-scoped-url.ts took a free-form `/${string}` suffix, so no test tied any
of the 44 call sites to a declared operation and a typo reached the
coordinator as a 404 with no contract to catch it.

The operation catalog is now a committed snapshot with the same
generate-and-diff CI gate as the broker-v2 vocabulary, and operationUrl builds
paths from it — refusing an undeclared operation, refusing an account-scoped
operation with no account, and leaving a {order_ref:path} parameter's slashes
intact, which encodeURIComponent would have under-matched.

broker-configuration.service.ts is not migrated here: its single laneUrl call
builds a prefix that ten sites concatenate onto, and the catalog declares no
bare /configuration operation to anchor it. Filed separately.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
```

---

### Task 11 — Delete the dead `listOrderGroups` (per decision 13)

**Decision 13 default is DELETE.** This task is written so it stands if the owner flips it to "bridge"; the flip path is stated at the end.

**Files**
- modify `Frontend/src/app/services/brokers.service.ts` (delete `listOrderGroups`, `:187-201`)
- modify `Frontend/src/app/services/brokers.service.spec.ts` (delete the block at `:103-107`)
- modify `Frontend/src/app/api/broker-models.ts` or wherever `BrokerOrderGroup` is declared, if nothing else imports it
- modify `docs/design/fleet-b-route-inventory.md` (the stranded-route list)

**Failing regression test** — add to `Frontend/src/app/fleet/operation-url.spec.ts` (Task 10's file), so the absence is pinned by the same contract that pins the catalog:

```ts
it('no service reaches an unscoped broker path that the catalog does not declare', async () => {
  // GET /api/brokers/{broker}/order-groups is mounted clerk-only and is not in
  // the catalog, so in fleet posture it 404s. Its one caller was dead.
  const source = await import('../services/brokers.service?raw');
  expect(source.default).not.toContain('order-groups');
});
```

If the raw-import idiom is not available in this Vitest config, use the `node:fs` read the Task 6 contract spec already establishes.

**Run / expected failure**
```
podman exec my-frontend npx ng test --watch=false --include='src/app/fleet/operation-url.spec.ts'
```
Expected: `expected '…listOrderGroups…' not to contain 'order-groups'`.

**Minimal implementation.** Delete `listOrderGroups` (`brokers.service.ts:187-201`) and its spec block. Verify `BrokerOrderGroup` has no other importer before deleting the type. Update the stranded-route list in `docs/design/fleet-b-route-inventory.md` to record that the frontend caller is gone and the route is now stranded with **no** consumer at all — Lane D's PR B edits the same list, so rebase on it.

**Run / expected pass**
```
podman exec my-frontend npx ng test --watch=false --include='src/app/services/brokers.service.spec.ts'
podman exec my-frontend npx ng test --watch=false
npx eslint Frontend/src/ --max-warnings 0
DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest tests/contracts -q
```
(the `tests/contracts` run is required because this task edits a file under `docs/`)

**If the owner flips decision 13 to "bridge" instead:** do not delete. Instead declare an `order_groups_read` operation in `fleet_adapter.py` (a `GET /order-groups`, `Capability.ORDERS_READ`, lane-scoped, no account), regenerate Task 10's catalog snapshot and the OpenAPI contract, and migrate `listOrderGroups` to `operationUrl('order_groups_read', target)` with a `ResourceTarget` parameter replacing the `broker = 'alpaca'` default. The route already exists and is mounted clerk-only; bridging it is a catalog declaration, not a new route. This path **regenerates contracts**, so it must not run concurrently with any other regenerating PR.

**Commit**
```
refactor(broker): delete the dead listOrderGroups caller (#2076)

GET /api/brokers/{broker}/order-groups is mounted clerk-only and is not a
catalog operation, so in fleet posture it 404s. Its only caller was
brokers.service.ts:187-201, which no production code invoked — the sole
reference was its own spec.

Per owner decision 13 the frontend method goes rather than the route being
bridged. The route inventory now records it as stranded with no consumer.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
```

---

### Task 12 — DEFERRED: per-lane `live-verdict`

**Not planned. Recorded as a deferral with its reason, per the master plan.**

`getLiveVerdict` (`brokers.service.ts:129-134`) hardcodes `alpaca` and carries no clerk, and the shell banner polls it every five seconds (`alpaca-live-verdict.service.ts:47`, mounted from `app.component.ts:9`). With two lanes registered, the shell presents one lane's verdict as the installation's.

It is deferred because the fix is a **backend redesign**, not a frontend change: `GET /api/brokers/{broker}/live-verdict` is one of exactly two unscoped reads the coordinator serves during the compatibility window (`app/routers/fleet_compatibility_reads.py`), it has no clerk-scoped successor and no catalog operation, and it is **absent from the committed OpenAPI contract** — the exporter forces `FLEET_ROLE=combined` (`scripts/export_openapi_contract.py:52-60`) while that router mounts only under `_FLEET_ROLE == "fleet_coordinator"` (`app/main.py:1236`). Making the banner lane-aware requires deciding what an installation-level verdict *means* across lanes with different authority states before any route is written. Lane D's §5 risk 6 names the same contract gap from the other side.

File it as its own issue referencing this paragraph.

---

## 4. Grouping advice

**PR boundaries — four PRs, in this order.** These match the master plan's table; nothing in the verification justified changing them.

| PR | Tasks | Why it stands alone | Regenerates contracts? | Deploy |
|---|---|---|---|---|
| **E-A** | 1–5, 5a, 6 | Pure frontend. Depends on nothing still open and is **unblocked today**. Task 1 must land before 3–5 or their tests are unfalsifiable; Task 5a must land after 3–5 (all five seams must freeze first) and before Task 6; Task 6 must land last or it activates the dormant bug. | No | frontend rebuild |
| **E-B** | 7 | Backend vocabulary, `next_step` backfill, refusal-body wrap, CI. | **Yes** | restart |
| **E-C** | 8–9 | Frontend copy map + `authority_state`. Imports E-B's committed snapshot. | No | frontend rebuild |
| **E-D** | 10–11 | Catalog snapshot + `operationUrl` + migration + the dead-method delete. | **Yes** | frontend rebuild |

**Ordering constraints, including two the master plan did not record.**

1. **E-B lands after Lane D's PR D-D.** Both touch `app/broker/fleet/routing.py` and `errors.py`; D-D also deletes `bots_deploy_apply` and bumps the adapter version. (Master plan; confirmed.)
2. **E-C follows E-B** — it imports E-B's committed snapshot. (Master plan; confirmed.)
3. **E-D's catalog snapshot is generated after D-D lands**, because D-D changes `fleet_adapter.py`'s declared operations. (Master plan; confirmed, and the exact effect is 77 → 76 operations plus `_ADAPTER_VERSION` `alpaca-fleet.4` → `.5` at `fleet_adapter.py:25`.)
4. **E-B and E-D each regenerate both contract files. Never two regenerating PRs in flight unrebased.** (Master plan; confirmed.)
5. **NEW — E-B and E-D both extend `.github/workflows/ci.yml`'s `broker-v2-vocabulary-contract` job** (`:203-236`). The master's `ci.yml` rebase chain is #2078 → Lane F A3 → E-B; **E-D must be appended after E-B**, or the two regenerate-and-diff blocks conflict. Add E-D to that chain.
6. **NEW — E-D Task 11 and Lane D's PR B both edit `docs/design/fleet-b-route-inventory.md`.** Lane D PR B rewrites the stranded-route section wholesale (its Task 2 step 5); E-D Task 11 adds one line to it. **Land Lane D PR B first; E-D rebases.** Neither plan recorded this.
7. **Within E-A: 1 → 2 → {3, 4, 5} → 5a → 6, strictly.** Task 1 is the falsifiability precondition; Task 5a needs all five seams frozen before it can route them through one verdict; Task 6 is the only task that may add a `refresh()` caller, and an unfenced dispatch racing a refresh is exactly what decision 15 forecloses — so 5a precedes it.

**Parallelism.** E-A runs in parallel with everything — it touches no file any other lane touches and needs no restart. E-B, E-C and E-D are strictly sequential with each other and sit behind D-D in Track C of the close-out plan.

**Deploy notes.**
- **E-A** needs only a frontend rebuild. It changes command behaviour on the panel, roster, gallery and both drawers: after the rebuild, verify one action on a lane whose generation you have not changed still succeeds, before trusting the conflict path.
- **E-B** needs a restart and is the one to stage carefully: 7c replaces the bare-string refusals on `app/security/data_plane_control.py`, which is the guard every browser call to the data plane passes. A mistake there refuses everything. Verify `GET /api/broker-clerks` answers 200 with the control secret before issuing any command after that restart.
- **E-C** and **E-D** are frontend rebuilds. E-D additionally moves both generated contract files, so its CI gates (`export_openapi_contract.py --check` at `ci.yml:271-272`, `codegen:check` at `Frontend/package.json:18`) fail loudly if the regeneration was skipped.
- `./restart.sh` is an unconditional `podman compose down` (`restart.sh:21`); every restart drops both clerks and any running bot. Batch E-B's restart with the rest of Track C rather than restarting for it alone.

---

## 5. Risks the issue text missed

1. **The fence fails open, not closed — and Task 5a is the task that closes it.** A cold or failed directory yields `bindingGeneration: null`; `commandContextOf` omits the envelope field (`resource-target.ts:139-141`); and both backend checks are `is not None`-gated (`service.py:1283-1284`, `routing.py:328-329`). The command routed **unfenced**. `laneFenceIsEnforceable` (Task 2) named the state; no task originally consumed it, because refusing every command while the directory is cold is a policy change the owner had not been asked about. **Decision 15, answered 2026-09-14: refuse, loudly, client-side.** Task 5a consumes the predicate at all five command seams. The accepted cost is that a cold directory refuses commands until it warms.

2. **A fake that cannot change makes three green tests that prove nothing.** `provideFleetDirectory` (`fleet-directory-testing.ts:45-73`) closes over one immutable response, and 28 spec files depend on it. Any implementer who writes Task 3's test before Task 1 will see it pass on unmodified code and conclude the fence is already frozen. Task 1 is not housekeeping; it is the only thing that makes Tasks 3–5 falsifiable at all.

3. **Six command origins read the directory, not the two #2068 names — and one of them is a service feeding four more components.** `alpaca-desk-account-data.service.ts:21-32` is a page-level computed passed as `input()` into `alpaca-order-entry.component.ts:64`, `alpaca-sqlite-custody.component.ts:94`, `account-desk-transaction-history.component.ts:128` and `deploy-paper-access.component.ts:52`, each of which reads `this.target()` at interaction time. This lane does **not** fix those four: their target is an input, so the freeze belongs at the desk, and the desk's target is legitimately reactive to route changes. Task 6's contract spec will not flag them (they contain no `fleetDirectory.lane(` call), so the gap is invisible unless it is written down. **File it.**

4. **Every fleet 409 currently reads as "Unknown", including the fence's own refusal.** `deriveActionRejection` (`panel-action-outcome.ts:33-61`) takes `outcome` from a field only the panel's error body carries. Until Task 8 lands, an operator who trips the newly-working fence sees severity error and the word "Unknown" — which the daily-lifecycle work explicitly banned. **This makes E-C's ordering load-bearing, not cosmetic:** E-A ships a fence that fires and reads as an unexplained error. Say so in E-A's PR body.

5. **The same exception type has two wire shapes, and nothing tests that.** `broker_clerks.py:78-80` writes `error.detail()` flat; `internal_fleet.py:111-113` hands the same dict to `HTTPException`, which FastAPI nests under `detail`. `refusalBody` survives only because #2081 taught it both. Task 7c unifies them, and the snapshot test is the first thing in the repo that would notice them diverging again.

6. **Four hand-built refusal writers bypass `detail()` entirely**, one of them building JSON by string concatenation (`agent_identity.py:236-239`). They live on raw-ASGI middleware paths where no `Response` exists, and `lane_runtime.py` is also edited by Lane D's PR D-A. Task 7c deliberately excludes them. That means the vocabulary snapshot will declare codes that a hand-built writer can still emit with a different shape — the snapshot pins the *code*, not the *body*. **File the four writers as a follow-up** rather than letting the snapshot imply a guarantee it does not make.

7. **`broker-configuration.service.ts` is 17 HTTP calls behind one `laneUrl`, and the catalog has no operation to anchor its prefix.** A future reader will count "44 call sites migrated" and assume the whole surface is catalog-derived. It is not: the desk-configuration write path — the one that can change which account a lane serves — still builds its URLs by string concatenation from a prefix the catalog never declares. Task 10 states this in its commit message for exactly that reason.

8. **`alpaca-deploy-workflow.component.ts` is 957 lines**, close enough to the thermo-nuclear review's ~1k file-size alarm that Task 5's branch could tip it. Extract in the same commit if it needs more than the branch; do not defer.

9. **`toOperationError` is 60 lines of dead parser with 13 spec references.** Deleting it (Task 8) will look like unrelated scope to a reviewer. It is in scope because it is half of the third parser #2067 named, and leaving it means the next person to need an error parser finds two, picks the dead one, and re-creates the bug #2081 just fixed.

10. **The `broker-v2-vocabulary-contract` CI job becomes three regenerate-and-diff blocks** (broker-v2, refusal vocabulary, operation catalog) across #2078, E-B and E-D. It is the right home — same dependencies, same shape — but the rebase chain now has four members and a missed rebase silently drops a gate rather than failing. Name the chain in each PR body.

11. **Five `tests/broker/v2panel` failures are inherited and environment-only** (`POSTGRES_URL` empty on the host venv, `app/data_lake/catalog_client.py:73`). An implementer who runs that directory while validating E-B's snapshot pattern will see red that is not theirs. Establish the baseline first, per the testing rules, and surface it in the PR body.

---

### Critical files for implementation

- `/Users/inkant/learn-ai/Frontend/src/app/fleet/fleet-directory-testing.ts`
- `/Users/inkant/learn-ai/Frontend/src/app/fleet/fleet-directory.service.ts`
- `/Users/inkant/learn-ai/Frontend/src/app/fleet/resource-target.ts`
- `/Users/inkant/learn-ai/Frontend/src/app/fleet/clerk-scoped-url.ts`
- `/Users/inkant/learn-ai/Frontend/src/app/components/broker/v2-panel/panel-shell/bot-panel-shell.component.ts`
- `/Users/inkant/learn-ai/Frontend/src/app/components/broker/v2-panel/bots-list-page/bots-list-page.component.ts`
- `/Users/inkant/learn-ai/Frontend/src/app/components/broker/v2-panel/gallery/bot-gallery-page/bot-gallery-page.component.ts`
- `/Users/inkant/learn-ai/Frontend/src/app/components/broker/v2-panel/cohort-archive/cohort-archive-drawer.component.ts`
- `/Users/inkant/learn-ai/Frontend/src/app/components/broker/broker-deploy-page/alpaca-deploy-workflow.component.ts`
- `/Users/inkant/learn-ai/Frontend/src/app/components/broker/v2-panel/lib/panel-action-outcome.ts`
- `/Users/inkant/learn-ai/Frontend/src/app/components/broker/operation-error.ts`
- `/Users/inkant/learn-ai/Frontend/src/app/services/brokers.service.ts`
- `/Users/inkant/learn-ai/PythonDataService/app/broker/fleet/errors.py`
- `/Users/inkant/learn-ai/PythonDataService/app/routers/broker_clerks.py`
- `/Users/inkant/learn-ai/PythonDataService/app/routers/internal_fleet.py`
- `/Users/inkant/learn-ai/PythonDataService/app/security/data_plane_control.py`
- `/Users/inkant/learn-ai/PythonDataService/scripts/regenerate_broker_v2_vocabulary_snapshot.py` (the pattern to copy)
- `/Users/inkant/learn-ai/PythonDataService/tests/broker/v2panel/test_vocabulary_snapshot.py` (the pattern to copy)
