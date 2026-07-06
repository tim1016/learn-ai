# Numerical rigor rules

The core scientific standards for this repo. Read before any work that ports, computes, or validates mathematical logic.

This is the authority document for "strict numerical equivalence" in learn-ai.

## The core principle

**A port is not done until it's proven equivalent to its reference.** "Looks right" is not a proof. Equivalence is demonstrated by a golden-fixture test with an explicit tolerance that must be justified.

## Equivalence levels

Define up front which level a port targets. Three levels exist:

| Level | What's matched | When appropriate |
|---|---|---|
| **Bit-exact** | Byte-identical output | Integer arithmetic, exact rational arithmetic, lookup tables |
| **Strict float** | `atol=1e-9, rtol=0` | Indicators, closed-form formulas, deterministic calculations |
| **Behavioral** | Same signals at same timestamps; PnL within documented tolerance | Strategies where fill/commission/slippage models are allowed to differ |

**learn-ai default is strict float.** Bit-exact when the math supports it. Behavioral only with explicit user approval and a documented reason.

## Golden fixtures

A golden fixture is a serialized record of (input, reference output, attribution) used as ground truth.

### Required contents

- **Input data** (CSV, Parquet, or JSON — depending on size and shape)
- **Output data** produced by the reference, with full float precision
- **Attribution file** (`README.md` or `attribution.json`) containing:
  - Reference source (URL, repo + commit SHA, or paper citation)
  - Date the fixture was generated
  - Command or script used to regenerate it
  - Any parameters passed to the reference
  - Any assumptions made (timezone, bar resolution, warmup handling)

### Location

`PythonDataService/tests/fixtures/golden/<construct-name>/`

Example:
```
tests/fixtures/golden/ema-10-lean/
  input.parquet
  output.parquet
  attribution.md
```

### Lifecycle

- **Generated once** from the reference.
- **Regenerated only with justification.** A commit changing a golden fixture must include a message explaining why (e.g., "reference upgraded from commit abc123 to def456; new fixture captures a bug fix in their indicator").
- **Never hand-edited.** If the fixture is wrong, the reference is rerun.

## Tolerance rules

Every float comparison in a test must specify `atol` and `rtol` explicitly. `np.allclose(a, b)` without explicit tolerances is a bug.

### Default tolerances

- **Indicator values**: `atol=1e-9, rtol=0`
- **Accumulated PnL**: `atol=1e-6, rtol=0`
- **Options Greeks**: `atol=1e-6, rtol=1e-6` (numerical differentiation can introduce small scale-dependent error)
- **Probabilities**: `atol=1e-10, rtol=0`

### Loosening tolerances

If a test fails at the default, do NOT loosen the tolerance to make it pass. Instead:

1. Classify the divergence using the `reconcile-backtest` taxonomy.
2. Find and fix the root cause.
3. Only accept a looser tolerance if the divergence is `precision` (floating-point accumulation) and the magnitude is small relative to the meaningful range of the output.
4. Document the accepted tolerance and why in the test file and in `docs/references/<construct-name>.md`.

## Timestamp rigor

**Moved.** Timestamp representation, the two conversion boundaries, the trading-calendar authority, the display modes, and the grep-able ban list now live in their own peer authority: **`.claude/rules/temporal-rigor.md`** (decision record: `docs/architecture/adrs/0022-temporal-authority-calendar-and-timestamp.md`). Time is as authoritative there as math is here.

The one-line invariant, kept here because numerical work depends on it: **every temporal value in flight, at rest, or on the wire is `int64 ms UTC`**, and all scheduled session structure derives from the single canonical calendar module. For warmup, bar-close labeling, and alignment rules that touch numerical equivalence, see the "Classical rules" and the calendar authority in `temporal-rigor.md`.

## Warmup rigor

- **Warmup length is part of the port.** An indicator with a window of N produces `NaN` for the first N-1 bars. If the reference emits output earlier (with seeded initialization), ours matches that seeding exactly.
- **Don't silently drop warmup rows.** Tests explicitly assert the `NaN` region.
- **Warmup behavior documented in the module docstring** of every indicator: "Emits valid output starting at bar index N-1 (0-indexed), where N is the window length. First value is seeded as the first input."

## Floating-point hygiene

- **`numpy.float64`** default. If the reference uses `float32` or `decimal.Decimal`, match it.
- **Accumulation order matters.** `(a + b) + c` and `a + (b + c)` can produce different float results. When porting, preserve the reference's accumulation order even if it feels unnatural.
- **Division before multiplication** to avoid overflow is a reference-specific choice that must be preserved.
- **Use Kahan summation** (`numpy.sum` uses pairwise; explicit Kahan in `scipy`) if the reference does, not otherwise.

## Reconciliation reports

Every reconciled port produces a report in `docs/references/reconciliations/<n>.md` with:

- What was reconciled (strategy or indicator, version)
- Against which reference (commit SHA or paper ref)
- Test window (start, end, symbol, bar resolution)
- Divergence count by category (using the `reconcile-backtest` taxonomy)
- Any accepted divergences with cumulative-impact justification
- Link to the test(s) that encode the reconciliation

## Trade-level reconciliation taxonomy

When reconciling our backtest engine against a reference execution
(LEAN, a vendor backtester, QC Cloud), every disagreement between our
trade log and the reference trade log gets classified into exactly one
of eight categories. The taxonomy is encoded as the
``DivergenceCategory`` ``StrEnum`` in
``PythonDataService/app/research/parity/qc_reconciler.py`` — keep these
two in lockstep.

| Category | Meaning | Routes to |
|---|---|---|
| ``FIXTURE_INSUFFICIENT`` | The captured price-history fixture cannot explain a reference fill price within tolerance — i.e., the input data is the gap, not our engine. | Phase 3.5: re-capture the fixture (minute bars, alternate price-adjustment mode, or extended hours). Halts reconciliation; downstream classes are not evaluated until the fixture is repaired. |
| ``DECISION_MISMATCH`` | One side has a fill on a given ``(trading_date, side)``; the other doesn't. | Phase 3 engine / spec bug. Most common causes: signal-condition mis-port, indicator warmup mismatch, prediction-set coverage gap. |
| ``DIRECTION_MISMATCH`` | Same ``(trading_date)`` and quantity, opposite signs. | Phase 3 engine bug (rare; usually indicates a side-flipping bug in order construction). |
| ``QUANTITY_MISMATCH`` | Same ``(trading_date, side)``, different fill quantities. | Phase 3: examine ``SetHoldings`` rounding / cash-buffer logic. Reference's position-sizing primitive may compute target shares differently. |
| ``FILL_PRICE_DRIFT`` | Fill prices differ by more than ``fill_price_atol`` (default $0.01). | Phase 3.5 if clustered (suggests partial-fill / VWAP / auction-print differences requiring a LEAN-style ``EquityFillModel`` port). One-off occurrences: examine whether the bar's ``open`` truly explains the fill. |
| ``COMMISSION_DRIFT`` | Reference's recorded fee differs from the IBKR-tier fee computed by ``IbkrEquityCommissionModel`` by more than ``commission_atol``. **Gating only when ``assert_fees=True``** (Branch A — fee data is recorded). | Phase 3.5: re-derive the IBKR tier or wire the model into the engine if Phase 3 requires byte-exact fee parity. |
| ``PNL_DRIFT`` | Per-trade P&L differs by more than the propagated tolerance ``Σ |fill_qty_i| × $0.01 + Σ fee_atol_i``. | Almost always a downstream consequence of one of the above; root-cause the upstream divergence first. Don't widen this tolerance to silence cascaded effects. |
| ``ORDER_TYPE_MISMATCH`` | Reference's order type isn't ``MARKET`` (QC enum code 0). | Re-examine whether the reference algorithm uses limit / stop orders that our spec's ``SetHoldings`` primitive can't express. Phase 3 only supports market orders. |

**Acceptance gate.** A reconciliation report is ``passed`` iff zero
divergences fall in the **gating set**:
``{FIXTURE_INSUFFICIENT, DECISION_MISMATCH, DIRECTION_MISMATCH,
QUANTITY_MISMATCH, FILL_PRICE_DRIFT, ORDER_TYPE_MISMATCH, PNL_DRIFT}``,
plus ``COMMISSION_DRIFT`` only on Branch-A fixtures. Non-gating
divergences (``COMMISSION_DRIFT`` on Branch-B fixtures) are reported as
diagnostics, not failures.

**Loosening rule.** A tolerance may be loosened past its default only
when the relevant category has been ruled out as a root cause. The
specific rule from the Tolerances section above applies here unchanged:
if a category-classified divergence's magnitude is small relative to
the meaningful range, document the accepted tolerance and the reasoning
in the reconciliation report at ``docs/references/reconciliations/<n>.md``.

## Sovereignty

"Sovereign over the math" means:

- The port does not call out to the reference at runtime. The reference exists for one-time fixture generation, not production use.
- If the reference changes upstream, our port does not. It pins to a specific version; upgrades are deliberate, tested, and documented.
- `references/` is a vendored historical record, not a live dependency.

## Anti-patterns to reject

- `np.allclose(a, b)` with default tolerances
- "Close enough after a few bars" warmup handling
- Silent timezone conversions mid-pipeline
- Regenerating a golden fixture to make a test pass
- Loosening a tolerance to pass a test that classified as `warmup` or `timestamp`
- "My engine works, LEAN must be buggy" — no, figure out which is right *per the reference specification*, and document
- Forward-filling to align mismatched timestamp series
- ISO-string, `DateTime`, or naive-`datetime` as a wire or storage format for timestamps (see "Canonical format" above — `int64 ms UTC` is the only allowed format)
- Any of the ban-list items under "Timestamp rigor → Ban list"
