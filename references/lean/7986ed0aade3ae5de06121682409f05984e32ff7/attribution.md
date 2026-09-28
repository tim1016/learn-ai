# LEAN — vendored extract

**Upstream:** https://github.com/QuantConnect/Lean
**Commit:** `7986ed0aade3ae5de06121682409f05984e32ff7`
**Commit message:** "Use REST API in stubs repo discovery and run it inside the container (#9434)"
**Commit date (UTC):** 2026-04-23T21:32:57Z
**Vendored on:** 2026-04-26
**Vendored by:** Inkant
**License:** Apache-2.0 (SPDX: `Apache-2.0`)
**Upstream LICENSE:** https://github.com/QuantConnect/Lean/blob/7986ed0aade3ae5de06121682409f05984e32ff7/LICENSE

## What is vendored

The minimum subset of LEAN required to audit the ports below: originally the
`Indicators/` files behind `PythonDataService/app/engine/indicators/`, and
since #2479 the factor-file sources behind
`PythonDataService/app/data_lake/factor_files.py`:

| Vendored file | Purpose | Port target | Port reference note |
|---|---|---|---|
| `Indicators/SimpleMovingAverage.cs` | Reference SMA implementation | `PythonDataService/app/engine/indicators/sma.py` | `docs/references/` (TBD on next touch of `sma.py`) |
| `Indicators/ExponentialMovingAverage.cs` | Reference EMA implementation, including the SMA-seeded warmup behavior | `PythonDataService/app/engine/indicators/ema.py` | `docs/references/` (TBD on next touch of `ema.py`) |
| `Indicators/RelativeStrengthIndex.cs` | Reference RSI implementation, including Wilders smoothing | `PythonDataService/app/engine/indicators/rsi.py` | `docs/references/` (TBD on next touch of `rsi.py`) |
| `Indicators/IndicatorBase.cs` | Base class — referenced by all three above for `Update`, `IsReady`, `Samples`, and `RollingWindow` semantics | (transitively used) | n/a |
| `Indicators/MovingAverageType.cs` | Enum — `RelativeStrengthIndex` accepts `MovingAverageType.Wilders` which our port pins to | (transitively used) | n/a |
| `Data/equity/usa/factor_files/aapl.csv` | QuantConnect's own published AAPL factor file — the **oracle** for factor-file dividend pricing (#2479): its rows encode each dividend as `1 − cash / reference_close` (raw/raw) | `PythonDataService/app/data_lake/factor_files.py::build_factor_file_bytes` | `docs/references/lean-factor-file-dividend-pricing.md`; golden fixture `PythonDataService/tests/fixtures/golden/lean-factor-file-aapl/` |
| `ToolBox/FactorFileGenerator.cs` | The generator the builder originally ported; `CalculateNextDividendFactor`'s `× SplitFactor` term belongs to a split-adjusted dividend feed basis — **deliberately not ported** (#2479) | `PythonDataService/app/data_lake/factor_files.py::build_factor_file_bytes` | `docs/references/lean-factor-file-dividend-pricing.md` |
| `Common/Data/Market/Dividend.cs` | `ComputeDistribution` — how LEAN turns two factor rows back into the cash a raw-mode backtest pays: `round(ref × (1 − pf_i/pf_{i+1}), 2)` | (read-side semantics check for the fixture) | `docs/references/lean-factor-file-dividend-pricing.md` |
| `Common/Data/Auxiliary/CorporateFactorRow.cs` | `GetDividend` — pairs a row with the next-newer row and its reference price to reconstruct a dividend | (read-side semantics check for the fixture) | `docs/references/lean-factor-file-dividend-pricing.md` |
| `Common/Data/Auxiliary/CorporateFactorProvider.cs` | `GetScalingFactors` / `HasDividendEventOnNextTradingDay` — the as-of lookup the reader's `factor_multiplier_as_of` ports | `PythonDataService/app/data_lake/factor_files.py::factor_multiplier_as_of` | `docs/references/lean-factor-file-dividend-pricing.md` |

## What is *not* vendored (and why)

- The full LEAN repo (~hundreds of MB). Only files necessary to audit the ports above.
- LEAN's data subscription layer, brokerage models, algorithm framework. We do not port from those — our engine in `PythonDataService/app/engine/` is a separate (LEAN-inspired) implementation.
- LEAN's tests. They don't define the math; they only verify it.

If a future port references additional LEAN source (e.g., a new indicator, or the `Engine/` event loop semantics), vendor the additional files into this same SHA subdirectory and add rows to the table above.

## How to regenerate (audit trail)

```bash
git clone --depth 1 --filter=blob:none --sparse https://github.com/QuantConnect/Lean.git
cd Lean
git sparse-checkout set Indicators
git checkout 7986ed0aade3ae5de06121682409f05984e32ff7
# Copy the five files listed above into references/lean/7986ed0aade3ae5de06121682409f05984e32ff7/Indicators/
```

The factor-file rows (#2479) can be re-fetched directly at the pinned commit:

```bash
base=https://raw.githubusercontent.com/QuantConnect/Lean/7986ed0aade3ae5de06121682409f05984e32ff7
curl -sL "$base/Data/equity/usa/factor_files/aapl.csv"
curl -sL "$base/ToolBox/FactorFileGenerator.cs"
curl -sL "$base/Common/Data/Market/Dividend.cs"
curl -sL "$base/Common/Data/Auxiliary/CorporateFactorRow.cs"
curl -sL "$base/Common/Data/Auxiliary/CorporateFactorProvider.cs"
```

## Validation status (cross-reference)

The Python ports of these indicators were validated bit-exactly against LEAN reference output as part of `PythonDataService/app/engine/tests/test_spy_validation.py` (see project memory for the seven LEAN reproducibility traps). The pinning of *this* commit is administrative — it freezes what we have on disk to compare against. If a future LEAN change alters one of these indicators, we will vendor the new commit alongside this one, run the parity test against both, and document any divergence.

## License notice

LEAN is licensed under the Apache License 2.0. Per the license, redistribution requires a copy of the LICENSE file. The full upstream LICENSE is available at the URL above; we do not vendor the LICENSE file itself in this directory because nothing here is being redistributed as a derivative work — this is an audit-only vendored extract under fair use for verification purposes. If `references/` is ever published or forked, the LICENSE file should be vendored alongside.
