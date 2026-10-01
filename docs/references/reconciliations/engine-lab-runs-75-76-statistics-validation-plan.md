# Engine Lab runs 75 and 76 — LEAN-oracle equity and statistics validation plan

**Status:** implemented 2026-08-08. New Compatibility pairs pin the exact shared
raw-bar fixture, LEAN runtime/source/binary provenance, complete 17/17 readiness
inputs, common performance-memory primitives, lossless LEAN artifacts and
analysis, all native statistics, and a 66-value plus 25-string LEAN Oracle
receipt. The gate fails closed on input, trade, readiness, or native-calculation
divergence. Historical runs are intentionally not backfilled. The proposed
FRED performance-risk-free contract remains a separately named future platform
metric; the product does not claim it is already in use.

Final live acceptance pair 95/96 passed every gate and is now committed as the
immutable `engine-lab-compatibility-95-96-v1` golden fixture: exact bar bytes,
fill mode, five trades, 66 native numerical values, 25 formatted dashboard
values, all 17 readiness inputs, identical C / 44 / Rework readiness,
performance memory, and three losslessly rendered LEAN analysis findings. The
offline reconciliation test replays run 96's retained workspace through the
current Python adapter and fails on input-hash, trade, readiness, native-stat,
analysis, or final-verdict drift.

**Investigated:** 2026-08-08

**Subject:** SPY `ema_crossover_signal`, 2024-08-08 through 2026-08-07,
$100,000 starting capital, 15-minute strategy bars

**Runs:** Python Engine Lab run 75 and LEAN sidecar run 76

## Executive conclusion

Runs 75 and 76 must not be presented as a successful or failed parity pair.
They are separate runs with materially different execution and measurement
contracts:

1. Run 75 used Engine Lab's ordinary `signal_bar_close` execution path with
   `SimpleFloorSizing`. Run 76 used LEAN `SetHoldings`, Interactive Brokers
   fees, and next-available-market-open fills for stale session-close signals.
2. Five entries and one exit therefore occurred at different timestamps and
   prices. Fifty-nine of 73 trades used different quantities.
3. The Python and LEAN equity curves are not the same artifact. Python retained
   10,000 points from 194,490 minute snapshots. The application imported only
   105 points from LEAN's reduced summary, but the retained full LEAN result has
   1,728 Strategy Equity samples and ends at the exact terminal equity on
   2026-08-07. The apparent two-session LEAN gap is an importer source-selection
   defect, not a missing terminal point in the full LEAN output.
4. The displayed Python KPIs, the Python readiness verdict, and the Python
   LEAN-style statistics are not all calculated from the same metric payload.
5. LEAN used its interest-rate provider when computing risk-adjusted statistics;
   the Python LEAN-style calculator was invoked with a zero risk-free rate.
6. Run 75 is explicitly marked `adjustment_unsupported` for cross-engine parity,
   and run 76 has no parity-group identifier. Run 76's own manifest calls its
   input policy `pre_adjusted_non_reconciliation`.
7. For a future identical-settings pair, Python must reproduce every exposed
   LEAN-native metric from LEAN-compatible primitives. The separately named
   FRED-based platform KPIs may differ by definition, but not masquerade as a
   LEAN parity failure.

The matching trade count and win/loss count are useful evidence that the
strategy state is mostly aligned. They do not prove equity, portfolio
accounting, statistics, performance memory, or production-readiness parity.

The credible proof must be a new, immutable, reconciliation-grade LEAN golden
fixture. Both engines must consume the same hashed bars and the comparison must
pass in this order:

> input bars -> consolidated state and signals -> orders and fills -> portfolio
> ledger and equity -> metric inputs -> statistics -> performance memory ->
> readiness verdict

A downstream layer is not comparable until every upstream gate passes.

## Product decisions from the follow-up review

### 1. Replace “Both” with one server-owned paired compatibility run

