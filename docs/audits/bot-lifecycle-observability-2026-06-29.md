# Bot Lifecycle Observability — Audit & Research Brief for Codex

**Date:** 2026-06-29
**Scope:** Turn the bot lifecycle flowchart into a foolproof observability surface — clickable lifecycle nodes that reveal state, why, when, and what system evidence supports it, at both bot and account level.
**Status:** Read-only audit. No code written in the original audit. Implementation slices followed after review.

> **2026-06-30 correction:** Current implementation authority is
> `docs/bot-lifecycle-account-owner-authority.md`. Several audit line refs
> below were true when recorded but are now stale: submit/broker subgraph nodes
> are event-backed or `unknown`+reason, `writer_guard` is no longer hardcoded
> passed, AccountOwner generation/unresolved-exposure/restart-intensity V1
> artifacts are shipped, restart intensity is 3 starts / 5 minutes, lifecycle
> status labels are backend-authored, Angular lifecycle edges use ngx-vflow
> floating routing rather than bespoke handle inference, and timeline/receipt
> rendering is split into focused components. Treat the explicit correction
> notes in this document as superseding the original G3/G6 wording.

---

## Executive summary

The lifecycle feature is **better architected than "vibe-coded" implies on the authorship boundary, and worse than it looks on the observability boundary.**

1. **Authorship is clean, but the naming is a trap.** The "backend-authored bot lifecycle overview" is authored in **PythonDataService**, not the .NET Backend. The .NET Backend has **zero** bot / lifecycle / live-instance / activity / broker-order code. "Backend-authored" = Python-authored, served straight to Angular over FastAPI REST, bypassing .NET entirely. This is actually consistent with the authority rules (Python owns trading meaning) — but it means the .NET "owns persistence/identity/GraphQL" rule does **not** currently apply to bots at all. Decide deliberately whether that's intended.

2. **Angular is clean.** No lifecycle meaning is computed in Angular. The flowchart graph (nodes, edges, statuses, action-enablement, subgraphs) is rendered 1:1 from the backend `lifecycle_chart` payload. Only x/y layout and status→color theming are local; lifecycle labels and explanations are backend-authored. No P&L/stats/signals in TS. The remaining gray-area is visual theming off the canonical enum, not lifecycle authorship.

3. **There is no single canonical lifecycle state machine.** The "lifecycle" is a **visual projection composed from ~8 unrelated state vocabularies** (`RunState`, `HostProcessState`, gate status, reconciliation state, desired state, intent-event types, poison triggers, chart-node status). No enum enumerates the lifecycle states + legal transitions. Two transition graphs exist (chart edges vs. gate-map mermaid) and are **not isomorphic**.

4. **The durable evidence already exists — but it is fragmented and the lifecycle view ignores the richest source.** Python persists a real event trail: an fsynced **Intent WAL**, a **broker-callback WAL**, `decisions.parquet` / `executions.parquet` / `trades.parquet`, and ~6 sidecar/flag files. But the three display projections (`bot_lifecycle_chart`, `bot_catalog_projection`, `live_run_state`) derive state from `OperatorSurface` + filesystem scans and **never read the Intent WAL or intent ledger**. So "blocked because `INTENT_DROPPED_BEFORE_SUBMIT(max_orders_per_day)`" is durably recorded yet invisible in the lifecycle UI. **This is the core gap to close.**

5. **No unified per-bot/per-account event stream or timeline.** A human cannot ask "where is this bot and why, with timestamps and evidence." Today they'd hand-join the WAL + callbacks + 3 parquet files + 6 sidecars. The fusion (`compute_operator_surface`) exists only as a per-request snapshot — nothing emits a durable, queryable, ordered event log keyed by bot id / account id.

**The work is mostly a Python event-emission + projection-enrichment task, plus an Angular drill-down panel — not new .NET, and not new trading math.** Remaining risk areas: recovery-lane placeholder nodes are still static, decision rationale is captured for only one strategy (Gap-2), and the .NET Portfolio domain (separate from bots) carries timestamp-format and trading-math authority violations that this work should NOT entangle with but should note.

---

## Relevant files found (with line refs)

