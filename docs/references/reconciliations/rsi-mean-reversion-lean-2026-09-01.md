# RSI Mean Reversion — LEAN parity validation (2026-09-01)

## Scope

This receipt validates the canonical strategy
`PythonDataService/app/engine/strategy/algorithms/rsi_mean_reversion.py::RsiMeanReversionAlgorithm`
against `RSI_MEAN_REVERSION_SOURCE` in the LEAN sidecar
(`app/lean_sidecar/trusted_samples/rsi_mean_reversion.py`), the named LEAN
template registered as this strategy's `lean_twin`.

Before this receipt the strategy had no LEAN counterpart at all: its Python
side was fully sealed (pinned `golden_trace_root`, `validated_settings`,
`equivalence_level="bit_exact"`, three parity tests) but `lean_twin` was
`None`, so `resolve_strategy_lean_source` raised and Strategy Lab filtered it
out of the LEAN-validatable list. The Python-side seal was never a LEAN claim,
and this document is the first cross-engine evidence for the strategy.

As with `ema_crossover_signal`, LEAN necessarily trades a concrete subscribed
equity while the canonical Python strategy emits asset-agnostic ENTER/EXIT
intents. Engine Lab binds those intents to the same signal symbol the template
subscribes to. Action Plan asset selection is an execution-boundary concern and
is **not** claimed here.

## Pinned runtime and data contract

- **LEAN image:** `localhost/learn-ai/lean-sandbox@sha256:3dd003372f1ef1981b4e80038e3f1c557f1fe414d1be531f485ef870f81a5771`
- **Twin source sha256:** `f9f1488f1b4e654770973d14aae8c5f6c37e2176a3217fe1be63bfd72e1d644c`
- **Repository commit:** `212a824c84bddf08cbe734a9c080c528dac106d1`
- **Brokerage:** Interactive Brokers, Margin; LEAN `ImmediateFillModel` and
  `InteractiveBrokersFeeModel`.
- **Bars:** Polygon-sourced, **raw** (unadjusted), regular-session, one-minute
  equity bars read through the read-only lake mount; both engines consolidate
  to fifteen-minute signal bars.
- **Signal constants:** Wilders RSI(14), entry strictly below 30 while flat,
  exit strictly above 70 while in trade, no time stop, end-of-run flatten.
  These are class constants in the twin, not `GetParameter` values, and the
  registration forwards no `lean_parameter_names` — a run overriding them is
  reported `parameters_unrepresentable_by_twin` rather than compared against a
  twin still running 14/30/70.

**Raw bars, not adjusted.** The LEAN runtime consumes raw bars only, so
`parity_companion.companion_ineligibility_reason` rejects an adjusted policy
outright. Strategy Lab run 198 (the run that prompted this work) was
`adjusted: true` and would have returned `adjustment_unsupported` even with the
twin wired. Both engines here run `adjusted: false`. Over the full
2024-08-30 → 2026-09-01 window the raw policy yields 61 trades against the
adjusted policy's 61 — same count, different win/loss split (41/20 vs 40/21)
and different equity, which is the expected dividend-driven divergence and not
a parity result.

## Results

Both cells were produced through the parity-companion path
(`POST /api/engine/backtest` with `requested_engine: "both"`), which dispatches
the LEAN twin under a shared `parity_group_id` and freezes a verdict.

| Ticker | Window | Sessions | Trades | Engine-Lab exec | LEAN exec | Parity group |
| --- | --- | ---: | ---: | ---: | ---: | --- |
| SPY | 2026-02-02 → 2026-04-30 (W3mo) | 62 | 8 | 201 | 202 | `pg-1ef2ef27539b413a9628` |
| SPY | 2025-11-03 → 2026-04-30 (W6mo) | 123 | 15 | 203 | 204 | `pg-0b32e6963ac8470e94ca` |

Headline aggregates are **identical**, not merely within tolerance:

| Metric | W3mo Engine Lab | W3mo LEAN | W6mo Engine Lab | W6mo LEAN |
| --- | ---: | ---: | ---: | ---: |
| Trades | 8 | 8 | 15 | 15 |
| Winning trades | 5 | 5 | 10 | 10 |
| Total P&L | 5008.60 | 5008.60 | 7286.6646 | 7286.6646 |
| Final equity | 105008.60 | 105008.60 | 107286.66 | 107286.66 |
| Total fees | 16.00 | 16.00 | 30.00 | 30.00 |

