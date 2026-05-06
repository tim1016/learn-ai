---
id: F-0022
severity: P1
status: deferred
area: timestamp
canonical_file: Backend/GraphQL/Query.cs; Backend/Services/Implementation/MarketDataService.cs; Backend/Services/Implementation/ResearchService.cs
reference: .claude/rules/numerical-rigor.md (Timestamp rigor → Ban list)
first_seen: 2026-05-06
last_seen: 2026-05-06
phase: 3
---

## What

Three .NET files parse user-supplied `fromDate` / `toDate` query strings via plain `DateTime.Parse(...)` without explicit format and without offset enforcement. Per the ban list, this is disallowed in any timestamp-canonicalization path — and the GraphQL date-range filter *is* a canonicalization path because the parsed value goes into EF queries against UTC `Timestamp` columns.

The pattern across the three files is the same: `DateTime.Parse(fromDate).ToUniversalTime()` (or no `ToUniversalTime()` at all, which is worse). When the input string has no offset designator, `Parse` returns `Kind=Unspecified`; `ToUniversalTime()` on `Unspecified` treats it **as local time** and shifts. So a user passing `"2024-01-01"` in a non-UTC server timezone gets a different DB query window than they think.

## Where

### Query.cs — 4 occurrences

```
Backend/GraphQL/Query.cs:206:    var from = DateTime.Parse(fromDate).ToUniversalTime();
Backend/GraphQL/Query.cs:207:    var to   = DateTime.Parse(toDate).ToUniversalTime().Date.AddDays(1).AddTicks(-1);
Backend/GraphQL/Query.cs:431:    var from = DateTime.Parse(range.FromDate).ToUniversalTime();
Backend/GraphQL/Query.cs:432:    var to   = DateTime.Parse(range.ToDate).ToUniversalTime().Date.AddDays(1).AddTicks(-1);
```

### MarketDataService.cs — 5 occurrences

```
Backend/Services/Implementation/MarketDataService.cs:119:    var from = DateTime.Parse(fromDate).ToUniversalTime();
Backend/Services/Implementation/MarketDataService.cs:120:    var to   = DateTime.Parse(toDate).ToUniversalTime().Date.AddDays(1).AddTicks(-1);
Backend/Services/Implementation/MarketDataService.cs:203:    var from = DateTime.Parse(fromDate);              // no UTC conversion at all
Backend/Services/Implementation/MarketDataService.cs:204:    var to   = DateTime.Parse(toDate);                // no UTC conversion at all
Backend/Services/Implementation/MarketDataService.cs:334:    var from = DateTime.Parse(fromDate);              // no UTC conversion at all
Backend/Services/Implementation/MarketDataService.cs:335:    var to   = DateTime.Parse(toDate);                // no UTC conversion at all
```

### ResearchService.cs — 2 occurrences

```
Backend/Services/Implementation/ResearchService.cs:367:    var startDate = DateTime.Parse(fromDate);
Backend/Services/Implementation/ResearchService.cs:368:    var endDate   = DateTime.Parse(toDate);
```

## Why this severity

P1 (not P0) because:

- These parse user-supplied date strings (date range filters), not Python-emitted timestamps where corruption from a misformatted producer would silently propagate downstream math.
- Most callers send `"YYYY-MM-DD"` from the Frontend, which `DateTime.Parse` accepts and where the timezone interpretation is explicit-by-convention (date-only).

But:

- `MarketDataService.cs:203/334` and `ResearchService.cs:367/368` skip `ToUniversalTime()` entirely and feed `Kind=Unspecified` straight into EF / chunking logic. Whatever EF does with `Unspecified` in a Postgres `timestamptz` query is implementation-defined.
- `Query.cs:206/207` and `:431/432` plus `MarketDataService.cs:119/120` use `ToUniversalTime()` on `Unspecified`, which silently treats the input as **local time**. On a server in ET, `"2024-01-01"` becomes `2024-01-01 00:00:00-05:00` → `2024-01-01 05:00 UTC`. On a UTC-configured server, same input → `2024-01-01 00:00 UTC`. **Same input, different DB query window depending on server timezone.**

## Reproduction

```
grep -n 'DateTime.Parse' Backend/GraphQL/Query.cs Backend/Services/Implementation/MarketDataService.cs Backend/Services/Implementation/ResearchService.cs
```

## Suggested resolution (NOT auto-applied)

Replace each occurrence with one of:

1. **Explicit format + offset enforcement**:
   ```csharp
   var from = DateTimeOffset.ParseExact(fromDate, "yyyy-MM-dd", CultureInfo.InvariantCulture).UtcDateTime;
   ```
   For date-only inputs, this treats the date as UTC midnight unambiguously.

2. **Change the wire type to `long` (ms-epoch)** if the GraphQL surface allows it — same recommendation as F-0021.

The 2 occurrences at `MarketDataService.cs:203/334` and the 2 at `ResearchService.cs:367/368` (no `ToUniversalTime()`) should be fixed first — those are silently feeding `Unspecified` into queries and are the most surprising failure mode.

## Provenance of the finding itself

Phase 3 / cursor: same grep pass as F-0021. Splits the 11 .NET `DateTime.Parse` matches into ingestion-path (F-0021) and query-parameter-path (this finding).

## Notes on the 4th candidate file

`Backend/StudiesApi.cs` had additional `DateTime.Parse` matches near `ParseUtc` (line 295) — that helper is covered in F-0021 as the second ingestion-path occurrence. Other matches in StudiesApi.cs would be subsumed under similar treatment if they exist; a deeper StudiesApi.cs sweep is owed.
