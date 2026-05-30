# SPY VWAP-reversion — QuantConnect orders golden fixture

## What

`qc_orders.json` is the order log from a QuantConnect Cloud (LEAN) backtest of
the reference algorithm `references/quantconnect/spy_vwap_reversion/main.py`,
normalized into the canonical shape `qc_reconciler._parse_qc_orders` accepts.
It is the **reference oracle** the Python port (`SpyVwapReversionAlgorithm`)
reconciles against trade-by-trade.

- **10 orders** (5 long round-trips): `VwapReversionEntry` (buy 100) /
  `VwapReversionExit` (sell 100), all market, all filled.
- **Window:** 2024-03-04 → 2024-03-08 (RTH).
- **Symbol:** SPY, 1-minute.

## Reference

- **Source:** QuantConnect Cloud backtest of `SpyVwapReversionReference`
  (`references/quantconnect/spy_vwap_reversion/main.py`), pinned parameters
  K=2.0, LOOKBACK=30, qty=100, session filter 5/5, force-flat 15:55,
  max 4 trades/day.
- **Retrieved:** 2026-05-29 (raw export `Alert Orange Fly.json`).
- **Fees:** not present in the backtest-result export → `orderFeeAmount: null`
  (Branch-B; commission divergence is non-gating per the reconciliation
  taxonomy in `.claude/rules/numerical-rigor.md`).

## Price-history source

The 1-minute SPY bars feeding both the engine replay and the reconciler's
fill-price audit come from the local `PythonDataService/lean-cache/`
(Polygon-sourced LEAN minute cache), days 20240304–20240308 — read via
`LeanMinuteDataReader` (1950 RTH bars). No live Polygon fetch is needed (and
the window predates the Polygon Starter 2-year history limit, so the cache is
the canonical source here).

## Regenerate

```
python references/quantconnect/spy_vwap_reversion/normalize_orders.py \
    <qc_export.json> \
    PythonDataService/tests/fixtures/golden/spy-vwap-reversion-qc/qc_orders.json
```

Regenerate only on a new QC backtest run (new window or changed parameters);
a regeneration commit must cite the parameter/window change per the fixture
lifecycle in `.claude/rules/numerical-rigor.md`.
