# LEAN native statistics Oracle v1

**Status:** implemented and acceptance-tested 2026-08-08

**Oracle:** QuantConnect LEAN build `17748`

**LEAN source:** commit `261366a7e26ae942df858ab20df4fef8fa07de67`

**Comparison contract:** `us-equity-raw-ibkr-v1`
**Fixture:** `PythonDataService/tests/fixtures/golden/lean-statistics-oracle-v1/`
**Formulas:** `PythonDataService/app/engine/results/lean_statistics.py`

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
