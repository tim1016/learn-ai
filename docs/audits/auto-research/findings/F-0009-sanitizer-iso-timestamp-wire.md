---
id: F-0009
severity: P1
status: fixed-verified
area: timestamp
canonical_file: PythonDataService/app/services/sanitizer.py
reference: docs/audits/computational-fidelity-2026-04-22.md (top-10 finding #1, #2)
first_seen: 2026-05-05
last_seen: 2026-05-06
phase: 1
---

## What

`PythonDataService/app/services/sanitizer.py:79` writes `df["timestamp"] = df["timestamp"].dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")` immediately before returning to the wire. This is the canonical ban-list violation from `.claude/rules/numerical-rigor.md` → "Timestamp rigor → Ban list": **`.strftime(".*Z")`** on a (formerly tz-aware) datetime, producing an ISO string that lies about timezone semantics for downstream consumers and which has been the source of the documented "naive-Z" defect in the prior audit.

The same module does **silent `drop_duplicates`** at line 57 (gated by a config flag, not fail-fast). Per `.claude/rules/numerical-rigor.md` → "Timestamp rigor → Two and only two conversion boundaries", duplicates are signals of upstream corruption and must surface, not be silenced.

## Where

- `PythonDataService/app/services/sanitizer.py:79` — `dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")` on the timestamp column before JSON serialization.
- `PythonDataService/app/services/sanitizer.py:57` — `df.drop_duplicates(subset=["timestamp"])` (silent dedup behind a settings flag).

This finding **overlaps with prior audit** `docs/audits/computational-fidelity-2026-04-22.md` top-10 findings #1 (`/api/sanitize` collapses ms-epoch timestamps by 10⁶ at `sanitizer.py:216`) and #2 (`_format_timestamp` emits "2024-01-01 00:00" parsed as local-time in browsers). Both flagged as **CRITICAL** in that audit.

## Why this severity

P1 (not P0) only because it has been **previously documented**. The original computational-fidelity audit classified this CRITICAL with reproduced evidence (1704067200000 → 1970-01-20 collapse, 5-hour shifts in ET browsers). Status `awaiting-human` because remediation is a coordinated change spanning the sanitizer wire format, the `MarketDataRecord` DTO on the .NET side (`Backend/Services/Implementation/SanitizationService.cs:28`), and any downstream caller that re-parses the string.

If a Phase-3 sweep on a different commit revealed this with no prior context, it would be P0.

## Reproduction

```
grep -n 'strftime' PythonDataService/app/services/sanitizer.py
grep -n 'drop_duplicates' PythonDataService/app/services/sanitizer.py
# Cross-ref:
grep -n 'sanitizer.py' docs/audits/computational-fidelity-2026-04-22.md
```

## Suggested resolution (NOT auto-applied)

Two coordinated changes:

1. **Stop converting timestamps to ISO at the wire boundary.** The wire format is `int64 ms UTC`. The sanitizer should return `df["timestamp"]` as the original `int64 ms` it received, after sort/clip/integrity filtering. The .NET consumer (`SanitizationService.cs::SanitizeAsync` → `MarketDataRecord` DTO) should declare the field as `long` ms-epoch, not a string.
2. **Fail-fast on duplicates.** Replace `drop_duplicates` with a check that raises `HTTPException(400, ...)` when duplicates appear. Same for non-monotonic sequences if not already enforced.

This is in scope for Phase 3 of this baseline (timestamp boundary) and is sequenced ahead of Phase 7 (ingestion fidelity).

## Provenance of the finding itself

Phase 1 / cursor: `app/services/sanitizer.py` head read. Cross-referenced against prior audit doc and `.claude/rules/numerical-rigor.md` ban list.
