# broker-v2 FIFO P&L — port note

The method, its reference and the fixture are in
`PythonDataService/app/broker/alpaca/clerk/fifo_pnl.py` and
`PythonDataService/tests/fixtures/golden/broker-v2-fifo-pnl/attribution.md`.

## Tolerance used and why

Exact fields: equality (zero tolerance).  Float display views:
`atol=1e-9, rtol=0` (a tighter pin than the accumulated-P&L default,
ADR 0069).

Justification: the reference is exact arithmetic, and so is the
implementation (#2550).  Each recorded fill quantity and price is normalized
once through `money.normalize_money`; lots, closures, open valuation and totals
accumulate in `Decimal` under `money.money_context`, where inexact arithmetic
raises.  The `exact_*` fields (`PnLResult.exact_realized_pnl`,
`PnLResult.exact_open_pnl` / `OpenPnLResult.exact_value`,
`ClosedLot.exact_realized_pnl`, `OpenLot.exact_qty` / `exact_cost`) are the
money authority (#2556).  They are what custody budgets, the simulated (Dry
Run and Shadow) account's equity, retained baseline and open P&L, and every
owner-visible P&L dollar string read: the bot page's open P&L
(`EconomicSnapshot.exact_open_pnl` through
`panel_projection_service.open_pnl_fields`) and the Activity page's Today
statement (`AccountPnlAttribution.exact_realized_pnl_total` /
`exact_start_open_pnl_total` / `exact_open_pnl_total` through
`account_activity.compose_today_statement`).  The float attributes (`qty`,
`cost`, `realized_pnl`, `open_pnl`, and the projections' `open_pnl`,
`realized_pnl_total`, `open_pnl_total`, `start_open_pnl_total`)
are those values rounded once for display, so their only error is that single
rounding — well inside the historical `1e-9` fixtures — but it can still
cross a half cent, so no displayed cent is rounded from one:
`money.display_cents` refuses anything but a `Decimal`.  The float views
still feed charts, the catalog and gallery rows, the C2 attribution API and
the C3 broker-curve comparison.  The lot-closing threshold (a lot at or below
`1e-9` shares closes) is the unchanged FIFO matching rule, now applied to
exact quantities.
