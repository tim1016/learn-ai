# Design handoff — P2.5 date-window semantics (v2)

> Superseded in implementation on 2026-05-19 for half-day policy:
> early-close NYSE sessions are now allowed trading sessions, and the
> `/api/lean-sidecar/calendar/blocked-dates` endpoint returns only
> weekends and holidays. The half-open `09:30 ET` boundary contract
> still applies.

**Replaces** `2026-05-18-design-p2-5-date-semantics.md` (kept for
history). Same approach as v1 (half-open session-boundary ms,
preserving `numerical-rigor.md`'s int64-ms-UTC rule). v2 fixes two
issues that would have shipped silent correctness bugs.

## What changes vs v1

1. **Boundary is `09:30 ET of next_trading_day(end_date)`, not
   "session-close of end_date".** Sidesteps half-day end-of-session
   ambiguity. Always 09:30 ET. Mirrors database-style `[date, date+1d)`
   ranges.
2. **Half-days are out of scope.** Validator rejects any window
   touching an early-close day. Clean failure mode; staging never sees
   a half-day-containing window.
3. **One calendar source of truth.** Validator AND staging both
   consult a new `trading_calendar` module. Drift between them is the
   bug class v1 left open.

## Contract (post-change)

- `start_ms_utc` = `09:30 ET` of `start_date`, converted to int64 ms UTC.
- `end_ms_utc` = `09:30 ET` of `next_trading_day(end_date)`,
  converted to int64 ms UTC.
- Half-open `[start, end)`.
- Validator rejects with HTTP 400 if:
  - `start_date` or `end_date` is not a trading day (weekend / holiday), OR
  - any date in `[start_date, end_date]` is an early-close day.

A one-day run on `2025-01-06` becomes `[2025-01-06 09:30 ET,
2025-01-07 09:30 ET)` (one full trading session; both bounds 09:30,
no half-day ambiguity, no 7pm-prev-day collision).

### DST contract

`09:30 ET` resolves through `pandas_market_calendars`' tz-aware
machinery. Never via fixed offset. EDT (Mar-Nov) and EST (Nov-Mar)
produce different UTC ms for the same wall-clock 09:30.

Mandatory test dates:
- **2026-03-08** — DST start, EST→EDT.
- **2026-11-01** — DST end, EDT→EST.
- Window straddling a DST boundary (e.g., 2026-03-06 to 2026-03-10).

## Calendar module

New: `PythonDataService/app/lean_sidecar/trading_calendar.py`.

Surface:

```python
def is_trading_day(d: date) -> bool: ...
def is_early_close(d: date) -> bool: ...
def next_trading_day(d: date) -> date: ...
def session_open_ms_utc(d: date) -> int: ...
```

Backed by `pandas_market_calendars` (already in
`requirements-light.txt`). `_CALENDAR = mcal.get_calendar("NYSE")`
once at module load, mirroring `nyse_calendar.py`'s pattern.

**Both the validator and the staging weekday-iteration loop import
from this module.** That is the load-bearing change.

`PythonDataService/app/engine/live/nyse_calendar.py` stays as-is —
single function, math-rigor path, its own test fixture. Don't merge:
different consumers, different test surfaces, different rate of
change.

## Files to touch

### Request validation
- `PythonDataService/app/routers/lean_sidecar.py` —
  `TrustedRunRequestModel.start_ms_utc/end_ms_utc` validators:
  - Derive `start_date`, `end_date` from input ms.
  - Compute expected `session_open_ms_utc(start_date)` and
    `session_open_ms_utc(next_trading_day(end_date))`; reject mismatch.
  - Reject on `not is_trading_day(d)` or `is_early_close(d)` for any
    `d` in `[start_date, end_date]`.
  - Error messages are operator-readable and name the offending date.

### UI conversion
- `Frontend/src/app/components/lean-lab/lean-lab.component.ts` ~669 —
  `isoDateToMsUtc` → `isoDateToSessionOpenMsUtc`. Uses
  `Intl.DateTimeFormat` with `timeZone: 'America/New_York'`. No new
  dep. Beware: `formatToParts` returns strings; assemble the UTC Date
  carefully or the DST days silently mis-convert by an hour.
- `Frontend/src/app/services/lean-sidecar.types.ts` — doc comment
  update; field stays `number`.
- **UI date picker must disable blocked dates** (weekends, holidays,
  half-days). Easiest path: backend exposes
  `/api/lean-sidecar/calendar/blocked-dates?from=&to=` that returns
  the union for a range. Single source of truth, no client-side
  duplication of the calendar.

### Staging
- `PythonDataService/app/services/lean_sidecar_service.py` ~152 —
  convert session-boundary ms back to trading dates; iterate via
  `trading_calendar.is_trading_day`; drop the `+1 day` hack.

### Manifest
- `PythonDataService/app/lean_sidecar/manifest.py` —
  `MANIFEST_SCHEMA_VERSION` bump (per D10). Add a note in `notes`:
  `date_semantics=session_open_half_open`. Old manifests stay readable
  on the prior schema_version; the reconciler reads the note to know
  which contract applies.

### Cross-reconciler
- `PythonDataService/app/lean_sidecar/cross_reconciler.py` — no logic
  change (it derives NY trading date from each bar's ms). Add a test
  asserting `requested_window_ms` is now directly comparable to the
  derived per-bar trading dates.

### Phase 5b reconciliation template
- The hardcoded `2025-01-06`–`2025-01-10` template needs
  `start_ms_utc` / `end_ms_utc` recomputed under the new contract.
  Full trading days, no half-days, no DST — value-only update, no
  semantic risk.

## What this PR does NOT do

- **No wire-type change.** `start_ms_utc` / `end_ms_utc` stay `int`.
  Semantic shift only.
- **No old-manifest migration.** Old runs stay readable on the prior
  `schema_version`. Re-runs produce new-schema manifests.
- **No half-day support.** Validator rejects, UI disables. If half-day
  support is needed later, `is_early_close` is already in the
  calendar module; validator just relaxes the rejection. Track in
  `docs/math-sources-of-truth.md` under `Status: deferred`.
- **No merge of `nyse_calendar.py` into `trading_calendar.py`.**

## Risks / things to watch

- **`Intl.DateTimeFormat` assembly is easy to get wrong.** The
  `formatToParts` recipe must produce a UTC Date that matches what
  pandas computes. UI test surface MUST include the same DST dates
  as the Python test surface.
- **Validator-before-staging invariant.** A rejected request never
  hits staging. If a code path inverts this (e.g., staging-driven
  validation as part of a refactor), the half-day rejection becomes
  silent corruption.
- **Frontend blocked-dates endpoint is the calendar-drift seam.**
  Cache it sensibly; don't let it become a per-pixel API call.

## Test surface (mandatory)

- Full-day window, all trading days → accept.
- Window touching a weekend → reject, message names the weekend day.
- Window touching a US federal holiday → reject, message names the holiday.
- Window touching a half-day (e.g., 2026-11-27 Black Friday) →
  reject, message names the half-day.
- DST start window (2026-03-08 inside range) → session-open ms
  reflects EDT.
- DST end window (2026-11-01 inside range) → session-open ms reflects
  EST.
- Round-trip: client sends, server validates, manifest persists,
  reconciler reads — assert all four agree on the trading-date set.

## Independence

No upstream PR dependencies. Single PR. Follow-up doc update to the
ADR's "Date-window and bar-consumption" section is expected but can
land separately.
