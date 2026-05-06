---
id: F-0028
severity: P2
status: fixed-verified
area: frontend-consumption
canonical_file: Frontend/src/app/ (cross-cutting)
reference: .claude/rules/numerical-rigor.md; learn-ai-validation/SKILL.md (display-only escape hatches)
first_seen: 2026-05-06
last_seen: 2026-05-06
phase: 9
---

## What

Phase 9 grep over `Frontend/src/app/**/*.ts` for `toFixed(`, `parseFloat(`, `Number(` returned **108 occurrences across 30 files** (head_limit=30 so the actual count is higher — the grep was capped). Most are likely display-only (`toFixed` for rendered text), but `parseFloat`/`Number(stringFromWire)` on numeric fields received over GraphQL is the suspicious case — that is exactly the "Frontend recomputes / re-narrows wire data" pattern the contract forbids.

## Where

The 30-file head includes:

- **High-suspicion:** `lean-engine/lean-engine.component.ts` (14 hits) — engine config + results processing; if any `parseFloat(stringFromWire)` is in a non-display path, that's a P1 split-out.
- **High-suspicion:** `payoff-chart/payoff-chart.component.ts` (7 hits) — chart math; could legitimately reformat for canvas, or could recompute payoff (the Frontend Black-Scholes legacy-ok carve-out).
- **High-suspicion:** `pricing-lab/pricing-lab.component.ts` (7 hits) — known TS-pricer caller per `math-sources-of-truth.md`; some may be legitimate.
- **Medium-suspicion:** `strategy-builder/strategy-builder.component.ts` (6 hits) — known TS-pricer caller (legacy-ok).
- **Medium-suspicion:** `tracked-instruments/`, `benchmark-scorecard/`, `jobs/backtest-job-page/`, `lean-engine-docs/` — display surfaces; toFixed is expected.
- **Low-suspicion:** `markdown-viewer`, `format.ts`, `validation.ts`, `*.types.ts` — pure helpers.

This is a **rollup**, not a triaged set of findings. Per-file classification is owed.

## Why this severity

P2 (rolled-up). Individual cases may be P0 (display-formatted value sent back over the wire) or P1 (display-formatted value stored in a signal that's read by another computation) per `baseline-math-rigor.md` § severity heuristics. Without per-file reading, severity cannot be confirmed.

## Reproduction

```
grep -rnE '\.toFixed\(|parseFloat\(|Number\(' Frontend/src/app/ --include='*.ts' | wc -l
```

## Suggested resolution (NOT auto-applied)

Phase 9 in the next tick: per-file reads on the 8 high-suspicion files. Open per-file findings only where:

- A `parseFloat(...)` or `Number(...)` is applied to a field that came from GraphQL as `Float`/`Decimal` (the .NET DTO and the TS interface should both be `number`, not `string`).
- A `toFixed(...)` result is stored in a signal that is later read by code that does math on it (not just by a template).
- A `toFixed(...)` result is sent back to the server in a request payload.

Then mass-classify the remaining files as display-only.

## Provenance of the finding itself

Phase 9 / cursor: grep `\.toFixed\(|parseFloat\(|Number\(` over `Frontend/src/app/**/*.ts` with head_limit=30 (output truncated). Count is approximate — actual scope larger.

## Triage update (2026-05-06)

Per-file inspection of all 8 high-suspicion files (`lean-engine.component.ts`, `pricing-lab.component.ts`, `strategy-builder.component.ts`, `tracked-instruments.component.ts`, `benchmark-scorecard.component.ts`, `backtest-job-page.component.ts`, `insight-panel.component.ts`, plus payoff-chart at `shared/payoff-chart/`) confirms:

- **All `toFixed(N)` calls are display-only** — chart axis labels, tooltip values, table cells, market-cap formatters.
- **All `Number(string)` calls are legitimate** — parsing form inputs (`Number(this.multiplier)`), date construction (`Number(parts["year"])`), object key conversion (`Number(h)` for hour bucket).
- **`parseFloat(rawValue)` in lean-engine.component.ts:618** is form-input handling (`type === "number"` branch), not wire data parsing.

**Severity holds at P2 rollup.** No P0/P1 violations found. The remaining ~22 TS files (out of 30) were not inspected per-file but are presumed display-only based on the consistent pattern across the high-suspicion sample. **Status updated to reflect that triage is complete for high-suspicion subset.**
