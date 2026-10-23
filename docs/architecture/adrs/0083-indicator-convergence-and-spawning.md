# ADR 0083 — Signal Programs decide after a fixed indicator-memory decay requirement

Accepted, 2026-10-22. Issue #2857 and the owner's clarifications.

Every Signal Program uses the same 0.001 initialization-memory tolerance,
sealed with each indicator's count. Readiness keeps its reference meaning.
Finite windows converge when ready; recursive indicators count the decay of
their seed, and chained indicators use a conservative cascade. This keeps the
answer independent of prices, shared by live runs and research, and avoids a
second reference indicator running beside every production indicator.

The owner explicitly chose this memory-decay rule with measured output checks
over a universal output-error promise. RSI and ADX take nonlinear ratios: a
small state error need not be a proportionally small output error, especially
near a zero denominator. MACD cancellation can likewise make relative output
error ill-conditioned. Supertrend additionally latches bands and direction.
Consequently this rule cannot guarantee identical trades across start dates
or an output error below 0.1% on every path. The pinned twin-run measurements
in `PythonDataService/tests/fixtures/golden/indicator-convergence/v1/` document
those limits, including a real-data RSI example above the output threshold.
Supertrend adds a fixed 100-bar direction-settling margin to ATR decay: at
default settings the synthetic sample had 12/125 direction mismatches at
76 bars and none at 176, while the real sample had none at either count.
This is a measured margin, not a proof for every price path or parameter.
The indicator value formulas and their LEAN readiness parity stay unchanged.

History is loaded first, using the existing canonical calendar sizing rule,
with a one-day replay floor and the feed's twenty-day cap. Complete but short
history means the program continues observing live bars in **Spawning**;
missing history or broken continuity still refuses the run. The owner chose
to permit a decision on the very bar that completes convergence. Discarded
spawn evaluations keep their own receipt reason and trace evidence. A new run
rebuilds from history; it does not inherit a previous run's spawn progress.

This applies equally to Signal Programs trading shares and option structures.
Standalone Options bots have no signal indicators; their session readiness
rule remains in force and they do not display Spawning. This follows the
owner's clarification that Spawning exists only for pending indicators.

Daily and longer bars retain the consolidator's existing data-bounded end
inside their canonical wall-clock bucket; they must not be rejected merely
for lacking a midnight-to-midnight span. Intraday bars still require the full
configured width. Existing closing-bar and late-order protections remain.

A backtest with an evaluation boundary must have converged during its run-up;
research reads the same counts. Plain backtests retain their existing trading
behavior. The session protocol and all program build proofs move together;
old seals require a new deployment, and unfinished research cannot resume
across the changed executable identity. The Data Lab lead-in remains the
separate accepted policy in ADR 0069.
