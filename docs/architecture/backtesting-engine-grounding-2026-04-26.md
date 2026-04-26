# Backtesting and Signal Research Engine Grounding

**Date:** 2026-04-26
**Scope:** stocks + options backtesting, signal research, numerical validation, code quality
**Calibration:** localhost research project, one-developer migration path, math rigor first
**Status (read first):** **This memo is a pre-Phase-0 snapshot** — it describes
repo state on 2026-04-26 *before* the numerical-authority migration started.
It is preserved here as the audit that motivated the migration plan, not as
current state. By the time you read this, much of the "current state"
described below has been changed:

- Phase 0 shipped: `references/` is no longer empty (vendored LEAN snapshots), `engine-authority-map.md` exists, `numerical-authority-migration-plan.md` sequences the work.
- Phases 1.1, 1.2, 1.4 shipped: `OptionsStrategyLabComponent` no longer computes BS in TypeScript; cross-engine BS parity test pins `bs_greeks.py` ↔ `quantlib_pricer.py` at `atol=1e-10`.
- Phase 2 shipped: `/api/portfolio/scenario` and `/api/portfolio/live-greeks` are canonical for portfolio scenario / live-Greeks math; `.NET` services are passthroughs.
- Phase 1.3, 2.3 shipped: `black-scholes.ts` formally `legacy-ok` for two intentional UI callers; `ComputeDollarDeltaAsync` + `ComputePortfolioVegaAsync` now use live Python Greeks.
- Phases 3, 4 deferred — see `numerical-authority-migration-plan.md` § Status as of 2026-04-27 for reasoning.

For current state, the authoritative sources are `engine-authority-map.md`,
`math-sources-of-truth.md`, and the migration plan's status section. Do not
treat this memo as an up-to-date description of any subsystem.

## Executive summary

The core diagnosis from the first pass was directionally right: learn-ai still
has split numerical authority across Python, .NET, and Angular. But the
priority framing was too production-heavy for the repo's actual operating
model.

This project is not a multi-environment trading platform under active
production load. It is a localhost research system whose strongest asset is its
validation culture: parity tests, reference notes, LEAN-oriented engine work,
and unusually explicit rules about numerical ownership.

That changes the order of work.

The most important next moves are not "harden everything like production."
They are:

1. freeze the missing reference provenance in `references/`
2. write down the engine authority map in one short doc
3. finish the already-declared migration of options math out of TypeScript
4. move portfolio scenario / live-Greeks math to Python
5. retire the legacy `.NET` backtest path as Strategy Lab finishes yielding to
   Engine Lab

Those five items fit the repo as it exists today. They strengthen scientific
reproducibility without dragging in operational ceremony that does not yet pay
for itself.

## What the repo already says

The destination is less ambiguous than a quick code scan suggests.

- `AGENTS.md` says Python owns all math and `.NET` / Angular are transport and
  visualization only.
- [options-math-authorities.md](C:/Users/inkan/learn-ai/docs/architecture/options-math-authorities.md:17)
  says the options stack already has named canonical modules and explicitly
  says "no math in C# or TypeScript."
- [engine-phase-1-2-refined-plan.md](C:/Users/inkan/learn-ai/docs/engine-phase-1-2-refined-plan.md:4)
  says Engine Lab becomes the single home for backtesting and Strategy Lab is
  deprecated.
- [strategy-lab.component.html](C:/Users/inkan/learn-ai/Frontend/src/app/components/strategy-lab/strategy-lab.component.html:1)
  repeats that deprecation in the live UI.
- [math-sources-of-truth.md](C:/Users/inkan/learn-ai/docs/math-sources-of-truth.md:1)
  already tracks many rule-5 violations and pending migrations in a more
  precise way than a generic architecture memo.

So the main problem is not "the repo has no declared direction."
The problem is that the declared direction is spread across several docs and
the remaining violations have not yet been sequenced into one migration arc.

## Current state, grounded in code

### What is genuinely strong

- Engine Lab already talks directly to the Python LEAN-oriented engine:
  [lean-engine.component.ts](C:/Users/inkan/learn-ai/Frontend/src/app/components/lean-engine/lean-engine.component.ts:43),
  [engine.py](C:/Users/inkan/learn-ai/PythonDataService/app/routers/engine.py:1).
- Strategy Lab is not pretending to be the future:
  [strategy-lab.component.html](C:/Users/inkan/learn-ai/Frontend/src/app/components/strategy-lab/strategy-lab.component.html:1).
