---
id: F-0021
severity: P0
status: open
area: timestamp
canonical_file: Backend/Services/Implementation/MarketDataService.cs; Backend/StudiesApi.cs
reference: .claude/rules/numerical-rigor.md (Timestamp rigor → Ban list)
first_seen: 2026-05-06
last_seen: 2026-05-06
phase: 3
---

## What

Two .NET ingestion paths use the **explicitly banned** `DateTime.Parse(..., DateTimeStyles.AdjustToUniversal | DateTimeStyles.AssumeUniversal)` pattern. From `.claude/rules/numerical-rigor.md` → Timestamp rigor → Ban list:

> `DateTime.Parse(...)` in any timestamp-canonicalization path — **disallowed**. The common belief that it "produces `Kind=Local`" is misleading: naive strings actually parse as `Kind=Unspecified`, and passing `DateTimeStyles.AssumeUniversal | DateTimeStyles.AdjustToUniversal` causes the parser to **silently coerce** naive strings to UTC. Both paths violate fail-fast ingestion.

Both occurrences are at boundaries where data flows in from Python, where F-0009 has already established that Python is emitting ISO-Z strings via `strftime("%Y-%m-%dT%H:%M:%S.%fZ")` (the "naive-Z lie" pattern from the prior audit).

## Where

### MarketDataService.cs:451 — aggregate-row hydration

```csharp
Timestamp = DateTime.Parse(
    dto.Timestamp,
    CultureInfo.InvariantCulture,
    DateTimeStyles.AdjustToUniversal | DateTimeStyles.AssumeUniversal),
```

This is mapping Python `dto.Timestamp` strings into the `StockAggregate.Timestamp` column. Every bar that flows from Python to Postgres goes through here. If Python ever emits a non-Z-suffixed naive string (which `_format_timestamp` in `rule_based_backtest.py:252` does — see prior audit top-10 #2), this silently calls it UTC.

### StudiesApi.cs:294-298 — Python-engine trade-timestamp normalization

```csharp
// Incoming trade timestamps from the Python engine are
// UTC ISO-8601 strings; normalize kind here.
private static DateTime ParseUtc(string s) =>
    DateTime.Parse(
        s,
        CultureInfo.InvariantCulture,
        DateTimeStyles.AdjustToUniversal | DateTimeStyles.AssumeUniversal);
```

The comment **claims** all inputs are "UTC ISO-8601 strings" but the parser uses the silent-coercion pattern that handles the case where the claim is false — by silently making it true. The Python side cannot guarantee that claim today (per F-0009, F-0019, F-0020).

## Why this severity

P0 — Active timestamp corruption pattern in ingestion paths. Per the rules, the parser is disallowed. The "but our inputs are always Z-suffixed UTC" defense fails because:

1. The Python emitter is currently `strftime("...Z")` on a tz-aware datetime that lies about timezone for downstream parsers (F-0009).
2. There's a documented producer (`rule_based_backtest.py:252`, prior audit #2) that emits naive `"YYYY-MM-DD HH:MM"` strings without the `T` or `Z`.
3. Even when both inputs are well-formed today, the parser pattern silently absorbs any future regression.

This is the same class of finding as the prior `computational-fidelity-2026-04-22.md` audit's introduction line: "fragile `DateTime.Parse` in `MarketDataService.cs:450`" — that audit specifically called out this file:line.

## Reproduction

```
grep -n 'DateTime.Parse' Backend/Services/Implementation/MarketDataService.cs Backend/StudiesApi.cs
grep -n 'AssumeUniversal' Backend/Services/Implementation/MarketDataService.cs Backend/StudiesApi.cs
```

## Suggested resolution (NOT auto-applied)

Per the ban list:

> Prefer: accept timestamps as numeric `long` (ms since epoch) and skip string parsing entirely. For string input, require `DateTimeOffset.ParseExact` (or `DateTime.ParseExact`) with `CultureInfo.InvariantCulture` and a format that includes an explicit offset designator; reject ambiguous/naive strings.

Two coordinated edits:
1. **Change the wire shape.** Python should emit `int64` ms-epoch (per `.claude/rules/numerical-rigor.md` "canonical format"). The .NET DTO field type changes from `string` to `long`. The parsing code disappears.
2. **If string is unavoidable for some reason**, replace `DateTime.Parse(..., AssumeUniversal | AdjustToUniversal)` with `DateTimeOffset.ParseExact(s, "yyyy-MM-ddTHH:mm:ss.fffzzz", CultureInfo.InvariantCulture)` and let it throw on naive input.

Cross-references: F-0009 (sanitizer producer side), F-0019 (parallel anti-pattern in Python).

## Provenance of the finding itself

Phase 3 / cursor: targeted grep of `DateTime.Parse(|DateTime.ParseExact(` across `Backend/`. 4 candidate files matched; this finding covers the 2 ingestion-path occurrences. The other 2 (Query.cs, ResearchService.cs) are split into F-0022.
