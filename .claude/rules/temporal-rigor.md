# Temporal rigor rules

The core scientific standard for **time** in this repo — representation, the trading calendar, and display. This is a peer of `numerical-rigor.md`: as authoritative for time as that document is for math. Read before any work that ingests, stores, transmits, computes with, or displays a timestamp, a trading date, or a market session.

Decision record: `docs/architecture/adrs/0022-temporal-authority-calendar-and-timestamp.md`.

Timestamp handling is the single largest source of divergence in backtesting. The rules are strict and there are no exceptions inside the scope defined below.

## Scope — what this rule governs, and what it does not

This authority governs **two** things:

1. **Representation.** How a temporal value is stored, transmitted, and serialized.
2. **Scheduled session structure.** Trading days, session open/close, early closes, bar alignment, warmup — anything derivable from the exchange calendar, deterministically, for any instant past or future.

It explicitly does **not** govern:

- **Real-time market liveness.** "Is the market open *this exact second*?" — which must reflect unscheduled halts, circuit breakers, emergency early closes — is answered by the live broker/vendor feed, not the calendar. A static calendar physically cannot know a real-time exception. The live feed **must agree with the calendar on scheduled days** and may diverge only on real-time exceptions; when it does, the live signal wins for operational decisions and the calendar wins for backtest reproducibility. See "Calendar authority" below.

## Canonical representation

**Every temporal value in flight, at rest, or on the wire is an integer count of milliseconds since Unix epoch UTC (`int64 ms UTC`).** No exceptions. ISO strings, `datetime` / `DateTime` objects, tz-aware ISO-with-`Z`, and naive datetimes are all **disallowed as wire and storage formats**. Language-native types are allowed only for arithmetic within a single function; they must be converted back to `int64 ms UTC` before returning, writing, or serializing.

Rationale: four different wire formats were in flight before this rule (`int ms`, naive-ISO-with-lying-`Z`, `"YYYY-MM-DD HH:MM"` that parses as local in the browser, .NET `DateTime` with `Kind=Local`-by-accident). See `docs/audits/computational-fidelity-2026-04-22.md` § 2 and its addendum § 3.

### Ingest to the closest constructible instant

When you consume a time from an external API (Polygon, IBKR, FRED), snap it to the **closest `int64 ms UTC` you can construct** for that value, at the ingestion boundary, and store that. The closest constructible instant is the most accurate durable representation — never keep the vendor's string.

### Date-anchored and wall-clock values

Some values are semantically a **date** (option expiry `2026-06-19`, a "trading date") or a **wall-clock session boundary** (`09:30` open on some date), not a point-in-time. They are **still one `int64 ms UTC` field** — there is no second date type. Construct them at a **defined ET session anchor** so the instant is unambiguous:

- **Option expiry** → 16:00 ET of the expiry date.
- **Trading date** → the session open (09:30 ET) of that date, unless a surface states another anchor.
- **Session boundaries** → the calendar's actual `market_open` / `market_close` for that date (respects half-days).

"The closest Unix timestamp" is ambiguous for a bare date — *closest to which instant, midnight where?* The ET session anchor removes the ambiguity and, paired with the display modes below, prevents the date from drifting a calendar day at render time.

## Two and only two conversion boundaries

1. **External-API ingestion.** Parse → `int64 ms UTC` immediately on receipt. Validate monotonicity and uniqueness at the same point and **fail fast** on violations: reject any duplicate timestamp and any non-strictly-increasing sequence with a descriptive error. Do not silently repair the feed (no `drop_duplicates`, no forward-fill, no reordering) — duplicates and gaps are signals about upstream corruption and must surface, not be masked. Everything downstream consumes `int64 ms`.
2. **UI rendering.** `int64 ms UTC` → a display string, via the shared display component (see "Display" below). The display-side string is never stored, never sent back to a server, never compared against another timestamp.

No other place in the codebase converts timestamps for wire, storage, or serialization. Transient in-function timezone conversion for wall-clock semantics (see "Classical rules") is allowed, provided the result is not persisted and is converted back to canonical `int64 ms UTC` before return, write, or serialize.

### Finite ingestion vs. live subscriptions

The fail-fast rule above governs **finite** ingestion — a historical fetch is a closed dataset where a duplicate or gap *is* upstream corruption, so it must halt. An **active broker subscription** is a different boundary: a long-lived stream (e.g. IBKR `reqRealTimeBars`) can legitimately *redeliver* the bar it most recently sent, and the vendor does not contractually promise duplicate-free delivery. For these and only these live subscriptions, a redelivery of the most-recently-accepted element may be absorbed **idempotently** — but it must be **surfaced, never silenced**: logged with a structured `action` and incremented on an observable counter, exactly like the fail-fast path emits an error. The relaxation is narrow:

- Exact redelivery (same timestamp, same payload) → skip; do not double-count.
- Same timestamp, *different* payload, before the aggregate it feeds is emitted → treat as a correction; recompute the open aggregate from its stored parts (never fold-and-sum).
- Any timestamp belonging to an *already-emitted* aggregate (i.e. `< last_accepted`) → still **fatal**; downstream has already consumed a now-stale value. Non-monotonic-within-the-open-aggregate stays fatal too until a real feed demonstrates otherwise.

Reference implementation: `app/broker/ibkr/bars.py` (`policy="strict"` is the finite default; `policy="live_idempotent"` is the subscription relaxation). Silent `drop_duplicates`/forward-fill/reorder remains banned in both modes — absorbing a redelivery is not the same as repairing a feed.

