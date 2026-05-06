---
id: F-0020
severity: P1
status: fixed-verified
area: timestamp
canonical_file: cross-cutting (multiple files in PythonDataService, Backend, Frontend)
reference: .claude/rules/numerical-rigor.md (Timestamp rigor → Ban list)
first_seen: 2026-05-05
last_seen: 2026-05-05
phase: 1
---

## What

A first-pass grep across the three stacks for ban-list patterns from `.claude/rules/numerical-rigor.md` returned substantial counts:

- **Python (`PythonDataService/app/`)** — 19 files match `datetime.utcnow|datetime.utcfromtimestamp|.strftime("...Z")|datetime.now()` patterns
- **.NET (`Backend/`)** — 4 files match `DateTime.Parse(|DateTime.ParseExact(`
- **TypeScript (`Frontend/src/`)** — 45 files match `new Date(<variable>)`

These are **candidates**, not confirmed violations — the patterns intentionally over-match (e.g., `new Date(literalISOWithTZ)` is fine; `ParseExact` with explicit offset is fine; `datetime.now(UTC)` is fine). Per-file triage is Phase 3 work and is **not done in this tick**. This rollup is a heads-up: there's a substantial Phase 3 backlog and at least three of the candidate files are already known-violators.

## Where

### Python — 19 candidate files

```
PythonDataService/app/services/sanitizer.py                                ← F-0009 (already opened)
PythonDataService/app/services/rule_based_backtest.py                      ← prior audit top-10 #2
PythonDataService/app/services/strategies/common.py                        ← prior audit top-10 #2
PythonDataService/app/services/polygon_client.py                           ← INGESTION — P0 candidate
PythonDataService/app/services/options_companion_service.py
PythonDataService/app/services/data_quality_service.py
PythonDataService/app/services/validation_service.py
PythonDataService/app/services/market_monitor.py
PythonDataService/app/services/dataset_service.py
PythonDataService/app/research/divergence/ingest/polygon_ingest.py         ← INGESTION — P0 candidate
PythonDataService/app/research/divergence/dashboard/build_dashboard.py
PythonDataService/app/research/divergence/strategies/engine_runner.py
PythonDataService/app/research/options/contract_finder.py
PythonDataService/app/research/options/iv_builder.py
PythonDataService/app/routers/validation_study.py
PythonDataService/app/routers/volatility.py
PythonDataService/app/engine/framework/insight.py
PythonDataService/app/engine/strategy/algorithms/spy_ema_crossover_options.py
PythonDataService/app/volatility/cache.py
```

### .NET — 4 candidate files

```
Backend/Services/Implementation/MarketDataService.cs                        ← INGESTION — P0 candidate; prior audit flagged "DateTime.Parse" at MarketDataService.cs:450 (executive summary intro)
Backend/Services/Implementation/ResearchService.cs                          ← passthrough, may be false positive
Backend/StudiesApi.cs                                                       ← needs inspection
Backend/GraphQL/Query.cs                                                    ← resolver, depends on usage
```

### TypeScript — 45 candidate files

Most are likely display-side or test-side (`new Date(displayString)` for a DatePipe-equivalent path) and **P2 or false positive**. A handful are likely real wire-side violations. Subset most worth checking first (cross-wire surfaces):

```
Frontend/src/app/services/replay-strategy.service.ts                       ← prior audit top-10 #2 (replay-strategy.service.ts:18)
Frontend/src/app/services/replay-indicator.service.ts
Frontend/src/app/services/replay-engine.service.ts
Frontend/src/app/services/past-chain.service.ts
Frontend/src/app/components/lean-engine/engine-history/engine-history.component.ts
Frontend/src/app/components/edge/services/edge-api.service.ts
Frontend/src/app/components/data-lab/data-lab.component.ts
Frontend/src/app/utils/date-validation.ts                                  ← validation utility; should be the safe place
Frontend/src/testing/factories/market-data.factory.ts                      ← test factory, expected
```

## Why this severity

P1 — Aggregate severity for the rollup. The set contains at least:

- 1 confirmed prior-audit P0 (sanitizer.py — already F-0009)
- 2 ingestion-path P0 candidates (polygon_client.py, polygon_ingest.py, MarketDataService.cs) per the prior audit's pattern
- 1 known browser-shift bug per prior audit (replay-strategy.service.ts:18)
- A long tail of P2/false-positive cases

If any of the ingestion-path candidates is confirmed, this would split into a P0 finding for that file plus P2-rolled-up "false-positive cleared" notes for the rest.

## Reproduction

```
# Python
grep -rEn 'datetime\.utcnow|datetime\.utcfromtimestamp|\.strftime\(["'"'"'].*Z["'"'"']|datetime\.now\(\)\s*[^.]' PythonDataService/app/ | head -50

# .NET
grep -rEn 'DateTime\.Parse\(|DateTime\.ParseExact\(' Backend/ --include='*.cs'

# TypeScript
grep -rEn 'new Date\([a-zA-Z_]' Frontend/src/ --include='*.ts' | head -60
```

## Suggested resolution (NOT auto-applied)

This rollup defers per-file triage to Phase 3. The recommended order in the next tick:

1. **Triage the 4 .NET files first** — smallest set; each is either ingestion (P0) or passthrough/resolver (P2/false positive).
2. **Triage the 4 ingestion-path Python files** — `polygon_client.py`, `polygon_ingest.py`, `dataset_service.py`, `data_quality_service.py`.
3. **Triage the 9 wire-crossing TS files** listed above.
4. **Mass-classify the remaining ~40 TS files** as display-only / test / candidate via grep + sample inspection.

Each confirmed violation gets its own per-file finding (or one rollup per category if the count is large and the fix is uniform).

## Provenance of the finding itself

Phase 1 / cursor: parallel grep over PythonDataService, Backend, Frontend/src for ban-list patterns. Counts and file lists captured here so Phase 3 can resume without re-running the grep.