**Implemented 2026-08-08:** the Engine Lab selector now sends one Python
anchor request. The Python service mints the parity group and dispatches exactly
one registered LEAN companion. The UI no longer launches an unrelated second
LEAN job. Ordinary Python runs cannot claim compatibility: companion dispatch
requires the explicit `us-equity-raw-ibkr-v1` profile. Runs 85/86 proved the
execution slice with 73 trades on each side and a frozen `agree` verdict with
zero quantity, fill-price, or P&L divergences. The Python anchor now hashes the
exact reference-first minute ZIPs it consumes; LEAN verifies the same fixture
before launch and again after staging. The frozen paired verdict compares the
fixture receipt instead of inferring sameness from symbol/date metadata.

The current Engine Lab “Both — validate equivalence” action is not a paired
run. The browser sends independent Python and LEAN requests concurrently. The
Python request then tries to launch a second, automatic LEAN companion, while
the independently requested LEAN run has no shared parity group. With the
currently hard-coded `adjusted=true` policy, the automatic companion is marked
`adjustment_unsupported`. This is how two visually adjacent runs can look like a
pair without sharing a comparison contract.

Replace that behavior with one compatibility coordinator owned by the Python
service. One request must:

1. resolve a registered Python/LEAN strategy twin;
2. produce and persist a versioned comparison contract;
3. stage one immutable data snapshot and calculate its aggregate hash;
4. mint one comparison-group identifier;
5. dispatch exactly one Python job and one LEAN job against that snapshot;
6. suppress the ordinary Python auto-companion for coordinator-managed jobs;
7. persist the contract ID, snapshot ID/hash, group ID, anchor run ID, and peer
   run ID on both results; and
8. publish one gate-by-gate comparison verdict.

The browser may display progress, but it may not assemble or infer a pair. The
existing LEAN workspace reader, `LeanSetHoldingsSizing`, IBKR fee model, and
stale-signal-next-open fill behavior in the cross-runner are the implementation
substrate. They must be promoted from a trade-only diagnostic into a persisted,
full Engine Lab compatibility execution.

Selecting an existing run follows a deterministic rule:

- if an exact peer with the same group ID, comparison-contract ID, and snapshot
  hash exists, select it;
- otherwise, if the selected LEAN run is eligible, offer **Create compatible
  Python run** against its exact staged workspace bytes;
- otherwise, show a stable unavailable reason and a concrete remediation;
- never pair runs heuristically by symbol, date range, trade count, or nearby
  run ID.

“Fixture” in the UI should mean the immutable shared data and assumption
snapshot. The generated Python execution remains a run. This distinction avoids
suggesting that an arbitrary historical run is a scientific oracle.

### 2. Use raw execution parity as the practical common mode now

**Implemented slice 2026-08-08:** selecting Compatibility pair forces
`adjusted=false` and pins LEAN `SetHoldings` sizing, Interactive Brokers equity
fees, and the stale-session-close next-open fill rule on the Python anchor. The
server rejects profile requests that change resolution, session, fill mode,
slippage, limit penetration, entry cutoff, or forced-flat behavior.

The initial profile will be `us-equity-raw-ibkr-v1`:

| Contract field | Required value |
| --- | --- |
| Security/data | US Equity minute bars, regular session |
| Adjustment | Raw/unadjusted input; LEAN `DataNormalizationMode.Raw` |
| Corporate actions | Matching map/factor files; fail closed if unsupported or missing |
| Fill-forward | Off |
| Strategy cadence | Explicit and identical, 15 minutes for this fixture |
| Brokerage/account | Interactive Brokers model and the same account type |
| Sizing | LEAN `SetHoldings` semantics, including the free-portfolio buffer |
| Fills | Same market-fill and stale-session-close next-open rule |
| Fees/slippage | Same versioned fee and slippage models |
| Time | Exchange calendar plus `int64 ms UTC` at every boundary |

This is the honest common denominator currently supported by the repository.
It is also closer to an execution comparison because it avoids silently
rewriting historical prices. It does **not** change the default for ordinary,
single-engine Python research runs.

Although LEAN defaults US Equity research subscriptions to adjusted data, the
current Engine Lab `adjusted=true` path is not equivalent to LEAN adjusted
history: the staged Polygon series is described as split-adjusted, while LEAN's
adjusted mode also uses its factor-file corporate-action semantics. Run 76 even
combines pre-adjusted input with LEAN `Raw`, which its own manifest correctly
labels non-reconciliation-grade. Therefore “adjusted” cannot be chosen merely
because it may be popular.

