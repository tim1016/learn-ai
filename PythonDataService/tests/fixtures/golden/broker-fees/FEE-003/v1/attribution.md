# FEE-003 — Alpaca Paper's posted option TAF rows

## Source

Alpaca Paper's account activity feed: non-trade `FEE` activities of sub type `TAF`, described as `OPT TAF fee for proceed of N contracts (M trades) on <date> by <account>`. They were read by the Paper qualification runs of 2026-10-14 to 2026-10-21 (#3103, #3225). The fixture was generated on 2026-10-21 (America/Chicago). No order or broker call was made to produce it, and the model it checks was not consulted.

| Trade date | Contracts sold (trades) | Posted | Read from |
|---|---:|---:|---|
| 2026-10-13 | 4 (4) | $0.02 | the recordings under `tests/fixtures/alpaca/option_paper_recordings`, repeated across their captures |
| 2026-10-14 | 19 (19) | $0.06 | the same recordings |
| 2026-10-15 | 2 (2) | $0.01 | the same recordings |
| 2026-10-16 | 30 (30) | $0.09 | the run record of 2026-10-21 in `docs/runbooks/qualify-option-orders-on-paper.md`, one row, read offline from the Paper journal |

The 2026-10-16 row is in no committed recording. Its 30 sold contracts are counted by the generator from the fills of that day in `3225-C4-buyback-retry-window-missed-20261016`: 30 buys, 15 sells and 15 short sells, every one a single contract. The $0.09 is the runbook's figure alone.

## What the rows say, and what they leave open

- **Each row is the day's sold contracts times one per-contract rate, rounded up to the cent.** Rounded to the nearest cent instead, the 19-contract day's $0.06 needs `19 r < 0.065`, so `r < 0.00343`, and then 4 contracts owe under `0.0138`, which rounds to $0.01, not the $0.02 posted. So Alpaca rounds up, as the fee schedule's "How fees are charged" says it does for every type.
- **The four rows bound the rate and do not fix it.** A day of N contracts posted at C cents says `(C - 0.01) / N < r <= C / N`: `(0.0025, 0.005]` for 4, `(0.0026316, 0.0031579]` for 19, `(0, 0.005]` for 2 and `(0.08/30, 0.003]` for 30. Together: `0.08/30 < r <= 0.003`, and `0.08/30` is about `0.0026667`. The model pins `0.003`, the top of that range and the figure the owner named (#3306, 2026-10-22). Every rate in the range posts these four rows; they part on larger days (100 contracts are $0.27 to $0.30).
- **FINRA's own `0.00329` is outside the range.** 19 contracts at it round up to $0.07 and 30 to $0.10, where Alpaca posted $0.06 and $0.09. FINRA's pause (SR-FINRA-2026-021) sets FINRA's TAF to zero from 2026-10-01 through 2026-12-31; Alpaca Paper charged anyway.
- **Not observed:** the days of the pause before 2026-10-13, what Alpaca charges once the pause ends on 2026-12-31 (the model keeps FINRA's resumed `0.00329`), a fill of several contracts (every execution here is one contract, so `sold_contracts == trades`), a day that also traded stock (these four held option fills alone), and a Live account.

## Independent numerical oracle

`reference_kind=vendor_observed`. Nothing is computed. `output.json` holds each row's posted amount in positive dollars (the activity's `net_amount` is its negative); `input.json` holds the contracts and trades the row's text names. The test builds that many one-contract sales and settles them as a day through the model.

## Cases

Four days, keyed by `trade_date_ms`: 4 contracts and `0.02`; 19 and `0.06`; 2 and `0.01`; 30 and `0.09`. Worked by hand: `4 × 0.003 = 0.012 → 0.02`, `19 × 0.003 = 0.057 → 0.06`, `2 × 0.003 = 0.006 → 0.01`, `30 × 0.003 = 0.090 → 0.09`.

## Timestamps

`trade_date_ms` is the ET session-open anchor of the trade date (`session_open_ms_utc`), `int64 ms UTC`; the model resolves the New York trade date inside its rate lookup (#2958).

## Tolerance

`atol=0, rtol=0`: the posted amounts are exact cents and the model's day TAF is an exact `Decimal` rounded up to the cent; the test compares them for equality.

## Regeneration

`cd PythonDataService && .venv/bin/python -m scripts.fixture_generators.alpaca_option_taf`. It rereads the three recorded days, counts the runbook day's sold contracts from the `3225-C4` capture, and carries only that day's $0.09 as a literal. Regenerate only when a recording or the runbook's record changes, and say which in the commit.
