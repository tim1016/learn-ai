# ADR 0069 — Numerical rigor: a port is proven by a golden fixture and a tolerance-pinned test, with strict float as the default

**Status:** Accepted 2026-09-30
**Provenance:** Records the standing numerical rules that lived only in `.claude/rules/numerical-rigor.md` and `CLAUDE.md` principles 1–4, which the owner ruled code may no longer cite (◆ "comments cite ADRs only", map [#2700](https://github.com/tim1016/learn-ai/issues/2700), 2026-09-30). Entry 6 of [#2745](https://github.com/tim1016/learn-ai/issues/2745). The tolerance table and the accumulated-P&L ruling come from [#2746](https://github.com/tim1016/learn-ai/issues/2746) H7 and the owner ruling on #2745 (2026-09-30). The three accepted departures come from #2742 D. The map's locked rule sets the paperwork: "math port paperwork = golden fixture (with attribution) + tolerance-pinned test". Written by [#2749](https://github.com/tim1016/learn-ai/issues/2749).
**Vocabulary:** none owed — equivalence levels, tolerances and the divergence taxonomy are math and repo-process terms, which `CONTEXT.md` excludes (ADR 0040).
**Related:** ADR 0068 (one canonical implementation; what a parity test pins), ADR 0022 (time — timestamp rigor moved there in 2026-07; finite ingestion fails fast, per its 2026-09-30 amendment), ADR 0070 (the LEAN quantization floor and reconciliation-grade runs), ADR 0036 (custody numeric boundaries).

## Context

This repo's main job is to port math from references (LEAN, open-source backtesters, papers) and prove the port matches. "Looks right" is not proof. Divergence hides in warmup bars, timestamp alignment, accumulation order and the commission and fill models. It surfaces months later as an unexplained P&L gap between two engines that both "work". These rules make equivalence a test that can fail, not a claim.

## Decision

### 1. A port is done only when a golden fixture and a tolerance-pinned test prove it

Every port of math from a reference ships with:

- a **golden fixture** under `PythonDataService/tests/fixtures/golden/<name>/`, holding the input, the reference's output at full precision, and an attribution file. The attribution names the source (URL, repo and commit SHA, or paper), the generation date, the regeneration command, the parameters, and the assumptions (time zone, bar resolution, warmup);
- an **equivalence test** that loads the fixture and compares with explicit `atol` and `rtol`.

A `docs/references/` note is written only when it holds something the fixture and the provenance block cannot: an accepted divergence, a tolerance with no fixture, or a vendor fact.

A fixture is generated once from the reference and **never hand-edited**. It is regenerated only with a commit message that says why (for example, "reference upgraded from abc123 to def456"). Regenerating a fixture to make a test pass is forbidden.

### 2. Strict float is the default equivalence level

| Level | What matches | When |
|---|---|---|
| **Bit-exact** | byte-identical output | integer or exact-rational arithmetic, lookup tables — whenever the math allows it |
| **Strict float** | `atol=1e-9, rtol=0` | **the default**: indicators, closed-form formulas, deterministic calculations |
| **Behavioral** | same signals at the same timestamps; P&L within a documented tolerance | strategies whose fill, commission or slippage models may differ — **only with owner approval and a written reason** |

### 3. Every float comparison states its tolerances, and these are the defaults

`np.allclose(a, b)` without explicit `atol` and `rtol` is a bug.

| Quantity | Default | Why |
|---|---|---|
| Indicator values | `atol=1e-9, rtol=0` | Deterministic arithmetic. Anything larger hides a real formula or warmup difference. |
| Accumulated P&L | `atol=1e-6, rtol=0` | Summation over many fills accumulates float residue. **A test may always be stricter.** The FIFO fixture (`golden/broker-v2-fifo-pnl`, `tests/broker/alpaca/clerk/test_fifo_pnl.py`) pins `1e-9` as a tighter pin, not as the default. `app/services/account_pnl_reconciliation.py` uses the default. |
| Options Greeks | `atol=1e-6, rtol=1e-6` | Numerical differentiation adds a small, scale-dependent error. |
| Probabilities | `atol=1e-10, rtol=0` | Bounded in [0, 1], where absolute error is meaningful. |
| Fill price (reconciliation) | `$0.01` absolute (`Tolerances.fill_price_atol`) | Vendors publish prices in cents, so a smaller difference cannot be told apart from rounding. The custody price conflict uses the same basis (ADR 0036, 2026-09-30 amendment). |
| Commission (reconciliation) | `$0.01` absolute (`Tolerances.commission_atol`) | Cent-denominated fees. |

LEAN-ingested prices have their own floor, `atol=0.0001`, because LEAN stores deci-cents (ADR 0070).

### 4. A tolerance is loosened only after the divergence is classified as precision

When a test fails at the default, do not loosen the tolerance to make it pass. Classify the divergence (taxonomy below) and fix its root cause. A looser tolerance is accepted only when the cause is floating-point accumulation and the size is small relative to the output's meaningful range. The accepted value and its reason are written into the test and the fixture's attribution, or into the reconciliation report. A divergence classified as warmup or timestamp is never closed by loosening.

### 5. Warmup and accumulation order match the reference

- **Warmup length is part of the port.** A window of N emits `NaN` for the first N−1 bars, unless the reference seeds earlier, in which case the port matches that seeding exactly. Tests assert the `NaN` region and never silently drop it. The module docstring states where valid output starts and how the first value is seeded.
- **Precision and order follow the reference.** `numpy.float64` is the default. If the reference uses `float32` or `decimal.Decimal`, the port matches it. Keep the reference's accumulation order, since `(a + b) + c` and `a + (b + c)` differ. Keep its order of division and multiplication. Use Kahan summation only if the reference does.

### 6. Divergences are classified with one eight-category taxonomy

The taxonomy is `DivergenceCategory` in `PythonDataService/app/research/parity/qc_reconciler.py`. This section and that enum change together.

| Category | Meaning | Routes to |
|---|---|---|
| `FIXTURE_INSUFFICIENT` | The captured price history cannot explain a reference fill within tolerance: the input data is the gap, not the engine. | Re-capture the fixture (minute bars, another adjustment mode, extended hours). Reconciliation halts until repaired. |
| `DECISION_MISMATCH` | One side fills on a `(trading_date, side)` and the other doesn't. | Engine or spec bug: a signal mis-port, a warmup mismatch, a prediction-coverage gap. |
| `DIRECTION_MISMATCH` | Same date and quantity, opposite signs. | Order-construction bug. |
| `QUANTITY_MISMATCH` | Same `(trading_date, side)`, different quantity. | Sizing: `SetHoldings` rounding or cash-buffer logic. `Tolerances.qty_atol` defaults to 0, and any accepted value goes in a reconciliation report. |
| `FILL_PRICE_DRIFT` | Fill prices differ by more than `fill_price_atol`. | If clustered, a fill-model port (partial fills, VWAP, auction prints). If isolated, check whether the bar's open truly explains the fill. |
| `COMMISSION_DRIFT` | The reference fee differs from the IBKR-tier fee by more than `commission_atol`. | Re-derive the tier, or wire the fee model into the engine. |
| `PNL_DRIFT` | Per-trade P&L differs by more than `Σ|fill_qty_i| × $0.01 + Σ fee_atol_i`. | Almost always downstream of another category. Fix that one first, and never widen this tolerance to silence a cascade. |
| `ORDER_TYPE_MISMATCH` | The reference order is not a market order. | The reference uses an order type the spec's primitive cannot express. |

**Acceptance gate.** A reconciliation passes only if no divergence falls in `{FIXTURE_INSUFFICIENT, DECISION_MISMATCH, DIRECTION_MISMATCH, QUANTITY_MISMATCH, FILL_PRICE_DRIFT, ORDER_TYPE_MISMATCH, PNL_DRIFT}`. `COMMISSION_DRIFT` gates only when the fixture records fees (`assert_fees=True`, "Branch A"). Otherwise it is a reported diagnostic. A reconciled port's report lives in `docs/references/reconciliations/<n>.md`. The report states what was reconciled, against which reference version, the test window, the count of divergences in each category, and any accepted divergence with its cumulative impact.

### 7. The port is sovereign over its math

A port never calls its reference at runtime. The reference is used once, to generate the fixture. The port pins a reference version, and an upstream change does not change the port: upgrades are deliberate, tested and documented. `references/` is a vendored record for audit, not a dependency. "Our engine works, so the reference must be buggy" is never a conclusion. Decide which side is right against the reference's specification, and write the answer down.

### 8. An accepted departure is named in its module docstring, with a kept note as the receipt

When a port cannot match its reference exactly, the module docstring says so and why, and a kept `docs/references/` note holds the evidence. The standing departures:

- **Lake prices are deci-cent, rounded half-up** (`PythonDataService/app/data_lake/lean_writer.py`). LEAN's writer scales by 10,000 and does not round, but our on-disk field must be an integer. Half-up is our own quantization decision, not a LEAN behavior. Prices already on the grid, which is nearly all Polygon data, round-trip byte-identical. Receipt: `docs/references/lean-deci-cent-encoding.md`; `tests/unit/data_lake/test_deci_cent_canonical.py` proves that every writer agrees.
- **Dividend factors price Polygon's raw `cash_amount` on the raw close** (`app/data_lake/factor_files.py`), as QuantConnect's published factor files do. We do not follow LEAN ToolBox's split-adjusted input convention. Ported verbatim, that convention made a later split rescale every earlier dividend day and made raw-mode backtests pay `cash × S`. Receipt: `docs/references/lean-factor-file-dividend-pricing.md` and the golden `lean-factor-file-aapl`.
- **The Data Lab indicator warm-up is bounded** (`app/services/indicator_warmup_policy.py`, #2611). It is not run to full convergence. The error is at most about 0.01 points on a 0–100 oscillator and about `1e-4` relative on a price scale, measured against the former 1,000-bar lead-in. Receipt: `docs/references/data-lab-indicator-warmup.md`.

A new departure is added the same way: a module docstring, a kept receipt, and a line in this list.

## Consequences

- A comment that justifies a tolerance, a warmup region or a taxonomy choice cites this ADR, never a rule file.
- Time rules are not here. Representation, the calendar, and finite-ingestion fail-fast belong to ADR 0022.
- The default table is the one place a "why this number" lives. A test that deviates from it names its reason beside the number.