An `us-equity-adjusted-research-v1` profile may be added later only after split,
dividend, map-file, factor-file, indicator-reset, and total-return behavior have
their own golden cells. Instrument successful run-policy usage so the future
default is based on observed use, but never allow popularity to override an
unsupported scientific contract.

### 3. Preserve LEAN's full chart and summary separately; neither is the platform ledger

**Implemented readiness safeguard 2026-08-08:** linked compatibility verdicts
do not use either LEAN chart artifact. Both sides grade the common closed-trade
ledger through the same platform statistics implementation. This closes the
readiness-input mismatch; it does not claim that a chart is a full portfolio
ledger or make one eligible for performance-memory calculations.

The old normalizer selected `MyAlgorithm-summary.json`. Its Strategy Equity
series has 105 resampled OHLC samples and stops on 2026-08-05. Direct inspection
of the retained `MyAlgorithm.json` shows 1,728 samples and a final 2026-08-07
sample whose close is the exact reported terminal equity, `$108,540.932`.
Accordingly, the importer must ingest the full result as the native evidence
source and retain the summary as a separately labeled reduced artifact. It must
never silently substitute one for the other.

No pointwise comparison will be made between Python's minute portfolio curve
and a LEAN display chart merely because both are named equity. A common
calculation-grid ledger or exact LEAN statistic primitive export is required
before a pointwise or formula parity claim can be made.

Each LEAN chart artifact gets an information badge with a receipt containing:

- source: `LEAN full result / Strategy Equity` or
  `LEAN summary result / Strategy Equity`;
- sample count and chart cadence/resample period when available;
- first and last chart timestamps;
- last order/statistics timestamp and terminal-equity timestamp;
- terminal sample gap in sessions; and
- the explanation that headline terminal equity comes from the LEAN result,
  not from an assumed chart endpoint.

The Python curve has a stronger contract: a full internal portfolio ledger,
an explicit terminal row, an accounting identity on every row, and a
presentation-only downsampler that retains the first and last points. Python
KPIs and readiness may use only that canonical ledger, never the retained UI
curve. LEAN performance-memory projections must remain unavailable until they
can be driven by a full ledger/native primitive export; neither LEAN chart
artifact is an acceptable substitute.

### 4. Require LEAN-native metric parity and separate platform-canonical KPIs

**Implemented 2026-08-08:** both linked rows receive the same complete 17-input
platform-readiness vector from the reconciled closed-trade ledger. Separately,
`lean.native` is retained field-for-field and
`python.lean-compatible.261366a7...` independently reproduces all 25 portfolio
and 41 trade values plus 25 formatted dashboard strings. The immutable Oracle,
tolerance, formulas, runtime pins, UI interpretation, and acceptance commands
are documented in `docs/references/lean-native-statistics-oracle-v1.md`.

The dashboard currently places identically named values beside one another even
when their definitions differ. A paired compatibility run must calculate three
explicit metric namespaces:

- `platform.performance.v2.*` — Python-owned canonical KPI definitions used by
  the platform, compatibility report, performance memory, and readiness;
- `lean.native.<source-commit>.*` — the values exactly reported by the pinned
  LEAN runtime, retained as independent evidence; and
- `python.lean-compatible.<source-commit>.*` — an independent Python
  reproduction using the identical LEAN primitive vectors, benchmark,
  risk-free file/model, calendar, formulas, and formatting conventions.

`python.lean-compatible` **must match** `lean.native` for identical settings.
This is a hard compatibility gate, not an optional informational comparison.
The comparison covers every LEAN dashboard portfolio and trade statistic that
the product exposes, not only Sharpe and Sortino. Full-precision values pass the
pinned tolerance, and formatted dashboard strings match exactly. If LEAN emits
only a rounded value, the Python result must fall inside its mathematically
valid quantization interval and reproduce the displayed string.

