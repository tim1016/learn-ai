---
id: VCR-0015
severity: P2
status: open
area: documentation
canonical_file: docs/architecture/numerical-authority-migration-plan.md
reference: docs/architecture/adrs/0009-live-sizing-authority-and-provenance.md
first_seen: 2026-06-14
last_seen: 2026-06-14
lens: architectural-drift-registries
dedupe_with_F: ["F-0018 — extends; F-0018 was scoped to PortfolioRiskService migration drift"]
confidence: high
---

## What

The migration plan (status header: *"Active — Phase 0/1/2 shipped; Phase 3 and Phase 4 reformulated"*, last "Status as of" note: 2026-04-27) is the third governance doc named by AGENTS.md / CLAUDE.md as one of three registries that must agree. It enumerates the three known rule-5 violations as of 2026-04-26 (BacktestService.cs, black-scholes.ts, PortfolioRiskService.cs), provides exit criteria, and sequences migration.

Between 2026-06-08 (ADR 0009 proposed) and 2026-06-13 (ADR 0009 PR2-PR7 catch-up shipped), an additional rule-5-relevant migration was completed: the live-sizing path retired `SimpleFloorSizing` for `LeanSetHoldingsSizing` via the new `order_sizer.py` policy adapter. `math-sources-of-truth.md` (line 33, 62) was updated in the same PR. The engine-authority-map (line 33) was updated. The migration plan was not.

The plan now silently omits a real migration that exists, exit criteria are missing for it, and its sequencing-summary table (lines 267-273) has no row for it. A contributor following AGENTS.md's "Active migrations are sequenced in…" instruction will not see that SetHoldings live sizing is now LEAN-faithful with buffered/fee-aware semantics and may attempt to wire `SimpleFloorSizing` into the live path again.

A separate issue noted on review: line 271 ("Phase 2 — portfolio scenario / live-Greeks: shipped") implies BOTH PortfolioRiskService AND PortfolioValuationService cleanup completed, but only the Risk path was migrated. See VCR-0005.

## Where

- `docs/architecture/numerical-authority-migration-plan.md:3-7` — status header (stale).
- `docs/architecture/numerical-authority-migration-plan.md:267-275` — sequencing-summary table (no live-sizing row).
- `docs/architecture/adrs/0009-live-sizing-authority-and-provenance.md` — the missing migration narrative.
- `docs/math-sources-of-truth.md:62` — registry has the live-sizing entry (the contrast).
- `docs/architecture/engine-authority-map.md:33` — engine map has the entry (the contrast).

## Why this severity

PRD §7 P2: moderate auditability / stale-doc. Two reviewers split P1 vs P2 — P1 because the migration plan is named in the authority hierarchy and a stale plan breaks the three-registry contract; P2 because the other two registries (math-sources-of-truth, engine-authority-map) are current and provide a safety net. Settling at P2 acknowledges the safety net while still requiring repair.

## Trading impact

Indirect — code is correct (the new `order_sizer.py` is what `LivePortfolio.set_holdings` actually calls, with pinned regression tests at `tests/engine/execution/test_order_sizer.py` and `tests/engine/live/test_live_portfolio.py`). The risk is that a future contributor reads the migration plan as "the prioritized backlog" and concludes a migration that just happened still needs doing — or worse, attempts to revert it.

## Reproduction

```bash
grep -nE 'ADR 0009|order_sizer|SimpleFloor|sizing' docs/architecture/numerical-authority-migration-plan.md
# Zero matches today.
```

## Suggested resolution (NOT auto-applied)

Add a "Phase 5 — Live-sizing migration (ADR 0009)" section with the shipped status of PR1-7, the deliberate SimpleFloor → Lean live-path cutover, the regression test names, and the exit criteria already met. Update the sequencing summary table with a Week 4 row. Update the header status to *"… Phase 0/1/2 shipped; Phase 3/4 reformulated; Phase 5 (live sizing) shipped 2026-06-13"*. Update the Phase 2 row to honestly disclose that PortfolioValuationService cleanup did not ship (see VCR-0005).

## Provenance of the finding

Lens: `architectural-drift-registries` (workflow `wf_def78013-ce4`, structured-finding `numerical-migration-plan-stale-no-adr-0009-awareness`, verified 2/2 by adversarial pass, one reviewer recommended downgrade to P2).
