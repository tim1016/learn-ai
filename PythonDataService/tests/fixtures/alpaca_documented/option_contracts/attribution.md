# Fixture attribution — option contracts (documentation examples)

- **broker:** alpaca
- **reference_kind:** `documentation_example`. **Not a recording.** To be joined by a recording from Alpaca Paper, beside it: this example stays.
- **read on:** 2026-10-06
- **regenerate:** `python -m scripts.fixture_generators.alpaca_documented_option_examples` (from `PythonDataService/`)

## Sources

| Key in `documented_contracts.json` | Page | Page last updated |
|---|---|---|
| `reference_example` | Get Option Contracts, https://docs.alpaca.markets/reference/get-options-contracts (the example under `OptionContractsResponse.option_contracts`) | 2026-05-27 |
| `not_found` | Get an option contract by ID or Symbol, https://docs.alpaca.markets/reference/get-option-contract-symbol_or_id (the 404 example) | 2026-05-27 |
| `options_trading_page_example` | Options Trading, https://docs.alpaca.markets/docs/options-trading (the first contract in the "Fetch Contracts" example) | 2026-09-16 |

The lookup of one contract is `GET /v2/options/contracts/{symbol_or_id}`, in
the Trading API. It is a reference read and needs no market-data
subscription.

## What the reference states, and where it is used

The `OptionContract` schema is the same on both reference pages. It requires
`id`, `symbol`, `name`, `status` (`active` or `inactive`), `tradable`,
`expiration_date`, `underlying_symbol`, `underlying_asset_id`, `type` (`call`
or `put`), `style` (`american` or `european`), `strike_price`, `multiplier`,
`size` and `ppind`.

- `multiplier`: what a price is multiplied by; always 100 for a standard
  contract.
- `size`: the shares delivered on exercise or assignment; always 100 for a
  standard contract, and not to be used as a multiplier.
- `deliverables`: one entry for a standard contract, of type `equity`, with
  the underlying's symbol and an amount of 100. The schema says the list is
  in the *list* answer only when `show_deliverables=true` is asked for.
- `ppind`: whether the contract trades in penny increments.

## Where two pages disagree, and which was taken

| Subject | The reference | The Options Trading page's example | Taken |
|---|---|---|---|
| `multiplier`, `ppind` | Required | Absent | The reference. A listing without `multiplier` cannot be shown to be standard and is not read as one |
| `root_symbol` | Not among the required fields | Present, as in the reference's own example | Read when present |
| The next-page token | `next_page_token` | `page_token`, beside a `limit` | Neither is read: only the lookup of one contract is used |

## What these examples do not show

No recording exists for any of this. Until one does, it is unverified.

- **Whether the lookup of one contract carries `deliverables`.** The schema
  documents the list for the list answer. When it is absent, a contract is
  judged standard by `multiplier` and `size` alone.
- **A non-standard, adjusted, inactive, untradable or European contract.**
  Both examples are a standard active American AAPL call.
- **A share-class underlying.** Both examples have `root_symbol` equal to
  `underlying_symbol`. That `BRK.B`'s contracts answer with root `BRKB` is
  the expected reading, not a documented one.
- **That the live answer carries `multiplier`.** The reference requires it;
  the older page's example does not have it.
- **Which contracts Alpaca lists** that IBKR also lists, and when a newly
  listed contract first appears.
