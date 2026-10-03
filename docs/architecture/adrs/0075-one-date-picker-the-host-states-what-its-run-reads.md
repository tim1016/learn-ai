# ADR 0075 — One shared date picker; the host states what its run can read

**Status:** Accepted 2026-10-03
**Provenance:** Owner decisions in [#2858](https://github.com/tim1016/learn-ai/issues/2858), grilled on 2026-10-03, and the corrections posted there while it was built.
**Related:** ADR 0022 (the calendar is the authority; time on the wire is `int64 ms UTC`), ADR 0066 (the shared symbol picker).

## Context

Dates were picked seven different ways. Most pages used the browser's own date field, and it failed in three ways:

- It reports each keystroke of a half-typed year as a whole date. On the Golden Search plan, typing 2022 ended at 1922 and the dates could not be set at all.
- It takes any day. A quick range counted calendar days back from the viewer's today, whatever the page's data held, and snapped to a weekday, so a holiday slipped through.
- It looks different in every browser.

The browser also had no way to read trading sessions: the only calendar route answered "next trading day after X".

## Decision

### 1. Two shared controls, and no raw date field

`app-date-range-picker` picks a range and `app-date-field` picks one date. Both live in `Frontend/src/app/shared/date-picker/`. No template may use `<input type="date">`; `raw-date-field.contract.spec.ts` fails if one does. PrimeNG's own table column filter is the one other date control: it opens the same calendar.

### 2. The value is a calendar date; the host converts it

The pickers hand hosts `YYYY-MM-DD` calendar dates, the form a person types and reads. Each host builds what its endpoint takes at its own HTTP call.

Why not ms in the picker: four anchors are on the wire (noon UTC for the Data Lake, ET midnight with a half-open end for Grid Search and Golden Search, the 09:30 session open for the LEAN sidecar, UTC midnight for Data Lab). One ms value cannot serve them. Some older endpoints still take the date itself (Research Lab's `from_date` and `to_date`). Unifying the endpoints is not decided here.

A single date is a plain calendar day whose zone is the host's to say: a trading date is an ET date, a bot's end date is the viewer's own.

A date held as a string in a page's form state is a deviation from ADR 0022 (a), which has no date type beside `int64 ms UTC`. ADR 0022 (f) names it. It covers the form state only, not what any endpoint takes.

### 3. The host states the bounds

Only the host knows what its run reads, so it hands the picker its bounds (`DateBounds`). Two bounds cover every page today:

- **Provider.** The run fetches its own bars, or materializes missing days before it reads. Dates run from the oldest day the provider serves (`GET /api/data-lake/backfill-defaults`) to the last completed session.
- **Held span.** The run reads the lake as it stands. Dates run from the first to the last day the lake holds for the instrument.

`app-ticker-range-picker` takes its bound as a required input, so no page inherits one by default. Research Lab's runs fetch from the provider even though their instrument picker shows a held span; a default would have got that wrong.

### 4. Typed entry

- `YYYY-MM-DD` only.
- A typed date applies on Enter or on leaving the field, never per keystroke.
- An entry that is not a usable date stays in the field with the reason underneath, and the host receives an empty date, so its run stays disabled. Nothing is corrected silently.

### 5. Quick ranges

- The list is `1D 1W 1M 3M 6M YTD 1Y 2Y 5Y Max`. A page may leave ranges out. It never adds its own labels.
- A range ends on the last day the page's run can read and is exactly its label, both ends included: 1Y ending 2026-09-18 starts 2025-09-19. It is therefore a whole number of months, which the fold planners require.
- Where dates must be sessions, the start moves forward to the next session.
- A range the data cannot serve stays in place, greyed, and says why.
- A range is shown as active only when the dates equal exactly what its click would set.
- A host whose ranges are resolved on the server (Data Lab's end on the session still forming) hands them to the picker, which shows them under the shared labels.

### 6. The calendar comes from the data plane

`GET /api/calendar/sessions` projects the canonical calendar (ADR 0022). The browser keeps no calendar of its own. When the read fails, the picker says so and offers a retry, and typed dates go unchecked: the servers that need a session boundary refuse a closed day themselves. Failing closed would disable every run each time the frontend is deployed ahead of the data plane.

### 7. Gaps inside a held span

A held span is a first and a last day, not coverage. On pages bounded by one, the picker marks the sessions the lake lacks in the calendar and counts them under the dates (`GET /api/data-lake/coverage`). The range stays selectable; the page's run decides what a gap means.

The route reports minute bars, so only a run on minute bars is shown gaps. A daily run reads the symbol's daily history, which no route reports on per session; it is shown none rather than the minute bars' gaps.

The gaps are read again whenever the tree's holdings are read again.

### 8. Layout, and the instrument picker beside it

The instrument and the dates are two controls. A page whose state is a `TickerRange` uses `app-ticker-range-picker`. Any other page puts the shared instrument picker first and the shared date control beside or under it.

The instrument picker has one field height with the date fields (40px), shows the symbol at 14px with no text under 12.8px, is at least 280px wide where its container allows, and carries one label: a page does not print its own above it.

## What this does not decide

- One ms anchor for every endpoint.
- A bound for a multi-symbol page that reads the lake as it stands. None exists; the one multi-symbol page fetches from the provider.
- Whether a gap should stop a run. That stays with each page's run.
