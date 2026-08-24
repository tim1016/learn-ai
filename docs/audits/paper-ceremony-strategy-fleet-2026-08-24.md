# Paper-ceremony strategy fleet — 2026-08-24

Operator-directed day run: take five registry strategies through 2-year
backtests on **both engines** (Python Engine Lab + LEAN sidecar), classify the
cross-engine divergences, then paper-trade all five as 1-share SPY bots for the
rest of the session on Alpaca paper account `PA3KWXU1C4C3`. The operator
directed the run in-session, chose the gate-policy restoration described below,
and personally recorded the human-validation flags for the first strategies and
the first deploy; the remaining flags/pairings/deploys were executed by the
agent on explicit operator instruction, all receipt-backed.

Related commits (this repo, master): `02365e82` (backend gate restoration),
`20338171` (deploy-drawer override note), plus this document's commit.

## 1. Python-engine backtests (2024-08-26 → 2026-08-21, SPY, $100k, validated defaults)

| Strategy | Verdict | Composite | Trades | Net P&L | CAGR | Sharpe | Max DD |
|---|---|---|---|---|---|---|---|
| spy_strategy_c | A | 82 | 101 | +$60,693 | 26.9% | 1.86 | 9.3% |
| spy_strategy_b | A | 71 | 159 | +$19,379 | 9.3% | 1.00 | 13.1% |
| spy_strategy_a | A | 70 | 404 | +$31,960 | 15.0% | 1.40 | 15.3% |
| rsi_mean_reversion | B | 65 | 61 | +$23,554 | 11.2% | 0.95 | 15.8% |
| sma_crossover | B | 57 | 236 | +$8,196 | 4.0% | 0.42 | 9.6% |
| daily_sma_crossover | — | — | 0 | — | — | — | — |

All runs persisted as Strategy Lab studies. `daily_sma_crossover` could not
run: the engine cache has no daily SPY bars and the engine's availability path
does not auto-fetch daily resolution. **Platform gap:** the run returned
`success=True` with zero bars loaded and zero trades (study save then failed
with "no timestamps"); a zero-bar simulation should be a refusal, not a
success. Left unfixed today (documented, not patched mid-run).

## 2. LEAN sidecar runs + cross-engine reconciliation

QC mirror algorithms for the five strategies were written for this exercise
(session scratchpad; behavioral mirrors, **not** canonical implementations —
canonical stays `app/engine/strategy/algorithms/`). Each ran the same 2-year
window through the LEAN sidecar (`x2y_<key>_0824` run ids, pinned
`lean-sandbox:arm64-dotnet109` image), then
`POST /runs/{id}/cross-reconcile` diffed LEAN's fills against the Engine-Lab
strategy on the same staged workspace data.

| Strategy | LEAN fills | Engine fills | Matched | Divergences by category |
|---|---|---|---|---|
| sma_crossover | 475 | 476 | 475 | decision_mismatch 1 |
| rsi_mean_reversion | 121 | 122 | 121 | decision_mismatch 1 |
| spy_strategy_b | 324 | 322 | 322 | decision_mismatch 2, fill_price_drift 2 |
| spy_strategy_c | 200 | 202 | 200 | quantity_mismatch 32, fill_price_drift 2, decision_mismatch 2 |
| spy_strategy_a | 812 | 808 | 806 | quantity_mismatch 586, fill_price_drift 16, decision_mismatch 8 |

Classification (taxonomy per `.claude/rules/numerical-rigor.md`; non-blocking
per operator decision):

- **decision_mismatch (SMA=1, RSI=1)** — the single mismatch in each is the
  end-of-window liquidation (2026-08-21 Sell): the engine's
  `on_end_of_algorithm` flatten fills; LEAN's end-of-algorithm order does not.
  Window-boundary artifact, not a signal-logic divergence.
- **decision_mismatch (A=8, B=2, C=2 across ~2 years)** — borderline-gate bars
  where indicator warmup/seeding differences flip a threshold comparison
  (e.g. A 2025-05-05 Buy on LEAN only). Expected for behavioral mirrors whose
  indicator internals are not bit-matched; would route to Phase-3 indicator
  seeding work if strict parity were the goal.
- **quantity_mismatch (A=586, C=32)** — off-by-one share (e.g. 175 vs 174):
  LEAN vs Engine-Lab `SetHoldings(1.0)` share-rounding under slightly
  different equity paths; compounds with trade count (A trades 4× more than
  C). Known cross-engine sizing-primitive difference.
- **fill_price_drift (≤16 per strategy, pennies)** — e.g. |583.03 − 582.93|:
  consolidated-bar-close vs next-minute-bar fill timing. Would route to the
  fill-model parity work (`docs/references/fill-model-parity-spike-2026-05-19.md`).

Sidecar note: spy_strategy_b's LEAN run reported `is_clean=False` solely from
LEAN's statistics-packet builder ("Benchmark and performance series has 1
misaligned values"); order events were intact and reconciliation proceeded.

## 3. Gate-policy restoration (operator decision 2026-08-24)

PR #1746 (merged 2026-08-23) made Paper deployment of evidence-only strategies
unsatisfiable end-to-end. Three stacked blocks, found and fixed today in
`02365e82` + `20338171`:

1. **Pairing gate** required a fully accepted proof (QC audit copy + cloud run
   + reconciliation receipt). Restored: a *current* evidence-only flag event
   plans/confirms — the two-step content-addressed pairing review is itself
   the durable human override. Stale/rejected/missing events still refuse.
