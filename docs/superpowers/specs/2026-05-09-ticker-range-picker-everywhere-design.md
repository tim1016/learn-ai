# Adopt the rich `ticker-range-picker` UX everywhere + unify the ticker-data API — design

**Status:** approved (brainstorm); revised after technical review (2026-05-09 — see "Revisions" below); pending implementation plan
**Date:** 2026-05-09
**Author:** Claude (with Tim)
**Predecessors:**
- `docs/superpowers/specs/2026-05-09-ticker-range-picker-everywhere-handoff.md` (handoff that framed the seven design tensions)
- `docs/superpowers/specs/2026-05-09-polygon-date-range-design.md` (PR #198, merged 2026-05-09 19:47 UTC) — partially superseded by this work

## Revisions (2026-05-09 post-review)

A technical review of the original spec turned up six concrete problems that revise the design before any code lands. Summary of what changed; full detail interleaved into the relevant sections:

1. **Timestamp-policy framing was wrong.** Original text said "no numerical-rigor rules are triggered" — but `.claude/rules/numerical-rigor.md` § "Timestamp rigor" mandates `int64 ms UTC` at every wire boundary, and `from_date: "YYYY-MM-DD"` is a wire format. This is an **approved temporary deferral**, not a non-applicable rule. Reframed below in §"Authority hierarchy notes" and §"Out of scope".
2. **Angular sub-component architecture clarified.** Original spec referred to "extracted partials" via `*ngTemplateOutlet`, which doesn't actually consume external HTML files. The implementation pattern is **child components** under `parts/`, which is what the plan already does — spec text now matches.
3. **Pre-existing Angular legacy patterns disclosed.** The current `ticker-range-picker.component.ts` uses `@HostListener`, `FormsModule`, and `ngModel` — all flagged by `.claude/rules/angular.md`. PR (i) **explicitly does not modernize them** (would balloon scope); they're moved into the new sub-components as-is and tracked in §"Out of scope" as a follow-up.
4. **.NET transitional aliases dropped.** `Backend/Jobs/JobsApi.cs` forwards JSON raw via `JsonNode.ParseAsync` for all five job types (cross-sectional, feature-research, signal-engine, backtest, engine-backtest), so .NET DTO aliases would be useless for those flows. Combined with the fact that `[JsonPropertyName]` is `AllowMultiple = false` (not repeatable), the original .NET alias plan was both incorrect and unnecessary. **Python `AliasChoices` is the only transitional layer**; .NET DTOs get canonical-only renames at the GraphQL-resolver paths that genuinely deserialize.
5. **`TickerRequest` inheritor list shrunk.** Three routes don't fit the `(symbol, from_date, to_date, timespan, multiplier)` shape and are removed from the inheritor list:
   - `chart.py` uses a single `timeframe: str` (`"1m"|"5m"|"15m"|...|"1D"`), not `timespan + multiplier`.
   - `research_divergence.py` preflight uses `timeframe: Literal["5m","15m","1h"]`, not the picker's resolution+multiplier.
   - `spec_strategy.py`'s spec carries `StrategySpec.symbols: list[str]` (plural — see `engine/strategy/spec/schema.py:365`), not a single symbol. The route picks `spec.symbols[0]` because Phase-1 is single-symbol. "Lifting symbol out" is a domain-shape change that needs its own design — not a cleanup step inside this initiative.
6. **Per-route default preservation.** The original `_BarRange` defaults (`multiplier=1`, `timespan="minute"`, `session="rth"`) silently change behavior at endpoints with different existing defaults — `SignalEngineJobRequest` defaults to `multiplier=15` (`jobs.py:162`), for example. Inheriting routes **override the inherited defaults explicitly** to preserve current behavior; behavior changes are surfaced as deliberate.
7. **Pydantic `extra="forbid"` + calendar/order validator.** Pydantic v2's default `extra="ignore"` made the original "PR (iii)'s alias removal causes 422 on legacy fields" claim wrong. The base now sets `extra="forbid"` and adds a `model_validator` checking calendar-date validity and `from_date <= to_date`.
8. **PR scope adjustments.**
   - `indicator-report` was never a `polygon-date-range` consumer (was deferred from PR #198 because it's template-driven). Including it in PR (iii) drags in a separate signal-refactor scope. **Deferred to its own follow-up PR.**
   - `spec-strategy-runner` symbol-lift is a domain-shape change (see #5) — **deferred to its own design**.
   - `ticker → symbol` rename **kept** (Tim's original goal of API consolidation), but only on routes that actually fit the unified shape.

## Goal

`learn-ai` has two shared input components today (`<app-ticker-range-picker>` for Engine Lab + Data Lab; `<app-polygon-date-range>` for six research-lab forms after PR #198) and a Python-route surface where the same conceptual request (`(symbol, range, sampling)`) is spelled three different ways across endpoints. This design fixes both halves of that inconsistency:

- **UI side** — the rich three-section picker (Instrument / Time window / Sampling) becomes the canonical input for *every* ticker-bearing form. The narrow `<app-polygon-date-range>` is deprecated and deleted; two new sibling components cover the cases the canonical picker can't (multi-ticker for `batch-runner`, single-date for snapshot tools like `ticker-explorer`).
- **Wire side** — every Python route that takes ticker bars inherits a single `TickerRequest` (or `MultiTickerRequest`) Pydantic base. `ticker → symbol` and `start_date/end_date → from_date/to_date` renames land in lockstep with .NET DTOs.

The north star Tim has cited before, quoted directly: *"consistency of the data ingestion from polygon and replicating the shape of ingested data into our UI in the beautiful way is what we want."*

## Decisions

Distilled from a six-question brainstorm. Each decision is one of multiple options the brainstorm surfaced; the rationale captures why this option won.

| # | Question | Decision | Rationale |
|---|---|---|---|
| Q1 | Relationship between `ticker-range-picker` and `polygon-date-range` | **Deprecate `polygon-date-range`.** Rich picker rolls out everywhere; narrow callsites get config flags (`hideSampling`, opt-in `availableMultipliers`). | Tim's framing ("this UI/UX gets repeated everywhere") points at one canonical component. A `hide*` config is cheaper than living with two competing components forever. Partially backs out PR #198, but PR #198's value (Polygon-aware constraints, weekend/holiday disable) was already in the rich picker's date inputs — only the narrow wrapper goes. |
| Q2 | Multi-ticker (`batch-runner`) | **Sibling component** `<app-multi-ticker-range-picker>`. Shares Time-window + Sampling **child components** with the canonical picker (under `shared/ticker-range-picker/parts/`). | Single-symbol Instrument card has UX (cache % hint, last-cached date, "snap to last 30 days of cache on pick") that doesn't generalize. A `multi: boolean` flag on the canonical picker would muddy the API and produce two different `value` shapes from one component. |
| Q3 | Multiplier (5m / 15m / 1h bars) | **Additive on `TickerRange`.** New optional `multiplier?: number` (default 1, backward-compatible). New `availableMultipliers` opt-in input renders a multiplier dropdown next to the resolution toggle. | `resolution` ("minute / hour / daily") is the right user-facing concept (matches what people read on a chart); `multiplier` is additive. Default 1 keeps `data-lab` and `lean-engine` byte-identical. Strategy-preflight's `'5m'` cleanly maps to `{ resolution: 'minute', multiplier: 5 }` — one canonical shape, easy backward-compat helper. |
| Q4a | `spec-strategy-runner` (symbol inside spec) | **DEFERRED to its own design (post-review).** Originally planned as a "lift symbol out of spec" cleanup step inside PR (iii), but `StrategySpec.symbols: list[str]` (`engine/strategy/spec/schema.py:365`) is a **plural list inside the domain spec object**, not a stray UI field. The route's single-symbol behavior (`spec.symbols[0]`) is a Phase-1 evaluator boundary. Changing this is a domain-shape decision, not a UI consolidation. Tracked in §"Out of scope". | Originally I assumed `symbol: str` on the spec was a UI artefact. It's a list and it's load-bearing. Out of scope here. |
| Q4b | `ticker-explorer` (single date) | **Sibling component** `<app-ticker-date-picker>`. Same Instrument card as canonical, single `<p-datepicker>`, no Sampling card. | Snapshot tools have a different mental model (one date, future-dated, no OHLCV range). Forcing the canonical picker would require multiple new flags for one consumer. A small sibling with a shared Instrument partial is YAGNI-clean. |
| Q5 | Wire-format unification | **Schema unification only — `YYYY-MM-DD` strings stay on the wire.** Define a `TickerRequest` Pydantic base; **rename fields in Python only.** .NET DTOs at GraphQL-resolver paths get canonical-only renames; **no transitional aliases on .NET** because `[JsonPropertyName]` is not repeatable and `JobsApi.cs` forwards JSON raw for all five job-type flows anyway. Python `AliasChoices` is the sole transitional layer. | Q5's option B (`int64 ms` everywhere — what `numerical-rigor.md` actually mandates) is the right direction but a much larger blast radius. Conflating it with this initiative means neither lands cleanly. **Approved temporary deferral**, tracked in §"Out of scope" cross-linked to F-0009 / F-0019 / F-0020 / F-0021. |
| Q6 | PR strategy | **Three coordinated PRs (revised post-review).** (i) picker enhancements + both new siblings + tests, **scope unchanged**. (ii) Python `TickerRequest` base + per-route default preservation + endpoint signature migrations on the **shrunk** inheritor list + .NET DTO canonical renames (no aliases). (iii) **six** consumer migrations (down from eight: `spec-strategy-runner` deferred to own design, `indicator-report` deferred to PR (iv)) + remove Pydantic `AliasChoices` + delete `polygon-date-range`. | Bigger surface per PR than PR #198's pattern, but each PR is internally coherent. PR (ii) provides transitional aliases (Python only) so PR (iii) has merge-order tolerance. |

## Architecture

Three layers, each with a single point of responsibility:

```
┌─────────────────────────────────────────────────────────┐
│ FRONTEND PICKER FAMILY (3 sibling components)           │
│   ticker-range-picker         (single ticker, range)    │
│   multi-ticker-range-picker   (universe, range)         │
│   ticker-date-picker          (single ticker, single date) │
│   shared partials: _instrument.html, _time-window.html, │
│                    _sampling.html                       │
└──────────────────┬──────────────────────────────────────┘
                   │ TickerRange / MultiTickerRange /
                   │ TickerSnapshot
                   ▼
┌─────────────────────────────────────────────────────────┐
│ WIRE ADAPTER  (Frontend/src/app/utils/ticker-wire.ts)   │
│   tickerRangeToWire(r): TickerRequestPayload            │
│   multiTickerRangeToWire(r): MultiTickerRequestPayload  │
│   — daily ↔ day translation                             │
│   — multiplier default 1                                │
│   — session default rth                                 │
└──────────────────┬──────────────────────────────────────┘
                   │ snake_case JSON over HTTP/GraphQL
                   ▼
┌─────────────────────────────────────────────────────────┐
│ PYTHON SCHEMA BASE                                      │
│   PythonDataService/app/schemas/ticker_request.py       │
│   _BarRange  →  TickerRequest      (single)             │
│              →  MultiTickerRequest (universe)           │
│   Inherited by: aggregates, chart, data_quality,        │
│     indicators, indicator_reliability, volatility,      │
│     dataset, jobs (4 sub-types), engine, spec_strategy  │
│   .NET DTOs in lockstep                                 │
└─────────────────────────────────────────────────────────┘
```

The whole point of the seam structure: any change to the wire format becomes a one-line edit in the adapter; any change to picker UI cannot reach the wire format without going through the adapter.

## Components — full surface

### Frontend partials (extracted, used by all three siblings)

```
Frontend/src/app/shared/ticker-range-picker/
  _instrument.html          (NEW — extracted from current ticker-range-picker.component.html)
  _time-window.html         (NEW — extracted)
  _sampling.html            (NEW — extracted, includes optional multiplier dropdown)
  ticker-range-picker.component.{ts,html,scss,spec.ts}
  ticker-range-picker.types.ts
```

```
Frontend/src/app/shared/multi-ticker-range-picker/
  multi-ticker-range-picker.component.{ts,html,scss,spec.ts}
  multi-ticker-range-picker.types.ts
```

```
Frontend/src/app/shared/ticker-date-picker/
  ticker-date-picker.component.{ts,html,scss,spec.ts}
  ticker-date-picker.types.ts
```

The three component HTMLs `<ng-container *ngTemplateOutlet>` into the partials so duplication is held to the Instrument-card variant.

### `ticker-range-picker` — additive changes (PR i)

```ts
// ticker-range-picker.types.ts
export interface TickerRange {
  symbol: string;
  from: string;             // YYYY-MM-DD
  to: string;               // YYYY-MM-DD
  resolution: Resolution;   // "minute" | "hour" | "daily"
  multiplier?: number;      // NEW — default 1; opt-in dropdown only renders when host passes availableMultipliers
  session?: Session;
  autoFetch?: boolean;
}
```

```ts
// ticker-range-picker.component.ts — additions
readonly availableMultipliers = input<readonly number[]>([]);   // NEW
readonly hideSampling         = input(false);                    // NEW (widens semantics)
// Note: existing `hideResolution` input is KEPT for one PR cycle as a
// deprecated alias — when set to `true`, behaves like `hideSampling=true`.
// Removed in PR (iv) (or whenever a future PR confirms no consumer calls it).

// `hideResolution` stays as a one-PR-cycle deprecation alias (see comment above).
```

Behavior:

- `availableMultipliers` non-empty → Sampling card grows a small `<p-select>` after the resolution toggle. Default selection is `multiplier ?? 1`.
- `availableMultipliers` empty / unset → no multiplier UI; existing consumers see no visual change.
- `hideSampling=true` → entire Sampling card collapsed (`indicator-reliability` uses this; sampling = indicator's own timeframe).
- Default-1 semantics: a `TickerRange` with no `multiplier` field round-trips to `multiplier: 1` through the adapter — backward-compatible for `data-lab` and `lean-engine`.

### `<app-multi-ticker-range-picker>` (new — PR i)

```ts
export interface MultiTickerRange {
  symbols: string[];        // chip array, min length 1
  from: string;
  to: string;
  resolution: Resolution;
  multiplier?: number;
  session?: Session;
  autoFetch?: boolean;
}

@Component({
  selector: 'app-multi-ticker-range-picker',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [CommonModule, FormsModule, ButtonModule, AutoComplete],
  templateUrl: './multi-ticker-range-picker.component.html',
  styleUrls: ['./multi-ticker-range-picker.component.scss'],
})
export class MultiTickerRangePickerComponent {
  readonly value = model.required<MultiTickerRange>();
  readonly tickerPool          = input<readonly TickerOption[]>([]);
  readonly recent              = input<readonly string[]>([]);
  readonly availableResolutions = input<readonly Resolution[]>(['minute','hour','daily']);
  readonly availableMultipliers = input<readonly number[]>([]);
  readonly hideSampling         = input(false);
  readonly title                = input('Cross-sectional data');
}
```

Instrument card:

- Chip array of currently-selected symbols (`<p-button>` per ticker, removable).
- "Add ticker" autocomplete combobox below the chips (filters `tickerPool`).
- "All / None" buttons on the right edge — replaces the loose grid currently in `batch-runner.component.html` lines 36–53.

Time-window card and Sampling card are byte-identical to canonical picker via shared partials.

Out of v1 (documented in component docstring): per-ticker availability strip, smart advisories, cache % hint. Multi-ticker UX for those is a separate problem.

### `<app-ticker-date-picker>` (new — PR i)

```ts
export interface TickerSnapshot {
  symbol: string;
  date: string;             // YYYY-MM-DD — single date
}

@Component({
  selector: 'app-ticker-date-picker',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [CommonModule, FormsModule, DatePickerModule, AutoComplete],
  templateUrl: './ticker-date-picker.component.html',
  styleUrls: ['./ticker-date-picker.component.scss'],
})
export class TickerDatePickerComponent {
  readonly value = model.required<TickerSnapshot>();
  readonly tickerPool = input<readonly TickerOption[]>([]);
  readonly recent     = input<readonly string[]>([]);
  readonly minDate    = input<Date | null>(null);
  readonly maxDate    = input<Date | null>(null);
  readonly title      = input('Snapshot');
  readonly dateLabel  = input('Date');
}
```

Instrument card reused via `_instrument.html` partial. Single `<p-datepicker>` instead of range. No Sampling card. Consumer (e.g. `ticker-explorer`) provides min/max so the "must be future Friday" constraint stays per-screen rather than baked into the component.

**Note** — `ticker-explorer` calls the *options snapshot* endpoint, which is not a ticker-bar route. The frontend component is shared; the backend route stays on its current schema. Out of scope here.

### `tickerRangeToWire` adapter (new — PR i)

```ts
// Frontend/src/app/utils/ticker-wire.ts
import type { TickerRange, MultiTickerRange, Resolution } from '...';

export interface TickerRequestPayload {
  symbol: string;
  from_date: string;        // YYYY-MM-DD
  to_date: string;
  timespan: 'minute' | 'hour' | 'day';
  multiplier: number;
  session: 'rth' | 'extended';
}

export interface MultiTickerRequestPayload extends Omit<TickerRequestPayload, 'symbol'> {
  symbols: string[];
}

const RESOLUTION_TO_TIMESPAN: Readonly<Record<Resolution, 'minute' | 'hour' | 'day'>> = {
  minute: 'minute',
  hour: 'hour',
  daily: 'day',  // UI-natural ↔ Polygon-natural translation lives here
};

export function tickerRangeToWire(r: TickerRange): TickerRequestPayload {
  return {
    symbol: r.symbol,
    from_date: r.from,
    to_date: r.to,
    timespan: RESOLUTION_TO_TIMESPAN[r.resolution],
    multiplier: r.multiplier ?? 1,
    session: r.session ?? 'rth',
  };
}

export function multiTickerRangeToWire(r: MultiTickerRange): MultiTickerRequestPayload {
  return {
    symbols: r.symbols,
    from_date: r.from,
    to_date: r.to,
    timespan: RESOLUTION_TO_TIMESPAN[r.resolution],
    multiplier: r.multiplier ?? 1,
    session: r.session ?? 'rth',
  };
}
```

The whole `daily ↔ day` translation lives at one boundary. Both sides stay idiomatic for their stack (UI says "daily" because that's what charts say; Polygon's API says "day" because that's its enum).

### Python `TickerRequest` base (new — PR ii)

```python
# PythonDataService/app/schemas/ticker_request.py
from __future__ import annotations
from datetime import date as Date
from typing import Literal
from pydantic import AliasChoices, BaseModel, ConfigDict, Field, model_validator

DATE_PATTERN = r"^\d{4}-\d{2}-\d{2}$"
Timespan = Literal["minute", "hour", "day"]
Session = Literal["rth", "extended"]

class _BarRange(BaseModel):
    """Common shape for any request that pulls bars over a date range.

    `extra="forbid"` is required: Pydantic v2's default `extra="ignore"`
    would silently drop unknown fields, hiding the rename bug instead of
    surfacing it once PR (iii) removes the transitional aliases.
    """
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    from_date: str = Field(
        ..., pattern=DATE_PATTERN,
        validation_alias=AliasChoices("from_date", "start_date"),
    )
    to_date: str = Field(
        ..., pattern=DATE_PATTERN,
        validation_alias=AliasChoices("to_date", "end_date"),
    )
    timespan:   Timespan = "minute"
    multiplier: int      = Field(1, ge=1)
    session:    Session  = "rth"

    @model_validator(mode="after")
    def _validate_dates(self) -> "_BarRange":
        # The pattern only checks shape; "2025-13-99" passes the regex
        # but isn't a real date. Parse + verify calendar validity, then
        # confirm from_date <= to_date.
        try:
            f = Date.fromisoformat(self.from_date)
            t = Date.fromisoformat(self.to_date)
        except ValueError as e:
            raise ValueError(f"invalid calendar date: {e}") from e
        if t < f:
            raise ValueError(
                f"to_date ({self.to_date}) must be >= from_date ({self.from_date})"
            )
        return self

class TickerRequest(_BarRange):
    """Single-symbol bar request."""
    symbol: str = Field(
        ..., min_length=1, max_length=20,
        validation_alias=AliasChoices("symbol", "ticker"),
    )

class MultiTickerRequest(_BarRange):
    """Universe-of-symbols bar request — used by cross-sectional research."""
    symbols: list[str] = Field(
        ..., min_length=1,
        validation_alias=AliasChoices("symbols", "tickers"),
    )
```

**Per-route default preservation.** The base sets `multiplier=1`, `timespan="minute"`, `session="rth"`. Inheriting routes whose existing defaults differ **must override the inherited fields** to preserve current behavior. Example for `SignalEngineJobRequest` (current default `multiplier=15` per `jobs.py:162`):

```python
class SignalEngineJobRequest(_CamelCaseTickerRequest):
    multiplier: int = Field(15, ge=1)   # explicit — preserves pre-migration default
    job_id: str = Field(..., min_length=1)
    feature_name: str = Field(..., min_length=1)
    flip_sign: bool = True
    regime_gate_enabled: bool = True
    force: bool = False
```

Per-route audit lives in **§"Contract matrix"** below; PR (ii)'s Task 0 confirms the `_confirm_` cells before any code change.

**Routes that inherit (PR ii) — REVISED post-review:**

| Router / model | Current shape | After PR ii |
|---|---|---|
| `aggregates.AggregatesRequest` | `ticker`, `from_date`, `to_date`, `timespan`, `multiplier` | inherits `TickerRequest` |
| `data_quality.DataQualityRequest` | `ticker`, `from_date`, `to_date` | inherits `TickerRequest` (no `multiplier`/`timespan` use; base defaults are harmless) |
| `indicators.IndicatorsRequest` | `ticker`, `from_date`, `to_date`, `timespan`, `multiplier` | inherits `TickerRequest` |
| `indicator_reliability.IndicatorReliabilityRequest` | `ticker`, dates | inherits `TickerRequest` |
| `volatility` `/series` models | `ticker`, dates | inherits `TickerRequest` (per-model — only the ones matching the canonical shape) |
| `dataset` JSON endpoints | varies | inherits `TickerRequest` for JSON endpoints; `Form()` endpoints stay |
| `jobs.RuleBasedBacktestJobRequest` (jobs.py:79) | `ticker`, dates, **multiplier=15**, timespan="minute" | inherits `TickerRequest`; **overrides `multiplier=15`** to preserve default |
| `jobs.FeatureResearchJobRequest` (jobs.py:136) | `ticker`, dates, multiplier=1, timespan="minute" | inherits `TickerRequest` (defaults match) |
| `jobs.SignalEngineJobRequest` (jobs.py:154) | `ticker`, dates, **multiplier=15**, timespan="minute" | inherits `TickerRequest`; **overrides `multiplier=15`** |
| `jobs.CrossSectionalJobRequest` (jobs.py:117) | `tickers: list[str]`, dates | inherits `MultiTickerRequest` |
| `engine.EngineBacktestRequest` (engine.py:1165) | `start_date`/`end_date` (strategy-owned symbol) | inherits **`_BarRange` only** (NOT `TickerRequest`); rename dates; symbol stays strategy-owned |

**Routes that explicitly do NOT inherit — REVISED:**

| Router / model | Why |
|---|---|
| `chart.ChartDataRequest` / `AllowedTimeframesRequest` (chart.py:33,53) | Uses single `timeframe: str` (`"1m"\|"5m"\|"15m"\|"30m"\|"1h"\|"4h"\|"1D"\|"1W"\|"1M"`), not `timespan + multiplier`. Different shape; would force a lossy conversion. Stays as-is; the picker emits a separate `timeframe` string for chart consumers via a `timeframeFromTickerRange()` helper added to `utils/ticker-wire.ts` |
| `research_divergence._PreflightRequestBody` (research_divergence.py:113) | Uses `timeframe: Literal["5m","15m","1h"]` — distinct shape, doesn't fit |
| `spec_strategy.SpecBacktestRequest` | `StrategySpec.symbols: list[str]` is a **plural list inside the domain spec object** (`engine/strategy/spec/schema.py:365`). Phase-1 picks `spec.symbols[0]` because evaluator boundary is single-symbol, but the type is plural and load-bearing. Lifting to top-level is a domain-shape change → **own design**, deferred |
| `options`, `quantlib_options` | Options endpoints — strike/expiration shape |
| `iv_recorder`, `iv30` | Recorder endpoints — different mental model |
| `edge` | Case-by-case: IV/RV-alignment shapes; none migrate here |
| `market_monitor`, `tickers`, `sanitize`, `snapshot`, `golden_fixtures`, `portfolio`, `broker` | Orthogonal concerns |

The revised discrimination rule: **a route inherits `TickerRequest` iff (a) its primary input matches `(symbol, from_date, to_date, timespan, multiplier)` exactly, AND (b) its current behavior is preserved by either the base defaults or an explicit field override.** Anything else stays as-is.

### Contract matrix (PR ii — Task 0, before any code)

PR (ii)'s first task is to confirm this table by grep against the live tree. Each `_confirm_` cell is filled in by Task 0; the completed table is committed back into this spec and is the authoritative reference for the per-route migrations.

| Model (file) | Current symbol field | Current dates | Current `multiplier` | Current `timespan` | Current `session` | Transport | Compat strategy |
|---|---|---|---|---|---|---|---|
| `AggregatesRequest` | `ticker` | `from_date`/`to_date` | _confirm_ | _confirm_ | _confirm_ | direct REST | inherit; override defaults if differ |
| `DataQualityRequest` | `ticker` | `from_date`/`to_date` | n/a | n/a | n/a | direct REST | inherit |
| `IndicatorsRequest` | `ticker` | `from_date`/`to_date` | _confirm_ | _confirm_ | _confirm_ | direct REST | inherit |
| `IndicatorReliabilityRequest` | `ticker` | `from_date`/`to_date` | _confirm_ | _confirm_ | _confirm_ | direct REST | inherit |
| `VolatilitySeriesRequest`(s) | `ticker` | dates | _confirm_ | _confirm_ | _confirm_ | direct REST | inherit per-model |
| `DatasetGenerationRequest` (JSON) | `ticker` | dates | _confirm_ | _confirm_ | _confirm_ | direct REST | inherit |
| `RuleBasedBacktestJobRequest` | `ticker` | `from_date`/`to_date` | **15** | `"minute"` | n/a | jobs (raw forward) | inherit; **override `multiplier=15`** |
| `FeatureResearchJobRequest` | `ticker` | `from_date`/`to_date` | 1 | `"minute"` | n/a | jobs | inherit (defaults match) |
| `SignalEngineJobRequest` | `ticker` | `from_date`/`to_date` | **15** | `"minute"` | n/a | jobs | inherit; **override `multiplier=15`** |
| `CrossSectionalJobRequest` | `tickers: list[str]` | `from_date`/`to_date` | n/a | n/a | n/a | jobs | inherit `MultiTickerRequest` |
| `EngineBacktestRequest` | (strategy-owned, NOT in body) | `start_date`/`end_date` | n/a | n/a | n/a | direct REST | inherit `_BarRange` only |

### .NET DTOs (PR ii) — REVISED post-review

**`Backend/Jobs/JobsApi.cs:88-121` forwards JSON raw** for all five job-type flows (`backtest`, `cross_sectional`, `feature_research`, `signal_engine`, `engine_backtest`) — it parses to `JsonNode`, injects `job_id`, and `ToJsonString()` forwards verbatim. The .NET layer **never deserializes those payloads into typed DTOs**, so .NET DTO renames or aliases on those flows would be inert.

The original spec proposed `[JsonPropertyName]` transitional aliases on .NET DTOs. That plan is dropped because:

1. **`[JsonPropertyName]` is `AllowMultiple = false`** — the original example wouldn't compile.
2. **Jobs flows forward JSON raw** — even a well-formed alias would not run.
3. **Python `AliasChoices` already covers every consumer path** — direct REST, GraphQL resolver, jobs forwarder. One transitional layer is enough.

**What .NET actually changes in PR (ii):**

| File class | Action |
|---|---|
| `Backend/Models/DTOs/ResearchModels.cs` (and similar) | **Canonical-only renames**: `Ticker → Symbol`, `StartDate/EndDate → FromDate/ToDate` where the DTO actually deserializes a body. No transitional aliases. The compiler surfaces every consumer; each gets renamed in lockstep |
| `Backend/GraphQL/*Mutation.cs` resolvers | If a resolver argument was named `ticker`, rename to `symbol` (with the `[GraphQLName("ticker")]` schema-side alias kept for one PR cycle so a stale frontend GraphQL query doesn't 400) |
| `Backend/Jobs/JobsApi.cs` | Untouched. JSON is forwarded raw; no DTO involved |

GraphQL-side compatibility is the only place a transitional alias is genuinely useful, and Hot Chocolate's `[GraphQLName]` does support coexisting field names by leaving the legacy name on a deprecated field marker:

```csharp
// resolver argument rename in PR (ii):
public Task<ResearchResult> RunResearch(string symbol, ...)   // canonical

// PR (iii) removes any deprecated GraphQL aliases that PR (ii) left
// behind for in-flight frontend queries.
```

The plan's Task 12 (PR ii) and Task 9 (PR iii) are revised accordingly: Task 12 does **canonical-only** DTO renames + GraphQL alias-pinning where applicable; Task 9 removes only those GraphQL aliases (no DTO setter cleanup, because there are no transitional setters to remove).

## Data flow

Per request:

1. Picker emits a typed payload (`TickerRange` / `MultiTickerRange` / `TickerSnapshot`) into the consumer's `signal()`/`model()`.
2. Consumer calls into a service method with that payload.
3. Service method passes the payload through `tickerRangeToWire(r)` (or sibling) to obtain a `TickerRequestPayload` matching the Python schema.
4. .NET Backend (when present) deserializes into a renamed DTO, optionally augments, and forwards to Python service.
5. Python route's Pydantic model validates against `TickerRequest` base; rejected fields surface as standard FastAPI 422 with the field path.

Type safety: `TickerRange` cannot be sent over the wire **except** through the adapter — the adapter is the only function with a return type matching the Python schema, and consumer code references payload types from the adapter module.

## Migration order (inside PR iii) — REVISED post-review

PR (iii) migrates **six** consumers (down from eight). `spec-strategy-runner` and `indicator-report` are removed — see "Deferred" below.

Order chosen to land the lowest-friction consumers first so picker-config bugs surface early on small surfaces. **Each migration preserves the consumer's existing per-route default** (e.g. signal-runner's `multiplier: 15`) by initializing the picker's `range` signal with that value — no silent behavior change.

1. **`indicator-reliability`** — cleanest fit (uses `hideSampling=true`, no multiplier, no symbol nesting). Smoke-tests the new `hideSampling` config.
2. **`strategy-preflight`** — consumes multiplier (`'5m'` → `{ resolution: 'minute', multiplier: 5 }`). Smoke-tests `availableMultipliers`. Note: this consumer talks to `research_divergence.preflight`, which **does not inherit `TickerRequest`** (uses its own `timeframe` shape). The frontend picker payload is converted to the route's `timeframe` string at the consumer's wire-call site.
3. **`feature-runner`** — full multiplier surface. Drops the standalone ticker input + timespan select + multiplier int. **Initializes `range.multiplier = 1`** to match existing default.
4. **`signal-runner`** — same shape as feature-runner. **Initializes `range.multiplier = 15`** to match `SignalEngineJobRequest`'s pre-migration default.
5. **`batch-runner`** — first consumer of `<app-multi-ticker-range-picker>`. Drops the chip grid (lines 36–53 of current HTML). Initializes with the existing default ticker universe.
6. **`ticker-explorer`** — first consumer of `<app-ticker-date-picker>`. Drops the raw `<input type="date">` + raw text ticker.
7. **Remove Pydantic transitional aliases** (`AliasChoices` for `ticker`/`tickers`/`start_date`/`end_date` come off in `app/schemas/ticker_request.py`). With the six migrated consumers now sending canonical names, legacy names are no longer in flight.
8. **Remove deprecated `hideResolution` input** from `ticker-range-picker.component.ts` (one-PR-cycle deprecation expires here).
9. **Delete** `Frontend/src/app/shared/polygon-date-range/` + remove from imports.

**Deferred from PR (iii) — moved to follow-ups:**

- **`spec-strategy-runner`** — `StrategySpec.symbols: list[str]` is plural and load-bearing inside the domain spec object. Lifting it to a top-level form field is a domain-shape decision, not a UI consolidation. Tracked in §"Out of scope" as **own design**.
- **`indicator-report`** — never adopted `polygon-date-range` (template-driven `[(ngModel)]` against non-signal fields). Migrating it requires a separate `signals + OnPush` refactor that has nothing to do with this initiative. Tracked in §"Out of scope" as **PR (iv)**.

PR (iii) is structured as one logical commit per consumer (matching PR #198's cadence inside a single PR), so any consumer can be reverted on its own without rewinding the others.

## Error handling

- **Pydantic validation** at the schema base level catches three classes of bug at every inheriting route:
  - **Shape**: `^\d{4}-\d{2}-\d{2}$` regex pattern on `from_date` and `to_date` (the original `2025-5-31` bug from PR #198's incident).
  - **Calendar validity**: `model_validator(mode="after")` parses the string with `date.fromisoformat`; rejects `2025-13-99` etc. The pattern alone doesn't catch this.
  - **Order**: same validator rejects `to_date < from_date`.
- **`extra="forbid"`** on the base — Pydantic v2's default `extra="ignore"` would silently drop unknown fields, masking the rename bug instead of surfacing it. With `forbid`, any leftover legacy field name (after PR (iii) removes the `AliasChoices`) produces a clear `extra_forbidden` 422 with the offending key.
- **FastAPI 422** with field path bubbles to the client. The original incident (`string_pattern_mismatch` on `to_date`) showed this works; we preserve the behavior.
- **Frontend type safety** — `TickerRequestPayload` is the only acceptable wire shape; any service method whose body parameter is typed `TickerRequestPayload` cannot accept a malformed object. Consumer code references types from `utils/ticker-wire.ts`, not from picker types directly.
- **.NET DTOs** — `required` keyword (C# 11+) on the renamed properties; missing fields fail at deserialization time, not at the Python boundary.

## Testing

### PR (i) — picker enhancements + siblings

- `ticker-range-picker.component.spec.ts` — extend with:
  - host sets `availableMultipliers=[1,5,15]` → multiplier dropdown renders → user picks `5` → `value().multiplier === 5`
  - host sets `hideSampling=true` → Sampling card not in DOM
  - default behavior: no `availableMultipliers` → no multiplier dropdown; `multiplier === undefined` initially → adapter defaults to 1
- `multi-ticker-range-picker.component.spec.ts` — full suite:
  - chip add/remove, "All" / "None" buttons, autocomplete filter
  - shared Time-window + Sampling templates render identically to canonical
  - `value().symbols` round-trips through chip operations
- `ticker-date-picker.component.spec.ts` — full suite:
  - Instrument card behavior matches canonical (delegated test reused)
  - `minDate`/`maxDate` constrain the picker
  - `value().date` round-trip
- `ticker-wire.spec.ts` — pure-function suite:
  - `daily → day` translation
  - `multiplier` defaults to 1 when undefined
  - `session` defaults to `rth`
  - multi-symbol shape preserves `symbols` array

### PR (ii) — schema + .NET DTOs

- `tests/schemas/test_ticker_request.py` — new shared parametric tests:
  - valid `TickerRequest` round-trip
  - rejects `from_date: "2025-5-31"` (regression for the original bug)
  - rejects calendar-invalid date `"2025-13-99"` (validator catches what the regex misses)
  - rejects `to_date < from_date`
  - rejects empty `symbol`, `multiplier=0`
  - `MultiTickerRequest` rejects empty `symbols` list
  - `extra="forbid"`: unknown fields produce `extra_forbidden` validation error
  - transitional aliases accepted: `ticker`/`tickers`/`start_date`/`end_date` resolve to the canonical fields
- For each inheriting router, a smoke test that posts both shapes (canonical + legacy alias) and asserts 200 — most are existing tests that rerun under the new schema.
- Per-route default-preservation test: e.g. `SignalEngineJobRequest()` with no `multiplier` still defaults to `15` (not the base's `1`).
- .NET `Backend.Tests` — DTO deserialization tests for the canonical-only field names (no transitional alias tests because there are no .NET aliases).

### PR (iii) — consumer migrations

- Per-consumer smoke spec: existing component spec re-runs against new HTML. No new specs added per consumer.
- One reconciliation test: post the same payload through every migrated consumer's wire path, assert identical Python-side reception (catches a forgotten adapter call).

### Project-scope tests before push

Per `.claude/rules/python.md` and `.claude/rules/testing.md`:

```
podman exec polygon-data-service python -m pytest /app/tests
cd Backend.Tests && dotnet test
podman exec my-frontend npx ng test --watch=false
ruff check PythonDataService/app/ PythonDataService/tests/
npx eslint Frontend/src/ --max-warnings 0
dotnet format podman.sln --verify-no-changes
```

Pre-existing failures (if any) baselined against `origin/master` before PR open; called out in PR description.

## Out of scope (tracked follow-ups)

- **`int64 ms UTC` wire-format migration. APPROVED TEMPORARY DEFERRAL.** `.claude/rules/numerical-rigor.md` § "Timestamp rigor" mandates `int64 ms` at every wire boundary; `from_date: "YYYY-MM-DD"` is a wire format and is therefore a known violation of that rule. This initiative does not fix it because the blast radius (every consumer of every endpoint, plus the .NET-side `DateTime`/`DateTimeOffset` parse paths flagged in F-0021/F-0022, plus the Frontend `Date` parsing paths in F-0034) is much larger than the picker scope; conflating the two means neither lands cleanly. Tracked separately, cross-linked to existing audit findings F-0009 (sanitizer ISO timestamp wire), F-0019 (trade-comparison naive strptime), F-0020 (timestamp ban-list rollup), F-0021 (.NET ingestion `DateTime.Parse` AssumeUniversal), F-0022 (.NET query-parameter `DateTime.Parse`), F-0024 (additional ISO-Z emission), F-0033 (Python non-ingestion ban-list violations), F-0034 (frontend naive date parse rollup). Until that PR ships, this design's wire format is a documented exception to the rule.
- **`spec-strategy-runner` symbol redesign.** `StrategySpec.symbols: list[str]` (`engine/strategy/spec/schema.py:365`) is plural and load-bearing inside the domain spec. The current Phase-1 evaluator boundary picks `spec.symbols[0]` because it's single-symbol-only, but the type permits multi-symbol strategies. Lifting `symbols` (or `symbol`) to a top-level form field touches the strategy domain shape, not just UI. **Own design**, separate initiative.
- **`indicator-report` migration to signals + picker.** Was deferred from PR #198 because of template-driven `[(ngModel)]` against non-signal fields. The migration requires a `signals + OnPush` refactor independent of the picker work. **PR (iv)** when scheduled.
- **Existing picker legacy patterns** — the canonical `ticker-range-picker.component.ts` uses `@HostListener` (line 308), `FormsModule` + `ngModel` (lines 17, 98). All three are flagged by `.claude/rules/angular.md`. PR (i) **explicitly does not modernize** them — moving them as-is into the new sub-components keeps the PR scoped. Tracked as a follow-up: replace `@HostListener` with the `host` object on `@Component`, replace `ngModel` with signal-based two-way binding, remove `FormsModule` from the imports list.
- **`hideResolution` deprecated alias removal.** Kept for one PR cycle to avoid breakage if any out-of-tree consumer was passing it. Removed in PR (iii)'s commit 8 (or a later cleanup PR if scope tightens).
- **Multi-ticker availability strip / advisories.** The `<app-multi-ticker-range-picker>` v1 omits per-ticker availability and smart advisories. Separate UX problem.
- **Single-date Polygon advisories.** `<app-ticker-date-picker>` v1 has no inline Polygon advisory (consumer provides min/max). Future consumers can add it.
- **Removing `polygon-date-range` from `docs/superpowers/specs/2026-05-09-polygon-date-range-design.md` history.** That doc stays as the record of what shipped in PR #198.

## Risks & open considerations

- **PR (ii) → (iii) merge-order tolerance.** Pydantic `AliasChoices` lets PR (ii) merge before PR (iii)'s frontend payload changes are ready. The aliases come off in PR (iii)'s commit 7 — risk: if a consumer is missed, it'll fail at runtime after alias removal. Mitigation: with `extra="forbid"` on the base, the failure surfaces as a clear `extra_forbidden` 422 with the offending field name (no silent acceptance). Project-scope test suite is run after the alias-removal commit and before the PR opens.
- **Per-route default preservation is a manual audit.** The contract matrix is the load-bearing artefact — Task 0 of PR (ii) confirms each `_confirm_` cell against the live tree before any code change. Skipping or rushing Task 0 is the single biggest regression risk in this initiative.
- **PrimeNG `dateFormat="yy-mm-dd"` quirk.** Already addressed in the existing canonical picker; new siblings inherit the same `<p-datepicker>` configuration.
- **Multiplier defaults + GraphQL.** Some endpoints arrive via the .NET GraphQL API (Hot Chocolate v15 resolvers) rather than direct REST. Check `Backend/GraphQL/*` for any resolver argument literally named `ticker` that needs `[GraphQLName("ticker")]` pinning during the rename, and any hardcoded `multiplier: 15` / `timespan: "minute"` in `Backend/Services/*` that conflicts with new defaults. PR (ii)'s plan enumerates the affected resolvers as Task 12.
- **Sampling card layout with multiplier dropdown.** Adding a multiplier `<p-select>` next to the existing minute/hour/daily toggle and session toggle pushes the Sampling card width. Verify (a) it stays under the existing card's wrap point at viewport breakpoints used by Engine Lab + Data Lab, and (b) AXE focus/contrast pass with the additional control. If layout fails, fall back to a small icon-button group rather than a `<p-select>`.
- **`research_divergence.preflight` shape mismatch.** `strategy-preflight` uses the picker (with `availableMultipliers`) but the underlying route uses a single `timeframe` string. The wire-call site at the consumer converts picker `{ resolution, multiplier }` → preflight `'5m'|'15m'|'1h'` via a small adapter helper; if the picker emits a combination not in that list, the adapter throws and the run is blocked at the form level (no silent coercion).
- **`chart` route shape mismatch.** Same shape mismatch as preflight — chart wants single `timeframe: str`. If a future consumer wants to use the picker against `/api/chart/data`, the same per-call adapter helper applies.

## Build sequence (for the implementation plan)

PR (i) — picker enhancements + new siblings (scope unchanged):
1. Extract `parts/instrument-card.component`, `parts/time-window-card.component`, `parts/sampling-card.component` (real Angular child components, not HTML partials).
2. Add `multiplier?: number` to `TickerRange`, `availableMultipliers` + `hideSampling` inputs to canonical picker (`hideResolution` kept as deprecated alias).
3. Update `ticker-range-picker.component.spec.ts` with the new flag tests.
4. Create `<app-multi-ticker-range-picker>` + types + spec.
5. Create `<app-ticker-date-picker>` + types + spec.
6. Create `Frontend/src/app/utils/ticker-wire.ts` + `ticker-wire.spec.ts`.
7. Project-scope ESLint + Vitest pass; open PR (i).

PR (ii) — Python `TickerRequest` base + canonical-only .NET DTO renames (REVISED):
1. **Task 0 — Contract matrix confirmation**: grep against live tree to fill every `_confirm_` cell in §"Contract matrix"; commit the completed matrix back into this spec.
2. Create `PythonDataService/app/schemas/ticker_request.py` (with `extra="forbid"`, `model_validator` for calendar/order, `AliasChoices` for legacy names) + `tests/schemas/test_ticker_request.py`.
3. Migrate each inheriting router one commit at a time, **preserving each route's existing defaults via explicit field overrides**. Each commit re-runs the project-scope test.
4. Canonical-only .NET DTO renames (`Ticker → Symbol`, `StartDate/EndDate → FromDate/ToDate`) + GraphQL `[GraphQLName]` schema-side aliases on resolver arguments where a stale frontend query might hit. **No `[JsonPropertyName]` transitional aliases** — Python is the only transitional layer.
5. Project-scope ruff + dotnet format + dotnet test pass; open PR (ii).

PR (iii) — consumer migrations + cleanup (REVISED — six consumers, not eight):
1. One commit per consumer in the migration order above (six commits: indicator-reliability, strategy-preflight, feature-runner, signal-runner, batch-runner, ticker-explorer).
2. One commit removing Pydantic `AliasChoices` from `ticker_request.py` (legacy field names now produce `extra_forbidden` 422).
3. One commit removing the deprecated `hideResolution` input from canonical picker.
4. One commit removing GraphQL `[GraphQLName]` schema aliases pinned in PR (ii) Task 4.
5. One commit deleting `Frontend/src/app/shared/polygon-date-range/`.
6. Project-scope test pass; open PR (iii). PR description lists the six migrated consumers and the two explicitly-deferred (spec-strategy-runner own-design, indicator-report PR (iv)).

## Files / folders this effort touches

**Created:**
- `Frontend/src/app/shared/ticker-range-picker/parts/instrument-card.component.{ts,html,scss,spec.ts}`
- `Frontend/src/app/shared/ticker-range-picker/parts/time-window-card.component.{ts,html,scss,spec.ts}`
- `Frontend/src/app/shared/ticker-range-picker/parts/sampling-card.component.{ts,html,scss,spec.ts}`
- `Frontend/src/app/shared/multi-ticker-range-picker/*` (component + multi-instrument-card sub-component + types + spec)
- `Frontend/src/app/shared/ticker-date-picker/*` (component + types + spec)
- `Frontend/src/app/utils/ticker-wire.ts` + `ticker-wire.spec.ts`
- `PythonDataService/app/schemas/ticker_request.py` + `tests/schemas/test_ticker_request.py`

**Modified:**
- `Frontend/src/app/shared/ticker-range-picker/*` (additive: `multiplier`, `availableMultipliers`, `hideSampling`; `hideResolution` kept as deprecated alias for one PR cycle)
- **Six** consumer components + their HTML (indicator-reliability, strategy-preflight, feature-runner, signal-runner, batch-runner, ticker-explorer)
- Inheriting Python routers per the contract matrix (NOT chart, NOT spec_strategy, NOT research_divergence): `aggregates`, `data_quality`, `indicators`, `indicator_reliability`, `volatility` (per-model), `dataset` (JSON only), `jobs.py` (4 models), `engine.py` (`_BarRange` only)
- `Backend/Models/DTOs/*.cs` — canonical-only renames (no transitional aliases)
- `Backend/GraphQL/*Mutation.cs` — resolver argument renames + `[GraphQLName]` schema aliases pinned for one PR cycle
- `Backend/Services/*` — any hardcoded `Ticker` / `StartDate` consumer references

**Deleted:**
- `Frontend/src/app/shared/polygon-date-range/` (final commits of PR iii)

**Untouched (and explicitly deferred — see §"Out of scope"):**
- `chart`, `research_divergence` (`preflight`), `spec_strategy` Python routers
- `spec-strategy-runner`, `indicator-report` consumers
- `Backend/Jobs/JobsApi.cs` (forwards JSON raw — no DTO involved)
- Options/snapshot routes (`options`, `quantlib_options`, `iv_recorder`, `iv30`, `edge`)
- `data-lab` (already adopted; defaults preserve byte-identical behavior)
- `lean-engine` (already adopted; same)
- `market-calendar` (no ticker-bar input)

## Authority hierarchy notes

This work touches three stacks:

- **Frontend** — `.claude/rules/angular.md`. **NEW code** (the three new sub-components, the two new sibling components, `tickerRangeToWire`) is fully compliant: standalone, OnPush, signals + `model()`, modern control flow, no decorators, no `mutate()`, no `ngClass`/`ngStyle`. **Pre-existing code** (the canonical picker's `@HostListener`, `FormsModule`, `ngModel`) is moved as-is into the new sub-components — it remains a known violation of the rules and is tracked in §"Out of scope" for separate cleanup. PR (i) does not modernize on touch (would balloon scope and is unrelated to this initiative's goal).
- **.NET** — `.claude/rules/dotnet.md`. PascalCase fields, canonical-only renames (no transitional `[JsonPropertyName]` aliases — see §".NET DTOs" for why), `[GraphQLName]` for one-PR-cycle GraphQL schema aliases, no silent catches, structured logging.
- **Python** — `.claude/rules/python.md`. Pydantic v2 (`Field`, `Literal`, `model_validator`, `AliasChoices`, `ConfigDict`), `from __future__ import annotations`, snake_case fields, project-scope ruff before push.

**Numerical-rigor:** `.claude/rules/numerical-rigor.md` § "Timestamp rigor" applies — `from_date: "YYYY-MM-DD"` is a wire-boundary timestamp and the rule mandates `int64 ms UTC`. This design ships an **approved temporary deferral**, not a non-applicable case. The deferral is tracked in §"Out of scope" with cross-links to the audit findings (F-0009, F-0019, F-0020, F-0021, F-0022, F-0024, F-0033, F-0034) that define the parallel `int64 ms` initiative. Math-porting rules are not triggered (no math is ported).
