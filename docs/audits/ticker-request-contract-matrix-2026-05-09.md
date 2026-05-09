# TickerRequest contract matrix — confirmed audit (2026-05-09)

PR (ii)'s Task 0 deliverable. Each candidate router/model from the spec was audited against the live tree on master. The completed matrix below replaces the `_confirm_` cells in `docs/superpowers/specs/2026-05-09-ticker-range-picker-everywhere-design.md` § "Contract matrix" (in PR #202).

## Models that DO inherit (PR ii migration list)

| Model (file:line) | Symbol field | Dates | Multiplier | Timespan | Session | Override needed |
|---|---|---|---|---|---|---|
| `DataQualityRequest` (data_quality.py:25) | `ticker` | `from_date`/`to_date` | n/a | n/a | n/a | none — base defaults are unused |
| `IndicatorReliabilityRequest` (models/indicator_reliability_models.py:28) | `ticker` | **`start_date`/`end_date`** | 1 | "minute" | n/a | none — `AliasChoices` accepts `start_date`/`end_date` |
| `IndicatorTableRequest` (models/requests.py:192) | `ticker` | `from_date`/`to_date` | 1 | "minute" | **"extended"** | **override session="extended"** |
| `DatasetGenerationRequest` (models/requests.py:287) | `ticker` | `from_date`/`to_date` | 1 | "minute" | **"extended"** | **override session="extended"** |
| `RuleBasedBacktestJobRequest` (jobs.py:79) | `ticker` | `from_date`/`to_date` | **15** | "minute" | n/a | **override multiplier=15** |
| `FeatureResearchJobRequest` (jobs.py:136) | `ticker` | `from_date`/`to_date` | 1 | "minute" | n/a | none — defaults match base |
| `SignalEngineJobRequest` (jobs.py:154) | `ticker` | `from_date`/`to_date` | **15** | "minute" | n/a | **override multiplier=15** |
| `CrossSectionalJobRequest` (jobs.py:117) | **`tickers: list[str]`** | `from_date`/`to_date` | n/a | n/a | n/a | inherit `MultiTickerRequest` |
| `EngineBacktestRequest` (engine.py:1165) | (strategy-owned) | **`start_date`/`end_date`** (Optional) | n/a | n/a | n/a | inherit `_BarRange` only; dates stay Optional via override |

## Models EXCLUDED post-audit (revising spec)

The spec listed these as candidates; the audit reveals they don't actually match the canonical `(symbol, from_date, to_date, timespan, multiplier)` shape. Forcing them into the base would either lose information (broader vocab) or fail Pydantic typing.

| Model (file:line) | Why excluded |
|---|---|
| `AggregateRequest` (models/requests.py:8) | `timespan` validator allows `["minute","hour","day","week","month","quarter","year"]` — broader than the base's `Literal["minute","hour","day"]`. Cannot widen via inheritance in Pydantic v2. Plus `limit`/`adjusted` add Polygon-specific fields. Stays as-is. |
| `IndicatorRequest` (models/requests.py:36) | Different shape — `indicator_type`/`window`/`timestamp`, no date range. |
| `CalculateIndicatorsRequest` (models/requests.py:166) | Takes pre-fetched `bars: list[OhlcvBar]` instead of dates — different shape. |
| All `volatility.py` endpoints | Use path/query params directly (`ticker: str` as function arg). No BaseModel request bodies to inherit. |
| `ChartDataRequest` / `AllowedTimeframesRequest` (chart.py:33,53) | Single `timeframe: str` ("1m"\|"5m"\|...\|"1D"), not `timespan + multiplier`. |
| `_PreflightRequestBody` (research_divergence.py:113) | Uses `timeframe: Literal["5m","15m","1h"]` — distinct shape. |
| `SpecBacktestRequest` (spec_strategy.py:53) | `StrategySpec.symbols: list[str]` is plural and load-bearing inside the domain spec. Deferred to own design (spec § "Out of scope"). |

## Notable behavior-preservation requirements

Three places where the base's defaults silently differ from the route's pre-migration default; explicit overrides on the inheriting class preserve current behavior:

- `IndicatorTableRequest` and `DatasetGenerationRequest`: `session="extended"` (base default `"rth"`)
- `RuleBasedBacktestJobRequest` and `SignalEngineJobRequest`: `multiplier=15` (base default `1`)

Each override carries an inline comment naming the pre-migration default it preserves; each gets a regression test in the corresponding test file.

## Volatility audit notes

`volatility.py` has 16 endpoints — most take `ticker: str` as a path/query param and use a mix of helpers (`get_volatility_series`, `compute_atm_iv`, etc.) directly. Only a handful have request *response* models (e.g. `VolatilitySeriesResponse`); none have `Request(BaseModel)` request bodies that match the canonical shape. Removed from inheritor list entirely.

## What this means for the plan

- Plan Task 5 (`indicators.py`) becomes "migrate `IndicatorTableRequest` (in models/requests.py)" rather than touching indicators.py directly.
- Plan Task 7 (volatility) is removed.
- Plan Task 8 (dataset) becomes "migrate `DatasetGenerationRequest` (in models/requests.py)".
- Plan Task 2 (`aggregates.py`) is **removed** — `AggregateRequest`'s broader timespan vocab makes inheritance lossy.

Net inheritor count: **9 models** (down from 11+ implied by the original spec).
