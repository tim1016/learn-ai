# LEAN Sidecar Lab

**Status:** Reference note — how LEAN itself behaves, where neither ADR 0070 nor the code says so.
**Pairs with:** [ADR 0070](adrs/0070-lean-sidecar-boundary.md) (the sidecar boundary, image pin, container flags, limits and reconciliation-grade pins, with their whys) and the [launcher runbook](../runbooks/lean-sidecar-launcher.md) (operator procedure).

The LEAN data-folder format is stated in `PythonDataService/app/engine/data/lean_format.py`; the corporate-action, date-window, quantization-floor and statistics-scope facts are in ADR 0070 decision 8; LEAN's statistic formulas are pinned by the `lean-statistics-oracle-v1` golden fixture (`docs/references/lean-native-statistics-oracle-v1.md`). What stays here are the LEAN defaults that the reconciliation-grade pins in ADR 0070 decision 8 override.

---

## LEAN defaults the reconciliation pins override

### Brokerage, fill, and fee policy

If the algorithm does not call `SetBrokerageModel`, the result is whatever the
pinned LEAN image's default brokerage/fill/fee model produces.

### Fill-forward policy

LEAN subscriptions can forward-fill missing minute bars by default.

### Data normalization mode policy

The bar staging policy and LEAN's runtime normalization mode are separate
controls. Even when raw bars and factor/map files are staged correctly,
`AddEquity(...)` defaults to adjusted runtime prices, so the algorithm sees a
different price series than Engine Lab unless it sets
`DataNormalizationMode.Raw`.

---

## References

- QuantConnect LEAN docs: https://www.quantconnect.com/docs/v2/lean-engine
- QuantConnect local data format/storage docs: https://www.quantconnect.com/docs/v2/lean-cli/datasets/format-and-storage
- QuantConnect local backtest docs: https://www.quantconnect.com/docs/v2/lean-cli/backtesting/deployment
