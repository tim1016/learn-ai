# deployment_validation session-window boundaries

Formula: `deployment_validation`'s decision window —
`detection_start_ms = session_open_ms_utc(d) + 15min`,
`stop_and_flatten_ms = session_close_ms_utc(d) - 15min`.

Reference: NYSE's published regular-session hours (09:30-16:00 ET) and its
day-after-Thanksgiving early-close hours (09:30-13:00 ET), as encoded in the
canonical calendar module.

Canonical implementation:
`PythonDataService/app/engine/strategy/algorithms/deployment_validation.py::_session_decision_window_ms`.

Validated against:
`PythonDataService/tests/engine/test_deployment_validation_session_window_parity.py`.

## Generation

Generated 2026-08-19 by calling
`app.lean_sidecar.trading_calendar.session_open_ms_utc` /
`session_close_ms_utc` (the single canonical NYSE-calendar source of truth
per `.claude/rules/temporal-rigor.md`) inside the `polygon-data-service`
container:

```
podman exec polygon-data-service python -c "
from datetime import date
from app.lean_sidecar.trading_calendar import session_open_ms_utc, session_close_ms_utc
for d in [date(2026, 1, 5), date(2024, 11, 29)]:
    o = session_open_ms_utc(d)
    c = session_close_ms_utc(d)
    print(d, o, c, o + 15 * 60 * 1000, c - 15 * 60 * 1000)
"
```

`2026-01-05` is an ordinary Monday (regular 09:30/16:00 ET session, no
early close). `2024-11-29` is the day after Thanksgiving, NYSE's most
common recurring early-close day (09:30/13:00 ET) — the #1672 regression
case.

## What this fixture proves, and what it does not

This is an **internal self-consistency fixture**, not an external-oracle
golden fixture — `deployment_validation` has no external reference to port
from (see the "No external golden fixture" note in
`docs/references/deployment-validation-consecutive-green.md`). It pins, at
tolerance 0:

1. The canonical Python kernel's `_session_decision_window_ms` output for
   both dates.
2. That the QC shadow copy's `_session_window` formula
   (`GetNextMarketOpen(...) + DETECTION_START_OFFSET`,
   `GetNextMarketClose(...) - STOP_AND_FLATTEN_OFFSET`, extracted from its
   source via `ast`) reproduces the same two boundary pairs when applied to
   this fixture's `session_open_ms_utc` / `session_close_ms_utc`.
3. The same for the LEAN Sidecar Lab trusted template's embedded
   `_session_window` (extracted via `ast` from
   `DEPLOYMENT_VALIDATION_SOURCE`).

It does **not** prove that QuantConnect LEAN's own `Exchange.Hours`
calendar actually returns these same open/close instants at runtime — that
requires executing the QC shadow copy / LEAN template inside a real LEAN
sandbox, which this fixture does not do. That gap is the same one already
tracked in `docs/references/deployment-validation-consecutive-green.md`
("Cross-engine reconciliation fixtures can be added later once this
template is included in the parity matrix") and is not new to this fixture.
What *is* new here is proof that all three implementations compute the
window with the identical formula and offsets given the same inputs, so a
future edit that silently drifts one copy's arithmetic (wrong sign, wrong
offset, offset applied to the wrong endpoint) fails this fixture's parity
test immediately instead of only surfacing after a LEAN Cloud backtest.
