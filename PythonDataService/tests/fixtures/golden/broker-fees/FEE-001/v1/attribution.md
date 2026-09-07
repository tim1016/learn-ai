# FEE-001 — Alpaca equity regulatory pass-through fees

## Source

Rates are the published pass-through schedule pinned on 2026-09-07: Alpaca Securities "Broker Fee Schedule" §"Pass-Through Regulatory and Exchange Fees — Equities" (https://files.alpaca.markets/disclosures/library/BrokFeeSched.pdf), SEC Fee Rate Advisories 2024-2 / 2025-2 / 2026-2, and the FINRA SR-FINRA-2024-019 TAF fee-adjustment schedule. Row-by-row citations: docs/references/alpaca-regulatory-fees.md.

## Independent numerical oracle

`reference_kind=hand_computed`. Expected values are exact decimal arithmetic over the literal rates in this generator, computed without importing `app.broker.alpaca.regulatory_fees`. Sells: `sec = qty × price × r_sec`, `taf = min(qty × r_taf, cap)`, `cat = qty × r_cat`; buys owe only CAT. A component with no pinned rate on the case date is `null`, never zero.

## Cases

- Six 2026-09-08 fills (SEC $20.60/M, TAF $0.000195 cap $9.79, CAT $0.000003) including the TAF cap boundary: 50,205 shares → `9.789975` (under the cap), 50,206 → `9.79` (capped).
- `sell_sec_zero_regime_2025` (2025-06-02): SEC `0` is a pinned zero; CAT unpinned → null.
- `sell_sec_2780_regime_2024` (2024-06-03): SEC $27.80/M → `1.39` on $50,000.
- `sell_before_sec_pin_2024` (2024-03-01): SEC unpinned → null; TAF pinned at 2024 rates.
- Session settlement over the six 2026-09-08 fills: each component summed then rounded UP to the cent — sec `14.9434666 → 14.95`, taf `29.389475 → 29.39`, cat `0.4818345 → 0.49`, total `44.83`.

## Timestamps

`trade_date_ms` is the ET session-open anchor of the trade date (`session_open_ms_utc`), `int64 ms UTC`; the model derives the ET date with `et_date_at_ms`.

## Tolerance

`atol=0, rtol=0`: the model and the oracle are both exact `Decimal` arithmetic; the test compares `Decimal` values for equality.

## Regeneration

`cd PythonDataService && .venv/bin/python -m scripts.fixture_generators.alpaca_regulatory_fees`
