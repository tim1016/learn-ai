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

The missing-input concern is confirmed. `compute_run_verdict` averages only
available subscores and then renormalizes only available dimensions. Run 76's
missing expectancy is omitted, so its Trade Edge score rises. Several planned
subscores are always absent, yet the result still receives a deployment-oriented
letter grade. Two runs can consequently receive comparable-looking grades from
different denominators.

**Implemented 2026-08-08:** `readiness-core-v2` now enforces the fixed 17-input
contract below. Runs with missing required evidence render an explicit coverage
count and receive no composite, letter grade, or deployment signal. The golden
cells for runs 77 and 78 are committed under
`PythonDataService/tests/fixtures/golden/run-verdict-v2/`. The scorer reads only
the platform-canonical statistics namespace; contradictory LEAN-native sentinel
values in the golden fixture prove that native evidence cannot backfill a
readiness input.

Introduce `readiness-core-v2` with these rules:

1. The version publishes one fixed required metric list and one fixed weight
   vector. Planned metrics are excluded until a future scorer version instead
   of being dynamically omitted per run.
2. Preserve the existing relative dimension intent by freezing Return Quality
   at `25/85` and Risk Control, Trade Edge, and Statistical Confidence each at
   `20/85`. Alpha Calibration remains a separate ungraded panel until it is
   implemented; adding it requires a new scorer version.
3. Require all five current Return Quality inputs, the three implemented Risk
   Control inputs, all five Trade Edge inputs, and the four implemented
   Statistical Confidence inputs: 17 required values in total.
4. Missing or invalid required evidence produces `status=incomplete`, no
   letter grade, and no deployment signal. A provisional score may be shown
   only with coverage such as `16/17`, and it may not be compared with a
   complete grade.
5. Unknown is not scored as zero. A bad observed value and an unavailable value
   remain distinct states.
6. Cross-run grade comparison requires the same verdict version, metric
   contract IDs, required availability mask, and full coverage.
7. Canonical platform metrics are the only readiness inputs for either engine.
   LEAN-native dashboard strings are evidence, not fallback score inputs.
8. Add the invariant that removing any required metric can never improve a
   certified grade; it makes the grade incomplete.

Under this rule, runs 75 and 76 would not display an A-versus-B conclusion.
They would show incomplete/incomparable readiness until the canonical metrics
are reconstructed and validated under one contract.

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

## What the two dashboards currently report

### Headline statistics

| Measure | Run 75 — Python | Run 76 — LEAN | Difference / interpretation |
| --- | ---: | ---: | --- |
| Net P&L | $8,184.95 | $8,540.93 | +$355.98 in LEAN; execution and sizing differ |
| Total return | 8.18% | 8.54% | Derived from the different final equity |
| Final equity | $108,184.95 | $108,540.93 | Must reconcile from a common portfolio ledger |
| Fees | $146.00 | $146.02 | Small difference, but cents remain part of the proof |
| Closed trades | 73 | 73 | Matching count only |
| Wins / losses | 48 / 25 | 48 / 25 | Matching outcome signs only |
| Win rate | 65.75% | 65.75% | Same numerator and denominator |
| Maximum drawdown | 1.91% | 2.60% | Curves and calculation inputs differ |
| Sharpe ratio | 1.54 | -1.01 | Python used a zero rate and a different return series; LEAN used its native rate provider |
| Sortino ratio | 1.00 | -0.60 | Different rate, return-series, annualization, and downside inputs |
| Profit factor | 1.86 | 1.96 | Trade cash P&L differs; another Python payload reports 2.00 |

The Python run contains internal contradictions that must be removed before it
can be certified. Its persisted headline drawdown is 1.91%, while its readiness
payload uses 2.5712%. Its headline profit factor is 1.8620, while its readiness
payload uses 1.9971. Its headline Sharpe is 1.5402, while its readiness payload
uses 1.4344. A single run must have one named, versioned source for each metric
or explicitly label metrics that intentionally use different definitions.

### Production-readiness result

| Dimension | Run 75 — Python | Run 76 — LEAN | Why the scores are not comparable yet |
| --- | ---: | ---: | --- |
| Composite / grade | 76 / A | 69 / B | Downstream of all differences below |
| Return Quality | 76 | 49 | Python uses positive zero-rate Sharpe/Sortino; LEAN reports negative risk-adjusted ratios |
| Risk Control | 73 | 73 | Same rounded score despite different drawdown and recovery inputs |
| Trade Edge | 82 | 91 | LEAN expectancy is missing and is omitted from the denominator, increasing its score |
| Statistical Confidence | 74 | 68 | LEAN trade-gap evidence is missing and the PSR inputs differ |
| Alpha Quality | unavailable | unavailable | Both composites reweight the remaining dimensions |

`compute_run_verdict` currently averages only available subscores and then
reweights only available dimensions. That behavior makes two grades with
different availability masks semantically different. The golden test must
assert the availability mask as well as the numeric score. Until that is done,
a missing value may not silently improve a score; the comparison must be marked
`INCOMPARABLE`. `readiness-core-v2` therefore uses a fixed 17-metric contract;
missing evidence makes the grade incomplete instead of shrinking the
denominator or being treated as poor performance.

### Performance memory

| Horizon | Run 75 — Python | Run 76 — LEAN |
| --- | --- | --- |
| 2 weeks | -0.82%; 33.33% wins; 3 trades; PF 0.12 | -0.93%; 0% wins; 2 trades; PF 0 |
| 1 month | -1.17%; 40% wins; 5 trades; PF 0.24 | -1.08%; 40% wins; 5 trades; PF 0.21 |
| 3 months | -1.72%; 46.67% wins; 15 trades; PF 0.51 | -2.10%; 46.67% wins; 15 trades; PF 0.39 |
| 6 months | -1.27%; 55.56% wins; 27 trades; PF 0.74 | -1.85%; 53.85% wins; 26 trades; PF 0.59 |
| 1 year | +2.87%; 62.79% wins; 43 trades; PF 1.50 | +2.04%; 61.90% wins; 42 trades; PF 1.31 |
| 2 years | insufficient return history; 65.75%; 73 trades; PF 2.00 | insufficient return history; 65.28%; 72 trades; PF 1.99 |

The Python analytics window ends at 2026-08-07 20:00 UTC. The LEAN memory path
was fed the reduced summary series, which ends at 2026-08-05 20:00 UTC even
though the full LEAN result contains the terminal 2026-08-07 sample. That
importer defect can explain one-trade count differences in several trailing
windows, but correcting it alone does not validate the legacy comparison: the
engines also used different execution contracts, and neither display chart is
a certified performance-memory ledger. The weekday/hour table is close because
only five entry timestamps and five entry prices diverge, and its Python trade
return is derived from entry and exit price rather than position size. Its
approximate agreement is a useful localization clue, not a
portfolio-accounting proof.

## Executed reconciliation plan after runs 87 and 88

This was the implementation backlog used to close the gap. Work proceeded in
dependency order and stopped at the first failed gate. Matching UI values were
never accepted as proof by themselves. The table records the final implemented
boundary; the detailed sections below preserve the investigation and design
rationale.

### Current certification boundary

