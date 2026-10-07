# Fixture attribution — the account's option fields (documentation example)

- **broker:** alpaca
- **reference_kind:** `documentation_example`. **Not a recording.**
- **read on:** 2026-10-06
- **regenerate:** `python -m scripts.fixture_generators.alpaca_documented_option_examples` (from `PythonDataService/`)

## Source

`documented_account.json` is the example answer of `GET /v2/account` on Get
Account, https://docs.alpaca.markets/reference/getaccount-1 (page last
updated 2026-05-27), as it stands.

It is kept because it shows something the real capture does not: an answer
with `options_buying_power` and `options_trading_level` and **no**
`options_approved_level`. A field Alpaca leaves out must stay unknown.

A real capture already exists for these three fields:
`tests/fixtures/alpaca/account/account.json`, from a paper account on
2026-07-24, carries all three. The option fields are tested against both.

## What the reference states

- `options_buying_power`: a decimal string.
- `options_approved_level` and `options_trading_level`: an integer from 0 to
  3. 0 is disabled, 1 covered calls and cash-secured puts, 2 bought calls and
  puts, 3 spreads and straddles. The trading level is the lower of the
  approved level and the most the account's configuration allows.
- **The trading level is never above the approved level.** The reference's
  words on `options_trading_level`, read again on 2026-10-07: "This is the
  minimum between account options_approved_level and account configurations
  max_options_trading_level." An answer that states both, with the trading
  level the higher, contradicts that and is refused. The reference's own
  `Account` schema example agrees with the rule: approved 2, trading 1. A
  level the answer leaves out holds the other to nothing.

## What neither file shows

- A level below 3, on a real account.
- A live (not paper) account's answer.
- How `options_buying_power` moves when an option order is accepted, filled
  or cancelled. The fixture is one reading.
