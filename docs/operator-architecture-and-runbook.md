# Operator architecture & runbook

**Status:** Phase 12 v1 draft (2026-06-14)
**Scope:** Paper trading only.
**Audience:** The single developer/operator of this repo.

This is the single canonical manual. It supersedes `docs/broker-user-manual.html` and `docs/broker-user-manual.pdf` (those artifacts will be hard-deleted in a follow-up PR after their unique content is migrated here per PRD §11 manual-migration step).

The manual is written to reflect **shipped behavior**, not aspirational design. Sections that describe in-flight work are explicitly labelled with the VCR finding and/or PRD phase that gates them.

---

## 1. Operating mode

**This platform supports paper trading only.** It has never been validated for live-money execution and must not be operated as if it were. Every order, ACK, and fill flows against an IBKR paper account. The four-layer enforcement that keeps live orders impossible is described in §6.

If the platform is ever extended to live trading, the broker safety verdict (§6) must be cleared by the explicit `paper-only → live-validated` migration — not by toggling a config field. Today, all four layers default to paper and no migration path exists.

---

## 2. Architecture in one page

Three services run in `podman compose`:

| Service       | URL                          | Purpose                                                                     |
|---------------|------------------------------|-----------------------------------------------------------------------------|
| `frontend`    | http://localhost:4200        | Angular 21 SPA — Bot Control, strategy lab, snapshots                           |
| `backend`     | http://localhost:5000/graphql| .NET 10 GraphQL — portfolio, snapshots, valuation, options strategy reads   |
| `python`      | http://localhost:8000        | FastAPI — Polygon proxy, indicators, backtests, **live engine**             |
| `postgres`    | localhost:5432               | PortfolioSnapshot, Position, Trade, StrategyExecution                       |

The **live engine** sits inside `python-service` (`PythonDataService/app/engine/live/`) and owns all runtime IBKR interaction. The .NET backend never talks to IBKR.

Live runs do not write into Postgres — they persist into per-run artifacts under `PythonDataService/artifacts/live_runs/<run_id>/`:

```
artifacts/live_runs/<run_id>/
  live_config.json               # deploy ledger (sized + signed at start)
  intent_events.jsonl            # append-only WAL: PENDING_INTENT, SUBMITTED,
                                 #   ACK_FAILED_UNCERTAIN, SIZING_RESOLVED, ...
  executions.parquet             # broker-reported fills
  session_metadata.json          # ledger_account_id, connected_account, connection_epoch
  desired_state.json             # PAUSED | RUNNING | STOPPED (durable intent)
  halt.flag                      # written on any fatal halt
  ...
```

The bot control page reads these artifacts (plus the runner-process status) via FastAPI endpoints. Postgres is for research and backtest persistence, not live state.

---

## 3. Strategy catalog (deployable today)

These are the registry entries in `PythonDataService/app/routers/engine.py::_STRATEGY_REGISTRY`. Each row's **deploy key** is exactly the module name — that contract was pinned by Phase 2 of the remediation (VCR-0004 / ADR 0010).

| Deploy key                    | Display name                | Algorithm class                          | Notes                                                                                                                                  |
|-------------------------------|-----------------------------|------------------------------------------|----------------------------------------------------------------------------------------------------------------------------------------|
| `spy_ema_crossover`           | EMA Crossover               | `SpyEmaCrossoverAlgorithm`               | Bit-exact LEAN-port. Default SPY 15-min long-only. Detailed below.                                                                     |
| `spy_orb`                     | SPY Opening Range Break     | `SpyOrbAlgorithm`                        | 30-min ORB breakouts on SPY.                                                                                                           |
| `spy_ema_crossover_options`   | EMA Crossover (Options)     | `SpyEmaCrossoverOptionsAlgorithm`        | Same signal as `spy_ema_crossover` but trades nearest-expiry call options.                                                             |
| `sma_crossover`               | SMA Crossover               | `SmaCrossoverAlgorithm`                  | Bit-exact LEAN-port. SMA(10)/SMA(30).                                                                                                  |
| `daily_sma_crossover`         | Daily SMA Crossover         | `DailySmaCrossoverAlgorithm`             | Daily-bar variant of `sma_crossover`.                                                                                                  |
| `rsi_mean_reversion`          | RSI Mean Reversion          | `RsiMeanReversionAlgorithm`              | Bit-exact LEAN-port. RSI(14) bounded by 30/70.                                                                                         |
| `spy_strategy_a`              | SPY RSI-range A             | `SpyStrategyA` (`_rsi_range_base`)       | EMA-gap + MACD entry; ADX<15 exit.                                                                                                     |
| `spy_strategy_b`              | SPY RSI-range B             | `SpyStrategyB` (`_rsi_range_base`)       | Supertrend + ADX>20 + MACD entry; ADX<20 exit.                                                                                          |
| `spy_strategy_c`              | SPY RSI-range C             | `SpyStrategyC` (`_rsi_range_base`)       | ADX>20 + ADX-rising entry; ADX<15 exit.                                                                                                |
| `deployment_validation`       | Deployment Validation       | `DeploymentValidationConsecutiveGreen`   | Internal — used by deploy-flow smoke tests, not an alpha strategy.                                                                     |