| Surface | Current state | Evidence and limit |
| --- | --- | --- |
| Compatibility launch | **Implemented** | One Python anchor dispatches one linked LEAN companion under `us-equity-raw-ibkr-v1`. |
| Orders, fills, quantities, fees, trade P&L | **Certified by golden runs 95/96** | Five trades per side; frozen verdict `agree`; zero gating divergences; exact input ZIP bytes and all trade fields are committed under `engine-lab-compatibility-95-96-v1`. |
| Platform readiness display | **Implemented** | Both peers author and render the same fixed 17/17 input contract. Missing evidence produces `incomplete`, never a smaller denominator. |
| Persisted LEAN-native statistics | **Implemented** | The lossless normalizer retains full `totalPerformance`; persistence exposes every 25 portfolio, 41 trade, and nine runtime values. |
| Python reproduction of LEAN-native statistics | **Implemented** | The independent Oracle reproduction matches 66 native values within the pinned rounding interval and all 25 formatted dashboard strings exactly. |
| Full platform performance and memory | **Implemented for compatibility semantics** | Both peers use the same return-normalized closed-trade curve for the platform contract. Full native marked curves remain separately labeled engine evidence. |
| LEAN image/source identity | **Pinned and verified** | The verifier checks image digest, build `17748`, six binary hashes, and SourceLink commit `261366a7e26ae942df858ab20df4fef8fa07de67`. |
| Immutable independent Oracle | **Established** | `lean-statistics-oracle-v1` proves the native calculator independently; `engine-lab-compatibility-95-96-v1` pins the successful end-to-end pair, exact inputs, readiness/performance receipts, LEAN analyses, and final `AGREE` verdict. |

### Extract everything LEAN already provides before porting formulas

Run 88 proves that the full LEAN result is substantially richer than the
application currently exposes:

| LEAN artifact | Available evidence |
| --- | ---: |
| Full result `MyAlgorithm.json` | 25 portfolio-statistic fields, 41 trade-statistic fields, 8 runtime fields, 27 formatted statistics |
| Closed trades | 73, including gross P&L, fees, MAE/MFE, duration, drawdown and order IDs |
| Orders and events | 146 logical orders; 292 events: 146 submitted and 146 filled |
| Rolling windows | 100 `M1`/`M3`/`M6`/`M12` result cells |
| Raw observations | 194,490 minute observations plus header |
| Strategy state | 12,952 state rows plus header |
| Full Strategy Equity chart | 1,728 OHLC equity samples, ending at terminal equity `108540.932` on 2026-08-07 20:00 UTC |
| Return/benchmark/drawdown charts | 731 return, 731 benchmark and 731 drawdown samples |
| Other calculation charts | turnover, exposure, capacity, sales volume and margin series |
| Analysis findings | 5 LEAN-authored analysis records |

The raw run-88 contract and completion state are also available and must be
retained rather than inferred from the UI:

| Category | LEAN-authored value |
| --- | --- |
| Algorithm | Python `MyAlgorithm`; source SHA-256 `7bd99036...`; completed with no runtime error |
| Window | 2024-08-08 00:00 UTC through 2026-08-07 23:59:59 UTC |
| Settings | SPY, raw, regular session, 15-minute bars, USD 100,000, 252 trading days/year |
| Brokerage/account | raw enum values `brokerage=1`, `accountType=0`; manifest maps brokerage to `interactive_brokers` |
| LEAN state | 146 orders, 0 insights, 3 log entries, completed in about 54 seconds |
| Runtime | equity `$108,540.93`; net profit `$8,540.93`; fees `-$146.02`; holdings/unrealized `$0`; volume `$15,538,707.42` |
| Data monitor | 1,004 successful requests, 0 failed requests, 0 universe requests |

The full-precision native result contains these calculation groups:

| Group | Selected raw values and required retained fields |
| --- | --- |
| Portfolio outcome | start equity `100000`; end equity `108540.932`; net profit `0.0854`; CAGR `0.0418`; drawdown `0.026`; recovery `85` |
| Portfolio trade rates | average win `0.0034`; average loss `-0.0033`; win/loss rates `0.6575`/`0.3425`; payoff ratio `1.0462`; expectancy `0.3454` |
| Portfolio risk | Sharpe `-1.0125`; Sortino `-0.6035`; PSR `0.5967`; annual deviation/variance `0.0256`/`0.0007`; VaR 95/99 `-0.002`/`-0.003` |
| Benchmark-relative | alpha/beta/Treynor `0`; information ratio `1.1351`; tracking error `0.0256`; turnover `0.1989` |
| Trade totals | 73 trades, 48 wins, 25 losses; gross P&L `8686.96`; profit `17526.81`; loss `-8839.85`; separate fees `146.02` |
| Trade edge/risk | profit factor `1.9827`; Sharpe `0.2259`; Sortino `0.3992`; payoff ratio `1.0327`; win/loss ratio `1.92`; profit/max-drawdown `3.0081` |
| Trade tails | largest profit/loss `2769.55`/`-1126.25`; largest MAE/MFE `-1546.57`/`2769.55`; closed/intratrade drawdown `-2887.88`/`-3393.93` |
| Trade sequences | maximum consecutive wins/losses `7`/`4`; average trade P&L `118.9995`; P&L deviation/downside deviation `526.7964`/`298.1239` |
| Trade time | first/last trade timestamps, average/median durations by outcome, and maximum drawdown duration `83.19:15:00` |
| Analyses | flat-equity intervals; 7k-points/second execution warning; low-margin-use finding; benchmark-Sharpe finding; daily-excess-return p-value `0.0684907141208504` |

The formatted 27-field `statistics` object, 8-field `runtimeStatistics` object,
and the full-precision 25-field portfolio/41-field trade objects are separate
Oracle surfaces. The fixture must preserve all fields even if the application
does not display them yet; the selected values above are an inventory check,
not an allowlist.

The current parser deliberately opens `MyAlgorithm-summary.json`, whose Strategy
Equity series has only 105 points and ends on 2026-08-05. The full
`MyAlgorithm.json` has 1,728 points and the correct terminal sample two sessions
later. Therefore the apparent LEAN terminal-curve gap is currently a parser
source-selection defect, not evidence that LEAN omitted its terminal equity.
The reconciliation must preserve both artifacts and label their different
purposes; it must not describe the reduced summary as the complete LEAN curve.

Run-88 receipt:

- image: `sha256:3dd003372f1ef1981b4e80038e3f1c557f1fe414d1be531f485ef870f81a5771`;
- upstream image: `docker.io/quantconnect/lean@sha256:4934c22c2b080a688f25b571746603e01533c5e581499d8457e5624a132ba77b`;
- image label: `lean_version=17748`;
- SourceLink commit in Common, Engine and Launcher PDBs:
  `261366a7e26ae942df858ab20df4fef8fa07de67`;
- algorithm source SHA-256: `7bd99036d8e98526bc5b75c6760b67960ae676c6dfc667c9b442df95a2c6d581`;
- Common/Engine/Launcher DLL SHA-256:
  `827339fd94aef0ea71f8576d918e0a361ef858f9d2159a4bb2293018a9a57cbc`,
  `b0eeec4f21b5a4cb458923ca7eb32e34b7ce39f22edd5377da0dfd13d81cf12a`,
  `b2004a97d5ee8f323ff5d342a6f10d57705f088cf417ee551681e708b7a93f0b`;
- Common/Engine/Launcher PDB SHA-256:
  `2d5f78082f8850a4e05bdfe775b23101ab3189e585f279b7961a1a083c8aad61`,
  `1d6f3ed66be0a6c989afff852bbbd01447914bbe915be89780e5d0e829178930`,
  `30788cd31dd9ddd953edbdaef116423afdfc4adfaa356b37a71e03e1f9948788`;
