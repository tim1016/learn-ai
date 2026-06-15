---
id: VCR-0005
severity: P1
status: remediated
area: architectural-drift
canonical_file: Backend/Services/Implementation/PortfolioValuationService.cs:111
reference: docs/architecture/numerical-authority-migration-plan.md § Phase 2.2/2.3
first_seen: 2026-06-14
last_seen: 2026-06-14
remediated_in: "Phase 9 — hard-deleted EntryDelta/EntryGamma/EntryTheta/EntryVega summation in PortfolioValuationService; removed NetDelta/NetGamma/NetTheta/NetVega from PortfolioValuation DTO, PortfolioSnapshot entity, EF model, and Frontend types; added DropPortfolioSnapshotGreekColumns EF migration"
lens: architectural-drift-registries
dedupe_with_F: ["F-0018 — extends; F-0018 was closed by editing only documentation, the underlying code remains"]
confidence: high
---

## What

F-0018 (closed 2026-05-06) recorded the registry-vs-migration-plan drift on Phase 2.3 and updated `math-sources-of-truth.md` item 3 to read *"CLOSED. … `ComputeDollarDeltaAsync` + `ComputePortfolioVegaAsync` were switched from stored entry Greeks to `IPolygonService.PortfolioLiveGreeksAsync`."* The sweep was incomplete. The migration plan's Phase 2.2/2.3 explicitly named BOTH `PortfolioValuationService.cs` AND `PortfolioRiskService.cs`. Only `PortfolioRiskService.cs` was migrated.

`PortfolioValuationService.cs::ComputeValuationInternal` still aggregates stale entry Greeks:

```csharp
netDelta = (netDelta ?? 0) + (legs.EntryDelta ?? 0) * position.NetQuantity * multiplier;
netGamma = (netGamma ?? 0) + (legs.EntryGamma ?? 0) * position.NetQuantity * multiplier;
netTheta = (netTheta ?? 0) + (legs.EntryTheta ?? 0) * position.NetQuantity * multiplier;
netVega  = (netVega  ?? 0) + (legs.EntryVega  ?? 0) * position.NetQuantity * multiplier;
```

The returned `PortfolioValuation` exposes `NetDelta/NetGamma/NetTheta/NetVega`. The `getPortfolioValuation` GraphQL resolver surfaces them. `Frontend/src/app/services/portfolio.service.ts:109` explicitly queries `netDelta netGamma netTheta netVega`. `SnapshotService.cs:170-173` persists these values into `PortfolioSnapshot` rows that feed `getEquityCurve` and `getPortfolioSnapshots` — so stale Greeks become permanent historical data.

The migrated `getDollarDelta` / `getPortfolioVega` paths return Greeks recomputed via Python. A consumer comparing the two will see undocumented divergence proportional to spot/time/IV drift since entry. The registry row at `math-sources-of-truth.md:87` labels `ComputeValuationInternal` "compliant — pure aggregation" but is silent about the stored-Greeks aggregation embedded in it.

## Where

- `Backend/Services/Implementation/PortfolioValuationService.cs:100-115` — stale entry-Greek aggregation lines.
- `Backend/Services/Implementation/PortfolioValuationService.cs:126-129` — surfaces NetDelta/NetGamma/NetTheta/NetVega in returned `PortfolioValuation`.
- `Backend/GraphQL/PortfolioQuery.cs:79-85` — `getPortfolioValuation` resolver.
- `Frontend/src/app/services/portfolio.service.ts:104-114` — frontend selects `netDelta netGamma netTheta netVega`.
- `Backend/Services/Implementation/SnapshotService.cs:170-173` — persistence of these fields into `PortfolioSnapshot`.
- `docs/math-sources-of-truth.md:87` — registry row claims "compliant — pure aggregation".
- `docs/architecture/numerical-authority-migration-plan.md:171-179` — Phase 2.2/2.3 exit criterion violated.
- `docs/audits/auto-research/findings/F-0018-migration-plan-vs-registry-phase-2-3-drift.md` — closure note overclaimed.

