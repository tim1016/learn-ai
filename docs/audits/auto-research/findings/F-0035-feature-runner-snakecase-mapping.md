---
id: F-0035
severity: P0
status: fixed-verified
area: frontend-consumption
canonical_file: Frontend/src/app/components/research-lab/feature-runner/feature-runner.component.ts
reference: n/a
first_seen: 2026-05-07
last_seen: 2026-05-07
phase: 9
---

## What
`toResearchResult()` in `feature-runner.component.ts` cast four complex nested API response objects (`quantile_bins`, `robustness`, `feature_spec`, `validation_verdict`) directly to their camelCase TypeScript interfaces using `as` without converting any snake_case keys. At runtime the camelCase properties were all `undefined`, causing a `TypeError: Cannot read properties of undefined (reading 'length')` on `rob.monthlyBreakdown.length` inside `computeStabilityGrade()`. This crash fired during the first template render cycle (line 5, the grade card `@for`), silently killing the entire feature-report template — all metric cards, IC charts, quantile tables, and verdict blocks rendered empty.

## Where
- `Frontend/src/app/components/research-lab/feature-runner/feature-runner.component.ts:283–297` — original `toResearchResult()` casts
- The crash surface: `feature-report.component.ts:268–296` — `computeStabilityGrade()` → `rob.monthlyBreakdown.length`
- Confirmed via `ng.applyChanges()`: `TypeError` originated in `computeStabilityGrade` at `FeatureReportComponent_Template` line 5

## Why this severity
P0: a user-visible computation delivered authoritative-looking research results that were in fact completely empty. The feature validation page showed headers and labels but zero numbers. Crash was silently swallowed by Angular's error boundary.

## Reproduction
1. Run feature research via the Jobs path (AAPL, momentum_5m, 2024-01-01 to 2024-03-31).
2. Observe result loads ("Loaded from cache" banner) but all metric cards empty.
3. `ng.applyChanges(document.querySelector('app-feature-report'))` → `TypeError: Cannot read properties of undefined (reading 'length')` at `computeStabilityGrade`.

## Suggested resolution (NOT auto-applied)
Add dedicated `private mapRobustness()`, `mapQuantileBins()`, `mapFeatureSpec()`, `mapValidationVerdict()`, `mapScreen()` methods that explicitly translate each snake_case field to its camelCase equivalent.

## Fix applied
Added 7 private mapper methods and updated `toResearchResult()` to call them. All 4 nested objects now correctly translated. Verified: feature report renders all values (Mean IC: -0.0475, t-stat: -2.58, etc.) and results are internally consistent.

## Provenance of the finding itself
Discovered during live Research Lab end-to-end test session (2026-05-07). Found by running the feature research, observing empty DOM, confirming via `ng.getComponent()` that data was present but `.badge-stat` and `.metric-value` elements were empty, then triggering the crash via `ng.applyChanges()`.