- full result SHA-256: `fd10d7f5793b324d9f70a3c4ecfb1b3e4bf08d431272d4f765861224cd7a69d4`;
- summary result SHA-256: `8c0507842028cdf664f32540db7a11002fe649c4caafe3d7078b930d85cbaf6d`;
- order-event export SHA-256: `6a66ae3d2e4a0b7c9fbefb4d37674c76f019593dcc539fade6727f1b0750642a`;
- observation export SHA-256: `0b4c9f5f669bfa158546aa127cd7371cf8bdcceb274905db4d1162226c6ad6e2`;
- state export SHA-256: `0dce83fa383c7e4a4b2095d70644cc8f9d330cd5ede1c6ef9b0e8c53acb02021`;
- normalized result SHA-256: `121c6026d05561f0d2f26c2e233c5c5e3d54a98d339a6934cf341147c2500729`;
- manifest SHA-256: `a2e12b22eb3cff8a7a27dae7162177f0c6ecc60e2e581bfdf7b2d70cc8eefc94`.

These hashes describe a retained local receipt only. Golden artifacts must be
re-generated from frozen inputs and may never be copied from the database or
edited by hand.

### Known defects to turn into regression tests

| Metric | Current Python mirror | Persisted LEAN row | Raw LEAN `totalPerformance` | Initial diagnosis |
| --- | ---: | ---: | ---: | --- |
| Portfolio Sharpe | `1.401496963760145` | `-1.012` | `-1.0125` | Different daily/rate primitives; persisted row uses rounded dashboard text. |
| Portfolio Sortino | `1.0491022445415183` | `-0.604` | `-0.6035` | Different downside/rate primitives plus display rounding. |
| CAGR | `0.04155659904856801` | `0.04185` | `0.0418` | Different endpoint/date basis and quantization. |
| Drawdown | `0.023752709526763116` | `0.026` | `0.026` | Python reconstruction does not consume LEAN's calculation ledger. |
| Probabilistic Sharpe | `0.7651158918355649` | `0.59675` | `0.5967` | Different return distribution and rounding. |
| Trade profit factor | `1.850746780310548` | `1.9607496178934203` | `1.9827` | Persistence fee-nets trades while LEAN reports gross trade P&L and fees separately. |
| Trade Sharpe | `3.67041740986151` | `0` | `0.2259` | Normalizer incorrectly treats an available full-result field as missing. |
| Trade Sortino | `6.813898504846181` | `0` | `0.3992` | Same loss of Oracle data as Trade Sharpe. |
| Runtime total orders | `73` | `73` | `146` | Both projections count round trips; LEAN counts entry and exit orders. |
| Effective window end | 2026-08-07 | 2026-08-05 | 2026-08-07 | Manifest derives the end from the reduced summary; use the configured/executed window and preserve artifact-specific endpoints. |

The first tests must pin these failures. A change is not accepted merely because
the displayed values become equal.

### Dependency graph and stop rule

```text
R0 contract/source pin
  -> R1 lossless LEAN extraction
    -> R2 deterministic golden fixture
      -> R3 primitive-vector export
        -> R4 Python LEAN-compatible reproduction
          -> R5 persistence and metric receipts
            -> R6 complete platform.performance.v2 and readiness
              -> R7 performance memory, evidence UI, help and CI
```

If a stage fails, every downstream result is `unavailable` with a stable reason.
It is not evaluated, zero-filled, copied from the peer, or made to pass with a
wider tolerance.

### R0 — freeze comparison and source contracts

1. Add a versioned `ComparisonContract` containing profile, strategy twins,
   snapshot/hash, corporate-action hashes, calendar, window, starting capital,
   brokerage/account, sizing, fills, fees, slippage, benchmark, rate, LEAN
   image/source, formatter, native metric, platform metric and readiness IDs.
2. Add an automated image-provenance extractor that asserts image `3dd003...`,
   LEAN build `17748`, the three binary/PDB hashes above, and the common
   SourceLink commit `261366a7...`. Direct inspection established that identity,
   but a manual observation is not yet a fixture contract.
3. Vendor from commit `261366a7...` the exact transitive statistics,
   StatisticsBuilder, formatting,
   interest-rate, benchmark and rolling-window sources used by the image.
   Surface the existing repository conflict explicitly: the older vendored
   commit `7986ed0...` supports indicator provenance, not this runtime's native
   statistics provenance.
   The PDB inventory identifies at least `AlgorithmPerformance.cs`,
   `PortfolioStatistics.cs`, `TradeStatistics.cs`, `StatisticsBuilder.cs`,
   `Statistics.cs`, `StatisticsResults.cs`, `PerformanceMetrics.cs`,
   `IStatisticsService.cs`, `InterestRateProvider.cs`, the constant/function
   risk-free models, the QuantLib constant/Fed-rate estimators, and
   `BacktestingResultHandler.cs`; source inspection must close the remaining
   transitive dependency set.
4. Register three non-interchangeable namespaces:
   `lean.native.<commit>`, `python.lean-compatible.<commit>` and
   `platform.performance.v2`.
5. Define stable unavailable codes for source, fixture, rate, benchmark,
   adjustment, ledger and primitive-export failures.

**Evidence:** schema round-trip/unknown-field tests, contract-hash determinism,
one-field mutation tests, image/source mismatch tests, attribution and authority
registry updates.

**Exit gate:** every dependency influencing a displayed number has an ID and
hash, and an automated test proves the image/binaries/PDBs resolve to the
vendored `261366a7...` sources. A mismatch blocks native formula work.

### R1 — losslessly extract the LEAN Oracle

1. Locate and bind all artifacts to the same algorithm ID: full result, summary,
   order events, log, observation/state exports, data-monitor report and
   manifest. Reject stale or mixed prefixes.
2. Version `NormalizedResult` and preserve full `totalPerformance`, closed
   trades, orders, runtime, formatted statistics, rolling windows, analyses,
   algorithm configuration and every chart/series with `int64 ms UTC`.
3. Preserve numeric text as Decimal-compatible strings at ingestion. Do not
   round-trip through binary float before serialization.
4. Store full-result values and formatted dashboard strings separately. The
   rounded `statistics` block is display evidence, not the numerical Oracle
   when `totalPerformance` exists.
5. Represent missing native data explicitly. Replace `0.0` defaults that
   conflate unavailable with a true zero.
6. Stop recomputing native fields from paired events when LEAN already authored
   them. Paired events remain independent reconciliation evidence.
7. Preserve the full 1,728-point equity series and its terminal sample. Keep the
   reduced 105-point summary series separately as a sampled summary artifact.
8. Derive the run's effective window from the executed algorithm contract and
   calculation evidence, not from the summary-chart endpoint. Persist separate
   first/last timestamps for every artifact and series.

**Evidence:** parser tests pin all run-88 counts above; exact string round trips
for `-1.0125`, `-0.6035`, `1.9827`, `0.2259`, `0.3992` and order count `146`;
missing-versus-zero tests; a hostile rounded/full-result conflict fixture.

**Exit gate:** normalized Oracle JSON is a lossless hashable projection of LEAN
and contains no Python-authored statistic.

### R2 — generate an immutable golden fixture

Create only through a regeneration script:

