# FEE-001 — Alpaca equity regulatory pass-through fees

## Source

v2 re-pins the TAF rows on 2026-10-08 from FINRA SR-FINRA-2026-021 (SEC Release 34-106409, Exhibits 5A and 5B — https://www.sec.gov/files/rules/sro/finra/2026/34-106409-ex5.pdf: paused at $0.00 per share and $0.00 per trade from 2026-10-01 through 2026-12-31, the 2026 rate resuming 2027-01-01) and FINRA SR-FINRA-2026-020 (91 FR 60427, immediately effective — https://www.finra.org/sites/default/files/2026-09/SR-FINRA-2026-020-91-FR-60427.pdf: 2027 and 2028 "no change", the step-ups landing 2029/2030/2031). Alpaca's schedule revision of 2026-10-01 passes the pause through: "FINRA Trading Activity Fees (TAF), when sells only, $0 per share" (https://files.alpaca.markets/disclosures/library/BrokFeeSched.pdf). Every other rate is the published pass-through schedule pinned on 2026-09-07 (Alpaca, SEC Fee Rate Advisories 2024-2 / 2025-2 / 2026-2, FINRA SR-FINRA-2024-019). Row-by-row citations: docs/references/alpaca-regulatory-fees.md.

## Independent numerical oracle

`reference_kind=hand_computed`. Expected values are exact decimal arithmetic over the literal rates in this generator, computed without importing `app.broker.alpaca.regulatory_fees`. Sells: `sec = qty × price × r_sec`, `taf = min(qty × r_taf, cap)`, `cat = qty × r_cat`; buys owe only CAT. A component with no pinned rate on the case date is `null`, never zero. The pause's `0` is a pinned zero (Exhibit 5A deletes the rate to `$0.00`, and Alpaca prints `$0`), not an unknown.

## Cases

- Six 2026-09-08 fills (SEC $20.60/M, TAF $0.000195 cap $9.79, CAT $0.000003) including the TAF cap boundary: 50,205 shares → `9.789975` (under the cap), 50,206 → `9.79` (capped).
- **The pause straddled** (#2951): the same 100-share sale on 2026-09-30 (taf `0.0195`), on 2026-10-01 and 2026-12-31 (taf `0`, the pause's first and last days), and on 2027-01-04 (taf `0.0195` again, Exhibit 5B's resumption).
- `sell_sec_zero_regime_2025` (2025-06-02): SEC `0` is a pinned zero; CAT unpinned → null.
- `sell_sec_2780_regime_2024` (2024-06-03): SEC $27.80/M → `1.39` on $50,000.
- `sell_before_sec_pin_2024` (2024-03-01): SEC unpinned → null; TAF pinned at 2024 rates.
- Session settlement over the six 2026-09-08 fills: each component summed then rounded UP to the cent — sec `14.9434666 → 14.95`, taf `29.389475 → 29.39`, cat `0.4818345 → 0.49`, total `44.83`.

## Timestamps

`trade_date_ms` is the ET session-open anchor of the trade date (`session_open_ms_utc`), `int64 ms UTC`; the model resolves the New York trade date inside its rate lookup (#2958).

## Tolerance

`atol=0, rtol=0`: the model and the oracle are both exact `Decimal` arithmetic; the test compares `Decimal` values for equality.

## Regeneration

`cd PythonDataService && .venv/bin/python -m scripts.fixture_generators.alpaca_regulatory_fees`