### Lifecycle authority sources (no single one — split across 5)
- `docs/bot-lifecycle-account-owner-authority.md` — registered canonical snapshot. Header says paper-only, R2; §10/changelog claims R3 artifacts shipped 2026-06-28 (**contradiction G6/G7**). Desired-state values stored uppercase `RUNNING/PAUSED/STOPPED` (`:83`); AccountOwner phases `accepting/reconnecting/draining/frozen` (`:93`); submit uncertainty states `ACK_FAILED_UNCERTAIN`/`SUBMIT_UNCERTAIN_HALTED` (`:104`).
- `PythonDataService/app/services/bot_lifecycle_chart.py` — **the backend-authored chart projection.** Original line refs are stale after issue #718 slices. Current chart facts include event-backed submit/broker subgraph nodes, node receipts/freshness, and explicit broker-activity vs AccountOwner-generation wording; only recovery placeholders such as `flatten` / `reconcile_after` / `fresh_run` remain static.
- `PythonDataService/app/schemas/live_runs.py` — chart contract + state vocabularies. `RunState` `:19-31`; `HostRunnerProcessState` `:244-250`; `HostProcessState` `:1070-1077`; `LifecycleChartStatus` `:1705-1713`; `LifecycleChartNode` (frontend-must-not-infer docstring) `:1728-1733`; `BotLifecycleChartView` `:1786`; `OperatorSurface` `:1636`.
- `docs/architecture/bot-lifecycle-gate-map.md` — richest transition model; supporting design context only. The inventory rows for account classifier, owner generation/fencing, unresolved exposure, and restart intensity were reconciled on 2026-06-30 to match the authority document.
- `docs/architecture/bot-lifecycle-account-owner-prd.md` — R3 AccountOwner future design.
- Registration: `docs/doc-authority.md:31`.

### Python engine / execution / broker (the evidence producers)
- Per-bar loop: `PythonDataService/app/engine/live/live_engine.py:788` (`run()`), main loop `:1001`, bar push `:1166-1170`, decision write `_maybe_write_decision` `:1425-1470`, `[BAR]` heartbeat `:1181-1186` (log-only).
- Process supervisor / exit codes: `PythonDataService/app/engine/live/run.py` (2949 lines).
- Strategy base + decision capture: `app/engine/strategy/base.py:213` (`Strategy`), `DecisionSnapshot` `:30-61`, `LoggedTrade` `:63-100`. Only SPY-EMA populates `last_decision_snapshot` (`spy_ema_crossover.py:289-296`) — **Gap-2**.
- Risk / gates: `pre_flight.py:613-621`; `post_halt_gate.py:42-68`; live pre-submit gates `live_portfolio.py:723-1097`; readiness `readiness.py:98-174`, verdict `:68-80`; sizing `execution/sizing.py:106-137`, `execution/order_sizer.py:252-330`.
- Order / fill / P&L: `execution/order.py:48-65`; `execution/fill_model.py:88-162`; `intrabar_resolver.py:41-75`; `portfolio.py:193-219` (`apply_fill`); `live_portfolio.py:704-722` (`record_broker_fill`), broker-authoritative refresh `:475-489`.
- Broker: `app/broker/ibkr/orders.py` `place_paper_order:307-405`, single `placeOrder` boundary `:451`, `stream_order_events:965-1027`; observe-only `no_submit_broker_adapter.py:73,127-156`; shadow fills `shadow_fill_simulator.py:64-97`; bar idempotency `app/broker/ibkr/bars.py:46`.
- State vocabularies: `desired_state.py:74-78`; `intent_events.py` `IntentEventType:36-67`, `DropReason:27-33` (**the "why blocked" causes**); `halt.py` `PoisonedHaltTrigger:58-96`, first-halt-wins `:144-167`; `submit_state_machine.py:56-92`; `engine_runtime.py` block Literals.
- **Durable evidence sinks:** `intent_wal.py:58-129` (fsynced JSONL, PENDING before placeOrder); `broker_callbacks.py:58-75` (fsynced); `artifacts.py` (`decisions.parquet` cols `:52-80`, `executions.parquet`, `trades.parquet`); sidecars `readiness.json`, `engine_runtime.json` (`engine_runtime_publisher.py`, 1Hz), `daemon_lease.json`, `desired_state.json`, `run_status.json`, `halt.flag`, `poisoned.flag`, `operator_incidents/*.json`; identity `run_ledger.json`.
- **Keystone fusion point:** `app/services/operator_surface.py:885` `compute_operator_surface(...)` — the single pure composition the router blends everything into. **Does not read Intent WAL/ledger** — Gap-3.
- Other projections: `services/bot_catalog_projection.py:23-55`; `services/live_run_state.py:105-189` (`infer_state` from filesystem).
- Router wiring: `app/routers/live_instances.py` — status+chart `:1166`, list `:1000`, catalog `:1176`, WAL sizing fold `:707`.

### Angular (visualization)
- Flowchart: `Frontend/src/app/components/broker/bot-control/overview-tab/overview-tab.component.{ts,html,scss,spec.ts}`. Chart read `:57`; node/edge build `:76-112`; layout `:32-42,183-195`; expand/collapse `:114-124`; status→label `switch` `:140-157` (**gray-area a**); status→color `switch` `:159-176` (**gray-area b**). Clickable only when backend marks `expandable:true` + `subgraph_id` (html `:33-38`).
- Action panel: `.../overview-tab/overview-actions.component.ts:21-24` (emits only when `action.enabled`).
- Host page (polls every 4s): `bot-control-page.component.ts` poll `:33`, fetch+set `:287-293`, action routing `:216-244`, banner derive `:109-123` (**gray-area c**).
- Catalog: `Frontend/src/app/components/broker/bots/bots-page.component.ts` (filters on backend `needs_attention` `:69-70`).
- Contract: `Frontend/src/app/api/live-instances.types.ts` (`BotLifecycleChartView` `:169,227`; `LifecycleChart*` `:112-141`; `BotCatalogRow` `:824-846`).
- Service: `Frontend/src/app/services/live-runs.service.ts` — status `:156`, catalog `:151`, account-summary `:267`, mutations `:165-260`. **All REST. No GraphQL for bots.**
- Display formatters only: `Frontend/src/app/components/broker/format.ts:41-42`.