```text
PythonDataService/tests/fixtures/golden/ema-crossover-lean-statistics-v1/
  manifest.json
  checksums.sha256
  attribution.md
  input/
    bars-or-fixture-reference.json
    map-files/
    factor-files/
    market-hours.json
    symbol-properties.json
    benchmark.json
    interest-rates.json
  oracle/
    raw-result.json
    summary-result.json
    normalized-result.json
    portfolio-statistics.json
    trade-statistics.json
    runtime-statistics.json
    formatted-statistics.json
    closed-trades.json
    rolling-windows.json
    charts.json
  primitives/
    observations.csv
    consolidated-state.csv
    signals.csv
    order-events.json
    portfolio-ledger.jsonl
    daily-equity.csv
    daily-performance.csv
    drawdown.csv
    benchmark-performance.csv
    risk-free-performance.csv
    distribution-moments.json
```

Run LEAN twice in clean, network-isolated workspaces from committed inputs and
require identical semantic hashes. Volatile host IDs may be removed only by a
documented canonicalizer; calculation evidence may not be removed.

Fixture cells:

| Cell | Purpose | Special assertion |
| --- | --- | --- |
| `small_algebra` | Hand-reviewable wins, losses, flat day, drawdown/recovery | Independent hand calculations for primitives and selected formulas. |
| `flat_no_trade` | Zero variance/no trade | Exact zero versus unavailable rules; no NaN/Infinity. |
| `single_trade` | Sample-size boundary | Exact PSR, variance, Sharpe and duration availability. |
| `all_wins` | No downside observations | Exact Sortino/downside behavior. |
| `all_losses` | No winning observations | Exact PF/payoff/expectancy behavior. |
| `session_boundaries` | Overnight stale fill, DST, early close, holiday/weekend | Exact calendar and `int64 ms UTC`. |
| `raw_corporate_action_guard` | Split/dividend in raw mode | Complete factor/map evidence or fail closed. |
| `SPY_2024-08-08_2026-08-07` | Runs 87/88 regression | 73 trades, 146 orders, terminal accounting, all native fields/strings. |
| Existing W6mo/W12mo multi-symbol matrix | Cross-symbol regression | Extend existing receipts; never replace execution evidence. |

**Exit gate:** hashes verify offline, duplicate generation is deterministic and
deliberate one-byte/one-value mutation tests fail.

### R3 — export LEAN calculation primitives

`totalPerformance` is the output Oracle but is insufficient to diagnose every
formula. Add fixture-only instrumentation inside the pinned LEAN runtime to
export StatisticsBuilder inputs:

- full portfolio ledger and calculation-grid timestamps;
- daily marked equity including flat days and terminal equity;
- adjacent daily returns and first-observation rule;
- drawdown, peak, trough, recovery timestamps and durations;
- benchmark return vector;
- dated LEAN interest-rate vector and interval conversion;
- trade P&L, MAE/MFE, durations, fees and profit/loss-rate vectors;
- means, counts, variance, downside variance, skewness and kurtosis inputs;
- turnover/capacity inputs where those values remain exposed; and
- formatter input, format specifier, culture and final string.

The exporter is fixture-generation code only; production Python never calls
LEAN. Chart series are evidence, not substitutes for calculation primitives.

**Exit gate:** every exposed statistic cites its primitive vector and source
function. `equity = cash + holdings` and terminal accounting identities pass.

### R4 — reproduce LEAN-native statistics in Python

Implement `python.lean-compatible.<commit>` from vendored source in this order:

1. trade counts, gross P&L, fees, win/loss rates, streaks and durations;
2. MAE/MFE, trade drawdowns, PF and profit/loss ratio;
3. daily equity, returns, drawdown and recovery;
4. CAGR, annual variance and standard deviation;
5. native risk-free/downside vectors, Sharpe and Sortino;
6. skewness, kurtosis and probabilistic Sharpe;
7. benchmark covariance, alpha, beta, tracking error, information and Treynor;
8. VaR, turnover and remaining exposed portfolio fields;
9. rolling-window results; and
10. exact formatted strings.

Test each primitive vector before its scalar. On failure classify the first
upstream difference using the existing taxonomy: input/rate/benchmark as
`data-quality`; grid/order as `timestamp` or `off-by-one`; fees as
`commission`; formula/availability branch as `strategy-logic`; numeric-only
accumulation as `precision`. Fix one first divergence at a time.

Every touched/new function carries the four-field Math Provenance Contract and
updates `docs/math-sources-of-truth.md`.

**Exit gate:** all 55 fields currently exposed by `LeanStatisticsResponse`
match, every dashboard string matches exactly, and additional LEAN fields remain
preserved even when not displayed. One unexplained mismatch blocks promotion.

### R5 — persist namespaces and metric receipts

Persist immutable `lean_native`, Python-authored `python_lean_compatible`, and
independently defined `platform_performance` objects. Each receipt contains
metric/version, engine, numeric/display value, unit, availability, primitive
hashes, observation boundaries/count, rate/benchmark IDs, fee treatment,
absolute error, tolerance/quantization interval, source commit and fixture.

Backend performs Decimal/JSON transport only. Angular renders receipts only.
Add one native-statistics parity verdict per pair; it is `agree` only when every
required metric and string passes. An intentional platform/native distinction
is `definition_differs`, not a parity failure.

**Exit gate:** either run resolves the same immutable receipts and no fallback
can substitute/copy values across namespaces.

### R6 — complete platform performance and readiness 17/17

Replace the temporary closed-trade projection with a full marked platform
ledger and frozen FRED `DGS3MO` platform risk-free contract. Implement the five
missing readiness inputs:

1. annual volatility from canonical daily returns;
2. drawdown recovery from canonical peak/recovery timestamps;
3. max consecutive losses from canonical fee-aware trades;
4. PSR from the canonical daily distribution; and
5. trade-versus-portfolio Sharpe gap from two versioned Sharpe definitions.

Both engines calculate the platform object from equivalent full ledgers. If a
LEAN ledger primitive is absent, its readiness remains incomplete; Python's
grade is never copied.

**Exit gate:** both members carry identical 17/17 receipts and exact readiness
JSON. Removing any required input produces `Incomplete` and cannot improve the
grade.

### R7 — performance memory, evidence UX and CI

1. Drive performance memory only from validated platform primitives with a
   frozen `as_of_ms`; never from either LEAN chart artifact.
2. Golden-test horizons, weekday/hour, month and rolling cells including counts,
   boundaries and unavailable reasons.
3. Show contract/snapshot hashes, namespaces, formulas, primitive series,
   rate/benchmark, tolerance, both values and first failed gate in the UI/manual.
4. Explain the full versus summary LEAN curve explicitly and retain both without
   synthetic alteration.
5. Run small/degenerate cells on each relevant PR and the full two-year and
   multi-symbol suite offline before merge/nightly. Fixture regeneration stays
   manual and reviewed.
6. Roll out receipts in shadow mode before gating. Historical evidence remains
   immutable; retained raw artifacts may receive a new derived version only.

**Exit gate:** every displayed number is reproducible from its receipt and no UI
state can imply certification while an upstream gate is unavailable.

### Tolerances

Use `rtol=0` throughout.

| Artifact | Comparison |
| --- | --- |
| Hashes, IDs, availability, counts, order, sides, quantities, timestamps | Exact |
| Raw Decimal strings | Exact round trip |
| Identical serialized OHLCV, signals, fills and fees | Exact at data/currency quantum |
| Dollar ledger/P&L after Decimal boundary | `atol=1e-6` |
| Indicator/return/distribution vectors and full native ratios | `atol=1e-9` |
| Probabilities | `atol=1e-10` |
| Quantized-only LEAN values | Exact primitives plus documented half-display-unit interval |
| Formatted values/durations | Exact string |
| Readiness inputs/scores/grade/signal/evidence | Exact |

