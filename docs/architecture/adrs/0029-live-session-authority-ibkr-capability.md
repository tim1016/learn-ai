# ADR 0029: Live Session Authority Extends Calendar With IBKR Capability

- **Date:** 2026-07-12
- **Status:** Accepted
- **Context:** Issue #1005 Slice 1

## Decision

`pandas_market_calendars` remains the source of truth for NYSE regular-hours scheduled structure and backtest reproducibility.

IBKR capability snapshots become the live per-instrument authority for extended-session structure when they are available. The live session authority asks, for one symbol and one `int64 ms UTC` instant, which phase the instrument is in: `PRE`, `RTH`, `POST`, `OVERNIGHT`, or `CLOSED`.

The operator surface must consume this authority instead of deriving PRE/POST from local hard-coded clock windows. If no matching IBKR capability snapshot exists, the authority falls back to the offline NYSE calendar so existing RTH-only behavior is preserved.

## Rationale

ADR 0022 made the calendar authoritative for scheduled time boundaries. That remains correct for historical backtests and regular-hours parity. It is incomplete for autonomous extended-hours trading because live reachability is instrument- and account-dependent. IBKR publishes `tradingHours`, `liquidHours`, `timeZoneId`, and valid venues for the contract the broker will actually trade.

The split is therefore:

- Calendar: deterministic regular-hours schedule and backtests.
- IBKR capability: live per-instrument extended-session windows, if probed and persisted.
- Parity requirement: where both claim RTH for a scheduled regular-hours day, they must agree.

## Consequences

The first implementation is intentionally restrictive:

- Strategy activity still defaults to RTH-only.
- The authority can expose `OVERNIGHT`, but it does not enable overnight submissions.
- Missing capability data does not create new behavior; it falls back to the prior calendar-backed state.
- Session-gated execution remains Slice 2.

Future slices can use the same authority for pre-submit gates, order construction, bar provenance, and 24-hour lifecycle rules.
