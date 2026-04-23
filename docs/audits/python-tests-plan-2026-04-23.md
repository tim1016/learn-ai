# Python Data Service — Test Coverage Research Plan (2026-04-23)

**Branch:** `tests/python-data-service-coverage`
**Scope:** Add tests to every production module under `PythonDataService/app/`.
**Environment:** Tests run via `podman exec 764748efa3b1_polygon-data-service python -m pytest tests/ -v -k "not slow"`. The compose mount is `./PythonDataService/app:/app/app:z`, so tests must be `podman cp`'d into the container (or the mount extended in a follow-up PR — see risks).

## 0 — Pre-work baseline

Before the autonomous session began:

- 660 tests collected; 648 passing, 4 pre-existing failures in `tests/volatility/test_cache.py` (unrelated to coverage work — see risks §6.4), 3 skipped, 5 xpassed.
- No vendored sources in `references/`. Existing "parity" tests compare against Lean JSON fixtures in `lean-cache/` and `tests/**/fixtures/`, not freshly generated golden output.
- No `docs/references/` notes exist. Ports that claim LEAN parity are validated only by embedded fixtures inside `tests/` — the strictest definition of "golden fixture" per `.claude/rules/numerical-rigor.md` is not met anywhere in the repo today.

## 1 — Module inventory and coverage gap

Legend: **Cov** = current coverage level (none / indirect / direct-thin / direct-adequate). **Pri** = priority per `testing.md` ordering (P0 business logic & transformations → P1 services w/ branching → P2 endpoints → P3 edge cases). Ports lacking a vendored reference are marked **CRITICAL**.

### 1.1 `app/` root

| Module | Purpose | Current coverage | Pri |
|---|---|---|---|
| `app/main.py` | FastAPI app init, router registration | indirect (every endpoint test) | P3 — no unit test needed |
| `app/config.py` | Pydantic Settings from env | none | P2 — trivial, worth a smoke test |

### 1.2 `app/utils/`

| Module | Purpose | Current coverage | Pri |
|---|---|---|---|
| `app/utils/error_handlers.py` | Global 500 handler | none | P0 — tiny, easy, user-visible response shape |

### 1.3 `app/ml/`

| Module | Purpose | Current coverage | Pri |
|---|---|---|---|
| `app/ml/protocols.py` | Protocol interfaces | none | — no logic, skip |
| `app/ml/preprocessing/stationarity.py` | ADF + KPSS wrapper | none | P0 — pure math wrapper, deterministic inputs possible |

### 1.4 `app/research/documentation/`

| Module | Purpose | Current coverage | Pri |
|---|---|---|---|
| `app/research/documentation/formulas.py` | LaTeX doc registry | none | P0 — contract check; guards Angular UI from regressions in field names |

### 1.5 `app/research/signal/`

| Module | Purpose | Current coverage | Pri |
|---|---|---|---|
| `app/research/signal/standardize.py` | Train-window z-score, threshold filter | none | P0 — pure math, 2 small functions |
| `app/research/signal/regime.py` | Daily vol/trend regime labels, bar gate | none | P0 — pure math over OHLCV |
| `app/research/signal/config.py` | Pydantic config | none | P3 — 21 lines, not worth |
| `app/research/signal/diagnostics.py` | Signal diagnostics | none | P1 |
| `app/research/signal/backtest.py` | Signal backtest loop | none | P1 |
| `app/research/signal/graduation.py` | Graduation rules | none | P1 |
| `app/research/signal/walk_forward.py` | Walk-forward driver | none | P2 |
| `app/research/signal/engine.py` | Orchestrator | direct (research/test_signal_engine.py) | adequate |
| `app/research/signal/standardize.py` | see above | | |

### 1.6 `app/research/options/`

| Module | Purpose | Current coverage | Pri |
|---|---|---|---|
| `app/research/options/bs_solver.py` | Black-Scholes + IV solver | direct (tests/research/options/test_bs_solver.py) | adequate |
| `app/research/options/contract_finder.py` | ATM/OTM picker | direct (test_contract_finder.py) | adequate |
| `app/research/options/iv_builder.py` | IV table builder | direct | adequate |
| `app/research/options/diagnostics.py` | IV diagnostics | direct | adequate |