“Identical settings” means the same input bytes and corporate-action files,
calendar, time window, starting capital, brokerage/account, sizing, fills,
fees, slippage, benchmark, interest-rate model and dated rate file, daily
performance vector, statistic source commit, and formatter convention. A
metric parity result is unavailable—not failed—until those prerequisites are
proved. Once they are proved, any remaining value mismatch is a calculation
defect and fails the paired run.

A parity delta is shown between `lean.native` and
`python.lean-compatible`; it must be zero within the declared tolerance. A
platform-versus-LEAN delta is allowed only when metric ID, version, snapshot
hash, execution contract, sample window, cadence, risk-free contract, and
availability state match. Otherwise the UI says **Definition differs** and
opens the two calculation receipts. LEAN-native values never flow into the
platform readiness score through fallback field selection.

Each KPI receipt must expose its human meaning, formula, primitive series,
observation count, first/last timestamps, fee treatment, annualization, rate and
benchmark inputs, unavailable behavior, unrounded value, display rounding,
source citation, and validating fixture/test. This applies at least to final
equity, net P&L, return, fees, drawdown, CAGR, volatility, Sharpe, Sortino,
Calmar, profit factor, payoff ratio, expectancy, win rate, and PSR.

### 5. Introduce a performance risk-free contract; do not reuse the options helper

The current FRED service interpolates Treasury-bill tenors for option pricing.
It is not wired into Engine Lab portfolio statistics. Run 75's LEAN-style
calculator was explicitly called with `risk_free_rate=0.0`. LEAN instead uses
its interest-rate provider, whose default US model is based on the Federal
Reserve primary credit rate. Primary credit is a bank discount-window lending
rate, not an investable Treasury return. The present UI therefore has no basis
to claim the Python value is superior.

Create a separate `performance-risk-free-usd-v1` contract. The recommended
source is FRED `DGS3MO`, the 3-month constant-maturity Treasury yield quoted on
an investment basis. The contract must freeze:

- the full dated source series and SHA-256 hash;
- publication-date and holiday forward-fill rules, with no backward fill;
- the annual-yield-to-return conversion and day-count convention;
- the exact equity-return intervals receiving each rate;
- annualization and downside-deviation conventions; and
- behavior before the first rate or when the frozen rate series is absent.

The proposed platform definition uses interval excess returns, not a single
undated constant: for each adjacent daily-equity interval, convert the last
available annual Treasury yield into the specified interval return, subtract it
from the strategy return, and calculate Sharpe/Sortino from the frozen excess
return vector. The exact conversion is an ADR decision and must be independently
hand-calculated in the small fixture before the `v1` contract is accepted. An
audit-grade run fails the metric closed if the rate fixture is unavailable; the
options service's 4.3% fallback is forbidden here.

The LEAN info button should eventually say, factually:

> LEAN native uses its primary-credit-rate model. Platform v2 uses the frozen
> 3-month Treasury performance-rate contract. The values answer different
> risk-adjusted-return definitions; open the receipts for the rates, dates, and
> formulas.

Only after the platform formula, FRED series, and golden outputs pass review may
the copy add that the Treasury contract was chosen as a more directly
investable USD opportunity-cost proxy. It should not use the unqualified claim
“ours is better.” For exact LEAN-oracle tests,
`python.lean-compatible` must use LEAN's frozen rate file and provider semantics
and match `lean.native`. The FRED contract is used only by
`platform.performance.v2`; it is never substituted into a LEAN-native parity
assertion.

### 6. Replace dynamic readiness reweighting with a fixed completeness contract

The decision, its rules and its why are recorded in [ADR 0073](../../architecture/adrs/0073-research-verdict-rules.md) decision 4 (implemented 2026-08-08 as `readiness-core-v2`; golden cells under `PythonDataService/tests/fixtures/golden/run-verdict-v2/`).

### Compatibility eligibility and user-facing behavior

