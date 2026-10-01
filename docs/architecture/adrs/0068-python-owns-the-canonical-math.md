# ADR 0068 — Python owns the canonical math; a copy elsewhere is a named, parity-tested exception

**Status:** Accepted 2026-09-30
**Provenance:** Owner rulings of 2026-09-30 on map [#2700](https://github.com/tim1016/learn-ai/issues/2700) (◆ "Python owns the math; exceptions are rare", ◆ "the canonical-math ADR is about math only", and ◆ the ruling that cuts the math registry) and on [#2745](https://github.com/tim1016/learn-ai/issues/2745) entry 5; the exceptions inventory is [#2747](https://github.com/tim1016/learn-ai/issues/2747). Written by [#2749](https://github.com/tim1016/learn-ai/issues/2749). The "Option A" reasoning comes from the 2026-04-22 computational-fidelity review (addendum §5).
**Vocabulary:** none owed — "canonical implementation", "provenance block" and "parity test" are repo-process and math terms, which `CONTEXT.md` excludes (ADR 0040).
**Related:** ADR 0069 (what a parity test must prove), ADR 0031 (Python is the authority for mathematical input/output; its decision is the transport), ADR 0036 (the flatness boundary, a numeric boundary the Frontend never holds), ADR 0022 (d) and ADR 0053 §15 (two in-Python duplicates that this ADR's duplicate rule governs).

## Context

Two rule files disagreed about where math lives. `CLAUDE.md` principle 5 said math "may live in any layer that fits the use case". `AGENTS.md` principle 5 said Python owns all math and that .NET or Angular computing math "is a bug". A separate math registry listed one row per concept and drifted from the code. The math inventory (#2747) found it wrong in four places it checked.

The 2026-04-22 review found the cost of split math. The live backtest statistics were assembled inside .NET while Python computed its own. Two `BacktestResult` shapes reached users, and the two engines could disagree with no test noticing. The review weighed two fixes. Option A put all math in Python, made .NET transport and made Angular rendering. Option B made a `Decimal` streaming engine own everything. Option B cost five to ten times as much for less than twice the benefit, so Option A won. The boundary it draws is clean and checkable: **any number shown in a number field or used in a strategy rule comes from Python.**

The inventory (#2747) then found very little math left outside Python. Of the real copies, only two have a reason to exist.

## Decision

1. **Python is the canonical layer for math.** Indicators, statistics, backtest calculations, fill and fee models, option pricing and Greeks, P&L, custody money, risk ratios and verdict scores live in `PythonDataService/`. .NET is transport and persistence: it may pass a Python number through without loss, but it may not compute a number a user will compare against another number. Angular renders: it may downsample, format and map numbers to geometry, but it may not compute signals, P&L, statistics or verdicts.
2. **There is one canonical implementation per math concept.** Its provenance block is the record: `Formula` / `Reference` / `Canonical implementation` / `Validated against`. No registry or index sits beside it, so code and the record of where the code lives cannot drift apart. A copy's provenance block names the canonical file in `Canonical implementation`, and the copy's parity test goes in `Validated against`.
3. **A copy outside Python must be a named exception.** It must appear in the table below, with a stated reason (latency or layer-locality) and a parity test that reads both sides. Adding an exception means amending this ADR. A copy not in the table is a defect, fixed by moving the computation to Python. It is never a pattern to extend. Exceptions must stay rare.
4. **A duplicate inside Python also needs a real reason and a parity test.** The real reasons are a sealed artifact that cannot be edited, latency, layer-locality or vendor parity. The duplicate's provenance block names the canonical file, and its parity test fails if the two diverge. ADR 0053 §15 (the decision clock's ET floor beside the sealed consolidator) and ADR 0022 (d) (any thin calendar adapter) are instances.
5. **This ADR covers math only.** Reusing a non-math helper, such as a URL prefix, a symbol pattern, a payload rule or a mode table, is an ordinary coding rule ("don't duplicate code"). It is not a decision recorded here.

### Named exceptions

| # | Copy | What it computes | Reason | Canonical | Parity |
|---|---|---|---|---|---|
| 1 | `Frontend/src/app/utils/black-scholes.ts`, called by `Frontend/src/app/components/strategy-builder/strategy-builder.component.ts` | Black-Scholes price, Greeks, strategy P&L and Greek curves, expiry payoff and breakevens for the Strategy Builder's live curves | **Latency.** Each leg edit re-prices a 1,200-point grid in the browser. A server round trip per edit would make the builder unusable. | `PythonDataService/app/services/bs_greeks.py` (price, Greeks); `app/services/strategy_engine.py` (`analyze_strategy`, `find_breakevens`) | **Price kernel only:** `Frontend/src/app/utils/black-scholes.parity.spec.ts`, 360 cases at `atol=1e-4, rtol=0` against `src/testing/bs-parity/grid.json`. **Owed:** Greeks, strategy P&L and Greek helpers, expiry payoff, breakevens. |
| 2 | `wholeCentLossCap` in `Frontend/src/app/components/brokers/alpaca-desk/configuration/configuration-revision-draft.ts` | Whether a loss cap is a whole-cent amount, within a 4-ULP float allowance | **Layer-locality.** Inline form feedback before submit. Python re-checks at the boundary, so the browser's answer never authorizes anything. | `PythonDataService/app/broker_configuration/envelope.py::require_whole_cent_loss_cap` | **Owed:** one shared edge-value fixture that both sides read. |

Exception 1 covers the Strategy Builder only. Pricing Lab (`Frontend/src/app/components/pricing-lab/pricing-lab.component.ts`) also calls `black-scholes.ts` and diffs against it by default. The exception does not cover that use; it is non-conformance (#2747 F6, H5). That module's own header names the wrong parity test (`test_bs_cross_engine_parity.py` compares `bs_greeks` with QuantLib and never reads the TypeScript), and its parity fixture is generated from a third inline copy rather than from `bs_greeks.py` (#2747 H2). Both are part of the owed parity work.

**.NET has no exception.** The .NET FIFO position engine (`Backend/Services/Implementation/PositionEngine.cs`) was once named as one, for layer-locality: EF/Postgres lots inside DbContext transactions. It serves only the Portfolio page, which the map rules dead (☆), and no test compares it to Python. So it goes with that page rather than being kept as an exception.

## Consequences

- A comment that justifies a duplicate cites this ADR, never a rule file.
- When a provenance block is wrong, the fix is to the block itself, because no registry exists to correct instead.
- Math that is still outside Python and not in the table is non-conformance, not an exception. This covers the dead Portfolio cascade's FIFO lots and statistics, and the hardcoded risk-free constants (`0.043` and `0.05`) in .NET and Angular. #2747 lists it, and it is tracked as issues under ADR 0039 Decision 1. It does not widen the table.
- The owed parity tests are follow-up issues from #2747. Until each lands, its exception stands on its stated reason, and the missing proof is visible in the table.

## Considered and rejected

- **Any layer, with a parity test for every duplicate** (the old `CLAUDE.md` #5). Rejected. It invites a second brain for every concept, and the parity test becomes the only thing standing between two authorities. In practice the tests were missing: the .NET FIFO engine had none.
- **Python only, with no exceptions.** Rejected for exception 1. A 1,200-point re-price per keystroke is the one place where the round trip costs more than the duplication.
- **Keeping the registry.** Rejected. It duplicated what each provenance block already says, and it drifted.