A tolerance change requires a `precision` classification, impact analysis,
reference-note update and review. It cannot accompany a fixture change made to
turn a failure green.

### Reviewable issue order

1. ComparisonContract, source/image proof and vendored Statistics sources.
2. Full-result/summary/rolling lossless normalizer with run-88 regressions.
3. Golden generator, deterministic small cells and hash/mutation tests.
4. Full SPY Oracle capture plus calculation-primitive exporter.
5. LEAN-compatible trade-statistics port and receipts.
6. Portfolio ledger, CAGR, drawdown, variance and recovery port.
7. Native rates, Sharpe/Sortino/PSR, benchmark metrics and formatter.
8. Persistence/API comparison verdict and evidence model.
9. Full platform performance, five missing inputs and 17/17 readiness fixture.
10. Performance-memory golden, UI/manual, shadow rollout and CI promotion.

Each issue must leave the repository independently green. Never combine an
unresolved upstream divergence with a downstream tolerance or fixture change.

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

## Calculation contract to validate

The fixture must carry both the primitive inputs and the LEAN output for every
calculation. Formula names alone are insufficient because cadence, calendar,
risk-free conversion, benchmark, fee inclusion, denominator convention, and
rounding all affect the result.

| Layer / measure | Required primitive inputs | Contract that must be frozen | Current authority / oracle |
| --- | --- | --- | --- |
| Portfolio equity | cash, holdings, mark, fees at every ledger timestamp | `equity = cash + sum(quantity * mark)`; specify mark and timestamp cadence | LEAN portfolio ledger export; Python execution portfolio |
| Final P&L | starting and terminal equity, deposits/withdrawals | `terminal equity - starting equity` for this no-cash-flow fixture | LEAN runtime and ledger terminal row |
| Trade P&L | entry/exit fills, signed quantity, entry/exit fees | exact net cash P&L; fees allocated by an explicit rule | LEAN closed trade / order-event export |
| LEAN-compatible trade return | LEAN trade P&L and capital base | exact pinned LEAN trade-statistics semantics | `lean.native` trade-statistics export |
| Platform trade return | net P&L and gross entry notional | fee-aware `net P&L / gross entry notional`; never substitute price-only return | Independent hand fixture plus Python implementation |
| Daily equity | full portfolio ledger and exchange calendar | last eligible mark per New York trading date, including flat days | Exact LEAN daily-performance series |
| Daily return | adjacent daily equity values | `r[t] = equity[t] / equity[t-1] - 1`; first-row rule frozen | Exact LEAN daily-performance series |
| Drawdown | full marked equity series | `1 - equity[t] / running_peak[t]`; cadence and peak initialization frozen | LEAN drawdown series and maximum |
| Annual volatility | daily return vector | sample/population variance and annualization factor frozen | Pinned LEAN `PortfolioStatistics` source |
| LEAN-compatible Sharpe | LEAN daily performance and frozen LEAN rate file | exact LEAN excess-return definition, average-rate rule, annualization, and zero-variance behavior | `lean.native` is oracle; Python reproduction must match |
| LEAN-compatible Sortino | LEAN daily performance, frozen LEAN rate file, target return | exact LEAN numerator, downside denominator, annualization, and degenerate behavior | `lean.native` is oracle; Python reproduction must match |
| Platform Sharpe | canonical daily equity and frozen FRED rate series | interval excess-return definition, rate conversion, cadence, annualization, and zero-variance behavior | Independent hand fixture plus Python implementation |
| Platform Sortino | canonical daily equity, frozen FRED rate series, target return | downside vector/denominator, rate conversion, annualization, and degenerate behavior | Independent hand fixture plus Python implementation |
| CAGR | starting/ending equity and exact elapsed period | LEAN period/year convention; no substitution of `252 / trading_days` unless oracle proves it | Pinned LEAN source and summary output |
| Calmar | CAGR and maximum drawdown | divide/zero behavior and precision frozen | Derived from already validated primitives |
| Profit factor | net trade P&Ls | `sum(wins) / abs(sum(losses))`; zero-loss convention frozen | LEAN trade-statistics export |
| Payoff ratio | winning and losing trade returns or P&Ls | mean basis, fee treatment, zero-loss convention frozen | LEAN trade-statistics export |
| Expectancy | win rate, average win, average loss | units and fee treatment frozen | LEAN trade-statistics export |
| PSR | Sharpe observations, skewness, kurtosis, benchmark Sharpe | exact sample, moment conventions, benchmark, and minimum-observation behavior frozen | Pinned LEAN source and primitive-vector export |
| Trailing memory | canonical equity and trades, fixed `as_of_ms` | horizon boundary inclusion, timezone, minimum-history rules | Python analytics applied to LEAN golden primitives |
| Weekday/hour expectancy | canonical entry timestamp and fee-aware trade return | New York timezone, DST, bucketing, and weighting | Python analytics applied to LEAN golden trades |
| Seasonality | canonical return series | calendar-month compounding, cross-year aggregation, missing-month behavior | Python analytics applied to LEAN golden primitives |
| Rolling stability | ordered canonical trades | window length, partial-window rule, return basis | Python analytics applied to LEAN golden trades |
| Readiness verdict | one validated metric payload and availability mask | scorer version, cutoffs, weights, missing-value policy, rounding | `compute_run_verdict`, tested against frozen expected JSON |

For calculations that are intentionally repository-specific, such as
performance memory and the production-readiness score, LEAN is the oracle for
the input equity/trade/statistics evidence, not for a feature LEAN does not
implement. Correctness means the Python projection gives the hand-reviewed,
versioned expected output when supplied with the LEAN-authored primitives.

## Golden-fixture design

### Fixture identity

Create a versioned fixture directory such as:

`PythonDataService/tests/fixtures/golden/ema-crossover-lean-statistics-v1/`

Use one primary two-year SPY cell matching the investigated scenario and at
least two small diagnostic cells:

- a no-trade/flat-equity cell for degenerate statistics;
- a short cell containing a session-close signal followed by a weekend or
  holiday, proving stale-signal next-open behavior;
- the full 2024-08-08 through 2026-08-07 SPY cell for realistic metrics and all
  performance-memory horizons.

The large cell is the acceptance oracle. The small cells make failures easy to
localize and review.

### Required fixture contents

```text
ema-crossover-lean-statistics-v1/
  attribution.md
  manifest.json
  input/
    bars-manifest.json
    session-calendar.json
    lean-interest-rates.csv
    fred-dgs3mo.csv
    benchmark.csv
  lean/
    observations.csv
    consolidated-state.csv
    signals.csv
    order-events.jsonl
    closed-trades.csv
    portfolio-ledger.csv
    daily-equity.csv
    daily-returns.csv
    drawdown.csv
    statistics-primitives.json
    statistics-full-precision.json
    statistics-formatted.json
  expected/
    python-lean-compatible-statistics.json
    platform-performance-v2.json
    performance-memory.json
    run-verdict.json
    availability-mask.json
```

`manifest.json` must contain and hash every file, plus:

- fixture schema/version and fixture ID;
- repository commit, LEAN source commit, container image digest, launcher hash,
  algorithm source hash, and generator source hash;