| Selected run | Exact compatible peer | Action | Scientific label |
| --- | --- | --- | --- |
| Python or LEAN | Present with matching group, contract, and snapshot hash | Auto-select exact peer | Paired compatibility run |
| LEAN trusted twin, raw reconciliation-grade workspace retained | Absent | Create Python run from exact LEAN workspace | Candidate paired run until gates finish |
| Python trusted twin, supported raw snapshot | Absent | Create LEAN peer through coordinator | Candidate paired run until gates finish |
| Adjusted/pre-adjusted or missing corporate-action receipt | Absent | Offer a new raw pair; do not reuse the old run | Original run incomparable |
| Arbitrary LEAN algorithm with no registered Python twin | Any | No automatic pair | LEAN-native only |
| Missing staged bytes/hash, unsupported asset/resolution/session/model | Any | Fail closed with reason and remediation | Incomparable |

Before launch, show a compatibility preview listing inherited settings,
coordinator-enforced settings, engine-specific features that will be disabled or
substituted, and metrics that will remain LEAN-native only. This is the place to
explain limitations without implying that either engine implements capabilities
it does not.

The persisted comparison envelope should be structurally equivalent to:

```json
{
  "comparison_contract_id": "us-equity-raw-ibkr-v1",
  "comparison_group_id": "uuid",
  "anchor": {"engine": "lean", "run_id": 76},
  "peers": {"python_run_id": null, "lean_run_id": 76},
  "strategy_twin_id": "ema-crossover-signal-v1",
  "snapshot": {
    "fixture_id": "sha256-addressed-id",
    "bars_sha256": "...",
    "calendar_sha256": "...",
    "factor_files_sha256": "...",
    "map_files_sha256": "..."
  },
  "execution": {
    "sizing": "lean-set-holdings-v1",
    "fill": "lean-market-fill-v1",
    "fees": "ibkr-us-equity-v1",
    "slippage": "zero-v1"
  },
  "metrics": {
    "lean_native": "lean-portfolio-statistics-<commit>",
    "python_lean_compatible": "lean-portfolio-statistics-<commit>",
    "platform": "platform-performance-v2",
    "readiness": "readiness-core-v2"
  },
  "capabilities": [],
  "unsupported": [],
  "status": "planned|running|comparable|diverged|unavailable"
}
```

All actual timestamps stored in this envelope or its receipts are `int64 ms
UTC`; strings above are identifiers, not temporal values.

## Reconciliation findings

### Provenance status

Run 76's local receipt is
`PythonDataService/artifacts/lean-sidecar/engine_lab_spy_mskujjuz/`.

| Receipt field | Observed value |
| --- | --- |
| LEAN run ID | `engine_lab_spy_mskujjuz` |
| LEAN image digest | `sha256:3dd003372f1ef1981b4e80038e3f1c557f1fe414d1be531f485ef870f81a5771` |
| Algorithm source SHA-256 | `7bd99036d8e98526bc5b75c6760b67960ae676c6dfc667c9b442df95a2c6d581` |
| Manifest SHA-256 | `1091ffe7c898ef3fae029703f7996bac4ec6cda2624b05041eda3659de0a25b3` |
| Normalized result SHA-256 | `121c6026d05561f0d2f26c2e233c5c5e3d54a98d339a6934cf341147c2500729` |
| Bars consumed | 194,490 SPY minute bars |
| Brokerage policy | Interactive Brokers |
| LEAN normalization | `Raw` |
| Requested data policy | Polygon, adjusted, regular session, minute input -> 15-minute strategy bars |
| Manifest adjustment policy | `pre_adjusted_non_reconciliation` |
| Exit status | 0; manifest note says `is_clean=True` |

This is good reproducibility evidence for the diagnostic LEAN run, but it is
not a committed golden fixture. In particular, `Raw` LEAN normalization applied
to pre-adjusted input is different from a reconciliation-grade raw-data
contract, and the fixture has no immutable `fixture_id` or aggregate input-bar
hash shared with run 75.

### Trade-level divergence

Index-aligning the 73 closed trades produced the following counts:

| Field | Mismatched trades |
| --- | ---: |
| Entry timestamp | 5 |
| Exit timestamp | 1 |
| Entry price | 5 |
| Exit price | 1 |
| Quantity | 59 |
| Net cash P&L | 60 |
| Price return | 6 |