Two algorithm files in `algorithms/` are **intentionally not registered** and must not be deployed: `buy_and_hold.py` and `spy_vwap_reversion.py` (reference primitives for cross-engine reconciliation per VCR-P3-B). The runner gates deploy keys against the registry, so a hand-edited deploy attempting one of these will fail at start with a clear "not deployable" error.

### 3.1 EMA Crossover exact behavior

The reference strategy is documented in detail because it is the LEAN-parity anchor.

- **Universe:** configurable; default `SPY`.
- **Resolution:** 15-minute bars consolidated from 1-minute feed.
- **Indicators (recomputed at every 15-minute bar close):**
  - `EMA_fast = ExponentialMovingAverage(5)`
  - `EMA_slow = ExponentialMovingAverage(10)`
  - `RSI = RelativeStrengthIndex(14, Wilders smoothing)`
- **Warmup:** no signals fire until `EMA_fast.is_ready` and `EMA_slow.is_ready`.
- **Entry condition (ALL of):**
  - Fresh crossover this bar: `EMA_fast > EMA_slow` now AND `EMA_fast <= EMA_slow` previous bar
  - Minimum EMA gap: `EMA_fast - EMA_slow >= 0.20`
  - RSI in the trend-confirmation band: `50 <= RSI <= 70`
  - Currently flat (only one position at a time)
- **Order:** `SetHoldings(symbol, 1.0)` — submitted as MARKET at the next bar open.
- **Exit:** unconditional after 5 × 15-minute bars (75 minutes wall-clock). No stop, no target, no scaling.
- **Session window:** RTH only (09:30–16:00 ET). Positions are closed before close if 75-minute timer would carry past.

The bit-exact LEAN reference is at `references/lean/7986ed0aade3ae5de06121682409f05984e32ff7/` and the parity test lives in `PythonDataService/tests/engine/strategy/algorithms/test_spy_ema_crossover_parity.py`.

---

## 4. Deployment flow

A live deploy crosses four layers:

```
Broker bot control (Angular)
        │  Deploy form (broker-deploy-form.component)
        ▼
GraphQL mutation runDeployLiveRunner  ─→  .NET Backend  ─→  POST /api/host-runner/deploy
                                                                     │
                                                              FastAPI deploy endpoint
                                                              (PythonDataService/app/routers/live_runs.py)
                                                                     │
                                                              host_daemon.py /deploy
                                                              (host-side subprocess)
                                                                     │
                                                              app/engine/live/run.py start
```

Each layer enforces an explicit slice of the contract:

