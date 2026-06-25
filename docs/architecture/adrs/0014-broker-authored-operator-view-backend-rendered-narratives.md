# ADR 0014 — Broker-authored operator view, backend-rendered narratives

**Status:** Accepted 2026-06-22. Drafted during the 2026-06-22 broker-activity reconciliation design pass (Codex second-opinion round).
**Decision drivers:** Operator instruction during the design pass: *"we want to show the user what he will see in the client portal and if he sees a divergence he should see an explanation why to expect that divergence … all the authoring has to be done by the back end and front end will simply render those things."* The existing engine-side Activity tab (`BotTradesTableComponent` + `SizingAuditTableComponent`) shows what the *engine* believes happened; it cannot answer the operator question *"does what I see here match what IBKR sees?"* — there is no broker-side authority on the surface at all. Layering a divergence chip on top of engine-derived rows would invert the trust direction and re-import the synthetic-verdict pattern ADR 0013 forbids.
**Related:** ADR 0008 (durable submit protocol — `order_ref` ownership, run-scoped WAL), ADR 0011 (broker safety verdict — reactive, fail-closed), ADR 0013 (operator-surface boundary: judgment vs evidence, no frontend-derived verdicts).

## Context

The Activity tab today renders engine-side trades and a sizing-audit table. The data sources are engine-derived (`/api/live-runs/{id}/trades`, `/api/live-runs/{id}/executions`). The frontend joins, sorts, formats, and renders rows. There is no broker-side row stream; the operator cannot ask *"is the engine telling me the truth about what IBKR did?"* from inside the cockpit.

Three structural reasons this is the wrong shape for live-trade reconciliation:

1. **Wrong source of truth.** The engine cannot prove an execution happened. Only the broker can. The operator's mental model when reconciling is *"what does IBKR show me on the Client Portal Trades tab?"* — engine state is at best an overlay on that, not a replacement for it.
2. **Inverted verdict authority.** ADR 0013 ruled that verdicts (`expected` / `unexpected`) belong on `operator_surface`, authored server-side. A divergence-chip layered onto engine-authored rows would derive its meaning from comparing engine to broker, which is judgment the cockpit must not perform.
3. **Untruthful narratives.** A narrative that explains *why* a divergence is expected (e.g. *"reflection delayed 8s — IBKR connection dropped 14:32:14–14:32:22, exec captured on resume"*) requires structured facts the frontend does not have. Composing such prose Angular-side would invent context.

Codex's review of the design surfaced an additional structural requirement: the broker-activity surface must be a *single authority* for execution verdicts — not an additive layer on top of existing engine-side trade rendering. Otherwise the cockpit ends up showing the same execution twice (engine row + broker row + divergence row) and the operator has to reconcile the surfaces themselves.

The cockpit-v2 Activity tab is the right home for the new surface; the rest of the cockpit (Status/Risk, Audit, Configuration) keeps its existing 4s `LiveInstanceStatus` poll because those tabs depend on operator-surface verdicts that are not execution-keyed.

## Decision

**Adopt a broker-authored operator view for the cockpit-v2 Activity tab. IBKR executions are the canonical source of the row stream; engine state is overlay/explanation only. The backend authors every operator-facing string from structured facts on the same row; the frontend is render-only.**

### 1. The verbatim rule

**The Activity tab's broker-activity surface renders one row per IBKR execution. The broker is the authority on row identity, columns, and timestamps. Engine state is overlay context the backend joins onto each row before authoring.**

**Every operator-facing string on a row (`headline`, `narrative`) is produced by a versioned backend template, deterministic-pure from the structured facts on the same row. The frontend renders strings verbatim; it does not compose narratives, classify verdicts, or derive lag chips from raw timestamps.**

**Frontend formatting allowed: currency / number / time formatting, layout, expand/collapse, chip color selection from a closed-enum `verdict` field. Frontend judgment forbidden: deriving `verdict` from facts, computing `headline` from sub-fields, conditionally hiding facts based on perceived relevance.**

### 2. The four-value verdict enum

A row's verdict is one of:

