# FEE-002 — Alpaca option regulatory and clearing pass-through fees

## Source

Read on 2026-10-06. No account, order or broker API call was used: every rate is from a published document.

| Fee type | Unit, side | Rate, and the date it is in force from here | Where it is stated |
|---|---|---|---|
| SEC §31 | per dollar of premium sold; sells | `0` from 2025-05-14; `0.0000206` ($20.60 per $1M) from 2026-04-04 | SEC Fee Rate Advisories 2025-2 and 2026-2 (https://www.sec.gov/rules-regulations/fee-rate-advisories/2026-2). A sale of an OCC-issued put or call is a covered sale: 17 CFR 240.31. The model reads this rate from the stock fee model's table. |
| FINRA TAF | per contract sold; sells; no per-trade cap on options | `0.00329` for 2026; `0` from 2026-10-01 through 2026-12-31; `0.00329` again from 2027-01-01 through 2028; `0.00390` from 2029, `0.00404` from 2030, `0.00420` from 2031 | SR-FINRA-2026-020, 91 FR 60427 (https://www.finra.org/sites/default/files/2026-09/SR-FINRA-2026-020-91-FR-60427.pdf) for the ladder; SR-FINRA-2026-021, SEC Release 34-106409, Exhibits 5A and 5B (https://www.sec.gov/files/rules/sro/finra/2026/34-106409-ex5.pdf) for the pause and the resumption. |
| ORF | per contract; buys and sells | `0.02375` (revision of 2026-02-12); `0.02295` (2026-04-09); `0.015` (2026-07-01, still printed 2026-10-01) | Alpaca Securities LLC, Brokerage Fee Schedule, "Pass-Through Regulatory and Exchange Fees", Options table (https://files.alpaca.markets/disclosures/library/BrokFeeSched.pdf, "Revised on October 1, 2026"). Earlier revisions: Internet Archive captures `20260219160225`, `20260410195706`, `20260720131641` and `20260919034252` of the same URL. |
| OCC clearing | per contract; buys and sells | `0.025`, printed "(no cap)" (revision of 2026-02-12, unchanged since) | same schedule and captures |
| FINRA CAT | per executed-equivalent share; buys and sells; one contract counts its multiplier (100, "or applicable multiplier") | `0.000003` (first listed under Options in the revision of 2026-07-01) | same schedule; the conversion is also in SEC Release 34-105449 (SR-FINRA-2026-010). |

Settlement is from the same schedule, "How fees are charged" (in the revisions of 2026-07-01 and later): fees use the exact executed quantity; each fee type is summed separately per account per day; each total is then rounded up to the next cent. **OCC is the exception, read from Alpaca Paper's posted rows rather than the schedule (#3306):** it is posted as one row per execution, linked by `execution_id`, and every recorded row (one-contract fills, `1 × 0.025` up to `0.03`) is that fill's OCC rounded up to the cent, so the oracle rounds each fill's OCC up and adds the cents. Executions of several contracts are not recorded.

## What these dates are, and are not

- **SEC and TAF dates are the regulators' effective dates.**
- **ORF, OCC and CAT dates are Alpaca's revision dates.** Alpaca states no effective dates, so the day it began charging each figure is not known. A case dated near one of these steps shows what the model does, not what Alpaca charged that day.
- Alpaca calls its ORF figure an average of what it remits to the exchanges.
- Before 2026-02-12 the ORF and OCC rows are not pinned, and before 2026-07-01 the CAT row is not: the model answers `null` there, never zero. Alpaca's earlier schedules print other figures (ORF `0.02685`, OCC `0.02` with a $55 cap), with no date to hang them on.
- **The rates are documentation, not a recording.** Only the OCC settlement rule above comes from Alpaca's posted rows (`tests/broker/alpaca/test_paper_option_recordings.py`). How a day that also traded stock rounds SEC, TAF and CAT is unverified. Paper posted OPT TAF on 2026-10-13 to 2026-10-15, inside FINRA's pause, so the TAF figures here are the regulators', not what Alpaca Paper charged (#3306).

## Independent numerical oracle

`reference_kind=hand_computed`. Expected values are exact decimal arithmetic over the literal rates in the generator, computed without importing `app.broker.alpaca.option_fees`. Sells: `sec = contracts × premium × multiplier × r_sec`, `taf = contracts × r_taf`. Both sides: `cat = contracts × multiplier × r_cat`, `orf = contracts × r_orf`, `occ = contracts × r_occ`. A buy owes no SEC fee and no TAF. A fee type with no pinned rate on the case date is `null`.

## Cases

- `buy_2_at_1_50` (2026-10-05): cat `2 × 100 × 0.000003 = 0.000600`, orf `0.030`, occ `0.050`.
- `sell_2_at_2_10` (2026-10-05): sec `2 × 2.10 × 100 × 0.0000206 = 0.0086520`, taf `0` (paused), cat `0.000600`, orf `0.030`, occ `0.050`.
- `sell_10_of_a_ten_share_contract` (2026-10-05): a multiplier of 10. sec on `10 × 0.55 × 10 = $55.00` of premium, `0.00113300`; cat `10 × 10 × 0.000003 = 0.000300`; orf `0.150`; occ `0.250`.
- **A rate change straddled:** the same sale of 2 at $2.10 on 2026-09-30 (taf `2 × 0.00329 = 0.00658`), on 2026-10-01 (taf `0`), and on 2027-01-04 (taf `0.00658` again). Settled as days of their own, 2026-09-30 costs `0.01 + 0.01 + 0.01 + 0.03 + 0.05 = 0.11` and 2026-10-01 costs `0.01 + 0.00 + 0.01 + 0.03 + 0.05 = 0.10`.
- `buy_5_last_day_of_old_orf` (2026-06-30) and `buy_5_first_day_of_new_orf` (2026-07-01): orf `5 × 0.02295 = 0.11475` then `5 × 0.015 = 0.075`; cat `null` then `0.001500`.
- `sell_1_before_sec_fee_resumed` (2026-04-02; 2026-04-03 was Good Friday) and `sell_1_after_sec_fee_resumed` (2026-04-06): sec `0` then `500 × 0.0000206 = 0.0103000`.
- `buy_1_before_alpaca_rows` and `sell_1_before_alpaca_rows` (2026-01-15): orf, occ and cat `null`; the sale's sec is a pinned `0` and its taf `0.00329`.
- Day `three_fills_on_2026_10_05`: sec `0.0086520 + 0.00113300 = 0.00978500 → 0.01`; taf `0 → 0.00`; cat `0.000600 + 0.000600 + 0.000300 = 0.001500 → 0.01`; orf `0.210 → 0.21`; occ `0.050 + 0.050 + 0.250`, each already whole cents, `= 0.35`; total `0.58`.
- Provision `one_bull_call_spread` (buy 1 at $4.10, sell 1 at $2.20, 2026-10-05): sec `220 × 0.0000206 = 0.0045320 → 0.01`; taf `0.00`; cat `0.000600 → 0.01`; orf `0.030 → 0.03`; occ one contract a leg, `0.025 → 0.03` twice, `= 0.06`; `11` cents.
- Provision `two_contracts_bought`: cat `0.01`, orf `0.03`, occ `0.05`; `9` cents.

## Timestamps

`trade_date_ms` is the ET session-open anchor of the trade date (`session_open_ms_utc`), `int64 ms UTC`; the model resolves the New York trade date inside its rate lookup (#2958).

## Tolerance

`atol=0, rtol=0`: the model and the oracle are both exact `Decimal` arithmetic; the test compares `Decimal` values for equality, and provisions as whole cents.

## Regeneration

`cd PythonDataService && .venv/bin/python -m scripts.fixture_generators.alpaca_option_fees`, then put the printed hashes in `tests/fixtures/golden/manifest.json` by hand. Regenerate only when a cited source changes, and say which in the commit.
