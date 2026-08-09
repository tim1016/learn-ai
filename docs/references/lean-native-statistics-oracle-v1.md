# LEAN native statistics Oracle v1

**Status:** implemented and acceptance-tested 2026-08-08

**Oracle:** QuantConnect LEAN build `17748`

**LEAN source:** commit `261366a7e26ae942df858ab20df4fef8fa07de67`

**Comparison contract:** `us-equity-raw-ibkr-v1`
**Fixture:** `PythonDataService/tests/fixtures/golden/lean-statistics-oracle-v1/`

## Claim

For the committed Oracle result, Python independently reproduces every value in
LEAN's `totalPerformance.portfolioStatistics` and
`totalPerformance.tradeStatistics`, then reproduces every formatted statistics
dashboard string. The numerical gate covers 25 portfolio values and 41 trade
values. The display gate covers 25 formatted values. No Oracle statistic is
used as a calculation input.

This proof validates the LEAN-compatible calculation and the lossless adapter.
It is separate from platform performance metrics and production readiness.
Those remain Python-owned definitions and are never backfilled from LEAN-native
statistics.

## Runtime and source identity

The runtime pin is not based on an image tag. The verifier checks:

| Evidence | Pinned value |
| --- | --- |
| Image digest | `sha256:3dd003372f1ef1981b4e80038e3f1c557f1fe414d1be531f485ef870f81a5771` |
| Image `lean_version` label | `17748` |
| SourceLink commit in all three PDBs | `261366a7e26ae942df858ab20df4fef8fa07de67` |
| `QuantConnect.Common.dll` | `827339fd94aef0ea71f8576d918e0a361ef858f9d2159a4bb2293018a9a57cbc` |
| `QuantConnect.Lean.Engine.dll` | `b0eeec4f21b5a4cb458923ca7eb32e34b7ce39f22edd5377da0dfd13d81cf12a` |
| `QuantConnect.Lean.Launcher.dll` | `b2004a97d5ee8f323ff5d342a6f10d57705f088cf417ee551681e708b7a93f0b` |
| `QuantConnect.Common.pdb` | `2d5f78082f8850a4e05bdfe775b23101ab3189e585f279b7961a1a083c8aad61` |
| `QuantConnect.Lean.Engine.pdb` | `1d6f3ed66be0a6c989afff852bbbd01447914bbe915be89780e5d0e829178930` |
| `QuantConnect.Lean.Launcher.pdb` | `30788cd31dd9ddd953edbdaef116423afdfc4adfaa356b37a71e03e1f9948788` |

Run the read-only verifier from `PythonDataService/`:

```bash
.venv/bin/python scripts/lean_sidecar_pin_image.py --verify-only
```

A changed image, build label, DLL, PDB, or SourceLink commit fails the verifier.

## Immutable Oracle artifacts

The regeneration command accepts only a retained workspace whose files match
the hard-coded source hashes. It refuses an accidental fixture rotation.

```bash
.venv/bin/python scripts/regenerate_lean_statistics_oracle.py \
  artifacts/lean-sidecar/companion-pg-6c4159107d2a4ccfb2d4
```

| Artifact | SHA-256 | Purpose |
| --- | --- | --- |
| `workspace/output/MyAlgorithm.json` | `fd10d7f5793b324d9f70a3c4ecfb1b3e4bf08d431272d4f765861224cd7a69d4` | Full native result, charts, closed trades, orders, native numerical answers |
| `workspace/output/MyAlgorithm-summary.json` | `8c0507842028cdf664f32540db7a11002fe649c4caafe3d7078b930d85cbaf6d` | Reduced native display artifact and formatted answers |
| `workspace/data/alternative/interest-rate/usa/interest-rate.csv` | `1d0e6f2ab20e61a4330e8a38735bc73cde4034e7293a45d23b5ebdc6467f7899` | Dated LEAN primary-credit-rate input |

The test also pins the fixture image digest to the committed runtime
provenance. Hash verification runs offline.

## Exact primitive contract

`reproduce_lean_total_performance` reads only these primitive namespaces:

- full `Strategy Equity/Equity`, `Strategy Equity/Return`, `Benchmark`, and
  `Portfolio Turnover` chart series;
