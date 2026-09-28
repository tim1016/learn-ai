# PNL-001 — Alpaca account-day P&L

## Source

Alpaca defines `last_equity` as equity on the previous trading day and exposes
signed account-activity `net_amount` values for cash deposits (`CSD`) and cash
withdrawals (`CSW`). The governing citations are the Alpaca Account Object and
Account Activities documentation captured in
`docs/references/alpaca-live-envelope.md` (verified 2026-09-27). ADR 0059 D4
binds those vendor facts to the owner-approved formula:

`current equity − last_equity − signed deposits and withdrawals after the same close`.

The fixture instant is Tuesday 2026-09-08 12:00 ET. Monday 2026-09-07 is the
NYSE Labor Day holiday, so the independently checked prior-session boundary is
Friday 2026-09-04 16:00 ET (`1788552000000` ms UTC).

## Independent numerical oracle

`reference_kind=hand_computed`. The generator uses only `decimal.Decimal` and
the literal formula above; it never imports the canonical implementation.
The four cases prove the no-flow loss, deposit-neutral, withdrawal-neutral,
and mixed-flow paths:

- `95000 − 100000 − 0 = -5000`
- `110000 − 100000 − 10000 = 0`
- `90000 − 100000 − (-10000) = 0`
- `102345.67 − 100000 − (7500 − 1250) = -3904.33`

## Tolerance

`atol=1e-9, rtol=0`. This is the repository default for accumulated P&L and
is far below one cent. Relative tolerance is zero so larger account balances
cannot silently widen the accepted dollar error.

## Regeneration

`cd PythonDataService && .venv/bin/python -m scripts.fixture_generators.alpaca_account_day_pnl`
