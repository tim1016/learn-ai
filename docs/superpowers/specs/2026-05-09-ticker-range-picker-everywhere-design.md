# Adopt the rich `ticker-range-picker` UX everywhere + unify the ticker-data API — design

**Status:** approved (brainstorm), pending implementation plan
**Date:** 2026-05-09
**Author:** Claude (with Tim)
**Predecessors:**
- `docs/superpowers/specs/2026-05-09-ticker-range-picker-everywhere-handoff.md` (handoff that framed the seven design tensions)
- `docs/superpowers/specs/2026-05-09-polygon-date-range-design.md` (PR #198, merged 2026-05-09 19:47 UTC) — partially superseded by this work

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
| Q2 | Multi-ticker (`batch-runner`) | **Sibling component** `<app-multi-ticker-range-picker>`. Shares Time-window + Sampling sub-templates with the canonical picker via extracted partials. | Single-symbol Instrument card has UX (cache % hint, last-cached date, "snap to last 30 days of cache on pick") that doesn't generalize. A `multi: boolean` flag on the canonical picker would muddy the API and produce two different `value` shapes from one component. |
| Q3 | Multiplier (5m / 15m / 1h bars) | **Additive on `TickerRange`.** New optional `multiplier?: number` (default 1, backward-compatible). New `availableMultipliers` opt-in input renders a multiplier dropdown next to the resolution toggle. | `resolution` ("minute / hour / daily") is the right user-facing concept (matches what people read on a chart); `multiplier` is additive. Default 1 keeps `data-lab` and `lean-engine` byte-identical. Strategy-preflight's `'5m'` cleanly maps to `{ resolution: 'minute', multiplier: 5 }` — one canonical shape, easy backward-compat helper. |
| Q4a | `spec-strategy-runner` (symbol inside spec) | **Refactor — lift `symbol` out of the spec to a top-level form field.** Picker drives normally with no `hideTicker` flag. Backend `SpecBacktestRequest` schema also moves `symbol` to top level. | Cleaner long-term shape: every form has ticker as a top-level concept. Drops the need for a `hideTicker` config. Costs a coordinated frontend+backend change but the spec shape was the only consumer that needed `hideTicker`. |
| Q4b | `ticker-explorer` (single date) | **Sibling component** `<app-ticker-date-picker>`. Same Instrument card as canonical, single `<p-datepicker>`, no Sampling card. | Snapshot tools have a different mental model (one date, future-dated, no OHLCV range). Forcing the canonical picker would require multiple new flags for one consumer. A small sibling with a shared Instrument partial is YAGNI-clean. |
| Q5 | Wire-format unification | **Schema unification only — `YYYY-MM-DD` strings stay on the wire.** Define a `TickerRequest` Pydantic base; rename fields in lockstep with .NET DTOs. | Q5's option B (`int64 ms` everywhere — what `numerical-rigor.md` actually mandates) is the right direction but a much larger blast radius (every consumer of every endpoint shifts). Conflating it with this initiative means neither lands cleanly. **Tracked as a separate follow-up cross-linked to F-0009 / F-0019 / F-0020 / F-0021** in §"Out of scope" below. |
| Q6 | PR strategy | **Three coordinated PRs.** (i) picker enhancements + both new siblings + tests. (ii) Python `TickerRequest` base + endpoint signature migrations + .NET DTO renames. (iii) all eight consumer migrations + `spec-strategy-runner` symbol-lift + delete `polygon-date-range`. | Tim's choice. Bigger surface per PR than PR #198's pattern, but each PR is internally coherent and PR (ii) provides transitional aliases so PR (iii)'s merge order has tolerance. |

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
readonly hideSampling         = input(false);                    // NEW (renames + widens hideResolution)

// existing hideResolution input is renamed to hideSampling. Currently no consumer
// passes hideResolution=true, so this is a rename without behavioural risk.
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
from typing import Literal
from pydantic import BaseModel, Field

DATE_PATTERN = r"^\d{4}-\d{2}-\d{2}$"
Timespan = Literal["minute", "hour", "day"]
Session = Literal["rth", "extended"]

class _BarRange(BaseModel):
    """Common shape for any request that pulls bars over a date range."""
    from_date: str = Field(..., pattern=DATE_PATTERN)
    to_date:   str = Field(..., pattern=DATE_PATTERN)
    timespan:  Timespan = "minute"
    multiplier: int    = Field(1, ge=1)
    session:   Session = "rth"

class TickerRequest(_BarRange):
    """Single-symbol bar request — used by every aggregates/indicator/research route."""
    symbol: str = Field(..., min_length=1, max_length=20)

class MultiTickerRequest(_BarRange):
    """Universe-of-symbols bar request — used by cross-sectional research."""
    symbols: list[str] = Field(..., min_length=1)
```

**Routes that inherit (PR ii):**

| Router | Current shape | After PR ii |
|---|---|---|
| `aggregates` | `ticker`, `from_date`, `to_date`, `timespan`, `multiplier` | inherits `TickerRequest` (rename `ticker → symbol`) |
| `chart` (`ChartDataRequest`, `AllowedTimeframesRequest`) | `ticker`, `from_date`, `to_date` | inherits `TickerRequest` |
| `data_quality` (`DataQualityRequest`) | `ticker`, `from_date`, `to_date` | inherits `TickerRequest` |
| `indicators` | `ticker`, `from_date`, `to_date`, `timespan`, `multiplier` | inherits `TickerRequest` |
| `indicator_reliability` | similar | inherits `TickerRequest` |
| `volatility` (`/series` endpoints) | `ticker`, dates | inherits `TickerRequest` |
| `dataset` | `ticker` (Form), dates | inherits `TickerRequest` for JSON endpoints; `Form()` endpoints stay (multipart upload constraint) |
| `jobs` (`backtest`, `feature-research`, `signal-engine`, `cross-sectional`) | varies | `RuleBasedBacktestJobRequest`, `FeatureResearchJobRequest`, `SignalEngineJobRequest` inherit `TickerRequest`; `CrossSectionalJobRequest` inherits `MultiTickerRequest` |
| `engine` (`EngineBacktestRequest`) | `start_date`, `end_date` (strategy owns symbol) | rename `start_date/end_date → from_date/to_date`; symbol stays at top level when caller overrides; uses `_BarRange` partial inheritance (not full `TickerRequest` because symbol can be strategy-owned) |
| `spec_strategy` (`SpecBacktestRequest`) | `symbol` inside spec | PR (iii) lifts `symbol` to top-level; PR (ii) prepares the alias |

**Routes that explicitly do NOT inherit:**

| Router | Why |
|---|---|
| `options`, `quantlib_options` | Options endpoints — strike/expiration shape, not bar-range |
| `iv_recorder`, `iv30` | Recorder endpoints — different mental model |
| `edge` | Case-by-case: most edge endpoints are not primary "bars over range" — `RealizedVsIvSeriesRequest` and similar are IV/RV-alignment endpoints with their own shape. Any edge endpoint that genuinely matches the `(symbol, from_date, to_date, timespan, multiplier)` pattern can opt-in via a follow-up PR; none migrate in PR (ii) |
| `market_monitor`, `tickers`, `sanitize`, `snapshot`, `golden_fixtures`, `portfolio`, `broker` | Orthogonal concerns |

The discrimination rule: **a route inherits `TickerRequest` iff its primary input is "bars for symbol X over date range [from, to]".** Anything else stays.

### .NET DTOs (PR ii)

`Backend/Models/PolygonRequests.cs` (and any subordinate DTO files): `Ticker → Symbol`, `StartDate / EndDate → FromDate / ToDate` where applicable. `JsonNamingPolicy.SnakeCaseLower` already produces the snake_case wire shape.

Backend forwarders (`JobsApi`, etc.) get the rename **plus** transitional `[JsonPropertyName]` aliases on the renamed fields, so PR (iii)'s frontend changes don't have to land atomically with PR (ii):

```csharp
public sealed class FeatureResearchJobRequest
{
    [JsonPropertyName("symbol")]
    [JsonPropertyName("ticker")]   // transitional alias — removed in PR (iii)
    public required string Symbol { get; init; }
    // ... etc
}
```

The aliases are removed in the final commit of PR (iii) (the `polygon-date-range`-deletion commit).

## Data flow

Per request:

1. Picker emits a typed payload (`TickerRange` / `MultiTickerRange` / `TickerSnapshot`) into the consumer's `signal()`/`model()`.
2. Consumer calls into a service method with that payload.
3. Service method passes the payload through `tickerRangeToWire(r)` (or sibling) to obtain a `TickerRequestPayload` matching the Python schema.
4. .NET Backend (when present) deserializes into a renamed DTO, optionally augments, and forwards to Python service.
5. Python route's Pydantic model validates against `TickerRequest` base; rejected fields surface as standard FastAPI 422 with the field path.

Type safety: `TickerRange` cannot be sent over the wire **except** through the adapter — the adapter is the only function with a return type matching the Python schema, and consumer code references payload types from the adapter module.

## Migration order (inside PR iii)

Order chosen to land the lowest-friction consumers first so any picker-config bugs surface early on small surfaces:

1. **`indicator-reliability`** — cleanest fit (uses `hideSampling=true`, no multiplier, no symbol-lift). Smoke-tests the new `hideSampling` config.
2. **`strategy-preflight`** — consumes multiplier (`'5m'` → `{ resolution: 'minute', multiplier: 5 }`). Smoke-tests `availableMultipliers`.
3. **`feature-runner`** — full multiplier surface. Drops the standalone ticker input + timespan select + multiplier int.
4. **`signal-runner`** — same shape as feature-runner.
5. **`batch-runner`** — first consumer of `<app-multi-ticker-range-picker>`. Drops the chip grid (lines 36–53 of current HTML).
6. **`ticker-explorer`** — first consumer of `<app-ticker-date-picker>`. Drops the raw `<input type="date">` + raw text ticker.
7. **`spec-strategy-runner`** — frontend lift of `symbol` from spec to top-level form field; backend `SpecBacktestRequest` schema lift in lockstep. Larger commit; documented in the PR description.
8. **`indicator-report`** — template-driven `[(ngModel)]` → signal refactor + picker swap. **Split rule:** if the signal refactor touches more than the consumer's own files (e.g. modifies a service or a parent route component), or pushes the PR over ~25 changed files total, this consumer splits to PR (iv).
9. **Delete** `Frontend/src/app/shared/polygon-date-range/` + remove from imports + remove transitional .NET aliases.

PR (iii) is structured as one logical commit per consumer (matching PR #198's cadence inside a single PR), so any consumer can be reverted on its own without rewinding the others.

## Error handling

- **Pydantic validation** at the schema base level. `^\d{4}-\d{2}-\d{2}$` catches the original `2025-5-31` bug class at every inheriting route, not just the six PR #198 covered.
- **FastAPI 422** with field path bubbles to the client. The original incident (`string_pattern_mismatch` on `to_date`) showed this works; we preserve the behavior.
- **Frontend type safety** — `TickerRequestPayload` is the only acceptable wire shape; any service method whose body parameter is typed `TickerRequestPayload` cannot accept a malformed object. Consumer code references types from `utils/ticker-wire.ts`, not from picker types directly.
- **.NET DTOs** — `JsonRequired` on the renamed fields; missing fields fail at deserialization time, not at the Python boundary.
- **No silent field coercion.** If a route receives a `start_date` field after PR (iii)'s alias removal, it fails 422 with a clear "unknown field" message rather than silently mapping to `from_date`.

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
  - rejects empty `symbol`, `multiplier=0`
  - `MultiTickerRequest` rejects empty `symbols` list
- For each inheriting router, a smoke test that posts a minimal `TickerRequest` body and asserts 200 / structured 422 — most are existing tests that rerun under the new schema.
- .NET `Backend.Tests` — DTO deserialization tests for the transitional alias (`ticker` and `symbol` both accepted) + the post-alias state (`ticker` rejected with 400).

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

- **`int64 ms UTC` wire-format migration.** `numerical-rigor.md` mandates `int64 ms` at every wire boundary; today's `from_date: "YYYY-MM-DD"` violates that policy. This work *would* be the right place to fix it, but the blast radius (every consumer of every endpoint) makes conflating it with the picker initiative unsafe — neither would land cleanly. Tracked separately, cross-linked to existing audit findings F-0009 (sanitizer ISO timestamp wire), F-0019 (trade-comparison naive strptime), F-0020 (timestamp ban-list rollup), F-0021 (.NET ingestion DateTime.Parse).
- **Multi-ticker availability strip / advisories.** The `<app-multi-ticker-range-picker>` v1 omits per-ticker availability and smart advisories. Those are a separate UX problem.
- **Single-date Polygon advisories.** `<app-ticker-date-picker>` v1 has no inline Polygon advisory (consumer provides min/max). If a future consumer wants the full Polygon-aware advisory, that ships as a follow-up.
- **`indicator-report` migration**, if its template-driven → signal refactor blows up the PR (iii) scope. Splits to PR (iv).
- **Removing `polygon-date-range` from `docs/superpowers/specs/2026-05-09-polygon-date-range-design.md` history.** That doc stays as the record of what shipped in PR #198.

## Risks & open considerations

- **PR (ii) merge-order tolerance.** The transitional `[JsonPropertyName]` aliases let PR (ii) merge before PR (iii)'s frontend changes are ready. But the aliases must come *off* in PR (iii) — risk: if a consumer is missed in the migration, it'll fail at runtime after alias removal. Mitigation: PR (iii)'s last commit is "remove transitional aliases" and runs the full project-scope test suite; any consumer still sending the old field name will fail there.
- **`spec-strategy-runner` symbol-lift coordination.** The frontend form change and the backend `SpecBacktestRequest` schema change must land in the same PR. If split, runs fail mid-deploy. This is captured as a single commit in PR (iii)'s ordering.
- **PrimeNG `dateFormat="yy-mm-dd"` quirk.** Already addressed in the existing canonical picker; new siblings inherit the same `<p-datepicker>` configuration.
- **Multiplier defaults + GraphQL.** Some endpoints arrive via the .NET GraphQL API (Hot Chocolate v15 resolvers) rather than direct REST. Check `Backend/Services/*` and `Backend/GraphQL/*` for any hardcoded `multiplier: 15` or `timespan: "minute"` that conflicts with the new defaults; the implementation plan must enumerate the affected resolvers and migrate them in PR (ii) lockstep with the DTO renames.
- **Sampling card layout with multiplier dropdown.** Adding a multiplier `<p-select>` next to the existing minute/hour/daily toggle and session toggle pushes the Sampling card width. Verify (a) it stays under the existing card's wrap point at viewport breakpoints used by Engine Lab + Data Lab, and (b) AXE focus/contrast pass with the additional control. If layout fails, fall back to a small icon-button group rather than a `<p-select>`.

## Build sequence (for the implementation plan)

PR (i) — picker enhancements + new siblings:
1. Extract `_instrument.html`, `_time-window.html`, `_sampling.html` partials from `ticker-range-picker.component.html`.
2. Add `multiplier?: number` to `TickerRange`, `availableMultipliers` + `hideSampling` inputs to canonical picker (rename + widen `hideResolution`).
3. Update `ticker-range-picker.component.spec.ts` with the new flag tests.
4. Create `<app-multi-ticker-range-picker>` + types + spec.
5. Create `<app-ticker-date-picker>` + types + spec.
6. Create `Frontend/src/app/utils/ticker-wire.ts` + `ticker-wire.spec.ts`.
7. Project-scope ESLint + Vitest pass; open PR (i).

PR (ii) — Python `TickerRequest` base + .NET DTOs:
1. Create `PythonDataService/app/schemas/ticker_request.py` + `tests/schemas/test_ticker_request.py`.
2. Migrate each inheriting router one commit at a time (rename `ticker → symbol`, `start_date/end_date → from_date/to_date`); each commit re-runs the project-scope test.
3. Update .NET DTOs with renames + transitional aliases.
4. Project-scope ruff + dotnet format + dotnet test pass; open PR (ii).

PR (iii) — consumer migrations + cleanup:
1. One commit per consumer in the migration order above (eight commits).
2. One commit for `spec-strategy-runner` backend symbol-lift.
3. One commit removing transitional .NET aliases.
4. One commit deleting `Frontend/src/app/shared/polygon-date-range/`.
5. Project-scope test pass; open PR (iii). PR description lists the eight migrated consumers and the explicit "if `indicator-report` proves messy, split it" note.

## Files / folders this effort touches

**Created:**
- `Frontend/src/app/shared/multi-ticker-range-picker/` (5 files)
- `Frontend/src/app/shared/ticker-date-picker/` (5 files)
- `Frontend/src/app/shared/ticker-range-picker/_instrument.html`, `_time-window.html`, `_sampling.html` (3 partials)
- `Frontend/src/app/utils/ticker-wire.ts` + `ticker-wire.spec.ts`
- `PythonDataService/app/schemas/ticker_request.py`
- `PythonDataService/tests/schemas/test_ticker_request.py`

**Modified:**
- `Frontend/src/app/shared/ticker-range-picker/*` (additive: `multiplier`, `availableMultipliers`, `hideSampling`)
- All eight consumer components + their HTML
- All inheriting Python routers (`aggregates`, `chart`, `data_quality`, `indicators`, `indicator_reliability`, `volatility`, `dataset`, `jobs`, `engine`, `spec_strategy`)
- `PythonDataService/app/schemas/spec_strategy.py` (or wherever `SpecBacktestRequest` lives) — symbol-lift
- `Backend/Models/PolygonRequests.cs` (or equivalent) — DTO renames + transitional aliases
- `Backend/Services/*` — any hardcoded `Ticker` / `StartDate` references
- `.NET` GraphQL resolvers that pass these payloads through

**Deleted:**
- `Frontend/src/app/shared/polygon-date-range/` (final commit of PR iii)

**Untouched:**
- Options/snapshot routes (`options`, `quantlib_options`, `iv_recorder`, `iv30`, `edge`)
- `data-lab` (already adopted; defaults preserve byte-identical behavior)
- `lean-engine` (already adopted; same)
- `market-calendar` (no ticker-bar input)

## Authority hierarchy notes

This work touches three stacks:

- **Frontend** — `.claude/rules/angular.md`. Standalone, OnPush, signals + `model()`, modern control flow, no decorators, no `mutate()`. All applied.
- **.NET** — `.claude/rules/dotnet.md`. PascalCase fields, `[JsonPropertyName]` for transitional aliases (only place in DTO that uses two), no silent catches, structured logging.
- **Python** — `.claude/rules/python.md`. Pydantic v2 (`Field`, `Literal`), `from __future__ import annotations`, snake_case fields, project-scope ruff before push.

No numerical-rigor rules are triggered (no math is being ported). Timestamp-rigor is touched at the perimeter — explicitly punted to the `int64 ms` follow-up tracked above.
