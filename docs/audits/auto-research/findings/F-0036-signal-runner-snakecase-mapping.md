---
id: F-0036
severity: P1
status: fixed-verified
area: frontend-consumption
canonical_file: Frontend/src/app/components/research-lab/signal-runner/signal-runner.component.ts
reference: n/a
first_seen: 2026-05-07
last_seen: 2026-05-07
phase: 9
---

## What
`toSignalEngineResult()` in `signal-runner.component.ts` cast 11 complex nested objects (`backtestGrid`, `walkForward`, `graduation`, `signalDiagnostics`, `dataSufficiency`, `effectiveSample`, `jointRegimeCoverage`, `signalBehavior`, `oosSharpeCi`, `deflatedSharpe`, `methodology`) via `as SignalEngineResult['field']` without any key conversion. The developer comment claimed "the report component already tolerates both forms" — false. The `signal-report.component.html` template accesses camelCase properties (`wf.meanOosSharpe`, `meth.trainMonths`, `wf.combinedOosDates`, etc.) that are `undefined` when the data is snake_case. Most critically, line 460 called `wf.combinedOosDates.length` — a guaranteed TypeError once `walkForward` is non-null but its `combinedOosDates` key is missing.

## Where
- `signal-runner.component.ts:283–298` — all 11 `as` casts
- `signal-report.component.html:460` — `@if (wf.combinedOosDates.length > 0)` crashes on snake_case `wf`
- `signal-report.component.html:104,108,316,388` — empty render for `meth.trainMonths`, `meth.windowType`, `wf.meanOosSharpe`, `wf.medianOosSharpe`

## Why this severity
P1 (not P0): the signal engine result is a less frequently hit path than feature validation, and the crash at line 460 only triggers when `walkForward` is non-null, making it intermittent. However, all nested metric fields would render empty silently in every run.

## Reproduction
1. Run signal engine job via the Jobs path (any ticker + feature).
2. When `walkForward` results are returned, template crashes at `wf.combinedOosDates.length`.
3. Even without crash: `meth.trainMonths`, `wf.meanOosSharpe`, all graduation criteria render empty.

## Fix applied
1. Added 15 private mapper methods for all nested structures (`mapBacktestGrid`, `mapWalkForward`, `mapWalkForwardWindow`, `mapAlphaDecay`, `mapGraduation`, `mapGraduationCriterion`, `mapStageAdvanceCriterion`, `mapSignalDiagnostics`, `mapDataSufficiency`, `mapEffectiveSample`, `mapJointRegimeCoverage`, `mapSignalBehavior`, `mapSharpeCi`, `mapDeflatedSharpe`, `mapMethodology`).
2. Updated `toSignalEngineResult()` to call all mappers.
3. Fixed template crash: `wf.combinedOosDates.length > 0` → `wf.combinedOosDates?.length` in `signal-report.component.html`.

## Provenance of the finding itself
Found by scan agent during Research Lab frontend review (2026-05-07), immediately after F-0035 was fixed using the same pattern. The signal-runner's developer comment explicitly acknowledged the snake_case issue but incorrectly claimed the template "tolerates both forms."