### 1.7 `app/research/validation/`

| Module | Purpose | Current coverage | Pri |
|---|---|---|---|
| `app/research/validation/ic.py` | Information coefficient | direct (test_ic.py) | adequate |
| `app/research/validation/quantile.py` | Quantile monotonicity | direct (test_quantile.py) | adequate |
| `app/research/validation/robustness.py` | Robustness suite | direct (test_robustness.py) | adequate |

### 1.8 `app/research/features/`

| Module | Purpose | Current coverage | Pri |
|---|---|---|---|
| `app/research/features/ta_features.py` | TA feature engineering | direct (test_ta_features.py) | adequate |
| `app/research/features/options_features.py` | Options feature engineering | direct (test_options_features.py) | adequate |
| `app/research/features/registry.py` | Feature registry | none | P2 — small enum-ish module |

### 1.9 `app/research/divergence/`

| Module | Purpose | Current coverage | Pri |
|---|---|---|---|
| `app/research/divergence/preflight.py` | Preflight data checks | none | P1 |
| `app/research/divergence/analysis/bar_divergence.py` | Bar-level divergence | none | P1 |
| `app/research/divergence/analysis/trade_divergence.py` | Trade-level divergence | none | P1 |
| `app/research/divergence/analysis/run_trades.py` | Trade run harness | none | P2 |
| `app/research/divergence/indicators/native.py` | Native indicator port | none | **CRITICAL** — claims to port math; no vendored ref, no golden fixture |
| `app/research/divergence/indicators/engine_adapter.py` | Engine indicator adapter | indirect | P2 |
| `app/research/divergence/ingest/align.py` | Timestamp alignment | none | P0 — timestamp-critical per numerical-rigor.md |
| `app/research/divergence/ingest/dividend_adjuster.py` | Dividend adjustments | none | **CRITICAL** — reference for expected adjustments unclear |
| `app/research/divergence/ingest/polygon_ingest.py` | Polygon data ingest | none | P1 — respx mock |
| `app/research/divergence/ingest/tv_ingest.py` | TradingView CSV ingest | none | P1 — synthetic CSV fixture |
| `app/research/divergence/strategies/*.py` | Divergence strategies | none | P2 |
| `app/research/divergence/dashboard/build_dashboard.py` | HTML dashboard builder | none | P3 — skip, rendering |
| `app/research/divergence/cli.py` | CLI entry | none | P3 — skip, CLI glue |

### 1.10 `app/research/` (top-level)

| Module | Purpose | Current coverage | Pri |
|---|---|---|---|
| `app/research/target.py` | Forward-return target | direct | adequate |
| `app/research/runner.py` | Research runner | direct (test_runner.py) | adequate |
| `app/research/options_runner.py` | Options runner | direct (test_options_runner.py) | adequate |
| `app/research/batch_runner.py` | Batch sweep | none | P1 |
| `app/research/indicator_reliability.py` | Indicator reliability | direct (test_indicator_reliability.py) | adequate |
| `app/research/config.py` | Pydantic config | none | P3 |

### 1.11 `app/engine/data/`

| Module | Purpose | Current coverage | Pri |
|---|---|---|---|
| `app/engine/data/trade_bar.py` | `TradeBar` dataclass | indirect | P0 — `period_seconds` property untested |
| `app/engine/data/lean_format.py` | LEAN CSV reader | direct (test_lean_daily_reader_parity.py) | adequate |
| `app/engine/data/polygon_export.py` | Polygon → LEAN export | none | P1 |
| `app/engine/data/availability.py` | Data availability scan | none | P1 |

### 1.12 `app/engine/execution/`

| Module | Purpose | Current coverage | Pri |
|---|---|---|---|
| `app/engine/execution/execution_config.py` | Execution config | direct (test_execution_config.py) | adequate |
| `app/engine/execution/order.py` | Order dataclasses + enums | indirect | P2 — worth a tiny smoke |
| `app/engine/execution/fill_model.py` | Market-order fill | indirect (end-to-end only) | P0 — core math for realized PnL, branching on FillMode |
| `app/engine/execution/portfolio.py` | Cash + position bookkeeping | indirect | P0 — averaging-and-flip logic is subtle and untested directly |
| `app/engine/execution/intrabar_resolver.py` | Intrabar fills | direct (test_intrabar_resolver.py) | adequate |

