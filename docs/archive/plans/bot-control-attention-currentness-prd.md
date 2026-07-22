> **Status:** Archived / superseded (2026-07-22).
> **Do not use as implementation authority or an operator procedure.**
> **Current authority:** `docs/bot-control-operator-manual.md`, ADR-0030, ADR-0026, and `docs/architecture/engine-authority-map.md`.
> **Archived because:** This point-in-time UI plan is retained only as provenance.

# PRD: Bot Control Attention Currentness and Proof Alignment

**Status:** Draft after investigation; initial implementation slice landed  
**Owner:** Inkant  
**Created:** 2026-07-01  
**Surface:** `/broker/bots/:id` bot control panel, especially the header posture pills and Attention dropdown  
**Related:** ADR 0013 (judgment vs evidence), ADR 0014 (backend-rendered narratives), ADR 0015 (operator notices), PRD #607, PRD #616, PRD #619-A/B/C/D, PRD #718

---

## Implementation Update: 2026-07-01

Initial slice implemented:

- `operator_surface.broker.safety_verdict` and `operator_surface.broker.connection` now prefer fresh child `engine_runtime.json` broker proof for a bound running process before falling back to readiness/data-plane evidence.
- The deploy form now exposes **Signal stream** separately from Action plan and submits it as `live_config.symbol`.
- The deploy form now labels the readonly toggle as **Execution capability: READ-ONLY OBSERVATION / PAPER ORDERS ENABLED** and requires confirmation before paper-order submission.
- The Activity tab lays out Trade chart + Orders Today as a desktop two-column monitoring row, with Broker Activity and Recent Incidents in the same surface.
- Orders Today now uses the compact columns: Time, Symbol, Position, Side, Order, Average fill, Filled.

Still not implemented from this PRD:

- Split bot-owned exposure from account-level exposure in the `current_risk` contract.
- Add the explicit multi-lane `broker_proof` object and source timestamps to the public operator-surface schema.
- Replace every trader-facing "Degraded" label with condition-specific copy while preserving backend enum compatibility.

## 1. Problem

The bot control panel can show attention items that are not current:

- The broker sidebar can show **Paper connected**, while the bot Attention dropdown still says the broker is disconnected or unknown.
- The host daemon/control plane can be running, while the bot surface implies the live daemon or runtime proof is missing.
- `Submit` can read **Broker state unproven** after the operator has connected the broker and the backend has enough fresh account evidence elsewhere.
- `Exposure` can read **Unknown** even when the backend has a connected account and a current account-level position snapshot.
- The deploy UI has no explicit **Signal stream** field, so the signal/data symbol is implicit in the selected strategy settings file while the traded asset is selected in the Action plan.
- The deploy UI buries execution capability under Launch options -> Mode, and the current Paper/Live wording is backwards-dangerous: internally Paper means `readonly=true`, while Live means `readonly=false`.
- The live monitoring activity surface is too wide for the current bot side-panel / half-page layout. Orders Today currently spends nine columns on time, symbol, side, order quantity, filled, average fill, type, status, and position effect; the operator needs a compact table that fits next to the price/trade chart.
- Recent incidents are useful while monitoring, but they are not consistently present in the Recent activity / broker activity side panel where the operator is already watching orders, broker events, and chart movement.
- The word **Degraded** is overloaded. It can mean a serious control-plane risk, a missing live-readiness sidecar, a closed-market bar loop, or a temporary broker data-farm condition. These are not the same trader situation.

The current UI mostly renders backend fields verbatim, so the fix is not a frontend copy patch. The fix is to make the backend's `operator_surface` consume the freshest authoritative event/proof lane for each attention item, and to expose enough provenance for the trader to understand what is proven, what is unknown, and why.

## 2. Live Investigation Snapshot

Observed on `SPY-SIG-AAPL-ASSET-20260629` at `2026-07-01` via local endpoints:

- `GET /api/broker/health` returned `connected=true`, `connection_state=connected`, `account_id=DUM284968`, `is_paper=true`, and safety `final_verdict=paper-only`.
- `GET /api/live-instances/.../daemon-health` returned `ok=true`, active process `state=running`, the same bot id, and `lease_status=CONNECTED`.
- The bot status returned `process.state=running`, `host_process.state=RUNNING`, and `control_plane.state=CONNECTED`.
- The run's `engine_runtime.json` had fresh broker evidence: `broker.connection_state=connected`, `identity=PAPER_VERIFIED`, `submission_capability=PAPER_ORDERS_ENABLED`, `connected_account=DUM284968`, and fresh probe timestamps.
- The bot status still returned `operator_surface.broker.connection=UNKNOWN`, causing an Attention item `broker_connection` with "Connection is UNKNOWN."
- `operator_surface.current_risk.posture=UNKNOWN`, because per-bot risk is read from `live_state.json`, and this run has no `live_state.json` yet.
- `readiness.kind=start_readiness`, `verdict=DEGRADED`, because `readiness.json` is absent and the backend fell back to start-readiness even though the daemon reports a running process and `engine_runtime.json` is fresh.