- exact command and parameters;
- symbol/security identifier, starting cash and account currency;
- input source and aggregate canonical bar hash;
- raw/adjusted normalization decision, factor/map files, exchange calendar,
  market hours, regular/extended-session policy, fill-forward setting, and data
  timezone;
- bar timestamp semantics (`int64 ms UTC` at the boundary), consolidation
  period, and session-close behavior;
- brokerage, account type, fee model, fill model, slippage model, sizing model,
  free-portfolio buffer, benchmark, LEAN risk-free provider/rate hash, platform
  FRED performance-rate contract/hash, and the mapping between native and
  Python LEAN-compatible metric identities;
- exact output row counts, first/last timestamps, and terminal-equity invariant;
- generation time and `clean=true` evidence.

The fixture should reference a shared immutable raw-bar object by SHA-256 rather
than duplicate hundreds of megabytes. CI must fail if the object is missing or
its bytes differ. A live Polygon request is never part of a golden test.

### Oracle independence

The golden output is credible only if it is independent of the Python code it
validates:

1. Generate signals, fills, portfolio ledger, return vectors, and full-precision
   statistics inside the pinned LEAN runtime.
2. Do not import Engine Lab calculators, reconciler helpers, or JSON models in
   the LEAN exporter.
3. Vendor the exact LEAN statistics source files used by the pinned image under
   `references/lean/<commit>/` and cite file, type, and relevant method in
   `attribution.md`. The current vendored extract does not contain the complete
   statistics implementation required for this claim.
4. Export primitive vectors before formatting. Dashboard strings rounded to
   three decimals are audit evidence, not a precise numerical oracle.
5. Refuse fixture generation if the worktree, algorithm hash, image digest,
   input hash, rate file, calendar, or expected source commit differs from the
   manifest.
6. Generate once, review the diff, and commit the receipt. Tests consume the
   fixture read-only; they never regenerate it automatically.
7. Never edit an expected value to make a test pass. A fixture change requires
   a new version or an explicit, reviewed re-baseline receipt explaining the
   upstream change.

## Validation gates

### Gate 0 — provenance and reproducibility

- Verify every manifest hash and the pinned LEAN image digest.
- Verify the LEAN source commit and vendored statistics sources.
- Re-run LEAN twice in isolated workspaces; all canonical artifacts must be
  byte-identical after excluding explicitly non-semantic metadata.
- Assert that both engines receive the same canonical bar hash, rate series,
  benchmark, market calendar, parameters, and effective time window.
- Reject `adjustment_unsupported`, `pre_adjusted_non_reconciliation`, missing
  parity group, missing terminal row, or live/unhashed data.

**Exit criterion:** one immutable input contract and one reproducible LEAN
receipt.

### Gate 1 — observations and consolidation

- Compare every minute observation: timestamp, OHLCV, session inclusion, and
  ordering.
- Compare every 15-minute consolidated close timestamp and OHLCV.
- Assert no duplicate, missing, or out-of-order timestamp.
- Assert exact session boundaries, early closes, holidays, DST transitions, and
  the end-date convention.

**Exit criterion:** exact timestamps and OHLCV values for all consumed and
consolidated bars.

### Gate 2 — strategy state and decisions

- Compare EMA(5), EMA(10), Wilder RSI(14), warm-up state, crossover state,
  holding-period state, and ENTER/EXIT/HOLD decision at every consolidated bar.
- Store signal timestamp separately from order submission and fill timestamp.
- Stop at the first mismatch and classify it before comparing trades.

**Exit criterion:** identical decision stream; indicator values within the
declared `1e-9` absolute tolerance.

### Gate 3 — orders, fills, fees, and sizing

- Run Engine Lab in an explicit `lean_parity` execution profile using
  `LeanSetHoldingsSizing`, the IBKR fee model, LEAN free-portfolio buffer,
  next-available-open stale-signal behavior, and the same slippage policy.
- Compare event identity, side, signal time, submit time, fill time, price,
  signed quantity, order status, per-fill fee, and cumulative fee.
- Extend the reconciliation assertion so timestamp equality is a gating field;
  aligning only by trade number is insufficient.
- Classify divergences using the repository taxonomy and include the first
  causal mismatch, not merely all downstream mismatches.

**Exit criterion:** zero gating order/fill divergences and exact cumulative
fees.

### Gate 4 — portfolio ledger and canonical equity

- Export, for both engines, cash, holdings, mark, unrealized P&L, realized P&L,
  cumulative fees, and total equity on a shared calculation grid. Do not use
  either LEAN display-chart artifact as a substitute for this export.
- Include every fill timestamp, each agreed daily mark, and an explicit terminal
  timestamp after final liquidation/end-of-run processing.
- Assert the accounting identity on every row.
- Assert terminal holdings are zero, terminal equity equals reported end
  equity, and `end equity - start equity` equals net P&L.
- Derive drawdown from the full calculation curve. Validate Python display
  downsampling separately and prove it preserves first/last points and never
  feeds calculations. Preserve both LEAN-native chart artifacts separately
  with their source and sampling receipts.

**Exit criterion:** both ledgers pass their accounting identities and agree on
the common calculation grid, daily marks, fees, terminal holdings, P&L, and
equity. Display-chart sampling is explicitly outside this equality assertion.

### Gate 5 — LEAN statistic primitive vectors

- Compare exact daily-equity dates and values.
- Compare daily returns, drawdown values, LEAN excess returns, LEAN downside
  values, benchmark returns, dated LEAN risk-free values, trade P&Ls, and
  fee-aware trade returns.
- Assert observation count and sample start/end for every vector.
- Store the convention metadata beside each vector.

**Exit criterion:** the Python LEAN-compatible calculation receives vectors and
availability states identical to the pinned LEAN statistic inputs.

### Gate 6A — LEAN-native portfolio and trade statistic parity

- Compute every Python LEAN-compatible metric only from the Gate 5 vectors.
- Compare every exposed portfolio and trade metric at full precision with the
  LEAN full-precision export, including all headline metrics, risk-adjusted
  metrics, distribution moments, trade statistics, capacity/turnover fields,
  and availability states present in the pinned result schema.
- Separately assert that formatter output reproduces LEAN dashboard strings.
- Publish a per-metric receipt containing LEAN value, Python reproduction,
  absolute error, tolerance/quantization interval, and pass/fail status.

**Exit criterion:** `python.lean-compatible` matches `lean.native` for every
exposed statistic. One unexplained mismatch fails the compatibility run.

### Gate 6B — platform KPI contract

- Calculate platform metrics from the canonical Python ledger and frozen FRED
  performance-rate contract, never from LEAN dashboard strings.
- Golden-test the FRED join, day-count/rate conversion, excess-return vector,
  downside vector, and every platform formula independently.
- Make persisted headline columns, API statistics, performance memory, and
  readiness inputs reference one `platform.performance.v2` object.
- Give intentionally different LEAN and platform definitions different metric
  IDs and show **Definition differs**, not a parity failure.

**Exit criterion:** platform KPI JSON matches the independently reviewed golden
artifact, and no run contains contradictory values for one metric identity.

### Gate 7 — performance memory

- Feed `engine_validation_analytics.py` the validated canonical platform equity
  and trade primitives using a frozen `as_of_ms`.
- Golden-test every trailing horizon, weekday/hour cell, calendar-month cell,
  and rolling window, including counts and unavailable reasons.
