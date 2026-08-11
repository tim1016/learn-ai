# GOOGL execution-slice round trip

## Source

Synthetic Alpaca `trade_updates` frames authored for PRD #1441, S0.3. The
wire shape is the adapter contract in
`app/broker/alpaca/adapter.py::from_alpaca_trade_update`: every execution
slice carries its own top-level `execution_id`, `price`, and `qty`; order
`filled_qty` and `filled_avg_price` remain cumulative evidence only.

## Fixture date

2026-08-10.

## Generation and assumptions

This is a deterministic acceptance fixture, not a live-account capture. It
models two BUY execution slices (0.4 and 0.6 GOOGL at $354.81) and one SELL
execution slice (1.0 GOOGL at $355.34), all inside the 2026-08-10 NYSE session.
The expected $0.53 realized P&L is `(355.34 - 354.81) * 1.0`; the bot is flat,
so open P&L is exactly $0.00 and marks are complete. The economic close is one
GOOGL round trip while all three execution slices remain separately auditable.

The fixture is the S1/S2 acceptance oracle. It is not regenerated from a
cumulative order snapshot; any change requires a PRD-reviewed re-derivation
from per-execution websocket evidence and an update to this attribution.

## Tolerance

`atol=1e-6, rtol=0` for P&L; all counts and execution identities are exact.
