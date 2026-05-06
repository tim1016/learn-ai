---
id: F-0032
severity: P3
status: wontfix
area: wire
canonical_file: Backend/Services/Implementation/PolygonService.cs; Backend/Models/DTOs/{ResearchModels,SignalModels,BatchResearchModels}.cs
reference: .claude/rules/numerical-rigor.md (Wire fidelity)
first_seen: 2026-05-06
last_seen: 2026-05-06
phase: 8
---

## What

Phase 8 sample-sweep for `(double)` casts in `Backend/` reveals **systematic `decimal → double` narrowing at the .NET ↔ Python wire boundary** on outbound requests, plus 3 DTO files with `double`/`float`-typed properties at the wire layer (inbound shape).

## Where

### Outbound: PolygonService.cs casts to double on every Python call

```csharp
// PolygonService.cs:692-700 (single-leg pricing request to Python)
spot = (double)spot,
strike = (double)strike,
risk_free_rate = (double)riskFreeRate,
volatility = (double)volatility,
expiration_date = expirationDate,
evaluation_date = evaluationDate,
dividend_yield = (double)dividendYield,

// PolygonService.cs:737-750 (multi-leg portfolio scenario request)
spot = (double)spot,
legs.Select(l => new {
    strike = (double)l.Strike,
    iv = (double)l.Iv,
    premium = (double)l.Premium,
    ...
}),
risk_free_rate = (double)riskFreeRate,
dividend_yield = (double)dividendYield,
```

The `decimal` in C# is 28-29 significant digits; `double` is ~15-17. The narrowing happens at **every** Python call.

### Inbound: 3 DTO files declare `double`/`float` properties

```
Backend/Models/DTOs/ResearchModels.cs    — has public double/float properties
Backend/Models/DTOs/SignalModels.cs       — has public double/float properties
Backend/Models/DTOs/BatchResearchModels.cs — has public double/float properties
```

These are the wire shapes Python responses deserialize into. If Python is computing in `decimal.Decimal` and the .NET DTO declares `double`, the deserialization narrows.

### Adjacent: BacktestService.cs:449 — internal narrow-then-widen

```csharp
var stdDev = (decimal)Math.Sqrt((double)variance);
```

`System.Math.Sqrt` only accepts `double`, so the round-trip is unavoidable in pure-.NET stat helpers. Acceptable but worth noting in the registry's max-drawdown / Sharpe rows since this introduces a precision loss that the canonical Python `engine/results/statistics.py` does NOT have (Python computes `Decimal`-aware where the reference does).

## Why this severity

P2 — Direction matters:

- **Outbound narrowing (`(double)spot`, `(double)strike`)**: low impact. Python is the canonical math authority; the inbound request is the user-supplied parameter. `decimal → double` is an acceptable lossy step for parameters that originally came from a UI numeric input. Still a wire-fidelity concern because the contract says preserve the value.
- **Inbound DTO `double` properties**: higher impact. If Python computes a Greek to 12 sig figs in `Decimal` and .NET deserializes into `double`, the rendered value is a narrow approximation. Whether the user's UI shows the difference depends on how many sig figs are displayed.
- **Internal narrow-then-widen** (BacktestService.cs:449): only matters until F-0011 (drawdown moves to Python).

For F-0011 (.NET drawdown duplicate) and F-0010 (FIFO accounting), this F-0032 confirms the narrowing risk.

## Reproduction

```
grep -nE '\(double\)' Backend/Services/Implementation/*.cs
grep -lE '(public|private)\s+(double|float)\s+\w+\s*\{' Backend/Models/DTOs/*.cs
```

## Suggested resolution (NOT auto-applied)

Per the rules' wire-fidelity heuristic ("Type narrowing on a numeric field that crosses the wire → P1", which I'm holding at P2 here for the outbound case):

1. **Outbound (PolygonService.cs):** acceptable as-is for parameters; document the precision contract in the registry's Black-Scholes row. If a future caller has `decimal` arithmetic upstream of the cast, the cast becomes the precision floor.
2. **Inbound DTOs (`ResearchModels.cs`, `SignalModels.cs`, `BatchResearchModels.cs`):** open the 3 files and audit each `double`/`float` property — if it's a Greek, IV, or PnL field that the user compares against another number, change to `decimal` (and document the JSON converter shape on the way in).
3. **Internal narrow-then-widen at BacktestService.cs:449:** subsumed by F-0011 (drawdown moves to Python), no separate fix needed.

## Provenance of the finding itself

Phase 8 / cursor: targeted grep of `(double)` in `Backend/`. Sample-sweep — full per-file audit of the 3 DTO files is owed.

## Triage update (2026-05-06): DTO audit complete

Full read of `ResearchModels.cs`, `SignalModels.cs`, `BatchResearchModels.cs` shows the inbound DTO `double` properties match Python's computation precision. Specifically:

- **Research statistics (IC, t-stat, p-value, mean drawdown, Sharpe, etc.)** — computed in Python via numpy/scipy as `float64`, which is bit-equivalent to .NET `double`. **No precision loss.**
- **IV measurements (`AtmIv`, `IvOtmPut`, `IvOtmCall`, etc. in `BatchResearchModels.cs`)** — computed by `bs_greeks.py` / `volatility/solver.py` in `float64` per the cross-engine parity test (`test_bs_cross_engine_parity.py` at `atol=1e-10`). `double` on the .NET side preserves this.
- **`BuildIvHistoryResponseDto.IvData : List<Dictionary<string, object?>>`** — loose typing (P3 hygiene; not a precision issue, but auditability is poor).

**Outbound `(double)` casts in `PolygonService.cs`** are similarly benign: the .NET `decimal` source is itself a UI/database approximation, and Python receives `float64` regardless.

**Severity dropped from P2 → P3.** The narrowing is consistent with canonical computation precision throughout the chain. Held open as P3 for the auditability concern (loose `Dictionary<string, object?>` and the lack of explicit precision contract documented in the registry).

If a future canonical math function is rewritten to use `Decimal` instead of `float64` in Python (which the rules permit when accumulation precision matters), the DTO `double` fields would need to widen — but that's a future concern.
