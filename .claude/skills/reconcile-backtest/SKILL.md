---
name: reconcile-backtest
description: Diagnose why two backtest runs diverge. Use when user says "backtest doesn't match", "LEAN gives different results", "reconcile the engines", "my port produces different trades", "numbers don't agree between", or presents two sets of backtest outputs and wants them reconciled.
---

# Reconcile Backtest

Given two backtest outputs (typically our engine vs a reference like LEAN or a prior Python experiment), produce a trade-by-trade diff, classify the divergence sources using a fixed taxonomy, and propose targeted fixes. This is the skill for "my engine says X, LEAN says Y" workflows.

## When to use

- User reports backtests don't match between implementations
- User has two output files (trades, equity curves, signals) they want compared
- A `port-indicator` port passes its indicator-level test but produces different trades at the strategy level

## When NOT to use

- Initial porting with no reference run yet — use `port-indicator` first
- Exploratory strategy research where there's no ground truth — there's nothing to reconcile against

## The divergence taxonomy

Every divergence must be classified into one of these buckets before a fix is proposed. Do not invent new categories; if something doesn't fit, discuss with the user before proceeding.

| Category | What it means | Typical fix location |
|---|---|---|
| `timestamp` | Bar alignment, clock zone, or bar-close convention mismatch | Bar aggregator / time handling |
| `warmup` | Indicator initialization or seeding differs; one engine produces signal N bars earlier | Indicator `__init__` or warmup logic |
| `fill` | Order fill model differs (market-on-close vs next-open vs midpoint) | Fill simulator |
| `commission` | Commission or fee model differs | Commission calculator |
| `slippage` | Slippage model differs or is missing in one engine | Slippage model |
| `precision` | Floating-point accumulation differs (rare, usually small) | Usually acceptable with documented `rtol` |
| `off-by-one` | Window boundary or index slip; signal produced one bar off | Indicator window logic |
| `data-quality` | Input data itself differs (splits, dividends, bad ticks, gaps) | Data pipeline, not the engine |
| `strategy-logic` | The strategy rules themselves are implemented differently | Strategy module |
| `sizing` | Position sizing or capital allocation differs | Portfolio/risk module |

## Execution

### PHASE 1: Align the inputs

Before comparing outputs, prove the inputs are identical.

1. **Compare the input bars bar-by-bar.** Load both engines' view of the underlying data for the backtest window. Assert they agree on OHLCV per bar, timestamp-for-timestamp. If they don't, classify as `data-quality` and stop — there's nothing to reconcile at the strategy level until data agrees.
2. **Document the comparison window** explicitly: start timestamp, end timestamp, timezone, symbol, bar resolution.
3. **Check for corporate action handling.** Splits and dividends applied differently will produce seemingly random trade divergences. If the window spans a corporate action, flag it.

### PHASE 2: Compare signals before trades

Trades are downstream of signals. Compare signals first; if signals agree, divergence is in fill/commission/sizing. If signals disagree, divergence is in indicators or strategy logic.

1. **Extract the signal series** from both engines (the boolean or categorical output of the strategy, per bar, before order placement).
2. **Align the series** on timestamp index. Use exact timestamp matching — do not forward-fill or interpolate.
3. **Compute a diff table** of every bar where signals disagree. Include: timestamp, both engines' signal values, relevant indicator values from both engines at that bar.
4. **First disagreement wins attention.** Focus on the earliest divergence in the series. A single root-cause divergence often cascades into dozens of downstream disagreements. Fix the first one, re-run, re-compare.

### PHASE 3: Classify the divergence

For the first disagreement:

1. **Check indicator values at that bar.** If indicators disagree, it's either `warmup`, `off-by-one`, or `timestamp`. Inspect the bar index: is one engine one bar ahead? Are both engines at the same wall-clock time but different internal state?
2. **Check the bar that produced the signal.** If signals disagree but indicators at the signal bar agree, the divergence is `strategy-logic` — the rules interpreting the indicators differ.
3. **If signals agree but trades differ**, it's `fill`, `commission`, `slippage`, or `sizing`. Check the fill timestamp and fill price of the first divergent trade.

### PHASE 4: Propose and apply the fix

1. **State the classification explicitly** to the user: "First divergence at 2024-03-14 09:45 EST is `warmup` — our EMA(10) emits its first value at bar 9 (0-indexed), LEAN emits at bar 10."
2. **Propose the fix** with the specific code location and change.
3. **Before applying, predict the outcome.** "Applying this fix should align the first N bars; downstream bars may still diverge if there are other root causes."
4. **Apply, re-run, re-compare.** Show the new divergence count and the new first-divergence timestamp. Iterate.

### PHASE 5: Document accepted divergences

Not all divergences get fixed. Some are acceptable — e.g., LEAN uses integer commission rounding that we've consciously chosen not to mirror. When accepting a divergence:

1. **Add an entry** to `docs/references/reconciliations/<strategy-name>.md` describing: the divergence, its classification, why it's accepted, the cumulative impact on PnL over the test window.
2. **Encode the tolerance in the reconciliation test** so future regressions fail loudly.

## Output

After a reconciliation session, report:

- Classification of every divergence found (with counts per category)
- First-divergence timestamp and its classification
- Fixes applied (with file + line references)
- Remaining divergences and whether they're being accepted or deferred
- New reconciliation test added, if applicable

## Anti-patterns to avoid

- Comparing equity curves first — always compare signals first, then trades, then PnL
- Eyeballing divergence magnitudes instead of counting bars
- Tolerating `timestamp` or `warmup` divergences because "they wash out" — they don't, they compound
- Writing a custom classifier for a divergence that doesn't fit the taxonomy without asking
- Fixing divergences in bulk. Fix first, re-run, re-compare. Always.