## Calendar authority

**One canonical calendar module is the sole source of truth for scheduled session structure.** Backed by `pandas_market_calendars` (NYSE), which must be **version-pinned**. Everything the module exposes — `is_trading_day`, `session_open_ms_utc`, `session_close_ms_utc`, `is_early_close`, `session_close_minute_et`, `next_trading_day`, `expected_sessions`, `blocked_dates_in_range`, `session_state_at_ms`, `previous_completed_session_close_ms` — returns `date` or `int64 ms UTC`. Internal tz-aware `pd.Timestamp` is fine; it must not escape.

- **No hardcoded session times.** `time(9, 30)`, `time(16, 0)`, or any literal RTH boundary in market/session logic is banned. Derive from the canonical module. Hardcoded times silently mishandle early-close half-days — a correctness bug, not a style nit.
- **One `mcal.get_calendar("NYSE")` in the repo.** Only the canonical module constructs the calendar. Every other consumer imports the module.
- **DST via the NY zone, never a fixed offset.** Wall-clock → UTC ms conversions go through `ZoneInfo("America/New_York")`. A fixed `-05:00`/`-04:00` is a silent one-hour bug across a DST boundary.
- **Duplicates need a parity test.** If a thin adapter must exist for a real reason (latency, layer-locality), it carries a parity test naming the canonical file, per CLAUDE.md guiding-philosophy #5.

## Display

`int64 ms UTC` reaches the UI and is rendered by the **shared timestamp display component**. The component takes the ms value and an explicit **display mode**; the mode — not the viewer's timezone — decides the output:

- **`local`** (default) — the viewer's local timezone. For instants: trades, fills, bar closes, event timelines.
- **`et`** — `America/New_York` with an ET marker. For session/trading contexts where the wall-clock must be exchange-aligned (session open/close, an operator's market-time view).
- **`date-et`** — date only, resolved in ET, **never shifts a calendar day**. For date-anchored values: option expiry, trading date.

Rules:

- Formatting lives in the shared component (or its backing pipe). Do not scatter `DatePipe`, `Intl.DateTimeFormat`, or `toLocaleString` through feature templates.
- Never render a date-anchored value in `local` mode — west of UTC it drifts to the previous day. That is what `date-et` exists to prevent.
- The rendered string is display-only: never stored, never returned to a server, never compared.
- Backend-authored operator/trader prose about time is not re-derived on the client; it arrives from the backend. The component renders *values*, not sentences.

## Ban list (CI-enforceable with grep)

- `datetime.utcnow` — deprecated in Python 3.12; use `datetime.now(UTC)` at the ingestion boundary, then immediately convert to ms.
- `datetime.utcfromtimestamp` — same.
- `datetime.now()` without a `tz=` argument — timezone-ambiguous.
- `pd.to_datetime(...)` without `utc=True` — produces naive objects that lie when `.strftime("...Z")` is appended.
- `DateTime.Parse(...)` in any timestamp-canonicalization path — **disallowed**. Naive strings parse as `Kind=Unspecified`, and `DateTimeStyles.AssumeUniversal | DateTimeStyles.AdjustToUniversal` **silently coerces** them to UTC. For string input, require `DateTimeOffset.ParseExact` (or `DateTime.ParseExact`) with `CultureInfo.InvariantCulture` and an explicit offset designator; reject ambiguous/naive strings. Prefer: accept timestamps as numeric `long` (ms since epoch) and skip string parsing entirely.
- `new Date(string)` in TypeScript where the string is not an ISO-8601 with tz designator — parses as local in Chrome/Safari, `Invalid Date` in Firefox. If the input is a timestamp, it should have been typed `number` and passed directly.
- `string` or `DateTime` typing on any field literally named `timestamp` / `ts` / `time` / `*_at` / `*At` in a GraphQL DTO, C# DTO, Pydantic model, or TS interface. The type is `long` / `int` / `number`.
- `time(9, 30)` / `time(16, 0)` (and equivalent `datetime.time` / minutes-past-midnight literals) in market/session logic — derive from the canonical calendar module.
- `mcal.get_calendar(...)` outside the canonical calendar module — import the module instead.

## Classical rules (kept)

- **All logic operates in `America/New_York`** when wall-clock semantics matter (session filters, exchange-aligned bar starts). The conversion is per-operation, never persisted.
- **Bar timestamp = bar close.** A bar labeled `09:45:00` contains trades from `09:30:00` (inclusive) to `09:45:00` (exclusive).
- **Never forward-fill or interpolate to align.** If two series have different timestamps, that's data telling you something — don't silence it.
- **Bar alignment is explicit.** A 15-min bar starts at an exchange-aligned minute (`:00`, `:15`, `:30`, `:45`). A bar that starts at `:07` is wrong.

## Anti-patterns to reject

- ISO-string, `DateTime`, or naive-`datetime` as a wire or storage format for any temporal value.
- A second "date" type for date-only values instead of an ET-anchored `int64 ms UTC`.
- Rendering a date-anchored value in the viewer's local zone (the option-expiry off-by-one-day bug).
- Hardcoded `09:30`/`16:00` session boundaries; a second `mcal.get_calendar` call.
- Routing "is the market live right now?" through the static calendar (it cannot see halts), or routing "was instant T a trading minute?" through the live feed (it cannot see the past/future schedule).
- Silent timezone conversions mid-pipeline; fixed-offset ET conversions across a DST boundary.
- `DatePipe` / `Intl.DateTimeFormat` scattered through feature templates instead of the shared display component.
- Any ban-list item above.
