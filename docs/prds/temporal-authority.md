# PRD — Temporal authority: one `int64 ms UTC` representation, calendar as source of truth, and a shared timestamp display component

> Decision record: `docs/architecture/adrs/0022-temporal-authority-calendar-and-timestamp.md`.
> Standing law: `.claude/rules/temporal-rigor.md` (peer of `numerical-rigor.md`).

## Problem Statement

As a developer and operator on learn-ai, I cannot trust what a timestamp *means* as it crosses the app. The same instant is `int ms` on REST endpoints, an ISO string on the GraphQL surface, Unix seconds on chart inputs, and a `.NET DateTime` in some DTOs — four wire formats for one concept. Option expiries render a day early for users west of UTC. "Is the market open?" is answered from nine different places: two calendar modules plus seven files that hardcode `09:30`/`16:00` and silently get early-close half-days wrong. There is a written `int64 ms UTC` rule, but it lives buried in a math document and is violated in ~22 places, so nobody treats it as law. The result is a class of quiet, recurring bugs (off-by-one dates, off-by-one-hour DST, half-day misclassification) that erode confidence in every number the app shows.

## Solution

Establish a single temporal authority and make the whole app obey it.

- **One representation.** Every temporal value in flight, at rest, or on the wire is `int64 ms UTC`. Values that are semantically a date or a wall-clock boundary (option expiry, trading date, session open) are still one `int64 ms UTC` field, constructed at a defined ET session anchor — no second date type.
- **One calendar.** All scheduled session structure (trading days, session open/close, early closes, alignment) derives from a single canonical calendar module. No hardcoded session times and no direct `mcal.get_calendar("NYSE")` constructors outside the canonical module. Real-time liveness ("is it halted right now?") stays out of scope, owned by the live feed, which must agree with the calendar on scheduled days.
- **One display component.** A shared, reusable Angular component takes an `int64 ms UTC` input and renders it in an explicit mode — `local`, `et`, or `date-et` — so the mode, not the viewer's timezone, decides the output and date-anchored values never drift a day.

The authority is captured in ADR 0022 and `.claude/rules/temporal-rigor.md`, and enforced across all three stacks via a destructive rip-and-replace (the app is local-only, no production, no back-compat).

## User Stories

1. As a developer, I want a single documented rule for how time is represented, so that I never have to guess whether a field is ms, seconds, an ISO string, or a `DateTime`.
2. As a developer, I want every timestamp on every wire and store to be `int64 ms UTC`, so that a value means the same thing in Python, .NET, and Angular.
3. As a developer, I want the ingestion boundary to snap each external time (Polygon, IBKR, FRED) to the closest constructible `int64 ms UTC` immediately, so that the vendor's string never leaks downstream.
4. As a developer, I want date-anchored values (option expiry, trading date) stored as `int64 ms UTC` at a defined ET session anchor, so that "the closest timestamp" is unambiguous.
5. As an operator, I want option expiries to display the correct calendar day regardless of my machine's timezone, so that I never see an expiry that is a day early.
6. As an operator, I want session-relative times (open, close, event timeline) shown in ET when the context is exchange-aligned, so that wall-clock times mean what a trader expects.
7. As an operator, I want ordinary instants (fills, trades, incidents) shown in my own local timezone by default, so that "when did this happen" matches my wall clock.
8. As a developer, I want one shared Angular timestamp component used everywhere, so that I stop copy-pasting `formatLocalTimestamp` / `fmtTimestampNy` / `DatePipe` variants into every template.
9. As a developer, I want the component to accept an explicit display mode (`local` / `et` / `date-et`), so that the caller declares intent instead of relying on the viewer's zone.
10. As a developer, I want the component to be style-agnostic and composable (content projection where a caller needs to wrap or decorate the rendered value), so that it fits any surface without forking it.
11. As a developer, I want a backing pipe for inline text cases, so that simple template bindings don't need a full component element.
12. As a developer, I want a single canonical calendar module, so that "is this a trading day / when does the session open or close" has exactly one answer.
13. As a developer, I want the seven hardcoded `time(9,30)`/`time(16,0)` sites removed, so that early-close half-days are handled correctly everywhere instead of silently mis-filtered.
14. As a quant, I want session boundaries to come from `pandas_market_calendars` via the canonical module, so that DST and half-days are correct without me thinking about them.
15. As a quant, I want the calendar to answer scheduled questions deterministically for any instant, past or future, so that backtests are reproducible and forward windows are valid.
16. As an operator, I want "is the market open right now" to reflect an unscheduled halt, so that the live surface tells the truth even when the static calendar cannot.
17. As a developer, I want the calendar-vs-live-feed boundary written down, so that no one later "consolidates" them and reintroduces a lying live surface or an unreproducible backtest.
18. As a reviewer, I want the two collapsed calendar modules to be provably equivalent (parity test) where any adapter survives, so that the collapse doesn't silently change behavior.
19. As a maintainer, I want `pandas_market_calendars` version-pinned, so that a calendar upgrade is a deliberate, tested event, not silent drift.
20. As a developer, I want the GraphQL surface to expose timestamps as numeric ms, so that the frontend stops parsing ISO strings and charts stop dividing by 1000 through `new Date(iso)`.
21. As a developer, I want Python response models to carry `int` ms timestamps instead of `.isoformat()`/`.strftime()` strings, so that the .NET and Angular consumers receive one format.
22. As a developer, I want .NET DTO timestamp fields typed `long`, so that Hot Chocolate stops serializing `DateTime` as ISO strings on the wire.
23. As a developer, I want the frontend GraphQL types regenerated/retyped to `number` for timestamp fields, so that the type system reflects the true wire contract.
24. As a developer, I want a CI-grep ban list covering the disallowed temporal patterns (naive `datetime`, `DateTime.Parse`, `new Date(string)`, hardcoded `time(9,30)`, extra `mcal.get_calendar`), so that regressions are caught mechanically.
25. As a developer, I want the temporal rule to be a named peer of the math rule in `CLAUDE.md`, so that its authority is obvious at the top of the repo.
26. As an operator, I want the rendered timestamp string to be display-only (never stored, sent back, or compared), so that a formatting choice can never corrupt a stored value.
27. As a developer, I want backend-authored trader/operator prose about time to arrive from the backend, so that the component renders values, not sentences.

