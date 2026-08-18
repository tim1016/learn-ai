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
committing effort. The account-registry, architecture P1, and IBKR B-05/B-06/
B-09--B-13 clusters were rechecked on **2026-08-17**; their individual sections
say which findings remain.

---

## 1. Safety-critical (partially re-verified 2026-08-17)

### Bot Control / Account Clerk reconciliation (verified 2026-07-29; BUG-16 fixed 2026-08-17)

- **[IBKR lineage only] Eight-bot A0 admission latency has no recorded
  production-load qualification (high).** Normal paper entries return after the
  Clerk's fsynced A0 receipt while later broker work runs asynchronously. The caller
  deadline is 10 s; deterministic qualification exists, but a relevant production I/O
  load measurement has not been recorded here. Preserve the invariant: A0 timeout is
  unknown, never a false retry permission. Qualification: run and retain the
  broker-free custody campaign and an appropriate paper-host load drill before
  relying on eight concurrent entry bursts.
  **Scope corrected 2026-08-17:** the 2026-07-28 audit this came from explicitly
  traces IBKR `run.py` → RPC → separate-process Account Clerk. The Alpaca Broker V2
  route (strategy → selected in-process Clerk) was swept and **does not carry this
  item**. An unscoped "eight-bot" entry reads as applying to whatever fleet the
  reader has in mind, which is now the Alpaca one.

- **[IBKR lineage only] Eight-bot end-day cancellation remains unqualified
  (high).** Direct operator cancel timeouts were raised in #1289, but the serialized
  namespace-cancel path used by concurrent CLOCK_OUT needs paper-broker qualification
  before it is advertised as fleet-safe. Preserve the invariant: a cancellation
  timeout is uncertain and cannot be represented as a clean exit. Qualification:
  eight-bot paper wind-down with terminal Clerk receipts and post-action
  reconciliation.
  **Scope corrected 2026-08-17:** same provenance and same correction as the A0 item
  above — IBKR call graph, not reachable on the Alpaca Broker V2 route.

- **Several audit findings need reachability qualification, not deletion (medium).**
  Async entry-queue saturation, broker-stream-silence under custody load, concurrent
  reconciliation-receipt publication, an enqueue-to-registration failure window, and
  after-close `flatten_and_pause` actuation are recorded in the supporting 2026-07-28
  call-graph audit. They are not proven dead or fixed by a search. Preserve their
  respective fail-closed, durable-receipt, and no-false-actuation invariants; turn
  each into a focused regression or paper qualification before cleanup.

### Alpaca submit-to-custody fail-open seams (verified 2026-08-17)

Source: `docs/audits/submit-to-custody-fail-open-sweep-2026-08-17.md`, read at
commit `e7325d2`. "Fail open" here means missing, indeterminate, or rejected
custody evidence can reach a state where a **later new-exposure decision is
allowed** — not merely that a display value is optimistic.

The sweep confirmed five seams and **refuted nine** candidates. The refutations
are recorded in the audit doc's candidate table and should not be
re-investigated; activated SQLite is fail-closed for ordinary faults, and each
seam below is a specific conditional gap, not a general weakness.