### .NET Backend (NOT involved in bots; separate Portfolio domain with violations)
- No bot/lifecycle/activity code anywhere in `Backend/` or `Backend.Tests/`.
- Portfolio domain (unrelated to bots): `Backend/Models/Portfolio/*`, `Backend/Data/AppDbContext.cs:41-42,279-337`, `Backend/GraphQL/PortfolioQuery.cs`.

---

## Risks / gaps

### Authority-rule violations (flag, mostly out-of-scope but must be acknowledged)
- **V1 — .NET Portfolio computes trading math.** `PortfolioValuationService.cs:79-105` (marketValue/unrealizedPnL/equity), `PortfolioRiskService.cs:60,107,188` (dollar-delta/vega aggregation), `SnapshotService.cs:62-88` (Sharpe/Sortino/Calmar/MaxDD — self-flagged `legacy-ok-pending-parity`, finding F-0011), `StrategyAttributionService.cs:188-223` (PnL/winRate/alpha), `PositionEngine.cs` (FIFO realized PnL). **Violates "Backend must not compute trading math."** Out of scope for lifecycle observability but do **not** route any new bot math through .NET.
- **V2 — .NET Portfolio timestamp-format violations (`DateTime`, not int64 ms UTC).** `Account.cs:22`, `Order.cs:29-30`, `Position.cs:26-28`, `PositionLot.cs:18-19`, `PortfolioTrade.cs:31`, `PortfolioSnapshot.cs:10`, `StrategyAllocation.cs:16-17`, `RiskRule.cs:16`, `ValidationResult.cs:6-7`, and the wire boundary `PortfolioQuery.cs:100-101` (`getEquityCurve(DateTime?...)`). Newer DataLake tables are compliant (`AppDbContext.cs:493,508`). Python live-runs is fully compliant. **If any account-level observability is to be served through .NET, this must be fixed first or it propagates the violation.**
- **V3 — Orders persisted but not queryable in .NET.** `DbSet<Order>` exists (`AppDbContext.cs:42`) with no resolver. Dead observability surface.

### Lifecycle-model gaps / contradictions
- **G1 — No canonical lifecycle state enum or single state machine.** `LifecycleChartStatus` is a node-color vocabulary, not lifecycle states. ~8 vocabularies coexist.
- **G2 — Two non-isomorphic transition graphs.** Chart edges (`bot_lifecycle_chart.py:95-104`) collapse the gate-map mermaid's `accountGate/runLedger/startGate/daemon/morning/process` into `deploy`+`preflight`, and render `poison/freeze/blocked/paused` as node *statuses* whereas the gate-map models them as terminal *nodes*. No cross-reference.
- **G3 — Corrected 2026-06-30.** The original audit found hardcoded submit/broker subgraph nodes. Current implementation projects `signal` / `intent_wal` / `place_order` / `ack_or_reconcile` / `broker_ack` from lifecycle events or renders `unknown` with server-authored reasons, and `writer_guard` renders AccountOwner generation/phase evidence or an explicit R2 limitation. Remaining static placeholders are in the recovery lane (`flatten`, `reconcile_after`, `fresh_run`).
- **G4 — `active` has no real entry guard.** `_primary_node_id` (`:489-503`) marks `active` only when `host_process.state=="RUNNING"` AND desired=="RUNNING"; otherwise silently falls back to `activate` even with all gates passed.
- **G5 — Desired-state casing fork.** Durable compare uppercase `"RUNNING/PAUSED/STOPPED"` vs operator-action enum lowercase `pause/resume/stop`; `DesiredStateView.state: str|None` unconstrained → a casing/typo silently reads as `unknown` instead of failing.
- **G6 — Corrected 2026-06-30.** The gate-map inventory now marks account-classifier V1, owner generation/fencing V1, unresolved-exposure freeze, and restart-intensity fold as shipped where the authority doc says they are shipped. It still calls out the future R3 daemon/IPC and unified account gate board gaps.
- **G7 — R2/R3 self-contradiction** within the authority doc (header "not R3" vs §5.1/changelog describing shipped AccountOwner lane).
- **G8 — Submit uncertainty state machine invisible.** `submit_state_machine.py` outcomes (`ACK_FAILED_UNCERTAIN`, `SUBMIT_UNCERTAIN_HALTED`, adopt/recover/halt) and the gate-map's submit branches never reach the chart's binary `submit_order→broker_writer` edge.

