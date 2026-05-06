---
id: F-0023
severity: P0
status: open
area: ingestion
canonical_file: PythonDataService/app/services/dataset_service.py
reference: .claude/rules/numerical-rigor.md (Timestamp rigor + "Never forward-fill or interpolate to align")
first_seen: 2026-05-06
last_seen: 2026-05-06
phase: 3
---

## What

`PythonDataService/app/services/dataset_service.py` defines `forward_fill_gaps` (lines 489-565) which **silently fills missing minute bars** with the previous close, zero volume, and zero transactions. This directly violates two rules:

1. `.claude/rules/numerical-rigor.md` → Timestamp rigor: "Never forward-fill or interpolate to align. If two series have different timestamps, that's data telling you something — don't silence it."
2. Same rule → "Fail-fast ingestion: duplicate timestamps and non-monotonic sequences reject with a descriptive error. ... no silent `drop_duplicates`, no forward-fill."

The function is called by default (`forward_fill: bool = True` at lines 667, 865, 1114, 1165). Every default ingestion run silently invents data that wasn't in Polygon's response.

## Where

- `PythonDataService/app/services/dataset_service.py:489` — `def forward_fill_gaps(...)`
- `:561` — `merged["close"] = merged["close"].ffill()`
- `:563` — `merged[col] = merged[col].fillna(merged["close"])` (fills OHLC opens/highs/lows with previous close)
- `:564` — `merged["volume"] = merged["volume"].fillna(0)`
- `:565` — `merged["transactions"] = merged["transactions"].fillna(0)`
- `:567` — `merged["vwap"] = merged["vwap"].ffill()`
- Default `forward_fill=True` at `:667, :865, :1114, :1165`
- Logging at `:520` says `"[FILL] Skipping forward_fill for timespan={timespan} — Polygon returns ..."` confirms forward-fill is intentional behavior, not a bug — which is itself the problem.

Downstream consumers think they're seeing real bars; they're seeing fabricated rows with `volume=0`. Every indicator computed downstream of this is being asked to handle synthetic gaps invisibly.

## Why this severity

P0 — Active data corruption in ingestion path. The fabricated rows have `volume=0` which most indicators *don't* treat specially, so flat synthetic bars enter rolling means, RSI, etc. as if they were real low-volatility periods. This is exactly the kind of "wrong number computed correctly" failure the rules exist to prevent.

The other ingestion-fidelity violations (F-0009 sanitizer ISO-Z, F-0021 .NET `AssumeUniversal`) are *boundary format* issues; this is a *data fabrication* issue. Higher severity.

## Reproduction

```
grep -nE 'forward_fill|ffill|fillna' PythonDataService/app/services/dataset_service.py
grep -n 'forward_fill: bool' PythonDataService/app/services/dataset_service.py   # confirms default=True
```

To confirm the impact:
```python
# A run that fetched a half-day of bars from Polygon:
# - raw_bar_count: 195
# - filled_bar_count: 390 (after forward_fill)
# Per the response shape at line 965: "bars_added_by_fill"
```

## Suggested resolution (NOT auto-applied)

Per the rule: **fail-fast on gaps**. Two coordinated changes:

1. **Default `forward_fill: bool = False`.** Make synthetic-bar generation an opt-in research mode, not the default ingestion behavior.
2. **Surface gaps as data, not silence them.** When gaps are detected, return them in the response payload as a `gaps: [{from_ts, to_ts, missing_bar_count}]` array. The downstream consumer decides whether to fill.

Optional follow-up: rename `forward_fill_gaps` to `synthesize_continuous_grid` so the *what* is honest. The current name implies a benign cleanup; the function actually fabricates data.

## Provenance of the finding itself

Phase 3 / cursor: targeted grep of `drop_duplicates|forward.{0,2}fill|ffill|fillna` across `dataset_service.py`. Function inspected at lines 489-565.

## Cross-references

- F-0009 — sanitizer.py also has silent `drop_duplicates` and `strftime("...Z")` issues. The two ingestion-path files have *complementary* violations: sanitizer corrupts the format on the way out; dataset_service fabricates rows on the way in.
- Prior audit `docs/audits/computational-fidelity-2026-04-22.md` — does not appear to flag the forward-fill specifically; this may be a finding the prior audit missed.
