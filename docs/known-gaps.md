# Known Gaps — Living Open-Defect Backlog

**Purpose.** One place that answers "what is still broken or deferred?" for an AI
agent or operator. This is the *only* durable home for open defects; the
point-in-time audit-finding files they came from (`docs/audits/auto-research/findings/`,
`docs/audits/vibe-coded-app-research/findings/`, `architecture-investigation-2026-07-02.md`,
and the auto-research run logs) were deleted on **2026-07-04** after their open
items were lifted here. The closed findings live in git history and in the
auto-research ledger (`docs/audits/auto-research/state.json`).

**Status convention.** Each item carries a severity and a code pointer captured
on the verification date named with its section — verify the `file:line` against
current code before acting, since the tree moves. When an item is fixed, delete
its bullet (git history is the record). When a new open defect is found, add it
here rather than starting a new finding-file tree.

**Scope note.** Safety-critical and broker items below were verified open against
current code on 2026-07-04. The architecture-investigation P1 tier and the
run-log functional items were **not** re-verified in that pass — confirm before
committing effort. The account-registry and architecture P1 clusters were
rechecked on **2026-08-17**. The IBKR B-05 order-cancel finding was closed by
deleting the complete order-actuation boundary on **2026-08-19**.

---

## 1. Safety-critical (partially re-verified 2026-08-17)

### Temporal authority and liveness (verified 2026-08-18)

Source: `docs/audits/temporal-compliance-2026-08-18.md`, read at commit
`deb9764f9`. Scheduled session structure and real-time market liveness are
separate authorities under ADRs 0022 and 0029.