### Observability gaps (the actual product gap)
- **Gap-1 — No unified, durable, queryable per-bot event stream.** Timeline must be reconstructed by joining Intent WAL + broker_callbacks.jsonl + 3 parquet + 6 sidecars. Fusion is per-request only.
- **Gap-2 — Decision rationale captured for one strategy only.** `last_decision_snapshot` populated only by SPY-EMA; SMA/RSI/buy-and-hold emit ENTER/EXIT/HOLD with no snapshot.
- **Gap-3 — Projections ignore order-intent truth.** `bot_lifecycle_chart`/`bot_catalog_projection`/`live_run_state` never read Intent WAL/ledger → the durably-recorded "why blocked / why halted at submit" never surfaces.
- **Gap-4 — Backtest execution layer is silent.** `order.py/fill_model.py/portfolio.py/sizing.py/order_sizer.py` compute-and-mutate with zero logging/events; any backtest-path observability must instrument the engine loop.
- **Gap-5 — No account-level activity history surface.** Broker fills/positions are broker-authoritative in Python (`live_portfolio.py`) but there is no account-keyed, ordered "trades placed by broker" history endpoint for the UI.
- **Latent — `live_portfolio.py:607/633/689`** do `int(ts.timestamp()*1000)` assuming inbound tz-aware; validate before surfacing.

---

## Observability matrix (per lifecycle state)

For each state: what evidence should exist → where stored → owner → logged today? → queryable by bot/account id? → int64 ms? → frontend-displayable without local meaning?

| State | Evidence | Storage | Owner | Logged today | Queryable by id | int64 ms | FE-safe |
|---|---|---|---|---|---|---|---|
| **active** | RunState.running, readiness READY, CommandLoopBlock=RUNNING, bar heartbeat | readiness.json, engine_runtime.json, run_status.json | Python | ✅ persisted | ⚠️ per-request only, not as event | ✅ | ✅ |
| **blocked** | readiness BLOCKED + `INTENT_DROPPED_BEFORE_SUBMIT` + `DropReason` | readiness.json + **Intent WAL** + operator_incidents | Python | ✅ persisted | ❌ WAL not surfaced (Gap-3) | ✅ | ❌ until projection reads WAL |
| **stopped** | DesiredState.STOPPED, RunState.stopped, ExitReason | desired_state.json, run_status.json | Python | ✅ persisted | ⚠️ snapshot only | ✅ | ✅ |
| **errored** | ExitReason.exception/fatal_halt, CommandLoopBlock=FAILED, poisoned.flag, PoisonedHaltTrigger | poisoned.flag/halt.flag, run_status.json, engine_runtime.json | Python | ✅ persisted | ⚠️ snapshot only | ✅ | ✅ |
| **awaiting-broker** | BrokerBlock.connection_state/submission_capability/reconnect_attempt/probe_completed_at_ms, daemon lease | engine_runtime.json, daemon_lease.json, readiness.json | Python | ✅ persisted | ⚠️ snapshot only | ✅ | ✅ |
| **awaiting-data** | BarLoopBlock.heartbeat_at_ms vs latest_source_bar_ms; RunState.waiting_for_bars/warming_up/stale | engine_runtime.json bar-loop block | Python | ✅ (the `[BAR]` warmup log itself transient) | ⚠️ snapshot only | ✅ | ✅ |
| **decision (per-bar)** | DecisionSnapshot (ema/rsi/signal/intended_price) | decisions.parquet | Python | ⚠️ SPY-EMA only (Gap-2) | ❌ not exposed per bot timeline | ✅ | ❌ until exposed |
| **order lifecycle** | IntentEventType (PENDING→SUBMITTED→ACK/FILL/DROP/HALT) | Intent WAL + broker_callbacks.jsonl + executions.parquet | Python | ✅ persisted | ❌ not surfaced (Gap-1/3) | ✅ | ❌ until exposed |

**Conclusion:** the *evidence* is ~90% there and timestamp-compliant. The gaps are **exposure and ordering**, not capture — except decision rationale (Gap-2) and backtest path (Gap-4).

---

## Proposed canonical event/snapshot model

**Principle:** do not invent a new parallel event system. The Intent WAL + broker-callback WAL + decision/execution parquet already ARE the events — they just aren't projected into a single ordered, typed, bot/account-keyed stream. Introduce **one read-side projection** (`BotEvent`) that folds existing sinks, and fill only the genuinely missing emissions (lifecycle transitions, account balance/position deltas) at the points that already mutate that state.

Each event: `{ event_id, bot_id (strategy_instance_id), account_id, ts_ms (int64 UTC), type, lifecycle_node (optional), severity, summary, evidence_ref (file+offset / parquet row / WAL seq), payload }`.