Primary root cause: `operator_surface.broker.connection` is derived from `_broker_connection_state_from_readiness(readiness)`. When `readiness.json` is absent, `_resolve_readiness()` falls back to backend-derived `start_readiness`, which has no `broker_connection` gate. The backend already has fresher broker connection evidence in `engine_runtime.json` and in the FastAPI singleton, but the operator-surface connection field does not consume it.

Secondary root cause: `current_risk` currently means "bot-owned namespace exposure from `live_state.json`." The UI label reads as general account exposure, but the implementation does not consult the connected broker account position snapshot for this field.

## 3. Deploy UI Investigation: Signal Stream and Execution Capability

As of `2026-07-01`, the deploy form has no first-class field named "Signal stream."

For a `SPY -> SPY` deployment today:

- Selecting **Deployment Validation** auto-selects the deployment-validation strategy settings file through the Strategy settings selector in `Frontend/src/app/components/broker/broker-deploy-form/broker-deploy-form.component.html`.
- `PythonDataService/app/engine/strategy/spec/fixtures/deployment_validation.spec.json` pins `"symbols": ["SPY"]`. That symbol is the signal/data stream.
- The Action plan is the traded instrument selector. For a SPY asset deployment, the operator adds a long stock leg for `SPY`.
- The runtime treats `live_config.symbol` as the signal stream and the action-plan stock leg as `trade_symbol` when the strategy supports a separate traded target. This is implemented in `PythonDataService/app/engine/live/run.py::_strategy_param_resolution_from_live_config`.

Conclusion: this is not a blocker for `SPY` signal -> `SPY` asset, but it is too implicit. The PRD must add a loud, first-class **Signal stream** selector that is separate from **Action plan / traded asset**, and persist it as `live_config.symbol`.

Readonly is also too easy to miss, and the current labels are misleading:

- Deploy sends `start_options.readonly = this.readonlyFlag()` when Start now is enabled in `broker-deploy-form.component.ts`.
- The form label says Mode: Paper / Live, but internally `Paper = readonly: true` and `Live = readonly: false`.
- The deployment-validation strategy spec already declares `submit_mode: live_paper`, so `readonly=false` means **paper orders enabled**, not real-money live trading, as long as broker identity is `PAPER_ONLY`.
- Runtime capability becomes `PAPER_ORDERS_ENABLED` only when `submit_mode=live_paper` and `readonly=false`, per `PythonDataService/app/engine/live/runtime_producer.py::compose_capability`.
- The important operator fact is execution capability: **READ-ONLY OBSERVATION** versus **PAPER ORDERS ENABLED**.

Requirement: the deploy UI must surface **Execution capability** as an explicit control and review item. Turning readonly off must require loud confirmation that the run can submit paper orders. The UI must not use ambiguous Paper/Live wording where the real submitted field is `readonly`.

## 4. What "Degraded" Means Today

There is no single meaning.

- **Readiness `DEGRADED`** in `app.engine.live.readiness`: hard gates did not fail, but at least one hard gate is `unknown` or a soft gate warns. In the live snapshot above, it meant "backend fell back to start-readiness and desired state is unknown," not "daemon down."
- **Runtime freshness `DEGRADED`** in `app.services.runtime_freshness`: a domain has impaired but typed evidence, such as a control-plane boot-id mismatch or halted market session.
- **Broker banner `Degraded`** in Angular: `/api/broker/health` is still connected, but `connection_state` is not `connected` (`soft_lost`, `reconnecting`, `recovering`, `subscriptions_stale`, `degraded_data_farm`).
- **Broker activity `degraded`** is mostly retired as a row-recency signal; activity rows are event-driven and no recent row is not itself a heartbeat failure.

Requirement: trader-facing copy must stop using "degraded" as a generic label. Backend notices should say the concrete condition: **Recovering connection**, **Market closed**, **Readiness proof missing**, **Control-plane lease stale**, **Data farm degraded**, or **Last known**.

## 5. Goals