| Verdict | Meaning | Operator interpretation |
|---|---|---|
| `expected` | Broker execution matches engine intent within policy; no caveats. | Normal fill, no action. |
| `expected_with_caveat` | Broker execution explained by structured context (timing, partial fill, reconnect window) the operator should be aware of but does not need to act on. | Surface the narrative; no action. |
| `unexpected` | Identity, quantity, side, price-policy, or lifecycle mismatch the operator must investigate. | Block-color chip; require operator attention. |
| `engine_only_pending` | Engine emitted an intent; no broker ack yet. Surfaced in a separate "Working / Pending Orders" panel, not the broker-activity table. | Visibility for in-flight; transitions to a broker-activity row once acked. |

Forensic detail (which divergence categories fired, by how much) lives in the row's structured `divergence_facts` drill-down; it does not multiply the verdict enum.

### 3. Versioned templates as the authoring contract

Every `headline` + `narrative` pair is rendered by exactly one template, identified by `(template_key, template_version)`. The pair (key, version) is persisted on the row.

- **Templates are pure-function constants** in `app/services/broker_activity_templates.py`. Selection (`select_template`) and rendering (`render_narrative`) are deterministic functions of the row's structured facts.
- **Templates are versioned, never edited in place.** A template version is frozen once a row has been authored under it. A v2 of a template ships as a new constant; old rows continue to render their v1 strings.
- **The persisted row carries both the structured facts AND the rendered strings.** Historical operator view is exactly reproducible from the row alone; template improvements re-render only new rows, never rewrite history.

Template authoring discipline (codified in tests):

- **Truthfulness contract:** for every `(template_key, template_version)`, the rendering function must be a pure function of the row's `facts` block. A property test asserts `render(facts) == row.headline + row.narrative` for every committed row in a recorded fixture.
- **No speculation.** Templates may only reference structured facts that are present on the row at write time. A template that would render *"during fast market"* is rejected unless the row carries a structured `market_condition` fact.
- **Closed reason-code vocabulary** drives selection. Reasons (`normal_fill`, `price_divergence`, `quantity_divergence`, `partial_fill`, `cancellation`, `rejection`, `pending_acknowledgement`, `reconnect_recovery`, `unmatched_execution`, `duplicate_execution`, `missing_commission`, `timing_caveat`) are a `StrEnum`; adding one requires a code change, not a config edit.

### 4. The stateful publisher pattern

The reconciliation publisher is a data-plane-owned background task per strategy instance. It:

1. Consumes the existing `stream_order_events` (IBKR fill/status events) for that instance's `bot_order_namespace`.
2. Joins each event to the engine's `LiveStateEnvelope.submitted_orders` by `order_ref` (the namespace round-trip locked in by ADR 0008, surfaced on every event by `IbkrOrderEvent.order_ref`).
3. Calls the pure reconciliation functions (`match_identity` → `classify_verdict` → `select_template` → `render_narrative`).
4. Appends one record to the append-only `broker_activity.jsonl` WAL.
5. Fans the authored `BrokerActivityRow` out to all SSE subscribers for that instance.

Authoring itself is pure. Only the publisher holds state (the per-instance sequence counter, the SSE subscriber set, the last-seen execution-identity dedupe cache). The publisher is the *only* code path that writes to `broker_activity.jsonl`.

### 5. Persistence — facts AND authored output

Each `broker_activity.jsonl` record contains:

- **WAL identity:** `seq` (per-instance monotonic), `ts_ms` (wall-clock observation).
- **Broker-recognisable columns** (mirroring IBKR Client Portal Trades): `exec_id`, `perm_id`, `order_ref`, `symbol`, `side`, `quantity`, `price`, `commission`, `net_amount`, `order_type`, `exec_ts_ms`.
- **Authored output:** `verdict`, `template_key`, `template_version`, `headline`, `narrative`.
- **Structured facts** (the drill-down): `engine_overlay` (intent_id, sizing_provenance, lag breakdown) and `divergence_facts` (price_delta, qty_delta, lag_total_ms, window_context).

The full row is written at author time; the authored strings are *not* re-rendered on read. This preserves operator-view reproducibility and makes the WAL safe to ship to forensic tooling.

`LiveStateEnvelope` carries only a `last_broker_activity_wal_seq: int` cursor for fold/resume; it does not store rows. The publisher writes the WAL; the envelope tracks the highest seq folded into the resume state.

## Amendment 2026-06-25 — Host-runner-owned raw callback WAL

