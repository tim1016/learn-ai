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

Timestamp handling is the single largest source of divergence in backtesting. Strict rules:

- **All stored timestamps are UTC, tz-aware.** Naive datetimes are bugs.
- **All logic operates in `America/New_York`.** Convert at the boundary, not ad-hoc in the middle of a pipeline.
- **Bar timestamp = bar close.** A bar labeled `09:45:00` contains trades from `09:30:00` (inclusive) to `09:45:00` (exclusive).
- **Never forward-fill or interpolate to align.** If two series have different timestamps, that's data telling you something — don't silence it.
- **Bar alignment is explicit.** A 15-min bar starts at an exchange-aligned minute (`:00`, `:15`, `:30`, `:45`). A bar that starts at `:07` is wrong.

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