## Why this severity

PRD §7 P1: "UI implies guarantees the backend/runtime does not enforce." The GraphQL field is named `netDelta` (current portfolio delta) and the registry says "compliant — pure aggregation" — both promise the value is what a downstream consumer would expect (live Greeks aggregated). The actual value is stored-at-entry Greeks × position quantity, diverging from `getDollarDelta` for any moved position.

Not P0 because no Frontend template currently binds the NetDelta fields (the service queries them but no component renders them today); risk-rule evaluation uses the already-migrated Risk path. But:
- The values are persisted into the snapshot history table — every snapshot polluted.
- The GraphQL contract publishes the field name as if it were current.
- An upcoming dashboard tile, risk-cap engine, or operator-facing summary that reads `netDelta` will silently consume stale Greeks.

## Trading impact

- A future portfolio summary card that binds `getPortfolioValuation.netDelta` would mislead operator hedging decisions.
- `PortfolioSnapshot.NetDelta/NetGamma/NetTheta/NetVega` columns accumulate as "historical portfolio Greeks" — any time-series analysis on those tables silently uses stale Greeks.
- Comparing `getPortfolioValuation.netDelta` against `getDollarDelta.delta` for the same position shows undocumented divergence proportional to spot/time/IV drift since the option was opened.

Risk profile: drift in **reporting / persistence**, not in order execution. The Risk-rule evaluation path (already migrated to Python) is the only consumer of live Greeks that gates actions today.

## Reproduction

```bash
# 1. Confirm stale entry-Greek aggregation present:
grep -n "EntryDelta\|EntryGamma\|EntryTheta\|EntryVega" \
  Backend/Services/Implementation/PortfolioValuationService.cs

# 2. Confirm GraphQL exposes:
grep -n "getPortfolioValuation\|NetDelta" Backend/GraphQL/PortfolioQuery.cs

# 3. Confirm Frontend queries:
grep -n "netDelta netGamma" Frontend/src/app/services/portfolio.service.ts

# 4. Confirm SnapshotService persists:
grep -n "valuation.NetDelta" Backend/Services/Implementation/SnapshotService.cs

# 5. Confirm Frontend has no consumer renderer today:
grep -rn "netDelta\b\|netGamma\b\|netTheta\b\|netVega\b" Frontend/src/app/components
# Expect: zero hits → fields exist on the wire but no template binds them yet.
```

## Suggested resolution (NOT auto-applied)

Prefer **completing Phase 2.3** to amending the registry. Options ordered by preference:

1. **Remove the aggregation entirely.** Delete the OptionLeg-Greek aggregation in `ComputeValuationInternal`; drop `NetDelta/NetGamma/NetTheta/NetVega` from the returned `PortfolioValuation` (and from the SnapshotService persistence) until a consumer needs them. The `PortfolioRiskService` already serves the live-Greek path; valuation should be pure mark-to-market in dollars.
2. **Or wire to Python `/portfolio/live-greeks`.** If a portfolio-summary card consumer materializes, route the request through the existing Python `PortfolioLiveGreeksAsync` and aggregate the recomputed Greeks — the same pattern Phase 2.3 used for `ComputeDollarDelta`/`ComputePortfolioVega`.
3. **Minimum** — update the registry row at `math-sources-of-truth.md:87` to disclose the stale-Greek aggregation honestly ("returns stored entry-time Greeks × quantity, not current portfolio Greeks; do not use for hedging decisions"). And re-open F-0018 closure note to clarify scope.

Add a regression test that compares `getPortfolioValuation.netDelta` against summed-via-Python live Greeks for the same option set under a small spot move — fails until either option (1) or (2) lands.

## Provenance of the finding

Lens: `architectural-drift-registries` (workflow `wf_def78013-ce4`, structured-finding `portfolio-valuation-stale-entry-greek-aggregation`, verified 2/2 by adversarial pass). Confirmed by direct read of all six cited file:line ranges plus negative grep of Frontend components (no current consumer).
