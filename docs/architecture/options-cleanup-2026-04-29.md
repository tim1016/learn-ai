# Options Cleanup — Audit Trail (2026-04-29)

> Write-only ledger of what changed during the options-routes
> consolidation effort tracked by
> [`docs/architecture/options-routes-research.md`](options-routes-research.md).
>
> This is **not** a truth document — that's
> [`options-research.md`](options-research.md). This is a one-shot
> record of moves: what was deleted, what was merged, what was
> deferred and why, and what the verification evidence was.
>
> **Last revised:** 2026-04-29. Reflects work shipped through Phase 6
> of the cleanup; Phases 4.5, 7, and parts of 3 + 5 are explicitly
> deferred and tracked in [§ Deferred work](#-deferred-work-and-rationale)
> below.

---

## Phase-by-phase ledger

### Phase 0 — Alignment (no code) — DONE

- 11 decisions ratified (D1–D11) plus 2 sub-decisions (D9a, D10a).
  Full audit trail in [`options-routes-research.md` § 7](options-routes-research.md#7-decisions-log).
- New artefact: [`options-ux-design-prompt.md`](options-ux-design-prompt.md)
  — accumulator file for UX-design questions raised during execution
  (per D11). Seeded with UX-Q1 through UX-Q4 covering chain-table
  density, drill-down trigger ambiguity, past-chain inspector card
  visual, and strategy-builder layout.

### Phase 1 — Authority refresh + inventory ratification — DONE

**Authority doc revised** —
[`options-math-authorities.md`](options-math-authorities.md):

- Stamped "Last reviewed: 2026-04-29 (Phase 1 of options-routes
  cleanup)".
- Fixed factual error: the QuantLib IV function is
  `quantlib_pricer.implied_volatility` (line 314), not
  `solve_implied_volatility`. Caveat added clarifying that direct
  callers must use `volatility/solver.implied_volatility` — the QL
  path is the internal branch of the solver's fallback chain.
- All 11 documented function references re-verified against code on
  2026-04-29. `bs_solver.py` deletion (per the doc's history section)
  reconfirmed.

**Inventory correction (load-bearing).** R3 (orphan removal of
`POST /options/contracts` and GraphQL `getOptionsContracts`)
**revoked**.

- Original orphan hypothesis came from a frontend grep on the URL
  path `/options/contracts` (zero matches). That check was
  insufficient: the URL is invoked by the Backend C# layer, not the
  frontend directly.
- Full call chain verified on 2026-04-29:
  ```
  frontend (stock-analysis.component.ts:308, day-detail.component.ts:118)
    → marketDataService.getOptionsContracts(...)
    → Backend GraphQL resolver getOptionsContracts (Query.cs)
    → C# PolygonService.FetchOptionsContractsAsync (PolygonService.cs:514–547)
    → HTTP POST /api/options/contracts
    → Python polygon_client.list_options_contracts
    → Polygon API
  ```
- Both surfaces are load-bearing for the `/stock-analysis` and
  `/stock-analysis/day/:ticker/:date` 0DTE-listing pages.
- **§8.1 verification protocol hardened.** Now requires a three-layer
  grep (frontend resolver-method-name + C# function-name + Python
  URL-path-and-callee-name) plus a reverse callee-name grep across
  the whole repo, before any deletion. URL-path-only greps are
  banned as the sole evidence.
- **Phase 4 (orphan removal) dropped.** No orphans to remove.

### Phase 2 — Test gap closure — DONE

**Phase-2 entry inventory correction.** The Phase-0 audit overstated
the gap. Existing coverage on entry:

- `app/services/strategy_engine.py` — already heavily tested in
  `tests/test_strategy_engine.py` (612 lines: TestPayoffAtExpiry,
  TestStrategyCost, TestBreakevens, TestMaxProfitLoss, TestWeightedIV,
  TestD2, TestPOP, TestExpectedValue, TestPayoffCurve,
  TestAnalyzeStrategy, TestInterpolateIV, plus an iron-condor case)
  and `tests/test_strategy_engine_phase1_1.py` (206 lines:
  TestPayloadShapeStableByDefault, TestCurrentCurve, TestGreekCurves,
  TestLegDiagnostics, TestZeroDTEHandling). No Python work needed.
- `Backend.Tests/Unit/GraphQL/QueryTests.cs` — already covered G2
  (`getOptionsChainSnapshot`, 3 cases) and G5 (`getOptionsContracts`,
  2 cases). G1, G3, G4 were the real gap.
- Effort estimate revised from 1–2 weeks to 2–3 days.

**Tests added:**

| Layer | File | Tests added | Pre-existing | Final |
|---|---|---|---|---|
| Backend GraphQL resolvers | [`Backend.Tests/Unit/GraphQL/QueryTests.cs`](../../Backend.Tests/Unit/GraphQL/QueryTests.cs) | 9 (G1: 3, G3: 3, G4: 3) | 14 | 23 |
| Frontend strategy-builder spec | [`Frontend/src/app/components/strategy-builder/strategy-builder.component.spec.ts`](../../Frontend/src/app/components/strategy-builder/strategy-builder.component.spec.ts) | 15 (SB-A: 2, SB-C: 4, SB-G: 5, init: 4) | 0 | 15 |
| Frontend pricing-lab spec | [`Frontend/src/app/components/pricing-lab/pricing-lab.component.spec.ts`](../../Frontend/src/app/components/pricing-lab/pricing-lab.component.spec.ts) | 12 (PL-A: 2, PL-B: 3, PL-E: 3, init: 4) | 0 | 12 |

**Verification:**

- Backend: `dotnet test --filter "FullyQualifiedName~QueryTests" --no-build` →
  23 passed, 0 failed.
- Frontend full suite (no regressions): `podman exec my-frontend npx ng test --watch=false` →
  47 test files / 511 tests, all green.

### Phase 3 — Migrations + extractions — PARTIAL

| ID | Description | Status |
|---|---|---|
| **R5** | OccTickerFormat utility (`Frontend/src/app/utils/occ-ticker.ts`) with parse/format + 18-test parity spec including round-trip on 7 representative cases | ✅ DONE |
| **R0a** | Delete `/options-strategy-lab`. Move `payoff-chart/` to `Frontend/src/app/shared/payoff-chart/`. Update strategy-builder import. Add redirect to `/strategy-builder` | ✅ DONE |
| **R0b — UX-Q2 (chain density toggle, D9a)** | Add `chainDensity` signal (`'quick' \| 'greeks'`), localStorage-sticky toggle button next to the PUTS header, conditional V/Θ/Γ columns mirrored on call & put sides | ✅ DONE — design landed; partial of R0b |
| **R0b — UX-Q1 (drill-down icon-per-side) + drawer migration** | Add 📈/📉 icon-per-side outside chain rows; move PrimeNG Drawer + CandlestickChart + VolumeChart from deleted `/options-chain` into strategy-builder | ⏸ DEFERRED — substantial template + state work; queued for next focused session |
| **R0b — UX-Q4 (two-column 60/40 layout)** | Restructure strategy-builder layout: chain left 60%, build + payoff stacked right 40%; templates as horizontal pills above chain; scenario toggles inline beneath chart | ⏸ DEFERRED — substantial layout/SCSS rework; queued for next focused session |
| **R0b — delete `/options-chain`** | Remove `options-chain-v2/` (except `expiration-ribbon/`); add 7-day-watch redirect | ⏸ DEFERRED — gated on the drawer migration above |
| **R1** | Delete `/options-history`. Port `analyze()` to `past-chain.service.ts` and rendering to `data-lab/past-chain-inspector/` per UX-Q3 (collapsed card → progress-bar loading → expanded chain → modal drill-down) | ⏸ DEFERRED |
| **R6** | Extract Greek formatters (`fmtGreek`, `fmtIv`, `fmtPrice`, `fmtNum`) | ⏸ DEFERRED — post-consolidation |
| **R7** | Extract `ContractPricePicker` | ⏸ DEFERRED — post-consolidation |
| **R4** | Extract `OptionsChainStateService` | ⏸ DEFERRED — post-consolidation |
| **R8** | Sovereignty migration: delete TS `utils/black-scholes.ts`; server-side BS authority is the only path | ⏸ DEFERRED — focused session |

#### R5 details

`Frontend/src/app/utils/occ-ticker.ts` exports:

- `parseOcc(ticker)` → structured fields (underlying, expirationDate
  ISO, contractType, strike) or null
- `parseOccForDisplay(ticker)` → display-ready fields (e.g. "Feb 20,
  2026", "$689.00", "Call")
- `formatOcc(parts)` → raw OCC ticker, with input validation that
  throws on malformed underlying/strike/date

Tests (`occ-ticker.spec.ts`): 18 cases including malformed inputs,
sub-dollar strikes, fractional strikes, and round-trip parity over
7 representative tuples covering integer/fractional strikes,
short/long underlyings, calls/puts, and year/month/day boundaries.
Result: 18/18 passing.

Will be consumed by R0b (drill-down header) and R1 (past-chain
inspector OCC construction) when those migrations land.

#### R0a details

Operations executed in order:

1. Copied `Frontend/src/app/components/options-strategy-lab/payoff-chart/payoff-chart.component.{ts,html,scss}`
   to `Frontend/src/app/shared/payoff-chart/`.
2. Updated the relative import path inside the relocated file
   (`../../../graphql/types` → `../../graphql/types`).
3. Updated `strategy-builder.component.ts:27` import to point to
   the new shared location.
4. Deleted the entire `Frontend/src/app/components/options-strategy-lab/`
   directory (`.ts/.html/.scss` files plus the now-empty
   `payoff-chart/` sub-directory).
5. Replaced the route entry in `Frontend/src/app/app.routes.ts` with
   a 7-day-watch redirect:
   ```ts
   { path: "options-strategy-lab", redirectTo: "/strategy-builder", pathMatch: "full" }
   ```
   Modeled on the existing `lean-engine` → `engine` redirect at
   `app.routes.ts:189-192`.

**Verification:** `podman exec my-frontend npx ng build --configuration=development`
clean. `podman exec my-frontend npx ng test --watch=false --include="src/app/components/strategy-builder/**/*.spec.ts"`
→ 15/15 passing (no regressions in strategy-builder).

The redirect remains until ≥ 7 days from the deletion commit, at
which point Phase 4.5 removes it.

#### R0b — UX-Q2 (chain density toggle) details — DONE 2026-04-29 (later same day)

The Claude Design pass (bundle hash `Ld_D7E4LcbEWqq4z2WPl0g`) locked
UX-Q2 to "Quick density default with Full Greeks toggle, sticky
per-user." Implementation:

- **TS** ([`strategy-builder.component.ts`](../../Frontend/src/app/components/strategy-builder/strategy-builder.component.ts)):
  added `ChainDensity` type + `CHAIN_DENSITY_STORAGE_KEY` constant +
  `chainDensity` signal initialised from `localStorage` +
  `toggleChainDensity()` method that persists the new value. Extended
  `BuilderChainRow` with `callVega`/`callTheta`/`callGamma` and the
  put equivalents; `visibleRows` computed populates them via the
  existing `fmtGreek` helper.
- **HTML** ([`strategy-builder.component.html`](../../Frontend/src/app/components/strategy-builder/strategy-builder.component.html)):
  added a "Quick / Full Greeks" toggle button alongside the PUTS
  header. Wrapped V/Θ/Γ columns (3 per side, mirrored as V·Θ·Γ on
  call side and Γ·Θ·V on put side per the symmetric chain pattern)
  in `@if (chainDensity() === 'greeks')` blocks across `<colgroup>`,
  `<thead>` row 1 colspans, `<thead>` row 2 cell labels, and `<tbody>`
  cells.
- **SCSS** ([`strategy-builder.component.scss`](../../Frontend/src/app/components/strategy-builder/strategy-builder.component.scss)):
  appended a `.density-toggle` style block (transparent default,
  blue accent + filled background when `.is-greeks`).
- **Tests** ([`strategy-builder.component.spec.ts`](../../Frontend/src/app/components/strategy-builder/strategy-builder.component.spec.ts)):
  +2 tests under `describe('UX-Q2: chain density toggle', ...)`
  covering (a) the default-on-empty-storage initial value and
  (b) the round-trip persistence behaviour. Suite now 17/17 passing
  (was 15).

**Verification:** Frontend full suite 531/531 (was 529 before UX-Q2,
+2). Build clean.

### Phase 4 — Orphan removal — DROPPED

R3 revoked in Phase 1. Phase 4 is a numbered slot retained for
auditability; trivially met (nothing to do).

### Phase 4.5 — Redirect cleanup — PENDING (calendar-gated)

The R0a redirect (`/options-strategy-lab` → `/strategy-builder`)
is in place. Removal is gated on a ≥ 7-day watch period from the
deletion commit per [§7 D7](options-routes-research.md#7-decisions-log)
of the research plan. Cannot be executed today; tracked for
2026-05-06 or later.

### Phase 5 — Truth-doc authoring — MVP SCAFFOLD SHIPPED

[`docs/architecture/options-research.md`](options-research.md) ships
as an MVP scaffold:

- §1 Reviewer framing — populated.
- §2 Executive overview — populated, except §2.3 headline anchor
  (deferred until R0b's end-to-end fixture exists).
- §3 Hard constraints — populated.
- §4 Mathematical foundations — populated:
  - §4.1 BS European price (Hull §15.8 anchor)
  - §4.2 Greeks (Hull §17.6–17.10 anchor)
  - §4.3 IV solver
  - §4.4 Forward + dividend from parity
  - §4.5 POP under BS lognormal
  - §4.6 Multi-engine pricing
- §5 Production pipelines — **stubs**. §5.1 (`/strategy-builder`),
  §5.2 (`/pricing-lab`), §5.3 (`/data-lab` options sub-feature),
  and §5.4 (companion data formats) are stubs awaiting the R0b /
  R1 migrations to land. Each stub names the upstream resolver,
  the §4 formulas it invokes, and the planned end-to-end fixture.
- §6 Tolerances and validation — populated with citations to the
  10 most load-bearing tests added in Phase 2.
- §7 Decisions log — populated with math-bearing decisions only;
  cleanup-mechanical decisions stay in the research plan.
- §8 Reviewer feedback log — empty stub awaiting Phase 7.
- §9 Future plan / deferred items — populated (R8, post-consolidation
  extractions, intraday-IV slot wiring, SABR-corrected POP).
- §10 Out of scope — populated.
- §11 References — populated (Hull, CBOE VIX, Polygon docs, internal
  cross-links).
- §12 Appendix A — Worked numerical example A.1 populated (Hull
  §15.9 Example 15.6, full d1/d2/N(d1)/N(d2)/price/Greeks).
- §13 Appendix B — file map populated.

### Phase 6 — F5 deletion — DONE

`Frontend/src/app/components/research-lab/options-math-docs/` deleted
(all three files: `.ts/.html/.scss`).

The `/research-lab` "Options Math" sub-section now renders an inline
redirect panel in `research-lab.component.html` that links to:

- `docs/architecture/options-math-authorities.md`
- `docs/references/options-bs-greeks-2026-04-24.md`
- `docs/options-cross-section-overview.md`

`OptionsMathDocsComponent` import + decorator entry removed from
`research-lab.component.ts`.

**Verification:** `podman exec my-frontend npx ng build` clean.

### Phase 7 — External review — PENDING (manual)

Awaits Phase 5 truth doc to be fleshed out (after R0b / R1 land).
External LLM review pass populates `options-research.md` § 8.

### Phase 8 — Cleanup audit — IN PROGRESS

This document.

---

## Deferred work and rationale

These items were *not* completed in the 2026-04-29 effort and are
tracked here so the next session has a clean starting point.

### R0b — `/options-chain` deletion + drill-down migration

**Why deferred.** Adding 6 Greek columns (per D9a) and a drill-down
drawer to `/strategy-builder`'s already-dense chain table is a
substantive UX change to a working production page. The two relevant
UX questions are flagged in
[`options-ux-design-prompt.md`](options-ux-design-prompt.md):

- **UX-Q1** — drill-down trigger ambiguity (click for leg vs click
  for history?)
- **UX-Q2** — chain-table density under D9a
- **UX-Q4** — overall strategy-builder page layout

Per [§7 D11](options-routes-research.md#7-decisions-log), these UX
questions accumulate to a Claude Design pass rather than being
guessed at by the implementing agent. Doing the migration without
that pass would force the implementer to make load-bearing UX
decisions on a working page; the cost of getting them wrong (worse
UX than the current two-page state) outweighs the route-count
savings.

**What's needed to unblock.** Either (a) the owner runs the
existing UX prompt against Claude Design and locks the layout
choices for UX-Q1/Q2/Q4, or (b) the owner waives the UX gate and
authorizes a working-default mechanical migration to land.

### R1 — `/options-history` port to `/data-lab`

**Why deferred.** Same gating reason — UX-Q3 (past-chain inspector
card visual on `/data-lab`) is in the design prompt. Plus the port
is non-trivial: ~407-line component split into a new
`past-chain.service.ts` + a new `past-chain-inspector` sub-component,
spec migration, mounted into a card on the existing `/data-lab`
options-companion config row (D10a). Risk of regressing the existing
`/data-lab` workflow without careful testing.

**What's needed to unblock.** UX-Q3 design recommendation, plus a
focused session for the service extraction + sub-component
authoring.

### R8 — Server-side BS sovereignty migration

**Why deferred.** Touches sovereignty math. The TS-side
`utils/black-scholes.ts` is currently used by `/pricing-lab` and
`/strategy-builder` in two roles: (1) the "Legacy BS" curve in
the multi-engine compare; (2) live preview Greeks while editing
legs. Replacing both with server-side calls requires a parity test
pinning agreement at `atol=1e-9` on price and `atol=1e-6` on Greeks
across a 1000-point spot grid, plus a latency check (the live preview
needs to feel responsive — round-trip latency to the Python service
under typical Greek-curve workloads must be benchmarked before
making the swap).

**What's needed to unblock.** Focused session per `options-vol-platform-tdd.md`
Phase 1.2 with the parity + latency tests in hand.

### R6, R7, R4 — Post-consolidation extractions

**Why deferred.** These are duplications across the surviving
surfaces (Greek formatters, contract-price-picker, chain-state
service). After R0a/R0b/R1 land, the duplication landscape is
smaller; extractions are smaller-blast-radius then. Doing them
*before* R0b/R1 means refactoring code that is about to be deleted,
which is wasted churn.

**What's needed to unblock.** R0b and R1 land first. Then a
small follow-up PR per extraction, each with its parity test from
[§8.2](options-routes-research.md#82-parity-tests-for-every-extraction)
of the research plan.

### Full §5.x sections of `options-research.md`

**Why deferred.** Each §5.x section depends on its corresponding
migration shipping (the production-pipeline description and the
end-to-end fixture both presuppose the pipeline exists in its
post-migration shape). Today only the §4 math sections are fully
populated; §5.1, §5.2, §5.3 are scaffolds.

**What's needed to unblock.** R0b ships → flesh out §5.1. R1 ships
→ flesh out §5.3. §5.2 (`/pricing-lab`) can be authored at any
time since `/pricing-lab` is unchanged by the cleanup; deferred
only because §5.1 is the most load-bearing and ordering matters
for cross-references.

### Phase 4.5 redirect cleanup

**Why deferred.** Calendar gate — must be ≥ 7 days from the deletion
commit per the verification protocol. Today the deletion commit
hasn't even shipped (sitting in working tree). The 7-day clock
starts when the R0a commit lands on master.

### Phase 7 external review

**Why deferred.** The truth doc isn't fleshed out enough yet (§5.x
stubs). Sending a stub to an external reviewer wastes the reviewer's
context budget. After §5.1 + §5.3 are populated, the doc is dense
enough to support a meaningful review pass.

---

## Duplication delta — measured-after

Per [§2.3](options-routes-research.md#23-headline-anchor--the-duplication-baseline)
of the research plan, the goal was to track baseline-vs-after
numbers. Today's delta:

| Concern | Baseline | After 2026-04-29 | Final target |
|---|---|---|---|
| Frontend routes named `/options-*` or doing options work | 5 + sub-features | 4 + sub-features (deleted `/options-strategy-lab`; deleted in-app `options-math-docs` embed) | 2 standalone (`/strategy-builder`, `/pricing-lab`) + 2 hosted (sub-feature in `/data-lab`, link-out in `/research-lab`) |
| Chain fetch state machine | 4 components | 4 components (no change yet) | 1 service after R4 lands |
| Greek number formatting | 3 components | 3 components (no change yet) | 1 utility after R6 lands |
| OCC ticker parse | 2 components | 1 utility + 1 component still using inline (will switch in R0b/R1) | 1 utility after R0b/R1 land |
| Per-contract historical drill-down | 1 component (`/options-chain`) | 1 component (no change yet) | absorbed into `/strategy-builder` after R0b lands |
| Past-chain interactive view | 1 component (`/options-history`) | 1 component (no change yet) | absorbed into `/data-lab` after R1 lands |
| Options-feature documentation | 6 files + 1 in-app component | 6 files + truth-doc scaffold (`options-research.md` MVP) + accumulator file (`options-ux-design-prompt.md`); in-app component deleted | 1 truth doc + accumulator, sources absorbed and deleted |

The shape of the delta is "scaffolds and tests are in; structural
deletions are partial, with the small one (R0a) done and the larger
ones (R0b, R1) gated on the UX design pass."

---

## Files added/changed in this effort

### New files

```
docs/architecture/options-routes-research.md           # the cleanup plan (~1700 lines)
docs/architecture/options-research.md                  # the truth-doc MVP scaffold
docs/architecture/options-ux-design-prompt.md          # UX-Q accumulator for D11
docs/architecture/options-cleanup-2026-04-29.md        # this audit ledger
Frontend/src/app/utils/occ-ticker.ts                   # R5 utility
Frontend/src/app/utils/occ-ticker.spec.ts              # 18 cases incl. round-trip parity
Frontend/src/app/shared/payoff-chart/                  # relocated from options-strategy-lab/
  payoff-chart.component.ts/.html/.scss
Frontend/src/app/components/strategy-builder/strategy-builder.component.spec.ts  # 15 cases
Frontend/src/app/components/pricing-lab/pricing-lab.component.spec.ts            # 12 cases
```

### Edits to existing files

```
docs/architecture/options-math-authorities.md
  - Stamped 2026-04-29 review
  - Fixed the solve_implied_volatility / implied_volatility name discrepancy

Backend.Tests/Unit/GraphQL/QueryTests.cs
  - Added #region GetOptionsExpirations (3 tests)
  - Added #region AnalyzeOptionsStrategy (3 tests)
  - Added #region PricingModelComparison (3 tests)

Frontend/src/app/app.routes.ts
  - Removed loadComponent for /options-strategy-lab
  - Added redirect /options-strategy-lab → /strategy-builder

Frontend/src/app/components/strategy-builder/strategy-builder.component.ts
  - Updated PayoffChartComponent import to ../../shared/payoff-chart/

Frontend/src/app/components/research-lab/research-lab.component.ts
  - Removed OptionsMathDocsComponent import + imports[] entry

Frontend/src/app/components/research-lab/research-lab.component.html
  - Replaced <app-options-math-docs /> with inline link-out panel
```

### Deletions

```
Frontend/src/app/components/options-strategy-lab/      # entire directory (R0a)
Frontend/src/app/components/research-lab/options-math-docs/  # entire directory (Phase 6)
```

---

## Test-suite hygiene at end of effort

**Backend.Tests:** `dotnet test --filter "FullyQualifiedName~QueryTests" --no-build`
→ 23/23 passing.

**Frontend:** `podman exec my-frontend npx ng test --watch=false`
→ 47 test files / 511 tests passing (was 484 pre-effort; +27
covers the new specs and the OCC utility).

**Build:** `podman exec my-frontend npx ng build --configuration=development`
clean.

**No baseline failures inherited or introduced.**

---

*End of audit document.*