- Attention items are current, event-driven, and backend-authored.
- The same broker connection fact that drives the sidebar is available to the bot control panel, with clear distinction between data-plane broker proof and child/run broker proof.
- A broker-connected bot cannot show a broker-disconnected attention item unless the item explicitly says which proof lane disagrees.
- A running daemon/control plane cannot surface as "daemon not running"; per-bot process stopped, daemon unreachable, and child runtime stale are separate items.
- Submit readiness explains the exact missing proof lane rather than collapsing everything to "broker state unproven."
- Exposure distinguishes bot-owned namespace exposure from account-level net/residual exposure.
- Deploy separates signal stream from traded asset so the operator can verify `SPY -> AAPL`, `SPY -> SPY`, or any future signal/asset pair before deployment.
- Deploy makes execution capability explicit and forces a confirmation before paper-order submission is enabled.
- Recent activity becomes a live monitoring workspace: price/trade chart, compact Orders Today, broker activity, and recent incidents are visible together without requiring a full-width page.
- Backend responses include provenance: source, timestamp, freshness, and whether the fact was child-authored, daemon-authored, or data-plane-authored.

## 6. Non-Goals

- No live trading expansion.
- No Angular-derived safety or exposure verdicts.
- No GraphQL or .NET changes; this surface is FastAPI REST direct.
- No replacement of the existing 4-second status poll in this PRD. Event-driven means backend state is fed by durable events, sidecars, daemon health, broker health, and SSE/publisher signals, then projected through the existing poll.

## 7. Proposed Contract

### 7.1 Broker Proof Lanes

Add a backend-composed broker proof block to `operator_surface`:

```ts
broker_proof: {
  data_plane: {
    connection: "CONNECTED" | "DISCONNECTED" | "DEGRADED" | "UNKNOWN"
    safety_verdict: "PAPER_ONLY" | "UNSAFE" | "UNKNOWN"
    account_id: string | null
    source: "broker_health"
    observed_at_ms: number | null
  }
  child: {
    connection: "CONNECTED" | "DISCONNECTED" | "DEGRADED" | "UNKNOWN" | "NOT_BOUND"
    safety_verdict: "PAPER_ONLY" | "UNSAFE" | "UNKNOWN" | "NOT_BOUND"
    submission_capability: "SATISFIED" | "BLOCKED" | "UNKNOWN" | "NOT_BOUND"
    account_id: string | null
    source: "engine_runtime" | "verdict_snapshot" | "run_status" | null
    observed_at_ms: number | null
  }
  consistency: "CONSISTENT" | "CONFLICTING" | "UNKNOWN" | "NOT_COMPARABLE"
}
```

Compatibility rule: existing `operator_surface.broker.connection` remains, but its resolver should prefer fresh `engine_runtime.broker.connection_state` for a bound running process, then fall back to live-readiness `broker_connection`, then data-plane broker health, then `UNKNOWN`.

### 7.2 Attention Group Eligibility

Each attention group must declare:

- `code`
- `severity`
- `headline`
- `explanation`
- `proof_source`
- `observed_at_ms`
- `suppressed_by?: string`

Suppression examples:

- Suppress `broker_connection` when either child broker proof is fresh-connected or data-plane proof is connected and broker observation consistency is `CONSISTENT`.
- Suppress "daemon not running" when `control_plane.state=CONNECTED` and daemon process for the bot is `running`.
- Do not use host-service recovery copy for per-bot `EXITED`/`IDLE`; route to `host_process.start_capability`.
- Suppress runtime missing/stale copy when `engine_runtime.json` is fresh and compatible.

### 7.3 Exposure Contract

Split exposure into two named fields:

```ts
current_risk: {
  bot_posture: "FLAT" | "LONG" | "SHORT" | "MIXED" | "UNKNOWN"
  bot_pending_order_count: number | null
  bot_source: "live_state" | "intent_wal" | null
  account_net_positions: Record<string, number> | null
  account_residual_positions: Record<string, number> | null
  account_source: "broker_positions" | null
  verdict: "READY" | "ATTENTION" | "UNKNOWN"
}
```

UI copy must label this as **Bot exposure** or **Account exposure**, not just **Exposure**, unless both agree and are fresh.

### 7.4 Deploy Signal Stream Contract

Deploy must expose a first-class signal/data selector:

```ts
live_config: {
  symbol: string
  action: ActionPlan
  sizing: SizingPolicy
}
```

Rules:

- `live_config.symbol` is the signal/data stream and must be visible in the deploy form.
- Action plan stock/option legs are traded assets and must be visually separate from the signal stream.
- The review/confirmation step must show both values, e.g. `Signal stream: SPY` and `Traded asset: AAPL`.
- Strategy settings may provide a default signal stream, but the deploy form must show the resolved value rather than hiding it inside the spec file.