- **Accepted ENTER survives lookup failure without becoming admission-blocking
  (high).** `sqlite/order_evidence.py:343-414` — exact lookup catches every
  `BrokerError` and returns without folding uncertainty, so a
  crash-after-accept/before-contact operation stays `operation_state=accepted`.
  Recovery does record `RECONCILIATION_ATTEMPTED` with `STILL_UNKNOWN`
  (`sqlite/reconcile.py:229-287`) but preserves that state and raises no
  admission-authoritative uncertainty or hold; admission checks
  reconciliation-in-progress, EXIT, holds, uncertainties, and manual work, but
  **not a nonterminal ENTER** (`sqlite/uncertainty.py:347-468`). Preserve the
  invariant: an operation whose broker outcome is unknown must block new
  exposure, not merely be annotated. [#1614](https://github.com/tim1016/learn-ai/issues/1614)

- **Capture or parse failure leaves execution health green (high).**
  `broker/alpaca/trade_updates.py:424-507` — a failed verbatim capture, invalid
  JSON, or unmappable fill returns from the frame handler and continues the same
  socket cycle. The evidence sink is never called, and health is socket-connected
  state only, with no last-valid-frame freshness budget
  (`trade_updates.py:318-343`). A later periodic REST pass may repair custody, but
  until then the submit gate sees execution as healthy. Preserve the invariant:
  a stream that is connected but not delivering usable evidence is not healthy.
  [#1615](https://github.com/tim1016/learn-ai/issues/1615)

- **A non-`BrokerError` sweep failure reopens admission without authoring
  uncertainty (medium).** `sqlite/reconciliation_sweep.py:128-146` — an unexpected
  periodic-pass exception is logged and converted to `False`, and the reconciler's
  `finally` releases the admission fence. A normal snapshot `BrokerError` correctly
  authors `BROKER_SNAPSHOT_STALE`; an adapter or programming failure *after* a
  previously clean snapshot authors neither stale uncertainty nor hold before
  `end_reconciliation` reopens admission. Preserve the invariant: a reconciliation
  pass that did not complete must not leave admission in the state a completed
  clean pass would. [#1616](https://github.com/tim1016/learn-ai/issues/1616)

- **Incomplete nonterminal ENTER records `RESOLVED_FAILURE` without terminalizing
  (low reachability, medium severity).** `sqlite/reconcile.py:221-257` — an ENTER
  effect with no captured order raises `ReconciliationInvariantError`; the catch
  records `RESOLVED_FAILURE`, increments `resolved_count`, and continues without
  terminalizing the effect or raising uncertainty/hold, so the final plan can
  return clean. Effect and order normally fold atomically, so ordinary
  valid-state reachability is low. Does **not** extend to malformed EXIT —
  `active_exit_for_strategy` independently blocks a later ENTER
  (`sqlite/uncertainty.py:363-385`). Preserve the invariant: counting an
  operation as resolved requires it to be terminal.
  [#1617](https://github.com/tim1016/learn-ai/issues/1617)

- **Legacy bot ENTER bypasses the stream-health gate (high, but see scope).**
  `clerk/effects.py:234-301` — legacy ENTER checks desired state and an existing
  hold, then calls `_submit_leg` directly, never consulting the installed
  dual-channel gate that protects `submit_for_instance`. Reachable only when the
  authority selector chooses legacy. **ADR 0038 note:** ADR 0037 retires legacy
  JSONL as a selectable Alpaca custody authority, so this seam resolves by
  **deletion**, not correction — the same pattern as ADR 0036 consequence 1 and
  `rollup_cache.py`. Do not write a regression test against a module scheduled for
  removal; verify the retirement closes it.
  [#1618](https://github.com/tim1016/learn-ai/issues/1618)

### Panel-layer flatness boundary (verified 2026-08-18)

Decision record: ADR 0036 (one flatness rule, `abs(q) >= 1e-9`, owned by
`folds.py::position_quantity_is_nonzero`). PR #1627 enforced it across the
backend fold, pre-flight, and IBKR position paths, and removed the Frontend's
own verdict. These sites were **not** in ADR 0036's consequence list — the
numeric census counted computation sites and missed the presentation layer.

All use `abs(x) > 0` (any nonzero) where the canonical rule is `abs(x) >= 1e-9`.
They agree everywhere except the open interval `(0, 1e-9)`, where these say
*exposed* and the canonical authority says *flat*.

**Reachability matters here, and the first sweep got it wrong.** On an activated
SQLite account — the live path — `panel_data_source.py:706` runs every panel
through `adapt_sqlite_panel`, which keeps only `resume` from the generic action
set (`sqlite_panel_adapter.py:56`, `SQLITE_PANEL_LIFECYCLE_ACTION_IDS =
frozenset({"resume"})`) and replaces the rest with `projection.recovery_actions`.
`_guard_resume` (`broker/v2panel/action_policy.py:146-172`) consults only
`ctx.resume_admission`. So nothing `presented_actions.py` derives from flatness
survives adaptation on an activated account. The three **live** sites are below;
the two `presented_actions.py` sites follow them, scoped as legacy-only.

- **The live exposure map is built with a non-canonical rule (medium).**
  `services/broker_v2_panel/sqlite_panel_adapter.py:110-114` filters
  `projection.positions` by `abs(position.attributed_qty) > 0` when constructing
  `BotPanelView.exposure`. This is the activated-SQLite path, and the resulting
  map is both what the Frontend reads to decide "Flat" and the `exposure`
  argument the two live sites below consume — so it is upstream of them.
  Preserve the invariant: the exposure map an operator surface reads is built
  with the canonical predicate.
  [#1628](https://github.com/tim1016/learn-ai/issues/1628)

- **The roster's flatness sentence is non-canonical (medium).**
  `services/broker_v2_panel/sqlite_panel_adapter.py:268` chooses between
  *"Off duty with Clerk-attributed exposure."* and *"Off duty and flat."* — the
  sentence rendered on every roster row (observed live on all 10 bots,
  `docs/audits/live-operator-surface-inventory-2026-08-18.md`). Preserve the
  invariant: the operator sentence and the custody verdict never disagree.
  [#1628](https://github.com/tim1016/learn-ai/issues/1628)

- **Resume labelling uses the same non-canonical rule (low).**
  `services/broker_v2_panel/panel_projection_service.py:170` chooses between
  *"Resume custody proof ready"* and *"Flat Resume ready"*. `adapt_sqlite_panel`
  does not replace the health card, so this copy survives on the live path.
  Affects copy only. [#1628](https://github.com/tim1016/learn-ai/issues/1628)

- **[Legacy path only] `presented_actions.py:61` and `:63`.** `has_exposure`
  (gating `flatten_stop`) and `account_expected_flat`, both `abs(x) > 0`. The
  first sweep filed these as live, and the `flatten_stop` one as high severity,
  on the assumption that the generic action set reaches an activated panel. It
  does not — see the reachability note above. They are therefore reachable only
  on the unactivated legacy path that **ADR 0037 retires**, and resolve by
  deletion, exactly like `rollup_cache.py` below. Do not pin a regression test
  to them; verify the retirement closes them.
  [#1628](https://github.com/tim1016/learn-ai/issues/1628)

**Not a defect to fix:** `broker/alpaca/clerk/rollup_cache.py:169` compares with
the wrong tolerance *and* the wrong inclusivity (`abs(updated) <= _ZERO_ABS_TOL`,
a lot-exhaustion constant). It was ADR 0036 consequence 1, but **ADR 0037
supersedes it** — the module is reachable only from the legacy JSONL path that
ADR 0037 retires, so it resolves by deletion. Do not write a regression test
against it.

### Resolved

- **[RESOLVED 2026-07-17] Transient account freeze permanently halted healthy
  running bots.** Active restart-intensity evidence now raises the non-terminal
  `TransientAccountFreezePauseError` (not a
  `ControlledLiveHaltError`); `live_engine` catches it, drops pending, and keeps
  the run alive until the authoritative provider reports the freeze cleared.
  Durable freezes
  (exposure/contamination) still halt via `AccountFreezeBlockError`. The safety
  invariant "never submit while frozen" is preserved (pending dropped at the
  gate for both). Because the transient path never raises a terminal error, the
  bot-event terminal classifier needed no change. Tests:
  `test_submit_pending_orders_pauses_not_halts_on_transient_restart_intensity_freeze`,
  `test_submit_pending_orders_resumes_after_restart_intensity_freeze_clears`,
  `test_live_engine_pauses_not_halts_on_transient_restart_intensity_freeze`.
  Original finding retained below for context.

  **[original finding]** (verified live 2026-07-17)
  `AccountFreezeBlockError` (`live_portfolio.py:1108`)
  is a `ControlledLiveHaltError` caught at the outer run loop (`run.py:2688`) →
  terminal `ExitReason.fatal_halt`. A **restart-intensity** freeze
  (`RestartIntensityPolicy`, threshold=3 / window=300000ms) starts from an
  expiring start-rate window, but its written account-freeze evidence remains
  active until clear. It previously HALTed any running bot on its next submit,
  so an unrelated restart-storm on the account killed healthy, unrelated bots,
  which then needed retire-and-replace. Reproduced today: 3 individual starts in
  <1 min froze the account and cascade-halted the running bot.
  **Decision (user-approved 2026-07-17): a running bot should _pause submits_ and
  keep running through a transient freeze, resuming when it clears** — rather than
  halt. Implementation is non-trivial and flips a safety invariant, so it needs an
  ADR: (a) classify freeze reason transient (restart_intensity) vs durable
  (exposure/contamination — keep halting); (b) move the transient case out of the
  terminal `ControlledLiveHaltError` path into a per-bar "skip submit, continue"
  branch; (c) re-evaluate the freeze each bar and resume; (d) update
  `bot_event_terminal_classifier` so a transient pause is not classified terminal;
  (e) regression test. See
  `docs/archive/reports/three-bot-concurrency-and-emergency-flatten-2026-07-17.md` §6.

## 2. Architecture-investigation P1 tier (re-verified 2026-08-17)

All five P0 safety issues from `architecture-investigation-2026-07-02.md` were
verified **fixed** in current code (unauth data plane now binds `127.0.0.1` +
HMAC control secret; panic-flatten stamps `order_ref`; recovery-flatten re-fetches
positions; freeze is clearable via `account_recovery_cli.py clear-freeze`;
IntentWal truncates its tolerated tail before append). The remaining P1s
carried forward are:

- Offline reconciliation/report bundle writers still publish Parquet and their
  companion JSON/hash files non-atomically. Live run artifacts, live bar
  compaction, and broker tick partitions use atomic publication; the remaining
  report-bundle work is research-output integrity rather than control-plane
  safety. [#1584](https://github.com/tim1016/learn-ai/issues/1584)
- Residual: committed dev-default control secret `local-dev-control-secret`
  (fine for local; must not reach a shared/live host).
  [#1585](https://github.com/tim1016/learn-ai/issues/1585)

The former R3 recovery-daemon item was retired from this backlog: it concerns
the deprecated IBKR bot-control surface, while the accepted Alpaca Clerk
cutover is complete (ADR-0035).

## 3. Broker subsystem (re-verified 2026-08-17)

The B-06 and B-09--B-13 items from the 2026-06-07 hunt are fixed in current
code and their regressions pass. The disconnect-blindness cluster (B-02/03/04/08)
still needs a separate reachability review. Remaining:

- **B-05** `cancel_paper_order` / `_order_belongs_to_account` match by `orderId`
  only → can cancel a *foreign* order on the same DU account; ownership check
  should be `account_id AND client_id` (`orders.py` / `order_projection.py`).
  *(also VCR-P3-H; [#1583](https://github.com/tim1016/learn-ai/issues/1583))*

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
- **Auto-clear of guards after clean broker recovery** — recovery keeps the
  engine `PAUSED` with operator-only resume; decide which guard states a clean
  recovery receipt may auto-clear vs. which stay manually acknowledged.

## 5. Daemon diagnostics — deferred phase-2 features

Shipped (ADR-0019, PR #910). Deferred, non-safety:

- Deploy/start last-error catalog via persisted `mutation_attempts`.
- clientId-collision detection via broker events.
- Logs / incidents link-outs; deep WAL / readiness checks.
- Account-level diagnostic rollup (`scope_ref` is per `strategy_instance_id` today).

## 6. Numerical-rigor & frontend debt (deferred, P2)

- **Golden-fixture coverage gap** — most canonical math still lacks a registered
  golden fixture; the `iv30/` snapshot sits outside manifest governance.
  *(was F-0026; deferred in `auto-research/state.json`)*
- **Frontend naive `new Date(string)` — Tier 2** — date-only params are still
  parsed browser-locally. The data-integrity Tier-1 case was fixed producer-side;
  Tier-2 is cosmetic-display risk. *(was F-0034)*
- **`FailureRow.ts_ms` mislabel** — a host-local time string is typed/named as
  `ms-UTC`; rename to `ts_local` and convert at ingestion. *(was VCR-P3-K)*

## 7. Functional findings parked in deleted run logs (not re-verified)

- **`exposure_pct` unit bug** — `bars_held_total` mixes 15-min strategy bars with
  a 1-min equity curve. Build-Alpha features **F6** (noise/robustness) and **F8**
  (parameter sensitivity) are unimplemented. *(2026-05-07 build-alpha run)*
- **ML-V-001** — Phase 3.0/3.5 canonical math not registered in
  `docs/math-sources-of-truth.md`. **ML-V-002** — provenance blocks missing on
  `research/parity/qc_reconciler.py` and the prediction-set `artifact.py`.
  *(2026-05-12 ML-predictions run)*
