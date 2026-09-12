# Numerical authority migration plan

**Status:** Phases 0/1/2/3/5 shipped. The only open item is **Phase 4 —
disambiguating `rule_based_backtest.py`**, deferred pending a design
decision. This doc was condensed 2026-09-12: the shipped-phase narration
and per-phase exit criteria are in git history (see the 2026-08-20 and
earlier revisions).
**Owner:** Inkant (single-developer migration)
**Started:** 2026-04-26

**Current-state authority:** `docs/math-sources-of-truth.md` (concept
level) and `docs/architecture/engine-authority-map.md` (engine level)
are the authorities for what is canonical *today*. This plan is the
sequencing record for how the migration ran, plus the open Phase 4
decision. Where this doc and those two disagree, they win.

## Shipped summary

| Phase | Scope | Shipped |
|---|---|---|
| 0 | Governance: vendored LEAN refs (`references/`), registry rows, engine-authority-map, this plan | 2026-04-26 (`e52e7c3`) |
| 1 | Options math cutover: server payload + `OptionsStrategyLabComponent` rewire; cross-engine BS parity 361/361 at `atol=1e-10` (`bs_greeks.py` ↔ `quantlib_pricer.py`); `black-scholes.ts` frozen `[LEGACY-OK — RENDER-HELPER ONLY]` with two documented intentional callers (pricing-lab comparison harness, strategy-builder live leg grid) | 2026-04-27 (`451394d`, fix `69d2bfe`) |
| 2 | Portfolio scenario / live-Greeks to Python: `/api/portfolio/scenario` + `/api/portfolio/live-greeks`; `PortfolioRiskService` switched to live-Greeks passthrough. Residual: `PortfolioValuationService` stale entry-Greek aggregation (VCR-0005, remediation Phase 9) | 2026-04-27 (`d9738a5`, `334d419`) |
| 3 | Retire `.NET BacktestService` math: `runBacktest` mutation, in-process `.NET` strategies/statistics, `Frontend/.../strategy-lab/`, and duplicate `app/services/strategies/` removed. Engine Lab is the sole interactive stock-backtest product | 2026-08-06 |
| 5 | Live-sizing authority (ADR 0009): `live_config.sizing` discriminated union resolved in `order_sizer.py`, delegating to `LeanSetHoldingsSizing`; `SimpleFloorSizing` retired from the live path | 2026-06-13 |
| — | Alpaca SQLite Clerk attributed-position and execution-economics authority (ADRs 0035/0037): sole selectable authority, activation-gated | 2026-08-19 |

Momentum RSI/Stochastic and RSI Reversal were dropped in the Phase 3
closeout rather than silently promoted — neither had reference provenance
or a canonical consumer.

## Phase 4 — Disambiguate `rule_based_backtest.py` (OPEN)

**Status: deferred since 2026-04-27.** The original "thin adapter to
`app/engine/strategy/algorithms/`" plan was abandoned after
investigation: `rule_based_backtest.py` is not a strategy implementation
but a **configurable rule engine** (composable entry conditions — EMA
crossover, RSI band, ADX filter, gap filter; multiple exit modes;
JSON-parameterized, 271 lines). The newer engine has only fixed
`Strategy` subclasses with hardcoded rules, so there is nothing to
delegate to. Reformulated options:

1. Build a `RuleBasedAlgorithm(Strategy)` subclass that takes config and
   dispatches — meaningful new code, not an adapter conversion.
2. Map specific configurations to specific fixed algorithms — fragile;
   `rule_based_backtest` has more parameters than any single fixed
   strategy.
3. Accept `rule_based_backtest.py` as a permanent alternate engine,
   document the divide, and cite parity tests against `app/engine/` for
   the configurations both can run.

Live consumers (dropping is not an option without UI consequences):
`Backend/GraphQL/Mutation.cs` `runRuleBasedBacktest`,
`PythonDataService/app/routers/jobs.py` `POST /backtest` (Redis-backed
async job), `Frontend/src/app/services/market-data.service.ts`
`runRuleBasedBacktest`; 638 lines of validation tests at
`tests/test_rule_based_backtest_validation.py`.

Once a path is chosen and shipped, update the `rule_based_backtest.py`
row in `math-sources-of-truth.md` to match.
