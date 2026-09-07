# Alpaca equity regulatory pass-through fees

## Canonical contract

`PythonDataService/app/broker/alpaca/regulatory_fees.py` prices the three regulatory
pass-throughs Alpaca charges on US listed equities and settles them the way Alpaca
charges them. ADR 0059 D6 fixes the authority: **observed `FEE` activities are the
truth**; this model predicts them (reconciliation) and prices fills where no
observation exists yet (shadow, backtest parity).

- Sells: `sec = quantity × fill_price × r_sec(d)`; `taf = min(quantity × r_taf(d), cap_taf(d))`.
- Buys and sells: `cat = quantity × r_cat(d)` (NMS equity: one share is one executed-equivalent share).
- Settlement: each component summed over the ET trade date, rounded **up** to the cent; the
  charge is the sum of the three rounded components.
- A component with no pinned rate on `d` is `None`, never `0`; a session containing one
  raises `RateNotPinnedError`. Buys owe no SEC or TAF on any date, so those are `0`
  for buys regardless of pinning.

## Rate table (pinned 2026-09-07)

| Component | Effective from | Rate | Source |
|---|---|---|---|
| SEC §31 (sells, on value) | 2024-05-22 | $27.80 per $1M (`0.0000278`) | SEC Fee Rate Advisory 2024-2, 2024-04-17 — https://www.sec.gov/rules-regulations/fee-rate-advisories/2024-2 |
| SEC §31 | 2025-05-14 | $0.00 per $1M (`0`) | SEC Fee Rate Advisory 2025-2, 2025-04-08 — https://www.sec.gov/rules-regulations/fee-rate-advisories/2025-2 |
| SEC §31 | 2026-04-04 | $20.60 per $1M (`0.0000206`) | SEC Fee Rate Advisory 2026-2, 2026-02-27 — https://www.sec.gov/rules-regulations/fee-rate-advisories/2026-2 |
| FINRA TAF (sells, per share, per-trade cap) | 2024-01-01 | `0.000166` / share, cap `8.30` | FINRA SR-FINRA-2024-019 fee-adjustment schedule — https://www.finra.org/rules-guidance/rule-filings/sr-finra-2024-019/fee-adjustment-schedule |
| FINRA TAF | 2026-01-01 | `0.000195` / share, cap `9.79` (binds at 50,206+ shares) | same; stated verbatim by Alpaca's schedule |
| FINRA TAF | 2027-01-01 | `0.000232` / share, cap `11.61` | same |
| FINRA TAF | 2028-01-01 | `0.000240` / share, cap `12.05` | same |
| FINRA TAF | 2029-01-01 | `0.000249` / share, cap `12.50` | same |
| FINRA CAT (both sides, per executed-equivalent share) | 2026-09-01 | `0.000003` / share | Alpaca Securities "Broker Fee Schedule", §"Pass-Through Regulatory and Exchange Fees — Equities" — https://files.alpaca.markets/disclosures/library/BrokFeeSched.pdf (retrieved 2026-09-07; the schedule is dated 2026-09-01) |

Unpinned windows (the model returns `None` there): SEC before 2024-05-22 (the FY2023 $8.00/M
rate's start is not pinned), TAF before 2024-01-01, CAT before 2026-09-01 (CAT's earlier
rate history is not pinned — pin it from the CAT fee filings when backtest parity over
2024–2026 needs the cent).

## Charging model and the one open question

Alpaca's schedule states fees accrue intraday and are charged at end of day with the
total rounded up to $0.01. Its support page (https://alpaca.markets/support/regulatory-fees)
reads as if SEC and TAF are each rounded up per trade. The canonical model implements
**end-of-day, per-component round-up**. The reconciliation tolerance
`$0.01 × (3 + 2 × sell_fills)` admits the per-trade reading (at most one extra cent per
sell for SEC and one for TAF) so a live session can decide which reading is true; tighten
it to `$0.03` once observed sessions agree with one reading.

## Reconciliation

`GET /api/brokers/{broker}/fees/session-reconciliation?session_open_ms=<int>` (the
calendar's session open of a trading day, ET-anchored `int64 ms UTC`) prices every
*effective* SQLite fill dated that ET calendar day, sums the `FEE` activities Alpaca posted
for that date, and returns a verdict: `within_tolerance`, `drift`, `pending` (no `FEE`
posted yet, within 24 h after the day ends), `unobserved` (fills but no `FEE` after the
grace period, or a `FEE` row without `net_amount`), `no_fills`, `rate_unpinned`, or
`unavailable` (no active SQLite Clerk). Implementation:
`PythonDataService/app/services/alpaca_fee_reconciliation.py`. The activity read is
bounded (the broker port follows at most three newest-first pages of 100).

## Validation

- `PythonDataService/tests/broker/alpaca/test_regulatory_fees.py` — regime boundaries,
  sides, the TAF cap boundary (50,205 vs 50,206 shares), unpinned handling, settlement.
- Golden `FEE-001` (`PythonDataService/tests/fixtures/golden/broker-fees/FEE-001/v1/`),
  `reference_kind=hand_computed`, `atol=0, rtol=0`, asserted by
  `PythonDataService/tests/fixtures/test_alpaca_regulatory_fees_fixture.py`.
- No observed-fee fixture exists yet: the paper account has produced no `FEE` activity in
  any captured payload, and whether paper accounts emit them is unverified. The first live
  session (ADR 0059 slice 8) is the first observation.

## Follow-ups (not this slice)

- Engine wiring: `app/engine/execution/fill_model.py` exposes `compute_fee(quantity, fill_price)`
  without side or trade date; the IBKR tier model stays there until a fill-model
  signature carries both.
- Per-component observed reconciliation once `activity_sub_type` is mapped onto
  `BrokerActivity` (today only the day's total is compared).
- CAT rate history before 2026-09-01.
