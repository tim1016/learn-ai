---
id: F-0011
severity: P1
status: open
area: python-authority
canonical_file: Backend/Services/Implementation/SnapshotService.cs
reference: docs/math-sources-of-truth.md (Max drawdown row, line 46)
first_seen: 2026-05-05
last_seen: 2026-05-05
phase: 1
---

## What

`Backend/Services/Implementation/SnapshotService.cs:60` defines `ComputeDrawdownSeries` and exposes `GetDrawdownSeriesAsync`. The registry's **Max drawdown** row (line 46) names canonical as `PythonDataService/app/engine/` (vague — see F-0006) and lists the .NET duplicate as `Backend/Services/Implementation/BacktestService.cs::CalculateMaxDrawdown` scheduled for removal in Phase 3.2. **`SnapshotService.cs` is a third drawdown implementation that the registry does not know about.**

Per `BacktestService.cs` row, the .NET path is pending-migration. But SnapshotService.cs is on the live portfolio path — when a user looks at their portfolio's drawdown series, that's what's running.

## Where

- `Backend/Services/Implementation/SnapshotService.cs:51` — `GetDrawdownSeriesAsync(Guid accountId, ...)`
- `Backend/Services/Implementation/SnapshotService.cs:60` — calls `ComputeDrawdownSeries(snapshots.Select(...))`
- Registry: max drawdown row (line 46) — does not list `SnapshotService.cs`

## Why this severity

P1 — Math-authority duplicate not registered. Drawdown is rendered to the user as authoritative ("your portfolio drawdown was 12.3%") so escalation to **P0** is defensible if Phase 8 confirms the value reaches the UI.

## Reproduction

```
grep -n 'ComputeDrawdownSeries\|GetDrawdownSeries' Backend/Services/Implementation/SnapshotService.cs
grep -n 'SnapshotService' docs/math-sources-of-truth.md         # 0 matches
grep -n 'Drawdown' docs/math-sources-of-truth.md
```

## Suggested resolution (NOT auto-applied)

Two options:
1. **Register as duplicate** — add to the Max drawdown row's "Legacy / duplicates" column with status `pending-migration` or `legacy-ok` (with parity test). Note the live portfolio consumer.
2. **Cutover to Python** — call into the Python statistics module per F-0006's recommendation (`app/engine/results/statistics.py`).

The first option is the smaller change; the second is the rule-5-pure end state.

## Provenance of the finding itself

Phase 1 / cursor: `Backend/Services/Implementation/SnapshotService.cs` read. Cross-checked against registry and F-0006.