2. **Deploy layer** rejected the `evidence_override` outright, while **Start
   admission still required it** to verify an `evidence_only` fact — a latent
   contradiction (route tests used a stub registry, so it never surfaced).
   Restored: evidence-only Paper deploys *require* the override
   (acknowledgement + reason); accepted strategies still reject a superfluous
   one. The deploy drawer got its override panel back (`20338171`).
3. **Flag snapshots** recorded any UI-typed QC backtest ID, which for
   proof-less candidates guarantees permanent hash-staleness (the manifest
   side is `None`) — the root cause of the operator's Aug-16/Aug-20 flags all
   reading "no longer matches its current artifacts". Now a QC ID binds only
   when a registered proof exists to bind it to.

Boundary kept: the override accepts the *absence* of registered reference
artifacts, never the *drift* of recorded ones, and Live is untouched.

**Also repaired (state, not code):** the canary admission ledger written
2026-08-23 predates #1746's external head-checkpoint guard, so every pairing
review failed closed ("canary admission checkpoint is missing"). The ledger's
hash chain was verified and its checkpoint anchored via the module's own
writer; the operator's pre-existing `ema_crossover_signal` activation became
visible again. No admissions were fabricated.

Verification: full Python suite 8,324 passed (1 inherited env-only failure:
`tests/slow` polygon live-refetch, no API key on host venv); scoped Vitest for
the deploy workflow 26 passed; ruff + folder eslint clean.

## 4. Judgment calls

- **Window** 2024-08-26 → 2026-08-21: first/last completed sessions of the
  2-year span; local cache held SPY minute bars 2024-07-12 → 2026-08-18 so no
  Polygon-history truncation was needed.
- **All five bots on SPY** (operator interview): matches the strategies'
  SPY-native design, keeps the review apples-to-apples.
- `sma_crossover` (not `daily_sma_crossover`) represents the SMA family in
  the fleet; daily variant is backtest-only and currently data-blocked.
- Engine divergences **documented, non-blocking** for paper (operator
  interview decision).
- Commits are **local to master, not pushed** — push after this evening
  review.

## 5. The fleet

All five ON_DUTY under SQLite Account Clerk custody, mode=trade (not
observe-only), sealed programs SPY · 1 share, market data live, launched
~11:45–11:55 ET:

| Bot (strategy_instance_id) | Strategy | Deployed by |
|---|---|---|
| TRETRRETE | spy_strategy_a | operator (UI drawer, first end-to-end use of the restored override panel) |
| ceremony-spy-strategy-b-0824 | spy_strategy_b | agent (operator instruction) |
| ceremony-spy-strategy-c-0824 | spy_strategy_c | agent (operator instruction) |
| ceremony-rsi-mean-reversion-0824 | rsi_mean_reversion | agent (operator instruction) |
| ceremony-sma-crossover-0824 | sma_crossover | agent (operator instruction) |

Every step is receipt-backed: fresh `validated`/`evidence_only` flag events
(actor `local:root`), canary admission ledger events seq 2–6, deploy receipts
`alpaca-paper-deploy:PA3KWXU1C4C3:ceremony-*`, and the durable override note
on each deploy request.

Expectation set at launch: 15-minute-bar strategies averaging 0.1–0.8 trades
per session, entering midday — zero fills for the day is a plausible outcome;
the validated deliverable is the launch path and live gate pipeline either way.

## 5b. Afternoon: lifecycle exercise + validated-strategy fleet

Operator-directed follow-on (~12:45–13:15 ET), fully documented in
`bot-launch-ops-study-2026-08-24.md` and `judgment-calls-2026-08-24.md`:

- All five bots taken through **stop → resume** via the presented-actions API.
  The circuit surfaced a fleet-wide dead Resume (concurrency-token churn from
  un-normalized market-liveness evidence refs; 0/20 attempts) — fixed on
  master as `238821c7` with a pre-failing regression test; post-fix resumes
  took 0.5–4.1 s. `pause`/`continue` proved unreachable under SQLite custody
  (never presented); stop works via two parallel surfaces (panel ~20 s,
  legacy route 0.29 s).
- Three **validated-strategy bots** launched: `validation-spy-0824` and
  `validation-qqq-0824` (deployment_validation, accepted proof, fresh pairing
  review — plan 11 ms / confirm 17 ms), and `validation-ema-spy-0824`
  (ema_crossover_signal, pairing already active — direct deploy). Deploys
  0.24–0.48 s. Negative probes confirmed the override boundary both ways.
- Fleet is therefore **8 bots** from here to the close; §6 covers all eight.

## 6. Session results (16:00 ET close)

_To be completed at close: fills per bot, P&L, any attention flags, end-of-day
bot handling._

## 7. Follow-ups

- Zero-bar engine run reports `success=True` (§1) — should refuse.
- Daily-resolution auto-fetch missing in the engine availability path (§1).
- LEAN statistics-packet benchmark misalignment on the B mirror (§2) —
  cosmetic but worth a pin if LEAN runs become routine.
- QC mirrors live in session scratchpad only; if cross-engine checks of these
  five strategies become routine, vendor the mirrors under `references/` with
  provenance blocks.
- The UI flag form still *requires* a QC backtest ID for production
  candidates even though it is no longer recorded for proof-less ones —
  drop the hard-requirement client-side.
- Afternoon study findings F1–F5 (dead pause vocabulary, token-stability
  property, dual stop surfaces, feed-readiness cold start, refusal ordering)
  with recommendations: `bot-launch-ops-study-2026-08-24.md` §3/§5.
