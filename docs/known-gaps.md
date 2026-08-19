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

### Execution-path fail-open seams (verified 2026-08-18)

Source: `docs/audits/execution-path-fail-open-2026-08-18.md`, read at commit
`a7771477`. This sweep excluded #1614–#1618 and #1592's nine refuted candidates.

- **Activated SQLite: first in-flight position mismatch is admission-clean
  (high).**
  `sqlite/reconcile.py:121-184,363-381,777-825` classifies a mismatch on a
  captured working-order symbol as indeterminate, returns `clean`, and authors no
  blocker unless a prior POSITION_DRIFT already exists. Ordinary working bot
  ENTRY orders are not an independent new-exposure fence.
  [#1655](https://github.com/tim1016/learn-ai/issues/1655)
- **Retiring legacy JSONL: incomplete reconciliation facts do not fence submit
  admission (high).** `stale`/`missing_intent` project a freeze but legacy
  manual and bot ENTER read only holds; in-flight-suppressed position drift can
  return clean with `broker_facts_complete=False`, which protects Start but not
  existing effects or manual submission. ADR 0037 resolves this by deletion. Do
  not add legacy regression tests; verify retirement closes reachability.
  [#1656](https://github.com/tim1016/learn-ai/issues/1656)
- **Retiring legacy JSONL: unowned activity replay advances its cursor without a
  submit fence (high).** An unowned fill is durably accepted without a hold,
  while the bounded closed-order pass can omit its old-submitted order. ADR 0037
  resolves this by deletion.
  [#1657](https://github.com/tim1016/learn-ai/issues/1657)
- **Retiring legacy JSONL: direct hold clear has a same-millisecond evidence
  race (high).** `clerk.py:600-666` uses
  `since_ms > proof_observed_at_ms`; equal-time later unexplained evidence can be
  followed by `HOLD_CLEARED`. ADR 0037 resolves this by deletion. Do not fix or
  regression-test the retiring module; verify the route is unreachable.
  [#1658](https://github.com/tim1016/learn-ai/issues/1658)
- **Retiring legacy JSONL: reconciliation accepts a full 500-order page as
  complete (high).** An older working foreign order can be omitted from the
  descending page, allowing a false-clean proof to clear a hold while the order
  persists. ADR 0037 resolves this by deletion; do not add legacy pagination
  behavior or regression tests.
  [#1659](https://github.com/tim1016/learn-ai/issues/1659)
- **Retiring legacy JSONL: developer reset can erase unactivated authority and
  reinstall an empty writer (high).** The paper reset intentionally omits
  broker-flat and runner-roll-call proof, checks no legacy process when SQLite
  is absent, and records no startup reset fence without an established
  generation. Selection then reconstructs empty legacy authority. ADR 0037
  resolves this through no-authority fallback and deletion.
  [#1660](https://github.com/tim1016/learn-ai/issues/1660)

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
  Preserve one contract for both surfaces: a backend-authored account operator
  view with `dominant_blocker: OperatorBlocker | null`, backend status copy, and
  one action reference. Reuse ADR 0027 disposition; do not create a parallel
  five-state posture enum. Angular renders it and mutation endpoints still
  recheck.
  [#1664](https://github.com/tim1016/learn-ai/issues/1664)

- **Both live bot-detail lenses derive the banner's primary command (medium).**
  `Frontend/src/app/components/broker/v2-panel/bot-detail-banner/lifecycle-action.ts:12-19`
  selects Resume, Continue, or Stop from `health.running` and
  `health.desired_state`; the Trader and Operator banners render that result as
  their sole primary command, and Operator readiness uses it for suppression.
  Preserve the invariant: `BotPanelView.primary_action_id` is the sole
  backend-selected primary-action authority at the same revision as mission and
  actions. Banner and readiness consumers use only it; any retained recovery
  `evidence.primary` marker is diagnostic-only and must agree exactly. Angular
  fails closed when the reference is absent or inconsistent.
  [#1665](https://github.com/tim1016/learn-ai/issues/1665)

### Bot control-plane boundary (ADR 0038, verified 2026-08-18)

Decision record: ADR 0038 — Alpaca is the only bot control plane; SQLite is the
authority for the duty facts it already fences (`runs.state = 'ACTIVE'` under
`ux_runs_one_active_per_instance`, `strategy_instances.retired_at_ms`); the
evaluator plane retires with the IBKR bot-control surface. These are its open
consequences, re-verified line-by-line against current code. **Two of ADR 0038's
own consequence statements did not survive that re-verification and are corrected
here**; a dated correction note is on the ADR itself.

- **`services/end_day_intent.py` is dead code (no action; retires with the
  plane).** 241 lines; zero importers of `app.services.end_day_intent` anywhere
  in the repo (`live_engine.py`'s `_end_day_intent_active` is an unrelated
  instance attribute). Evaluator-plane, retires under ADR 0038 Decision 1.
  Recorded so its deletion is not mistaken for a behaviour change. [#1635](https://github.com/tim1016/learn-ai/issues/1635)

- **Evaluator-plane retirement inventory (no action here; sequence after the
  discriminator lands).** `engine/live/bot_lifecycle_evaluator.py`, its
  disposition receipt log, `engine/live/bot_lifecycle_fence.py`, the
  `routers/live_instances.py` deploy/start path, `run_ledger.json`, and the
  IBKR-lineage account-binding `DEPLOYED`/`ACTIVE`/`RETIRED` family. **The
  evaluator's live callers must be migrated or deleted first** — a repo sweep for
  `BotLifecycleEvaluator(` finds nine sites beyond the router:
  `engine/live/run.py:1877`, `:1961`, `:3022`; `engine/live/host_daemon.py:1340`;
  `engine/live/lifecycle_exit_finalizer.py:51`, `:115`;
  `services/bot_deletion.py:480`; `services/risk_reducing_lifecycle_intent.py:77`;
  and `services/end_day_intent.py:60` (dead, see above). The account-binding
  family likewise has safety consumers beyond the two obvious ones:
  `services/account_directory.py`, `routers/account_reconciliation.py`,
  `engine/live/account_classifier.py:268`, `engine/live/account_safety.py:1315`,
  `broker/ibkr/account_truth.py:857`. As with
  `rollup_cache.py` under ADR 0036 and the legacy ENTER seam above: **do not
  write a regression test against a module scheduled for removal** — verify the
  retirement closes it. [#1636](https://github.com/tim1016/learn-ai/issues/1636)

**Vocabulary hand-off.** "Deploy state" names four artifact families; two retire
with this plane. Naming the two survivors — SQLite registration/run folds, and
runner JSON instance/run records — is glossary work, tracked as item 6 of
[#1623](https://github.com/tim1016/learn-ai/issues/1623), not a defect here.

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