| Event | Source today | Action |
|---|---|---|
| `BotLifecycleTransitioned` | derived per-request only; no transition record | **Emit** on primary-node change (the missing durable transition log) |
| `BotBlocked` | readiness BLOCKED + Intent WAL `INTENT_DROPPED_BEFORE_SUBMIT`+`DropReason` | **Project** from existing |
| `BotStopped` | desired_state.json STOPPED + run_status.json | **Project** |
| `BotErrored` / `BotPoisoned` | poisoned.flag + PoisonedHaltTrigger + ExitReason | **Project** |
| `BotDecisionEvaluated` | decisions.parquet (SPY-EMA only) | **Project** + **fill Gap-2** (populate `last_decision_snapshot` for SMA/RSI/B&H) |
| `RiskCheckPassed` / `RiskCheckFailed` | pre_flight CheckResult, readiness gates | **Emit** structured per-gate result (currently log-only at boundaries) |
| `BrokerOrderRequested` | Intent WAL `PENDING_INTENT` | **Project** |
| `BrokerOrderPlaced` | Intent WAL `SUBMITTED` / IbkrOrderAck | **Project** |
| `BrokerOrderRejected` | `INTENT_NOT_ACCEPTED` / OrderEventType="error" | **Project** |
| `BrokerOrderFilled` | broker_callbacks.jsonl fill / executions.parquet | **Project** |
| `BrokerOrderCancelled` | OrderEventType="cancel" | **Project** |
| `BrokerOrderUncertain` | `ACK_FAILED_UNCERTAIN`/`SUBMIT_UNCERTAIN_HALTED` (submit_state_machine) | **Project** (closes G8) |
| `AccountBalanceChanged` | live_portfolio cash mutation (no event) | **Emit** delta on broker refresh / fill |
| `PositionChanged` | apply_fill / record_broker_fill (no event) | **Emit** delta |

Owner: **PythonDataService** for all (it owns the meaning). Storage: append to a per-run `events.jsonl` (fsynced, same pattern as Intent WAL) as the durable ordered log, OR a read-side fold exposed via the router — recommend the durable log so the timeline survives restart and is replayable. Keep .NET out unless a deliberate decision is made to put bot persistence/identity there (see Open Questions).

---

## Proposed API/query shapes (FastAPI, consistent with current REST surface)

All under `/api/live-instances`. Keep REST (bots already are; GraphQL is portfolio-only). Snake_case, int64 ms.

- **Current snapshot:** `GET /{bot_id}/lifecycle/snapshot` → `{ primary_node, node_statuses[], desired_state, readiness_verdict, blockers[], evidence_summary }` (extend existing `/status`).
- **Lifecycle timeline:** `GET /{bot_id}/lifecycle/timeline?since_ms=&until_ms=&types=` → `BotEvent[]` ordered by `ts_ms`.
- **Bot activity history:** `GET /{bot_id}/activity?since_ms=&kinds=decision,order,risk` → folded `BotEvent[]` (decisions + orders + risk).
- **Account activity history:** `GET /accounts/{account_id}/activity?since_ms=` → account-scoped `BotEvent[]` (balance/position/broker fills).
- **Broker order history:** `GET /accounts/{account_id}/orders?status=&since_ms=` → ordered order-lifecycle events (request→place→ack→fill/reject/cancel), int64 ms.
- **Node detail (for the clickable chart):** `GET /{bot_id}/lifecycle/node/{node_id}` → `{ node_id, status, current_summary, why, since_ms, transitions[], related_events[], evidence_refs[] }` — server-authored explanation, so Angular renders verbatim.

---

## Proposed frontend UX

The flowchart stays the navigation surface (`overview-tab.component`). Make **every** node clickable (not just `expandable` subgraph nodes), opening a **node-detail panel** (new child component, e.g. `lifecycle-node-detail.component`) that renders, all backend-authored:
- **Current state summary** — node `status` + server `current_summary` + `why` string (no local switch authoring the reason).
- **Timeline entries** — `GET /{bot_id}/lifecycle/node/{id}` `related_events[]`, rendered as an ordered list (server provides label + ts_ms; Angular only formats the ms→NY display string, per the int64-ms display-boundary rule).
- **Related broker/account events** — for submit_order/broker_writer/recovery nodes, render backend-authored order-lifecycle and account-event rows. Submit/broker nodes are no longer hardcoded; recovery-lane placeholders still need richer evidence.
- **Decision inputs/outputs** — for `active`, the latest `BotDecisionEvaluated` (indicators in, signal out).
- **Blockers/errors** — `blockers[]` with `DropReason`/`PoisonedHaltTrigger` (server-authored text).
- **Raw evidence references** — `evidence_refs[]` (WAL seq / parquet row / sidecar file) for the debugging human; render as references, not parsed locally.

Account-level: a sibling account-history view (catalog → account drill-in) backed by `/accounts/{id}/activity` and `/accounts/{id}/orders`.