### 1.13 `app/engine/framework/`

| Module | Purpose | Current coverage | Pri |
|---|---|---|---|
| `app/engine/framework/insight.py` | Insight dataclass | direct (test_insight_framework.py) | adequate |
| `app/engine/framework/insight_manager.py` | Insight expiry/scoring | direct | adequate |
| `app/engine/framework/insight_scorer.py` | Default scorer | direct | adequate |

### 1.14 `app/engine/indicators/`

| Module | Purpose | Current coverage | Pri |
|---|---|---|---|
| `app/engine/indicators/sma.py` | Simple MA | direct (test_indicators.py, test_indicator_parity.py) | adequate |
| `app/engine/indicators/ema.py` | EMA | direct | adequate |
| `app/engine/indicators/rsi.py` | RSI | direct | adequate |

### 1.15 `app/engine/options/`

| Module | Purpose | Current coverage | Pri |
|---|---|---|---|
| `app/engine/options/chain_resolver.py` | Option chain resolver | none | P1 |
| `app/engine/options/pricer.py` | Black-Scholes pricer (engine-local) | none | **CRITICAL** — duplicate of `app/research/options/bs_solver.py` and `app/services/quantlib_pricer.py`; see risks §6.1 |

### 1.16 `app/engine/consolidators/`

| Module | Purpose | Current coverage | Pri |
|---|---|---|---|
| `app/engine/consolidators/trade_bar_consolidator.py` | Minute→N-minute consolidator | indirect (test_daily_sma_crossover_end_to_end.py) | P0 — boundary alignment is timestamp-critical per numerical-rigor.md, warrants a focused test |

### 1.17 `app/engine/results/`

| Module | Purpose | Current coverage | Pri |
|---|---|---|---|
| `app/engine/results/statistics.py` | Sharpe/drawdown/etc summary | direct (test_statistics.py) | adequate |

### 1.18 `app/engine/strategy/algorithms/`

| Module | Purpose | Current coverage | Pri |
|---|---|---|---|
| `app/engine/strategy/algorithms/sma_crossover.py` | SMA-X algo | direct (parity test) | adequate |
| `app/engine/strategy/algorithms/rsi_mean_reversion.py` | RSI-MR algo | direct (parity test) | adequate |
| `app/engine/strategy/algorithms/spy_ema_crossover*.py` | SPY EMA algos | direct (validation tests) | adequate |
| `app/engine/strategy/algorithms/spy_orb.py` | SPY ORB | direct (one-trade-per-day test) | adequate |

### 1.19 `app/services/`

| Module | Purpose | Current coverage | Pri |
|---|---|---|---|
| `app/services/sanitizer.py` | Data sanitization | direct (test_sanitizer.py, test_sanitize_endpoint.py) | adequate |
| `app/services/ta_service.py` | TA facade | direct (test_ta_service.py) | adequate |
| `app/services/strategy_engine.py` | Strategy engine | direct (test_strategy_engine.py) | adequate |
| `app/services/market_monitor.py` | Market monitor | direct (test_market_monitor.py) | adequate |
| `app/services/fred_service.py` | FRED client | direct (test_fred_service.py) | adequate |
| `app/services/dataset_service.py` | Dataset builder (957 lines) | direct-thin (only `filter_session`, `_tag_session_column`, `calculate_dynamic_indicators` used in tests) | P1 — 90% of module untested |
| `app/services/chart_service.py` | Chart service (1061 lines) | none | P2 — visualization serializer, low risk |
| `app/services/polygon_client.py` | Polygon client (664 lines) | none | P1 — respx mocks; critical ingestion boundary |
| `app/services/quantlib_pricer.py` | QuantLib pricer (472 lines) | none | **CRITICAL** — math port with no vendored reference |
| `app/services/rule_based_backtest.py` | Rule-based backtest | direct (test_rule_based_backtest_validation.py) | adequate |
| `app/services/data_quality_service.py` | Data-quality checks | none | P1 |
| `app/services/validation_service.py` | Validation harness | none | P2 |
| `app/services/trade_comparison.py` | Trade diff helper | none | P0 — pure data transformation |
| `app/services/strategies/common.py` | Shared strategy utils | indirect | P1 |
| `app/services/strategies/registry.py` | Strategy registry | indirect | P2 |
| `app/services/strategies/sma_crossover.py` | Legacy SMA-X | indirect | P2 |
| `app/services/strategies/rsi_mean_reversion.py` | Legacy RSI-MR | indirect | P2 |
| `app/services/strategies/rsi_reversal.py` | Legacy RSI reversal | none | P2 |
| `app/services/strategies/ema_crossover_rsi.py` | EMA-X + RSI | none | P2 |
| `app/services/strategies/momentum_rsi_stochastic.py` | Mom+RSI+Stoch | none | P2 |
| `app/services/strategies/lean_statistics.py` | Lean-style stats | none | **CRITICAL** — duplicates `app/engine/results/statistics.py`; reconciliation unclear |