- Add boundary cases for a trade exactly on the horizon boundary, DST, weekend,
  early close, empty window, all-win window, and all-loss window.
- Resolve the current return-definition conflict: Python detail/analytics use a
  price-only return, while LEAN parity evidence uses net P&L divided by entry
  notional. Platform performance memory must use the fee-aware platform trade
  return; LEAN-compatible trade statistics must retain the exact pinned LEAN
  definition.
- Do not calculate LEAN-native performance memory from either chart artifact.
  Show it as unavailable unless the full LEAN primitive export is present.

**Exit criterion:** entire JSON payload, timestamps, counts, availability, and
numbers match the committed expected artifact.

### Gate 8 — production-readiness verdict v2

- Pass only the validated `platform.performance.v2` object and its explicit
  availability mask to the v2 scorer.
- Freeze scorer version, weights, thresholds, rounding, and missing-value
  policy in the expected artifact.
- Assert raw input value, subscore, dimension score, composite, grade,
  deployment signal, evidence copy, and availability reason.
- Require all 17 published inputs for a certified letter grade. Assert that one
  missing required value yields `status=incomplete`, `grade=null`, and a
  coverage receipt rather than a reweighted grade.
- Add metamorphic tests: removing expectancy or any other required input cannot
  improve a certified grade; changing only display rounding must not change a
  score; equivalent payload order must not change the verdict.
- The UI must render the backend-authored verdict without recalculation.

**Exit criterion:** exact verdict JSON, full required coverage for a certified
grade, and a comparison contract that forbids different scorer/metric contracts
from being called equivalent.

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

## Proposed implementation sequence mapped to R0–R7

### PR 1 — freeze terminology, schemas, and the common profile

- Add a versioned `ComparisonContract` schema containing strategy-twin IDs,
  data-snapshot ID/hash, corporate-action hashes, time/calendar policy,
  execution model IDs, LEAN source/image IDs, benchmark/rate contracts, metric
  contracts, and capability/unavailable receipts.
- Register `us-equity-raw-ibkr-v1` as the first supported intersection profile.
- Define **native run**, **paired compatibility run**, **LEAN-compatible
  metric**, **platform metric**, **reconciliation-grade**, and **diagnostic
  replay** in the domain glossary.
- Pin the LEAN source commit and vendor the exact statistics implementation and
  dependencies used by the image.
- Add fixture schema, attribution template, hash verifier, and stable reason
  codes. Update `docs/math-sources-of-truth.md` and
  `docs/architecture/engine-authority-map.md` for the new authorities.

**Review gate:** the schema can represent every setting that caused runs 75 and
76 to diverge; omission of an execution or statistics dependency fails review.

### PR 2 — make LEAN extraction lossless and repair run envelopes

- Replace the summary-only parser with an algorithm-ID-bound extractor that
  ingests the full result, summary, order events, observation/state exports,
  data-monitor report, logs and manifest as distinct artifacts.
- Preserve the full-precision portfolio/trade objects, closed trades, orders,
  runtime/formatted statistics, rolling windows, analyses, configuration and
  every chart series without Python recomputation.
- Preserve Decimal-compatible numeric text and distinguish unavailable from a
  true zero. Add a versioned normalized schema and reject mixed/stale artifact
  prefixes.
- Repair the run-envelope window derivation: the algorithm window cannot end at
  the reduced summary curve's last timestamp. Persist run, artifact and series
  endpoints separately.
- Pin the run-88 artifact counts, full/summary curve endpoints, native
  statistics, hashes, 146-order count and missing-versus-zero cases as
  regressions.

**Review gate:** the normalized object is a lossless hashable LEAN projection;
its native namespace contains no Python-authored or zero-filled statistic.

### PR 3 — build and freeze the independent LEAN oracle exporter

- In a fixture-only LEAN algorithm, export observations, consolidated state,
  signals, full order events, closed
  trades, portfolio ledger, daily performance, drawdown, benchmark/rate inputs,
  distribution primitives, and full-precision portfolio/trade statistics from
  inside the pinned LEAN runtime.
- Add terminal-equity and accounting-identity self-checks to the exporter.
- Capture both full-result and summary chart metadata separately from
  calculation primitives.
- Generate the no-trade, weekend/holiday-fill, and full two-year cells twice in
  isolated workspaces and demonstrate deterministic semantic hashes.
- Commit the reviewed artifacts; fixture tests remain offline and read-only.

**Review gate:** no Python calculator participates in oracle generation, and
the receipt identifies every byte and source version needed to reproduce it.

### PR 4 — make execution compatibility a persisted Python profile

- Harden the existing server-owned pair launch so it stages and hashes one raw
  snapshot, persists contract/group/peer IDs on both members, dispatches
  exactly two jobs, and resolves peers only by those immutable identifiers.
- Add the reverse path for an eligible retained LEAN workspace to create a
  persisted Python peer. Fail closed for adjusted input, missing bytes/hashes,
  an unknown strategy twin or unsupported settings.
- Route coordinator-managed Python runs through the proven
  `LeanSetHoldingsSizing`, IBKR fee model, LEAN free-portfolio buffer, and
  stale-session-close next-open behavior.
- Keep ordinary `SimpleFloorSizing`/signal-close research semantics as a
  separately named native profile; do not silently change historical behavior.
- Extend reconciliation to gate exact signal, submit, and fill timestamps,
  prices, quantities, statuses, fees, cumulative cash, and terminal holdings.
- Turn the 59 quantity mismatches and six timestamp/price mismatches from runs
  75/76 into explicit regression assertions.

**Review gate:** Gates 0–4 pass with zero unexplained gating divergences on all
fixture cells.

### PR 5 — reproduce every exposed LEAN-native metric in Python

- Implement `python.lean-compatible.<source-commit>` from the vendored LEAN
  formulas without a runtime dependency on LEAN.
- Feed it the exact LEAN daily-performance, benchmark, trade, and interest-rate
  primitives; do not substitute FRED or the platform ledger.
- Reconcile metric inputs in dependency order, then portfolio statistics,
  trade statistics, availability/degenerate cases, and formatter strings.
- Persist a per-metric receipt with LEAN native value, Python value, absolute
  error, tolerance or quantization interval, and status.
- Eliminate conflicting Python headline/LEAN-style payload identities and add
  the required reference note and math-source registry rows.

**Review gate:** every exposed LEAN-native statistic matches for identical
settings. A single unexplained mismatch blocks the PR.

### PR 6 — establish the platform KPI, risk-free, and equity contracts

- Emit a full canonical Python ledger and derive daily performance from marked
  equity rather than closed-trade pseudo-equity.
- Add the frozen FRED `DGS3MO` performance-rate provider with no silent
  fallback; record the rate join and conversion receipt.
- Complete the rate-conversion ADR and independently calculate the small-cell
  expected Sharpe and Sortino before implementing the general path.
- Calculate all `platform.performance.v2` KPIs from one versioned object and
  make persisted headline fields/API consumers reference it.
- Make Python downsampling preserve the terminal point and remain display-only.
  Keep both LEAN chart artifacts untouched and add their full-versus-summary
  source, sample-count and endpoint receipts.

**Review gate:** Gate 6B passes, all headline consumers use the same metric IDs,
and platform-versus-LEAN definition differences are explicit rather than
reported as parity errors.

### PR 7 — validate performance memory and readiness v2

- Resolve the fee-aware trade-return definition and compute all performance
  memory from canonical platform primitives.