## Implementation Decisions

**Authority (already landed in this branch):**
- ADR 0022 records the scheduled-vs-liveness boundary and the module collapse.
- `.claude/rules/temporal-rigor.md` is the standing law; `numerical-rigor.md` § Timestamp rigor is a stub pointer; `CLAUDE.md` guiding-philosophy #6 and the stack-rules list name it as a peer authority.

**Slice A — shared Angular timestamp display component:**
- A single standalone, `OnPush`, signal-based component takes an `int64 ms UTC` input and a display mode input (`local` | `et` | `date-et`), plus an optional format/granularity input (date, time, datetime) and content projection for callers that decorate the rendered value.
- A backing pure pipe covers inline text bindings; the component and pipe share one formatting core so there is one implementation of "ms + mode → string".
- The existing scattered helpers (`formatLocalTimestamp`, `formatLocalClock` in `utils/local-timestamp.ts`; `fmtTimestampLocal`, `fmtTimestampNy`, `fmtDateNy` in `broker/format.ts`) collapse into the shared core; call sites and the ~10 `DatePipe` templates repoint to it.
- `local` = viewer zone (instants). `et` = `America/New_York` with an ET marker (session/trading context). `date-et` = date-only resolved in ET, never shifts a calendar day (expiry, trading date).

**Slice B — Python calendar hard collapse:**
- One canonical calendar module owns all session math. It absorbs `nyse_calendar.py`'s two functions as additive helpers (`session_close_ms_utc`, `session_state_at_ms`, `previous_completed_session_close_ms`) alongside the existing `is_trading_day`, `session_open_ms_utc`, `is_early_close`, `session_close_minute_et`, `next_trading_day`, `expected_sessions`, `blocked_dates_in_range`.
- `engine/live/nyse_calendar.py` is deleted; its one consumer (indicator-state hydrate-validation) repoints to the canonical module.
- The seven hardcoded-session-time files (`spy_orb.py`, `operator_surface.py`, `polygon_ingest.py`, `tv_ingest.py`, `engine_runner.py`, `derived_daily.py`, `run_spy_partial_parity.py`) drop their `time(9,30)`/`time(16,0)` literals and call the canonical module, gaining correct early-close handling.
- Direct `mcal.get_calendar("NYSE")` users collapse into the canonical module too: `chart_service.py`, `dataset_service.py`, `data_quality_service.py`, `volatility/basis.py`, `research/runs/window.py`, `lean_sidecar/trading_calendar.py`, and `engine/live/nyse_calendar.py`. Any surviving adapter must carry a parity test naming the canonical module.
- Only the canonical module constructs `mcal.get_calendar("NYSE")`. `pandas_market_calendars` is version-pinned in `requirements-light.txt`.
- Real-time liveness (Polygon `get_market_status`) is untouched and explicitly out of scope; it is not a violation.