**Status:** Accepted for issue #684 PR 2. Supersedes ADR 0014 §4 only where §4 made the data-plane publisher the live event source and the sole live author of broker-activity capture. The backend-rendered narrative rule, the closed verdict enum, and the template truthfulness contract remain unchanged.

The June25 incident showed that the data-plane publisher is the wrong owner for first-capture durability: the host runner can submit and fill orders while the long-lived data-plane process is stale, detached, or crashed. Therefore the process that owns the order lifecycle must also own the durable broker-callback evidence.

### New ownership chain

1. The host runner writes an append-only per-run raw callback WAL, `broker_callbacks.jsonl`, before any callback can be lost to a detached data-plane publisher.
2. Each raw row records the broker callback fact (`orderStatus`, `execDetails`, `commissionReport`, position snapshot, disconnect/reconnect event) plus idempotency keys: `exec_id`, `perm_id`, `order_ref`, callback type, and the run-local callback `seq`.
3. The data-plane broker-activity publisher becomes a projector/enricher over `broker_callbacks.jsonl`: it reads raw callback facts, joins the current engine overlay by `order_ref`, applies the existing pure reconciliation/template functions, and appends authored `BrokerActivityRow`s to `broker_activity.jsonl`.
4. The cockpit continues to read only the authored broker-activity surface. Raw callbacks are audit/projection input, not UI copy.

### What changes

- `broker_callbacks.jsonl` is the authoritative capture record for broker activity because it is written by the host runner that owns the IBKR order lifecycle.
- `broker_activity.jsonl` remains the authored operator-view WAL, but it is now a derived projection. Losing or restarting the data-plane publisher must not lose raw history; the projection can be rebuilt from raw callbacks.
- A submit-enabled start may temporarily fail closed when durable callback capture cannot be attached. That guard is transitional and is superseded by host-runner-owned capture once the raw callback WAL is implemented.
- Reconnect sweeps and redeliveries dedupe against raw callback idempotency keys before projection so repeated IBKR delivery cannot double-count fills.

### What does not change

- ADR 0008's `order_ref` ownership ladder and uncertain-ack semantics are unchanged.
- ADR 0008's JSONL durability contract is reused: append-only rows, monotonic `seq`, fsync-before-return, and tolerance only for one trailing unterminated line.
- ADR 0014's narrative contract is unchanged: verdicts, `headline`, and `narrative` are still backend-authored, template-versioned, and persisted on `BrokerActivityRow`.
- The data plane may still enrich, backfill, stream, and expose the authored rows, but it is no longer the authority for whether a broker callback happened.

### 6. Per-instance configurable timing policy

Lag-driven verdict thresholds (`expected` → `expected_with_caveat` → `unexpected`) are not hardcoded universal constants. Each strategy instance's configuration carries a `reconciliation_timing_policy` block:

```
reconciliation_timing_policy:
  caveat_lag_ms: <int>          # > this → expected_with_caveat (timing_caveat reason)
  excessive_lag_ms: <int>       # > this AND no known explanation → unexpected
```

Conservative defaults ship in the schema; per-instance overrides are explicit. A high-lag execution with a *known* explanation (e.g. captured during a reconnect window) renders as `expected_with_caveat` via the reconnect-recovery template, NOT `unexpected` — verdicts depend on what the publisher knows, not the raw clock alone.

### 7. UI cleanup discipline

The Activity tab's existing components are replaced, not supplemented:

- **Delete:** `SizingAuditTableComponent` (the useful provenance moves into `engine_overlay.sizing_provenance` on the broker-activity row drill-down).
- **Replace:** the activity-data fetch path. The new SSE channel replaces engine-side trade polling for the Activity tab only; Status/Risk, Audit, and Configuration tabs continue to use the existing 4s `LiveInstanceStatus` poll.
- **Keep:** `BotTradesTableComponent` (separate per-trade P&L audit surface on the Audit tab — different consumer, different surface). `IncidentsPanelComponent` (operational health, not execution narratives — different domain).

### 8. What this does NOT cover

- **Account-level NLV / cash reconciliation across the singleton account.** Out of scope for v1. The data plane's IBKR connection is shared across multiple deployed instances; per-account ledger snapshots cannot be attributed to a single instance's trades. Per-instance reconciliation via `order_ref` namespace is the only authoritative join.
- **CP Web API mechanics** (REST `/portfolio/.../ledger`, WebSocket topic syntax, `cOID`, `/tickle`, 10-req/sec global limit). The repo's broker layer uses TWS API via `ib_async`; CP Web API specifics from the design research are explicitly NOT carried into the implementation or the repo's reference docs.

