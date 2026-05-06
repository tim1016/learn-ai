# Trade divergence

**Concept**: Comparing two trade lists (ours vs reference, or two engine runs) and producing a categorized diff: matched trades, extra-ours, extra-reference, plus per-field deltas (entry/exit time, entry/exit price, PnL, PnL%).

**Reference**: Internal — no external paper. The reconciliation taxonomy is the one used by the `reconcile-backtest` skill: `precision`, `warmup`, `timestamp`, `commission`, `fill-model`, `algorithm`, `data` divergence categories.

**Canonical implementation**: `PythonDataService/app/research/divergence/analysis/trade_divergence.py` (registry: § Research / divergence pipeline).

**Status note**: `pending-fixture`. The matching logic uses a configurable timestamp tolerance (default 900s) and pairs trades by chronological proximity; per-field deltas are computed for matched pairs.

**Cross-references**:
- `app/services/trade_comparison.py` — separate implementation that operates on simpler dict-shaped trades; finding F-0019 flagged its `_parse_ts` helper for naive-string timestamp handling (deferred behind the wire-format change).
- `app/research/divergence/strategies/{s1,s2,s3}_*.py` — divergence-research-only parallels of engine-canonical strategies; produce trades for divergence checking.
- `docs/references/reconciliations/data-lab-spy-2026-04-17-to-2026-04-24.md` — example reconciliation report.

**Validation**: `NONE — pending`. A golden-fixture test that constructs two known-divergent trade lists and asserts the diff classification + delta values would close the §6 gate item for this row.