**Constraints to hold:** keep `statusLabel`/`edgeColor` (`overview-tab.component.ts:140-176`) as pure theming off the canonical enum — do NOT add new derived verdicts; move operator *wording* to the server payload if product wants it canonical (gray-area a). Keep `bot-control-page.component.ts:109-123` banner derivation under review (gray-area c).

---

## Proposed tests

- **Python (pytest, the bulk):**
  - Event-projection fold: given a fixture run dir (Intent WAL + broker_callbacks + parquet + sidecars) → assert exact `BotEvent[]` ordering, types, and `ts_ms` monotonicity.
  - Regression per state: blocked (`INTENT_DROPPED_BEFORE_SUBMIT`+`DropReason`), stopped, errored/poisoned (`PoisonedHaltTrigger`), awaiting-broker, awaiting-data — each asserts the snapshot + emitted events. **These are the foolproofing regression tests.**
  - Gap-2: SMA/RSI/buy-and-hold now populate `DecisionSnapshot` — parity test that decision rationale is captured for every strategy.
  - Submit uncertainty (G8): `submit_state_machine` outcomes surface as `BrokerOrderUncertain` events.
  - **Timestamp boundary tests:** every event field is int64 ms UTC; validate `live_portfolio.py:607/633/689` tz-aware assumption; ban-list grep stays clean.
  - New endpoints: `httpx.AsyncClient` + `ASGITransport`, assert query-by-bot-id / account-id, `since_ms` filtering, ordering.
- **Frontend (Vitest + Testing Library):** node-detail panel renders backend `why`/timeline verbatim; clicking a node fetches + shows the panel; assert NO new local meaning (status/label/color stay off the enum). Account-history view renders server events.
- **.NET:** none required unless Open Question routes account history through .NET (then: resolver + int64-ms migration tests).
- **Math touched?** Only if Gap-2 changes decision capture — if any indicator/signal math is touched, golden-fixture + tolerance-pinned parity test per numerical-rigor rules. Pure event projection touches no math, so no new fixtures unless math moves.

---

## Staged implementation plan (PR-sized)

1. **PR-1 — Audit + authority reconciliation (docs only).** Resolve G1/G2/G6/G7: declare ONE canonical lifecycle state set + transition table; mark gate-map superseded or reconcile it; fix R2/R3 wording; document desired-state casing contract (G5). Output: updated `docs/bot-lifecycle-account-owner-authority.md` + a single state-machine table. No code.
2. **PR-2 — Canonical `BotEvent` schema + fold (read-side, no behavior change).** Add `BotEvent` Pydantic model + a projection that folds existing Intent WAL + broker_callbacks + parquet + sidecars into an ordered, bot/account-keyed stream. Pure read; fully testable against fixtures. Closes Gap-1/Gap-3 at the data layer.
3. **PR-3 — Durable lifecycle-transition + balance/position emission.** Emit `BotLifecycleTransitioned`, `AccountBalanceChanged`, `PositionChanged`, and structured `RiskCheckPassed/Failed` at the points that already mutate that state (engine loop, live_portfolio, gates). Append to fsynced `events.jsonl`. Fills the genuinely-missing events.
4. **PR-4 — FastAPI query endpoints.** Snapshot, timeline, bot activity, account activity, broker order history, node detail. Server-authored `why`/summary strings.
5. **PR-5 — Fix chart truthfulness (G3/G4/G8).** Submit/broker truthfulness is now partially shipped; remaining work is recovery-lane evidence, richer `active` entry rationale, and continued submit-uncertainty coverage.
6. **PR-6 — Decision rationale backfill (Gap-2).** Populate `last_decision_snapshot` for SMA/RSI/buy-and-hold; parity test.
7. **PR-7 — Angular node-detail panel + account-history view.** Clickable nodes → backend-authored detail; account drill-in. Hold the no-local-meaning line.
8. **PR-8 — Test hardening.** Per-state regression suite, timestamp-boundary tests, endpoint tests, FE component tests.
9. **PR-9 (optional, gated by Open Question) — .NET account-history passthrough.** Only if account observability is to live in .NET; requires int64-ms migration of Portfolio models first (V2) and strict no-math passthrough.

---

## Open questions for the user (decide before PR-2)
1. **Where should bot/account observability persistence and identity live?** Today it's 100% Python filesystem/WAL, zero .NET. The rules say .NET owns persistence/identity/GraphQL. Is the Python-only model the deliberate architecture, or should a durable, DB-backed, GraphQL-queryable event store sit in .NET (Python emits, .NET persists/serves)? This decides PR-4 (FastAPI) vs PR-9 (.NET GraphQL).
2. **Is a single canonical lifecycle state machine in scope** (PR-1), or do we keep the projection-only model and just make it observable?
3. **Backtest-path observability (Gap-4)** in scope, or live-trading only?
4. **Should operator-facing wording (status labels) move server-side** (gray-area a), or stay as Angular theming?

---