- `totalPerformance.closedTrades`, including P&L, fees, MAE, MFE,
  end-trade drawdown, duration, entry time, and exit time;
- the timestamped `profitLoss` series;
- algorithm configuration, starting capital, and trading-days-per-year;
- the frozen LEAN interest-rate CSV.

It deliberately does not read either native statistics answer object. The
hostile-answer test replaces both answer objects with `999` and requires the
reproduction to remain byte-for-byte unchanged.

LEAN preprocessing is reproduced exactly: discard day 0 and day 1 from the
Strategy Return samples, convert chart percentages to fractional returns,
discard the corresponding first benchmark change, use sample variance
(`n - 1`), and average the dated LEAN risk-free observations selected for the
equity sample dates.

## Portfolio formulas: all 25 fields

Let `r` be the preprocessed performance-return vector, `b` the aligned
benchmark-return vector, `N = tradingDaysPerYear`, and `rf` the average dated
LEAN rate.

| Fields | Reproduced definition |
| --- | --- |
| Average win/loss rate | Each closed P&L divided by running capital, then arithmetic mean by sign |
| Profit/loss ratio | `averageWinRate / abs(averageLossRate)` |
| Win rate, loss rate | Winning or losing closed trades divided by all closed trades |
| Expectancy | `winRate * profitLossRatio - lossRate` |
| Start/end equity | Starting-capital parameter and final full-result equity close |
| Total net profit | `endEquity / startEquity - 1` |
| CAGR | `(endEquity / startEquity)^(1 / calendarYears) - 1`, with 365-day years |
| Annual variance/std. dev. | `sampleVariance(r) * N`; square root for standard deviation |
| Annual performance | `(mean(r) + 1)^N - 1` |
| Sharpe | `(annualPerformance - rf) / annualStdDev` |
| Sortino | `(annualPerformance - rf) / annualized downside deviation` using negative observations |
| Beta | `sampleCovariance(r, b) / sampleVariance(b)` |
| Alpha | `annualPerformance - (rf + beta * (benchmarkAnnualPerformance - rf))`; LEAN returns zero when beta is zero |
| Tracking error | `sqrt(sampleVariance(r - b) * N)` |
| Information ratio | `(annualPerformance - benchmarkAnnualPerformance) / trackingError` |
| Treynor ratio | `(annualPerformance - rf) / beta` |
| PSR | LEAN's normal-CDF expression from observed Sharpe, skewness, excess kurtosis, sample size, and `1/sqrt(N)` benchmark Sharpe |
| Drawdown | Largest full Strategy Equity peak-to-trough loss; LEAN scale-3 rounding is retained |
| Drawdown recovery | Longest integer calendar-day peak-to-recovery interval |
| VaR 99/95 | Normal inverse CDF of the last `N` return observations, rounded to three decimals |
| Portfolio turnover | Arithmetic mean of the native Portfolio Turnover series |

Zero-denominator behavior follows the pinned LEAN source rather than inventing
`NaN` or infinity.

## Trade formulas: all 41 fields

The trade reproduction preserves:

- first entry, last exit, total/winning/losing counts;
- total, winning, losing, largest, and average P&L fields;
- average and median durations for all/winning/losing trades;
- maximum consecutive wins and losses;
- profit/loss ratio, win/loss count ratio, win rate, and loss rate;
- average/largest MAE and MFE;
- maximum closed-trade and intra-trade drawdowns;
- P&L sample standard deviation and losing-P&L downside deviation;
- trade Sharpe `mean(P&L) / sampleStdDev(P&L)`;
- trade Sortino `mean(P&L) / downsideStdDev(losing P&L)`;
- profit factor `totalProfit / abs(totalLoss)`;
- total P&L divided by maximum closed drawdown;
- maximum and average end-trade drawdown, maximum drawdown duration, and
  total fees.

LEAN sentinel behavior is retained: a positive numerator with no denominator
can produce `10`, as defined in the pinned source. Average durations reproduce
the C# online `TimeSpan` update, including the intermediate double conversion
and tick truncation; a simple arithmetic mean differs by microseconds and is
not accepted.