Per-cell gate results:

| Gate | W3mo | W6mo |
| --- | --- | --- |
| Trade-level divergences (8-category taxonomy) | **0** | **0** |
| Input parity (bar-store fixture) | match, 15 fields | match, 15 fields |
| Readiness parity | match, 17 fields | match, 17 fields |
| LEAN native metrics | 65 of 66 match | 65 of 66 match |

Input fixture hashes: W3mo `0eb06aa97f4e9159b4b73a89dbad29b551922930611065a0c39cbaab754039e4`,
W6mo `e89d2b230a31d8dfdd5617a19088686e84c9671145cba54c459c2b848f359bd9`.

## Accepted divergence: median duration convention

Both cells report `status: diverged` with
`reason: lean_native_metric_mismatch`, and in both the divergence count is
exactly **1 of 66**. It is the same defect in both, and it is **not in the RSI
strategy logic** — it is in shared LEAN-compatible statistics code:

| Cell | Metric | LEAN native | Ours |
| --- | --- | --- | --- |
| W3mo | `medianTradeDuration` | `7.00:15:00` | `6.12:07:30` |
| W6mo | `medianWinningTradeDuration` | `6.00:30:00` | `6.00:15:00` |

**Root cause.** `median()` in `app/engine/results/lean_statistics.py` returns
the arithmetic mean of the two middle values on an even-length input:

```python
return ordered[middle] if len(ordered) % 2 else (ordered[middle - 1] + ordered[middle]) / 2
```

LEAN's `Median()` extension is a QuickSelect at index `n / 2`, which for even
`n` returns the **upper-middle element** with no interpolation. The two agree
on odd counts and disagree on even ones.

**Confirmed, not inferred.** The W3mo cell's eight sorted durations are
`1.22:30:00, 3.03:30:00, 5.02:45:00, 6.00:00:00, 7.00:15:00, 7.23:30:00,
11.00:00:00, 11.22:16:00`. The upper-middle element is `7.00:15:00` (exactly
LEAN's value) and the mean of the two middle values is `6.12:07:30` (exactly
ours). The W6mo cell is an independent confirmation of the same rule: 15 total
trades (odd) → `medianTradeDuration` **agrees**; 10 winning trades (even) →
`medianWinningTradeDuration` **diverges**. The defect fires on even-count
subsets and only there.

**Disposition.** Reported, not fixed here. This is pre-existing shared code
affecting `medianTradeDuration`, `medianWinningTradeDuration`, and
`medianLosingTradeDuration` for *every* strategy with an even-count subset; the
helper is strategy-independent and nothing about RSI reaches it. Correcting it
changes persisted statistics repo-wide and
belongs in its own change with its own regression test, not smuggled into the
PR that introduces this twin. Per `numerical-rigor.md` the divergence is **not**
accepted as tolerance: the convention is simply wrong relative to the reference
and should be repaired.

Because the sole divergence is a statistics-formatting convention with zero
trade-level, input, or readiness divergences across both cells, this receipt
records the RSI mean reversion **signal and execution** logic as reconciled
against LEAN, and the median convention as a separate open defect.

## Not covered

- **Cross-engine golden matrix cells.** Gate 2's comparator
  (`parity_matrix/state_parity.py`) hardcodes an EMA-shaped `state.csv` schema
  (`ema_fast`, `ema_slow`, `cross_state`) and `Cell.cell_id` has no strategy
  axis, so the committed matrix is implicitly EMA-only. This twin emits an
  honest RSI-shaped `state.csv` (`ts_ms_utc,close,rsi,signal`) rather than dummy
  EMA columns, which would fake the exact agreement that gate exists to detect.
  Extending the matrix to a second strategy is separate work.
- **`POST /runs/{id}/cross-reconcile`.** Unusable for any current
  Polygon-sourced run: `cross_runner.py` wires its reader at
  `<workspace>/data`, but lake-mode runs read from the read-only lake mount and
  leave that directory empty, so the endpoint returns
  `workspace_data_missing`. This is pre-existing and strategy-independent; the
  parity-companion path above was used instead.
- Symbols other than SPY. `validated_symbols` also lists AAPL, QQQ, and TSLA.