- Implement `readiness-core-v2` with the fixed 17-input requirement and frozen
  dimension weights; missing evidence yields no grade.
- Remove fallback selection between unrelated native/platform fields and remove
  any remaining frontend scoring after its parity gate passes.
- Commit exact performance-memory, readiness, coverage, and unavailable-reason
  JSON plus the metamorphic tests.

**Review gate:** the same platform metric object feeds headline KPIs,
performance memory, and readiness; removing a required input makes the grade
incomplete and never improves it.

### PR 8 — product manual, evidence UI, telemetry, and CI acceptance

- Replace “Both — validate equivalence” with **Paired compatibility run** and a
  pre-launch assumption/capability preview.
- Add the deterministic **Create compatible Python run** action on eligible
  LEAN runs and auto-select an existing exact peer when present.
- Add an evidence panel showing fixture/snapshot ID, contract/profile IDs,
  hashes, LEAN source/image, engine settings, gate matrix, first divergence,
  and per-metric parity receipts.
- Create a metric and compatibility help manual. Each KPI page includes meaning,
  formula, primitive inputs, rate/benchmark, cadence, units, unavailable rules,
  interpretation, caveats, LEAN-native mapping, and golden test. Info buttons
  are driven by closed backend metric metadata; Angular performs no math.
- Add the LEAN full-result-versus-summary help text and the risk-free
  definition-difference help text specified above.
- Record anonymized counts of requested and successful compatibility profiles
  so a future adjusted profile/default decision uses actual product behavior.
- Run small fixture gates on every relevant change and the deterministic
  two-year fixture offline in CI; regeneration remains manual and reviewed.

**Review gate:** a user cannot mistake an inferred pair, LEAN chart artifact,
different metric definition, incomplete readiness score, or diagnostic replay
for certified parity.

## Proposed tests

The exact filenames may follow nearby conventions, but the ownership should be
clear:

- `PythonDataService/tests/research/parity/test_ema_crossover_lean_statistics.py`
  — Gates 0 through 6 for the new fixture.
- `PythonDataService/tests/services/test_compatibility_coordinator.py` — exact
  peer lookup, reverse LEAN-to-Python creation, eligibility, idempotency,
  partial failure, and exactly-two-job behavior.
- `PythonDataService/tests/research/parity/test_cross_engine_study.py`
  — extend the existing observation/state/trade harness with exact timestamp
  and portfolio-ledger gates.
- `PythonDataService/tests/engine/results/test_lean_statistics_golden.py`
  — every exposed `python.lean-compatible` metric, availability state,
  full-precision tolerance, and formatted-string parity with `lean.native`.
- `PythonDataService/tests/engine/results/test_platform_performance_v2_golden.py`
  — canonical ledger, frozen FRED join/conversion, and platform KPI JSON.
- `PythonDataService/tests/services/test_engine_validation_analytics_golden.py`
  — complete canonical performance-memory JSON and explicit LEAN
  chart-as-calculation-source unavailability.
- `PythonDataService/tests/services/test_run_verdict_v2_golden.py` — complete
  readiness JSON, fixed 17-input coverage, and no dynamic denominator.
- Angular contract tests — prove the page renders backend values and does not
  calculate or reinterpret them; exact-peer selection, compatibility preview,
  metric-definition labels, and curve/risk-free help copy are included.

The existing committed SPY/QQQ W3mo and W6mo fixtures under
`PythonDataService/tests/fixtures/golden/cross-engine-studies/cells/` already
prove observation, state, signal, fill, quantity, and commission parity for the
explicit LEAN-parity runner. They should be extended, not replaced. They do not
currently prove the full equity/statistics/readiness chain described here.

## Acceptance criteria for a credible proof

The validation may be called complete only when all of the following are true:

1. One committed manifest pins the exact LEAN runtime, source, algorithm, data,
   calendar, rates, benchmark, execution, and calculation contracts.
2. Repeated LEAN oracle generation is deterministic.
3. Both engines consume identical observations and emit identical consolidated
   decisions.
4. Signal, submit, and fill timestamps; prices; quantities; fees; and trade
   outcomes pass Gate 3 with zero gating divergences.
5. The comparison coordinator creates or resolves exactly one deterministic
   peer using shared group, contract, and snapshot identifiers; no inferred or
   duplicate companion is possible.
6. The full portfolio ledgers pass their accounting identities and agree on the
   common calculation grid, daily marks, terminal holdings, cash, fees, P&L,
   and equity. Both LEAN display-chart artifacts are outside this assertion.
7. LEAN daily performance, drawdown, native rate, benchmark, downside, and
   trade primitive vectors have identical timestamps, lengths, and values in
   LEAN and the Python LEAN-compatible calculation.
8. Every exposed full-precision `python.lean-compatible` portfolio and trade
   statistic passes against `lean.native`, and every formatted LEAN dashboard
   string matches exactly.
9. The independently validated `platform.performance.v2` artifact passes, and
   headline KPIs, persisted platform fields, API payloads, performance memory,
   and readiness cite those same versioned metric identities.
10. Performance-memory JSON matches its golden expected artifact exactly and
    is never derived from either LEAN chart artifact.
11. Readiness input values, 17/17 coverage, subscores, dimensions, composite,
    grade, signal, and evidence match exactly; any missing required input makes
    the grade incomplete.
12. Python's downsampled UI curve retains both endpoints and is never used for
    calculation. LEAN full-result and summary curves are separately labeled,
    disclose their endpoint relationship, and are never synthetically altered.
13. The help manual and every metric info button identify formula, inputs,
    rate/benchmark, metric namespace/version, and fixture receipt.
14. CI verifies artifact hashes and every gate without network access.

The Python LEAN-compatible formula may not deliberately differ from the pinned
LEAN formula; a mismatch is a failed compatibility test. A separate platform
formula may intentionally differ, but it must have a different metric
name/version, expose both calculation receipts, and must not be presented as a
LEAN-native parity delta or feed a mixed-definition grade comparison.

## Disposition of runs 75 and 76

- Preserve run 76's manifest and normalized result as investigation evidence.
- Do not promote them to the golden directory.
- Do not use the A-versus-B readiness result as evidence that one engine or
  strategy is better.
- Use the six timing examples, the 59 sizing divergences, the summary-file
  source-selection defect, and the conflicting Python metric payloads as
  regression cases when building the new gates.
- Re-run the same strategy only after a reconciliation-grade fixture and
  explicit `lean_parity` execution profile exist. That new paired run, not IDs
  75 and 76, becomes the candidate acceptance receipt.

## Related repository evidence

- [Engine Lab compatibility runs 95 / 96 golden attribution](../../../PythonDataService/tests/fixtures/golden/engine-lab-compatibility-95-96-v1/attribution.md)
  — immutable live acceptance pair, exact input bytes, raw LEAN artifacts and
  offline reconciliation gate.
- [EMA crossover signal LEAN parity receipt](./ema-crossover-signal-lean-2026-07-18.md)
  — existing observation/state/fill parity receipt.
- [Six-day SPY EMA smoke receipt](./lean-vs-python-spy-ema-6day-2026-06-10.md)
  — short no-trade smoke receipt.
- [Engine validation analytics reference](../engine-validation-analytics.md) —
  current performance-memory definitions.
- [Math sources of truth](../../math-sources-of-truth.md) — current statistic
  and verdict authorities.
- [Engine authority map](../../architecture/engine-authority-map.md) — Engine
  Lab's engine-level ownership.

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