### 1.20 `app/routers/`

Endpoint coverage summary — tested at least via smoke:

- `aggregates`, `engine`, `strategy`, `sanitize`, `market_monitor`, `snapshot`, `tickers` (via test_insight_framework? no — verify), `volatility`, `research_divergence` (via research suite).

No tests at all for routers:
- `chart` (P2), `dataset` (P2), `data_quality` (P2), `indicator_reliability` (P1 — domain-critical but covered via service test), `indicators` (P0 — high-use endpoint), `options` (P1), `quantlib_options` (**CRITICAL** — math), `research` (P1), `validation_study` (P2).

### 1.21 `app/models/`

Pydantic models are validated implicitly whenever an endpoint uses them. No dedicated tests. Pri = P3 — not worth unless a validator has branching logic.

## 2 — Test category per module (what kind of test)

| Module | Test kind | Justification |
|---|---|---|
| `utils/error_handlers.py` | Unit (ASGI-less) | Pure async function; instantiate Request + Exception, assert response |
| `ml/preprocessing/stationarity.py` | Unit with fixed RNG | Deterministic inputs (pure-sine, AR(1), random walk); assert `is_stationary` flag + pvalue ranges |
| `research/documentation/formulas.py` | Unit contract | Assert required keys exist on every feature/validation entry — guards the Angular contract |
| `research/signal/standardize.py` | Unit pure math | `compute_train_zscore`: mean/std held out of test mask; `apply_threshold_filter`: sign + magnitude cutoff |
| `research/signal/regime.py` | Unit pure math | Synthetic bar sequence with known vol tercile / MA slope |
| `engine/data/trade_bar.py` | Unit | `period_seconds` on a 15-min interval |
| `engine/execution/order.py` | Unit | Enum values + dataclass defaults |
| `engine/execution/fill_model.py` | Unit pure math | Both FillModes; slippage direction; commission; next_bar None branch |
| `engine/execution/portfolio.py` | Unit bookkeeping | Open/add/reduce/flip-through-zero; cash accounting; `set_holdings`; `liquidate` |
| `engine/consolidators/trade_bar_consolidator.py` | Unit timestamp-boundary | Synthetic minute bars at 09:30–09:45; assert 15-min bar timestamp = bar close (09:45), open = first, close = last |
| `services/trade_comparison.py` | Unit data transform | Construct pair of trade lists; assert diff classification |

All tests follow:
- `pytest.ini` has `asyncio_mode = auto`.
- Names: `test_<fn>_<scenario>`.
- Function-scoped fixtures.
- Explicit `atol`/`rtol` on every `np.isclose` / `pytest.approx` with a float tolerance.
- Timestamp inputs/outputs are `int64 ms UTC` per `.claude/rules/numerical-rigor.md`.

## 3 — Fixtures and shared infra

Existing `tests/conftest.py` already provides `client` (ASGITransport) and `make_sample_bars`. This session adds **no new global fixtures** — the untested modules are pure enough that local fixtures inside each test file keep things isolated.

Proposed new shared helpers (added only if a second test reuses them):

- `make_minute_bars(n, start_ts_ms, freq_ms=60_000)` — synthetic 1-minute bars with deterministic price walk, for consolidator tests. **Not added this session** (single test file — local helper).
- `make_trade(...)` — factory for engine OrderEvent objects. **Not added** (inline dataclass).