- The repo has a real provenance and validation mindset:
  [options-bs-greeks-2026-04-24.md](C:/Users/inkan/learn-ai/docs/references/options-bs-greeks-2026-04-24.md:1),
  [test_indicator_parity.py](C:/Users/inkan/learn-ai/PythonDataService/tests/test_indicator_parity.py:1),
  [test_sma_crossover_parity.py](C:/Users/inkan/learn-ai/PythonDataService/app/engine/tests/test_sma_crossover_parity.py:1).
- Signal-research diagnostics are materially stronger than most hobby
  backtesters:
  [ic.py](C:/Users/inkan/learn-ai/PythonDataService/app/research/validation/ic.py:1),
  [robustness.py](C:/Users/inkan/learn-ai/PythonDataService/app/research/validation/robustness.py:1).

### What is transitional, not mysterious

- The new interactive backtest path is Python Engine Lab:
  [lean-engine.component.ts](C:/Users/inkan/learn-ai/Frontend/src/app/components/lean-engine/lean-engine.component.ts:43),
  [engine.py](C:/Users/inkan/learn-ai/PythonDataService/app/routers/engine.py:1308).
- The old interactive backtest path is still `.NET` GraphQL + `BacktestService`:
  [Mutation.cs](C:/Users/inkan/learn-ai/Backend/GraphQL/Mutation.cs:98),
  [BacktestService.cs](C:/Users/inkan/learn-ai/Backend/Services/Implementation/BacktestService.cs:9).
- The options UI already admits its client-side BS engine is legacy:
  [black-scholes.ts](C:/Users/inkan/learn-ai/Frontend/src/app/utils/black-scholes.ts:1).
- Strategy Lab and TS Black-Scholes are therefore not hidden surprises.
  They are known migration leftovers.

### Where the real gaps still are

#### 1. `references/` is still empty

This is the cheapest high-value fix.

Top-level `references/` still contains only `.gitkeep`, even though the repo's
authority hierarchy treats it as rank 1. That is different from
`docs/references/`, which now contains useful provenance notes. The gap is not
"no notes exist." The gap is "frozen source snapshots are not on disk."

Why it matters here:

- parity claims become harder to reproduce later
- upstream LEAN changes can erase the exact code a fixture was derived from
- future audits have to trust prose instead of pinned source

#### 2. Backtest authority is split between a real future path and a live legacy path

`.NET` still runs strategies and computes user-facing metrics in-process:

- [Mutation.cs](C:/Users/inkan/learn-ai/Backend/GraphQL/Mutation.cs:98)
- [BacktestService.cs](C:/Users/inkan/learn-ai/Backend/Services/Implementation/BacktestService.cs:31)

At the same time, Engine Lab already uses the Python engine directly:

- [lean-engine.component.ts](C:/Users/inkan/learn-ai/Frontend/src/app/components/lean-engine/lean-engine.component.ts:153)
- [engine.py](C:/Users/inkan/learn-ai/PythonDataService/app/routers/engine.py:1308)

This is not a conceptual dispute. The repo has already chosen the winner.
The remaining work is migration and deletion.

#### 3. Options math is partly migrated, not fully migrated

There is already a Python strategy-analysis path:

- [strategy_engine.py](C:/Users/inkan/learn-ai/PythonDataService/app/services/strategy_engine.py:1)
- [AnalyzeOptionsStrategy](C:/Users/inkan/learn-ai/Backend/GraphQL/Query.cs:830)

That path computes:

- payoff curve at expiry
- POP
- expected value
- aggregate Greeks

But `OptionsStrategyLabComponent` still computes current-time PnL curves,
what-if curves, Greek curves, POP, and diagnostic tables in TypeScript:

- [options-strategy-lab.component.ts](C:/Users/inkan/learn-ai/Frontend/src/app/components/options-strategy-lab/options-strategy-lab.component.ts:372)
- [options-strategy-lab.component.ts](C:/Users/inkan/learn-ai/Frontend/src/app/components/options-strategy-lab/options-strategy-lab.component.ts:438)
- [options-strategy-lab.component.ts](C:/Users/inkan/learn-ai/Frontend/src/app/components/options-strategy-lab/options-strategy-lab.component.ts:527)

This is important because the right fix is smaller than "build a whole new
options engine." The repo already has the server-side foothold. The likely next
move is to extend the existing Python analysis payload, not invent a parallel
service stack.

#### 4. Portfolio risk is the most substantive remaining options-math issue

Current portfolio risk/scenario logic uses stored entry Greeks and shock
adjustments in `.NET`:

- [PortfolioValuationService.cs](C:/Users/inkan/learn-ai/Backend/Services/Implementation/PortfolioValuationService.cs:86)
- [PortfolioRiskService.cs](C:/Users/inkan/learn-ai/Backend/Services/Implementation/PortfolioRiskService.cs:57)
- [PortfolioRiskService.cs](C:/Users/inkan/learn-ai/Backend/Services/Implementation/PortfolioRiskService.cs:181)

For a summary card this is tolerable. For a researcher relying on scenario
math, it is not.

Once spot, time, or IV moves materially, entry Greeks stop being an acceptable
proxy. This is the one recommendation from the first pass that should stay near
the top even after recalibration.

#### 5. `rule_based_backtest.py` is still ambiguous in role

There are at least three Python backtest-related paths with different jobs:

- event-driven LEAN-style execution:
  [engine.py](C:/Users/inkan/learn-ai/PythonDataService/app/engine/engine.py:1)
- signal-research threshold backtests:
  [backtest.py](C:/Users/inkan/learn-ai/PythonDataService/app/research/signal/backtest.py:1)
- configurable rule-based strategy runner:
  [rule_based_backtest.py](C:/Users/inkan/learn-ai/PythonDataService/app/services/rule_based_backtest.py:1)

That is not automatically bad. But only the first two already have a clean,
named role in the docs. `rule_based_backtest.py` needs an explicit statement:
adapter to be folded in, validation-only utility, or supported secondary
engine. Without that statement, it will keep generating authority confusion.

## Comparison to open-source peers

The comparison is most useful when it answers "what should learn-ai borrow?"
rather than "which project is best?"