Quantity difference `(Python - LEAN)` had this distribution: -2 shares on 7
trades, -1 on 41, zero on 14, +1 on 7, and +3 on 4. The first divergence occurs
on trade 2: timestamps and prices match, but Python holds 183 shares and LEAN
holds 182. This localizes the first cause to sizing rather than indicators or
signals.

The six fill-timing/price divergences are:

| Trade | Python event | LEAN event | Classification |
| ---: | --- | --- | --- |
| 10 entry | 2025-02-06 21:00 UTC @ 606.34 | 2025-02-07 14:31 UTC @ 606.89 | stale session-close signal / next market open |
| 15 entry | 2025-03-21 20:00 UTC @ 564.19 | 2025-03-24 13:31 UTC @ 570.80 | stale session-close signal / next market open |
| 19 exit | 2025-04-22 20:00 UTC @ 526.94 | 2025-04-23 13:31 UTC @ 540.43 | stale session-close signal / next market open |
| 31 entry | 2025-08-07 20:00 UTC @ 632.30 | 2025-08-08 13:31 UTC @ 634.06 | stale session-close signal / next market open |
| 33 entry | 2025-09-03 20:00 UTC @ 643.64 | 2025-09-04 13:31 UTC @ 644.42 | stale session-close signal / next market open |
| 67 entry | 2026-07-02 20:00 UTC @ 744.80 | 2026-07-06 13:31 UTC @ 748.74 | stale session-close signal / next market open |

The run-75 router constructs the ordinary `BacktestEngine` without a sizing
model. It therefore defaults to `SimpleFloorSizing`, approximately
`floor(portfolio_value / price)`. The repository's established LEAN parity
runner instead supplies `LeanSetHoldingsSizing` with the IBKR fee model and
enables `fill_stale_signal_at_current_open`; LEAN also reserves its free
portfolio-value buffer. Ordinary Engine Lab execution is consequently not the
same execution contract as the already validated parity path.

### Equity-curve divergence

The curves currently imported by the application cannot be compared point by
point:

| Property | Run 75 — Python | Run 76 — LEAN |
| --- | --- | --- |
| Source | Engine portfolio snapshots | Imported LEAN summary `Strategy Equity` series |
| Raw / retained count | 194,490 / 10,000 | Full result 1,728 / imported summary 105 |
| Retained label | `strategy_bar_close` | currently `lean_chart_sampling`; must identify full versus summary |
| Last analytics timestamp | 2026-08-07 20:00 UTC | Summary 2026-08-05; full result 2026-08-07 |
| Last imported summary chart value | n/a | $108,418.332 |
| Last full-result chart value | n/a | $108,540.932 |
| Final reported equity | $108,184.95 | $108,540.93 |

The final value of the imported LEAN summary is not final LEAN equity. The
normalizer chose the reduced summary even though the full result has order
events through 2026-08-07, reports `$108,540.93` end equity, and has a matching
terminal Strategy Equity sample. Therefore the two-session gap is an importer
defect. The full chart is still a LEAN chart artifact rather than a proven
portfolio calculation ledger, so this correction does not by itself make the
chart a statistics or performance-memory Oracle.

Python's canonical proof series must instead be its full portfolio ledger with
a specified marking cadence and a mandatory terminal row. UI downsampling must
be tested only after the full-resolution curve passes and must never feed any
statistic. Both LEAN chart artifacts are preserved with explicit source,
sampling and endpoint receipts; neither is point-diffed against Python without
a shared calculation-grid contract. A full independent LEAN
ledger/statistic-primitives export remains required for LEAN-native metric
parity, but it need not be rendered as the same visual curve.

### Statistics-input divergence

Run 75 currently has at least three statistical paths:

1. `app/engine/results/statistics.py` calculates generic portfolio and trade
   statistics from the Engine Lab curve and trades.
2. `app/engine/results/lean_statistics.py` attempts to reproduce LEAN statistics.
   Its daily-equity builder applies `trade.pnl_pct * starting_capital` only on
   exit dates, leaves equity flat between exits, and is called with a zero
   risk-free rate and no benchmark series.