Golden fixtures: **none added in this session**. Creating a golden fixture requires a vendored reference (per numerical-rigor.md §"Lifecycle"), and none exists for any of the P0 modules on our deferred list (`quantlib_pricer`, `engine/options/pricer`, `lean_statistics`, `divergence/indicators/native`, `dividend_adjuster`). See §5.

## 4 — Execution phases

**Phase 1 — Baseline capture (small; DONE this session).** Run existing suite, catalogue passes/failures, create branch. Exit: all existing tests run green except the 4 pre-existing `test_cache` failures (see §6.4).

**Phase 2 — P0 pure-logic coverage (medium; DONE this session).** Add unit tests for modules flagged P0 above with no direct coverage:
- `utils/error_handlers.py`
- `ml/preprocessing/stationarity.py`
- `research/documentation/formulas.py`
- `research/signal/standardize.py`
- `research/signal/regime.py`
- `engine/data/trade_bar.py`
- `engine/execution/order.py`
- `engine/execution/fill_model.py`
- `engine/execution/portfolio.py`
- `engine/consolidators/trade_bar_consolidator.py`
- `services/trade_comparison.py`

Exit: every listed module has a direct test file, suite remains green, ruff clean on touched files.

**Phase 3 — P1 service coverage (medium; DEFERRED).** Add service tests for `polygon_client`, `data_quality_service`, `dataset_service` (the uncovered 90%), `batch_runner`, `divergence/ingest/polygon_ingest`, `divergence/ingest/tv_ingest`, `divergence/preflight`, `divergence/analysis/*`. Mocks via `respx`. Exit: each module has a test file and at least one branch-covering test.

**Phase 4 — P2 endpoints (small; DEFERRED).** Endpoint smoke tests for `routers/indicators`, `routers/options`, `routers/chart`, `routers/dataset`, `routers/data_quality`, `routers/research`, `routers/validation_study`. Pattern: `httpx.AsyncClient` + `ASGITransport`, respx mock for external calls. Exit: every router module has at least one endpoint test.

**Phase 5 — Port reconciliation and CRITICAL items (large; DEFERRED; blocked on user).** For each CRITICAL item in §6, vendor a reference under `references/`, write a generator script, produce a golden fixture, add the equivalence test under `PythonDataService/tests/fixtures/golden/<name>/`, add a note under `docs/references/<name>.md`. Exit: every CRITICAL module has a golden-fixture test at `atol=1e-9, rtol=0` (or a documented-and-justified looser tolerance).

## 5 — What this session actually delivered

Phase 1 ✅ and Phase 2 ✅ completed in this autonomous session. See the commit log on branch `tests/python-data-service-coverage` and the final "Status" section at the bottom of this doc for counts.

Phases 3, 4, 5 are deferred to subsequent PRs. The module inventory above is the punch list for those PRs.

## 6 — Risks and open questions (CRITICAL items flagged for user)

### 6.1 Duplicate Black-Scholes implementations — **CRITICAL — needs user input**

Three separate pricers exist:
- `app/research/options/bs_solver.py` (tested)
- `app/engine/options/pricer.py` (untested, appears to duplicate)
- `app/services/quantlib_pricer.py` (untested, uses QuantLib)

**Why:** violates "one authority for any given numerical answer" per `CLAUDE.md` §Guiding philosophy 5. Writing tests against the two untested copies would entrench the duplication.

**How to apply:** user must decide which pricer is canonical before the other two get tests. The tests themselves are cheap; the decision is architectural. Deferred — not implemented this session.

### 6.2 Duplicate statistics — **CRITICAL — needs user input**

`app/engine/results/statistics.py` has `test_statistics.py`; `app/services/strategies/lean_statistics.py` has none. Both compute Sharpe/drawdown/etc. Same reasoning as §6.1 — pick a canonical implementation before writing tests for the duplicate.

### 6.3 Ports without vendored references — **CRITICAL — needs user input**

Per `.claude/rules/numerical-rigor.md` every port "ships with (a) a golden fixture, (b) a `docs/references/` note, (c) a tolerance". The following claim to be ports and have **none of (a), (b), (c)**:

- `app/services/quantlib_pricer.py` (reference: QuantLib)
- `app/engine/options/pricer.py` (reference: ambiguous — LEAN? textbook?)
- `app/services/strategies/lean_statistics.py` (reference: LEAN)
- `app/research/divergence/indicators/native.py` (reference: ambiguous)
- `app/research/divergence/ingest/dividend_adjuster.py` (reference: unclear — CRSP? Polygon? Yahoo?)

User must either vendor each reference under `references/` with a commit SHA, or declare the module "original code, not a port" and document that in the module docstring. Tolerances (`atol`/`rtol`) for each port must also be agreed — defaults per numerical-rigor.md are strict-float (`atol=1e-9, rtol=0`) but QuantLib numerical integration may require `1e-6` with a paper-level justification. Skipped this session; flagged here for PR follow-up.

### 6.4 Pre-existing test_cache failures

Four failures in `tests/volatility/test_cache.py` pre-date this branch. Likely container-cache path issue. **Not touched** — out of scope for "add tests" work, belongs in a separate fix PR.

### 6.5 Test-mount gap in `compose.yaml`

`compose.yaml` mounts only `./PythonDataService/app:/app/app:z`. The `tests/` directory is not mounted, so `podman exec polygon-data-service python -m pytest tests/` as documented in `PythonDataService/CLAUDE.md` **fails** on a fresh container. This session worked around it with `podman cp`. A follow-up PR should add `./PythonDataService/tests:/app/tests:z` to the compose file. Not changed this session to keep the diff test-only.

### 6.6 Container is missing dev dependencies

The image installs only `requirements-heavy.txt` + `requirements-light.txt` — no `pytest`, `pytest-asyncio`, `respx`. Running the documented test command from a clean image fails at import. Options: (a) extend the Dockerfile to install `requirements-dev.txt`, (b) add a `Dockerfile.test` stage, (c) always `pip install pytest` at test time. This session chose (c) as a workaround; a durable fix belongs in a separate PR.

### 6.7 Timestamp policy enforcement

Several modules that work with timestamps (`research/signal/regime.py` uses `pd.to_datetime(..., unit="ms")` — good), but `app/engine/framework/insight.py` uses `datetime.utcnow()` (banned per numerical-rigor.md — see deprecation warning in test output). **Not a test gap** — it's a production-code violation. Flagged here so the user sees it, but fixing it belongs in a timestamp-policy PR, not this one.

## 7 — Explicit non-goals

- No .NET or Angular tests.
- No changes to production code under `app/` (except docstring typo fixes that happen to be in a touched file — none encountered).
- No rewriting of existing adequate tests.
- No new dependencies added.
- No golden-fixture generation — all CRITICAL items are parked for the user.
- No compose/Dockerfile changes (even though §6.5 and §6.6 are real issues, they belong in a dev-infra PR).
- No fix for the 4 pre-existing `test_cache` failures.

## 8 — What's left / next steps

Ordered so the user can pick up and execute each in isolation:

1. **Compose + Dockerfile fixes (§6.5, §6.6).** Mount tests into the container; install dev deps in the image. Small, unblocks the documented test command.
2. **Phase 3 — P1 service tests.** Start with `services/polygon_client.py` (respx mocks over `api.polygon.io`) because every ingestion boundary depends on it. Then `data_quality_service`, `dataset_service` gaps, `batch_runner`, divergence ingest modules.
3. **Phase 4 — P2 router smoke tests.** Each new router test file is ~30 lines; 7 files total.
4. **Phase 5 — CRITICAL port reconciliation (§6.1, §6.2, §6.3).** Biggest chunk — requires user direction on which Black-Scholes / statistics implementation is canonical, then vendor-and-port each.
5. **Timestamp-policy cleanup (§6.7).** Replace `datetime.utcnow()` usages, add a grep-based CI check against the ban list in numerical-rigor.md.
6. **Pre-existing test_cache fix (§6.4).** Triage and fix the 4 volatility cache failures.

## 9 — Final status (updated after implementation)

See the PR description for the exact list of test files added, test counts delivered, and any deltas to this plan discovered during implementation.