*Do not assume things are correct because the UI renders: the chart's submit/broker lane now renders event-backed facts or unknown reasons, but timeline/projection rows still need source-provenance review, and some "blocked" causes that are durably recorded in artifacts remain outside the visible operator path.*

---

# Reconciliation v2 (2026-06-29) — after codex review + user decisions

This supersedes the "Proposed event model", "API shapes", and "Staged plan" sections above where they conflict. The audit's diagnosis stands; the **first implementation slice is narrowed to projection-first**.

## Corrections accepted (my audit missed these)
- **`GET /api/live-instances/{id}/activity` already exists** (`live_instances.py:2713`, `LiveInstanceActivityProjection`) — the canonical, backend-owned projection for broker activity, orders-today blotter, fill markers, and raw IBKR evidence, with an explicit invariant that the chart cannot show a fill absent from the activity table. Consumed by `Frontend/.../bot-control/tabs/activity-tab.component.ts`. **Reuse, do not rebuild.** The earlier "broker order history" endpoint proposal collapses into "extend/reference `/activity`."
- **`account_events.jsonl` already exists and is durable** (fsynced + file-locked + atomic, `account_artifacts.py:571`), but is **untyped**: `read_account_events` returns raw `dict[]` (`:259`), `append_account_event` accepts arbitrary payloads with no required `ts_ms`, no sequence (`:271`). **Harden into a typed schema before exposing as an account timeline** — do not treat it as ready.
- **"No unified event stream" is directionally right but the fix is fold/normalize, NOT a new `events.jsonl`.** A parallel durable log would drift against Intent WAL / Broker Activity WAL / parquet / account artifacts. **Build a typed read-side projection over the existing sources; add new durable writes only for the one genuinely-missing primary fact (lifecycle transitions), and only at authoritative mutation points.**