3. `app/services/run_verdict_service.py` selects fields from the persisted
   statistics payload with a fallback order that is not the same as the
   headline KPI persistence path.

The LEAN run obtains its risk-free-rate series from LEAN's interest-rate
provider. The staged provider data in this run carries a 5.5% rate from
2023-07-27. That is sufficient to make a roughly 4.2% annual strategy return
have a negative excess-return Sharpe, while the Python zero-rate calculation is
positive. This is a definition/input mismatch, not evidence that either
arithmetic operation is numerically broken.

The repository's FRED helper is currently an option-pricing service that
interpolates Treasury-bill tenors and can fall back to 4.3%. It did not supply
run 75's Sharpe or Sortino. Performance statistics need the separate frozen
rate contract defined above, while the Python LEAN-compatible projection needs
LEAN's exact rate file and provider semantics.

The Python LEAN-style payload is also not internally ledger-consistent: it
reconstructs approximately $108,104.23 end equity and zero fees, while the
portfolio reports $108,184.95 and $146.00. A trade-exit-only pseudo-equity curve
cannot be used to validate mark-to-market drawdown, daily returns, volatility,
Sharpe, Sortino, CAGR, or PSR.

## Tolerance policy

Use `rtol=0` throughout. Tolerances are part of the fixture contract and may not
be loosened merely to pass a test.

| Field | Required comparison |
| --- | --- |
| Timestamps, dates, ordering, IDs, sides, states, counts, quantities, availability | exact |
| Input OHLCV and fill prices from identical fixture bytes | exact decimal representation; otherwise a documented data-format quantum, never a fitted tolerance |
| Indicator values | `atol=1e-9` |
| Fees | exact to the fee-model currency quantum (USD cents here) |
| Cash P&L and equity | `atol=1e-6` USD after exact-decimal boundary conversion |
| Return vectors and full-precision ratios | `atol=1e-9` |
| Integer readiness scores, grade, signal, evidence and availability mask | exact |
| LEAN formatted dashboard strings | exact string after the numeric value passes independently |

If the LEAN runtime cannot export a full-precision statistic, compare its
primitive vector exactly and validate the formatted string using an explicit
quantization interval. For example, a ratio printed to three decimals admits
at most half a displayed unit. Do not treat the rounded string itself as a
`1e-3` numerical oracle.

## External method sources to pin in the implementation receipts

- [QuantConnect US Equity data normalization](https://www.quantconnect.com/docs/v2/writing-algorithms/securities/asset-classes/us-equity/requesting-data)
  — adjusted is the default for US Equity subscriptions; raw and adjusted modes
  have different split/dividend behavior.
- [LEAN `BaseResultsHandler`](https://github.com/QuantConnect/Lean/blob/master/Engine/Results/BaseResultsHandler.cs)
  — source for the sampled Strategy Equity chart and resample period.
- [LEAN `PortfolioStatistics`](https://github.com/QuantConnect/Lean/blob/master/Common/Statistics/PortfolioStatistics.cs)
  and [statistics helpers](https://github.com/QuantConnect/Lean/blob/master/Common/Statistics/Statistics.cs)
  — source formulas to vendor and reproduce in Python.
- [LEAN `InterestRateProvider`](https://github.com/QuantConnect/Lean/blob/master/Common/Data/InterestRateProvider.cs)
  and [QuantConnect supported risk-free models](https://www.quantconnect.com/docs/v2/writing-algorithms/reality-modeling/risk-free-interest-rate/supported-models)
  — native dated-rate loading/averaging behavior and the default primary-credit
  rate model.
- [Federal Reserve primary credit description](https://www.federalreserve.gov/monetarypolicy/discountrate.htm)
  — establishes that primary credit is discount-window lending to depository
  institutions.
- [FRED `DGS3MO`](https://fred.stlouisfed.org/series/DGS3MO) — proposed frozen
  platform performance-rate source, a 3-month constant-maturity Treasury yield
  quoted on an investment basis.

This plan validates research software behavior; it is not financial advice or
evidence that the strategy is suitable for live trading.
