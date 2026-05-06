# Bacon §8.2 — Maximum Drawdown (reference extract)

**Source**: Bacon, Chris L. *Practical Portfolio Performance Measurement and Attribution.* 2nd edition. Wiley, 2012. Chapter 8 "Risk."

## Definition (Bacon §8.2)

Maximum drawdown is the largest percentage decline from a portfolio equity peak to a subsequent trough, over the full return period:

```
MaxDrawdown = max over all t { (peak_t - trough_after_peak_t) / peak_t }
```

where `peak_t` is the running maximum of the equity curve up to time t.

In the learn-ai implementation (`_max_drawdown` in `statistics.py`), the running-peak formulation is used directly:

```python
peak = curve[0]
max_dd = 0.0
for value in curve:
    if value > peak:
        peak = value
    if peak > 0:
        dd = (peak - value) / peak
        if dd > max_dd:
            max_dd = dd
return max_dd          # positive fraction; 0.18 means 18% drawdown
```

The result is a positive fraction (0 → 1 range), not signed. This matches Bacon's convention where drawdown is reported as a loss magnitude.

## Where this lands in the codebase

- **Canonical Python**: `PythonDataService/app/engine/results/statistics.py::_max_drawdown` — called by `compute_statistics()` which packages it into `PerformanceStats.max_drawdown_pct`.
- **Legacy .NET duplicate (pending migration)**: `Backend/Services/Implementation/BacktestService.cs::CalculateMaxDrawdown` — scheduled for removal in Phase 3.2 of the numerical-authority migration plan. `BacktestService.cs:449` also contains a `(decimal)Math.Sqrt((double)variance)` round-trip that introduces a precision floor; this is absorbed by the Python canonical once Phase 3.2 ships.
- **Live-portfolio variant (pending migration)**: `Backend/Services/Implementation/SnapshotService.cs::ComputeDrawdownSeries` — tracked in finding F-0011.

## Notes on assumptions

- The formula operates on the **equity curve** (cumulative portfolio value), not on the return series. If the input is already a return series, it must be converted to an equity curve before calling `_max_drawdown`.
- The result is the **maximum single drawdown episode**. It does not average across multiple drawdowns (that is "average drawdown" per Bacon §8.4).
- A drawdown of exactly 0 can occur if the equity curve is monotonically non-decreasing.

## Registry rows that cite this reference

- Max drawdown (`PythonDataService/app/engine/results/statistics.py`)