### 7.5 Deploy Execution Capability Contract

Deploy must expose execution capability directly:

```ts
execution_capability:
  | "READ_ONLY_OBSERVATION"
  | "PAPER_ORDERS_ENABLED"
```

Mapping:

- `READ_ONLY_OBSERVATION` -> `start_options.readonly=true`
- `PAPER_ORDERS_ENABLED` -> `start_options.readonly=false`

Rules:

- The deploy form must not label `readonly=false` as generic "Live" when the configured mode is paper.
- The deploy form must remove or quarantine the Paper/Live radio copy for this path; the primary control label is **Execution capability**.
- Enabling `PAPER_ORDERS_ENABLED` requires a confirmation dialog before submit.
- The confirmation copy must say that `submit_mode=live_paper + readonly=false` allows paper orders after broker identity proves `PAPER_ONLY`.
- The deployed run summary and bot control header must display the resulting runtime capability from backend proof, not the frontend selection alone.
- Post-launch proof must show all of:
  - `readonly_at_start: false`
  - `submission_capability: PAPER_ORDERS_ENABLED`
  - bot control execution posture: `PAPER_EXECUTION` or trader copy **Live paper / paper execution**

### 7.6 Live Monitoring Activity Layout Contract

The Recent activity / live bot monitoring side panel must be dense enough for a half-page width while preserving the information needed during active supervision.

Required regions:

- **Price & Trades chart**: occupies one half-width lane and remains readable without relying on the full page.
- **Orders Today**: occupies the other half-width lane using a compact column set.
- **Broker Activity**: visible in the same monitoring surface, below or beside the chart/orders pair depending on available width.
- **Recent Incidents**: visible in the same monitoring surface so warnings/errors are seen while the operator is watching live broker events.

Orders Today compact columns:

| Column | Meaning |
|---|---|
| `Time` | local display time for the order/update |
| `Symbol` | traded symbol |
| `Position` | backend-authored position effect or compact derived display when available |
| `Side` | buy/sell |
| `Order` | order quantity plus type/status compactly if needed |
| `Average fill` | average fill price |
| `Filled` | filled quantity or filled/ordered compact value |

Implementation guidance:

- Remove the separate wide columns for `TYPE`, `STATUS`, and `POSITION EFFECT` in the half-width monitoring table. Fold type/status into `Order` only when needed.
- Preserve full forensic detail in row expansion, tooltip, or the full audit trail; the compact monitoring table is for scanning.
- Broker activity should remain a reusable component because the same broker event stream is useful in other broker-information surfaces.
- Recent incidents should use the existing operator-language incident copy, not raw log categories.
- Layout must use responsive grid/flex constraints so chart/table height and column widths remain stable at half-page width.

## 8. Implementation Plan

### PR 1 — Evidence Inventory and Regression Fixtures

- Add failing backend tests for the observed case:
  - running daemon + fresh `engine_runtime.broker.connection_state=connected` + broker health connected => no `broker_connection` attention group.
  - control plane connected + daemon process running => no daemon-not-running attention item.
  - absent `readiness.json` with fresh `engine_runtime.json` does not force broker connection to `UNKNOWN`.
- Add a fixture for `SPY-SIG-AAPL-ASSET-20260629`-style state under `tests/routers/test_live_instances_operator_surface.py`.

### PR 2 — Broker Connection Resolver

- Replace `_broker_connection_state_from_readiness(readiness)` with a resolver that accepts:
  - `runtime_freshness` / `engine_runtime.broker`,
  - live-readiness gate,
  - data-plane broker snapshot,
  - broker observation consistency.
- Keep the child/run proof authoritative when bound and fresh.
- Use data-plane proof when there is no child proof and the consistency check is not contradictory.
- Emit source/timestamp into advanced evidence.

### PR 3 — Attention Group Suppression and Specific Copy

- Extend `operator_trader_guidance.build_submit_readiness_findings()` so each finding checks proof freshness and source.
- Replace generic "Broker disconnected or unknown" with specific cases:
  - "Broker connected; child proof pending"
  - "Broker child proof stale"
  - "Broker observations conflict"
  - "Broker disconnected"
- Replace generic degraded labels with concrete condition copy.

### PR 4 — Exposure Split

- Add account-level exposure inputs from `/api/broker/positions` / `account-summary` into the status assembly, with bounded failure behavior.
- Preserve bot-owned namespace exposure from `live_state.json`.
- Render header as "Bot exposure" when only bot namespace is known, "Account exposure" when account net/residual is the only current proof, or "Exposure agrees" when both are current and consistent.