| Project | Useful lesson from official docs | Where learn-ai is already differentiated |
|---|---|---|
| [QuantConnect LEAN](https://www.quantconnect.com/docs/v2/lean-engine/getting-started) | One coherent engine contract across backtests, statistics, and multi-asset semantics | learn-ai is more explicit about provenance, tolerances, and reconciliation culture |
| [vectorbt](https://vectorbt.dev/) | Parameter-sweep ergonomics and fast research iteration | learn-ai is more serious about event semantics and reference-port validation |
| [Backtrader](https://www.backtrader.com/docu/strategy/) | Simple mental model: one obvious Python path for user strategies | learn-ai's research diagnostics and options ambition are stronger |
| [NautilusTrader](https://nautilustrader.io/docs/latest/concepts/overview/) | Strong separation of execution semantics, research, and live-trading architecture | learn-ai has a stronger "port math with receipts" identity |
| [Zipline](https://zipline.ml4trading.io/) | Coherent notebook-friendly backtest workflow | learn-ai is more advanced on cross-source validation and options math depth |

The most important takeaway is not feature envy. It is identity:

learn-ai should compete on "scientific trading-logic porting with reproducible
equivalence," not on trying to out-productionize LEAN or out-vectorize
vectorbt in the short term.

## Recommended sequence for this repo

This is the sequence that best matches the repo's own direction and the amount
of work already completed.

### Step 1. Vendor the real references

Priority: `Now`

Do this first because it is cheap and it removes the highest-leverage
reproducibility gap.

Suggested shape:

- `references/lean/<commit-sha>/...`
- `references/options/<source>/<version-or-sha>/...`
- one short `README.md` or attribution note per vendored subtree

Pair each vendored snapshot with the existing `docs/references/*` note instead
of replacing those notes.

### Step 2. Freeze the engine authority matrix

Priority: `Now`

Do not rely on memory alone now that the repo already has:

- Engine Lab
- deprecated Strategy Lab
- research backtests
- rule-based backtest helpers
- options analysis endpoints

The right artifact is short. It does not need to explain every module; it only
needs to answer:

- what path owns interactive backtests
- what path owns research scoring
- what path owns options analysis
- what is legacy
- what is validation-only

This addendum ships that doc as
[engine-authority-map.md](engine-authority-map.md).

### Step 3. Finish the options-math cutover using the existing server foothold

Priority: `Next`

Do not treat this as a greenfield rewrite. The repo already has:

- Python options authorities in `bs_greeks.py`, `quantlib_pricer.py`,
  `volatility/solver.py`
- a Python strategy-analysis service in `strategy_engine.py`
- GraphQL passthrough for that analysis

The pragmatic next move is to extend the existing server response so
`OptionsStrategyLabComponent` stops computing:

- current-time curve packs
- what-if curve packs
- Greek curves
- diagnostic rows

Angular should be left with:

- leg editing
- request state
- chart/table rendering

This is a targeted follow-on, not a standalone sprint.

### Step 4. Move portfolio scenario and live-Greeks math to Python

Priority: `Next`

This is the most technically substantive numerical gap still affecting options
research quality.

Target outcome:

- Python endpoint recomputes Greeks per scenario point
- pricing uses current spot, time, IV, and contract metadata
- `.NET` aggregates and persists, but does not synthesize theoretical option
  value from entry Greeks

This can reuse the existing options authorities rather than introducing new
math.

### Step 5. Retire `.NET BacktestService` as Strategy Lab winds down

Priority: `Next`

By this point the repo will already have:

- Engine Lab as the preferred UI
- Strategy Lab openly marked deprecated
- a written authority matrix

Then the removal becomes operationally simple:

- point any remaining backtest consumers at `/api/engine/backtest`
- keep the old route only as temporary transport if needed
- delete the in-process strategy implementations and local Sharpe /
  drawdown assembly once unused

This is a migration closeout, not a discovery exercise.

## What is not urgent yet

These items are not wrong in principle. They are simply not at the top of the
stack for this repo today.

### EF migrations and startup logging

`EnsureCreated()` and `Console.WriteLine` in
[Program.cs](C:/Users/inkan/learn-ai/Backend/Program.cs:105)
are not ideal long-term. But in a localhost containerized research setup they
are not the thing blocking numerical trust.

Revisit them when one of these becomes true:

- more than one environment matters
- schema history needs to survive repeated rebuilds
- rollback / migration ordering becomes part of normal work

Until then, this is infrastructure hygiene, not a research-engine blocker.

### Warning tracking as a formal validation program

The Pydantic and FastAPI warnings seen in the targeted Python test run are
worth cleaning up, but they are toolchain maintenance, not scientific
non-determinism.

Fix them opportunistically when those files are touched. No separate
"validation debt tracking" mechanism is needed for them.

### Large Angular component decomposition as a standalone goal

`options-strategy-lab.component.ts` is large, but in this case the size is
mostly a symptom of client-side domain math. Once the math moves server-side,
the component shrinks naturally.

So "thin the component" should be treated as a consequence of Step 3, not as a
parallel workstream.

### No-arbitrage invariant suites before authority consolidation

Put-call parity bands, monotonicity, convexity, and calendar checks are good
tests for the options stack. They will pay off more after there is one clear
authority for the relevant calculation.

Consolidate first, then broaden the invariant suite.

## Code quality suggestions that fit this repo

1. Use `docs/math-sources-of-truth.md` as the live registry and keep it honest.
   It already names many violations more precisely than broader architecture
   docs.
2. Prefer extending existing Python payloads over adding new cross-layer
   endpoints. The options stack already has server-side analysis hooks worth
   building on.
3. Keep duplicate numerical paths only when they are explicitly labeled
   `legacy` or `validation-only`, and make them cite the canonical path.
4. Treat vendored source snapshots and reference notes as a pair:
   `references/` for frozen code, `docs/references/` for the explanation.
5. Keep production-grade platform asks out of the critical path unless they
   improve reproducibility or remove a numerical authority split.

## Bottom line

learn-ai is closer to a coherent research engine than the first pass implied.
The repo has already chosen:

- Python as the numerical authority
- Engine Lab as the backtesting future
- Strategy Lab as deprecated
- TypeScript Black-Scholes as legacy

What remains is to finish the migration in the order that best supports a
localhost research workflow:

1. freeze references
2. freeze authority
3. finish options cutover
4. fix scenario Greeks
5. delete legacy backtest math

That is a focused 2-3 week migration path, not a fire drill.

## External grounding sources

- [QuantConnect LEAN Engine docs](https://www.quantconnect.com/docs/v2/lean-engine/getting-started)
- [QuantConnect Index Options docs](https://www.quantconnect.com/docs/v2/writing-algorithms/universes/index-options)
- [VectorBT docs](https://vectorbt.dev/)
- [Backtrader strategy docs](https://www.backtrader.com/docu/strategy/)
- [NautilusTrader overview](https://nautilustrader.io/docs/latest/concepts/overview/)
- [NautilusTrader backtesting concepts](https://nautilustrader.io/docs/latest/concepts/backtesting)
- [NautilusTrader options concepts](https://nautilustrader.io/docs/latest/concepts/options/)
- [Zipline 3.0 docs](https://zipline.ml4trading.io/)
