---
id: F-0018
severity: P2
status: open
area: inventory
canonical_file: docs/math-sources-of-truth.md (vs docs/architecture/numerical-authority-migration-plan.md)
reference: docs/architecture/numerical-authority-migration-plan.md (Status as of 2026-04-27, Phase 2.3)
first_seen: 2026-05-05
last_seen: 2026-05-05
phase: 1
---

## What

`docs/architecture/numerical-authority-migration-plan.md` line 24 (Status as of 2026-04-27) declares **Phase 2.3 SHIPPED**: *"`ComputeDollarDeltaAsync` + `ComputePortfolioVegaAsync` in `PortfolioRiskService.cs` switched from stored `EntryDelta`/`EntryVega` to `IPolygonService.PortfolioLiveGreeksAsync` (commit `334d419`). ... 7/7 tests pass."*

`docs/math-sources-of-truth.md` line 117 (§ "Known rule-5 non-compliance" item 3) still says: *"Phase 2.3 partial: remaining `ComputeDollarDeltaAsync` and `ComputePortfolioVegaAsync` still pick up entry Greeks for risk-rule evaluation; flagged with STALE-GREEK NOTICE comment in source pointing to `IPolygonService.PortfolioLiveGreeksAsync` follow-up."*

These two statements contradict each other. Migration plan says shipped; registry still describes the partial state.

## Where

- Migration plan: `docs/architecture/numerical-authority-migration-plan.md:24` (and §"Sequencing summary" line 271 marks Phase 2 as **shipped**)
- Registry: `docs/math-sources-of-truth.md:117` (item 3 of "Known rule-5 non-compliance")

## Why this severity

P2 — Drift between two governance docs that the contract uses as ground truth. A reviewer reading the registry to assess completeness will think Phase 2.3 is incomplete and flag it; a reviewer reading the migration plan thinks it's done. They reconcile by reading the commit (`334d419`), which is what governance docs are meant to prevent.

## Reproduction

```
grep -n 'Phase 2.3' docs/architecture/numerical-authority-migration-plan.md
grep -n 'Phase 2.3' docs/math-sources-of-truth.md
git show 334d419 --stat   # confirm the commit landed and what it touched
```

## Suggested resolution (NOT auto-applied)

Update `docs/math-sources-of-truth.md` § "Known rule-5 non-compliance" item 3 to match the migration plan:

> 3. **`Backend/Services/Implementation/PortfolioRiskService.cs` / `PortfolioValuationService.cs`** — shock-propagate from stored `EntryDelta` / `EntryVega` / `EntryTheta` instead of recomputing Greeks at current spot/time/IV. **Migration status (2026-05-05):** Phase 2.1 + 2.2 + 2.3 all shipped (Phase 2.3 in commit `334d419`, 2026-04-27 — `ComputeDollarDeltaAsync` and `ComputePortfolioVegaAsync` now call `IPolygonService.PortfolioLiveGreeksAsync`). This rule-5 violation is **fully closed**.

If the violation is fully closed, item 3 should move from "Known rule-5 non-compliance" to a "Resolved" subsection (or be deleted with a commit message recording the closure date).

## Provenance of the finding itself

Phase 1 / cursor: cross-check of `numerical-authority-migration-plan.md` § Status-as-of-2026-04-27 vs. `math-sources-of-truth.md` § "Known rule-5 non-compliance".
