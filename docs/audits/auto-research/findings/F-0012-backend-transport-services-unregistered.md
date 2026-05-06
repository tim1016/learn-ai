---
id: F-0012
severity: P2
status: open
area: inventory
canonical_file: Backend/Services/Implementation/{SanitizationService,ResearchService,SpecStrategyService,PortfolioService}.cs
reference: missing
first_seen: 2026-05-05
last_seen: 2026-05-05
phase: 1
---

## What

Four Backend services are pure transport / persistence (no canonical math) but have no row in `docs/math-sources-of-truth.md` § "Data / transport (rule-5 compliant by design)". Adding rows would close the inventory loop and make their compliant role explicit.

## Where

- `Backend/Services/Implementation/SanitizationService.cs` — HTTP passthrough to Python `/api/sanitize` (transport).
- `Backend/Services/Implementation/ResearchService.cs` — HTTP passthrough to Python research endpoints (transport + EF persistence).
- `Backend/Services/Implementation/SpecStrategyService.cs` — explicitly self-described as "Thin passthrough to PythonDataService /api/spec-strategy/backtest" (transport).
- `Backend/Services/Implementation/PortfolioService.cs` — order/account persistence + dispatch to PositionEngine (persistence; the math is in PositionEngine, see F-0010).

## Why this severity

P2 — These are fine as-is; the registry just doesn't know about them. Adding rows is a pure documentation change that closes the inventory question "is every Backend service classified?"

## Reproduction

Static — no test.

## Suggested resolution (NOT auto-applied)

Add four rows to `docs/math-sources-of-truth.md` § "Data / transport (rule-5 compliant by design)":

| Concept | Canonical | Legacy / duplicates | Reference | Validated against | Status |
|---|---|---|---|---|---|
| Sanitization endpoint fan-out | `Backend/Services/Implementation/SanitizationService.cs` | — | n/a (transport) | `Backend.Tests/Unit/Services/SanitizationServiceTests.cs` (if exists) | **not math** — HTTP passthrough to Python `/api/sanitize`. |
| Research endpoint fan-out | `Backend/Services/Implementation/ResearchService.cs` | — | n/a (transport + persistence) | (existing tests) | **not math** — HTTP passthrough plus EF persistence of research reports. |
| Spec strategy backtest passthrough | `Backend/Services/Implementation/SpecStrategyService.cs` | — | n/a (transport) | (existing tests) | **not math** — pure passthrough to Python `/api/spec-strategy/backtest`. |
| Portfolio account / order persistence | `Backend/Services/Implementation/PortfolioService.cs` | — | n/a (persistence; math in `PositionEngine.cs` per F-0010) | `Backend.Tests/Unit/Services/PortfolioServiceTests.cs` (if exists) | **persistence** — order/account writes, dispatches to PositionEngine for math. |

## Provenance of the finding itself

Phase 1 / cursor: `Backend/Services/Implementation/*.cs` head reads.