## User decisions (locked)
1. **DB-backed persistence/identity is desired but not yet achieved** → keep this work on Python/FastAPI now; design the typed read models so a future DB (and possibly .NET passthrough) can persist them verbatim. **No .NET detour in this work** (also codex's guard).
2. **A single canonical lifecycle state machine is wanted but does not exist** → docs reconciliation (Phase 0) is in-scope and is the foundation.
3. **Scope priority: live-trading algorithms; orders must be traceable without inconsistency** → prioritize the live path; **defer backtest-path observability (Gap-4)**; add an explicit order/fill *consistency* invariant (below).
4. **Calculations AND verbiage server-side** → move operator wording (status labels, node summaries, "why" text, blocker reasons) into the backend payload. Angular stops authoring labels (`overview-tab.component.ts:140-157` switch becomes a consumer of server text). Status→color (`:159-176`) may remain as pure theming off the canonical enum.

## Revised, projection-first staged plan

- **Phase 0 — Docs reconciliation (no code).** Make the authority doc name the canonical lifecycle node ids, gate ids, and the single transition table (resolve G1/G2). Demote or update the gate-map so stale "Proposed" rows aren't read as truth (G6/G7). Lock the desired-state casing contract (G5: constrain `DesiredStateView.state` to an enum; one canonical case). For each canonical node, name the existing source that backs it.
- **Phase 1 — Typed read models + fold (read-only, no new writes).** Define `BotLifecycleEvent`, `LifecycleNodeDetail`, and a typed `AccountEvent`. Fold existing sources — Intent WAL, the `/activity` Broker Activity projection, `decisions/executions/trades.parquet`, account artifacts, and `OperatorSurface` — into an ordered, bot/account-keyed stream exposing `ts_ms` (int64 UTC), `source`, `evidence_ref`, `node_id`, `severity`, and **server-authored** `summary`/`why`. This closes Gap-1/Gap-3 at the data layer with zero behavior change. These models double as the future DB schema (decision 1).
- **Phase 2 — Harden account events.** Add a typed schema, required `ts_ms`, and a monotonic `seq` to the account-event write/read path (`account_artifacts.py:259/271/571`) before exposing account history. Hardening existing writes — not a new log.
- **Phase 3 — Fix chart truthfulness (G3/G4/G8).** Submit/broker subgraph statuses are now projection-backed or `unknown`+reason, and submit-uncertainty reaches `ack_or_reconcile`. Remaining work: replace recovery placeholders with evidence-backed facts and keep improving the server-authored reason for the `active`-vs-`activate` decision.
- **Phase 4 — Server-authored verbiage (decision 4).** Move status labels / node summaries / blocker reasons into the backend payload; Angular renders verbatim.
- **Phase 5 — Endpoints (bounded, paginated, side-effect-free GET).** `GET /{id}/lifecycle/node/{node_id}`, `GET /{id}/lifecycle/timeline?since_ms=&limit=`, `GET /api/live-instances/accounts/{account_id}/activity?since_ms=&limit=`. Reuse `/{id}/activity` for order/fill/broker rows rather than duplicating.
- **Phase 6 — Lifecycle-transition emission (the only genuinely-new durable write).** Emit `BotLifecycleTransitioned` **only at authoritative mutation points** — desired-state writes, AccountOwner submit results, freeze/clear, halt/poison, reconciliation outcomes, engine readiness transitions — **with debounce**. **Never from GET/status polling** (the bot control page polls every 4s; reads stay pure). Prefer reconstructing transitions from existing durable trails where possible; persist a typed transition record only where no history exists today, co-located with run artifacts (not a new global log).
- **Phase 7 — Angular drill-down.** Every chart node clickable → node-detail panel rendering server-authored text + event rows verbatim; no local lifecycle reasoning. Reuse `activity-tab` patterns.
- **Phase 8 — Tests (live-first, consistency-first).** Fixture-heavy Python proving `blocked / stopped / errored / awaiting-broker / awaiting-data / submit-uncertain / account-frozen / AccountOwner submit+reconnect`. **Order-consistency invariant test** (serves decision 3): no order/fill appears in `lifecycle/timeline` that is absent from `/activity`, and vice versa — extending the existing chart↔activity guarantee. Timestamp-boundary tests (every event field int64 ms UTC; validate the `live_portfolio.py:607/633/689` tz-aware assumption). Frontend tests prove node-click renders backend text verbatim with no derived verdicts.

## First slice recommendation
Phases 0 → 1 → 2 are the safe, high-value opening: reconcile the canonical state machine, build the typed read-side fold over existing durable sources, and harden account events — all read-only/hardening, no new behavior, no .NET, no GET-side writes. Everything visible to the operator (Phases 3–7) layers on top once the projection is trustworthy.

---

# Reconciliation v3 (2026-06-29) — pre-implementation refinements (accepted, specs locked)

Seven refinements accepted and made concrete so they're settled before code.

1. **Phase 1 reads via a tolerant adapter; Phase 2 hardens forward writes only.** `raw account_events.jsonl → AccountEventProjection` tolerates missing `seq`/`ts_ms`. **No backfill** of historical events — old rows are never required to gain `seq`/canonical `ts_ms` retroactively.

2. **Deterministic timeline ordering (defined now).** Sort key is the tuple `(ts_ms ASC, source_rank ASC, source_local_seq ASC)`. `source_rank` is a fixed, documented table used *only* as a same-millisecond tie-break (not a causality claim). Representative ranks: `decision=10, risk_gate=20, intent_pending=30, submit=40, broker_ack=50, fill=60, position_change=70, account_balance=80, freeze/halt/poison=85, lifecycle_transition=90`. `source_local_seq` = WAL line seq / parquet row index / account-event `seq`. Ordering is covered by a unit test with colliding `ts_ms` across sources.

3. **Absent evidence renders as `unknown` + a server-authored reason.** Do **not** introduce `not_available`; keep `LifecycleChartStatus` at its stable 7 values (`passed, active, blocked, poison, freeze, inactive, unknown`, `live_runs.py:1705-1713`). Projection-backed chart nodes resolve to real status or `unknown`+reason when evidence is absent. If a distinct value is ever justified later, it ships with frontend theming + label tests.

4. **Consistency invariant scoped.** "No order/fill in the lifecycle timeline absent from `/activity`, and vice versa" applies only to: same `bot_id`, same session/date window, and event types `{order, fill}`. The timeline's `decision / risk_gate / freeze / desired_state / lifecycle_transition` rows are explicitly **outside** this invariant.

5. **Account-event `ts_ms` normalization (migration semantics).** Reader derives canonical `ts_ms` by precedence: explicit `ts_ms` → domain field in fixed order (`recorded_at_ms` → `created_at_ms` → `approved_at_ms` → `cleared_at_ms` → …) → if none, set `ts_ms_resolved=false`, order by file-append position, and **surface the gap** (do not fabricate a timestamp — consistent with the repo's fail-fast timestamp posture). Phase 2 makes `ts_ms` + `seq` required on new writes, so `ts_ms_resolved=false` only ever appears for legacy rows.

6. **Performance acceptance criterion.** `lifecycle/node/{id}` and `lifecycle/timeline` must be **bounded**: `since_ms` + `limit` (default limit, hard max), lazy per-source reads, no full scan of all run directories / full parquet / full WAL per click. A small projection service caches source handles. Acceptance bar to confirm against real data: node-detail **p95 < ~300 ms warm, < ~1 s cold**; reject any design that is O(all history) per request.

7. **Server-authored verbiage in the schema.** Add `status_label`, `summary`, `why`, and `operator_next_step` to the node/event models. Angular deletes the `statusLabel` switch (`overview-tab.component.ts:140-157`) and renders these verbatim — no wording invented client-side anywhere else.

**First slice unchanged:** Phase 0 (docs reconciliation) → Phase 1 (typed projection over existing sources, tolerant adapter) → Phase 2 (account-event hardening) → Phase 3 (chart truthfulness). Keep the system honest before making the UI more confident-looking.