## Rounding and tolerance

The native numerical Oracle serializes many values to four decimal places.
The full-precision Python reproduction must therefore fall within
`abs <= 0.0000500001`, the closed interval implied by four-decimal rounding
plus a minimal floating boundary allowance. Relative tolerance is zero.

The formatted gate is stricter: all 25 strings must match exactly, including
percent scaling, currency prefix, integer rates, scale-preserved drawdown, and
midpoint-to-even decimal rounding. A wider tolerance is forbidden.

## Production-readiness contract

Both compatibility peers compute readiness from the same platform closed-trade
ledger, never from a native chart or a LEAN-native answer. Verdict v2 requires
all 17 inputs before assigning a grade:

- Return Quality: Sharpe, Sortino, CAGR, Calmar, annual volatility;
- Risk Control: maximum drawdown, recovery, consecutive losers;
- Trade Edge: profit factor, expectancy, win rate, payoff, fee drag;
- Statistical Confidence: PSR, trade count, trade-versus-portfolio Sharpe gap,
  and net profitability.

Missing inputs produce `incomplete`, no grade, and no deployment signal. The
denominator and weights never change from run to run. A paired verdict compares
the Python-authored readiness signature and cannot report `agree` if any grade,
score, coverage field, or required input differs. The receipt canonicalizes raw
inputs at an absolute `1e-12` transport tolerance—far below every published
display and scoring threshold—so binary floating noise cannot create a false
divergence.

Compatibility performance-memory horizons use a shared return-normalized
closed-trade curve with pinned endpoints. Native Python and LEAN charts remain
separate evidence; weekday/hour, seasonality, rolling stability, and horizons
therefore consume one platform analytics contract for paired runs.

## UI interpretation

- **LEAN native statistics** displays all 25 portfolio fields, all 41 trade
  fields, all nine runtime fields, and the source/tolerance receipt.
- Trade P&L, MAE/MFE, drawdowns, and P&L deviations render in account
  currency; only native rate fields render as percentages.
- **LEAN native analysis** displays every native analysis finding, arbitrary
  sample payload, and every proposed solution without folding it into the
  readiness grade.
- **Production readiness** displays only the 17 platform inputs.
- **LEAN curve note** explains that the native chart can be sparse or end
  before terminal portfolio value; headline KPIs never interpolate it.
- LEAN Sharpe/Sortino are labeled as native primary-credit-rate results. A
  differently defined platform risk-free contract must use a different metric
  namespace; it is not a native-parity error.

## Acceptance commands

From `PythonDataService/`:

```bash
.venv/bin/python -m pytest tests/test_lean_statistics.py \
  tests/services/test_lean_sidecar_persistence.py \
  tests/services/test_run_verdict_parity.py -q
```

From `Frontend/`:

```bash
npx ng test --watch=false \
  --include='src/app/components/lean-engine/engine-results/engine-results.component.spec.ts' \
  --include='src/app/components/engine-lab/run-report/run-report.component.spec.ts'
```

The paired verdict is `agree` only when the input fixture, trades, readiness
signature, and LEAN-native calculation receipt all match. Historical rows are
not silently backfilled; create a new Compatibility pair to obtain these
receipts.

Local acceptance pair 95/96 exercised the complete live path on 2026-08-08:
same bar-store fixture, five trades with no execution divergence, 66 native
values and 25 formatted values matched, all 17 readiness inputs matched, both
runs received C / 44 / Rework, and all three LEAN analysis findings rendered.

## Source citations

- QuantConnect LEAN commit `261366a7...`:
  `Common/Statistics/StatisticsBuilder.cs`,
  `Common/Statistics/PortfolioStatistics.cs`,
  `Common/Statistics/TradeStatistics.cs`, and
  `Common/Statistics/Statistics.cs`.
- Runtime SourceLink in the pinned PDBs resolves those files to the same exact
  commit.
- Full investigation and gate sequencing:
  `docs/references/reconciliations/engine-lab-runs-75-76-statistics-validation-plan.md`.

This validates research-software behavior. It is not financial advice or a
claim that the strategy is suitable for live trading.