**Slice C — wire-format migration (destructive, cross-stack):**
- .NET: GraphQL/DTO timestamp fields (`AggregateBar.Timestamp`, `*.CreatedAt`, `BacktestTradeType.Entry/ExitTimestamp`, and peers) move from `DateTime`/`string` to `long` ms; `.ToString("o")` conversions removed; ingestion continues to accept numeric `long`.
- Python: response-model and dict-building code stops emitting `.isoformat()`/`.strftime()` for timestamps (`engine.py` equity_curve and `TradeRecord`, and the other flagged sites); timestamp fields are typed `int` ms.
- Angular: GraphQL types for timestamp fields become `number`; chart adapters consume ms directly instead of `new Date(iso).getTime()/1000`; all rendering flows through the Slice A component.
- No compatibility shims; the format flips in one destructive pass per surface.

## Testing Decisions

Good tests here assert **external behavior at the highest seam**, not internals: given an input, assert the rendered string or the returned ms — never a private signal or an intermediate `pd.Timestamp`.

- **Slice A — component + pipe.** Seam: rendered output. Use Angular Testing Library `render()` + `screen` to assert the displayed string for representative `(ms, mode)` pairs, including the `date-et` off-by-one guard (a value that would drift a day in a west-of-UTC local zone must not drift). Pin the test zone to `America/New_York` for determinism, as `utils/local-timestamp.ts` tests already do. Pure-pipe cases get a direct function test. Prior art: existing `*.component.spec.ts` and the local-timestamp util tests.
- **Slice B — canonical calendar.** Seam: the module's public functions. Golden-fixture + parity tests proving the folded `session_state_at_ms` / `previous_completed_session_close_ms` match the deleted module's prior behavior, plus an explicit **early-close regression test** (a half-day session must produce the correct close boundary — the bug the seven hardcoded sites had). Assert `int64 ms UTC` return dtypes and a DST-boundary case (EDT vs EST open ms differ). Prior art: existing calendar tests and `tests/fixtures/golden/`.
- **Slice C — wire contract.** Seam: the HTTP/GraphQL boundary. FastAPI endpoint tests via `httpx.AsyncClient` + `ASGITransport` assert timestamp fields arrive as JSON numbers; Hot Chocolate resolver tests via `IRequestExecutor` assert `long`/number-typed timestamp fields in the schema and responses. Prior art: existing endpoint and resolver contract tests.
- **CI grep guard.** A ban-list check (mechanical) for the disallowed patterns in `temporal-rigor.md`, so representation regressions fail fast. The guard has explicit allowlists for tests, generated EF migrations/model snapshots, docs/reference fixtures, and documented adapter exceptions; every allowlist entry names why the pattern is permitted.

## Out of Scope

- Real-time market liveness / halt detection. The live broker/vendor feed owns "is it open this second"; the calendar is not extended to synthesize halts.
- Any change to backtest math, fill models, or commission logic — this is representation, calendar sourcing, and display only.
- Backend-authored operator/trader prose about time; the component renders values, not sentences.
- Migrating non-timestamp date arithmetic that is already correct (e.g. weekday helpers) beyond repointing them at the canonical calendar where they duplicate it.
- Introducing a new calendar for non-NYSE exchanges; the canonical module remains NYSE until a second exchange is actually needed.

## Further Notes

- The three slices are independently shippable and should be separate PRs, each with its own thermo-nuclear review + project-scope lint + the relevant test surface. Recommended order: A (self-contained, the literal ask, unblocks C's frontend half) → B (fixes the half-day bug) → C (the large destructive cross-stack flip).
- The migration is destructive by explicit decision (local-only app, no production, no back-compat) — no shims, no dual-format transition window.
- `pandas_market_calendars` is currently unpinned; pinning it is part of Slice B and a prerequisite for the calendar being a stable single point of truth.