**Resolved in slice 3 (2026-06-22):** Reconnect-recovery sweep semantics shipped with the ADR 0011 amendment — `BrokerActivityPublisher.sweep_reconnect_recovery` runs on every successful reconnect via the `AutoReconnectMonitor.recovery_callbacks` chain, fetches the day's executions via `IB.reqExecutionsAsync`, dedupes by `exec_id`, and authors any unseen executions with `reconnect_recovery_active=True` so the `reconnect_recovery` template fires. `place_paper_order` refuses new submissions while any sweep is active.

## Consequences

**Positive:**
- The operator's mental model (*"what does the Client Portal show me"*) is the literal source of the row stream. Divergence is surfaced where it actually exists, with backend-authored context.
- The synthetic-verdict regression mode is blocked at the source: there is no frontend code path that computes `verdict` from sub-fields. A future contributor adding *"merged_status"* on the cockpit fails the truthfulness property test.
- Template versioning + fact persistence guarantees historical operator-view reproducibility. A template-library improvement (better wording, more specific reasons) re-renders only new rows.
- The broker-activity surface is the *single authority* for live execution verdicts. There is no shadow surface (engine-derived rows + broker-derived rows + reconciliation overlay) for the operator to mentally merge.
- Per-instance timing policy keeps lag thresholds out of universal constants; strategies with different latency expectations configure independently.

**Negative:**
- A new template requires a code change, a test case, and a `template_version` bump. Templates are not configuration. Acceptable because templates ARE the truthfulness contract.
- The publisher introduces a stateful background task per instance — new lifecycle surface to manage (start when instance starts, stop when instance stops, drain SSE subscribers on stop). After the 2026-06-25 amendment it is a projector over host-runner-captured callbacks, not the first durable capture point.
- Activity-tab UX regressions are possible while operators learn the new verdict semantics. Mitigated by the runbook landing alongside (`docs/runbooks/live-trade-reconciliation.md`).
- Two refresh sources on the cockpit (the existing 4s poll + the new Activity-tab SSE) is more wiring than a single source. Accepted because the alternative (delete the 4s poll) would dark-fire three other tabs.

**Non-consequences:**
- `LiveStateEnvelope` semantics (ADR 0008 §5) are unchanged. The only new field is a sequence cursor.
- The IBKR-side wire model (`IbkrOrderEvent`, `IbkrOrderSpec`) is unchanged after slice 0's `order_ref` addition.
- ADR 0013's operator-surface boundary is reinforced, not redefined: the broker-activity row's `verdict` is server-authored on its own channel; nothing about the row reaches `operator_surface`.

## References

- `PythonDataService/app/services/broker_activity_reconciler.py` — pure functions (`match_identity`, `classify_verdict`, `select_template`, `render_narrative`).
- `PythonDataService/app/services/broker_activity_templates.py` — versioned template constants + selection table.
- `PythonDataService/app/services/broker_activity_publisher.py` — stateful per-instance projector/enricher; writes authored `broker_activity.jsonl` rows but is no longer the authority for raw broker-callback capture.
- `run_dir/broker_callbacks.jsonl` — host-runner-owned raw broker-callback WAL accepted by the 2026-06-25 amendment; implementation lands in the following slice.
- `PythonDataService/app/schemas/broker_activity.py` — `BrokerActivityRow`, `EngineOverlay`, `DivergenceFacts`, `Verdict`, `ReconciliationTimingPolicy`.
- `PythonDataService/app/engine/live/live_state_sidecar.py` — `last_broker_activity_wal_seq` cursor (additive).
- `PythonDataService/app/routers/broker_activity.py` — SSE stream + paginated REST backfill.
- ADR 0008 §3 — sibling WAL pattern (`intent_events.jsonl`). The amendment registering `broker_activity.jsonl` as a peer WAL ships with this slice.
- `Frontend/src/app/components/broker/cockpit-v2/tabs/activity-tab/broker-activity-table.component.ts` — render-only SSE subscriber.
- `docs/runbooks/live-trade-reconciliation.md` — operator runbook (lands with slice 4).