1. **Deploy form** — UI picks `strategy_key` (= module name, see §3) and `sizing` (Safe canary / Reference parity / Custom). Per ADR 0009 the safe default is `FixedShares(1)`; the operator must consciously choose otherwise.
2. **GraphQL → FastAPI deploy** — validates the request shape with the Pydantic `HostRunnerDeployRequest` model. Phase 1 of the remediation makes the `sizing` field mandatory (VCR-0001); empty `live_config` is rejected with HTTP 422 before reaching the host daemon.
3. **Host daemon `/deploy`** — authoritatively writes the deploy ledger at `artifacts/live_runs/<run_id>/live_config.json`, hashed into `run_id` per ADR 0006. The shared-secret token in the `X-Live-Runner-Token` header is verified by `host_daemon._verify_token` using `hmac.compare_digest` (Phase 7C closure of VCR-0011).
4. **Runner `start`** — Phase 3 (#495) compares `ledger.account_id` against `IbkrClient.connected_account` using the strict normalization at `account_identity.py`. Mismatch refuses to start and surfaces the raw values in the failure entry. Reconnect re-validation (PRD §11 C) is the deferred VCR-0006 follow-up.

### 4.1 Pre-flight checks

Per ADR 0011, the runner re-runs these gates at start (not only at deploy) and writes `halt.flag` on any failure:

- `halt.flag` not already present
- Working tree clean if `clean_tree_required = true` in `live_config`
- NTP healthy if available (advisory only)
- `live_config.sizing` resolves to a valid policy
- IBKR connectivity and account-prefix verdict (paper-only)
- Per-instance start lock (Phase 6D, VCR-P3-P/Q closed) — refuses to start a second process for the same instance

### 4.2 Mandatory sizing (VCR-0001 closure)

`live_config.sizing` cannot be omitted. The deploy boundary refuses an empty `live_config`, an unknown sibling key (the allow-list is `LIVE_CONFIG_LEDGER_KEYS`), or a missing `sizing` field. Legacy pre-policy ledgers may be **viewed** in the bot control page but `cmd_start` refuses to start them — the only forward path is redeploy with an explicit sizing.

---

## 5. Bot Control states and commands

The bot control page surfaces the desired-state machine + one-shot commands separately. Phase 6A of the remediation (closure of VCR-0007) hardened the contract so every button has one documented effect.

### 5.1 Desired-state intents

| State    | Meaning                                                                   |
|----------|---------------------------------------------------------------------------|
| `RUNNING`| The runner accepts new bars and emits new orders if signals fire.          |
| `PAUSED` | The runner is alive but refuses to enter new positions.                   |
| `STOPPED`| The runner process exits. Subsequent operations require a redeploy.       |

Resume from `PAUSED` is a **guarded write** (Phase 7A): the runner consults the broker safety verdict and refuses to flip to `RUNNING` unless `final_verdict == "paper-only"`. The bot control page surfaces the blocking gate.

### 5.2 One-shot commands

| Command              | Effect                                                                                                      |
|----------------------|-------------------------------------------------------------------------------------------------------------|
| `FLATTEN_NOW`        | Pure one-shot: cancel owned open orders, then liquidate net positions. Does **not** mutate desired state.   |
| "Flatten and pause"  | Composite endpoint: write `desired_state = PAUSED`, then enqueue `FLATTEN_NOW`. Process stays alive.        |
| `STOP`               | Graceful shutdown with optional flatten only if `--with-flatten` is explicitly set. Returns                  |
|                      | `still_running_after_2s` when the SIGTERM is not honored (Phase 6B / VCR-0018-B).                            |
| `EMERGENCY_FLATTEN`  | Out-of-process force-flat path; requires operator confirmation. Phase 5C's `force=True` carve-out (gated).  |
| `RECONCILE`          | **Runtime no-op** (Phase 4 / VCR-0008). The bot control page banner explains that restart safety is not fully wired  |
|                      | until ADR 0008 Phases 5C/5D/5E ship. The CLI verb still exists for forward compatibility; the runtime       |
|                      | handler returns `{result: "accepted_noop", reason: "runtime_reconcile_not_wired"}`.                          |

### 5.3 The broker safety verdict (Phase 7A)

The verdict is computed server-side and shown as the bot control page hero band:

| Verdict        | Hero color | Operator interpretation                                                              |
|----------------|------------|--------------------------------------------------------------------------------------|
| `paper-only`   | green      | All four gates positively verify paper: mode, readonly flag, port, account prefix.   |
| `unsafe`       | red        | Any gate positively indicates a live/non-paper path. Orders blocked at the engine.    |
| `unknown`      | amber      | A required gate is missing. Run start is blocked unless the diagnostic path is used. |

Phase 7A (#502, VCR-0010) wired the verdict surface; Phase 7B (deferred) is the enforcement half — verdict-driven order block, mid-session transition halt, Resume guard.

---

## 6. Sizing policies and audit (ADR 0009)

`live_config.sizing` is a Pydantic discriminated union:

```jsonc
{ "kind": "FixedShares",   "value": 1 }                 // safe default
{ "kind": "SetHoldings",   "value": "1.0" }             // % of account; LEAN-parity sizing
{ "kind": "FixedNotional", "value": "10000" }           // dollars; floor(notional / price)
{ "kind": "StrategyExplicit" }                          // strategy uses market_order
```

The percent path (`SetHoldings`) delegates to `LeanSetHoldingsSizing` — the canonical LEAN-faithful, buffered, fee-aware quantity-math authority. Legacy `SimpleFloorSizing` was retired from the live path; a pinned regression test documents the intentional share-count shift (e.g., 199 vs 200 on the standard SPY-EMA scenario).

### 6.1 The Sizing card audit

Every sizing decision the engine makes during a live run is captured. As of Phase 8 (VCR-0003 partial closure):

- For every minted `intent_id`, a `SIZING_RESOLVED` WAL event is appended to `intent_events.jsonl` **before** the `PENDING_INTENT` for the submit. The event carries `intent_id`, `policy_kind`, `policy_value`, `intended_qty`, `reference_price`, `sizing_provenance_at_resolve_time`, and `sized_via`. This is the durable audit trail the Sizing card and the per-trade provenance row will join on.
- An in-memory `sizing_resolutions` list is still maintained on `LivePortfolio` for the current Sizing card render; the cutover to a WAL fold is PRD §8 step 6, deferred.
- `SIZING_SKIP` (no intent_id) is deferred (see VCR-0003 follow-up): the `IntentEvent` model currently requires non-empty `intent_id` + `order_ref`, and relaxing those would ripple through the fold (`intent_ledger.py`) and ColdStartReconciler.

### 6.2 Sizing provenance

`sizing_provenance_at_resolve_time` distinguishes:

- `live_override` — sized by the live engine's `OrderSizer` (the only PR1 value today).
- (future) `qc_audit_copy` — sized from the static QC audit copy, when the QC-parity rail is added.

This is the audit anchor: every fill traces to a specific sizing rule with a verifiable provenance.

---

## 7. Pre-deploy checklist

Before a paper deploy:

- [ ] IB Gateway / TWS is on the paper port (default 7497).
- [ ] `IBKR_MODE=paper` and `IBKR_READONLY=true` in the runner's environment.
- [ ] The deploy form's `sizing` field is explicit. The Safe canary radio is the default; **never** unselect it to "let it default" — there is no default.
- [ ] The Provenance card shows the audit-copy SHA verified against the on-disk audit copy. The QC Cloud backtest ID is operator-recorded — it is **not** automatically verified (Phase 7D closure of VCR-0014); do not treat it as a positive proof of QC approval.
- [ ] Working tree is clean if the strategy requires it (`clean_tree_required = true` in the live config).
- [ ] No existing `halt.flag` is present in the run dir.
- [ ] **The bot control page env chip in the page utility row reads `PAPER` AND the identity-strip `SAFETY` indicator reads `PAPER_ONLY`.** They are driven by the same server-authored `operator_surface.broker.safety_verdict` (ADR-0011 + ADR-0013 §1, locked in by P1-001 of the 2026-06-22 audit). If either one reads `UNSAFE` or `UNKNOWN`, stop and resolve before deploying — the verdict is fail-closed.

---

## 8. Emergency procedures

### 8.1 "Flatten and pause"

The bot control page's primary panic button. Two steps composed:

1. Persists `desired_state = PAUSED` to the run-dir sidecar.
2. Enqueues `FLATTEN_NOW` one-shot.

The runner keeps the process alive (PAUSED, not STOPPED) so the operator can inspect state. Re-entering bars require an explicit Resume click.

### 8.2 Emergency flatten without ownership proof

This is an explicit out-of-process path with operator confirmation. Today it is gated by Phase 5C — the durable-submit ownership-query work hasn't shipped. The CLI variant exists; the bot control page-confirmed button is deferred.

When invoked with `force=True`, it liquidates broker-account-net positions and writes an `EMERGENCY_FLATTEN_WITHOUT_OWNERSHIP_PROOF` audit event. Use only as a last resort — without ownership proof, foreign positions could also be touched.

Phase 5C also adds the **cancel-then-liquidate** ordering that the current `cmd_emergency_flatten` is missing (VCR-0009). Until Phase 5C ships, an emergency flatten can race with an open bot order and over-sell. The mitigation is to PAUSE first, observe the open-order panel is empty, then flatten.

### 8.3 Recovery flatten (`_recovery_flatten`)

This *does* cancel-then-liquidate today and is the safer of the two paths. It is what the runner invokes from cold-start cleanup when it sees a stale run that needs draining.

### 8.4 Hard stop

If the bot control page / daemon paths are unresponsive:

```
podman exec polygon-data-service pkill -TERM -f "engine.live.run start"
```

Then in IB Gateway / TWS, manually close any positions or cancel any orders that the runner left behind. Note: hard stop bypasses the WAL ordering guarantees — the next start of that `run_id` will go through `ColdStartReconciler.verify()` (Phase 5B, #aae1cf2c) before any new submits, and will halt if it cannot classify the divergence.

---

## 9. Troubleshooting

### 9.1 Deploy fails with `live_config.sizing` is required

Phase 1 (VCR-0001) made `sizing` mandatory. Open the deploy form and pick a sizing policy. Do **not** add a CLI flag to bypass — there is none, and the run_id hash depends on the sized live_config, so a bypass would make identity dishonest.

### 9.2 Runner refuses to start with "account identity mismatch"

`ledger.account_id` is checked against `IbkrClient.connected_account` with strict normalization (uppercase, trim, no internal whitespace) per Phase 3. The error includes both raw values. Resolution: redeploy. **Do not** edit the ledger by hand — `account_id` is part of `run_id`'s identity hash.

### 9.3 Strategy key not found

Phase 2 (VCR-0004) makes the deploy key the canonical module name. If the bot control page dropdown shows it but the runner can't import it, the registry is out of sync with `algorithms/`. The intersection check at registry load time will fail noisily; check the runner logs.

### 9.4 "Re-sync now" / `RECONCILE` returns `accepted_noop`

Expected behavior. Phase 4 (VCR-0008 closure) removed the runtime affordance because the durable-submit reconciler (Phases 5C/5D/5E) hasn't shipped. The banner explains the gap. If you need cold-start reconciliation, stop the runner cleanly and start it again — `ColdStartReconciler.verify()` (Phase 5B, #aae1cf2c) runs at every start.

### 9.5 Bot Control `SAFETY` indicator reads `UNKNOWN` (page utility env chip also reads `UNKNOWN`)

One or more gates of the broker safety verdict is not positively verifiable:

- `configured_mode` — check `IBKR_MODE`.
- `readonly_flag` — check `IBKR_READONLY` (informational only since the ADR-0011 amendment / PRD #619-A — read separately from the identity verdict).
- `port_class` — port is not in the known `PAPER_PORTS` set (`{7497, 4002}`).
- `connected_account_prefix` — account string does not start with `DU`.

The fail-closed derivation degrades to `unknown`; the runner refuses to start a new run (Phase 7A). Resolve the gate, then redeploy.

The env chip and the identity-strip `SAFETY` indicator always agree (both project from `operator_surface.broker.safety_verdict` per ADR-0013 §1; pre-2026-06-22 audit, the env chip was driven by status truthiness and could disagree — that regression is now structurally blocked, see P1-001).

### 9.6 Action button is disabled and the tooltip is a code I don't recognize

If an action button (Resume, Pause, Flatten and pause, Stop, Mark POISONED) shows a tooltip starting with `Unrecognized reason code:` followed by a raw `SCREAMING_SNAKE_CASE` string, the server returned a reason code the bot control page does not have copy for yet. The raw code is preserved verbatim in the tooltip so it is visibly diagnosable (no silent generic-success copy — see `Frontend/src/app/components/broker/bot-control/lib/disabled-reason-copy.ts`, which is shared by the current bot control implementation).

Resolution: add the code to `OperatorReasonCode` in `disabled-reason-copy.ts` and an operator-language entry to `OPERATOR_REASON_COPY` in the same file. The parity test `disabled-reason-copy.spec.ts` fails on any drift between the bot control page map and the server's closed vocabulary (`REASON_CODES ∪ RESUME_REASON_CODES`).

### 9.7 Pre-existing failure: `halt.flag` from a prior run

Phase 6D (VCR-P3-P/Q closure) re-runs the `halt.flag` pre-flight check at every start, not only at deploy. If a prior run wrote a halt, the runner refuses to start. Inspect the halt cause and decide whether to clear (`rm halt.flag`) or redeploy. Never silently clear a halt for a SUBMIT_UNCERTAIN / cold-start divergence cause — those bear forensic evidence.

---

## 10. Known gaps (PRD §9 done definition)

These are the in-flight remediations the bot control page and runtime do not yet enforce. Each is tracked by a VCR finding so the audit trail is honest about what is and isn't shipped.

| Gap                                                                             | Tracked under          | Status                                          |
|----------------------------------------------------------------------------------|------------------------|-------------------------------------------------|
| Ownership-query gates on cancel/flatten paths; cancel-then-liquidate ordering     | VCR-0009, Phase 5C     | Pending — re-grounded; gated on this work       |
| Submit retry state machine + `SUBMIT_UNCERTAIN_HALT`                              | Phase 5D               | Pending                                         |
| Fill-conversion uses durable ownership classifier (perm_id / order_ref fallback)  | VCR-0012, Phase 5E     | Pending — re-grounded                           |
| Verdict-driven order block + mid-session transition halt + Resume guard           | Phase 7B               | Pending                                         |
| Reconnect re-validation of ledger↔broker account identity                         | VCR-0006, Phase 3 follow-up | Pending — gating decisions captured in finding |
| `SIZING_SKIP` WAL event + Sizing card cutover from in-memory list to WAL fold      | VCR-0003, Phase 8 follow-up | Pending                                         |
| Sentinel pill, "Live mode" dialog, readiness labels, NY timestamp formatter, ts_ms tail | VCR-0018-A/C/D/E/J/K | Pending Phase 7 mechanical follow-ups           |

When these ship, the relevant runbook section above gets an "Updated" line and the gap row is removed.

---

## 11. Evidence appendix

| Claim                                                          | Source                                                                  |
|----------------------------------------------------------------|-------------------------------------------------------------------------|
| Sizing policy is mandatory at deploy                            | `app/schemas/live_runs.py::_validate_sizing`                            |
| Account identity normalization is strict                         | `app/engine/live/account_identity.py::normalize_account_id`             |
| Daemon token compare is constant-time                            | `app/engine/live/host_daemon.py:902-913` (Phase 7C)                     |
| Per-instance start lock                                         | `app/engine/live/runner_process_manager.py` (Phase 6D)                  |
| RECONCILE runtime no-op contract                                 | `app/engine/live/live_engine.py::cmd_reconcile` (Phase 4)               |
| Intent identity invariant: `order_ref == namespace:intent_id`   | `app/engine/live/intent_events.py::IntentEvent._check_order_ref_invariant` |
| SIZING_RESOLVED emitted before PENDING_INTENT                    | `app/engine/live/live_portfolio.py::set_holdings` (Phase 8)             |
| Cold-start reconciliation runs at every start                    | `app/engine/live/cold_start_reconciler.py` (Phase 5A/5B)                |
| Broker safety verdict server-side                                | `app/broker/safety_verdict.py` (Phase 7A)                                |
| Entry-Greek aggregates removed from PortfolioValuation           | `Backend/Services/Implementation/PortfolioValuationService.cs` (Phase 9) |

---

## 12. Change log for this manual

- **2026-06-14, v1 draft** — initial commit per remediation PRD §12. Reflects shipped state through commits aae1cf2c (Phase 5B) and the VCR remediation Phases 1-11 batch. Gaps in §10 are sourced from the live VCR finding statuses.