### PR 5 — Frontend Contract Rendering

- Update `LiveInstanceStatus` types.
- Render source-specific labels and avoid raw `DEGRADED` copy in the Attention dropdown.
- Keep `receiptLabel` for code-like diagnostic values.
- Add Vitest coverage for the dropdown suppression rules and renamed exposure pill.

### PR 6 — Deploy Signal and Execution Capability Clarity

- Add a visible Signal stream selector populated from the selected strategy spec default.
- Persist the selected signal stream as `live_config.symbol`.
- Keep Action plan as traded asset selection and show signal/traded-asset side by side in deploy review.
- Replace ambiguous Paper/Live launch wording with explicit Execution capability states.
- Require confirmation before submitting with `PAPER_ORDERS_ENABLED`.
- Add post-launch proof rows for `readonly_at_start`, `submission_capability`, and bot control execution posture.
- Add tests for `SPY -> SPY` and `SPY -> AAPL` deploy payloads, asserting `live_config.symbol` and action-plan stock target are distinct fields.

### PR 7 — Live Monitoring Activity Panel Density

- Rework the Recent activity / live bot monitoring panel into a responsive monitoring workspace with chart + compact Orders Today in a two-column half-width-friendly layout.
- Change Orders Today to the compact column set: `Time`, `Symbol`, `Position`, `Side`, `Order`, `Average fill`, `Filled`.
- Move non-scan fields (`type`, `status`, verbose position effect, replay count) into row detail or full audit trail rather than top-level columns.
- Show Recent Incidents inside the broker activity / monitoring panel alongside broker activity events.
- Keep broker activity reusable for other broker-information surfaces; do not fork a one-off table for the bot panel.
- Add Angular Testing Library coverage for the compact column headers, incident presence, and responsive half-width rendering.

## 9. Acceptance Criteria

- With broker health connected and fresh child broker proof connected, the Attention dropdown does not contain `broker_connection`.
- With daemon health `CONNECTED` and process `running`, the Attention dropdown does not imply the daemon is down.
- If child and data-plane broker accounts diverge, the dropdown shows a conflict item rather than hiding it behind "connected."
- `Submit` no longer says **Broker state unproven** when broker safety, connection, submission capability, AccountOwner, reconciliation, and runtime proofs are all present.
- `Exposure` never shows unknown when a connected account position snapshot is current; if bot-owned exposure is unknown but account exposure is known, the label says so.
- No trader-facing surface uses "Degraded" without a concrete backend-authored explanation.
- Deploy review clearly shows **Signal stream** and **Traded asset** as separate values.
- `SPY -> SPY` remains supported and explicit: signal stream `SPY`, traded asset `SPY`.
- `SPY -> AAPL` is representable without changing strategy settings by setting signal stream `SPY` and Action plan asset `AAPL`.
- Enabling paper order submission is shown as **PAPER ORDERS ENABLED**, not hidden behind Paper/Live wording.
- The deploy form no longer presents `readonly=false` as generic **Live** for a `submit_mode=live_paper` strategy.
- A post-launch proof panel shows `readonly_at_start: false`, `submission_capability: PAPER_ORDERS_ENABLED`, and bot control execution posture `PAPER_EXECUTION` or trader copy **Live paper / paper execution**.
- The live monitoring Recent activity panel shows Price & Trades, compact Orders Today, Broker Activity, and Recent Incidents in one monitoring surface.
- Orders Today renders the compact headers `Time`, `Symbol`, `Position`, `Side`, `Order`, `Average fill`, and `Filled`, and fits in a half-page-width lane without horizontal scrolling at the target desktop breakpoint.
- Broker activity remains available as a reusable component for other broker-information surfaces.

## 10. Open Questions

- Should AccountOwner generation be inferred or bootstrapped from a connected, consistent broker account when no account-owner artifact exists, or must it remain a separate required proof?
- Should a running bot that has no `desired_state` sidecar default to RUNNING for submit-readiness, or should the missing durable intent continue to block?
- Is `readiness.json` still required for running bots, or has `engine_runtime.json` become the canonical event-driven replacement for broker/process freshness?
- Should account residual contamination block submits when `policy_blocks_starts=false`, or only surface as warning?
- Should strategy settings be allowed to lock the signal stream, or should every deploy make the default editable with validation against the strategy's supported symbols?
- Should `PAPER_ORDERS_ENABLED` confirmation happen once per deploy submit, once per session, or every time the operator toggles out of read-only observation?