- **Live Alpaca engine bars carry native datetimes across strategy boundaries
  (high).** `services/bot_trade_strategy.py:80-90,139-146` converts numeric feed
  timestamps into `TradeBar.time` / `end_time`, returns the object, stores its
  end time on `StrategyContext`, and passes it through strategy/consolidator
  objects. `engine/data/trade_bar.py:17-40` makes both fields `datetime`. Migrate
  the one canonical engine bar/context contract to numeric start/end ms and
  preserve EMA/LEAN parity; do not add a live-only duplicate model.
  [#1674](https://github.com/tim1016/learn-ai/issues/1674)
- **Live Alpaca conflates scheduled phase with real-time market liveness
  (high).** `services/bot_start_admission.py:324-335` and
  `services/broker_v2_panel/market_pulse.py:24-61` use scheduled phase to decide
  whether bars are expected and to render `OPEN`, while the automated strategy
  can reach the Alpaca Clerk without a live market-status fact. Channel health
  is not market-wide open evidence or proof against a symbol halt. Preserve the
  authority split: calendar/capability owns scheduled phase; fresh market-wide
  and symbol-scoped evidence wins at Start, operator projection, and the
  automated new-exposure effect gate.
  [#1671](https://github.com/tim1016/learn-ai/issues/1671)
- **Live deployment validation misses early-close flattening (high).**
  `engine/strategy/algorithms/deployment_validation.py:31-32,92-108` hardcodes
  09:45 detection and 15:45 stop/flatten. A 13:00 half-day never reaches the
  stop. The QC reference explicitly defines absolute clocks, so first decide
  absolute-with-calendar-clamp versus calendar-relative cutoffs; then update the
  reference and pin Python/LEAN parity on regular and early-close days.
  [#1672](https://github.com/tim1016/learn-ai/issues/1672)
- **Alpaca V2 chart crosshair bypasses the shared timestamp formatter
  (medium).** `dual-pane-chart/dual-pane-chart.component.ts:77-89` constructs
  `Intl.DateTimeFormat` inside the feature. The numeric input is unambiguous, but
  formatting ownership still violates the temporal display rule. Delegate the
  numeric-ms readout to the shared timestamp core and preserve local/ET/DST
  behavior without retaining the rendered string.
  [#1677](https://github.com/tim1016/learn-ai/issues/1677)

### Non-numeric operator verdict ownership (verified 2026-08-18)

Source: `docs/audits/non-numeric-operator-verdict-census-2026-08-18.md`, read at
commit `a16571c2736b`. No new ADR is owed: ADR 0035 Decision 12 already makes
Alpaca safety, capability, freshness, and primary-action choice backend-owned,
while ADR 0027 owns blocker disposition and moves.

- **Live account surfaces derive operational posture and availability
  (medium).** Account Desk combines guidance, uncertainties, authority health,
  and recovery-action flags into `healthy` / `fix_here` / `wait` / `review` /
  `terminal`. Account Strip combines account flags into “Trading available” or
  “Trading blocked” and freeze/hold flags into custody-block verdicts; the
  available case omits the backend's paper-mode and active-status requirements.
  Preserve one evidence/condition authority for both surfaces, with separate
  host-correct `account_desk` and `fleet_roster` blocker projections. Include
  unresolved intents and missing/stale/unhealthy Clerk channels in the backend
  decision table. Reuse ADR 0027 disposition; do not create a parallel
  five-state posture enum or render one host's cure on the other surface.
  [#1664](https://github.com/tim1016/learn-ai/issues/1664)

- **Both live bot-detail lenses derive the banner's primary command (medium).**
  `Frontend/src/app/components/broker/v2-panel/bot-detail-banner/lifecycle-action.ts:12-19`
  selects Resume, Continue, or Stop from `health.running` and
  `health.desired_state`; the Trader and Operator banners render that result as
  their sole primary command, and Operator readiness uses it for suppression.
  Preserve one backend selection policy with audience-correct Trader and
  Operator references at the same revision as mission/actions. Operator
  recovery may be primary only in the Operator lens; Trader remains restricted
  to Trader-visible lifecycle actions. Operator readiness uses the Operator
  reference; any retained recovery `evidence.primary` marker is diagnostic-only
  and must agree exactly. Angular fails closed when a reference is absent or
  inconsistent.
  [#1665](https://github.com/tim1016/learn-ai/issues/1665)

## 2. Architecture-investigation P1 tier (re-verified 2026-08-17)

The still-reachable P0 safety issues from
`architecture-investigation-2026-07-02.md` were verified fixed. Its IBKR
panic-flatten, recovery-flatten, freeze mutation, and IntentWal findings left
the living backlog when #1583 deleted those executable paths. The remaining
P1s carried forward are:

The former R3 recovery-daemon item was retired from this backlog: it concerns
the deprecated IBKR bot-control surface, while the accepted Alpaca Clerk
cutover is complete (ADR-0035).

## 3. Broker subsystem (re-verified 2026-08-19)

The B-05 direct-cancel risk is closed by deletion: no application route or
production helper can cancel an IBKR order. B-06 and B-09--B-13 remain fixed in
current code and their regressions pass. The disconnect-blindness cluster
(B-02/03/04/08) still needs a separate reachability review.

## 4. Broker session mirror — deferred product/safety decisions

Shipped read-only (ADR-0018, PRs #881–#908). Four items were intentionally not
built because they need a product/safety decision or authority the codebase does
not yet provide:

- **Exact 1:1 data-plane socket de-dup** — `/api/broker/health` publishes the
  data-plane `client_id`/account/host/port but not `local_port` or host PID, so
  the reconciler cannot join a health row to a specific `lsof` row without
  guessing. Needs a data-plane socket-identity contract.
- **Durable orphaned-socket incident lifecycle** — orphan notices are projected
  on live rows only, not persisted as acknowledgeable/resolvable incidents.
  Decide whether they enter the incident store and what resolves them.
- **Strong orphan attribution without PID/run-dir evidence** — a raw Gateway
  socket with no live PID and no run-dir stays `ghost`; may under-classify real
  orphaned bot sockets. Needs a durable session-level socket-identity history.
## 5. Numerical-rigor & frontend debt (deferred, P2)

- **Golden-fixture coverage gap** — most canonical math still lacks a registered
  golden fixture; the `iv30/` snapshot sits outside manifest governance.
  *(was F-0026; deferred in `auto-research/state.json`)*
- **Temporal wire/storage contracts outside Alpaca V2 remain non-numeric
  (medium).** The 2026-08-18 census confirmed 4 Pydantic, 29 C#, and 22 real
  TypeScript temporal field declarations using strings or native date types
  across golden fixtures, Data Lab, portfolio, market-data, validation, and
  research surfaces. The fourth Pydantic field is the active
  `EngineBacktestRequest.force_flat_at: datetime.time` boundary whose OpenAPI
  and generated TypeScript representation is a time string. Group migrations
  by one source contract at a time; do not create another cross-stack duplicate.
  The live Alpaca V2 wire/storage path is not in this cluster.
- **Frontend naive `new Date(string)` — Tier 2 (medium).** Eighteen production
  calls still parse date-only or local-wall strings across validation,
  option-expiry, Options Lab, Strategy Builder, ticker ranges, Data Lab, Past
  Chain, Indicator Report, Market Calendar, and Research Feature Report; five
  tests repeat the pattern. Migrate the owning wire fields to numeric ms and the
  shared display boundary rather than appending guessed offsets. *(was F-0034)*
- **Active non-live session structure still embeds clock constants (medium).**
  Fifteen occurrences remain across canonical-calendar/LEAN adapters, Data
  Lab/chart coverage, data quality, research features, and deployment-validation
  parity copies. Replace exchange/session assumptions with the canonical
  calendar; update reference/parity copies atomically with their canonical
  strategy. The former evaluator run-ledger `force_flat_at` was deleted under
  ADR 0038 and is not a migration target.
- **`FailureRow.ts_ms` mislabel** — a host-local time string is typed/named as
  `ms-UTC`; rename to `ts_local` and convert at ingestion. *(was VCR-P3-K)*

## 6. Functional findings parked in deleted run logs (not re-verified)

- **`exposure_pct` unit bug** — `bars_held_total` mixes 15-min strategy bars with
  a 1-min equity curve. Build-Alpha features **F6** (noise/robustness) and **F8**
  (parameter sensitivity) are unimplemented. *(2026-05-07 build-alpha run)*
- **ML-V-001** — Phase 3.0/3.5 canonical math not registered in
  `docs/math-sources-of-truth.md`. **ML-V-002** — provenance blocks missing on
  `research/parity/qc_reconciler.py` and the prediction-set `artifact.py`.
  *(2026-05-12 ML-predictions run)*

## 7. Contract-surface drift gates (verified 2026-08-18)

Source: `docs/audits/contract-surface-drift-2026-08-18.md`, read at commit
`a16571c2`. OpenAPI, GraphQL, both Frontend generated clients, and the
broker-v2 operator manual regenerated clean; deliberate drift proved those
regenerate-and-diff gates can turn red.

- **Broker-v2 vocabulary snapshot prose is not source-pinned (medium).** CI
  does not run `regenerate_broker_v2_vocabulary_snapshot`, the Python contract
  test compares only code membership, and the Frontend test checks only
  nonempty copy/fallback coverage. Both committed snapshots can carry the same
  wrong label or explanation while every current contract test remains green.
  [#1666](https://github.com/tim1016/learn-ai/issues/1666)
- **Eleven live Broker V2 REST types bypass generated OpenAPI aliases
  (medium).** Chart, evidence, and gallery consumers hand-copy schemas already
  present in `broker.types.ts`; the gallery snapshot is also a REST bootstrap,
  not solely an SSE exception. Generated clients can be current while these
  live consumers compile against stale mirrors. Keep true stream-only
  envelopes handwritten: pin `GalleryLiveUpdate` to
  `app.schemas.broker_v2_gallery.GalleryLiveUpdate`; for model-less
  `GalleryResetEvent`, introduce a backend model or explicitly fixture-pin the
  router-owned `{reason, cursor}` shape.
  [#1667](https://github.com/tim1016/learn-ai/issues/1667)
- **Accepted-ADR `Vocabulary:` metadata is not gated (low/medium).** The ADR
  status guard correctly enforces status syntax and value but accepts a
  governed accepted ADR after its ADR 0040 declaration is removed. Enforce the
  forward-only rule without backfilling pre-0040 ADRs.
  [#1668](https://github.com/tim1016/learn-ai/issues/1668)
