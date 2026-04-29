# Options-Routes Research & Cleanup Plan

> **Single source of truth** for the options-page consolidation, route hardening,
> and implementation-truth documentation effort. Complementary to but distinct
> from `options-chain-research-plan.md` at the repo root, which is the (now
> implemented) TradingView-style UI build plan for the chain viewer itself.
> This document is a *cleanup* and *audit* plan — what we keep, what we merge,
> what we delete, and how we prove the survivor set is correct.
>
> **Modeled on:** `docs/architecture/iv-ownership-research.md` — same rigor
> bar (math citations, tolerances, audit trail, deferred items, reviewer
> framing).
>
> **Status:** Research plan, alignment pass complete (2026-04-29). All
> ratified decisions in [§7](#7-decisions-log): D1 (delete in-app math
> doc, markdown only), D2 (full backfill of tests), D3 (single doc),
> D4 (all authorisations granted), D5 (analyzeOptionsStrategy is a
> query), D6 (textbook anchors for math, market dates for E2E),
> D7 (same-PR delete for refactors), D8 (`/strategy-builder` survives,
> `/options-strategy-lab` deleted), D9 (`/options-chain` deleted after
> drill-down migrates into `/strategy-builder`), D10
> (`/options-history` deleted after past-chain logic ports into
> `/data-lab`), D11 (UX-design questions accumulate into a
> deliverable prompt for Claude Design rather than blocking
> implementation). Phase 0 done; Phase 1 may begin.
>
> **Last revised:** 2026-04-29.

---

## Table of contents

1. [Reviewer framing](#1-reviewer-framing)
2. [Executive overview](#2-executive-overview)
3. [Hard constraints](#3-hard-constraints)
4. [Current surface-area inventory](#4-current-surface-area-inventory)
5. [Consolidation proposal](#5-consolidation-proposal)
6. [Documentation deliverables and rigor template](#6-documentation-deliverables-and-rigor-template)
7. [Decisions log](#7-decisions-log)
8. [Validation & proof plan](#8-validation--proof-plan)
9. [Phased execution plan](#9-phased-execution-plan)
10. [Out of scope](#10-out-of-scope)
11. [Risks, anti-patterns, and rollback](#11-risks-anti-patterns-and-rollback)
12. [References](#12-references)
13. [Appendix A — File map of every options touchpoint](#13-appendix-a--file-map-of-every-options-touchpoint)
14. [Appendix B — Documentation skeleton for each surviving page](#14-appendix-b--documentation-skeleton-for-each-surviving-page)

---

## 1. Reviewer framing

This document is the design brief for the options-page cleanup work. The
audience is:

1. The owner (Tim) — to authorise the consolidation moves and the
   documentation deliverables before any code is touched.
2. Future-self / contributors — so the *why* of every removal is durable.
3. External reviewers — to give them a single artefact that explains the math,
   the data flow, and the trade-offs of the survivor set, without chasing
   links across the repo.

**Style preferences for any reviewer response:**

- **Specificity > breadth.** Cite a specific row in [§4](#4-current-surface-area-inventory)
  or [§5](#5-consolidation-proposal) when challenging a recommendation.
- **Quote the line.** If you disagree with a deletion, quote the exact route
  or component path being deleted.
- **Don't pull punches.** This is a cleanup; saying "your hypothesis that X is
  unused is wrong because Y" is more useful than "have you considered…"

**This is not a rebuild plan.** The TradingView-style chain UI (described in
`options-chain-research-plan.md` at the repo root) is already built and
shipped. This document does not propose redesigning that page. It proposes
that we (a) catalogue what we have, (b) consolidate where math or data flow
is duplicated, and (c) produce documentation that proves the survivor set is
mathematically and operationally correct.

---

## 2. Executive overview

### 2.1 The thesis

**Most options pages are variations on the same workflow:** fetch the chain,
do something with it. The "something" is what differs — render it
(`/options-chain`), price one contract many ways (`/pricing-lab`), build a
multi-leg payoff (`/options-strategy-lab` or `/strategy-builder`), or
replay a past day's chain (`/options-history`). Every page repeats the
*same* prelude — pick a ticker, list expirations, fetch the snapshot —
and then diverges.

Today this prelude is duplicated five times across five components and
the canonical math is documented in six separate places that disagree on
formatting, tolerance language, and which file is authoritative.

*(The first revision of this plan also claimed there was at least one
orphan REST endpoint. Phase 1 verification showed the original orphan
hypothesis was wrong — see [§4.2.1](#421-phase-1-correction--p1-and-g5-are-not-orphan).
Lesson: URL-path-only greps miss the indirect frontend → GraphQL → C# →
Python proxy chain.)*

**Per the ratified decisions in [§7](#7-decisions-log)**, four of those
five frontend routes go away. The cleanup is more aggressive than the
original draft of this plan envisioned:

- `/options-strategy-lab` deleted (D8) — `/strategy-builder` is the
  surviving multi-leg builder.
- `/options-chain` deleted (D9) — chain view + per-contract drill-down
  migrate into `/strategy-builder`.
- `/options-history` deleted (D10) — past-chain logic ports into
  `/data-lab`.
- in-app `options-math-docs` embed deleted (D1) — markdown-only docs.

Survivors: **`/strategy-builder`, `/pricing-lab`, `/data-lab` (extended
with the past-chain inspector), `/research-lab` (with the math-docs
sub-section as a link-out)**.

### 2.2 What "done" looks like

When this plan is executed:

- **Each surviving page has a single owner doc** in `docs/architecture/`
  written to the rigor of `iv-ownership-research.md` — math, tolerances,
  authority hierarchy, reviewer feedback log, audit trail.
- **Shared chain-prelude logic** is extracted into one Angular service, one
  Python authority, and one documented contract.
- **Orphan endpoints are removed** with a deletion commit that names every
  caller that was searched and not found. (Sovereignty over the math
  *includes* sovereignty over the route surface — we don't ship code we
  cannot account for.)
- **Every option-Greek answer in the UI is traceable** in one hop to the
  Python authority that produced it, via a citation block in the rendering
  component.

### 2.3 Headline anchor — the duplication baseline

| Concern | Locations today | After consolidation |
|---|---|---|
| Frontend routes named `/options-*` or doing options work | 5 (`/options-chain`, `/options-strategy-lab`, `/strategy-builder`, `/options-history`, `/pricing-lab`) + sub-feature in `/data-lab` + sub-section in `/research-lab` | 2 standalone (`/strategy-builder`, `/pricing-lab`) + 2 hosted (sub-feature in `/data-lab`, link-out in `/research-lab`) |
| Chain fetch state machine (expirations → snapshot → loading/error) | 4 components | 1 service (R4) |
| Greek number formatting (`fmtGreek`, `fmtIv`, `fmtPrice`) | 3 components | 1 utility (R6) |
| OCC ticker parse (`O:SPY260220C00689000` → fields) | 2 components | 1 utility (R5) |
| Premium/IV resolution from snapshot (`day.close → lastTrade → quote mid → bid/ask mid`) | 2 components | 1 utility (R7) |
| Per-contract historical drill-down (Drawer + CandlestickChartComponent + VolumeChartComponent) | 1 component (`/options-chain`) | absorbed into `/strategy-builder` per D9 |
| Past-chain interactive view | 1 component (`/options-history`) | absorbed into `/data-lab` per D10 (lifted into `past-chain.service.ts` + `past-chain-inspector` sub-component) |
| Options-feature documentation | 6 files + 1 in-app component (see [§4.4](#44-existing-options-related-docs)) | 1 truth doc, sources absorbed and deleted |

Numbers are *expected reductions*, not measurements; this plan deliberately
does not auto-execute the consolidation, so the actual delta is recorded in
[§9](#9-phased-execution-plan) per phase.

---

## 3. Hard constraints

These shape every consolidation decision. If a recommendation in this
document violates one, that should be flagged explicitly.

| Constraint | What it rules out |
|---|---|
| **Single source of truth per concept** (CLAUDE.md guiding philosophy #5) | Two different `fmtGreek` implementations even if both are tested. Duplicates allowed only with a parity test naming the canonical file. |
| **Sovereignty over the math** (philosophy #3) | Frontend cannot recompute Greeks or IV "as a fallback" if the Python authority disagrees. All Greek/IV numbers in the UI come from the server. (This is also Phase 1.2 of `options-vol-platform-tdd.md`.) |
| **`int64 ms UTC` at all wire/storage boundaries** (numerical-rigor.md → Timestamp rigor) | Any new endpoint must accept and return timestamps as `int64 ms`. ISO strings or `DateTime` are disallowed. |
| **No silent forward-fill or synthetic alignment** | When the chain has gaps (missing strikes, missing IVs), they are surfaced — not patched. |
| **No new dependencies without justification** (CLAUDE.md hard rules) | The shared Angular service uses what's already in the project (Apollo, signals, RxJS); no new state-management libraries. |
| **Every port has a golden fixture and tolerance** (numerical-rigor.md) | If a calculation moves from a frontend `utils/` file to a backend Python authority, the parity test compares old and new outputs at `atol=1e-9, rtol=0` (Greeks: `atol=1e-6, rtol=1e-6` per the existing default) before the legacy code is deleted. |
| **No regressions to live `/options-chain` UX** | The user-visible behaviour of the TradingView-style chain viewer is locked. Refactors are internal-only unless explicitly authorised. |

---

## 4. Current surface-area inventory

This section is the *as-is* map. The proposal in [§5](#5-consolidation-proposal)
is built on top of it and references row numbers here.

### 4.1 Frontend routes that touch options

Source of truth: `Frontend/src/app/app.routes.ts` (read 2026-04-29).

| # | Route | Component path | LOC (.ts) | Has `.spec.ts`? | Status (per §5 + §7 D8 / D9 / D10) |
|---|---|---|---|---|---|
| F1 | `/options-chain` | `Frontend/src/app/components/options-chain-v2/options-chain.component.ts` | ~476 | **No** | **DELETE** per D9 — chain view + drill-down migrates into F2 |
| F2 | `/strategy-builder` | `Frontend/src/app/components/strategy-builder/strategy-builder.component.ts` | TBD | **No** | **Keep** (survivor per D8); absorbs F1's drill-down |
| F2-DEAD | `/options-strategy-lab` | `Frontend/src/app/components/options-strategy-lab/options-strategy-lab.component.ts` | ~827 | **No** | **DELETE** per D8 — duplicates F2's job |
| F3 | `/options-history` | `Frontend/src/app/components/options-history/options-history.component.ts` | ~407 | Yes | **DELETE** per D10 — past-chain logic ports into F6 (data-lab) |
| F4 | `/pricing-lab` | `Frontend/src/app/components/pricing-lab/pricing-lab.component.ts` | ~697 | **No** | Keep |
| F5 | `/research-lab` (sub-section) | `Frontend/src/app/components/research-lab/options-math-docs/options-math-docs.component.ts` | ~583 | **No** | **DELETE** per D1 — markdown only |
| F6 | `/data-lab` (options sub-feature) | `Frontend/src/app/components/data-lab/data-lab.component.ts` | (~1500, mostly non-options) | Partial | **Keep + extend** per D10 — gains an interactive past-chain inspector |

Spec-file existence verified by `Glob` on each component directory; F1, F2,
F4, F5 returned no matches. Test gap is recorded in
[§7 D2](#7-decisions-log).

**Per-route function summary** (one line each — fuller summary in the
exploration report archived in PR commentary):

- **F1 `/options-chain`** — Live chain table viewer. Calls
  `getOptionsExpirations`, `getOptionsChainSnapshot`, plus per-contract
  `getOrFetchStockAggregates` for the drill-down chart. Pure read; no
  computation beyond ATM-centred filtering and OCC-ticker parsing.
  **DELETED per D9.** Its unique value over F2 is the per-contract
  historical drill-down drawer (Drawer + CandlestickChartComponent +
  VolumeChartComponent at [options-chain.component.ts:20-22](../../Frontend/src/app/components/options-chain-v2/options-chain.component.ts#L20-L22)),
  which migrates into F2 before deletion.
- **F2 `/strategy-builder`** (survivor per [§7 D8](#7-decisions-log)) —
  Multi-leg options-strategy builder. Calls the same two snapshot queries
  as F1, then `analyzeOptionsStrategy` (server-side P&L + Greeks curve +
  POP). Imports `ExpirationRibbonComponent` from `options-chain-v2/` and
  `PayoffChartComponent` from `options-strategy-lab/` — the latter
  triggers a relocation step described in [§5.1](#51-routes--keep-merge-delete).
  Per [§7 D9](#7-decisions-log), also gains the per-contract historical
  drill-down absorbed from F1.
- **F2-DEAD `/options-strategy-lab`** — same job as F2, older
  implementation. Deleted per D8. Its `payoff-chart/` child component is
  relocated (not deleted) before the parent directory is removed.
- **F3 `/options-history`** — Past-date chain reconstruction. Does *not* call
  the snapshot endpoint (snapshots are live-only). Instead, fetches stock
  aggregates to find ATM, then batches `getOrFetchStockAggregates` against
  candidate OCC contract tickers (30-at-a-time loop) to discover which
  strikes traded. **DELETED per D10.** The
  [`analyze()` procedure at lines 117-289](../../Frontend/src/app/components/options-history/options-history.component.ts#L117-L289)
  is extracted into a service and the rendering UI into a sub-component
  hosted by F6 (data-lab) before the parent directory is removed.
- **F4 `/pricing-lab`** — Single-contract multi-model price comparison.
  Calls the snapshot to seed inputs, then `comparePricingModels` for the
  multi-engine sweep (analytic BS, binomial CRR/JR/LR, finite-diff,
  Monte Carlo).
- **F5 `/research-lab/options-math-docs`** — Static reference docs (no HTTP).
  Embeds formulas and a glossary. Substantially overlaps with the markdown
  docs in [§4.4](#44-existing-options-related-docs).
- **F6 `/data-lab`** (options sub-feature only — the rest of data-lab is
  out of scope). Today: `optionsCompanionEnabled` plus 4 config knobs
  (`optionsStrikesEachSide`, `optionsIncludeCalls`, `optionsIncludePuts`,
  `optionsDteDistance`) at [data-lab.component.ts:514-521](../../Frontend/src/app/components/data-lab/data-lab.component.ts#L514-L521)
  attach an `options_companion` block to a dataset run request. The
  output is a backtest data file, *not* an interactive view. Per D10
  this page extends to host an interactive past-chain inspector ported
  from F3.

### 4.2 Python (FastAPI) routes that serve options data

Source of truth: `PythonDataService/app/routers/options.py` (read in full,
100 lines), `PythonDataService/app/routers/quantlib_options.py` (header
inspected).

| # | Method + path | What it returns | Frontend caller |
|---|---|---|---|
| P1 | `POST /options/contracts` | `OptionsContractsResponse` — list of contracts for a ticker | **In use** — proxied via `PolygonService.FetchOptionsContractsAsync` at [Backend/Services/Implementation/PolygonService.cs:545-547](../../Backend/Services/Implementation/PolygonService.cs#L545-L547), reached by frontend at `stock-analysis.component.ts:308` and `day-detail.component.ts:118`. *(Originally suspected orphan in v1 of this plan — corrected in Phase 1, see [§4.2.1](#421-phase-1-correction--p1-and-g5-are-not-orphan).)* |
| P2 | `POST /options/expirations` | `OptionsExpirationsResponse` — unique expiration dates | Indirect — via Backend GraphQL `getOptionsExpirations` |
| P3 | `POST /quantlib/price` (and siblings under `quantlib_options.py`) | Single-contract multi-engine pricing | Indirect — via Backend GraphQL `comparePricingModels` |

#### 4.2.1 Phase 1 correction — P1 and G5 are NOT orphan

The first revision of this plan (2026-04-29 morning) listed P1 and G5 as
suspected orphans on the basis of a frontend grep for the URL path
`/options/contracts` returning zero matches. That verification was
**insufficient**: the URL path is invoked by the Backend C# layer, not
by the frontend directly, so the URL-path grep cannot detect the
indirect chain.

The full call chain, verified in Phase 1 on 2026-04-29:

```
Frontend (stock-analysis, day-detail)
  → marketDataService.getOptionsContracts(...)        # GraphQL client
  → Backend GraphQL resolver getOptionsContracts (G5)  # Backend/GraphQL/Query.cs
  → C# PolygonService.FetchOptionsContractsAsync       # Backend/Services/Implementation/PolygonService.cs:514-547
  → HTTP POST /api/options/contracts                   # P1 (Python REST)
  → Python polygon_client.list_options_contracts       # PythonDataService/app/services/polygon_client.py
  → Polygon API
```

Both P1 and G5 are *load-bearing*. Neither is removed by this cleanup.
The `/stock-analysis/day/:ticker/:date` route (route line 48-53 of
`app.routes.ts`) and `/stock-analysis` (route line 34-38) are 0DTE
contract-listing pages that depend on this chain.

**Implication for [§5 R3](#51-routes--keep-merge-delete):** the
recommendation is revoked. See updated R3 in §5.1.

**Implication for [§8.1](#81-pre-deletion-verification-protocol):** the
verification protocol is hardened — see updates in §8.1.

### 4.3 Backend GraphQL resolvers that touch options

Source of truth: `Backend/GraphQL/Query.cs` and
`Backend/Services/Implementation/PolygonService.cs` (grepped, not read in
full — see [§7 question 4](#7-decisions-log--open-questions)).

| # | Resolver | Frontend calling components | Status |
|---|---|---|---|
| G1 | `getOptionsExpirations` | F1, F2, F3 (indirect), F4 | **Used** (all routes depend) |
| G2 | `getOptionsChainSnapshot` | F1, F2, F4 | **Used** (core dependency) |
| G3 | `analyzeOptionsStrategy` | F2 only | **Used** (strategy lab) |
| G4 | `comparePricingModels` | F4 only | **Used** (pricing lab) |
| G5 | `getOptionsContracts` | `stock-analysis.component.ts:308`, `day-detail.component.ts:118` | **In use** — 0DTE contract listing for stock-analysis pages. *(Originally suspected unused — corrected in Phase 1, see [§4.2.1](#421-phase-1-correction--p1-and-g5-are-not-orphan).)* |

The original suspicion that G5 was unused came from a grep that
restricted itself to the options-named components (F1–F5). Phase 1
re-ran the grep over `Frontend/src/app/components/` without that
filter and found the two stock-analysis callers.

### 4.4 Existing options-related docs

Source of truth: `Glob "docs/**/*options*.md"` plus the agent's audit.

| File | Type | Status today | Recommended fate |
|---|---|---|---|
| `docs/architecture/options-math-authorities.md` | Authority index | TRUTH | **Promote**: this is the canonical authority map. Other docs link to it. |
| `docs/architecture/options-vol-platform-tdd.md` | 8-phase migration roadmap | ROADMAP | **Keep**: tracks the Phase 1.2 server-authority migration. Mark which phases are complete on each revision. |
| `docs/options-cross-section-overview.md` | Math overview | REFERENCE | **Fold** the unique math content into `options-math-authorities.md`; convert this file to a redirect stub or delete. |
| `docs/options-companion-format.md` | Data-format spec for the 30-day IV companion series | REFERENCE | **Keep** — it is a wire-format spec, not math. Cross-link from the IV doc and from the new pricing-lab doc. |
| `docs/references/options-bs-greeks-2026-04-24.md` | BS formula reference card | REFERENCE | **Keep** as the formula card under `references/`. Cross-link from each per-route doc that needs the formulae. |
| `docs/phase-1-2-deep-dive.md` | Design for server-side BS authority | PLAN | **Fold** into `options-vol-platform-tdd.md` once Phase 1.2 stabilises; today it is a working doc. |
| `Frontend/.../options-math-docs.component.ts` | In-app embedded math reference | UI ARTEFACT | **Decision pending** — see [§7 question 1](#7-decisions-log--open-questions). |

### 4.5 Tests for the options stack

Source of truth: `Glob "PythonDataService/tests/**/options*"`,
`Glob "Frontend/src/app/components/**/options*spec*"`,
`Glob "Frontend/src/app/components/pricing-lab/*spec*"`.

| Layer | Test file | Coverage |
|---|---|---|
| Python research | `tests/research/options/test_contract_finder.py` | ATM strike, delta-based selection, liquidity filters |
| Python research | `tests/research/options/test_iv_builder.py` | IV solving, variance-time interpolation, quality flags |
| Python research | `tests/research/options/test_diagnostics.py` | Data-quality checks |
| Python research | `tests/research/options/test_options_features.py` | IV rank, log skew, VRP |
| Python services | `tests/services/test_options_companion_service.py` | 30-day IV pipeline E2E |
| Python research | `tests/research/test_options_runner.py` | IC analysis, quantile validation |
| Frontend | `options-history/options-history.component.spec.ts` | Initialisation + bindings |

**Gaps (no tests found):**

- `app/services/strategy_engine.py` — `analyze_strategy()` (POP, EV, Greeks
  curve)
- `app/engine/options/chain_resolver.py` — backtest chain-resolution modes
- `app/engine/options/pricer.py` — QuantLib adapter
- Backend GraphQL resolver tests for `getOptionsChainSnapshot`,
  `analyzeOptionsStrategy`, `comparePricingModels`
- Frontend specs for F1, F2, F4, F5

This gap is the second-largest piece of work in this plan. Closing it is a
hard precondition for documenting the routes "with the rigor of
iv-ownership-research" — the rigor doc *quotes* the test that proves the
math, so the tests have to exist before the doc can cite them.

---

## 5. Consolidation proposal

Each recommendation has an ID (R1, R2, …) and is referenced by the phased
plan in [§9](#9-phased-execution-plan).

### 5.1 Routes — keep, merge, delete

**KEEP as standalone routes:**

- **F2 `/strategy-builder`** — multi-leg payoff builder. Survivor of D8;
  absorbs F1's per-contract drill-down per D9.
- **F4 `/pricing-lab`** — multi-model pricing comparison. Distinct workflow
  (single contract, multi-engine sweep).
- **F6 `/data-lab`** — keeps its existing scope; the options sub-feature
  extends to host the past-chain inspector ported from F3 per D10.

**DELETE the four remaining options-named routes** (`/options-chain`,
`/options-strategy-lab`, `/options-history`, in-app `/research-lab/options-math-docs`).
Each deletion has a migration step that runs first. The recommendations
below are the migrations + the deletions, in the order they ship.

---

**Recommendation R0a — delete `/options-strategy-lab`; relocate
`payoff-chart/` first.** Per [§7 D8](#7-decisions-log),
`/strategy-builder` is the surviving multi-leg builder. The deletion has a
non-trivial dependency: `strategy-builder.component.ts:27` imports
`PayoffChartComponent` from `options-strategy-lab/payoff-chart/`. Order of
operations:

1. Move `Frontend/src/app/components/options-strategy-lab/payoff-chart/` to
   a shared location — proposed: `Frontend/src/app/shared/payoff-chart/`.
   (One PR; rename imports in strategy-builder; verify build + manual smoke
   on `/strategy-builder`.)
2. Delete the rest of `Frontend/src/app/components/options-strategy-lab/`
   (the `options-strategy-lab.component.ts/.html/.scss`).
3. Remove the `/options-strategy-lab` route entry from `app.routes.ts`.
4. Add a redirect in `app.routes.ts`:
   `{ path: "options-strategy-lab", redirectTo: "/strategy-builder", pathMatch: "full" }`
   following the existing `lean-engine` → `engine` redirect precedent at
   `app.routes.ts:189-192`.
5. Watch period of 7 days, then remove the redirect in a follow-up commit
   if no inbound links break.

---

**Recommendation R0b — delete `/options-chain`; migrate per-contract
historical drill-down into `/strategy-builder` first.** Per [§7 D9](#7-decisions-log),
strategy-builder absorbs the chain-viewer role. F1's unique-vs-F2 features
are: (1) the click-strike-to-see-history drawer (PrimeNG `Drawer` +
`CandlestickChartComponent` + `VolumeChartComponent` at
`options-chain.component.ts:20-22`); and (2) the full Greek display per
chain row (vega/theta/gamma alongside delta — strategy-builder shows
mainly delta per row). Order of operations:

1. Add the drill-down drawer to `strategy-builder` — import the same three
   components, add a `selectedContractForHistory` signal, wire a click
   handler on each chain-row strike cell. Reuse the existing
   `getOrFetchStockAggregates(occTicker, fromDate, toDate, 'day')` call
   pattern from `options-chain.component.ts`.
2. Add the full-Greek-per-row display to `strategy-builder`'s chain table
   (or document explicitly in the truth doc that this feature was dropped
   if owner agrees). **Owner decision deferred** — track in §7 as
   sub-decision under D9.
3. Use R5 `OccTickerFormat` (the parser) for the contract-metadata header
   in the drawer — extract before this migration if R5 hasn't shipped yet.
4. Manual smoke pass: open `/strategy-builder`, click each cell type
   (call/put/ATM/ITM/OTM), verify the historical drawer renders.
5. Delete `Frontend/src/app/components/options-chain-v2/` *except*
   `expiration-ribbon/` (already used by `/strategy-builder`).
6. Remove the `/options-chain` route entry; add redirect to
   `/strategy-builder`.
7. Watch period of 7 days; remove redirect in follow-up.

---

**Recommendation R1 — delete `/options-history`; port its analyze() logic
and rendering into `/data-lab`.** Per [§7 D10](#7-decisions-log) and the
"if it is well built" qualifier from the owner: F3 today is one ~407-line
component mixing data-fetching, computation, and rendering. The port
makes that mix explicit:

1. **Extract data-fetching into a service** — proposed
   `Frontend/src/app/services/past-chain.service.ts` exposing
   `fetchPastChain(ticker, date, numStrikes, atmMethod) → PastChainResult`.
   Body is the contents of `options-history.component.ts:117-289` lifted
   verbatim, modulo signal manipulation (the service returns a value;
   loading state stays at the component layer). Use R5 `OccTickerFormat`
   for the OCC ticker construction at lines 192-214 once R5 ships.
2. **Extract rendering into a sub-component** — proposed
   `Frontend/src/app/components/data-lab/past-chain-inspector/`. Inputs
   are the `PastChainResult`; the call/put split, ATM marker, change %,
   and per-contract drill-down (LineChartComponent + VolumeChartComponent
   at `options-history.component.ts:10-11`) move with it.
3. **Mount it in data-lab** — exact placement is a UX-detail decision
   (proposed default: a "Historical chain preview" card on the
   options-companion config row, opens to a modal or expandable panel).
   Track as sub-decision under D10.
4. Verify the existing `options-history.component.spec.ts` is preserved
   or rewritten — F3 has the only test among F1/F2/F4/F5 (per §4.1), so
   we lose coverage if we drop it on the floor. The spec migrates to
   `past-chain-inspector.component.spec.ts`.
5. Delete `Frontend/src/app/components/options-history/`.
6. Remove the `/options-history` route entry; add redirect to
   `/data-lab` (with a query param hinting at the inspector tab if
   technically simple, otherwise a plain redirect).
7. Watch period of 7 days; remove redirect in follow-up.

---

**Recommendation R2 — fold the in-app `options-math-docs` (F5) into the
truth doc.** Per [§7 D1](#7-decisions-log), the 583-line embedded component
is removed outright; the research-lab sub-section that hosted it links to
the relevant `#anchor` in `options-research.md` instead.

**Recommendation R3 — REVOKED.** Phase 1 verification (2026-04-29) found
that both P1 (`POST /options/contracts`) and G5 (`getOptionsContracts`)
are in active use by the `/stock-analysis` and `/stock-analysis/day/...`
routes for 0DTE contract listing. The full call chain is documented in
[§4.2.1](#421-phase-1-correction--p1-and-g5-are-not-orphan).

The original orphan claim came from a grep that restricted itself to
options-named components and to the URL path `/options/contracts` (which
the frontend never calls directly — it calls the GraphQL resolver, which
is what the C# Backend proxies through). Both filters were too narrow.

No deletion happens. Phase 4 (formerly "orphan removal") is dropped from
the phased plan in [§9](#9-phased-execution-plan) — there are no orphan
routes to remove. The phase-numbering in §9 is left as-is for
auditability; Phase 4 is now a one-line note rather than active work.

**KEEP, refactor internals only:**

- **G1 `getOptionsExpirations`, G2 `getOptionsChainSnapshot`** — these are
  the load-bearing read paths. No surface change.
- **G3 `analyzeOptionsStrategy`, G4 `comparePricingModels`** — these are the
  load-bearing compute paths. No surface change.

### 5.2 Shared logic — extract before documenting

Each extraction is a separate, individually-revertable refactor. Each lands
with a parity test that compares the new shared output to the old per-component
output across a fixture chain (e.g., SPY 2024-12-20). No extraction lands
without that parity test passing at the strict-float default
(`atol=1e-9, rtol=0`) for IV/Greeks fields, or bit-exact for string outputs.

**Recommendation R4 — `OptionsChainStateService` (Angular signal-driven).**
One service owns the prelude state machine: ticker → expirations →
selected expiration → snapshot. Each of F1, F2, F4 reads from it via
signals. Isolation: `selectedTicker`, `selectedExpiration`,
`chainSnapshot()`, `loadingState()`, `error()`. Replaces the four
duplicated state machines with one.

**Recommendation R5 — `OccTickerFormat` utility.** One TS module exports
`parseOccTicker(s)` and `formatOccTicker(parts)`. Replaces the parser
duplicated between F1 and F3. Round-trip parity test:
`format(parse(t)) === t` for every contract in a SPY golden chain.

**Recommendation R6 — `GreekFormat` utility.** One TS module exports
`fmtGreek(value, opts)`, `fmtIv(value)`, `fmtPrice(value)`, `fmtNum(value)`.
Replaces the formatters duplicated in F1, F2, F4. Snapshot test against
representative input ranges (negative deltas, near-zero gammas, NaN, ±Inf).

**Recommendation R7 — `ContractPricePicker` utility.** One TS module
implements the resolution hierarchy
`day.close → lastTrade.price → lastQuote.mid → bid/ask mid → null`.
Replaces the duplicate logic in F1 and F2. Test cases: each level present,
each level missing, all missing.

**Recommendation R8 — Make `bs_greeks.py` the single Python authority for
all UI-visible BS / Greeks numbers.** This is a re-statement of Phase 1.2
of `options-vol-platform-tdd.md`. After this plan is executed, no Angular
component imports from `Frontend/src/app/utils/black-scholes.ts` for any
number that ends up rendered. The legacy file is deleted with a parity test
that compares its output to `bs_greeks.python` outputs across a 1000-point
spot grid for a fixed (S, K, T, r, σ).

### 5.3 What is *not* recommended

These are deliberately *not* in the proposal:

- **Merging F2 `/strategy-builder` and F4 `/pricing-lab` into one page.**
  Their UX, chart libraries, and data shapes diverge. Saving a tab is not
  worth a worse workflow for either user.
- **Replacing the QuantLib pricing engine option in F2 / F4.** The
  multi-engine comparison is the *point* of those pages.
- **Refactoring F3 `/options-history` to use the snapshot endpoint.** The
  snapshot endpoint is live-only on the Starter plan. F3's contract-by-
  contract aggregate scan is the only way to reconstruct a historical chain
  from what we have access to.

---

## 6. Documentation deliverables and rigor template

The output of this work — alongside the consolidation refactors — is a set
of authority documents that match `iv-ownership-research.md` in rigor.

### 6.1 What ships

**One single authority document** for the whole options feature, parallel
to `iv-ownership-research.md` for the IV pipeline. Per Q3, the per-page
truth content lives as *sections* inside that one file, not as separate
files. Decision rationale in [§7 D3](#7-decisions-log).

```
docs/architecture/options-research.md     ← single truth doc, sections per page
```

The shape of that file (high level — §-numbering matches iv-ownership for
cross-doc coherence):

| § | Section | Source today |
|---|---|---|
| 1 | Reviewer framing | new |
| 2 | Executive overview (entire options feature) | new |
| 3 | Hard constraints (platform + options-specific) | from this plan §3 + iv-ownership §3 |
| 4 | Mathematical foundations (BS price, Greeks, IV solver, POP, payoff) | absorbs `options-math-authorities.md`, `options-cross-section-overview.md`, `options-bs-greeks-2026-04-24.md` |
| 5 | Production pipelines, **per surviving surface** | new — one subsection per surviving page or absorbed feature |
| 5.1 | Pipeline: `/strategy-builder` (chain view + payoff builder + per-contract drill-down) | new |
| 5.2 | Pipeline: `/pricing-lab` | new |
| 5.3 | Pipeline: `/data-lab` options sub-feature (companion config + past-chain inspector ported from F3) | new — incorporates the live-vs-historical constraints box that was originally planned for §5.4 |
| 5.4 | Companion data formats | absorbs `options-companion-format.md` |
| 6 | Tolerances and validation | new (cites tests across all pages) |
| 7 | Decisions log | absorbs decisions in this plan + decisions in `options-vol-platform-tdd.md` |
| 8 | Reviewer feedback log | new |
| 9 | Future plan / deferred items | absorbs the unfinished phases of `options-vol-platform-tdd.md` |
| 10 | Out of scope | new |
| 11 | References + PR audit trail | new |
| 12 | Appendix A — worked numerical examples | new, anchored per Q6 |
| 13 | Appendix B — file map | absorbs §13 of this plan |

**Existing docs that get absorbed and then deleted:**

- `docs/architecture/options-math-authorities.md` → folded into §4
- `docs/architecture/options-vol-platform-tdd.md` → roadmap items folded
  into §9; phase descriptions folded into §7 decisions log
- `docs/options-cross-section-overview.md` → folded into §4
- `docs/options-companion-format.md` → folded into §5.5
- `docs/references/options-bs-greeks-2026-04-24.md` → folded into §4
- `docs/phase-1-2-deep-dive.md` → folded into §7 (decision: server-side BS
  authority) and §9 (remaining work)

Each fold-in is a single PR that (a) copies the content into the
appropriate § of `options-research.md`, (b) replaces the source file with a
one-line redirect stub pointing to the new section anchor, (c) deletes the
stub in a follow-up commit ≥ 7 days later if no inbound link breaks.

**Existing artefact converted to markdown:**

- `Frontend/.../options-math-docs.component.ts` (583 LOC, F5) → deleted
  outright per Q1. The research-lab sub-section that hosted it links
  directly to the relevant `#anchor` of `options-research.md`. There is no
  in-app embedded math doc.

**Plus one retiring audit-trail doc** that is *not* the truth doc:

- `docs/architecture/options-cleanup-2026-XX-XX.md` — a write-only audit
  trail of every deletion, merge, and verification result for this
  cleanup. Created at the *end* of [§9 Phase 8](#9-phased-execution-plan).
  Lives separately from `options-research.md` because it is a one-shot
  ledger, not an evergreen truth doc.

**Plus one accumulator file** that is delivered to the owner as a
standalone prompt:

- `docs/architecture/options-ux-design-prompt.md` — accumulates UX-design
  questions per [§7 D11](#7-decisions-log). Seeded during Phase 0 with
  the four currently-known questions (UX-Q1 through UX-Q4); appended to
  by every Phase-3 migration PR that hits a UX choice. Delivered to the
  owner at Phase 8 alongside the cleanup audit doc, ready to paste into
  Claude Design.

### 6.2 Rigor template — every page section inside `options-research.md` contains

Skeleton in [§14 Appendix B](#14-appendix-b--documentation-skeleton-for-each-surviving-page).
The non-negotiable sub-sections (within §5.x for each page) are:

1. **Executive overview** — what the page does, in three paragraphs.
2. **Hard constraints** — table of constraints inherited from the platform
   (Polygon Starter, `int64 ms UTC`, sovereignty, etc.) and any
   page-specific ones (e.g., F3's "live snapshots not available
   historically").
3. **Mathematical foundations** — each formula on the page, with citation
   (paper or repo SHA), implementation file path, tolerance level (bit-
   exact / strict-float / behavioural per
   `numerical-rigor.md`), and a worked numerical example anchored on a
   reproducible fixture (SPY 2024-12-20 is the canonical anchor — same as
   the IV doc).
4. **Data flow** — sequence diagram or numbered list, naming every endpoint,
   service, and storage boundary. Every timestamp on the wire is annotated
   `int64 ms UTC` to make the canonical-format compliance auditable.
5. **Tolerances and validation** — which tests cover which assertions, with
   file paths. Each numerical claim cites a test.
6. **Decisions log** — accepted and rejected design choices, dated.
7. **Reviewer feedback log** — copy of the iv-ownership template.
8. **Future plan / deferred items** — anything punted, with rationale.
9. **Out of scope** — explicit non-goals.
10. **References** — papers, vendor docs, internal docs, PR list.
11. **Appendix — file map** — every code file the page touches.

### 6.3 Why this rigor

Quoting the user's framing: "We have to prove others that we are right." The
proof is built from three things, all of which the iv-ownership doc has and
the existing options docs lack:

- **A worked numerical example.** Anyone can re-run the fixture and reproduce
  the number. The 19-bp SPY-vs-CBOE agreement in the IV doc is what makes
  that doc credible. We need an analogue per page (e.g., for the strategy
  lab: a worked iron-condor P&L curve at a known spot grid that matches a
  textbook citation to within `atol`).
- **An explicit tolerance.** Without `atol=1e-9, rtol=0`, "close enough" is
  argument by tone. With it, disagreement is falsifiable.
- **A reviewer-feedback log.** Showing the prior critiques and how they
  were addressed (or rejected with rationale) is the audit trail that
  separates authority docs from marketing copy.

---

## 7. Decisions log

Ratified 2026-04-29. Each decision retains the original framing for
auditability so a future reviewer can see *what was asked* and *what was
chosen*. Supersedes the open-questions list that lived here in the first
revision of this file.

**D1 — F5 `options-math-docs` component fate. Decision: DELETE.**

The 583-line in-app math reference is removed. All math content lives in
`docs/architecture/options-research.md` §4 (markdown). The research-lab
sub-section that hosted F5 becomes a link-out to the relevant `#anchor`.

*Rationale:* Owner directive — "all our documentation in the md format".
Matches single-source-of-truth and the IV-ownership precedent
(no in-app math doc).

*Implication:* the `Frontend/src/app/components/research-lab/options-math-docs/`
directory is removed in [§9 Phase 6](#9-phased-execution-plan), in the same
PR that adds the link-out.

**D2 — Test backfill scope. Decision: (b) FULL BACKFILL.**

Component specs are written for F1 (`/options-chain`), F2
(`/strategy-builder`), and F4 (`/pricing-lab`) — i.e., for every surviving
options-touching component without a `.spec.ts` today. Backend GraphQL
resolver tests are added for G2 (`getOptionsChainSnapshot`),
G3 (`analyzeOptionsStrategy`), and G4 (`comparePricingModels`). Python
service tests are added for `app/services/strategy_engine.py` (the
`analyze_strategy` POP / EV / Greeks-curve path), and for the
`app/engine/options/` adapter layer if any of its surface ends up cited
by the truth doc.

*Rationale:* owner directive — match the IV-doc rigor count-for-count.
The truth doc will cite tests across every §5.x sub-section, not just
the new math.

*Trade-off accepted:* this phase's ETA is ~1–2 weeks of test-authoring
work (vs ~2 days for the minimum-viable alternative). It is the
single largest piece of work in the plan.

*Constraint inherited:* `numerical-rigor.md` § "new-math-only rule, not
a backfill" is *softened*, not violated — the rule says backfill is not
*automatic*; it does not forbid backfill *when authorised*. This is
authorised.

**D3 — Authority document structure and ordering. Decision: SINGLE doc,
authority first.**

One file, `docs/architecture/options-research.md`, contains the
authoritative content for the whole options feature. Per-page material
lives as §5.1, §5.2, §5.3, §5.4 inside it (see §6.1). The authority
sections (§3 constraints, §4 math) are written before the per-page §5.x
sections, so the per-page sections can reference back to them.

*Rationale:* Owner directive — "finally there should be only one
authority documentation for the options feature." Matches IV-ownership
precedent (one file for the whole IV pipeline).

*Operational consequence:* the existing docs listed in
[§4.4](#44-existing-options-related-docs) (`options-math-authorities.md`,
`options-vol-platform-tdd.md`, `options-cross-section-overview.md`,
`options-companion-format.md`, `options-bs-greeks-2026-04-24.md`,
`phase-1-2-deep-dive.md`) are absorbed into `options-research.md` and then
deleted (with the redirect-stub-then-delete protocol in §6.1). No sibling
docs are spawned during subsequent revisions; the doc is re-revised
in place per the IV-ownership convention.

**D4 — Authorisation scope. Decision: ALL authorisations granted.**

Cleared to read `Backend.Tests/Unit/GraphQL/QueryTests.cs`,
`Backend/Schedulers/*` (if present), and any other Backend / Backend.Tests
file needed to verify caller-counts before deletion. Not authorised to:
delete data, push to remote, modify CI, or run destructive operations.

**D5 — `analyzeOptionsStrategy` resolver shape. Decision: confirmed QUERY.**

Verified at [Backend/GraphQL/Query.cs:834-847](../../Backend/GraphQL/Query.cs#L834-L847):
`[GraphQLName("analyzeOptionsStrategy")] public async
Task<StrategyAnalyzeResult> AnalyzeOptionsStrategy(...)` — defined on the
`Query` class, not `Mutation`. XML doc explicitly: "All probability math
is computed server-side in Python using Black-Scholes." No state change.

*Implication:* §5.2 of the truth doc describes it as a pure compute path —
"inputs → server-side BS + numerical integration → payoff/Greeks/POP
curves". The truth doc does not need a "what state changes" subsection
for this resolver.

*Bonus finding while verifying:* a third frontend route consumes this
resolver — `strategy-builder.component.ts` (route `/strategy-builder`).
This was missed in the original §4.3 inventory because the route name
does not contain "options". See
[§7.1 Scope amendment](#71-scope-amendment--strategy-builder-as-third-consumer).

**D6 — Worked-example anchor. Decision: SPLIT — textbook for math, market
date for end-to-end fixture.**

The §4 Mathematical foundations sections anchor every formula on a
**textbook example** (Hull, *Options, Futures, and Other Derivatives*,
9th ed.; specific section/page cited per formula). This makes the math
reproducible by anyone with the textbook — no vendor access required.

The §5 Production pipeline sections anchor the end-to-end fixtures on a
**real market date** (default: SPY 2024-12-20, same as the IV doc, for
cross-doc coherence). This proves the production pipeline works on real
Polygon data.

*Rationale:* different jobs, different anchors. Math reviewers need the
textbook; pipeline reviewers need the market date. Forcing one anchor to
serve both produces a worked example that is either non-textbook
(reviewer can't independently check) or non-production (doesn't prove
the pipeline).

*Reformulated yes/no:* this default applies unless the owner overrides
on a specific section. The owner's response to the [Q6 reformulation](#71-scope-amendment--strategy-builder-as-third-consumer)
in the cover note ratified this approach.

**D7 — Deletion in same PR as refactor (R3, R4 and analogues). Decision:
ALLOWED.**

When a consolidation move (R3 orphan removal, R4–R8 extractions) ships,
the legacy code is deleted in the *same* PR as the new code, conditional
on the parity test from [§8.2](#82-parity-tests-for-every-extraction)
passing. No deprecation shims, no `// removed` comments, no dual-running
with feature flags.

*Rationale:* Owner directive — "delete can be used". Also matches CLAUDE.md
("avoid backwards-compatibility hacks like renaming unused _vars,
re-exporting types, adding // removed comments…").

*Exception:* the verification protocol in
[§8.1](#81-pre-deletion-verification-protocol) still applies for
*orphan* removals (R3, P1, G5) — those have a 7-day watch period because
the absence of a caller is asserted by negative evidence (greps), and
the watch is the second mitigation. Refactor deletions (R4–R8) are
positively gated by the parity test, so no watch period.

**D8 — Survivor between `/options-strategy-lab` and `/strategy-builder`.
Decision: `/strategy-builder` SURVIVES; `/options-strategy-lab` DELETED.**

Both routes are multi-leg options-strategy builders. They share:

- The same chain-row shape (`SnapshotContractResult` per call/put/strike).
- The same `PayoffChartComponent` (currently at
  `Frontend/src/app/components/options-strategy-lab/payoff-chart/`,
  imported by `strategy-builder.component.ts:27`).
- The same `ExpirationRibbonComponent` from `options-chain-v2/`.
- The same backend resolver (`analyzeOptionsStrategy`).
- The same per-leg config shape (`{strike, optionType, position, premium,
  iv, quantity, enabled}`).

This is functional duplication, not two distinct workflows. Owner
directive: keep `/strategy-builder`, delete `/options-strategy-lab`.

*Operational consequence (executed in [§5.1 R0](#51-routes--keep-merge-delete)):*

1. Relocate `payoff-chart/` from `options-strategy-lab/` to
   `Frontend/src/app/shared/payoff-chart/` (or another shared
   location) before deleting the `options-strategy-lab/` directory.
   `strategy-builder.component.ts:27` is the only known external
   consumer; verify by grep.
2. Delete `options-strategy-lab.component.ts`/`.html`/`.scss`.
3. Remove the `/options-strategy-lab` route entry from
   `app.routes.ts:83-88`.
4. Add a 7-day-watch redirect (`options-strategy-lab` →
   `strategy-builder`) following the `lean-engine` → `engine` redirect
   precedent at `app.routes.ts:189-192`.
5. Remove the redirect in a follow-up commit ≥ 7 days later if no
   bookmark / inbound link breaks.

*Implication for the truth doc:* §5.2 of `options-research.md` describes
`/strategy-builder` (not `/options-strategy-lab`). The dead route is
listed in §13 file map under "deleted in this cleanup" with the PR
number, so future readers can find what happened.

*Side-effect (now resolved):* the prior open question of whether
`/strategy-builder` was in scope — surfaced when D5 verification found it
as a third consumer of `analyzeOptionsStrategy` — is closed by D8: it is
**the** in-scope strategy builder. There is no longer a second one to
debate.

**D9 — `/options-chain` consolidation. Decision: option (c) — migrate
drill-down into `/strategy-builder`, then DELETE.**

The chain view on `/options-chain` substantially overlaps with
`/strategy-builder`'s chain rendering. F1's load-bearing unique features
over F2 are:

- **Per-contract historical drill-down drawer.** Click a strike → drawer
  opens with a 2-year candlestick chart of the contract's premium plus
  a volume chart. Wired via `Drawer` + `CandlestickChartComponent` +
  `VolumeChartComponent` at
  [options-chain.component.ts:20-22](../../Frontend/src/app/components/options-chain-v2/options-chain.component.ts#L20-L22).
  This is preserved by migrating the three components into
  `/strategy-builder` per [§5.1 R0b](#51-routes--keep-merge-delete) before
  the F1 deletion.
- **Full-Greek-per-row display** (vega/theta/gamma all rendered alongside
  delta in each chain row). Strategy-builder's chain row shows
  predominantly delta. This is a *minor* unique feature; preservation is
  a sub-decision listed below.

*Sub-decision D9a — preserve full-Greek-per-row display.
Decision: PRESERVE (ratified 2026-04-29).*

Strategy-builder's chain table extends `BuilderChainRow` with Vega,
Theta, Gamma columns alongside the existing Delta. The R6 `GreekFormat`
utility supplies the formatters. The chain table gets denser but
preserves parity with the F1 chain reader's output, so the cutover from
`/options-chain` to `/strategy-builder` is feature-complete, not
feature-reduced.

*Implication for R0b operational steps:* step (3) "optionally extend
`BuilderChainRow`" is no longer optional — it ships with the migration
PR.

*Operational consequence (executed in [§5.1 R0b](#51-routes--keep-merge-delete)):*

1. Add the drill-down drawer to `/strategy-builder` (PrimeNG `Drawer`,
   `CandlestickChartComponent`, `VolumeChartComponent` — same imports F1
   uses today).
2. Wire click-strike → open-drawer → fetch
   `getOrFetchStockAggregates(occTicker, fromDate, toDate, 'day')` →
   render. Use R5 `OccTickerFormat` for contract-metadata header.
3. Per D9a outcome, optionally extend `BuilderChainRow` with Vega/Theta/
   Gamma cells.
4. Delete `Frontend/src/app/components/options-chain-v2/` *except*
   `expiration-ribbon/` (already imported by `/strategy-builder` per
   [strategy-builder.component.ts:26](../../Frontend/src/app/components/strategy-builder/strategy-builder.component.ts#L26)).
5. Remove `/options-chain` route entry; add redirect to
   `/strategy-builder`.
6. Watch period of 7 days; remove redirect in follow-up commit.

*Implication for the truth doc:* there is no §5.x for `/options-chain`.
The chain-viewer pipeline is documented inside §5.1 (`/strategy-builder`)
because that is where it lives after the migration. The dead route is
listed in §13 file map under "deleted in this cleanup" with the PR
number.

**D10 — `/options-history` consolidation. Decision: PORT into `/data-lab`,
then DELETE the route.**

The owner's directive — "if it is a well built functionality that can be
ported easily port it" — applies. F3 today is one ~407-line component
that mixes:

- A data-fetching procedure (analyze() at lines 117-289 — fetches stock
  bars, computes ATM, constructs OCC tickers for ±5n strikes, batches
  per-contract aggregates 30 at a time, filters to N strikes per side
  with data, builds rows).
- A rendering layer (calls/puts split, ATM marker, change-from-prior-
  close coloring, per-contract LineChart + VolumeChart drill-down).
- A scan-results audit table (which strikes had data, which made the
  cut).

This is portable (it has a spec file, defined `ContractRow` /
`ScanResult` interfaces, sensible batching, no entangled global state),
but the port is *not* a copy-paste — the procedure has to be lifted out
of the component into a service before it can be hosted elsewhere.

*Operational consequence (executed in [§5.1 R1](#51-routes--keep-merge-delete)):*

1. Lift `analyze()` into `Frontend/src/app/services/past-chain.service.ts`
   exposing `fetchPastChain(ticker, date, numStrikes, atmMethod) →
   PastChainResult`. The OCC construction step at lines 192-214 calls
   into R5 `OccTickerFormat` once R5 ships.
2. Lift the rendering UI into
   `Frontend/src/app/components/data-lab/past-chain-inspector/` as a
   standalone sub-component that takes `PastChainResult` as input.
3. Mount the sub-component in `/data-lab` (UX detail — sub-decision D10a
   below).
4. Migrate `options-history.component.spec.ts` to
   `past-chain-inspector.component.spec.ts` (don't lose coverage —
   F3 has the only spec among the deleted components).
5. Delete `Frontend/src/app/components/options-history/`.
6. Remove `/options-history` route entry; add redirect to `/data-lab`.
7. Watch period of 7 days; remove redirect in follow-up commit.

*Sub-decision D10a — past-chain inspector mount point in data-lab.
Decision: option (i), CARD on the options-companion config row
(ratified 2026-04-29).*

The inspector lives as a compact card colocated with the options-
companion config knobs (`optionsStrikesEachSide`, `optionsIncludeCalls`,
`optionsIncludePuts`, `optionsDteDistance`). The card carries a
"Preview chain on this date" button that opens the inspector — modal
or expandable panel is a UI-detail choice for the implementing PR
(default: expandable panel inline with the card so the user can keep
the config visible while inspecting).

*Rationale:* the past-chain view is a *preview* of what the bundle
will fetch on its run, so colocating it with the bundle config is
the natural place. Owner has not requested a heavier UX (tab or
results-inline) at this time.

*Implication for R1 operational steps:* step (3) "Mount the
sub-component in /data-lab" specifies the card location described
above. The PR includes a screenshot showing the card in context.

*Implication for the truth doc:* §5.3 of `options-research.md` documents
the data-lab options sub-feature: companion-config + past-chain
inspector together. The §3 hard-constraints box for "live snapshot
endpoint is unavailable historically on Polygon Starter" lives inside
§5.3 because that constraint is what makes the past-chain inspector's
OCC-scan approach necessary.

*Side effect of D10:* the `getOrFetchStockAggregates` batching pattern
(30-at-a-time) becomes part of `past-chain.service.ts`'s public
contract. It is a sensible default but should be considered a tunable.
Documented in §5.3 of the truth doc.

**D11 — UX-design questions are accumulated, not blocked on. Decision:
DEFER UX decisions to a "Claude Design" prompt deliverable (ratified
2026-04-29).**

**Update 2026-04-29 (later same day): the design pass landed.** Bundle
hash `Ld_D7E4LcbEWqq4z2WPl0g`,
`quant-trading-lab-design-system/project/options_ux_design/`. All four
UX questions answered; the picks are recorded inline in
[`options-ux-design-prompt.md`](options-ux-design-prompt.md) under each
UX-Q heading. Headline answers:

- **UX-Q1** — drill-down trigger = **icon button per side**
  (📈 calls / 📉 puts) outside the chain row; row body and L/S buttons
  stay free.
- **UX-Q2** — density default = **"Quick" (Δ + Price + Vol)** with a
  **"Full Greeks" toggle** (V/Θ/Γ added) sticky per-user via
  `localStorage`.
- **UX-Q3** — past-chain inspector = **inline collapsed card** →
  loading-with-progress → expanded chain → **modal** drill-down.
  "Show scan details" link off by default.
- **UX-Q4** — strategy-builder layout = **two-column 60/40** (chain
  left, build + payoff stacked right); templates as pills above the
  chain; scenario toggles inline beneath the chart.

The locked picks unblock R0b and R1, which were previously
UX-design-gated. R0b/R1 implementation work resumes; see
[§9 Phase 3](#9-phased-execution-plan) status table.

When a phase hits a UX-design choice that the implementing agent cannot
resolve cleanly from existing patterns in the codebase — visual
hierarchy, density, layout, interaction modality, transition timing,
copy, iconography — the choice is **not** blocked on owner input and
**not** decided arbitrarily. Instead, the question is appended to a
single accumulator file:

```
docs/architecture/options-ux-design-prompt.md
```

…with enough context that the question is self-contained when read
later: what the user is trying to do, what the implementer chose as a
working default to unblock the PR, what the surrounding visual
neighbours look like, what trade-offs are in play, and what the
implementer wants the design pass to consider.

The implementer ships the working default (so progress doesn't stall)
and adds the entry in the same PR. At the end of the project (Phase 8),
the accumulated file is delivered alongside the cleanup audit doc as
a self-contained prompt the owner can paste into Claude Design (or
another design-capable LLM) to get a UX-improvement pass over the
whole feature.

*Rationale:* the implementing agent is a software-engineering agent,
not a UX designer. Asking it to make UX decisions in isolation produces
mediocre defaults; asking it to *block* on every UX choice produces a
stalled project. Externalising design to a dedicated pass at the end
is the right division of labour.

*Scope of D11 (what goes in the prompt):* anything that is purely a
UX/visual/interaction decision. Examples already known to be in scope:

- **D9 / R0b — drill-down trigger ambiguity in `/strategy-builder`:**
  in F1, clicking a chain cell opens the historical drawer. In
  `/strategy-builder`, clicking a chain cell adds a leg. After R0b,
  both behaviours coexist on the same chain table. How does the user
  disambiguate? (Modifier key, separate icon, right-click menu, hover
  affordance, double-click, …?)
- **D9a — chain-table density under the preserve decision:** the
  table now carries Vega/Theta/Gamma/Delta/Price/Bid-Ask/OI/Volume per
  side plus Strike + IV centre. Strategy-builder co-exists with the
  leg builder and the payoff chart on the same page. How is the chain
  laid out so it remains scannable without dominating the page?
- **D10a — past-chain inspector card visual:** the card lives on the
  options-companion config row in `/data-lab`. What does it look like
  in its collapsed state, in its expanded state, and during loading?
  Where does the "scan results" audit table go (today F3 shows it
  prominently — does that survive the port at the same prominence)?
- **R0b — drawer placement in `/strategy-builder`:** strategy-builder
  already has a drawer for some functions (per its imports at
  [strategy-builder.component.ts:8](../../Frontend/src/app/components/strategy-builder/strategy-builder.component.ts#L8)).
  Does the drill-down reuse the same drawer or a new one? What's the
  z-index / focus-trap relationship?

*Scope exclusions (what does NOT go in the prompt):* math choices,
tolerance choices, route surface choices, data-format choices, test
coverage choices. These are engineering decisions the implementer owns.

*Format of each entry:* see the seed at
[docs/architecture/options-ux-design-prompt.md](options-ux-design-prompt.md).
Each entry is a self-contained section with: title, context, what was
done as a working default, screenshots-or-paths-to-component, and the
specific UX questions the design pass should answer.

*Phase-8 deliverable:* the accumulator file is delivered to the owner
alongside the cleanup audit doc. Owner pastes it into Claude Design;
Claude Design returns a UX-improvement plan; owner picks which items
to action in a follow-up PR (out of scope for *this* cleanup).

*Implication for execution speed:* implementing PRs no longer block
on UX questions. They ship working defaults and extend the prompt.
This is the single most important productivity enabler in this plan.

---

## 8. Validation & proof plan

This section operationalises [§6.3 — why this rigor](#63-why-this-rigor).

### 8.1 Pre-deletion verification protocol

For every route or resolver proposed for deletion (no candidates today
after R3 was revoked; future candidates as discovered), the deletion
commit message includes:

1. **The frontend grep** that proved no direct caller exists, with the
   exact pattern, the search root, and the date run. Use the **resolver
   method name** as the pattern (e.g., `marketDataService\.getOptionsContracts`),
   **not** the URL path — URL paths are reached indirectly via the
   Backend C# layer and a URL-path-only grep produces false negatives
   (this is the lesson from the [§4.2.1 Phase 1 correction](#421-phase-1-correction--p1-and-g5-are-not-orphan)).
2. **The C# layer grep** — search `Backend/` for any function named or
   ending in the C# equivalent of the resolver
   (`FetchOptionsContractsAsync`, etc.). If the C# layer references the
   surface, it is not orphan even if the frontend doesn't call it
   directly; it may be feeding another GraphQL resolver, a scheduled
   job, or a webhook.
3. **The Python layer grep** — for REST endpoints, search
   `PythonDataService/app/` for the URL path string and for the
   `polygon_client` callee name. Background jobs and tests may consume
   the endpoint without touching the frontend.
4. **The result of all three greps** ("0 matches across `Frontend/src`,
   `Backend/`, and `PythonDataService/`").
5. **The reverse search** — grepping for the *callee* function name
   (e.g., `list_options_contracts`) across the *whole repo* to find
   indirect callers, with results.
6. **The duration of the watch period** — at least 7 days between the
   verification commit and the deletion commit, so any uncommitted local
   work has a chance to surface a missed caller.
7. **The fallback plan** — the commit that re-introduces the surface, in
   case a caller is found post-deletion.

This protocol is borrowed directly from the way `iv-ownership-research.md`
audits Polygon's `implied_volatility` field — claim, evidence, watch
period, fallback. **Three-layer grep** (frontend + C# + Python) is the
addition that came out of the Phase 1 correction; the v1 protocol only
specified the frontend grep, and that is what missed the indirect
P1/G5 chain.

### 8.2 Parity tests for every extraction

Each `R4–R8` extraction lands with a test that runs the *new* shared
implementation and the *old* per-component implementation against the same
fixture, and asserts equivalence at the appropriate tolerance:

| Extraction / migration | Tolerance | Fixture |
|---|---|---|
| R4 `OptionsChainStateService` | Behavioural — same emitted snapshot for same input ticker/expiration | SPY 2024-12-20 + 1 alternate date for sanity |
| R5 `OccTickerFormat` | Bit-exact (string round-trip) | All contracts in SPY 2024-12-20 chain |
| R6 `GreekFormat` | Bit-exact (string output) | Cartesian sweep over (negative δ, near-zero γ, NaN, ±Inf, very large vega) |
| R7 `ContractPricePicker` | Strict float (`atol=1e-9, rtol=0`) | Synthetic snapshot covering each field-presence permutation |
| R0a payoff-chart relocation | Behavioural — `/strategy-builder` renders identically before and after the relocate-only PR (no logic change, just import path) | Manual smoke + existing strategy-builder integration paths |
| R0b drill-down migration into `/strategy-builder` | Behavioural — clicking a strike in `/strategy-builder` produces the same drawer + charts as `/options-chain` did for the same OCC ticker / date range | SPY 2024-12-20, three contracts (one ATM call, one ITM put, one OTM call) — golden snapshot of the drawer's data shape |
| R1 past-chain port | Behavioural — `past-chain.service.ts.fetchPastChain(ticker, date, n, atmMethod)` returns the same `ContractRow[]` shape and values that the legacy `analyze()` produced | AAPL on the default last-weekday for `numStrikes=5`, `atmMethod='open'` (current F3 default) — record output once, assert byte-equal thereafter |
| R8 `bs_greeks` authority | Strict float for price (`atol=1e-9, rtol=0`); Greeks `atol=1e-6, rtol=1e-6` per existing default | 1000-point spot grid for one (K, T, r, σ) tuple |

After the parity test passes, the legacy code is deleted in the same PR
per [§7 D7](#7-decisions-log). Deleting in a separate PR is allowed only
if the legacy code is unreachable (verified via [§8.1](#81-pre-deletion-verification-protocol)
protocol — applies to R3 orphan removal and to the redirect-cleanup
follow-ups for R0a / R0b / R1).

### 8.3 End-to-end fixture for each §5.x page section

Each §5.x sub-section of `options-research.md` cites at least one
**end-to-end fixture** — e.g., "SPY 2024-12-20, expiration 2025-01-17,
spot 591.15, all 50 calls and 50 puts within 5% of ATM, run through the
page's primary workflow, output matches the recorded golden values."
This fixture lives under
`PythonDataService/tests/fixtures/golden/<page-name>/` and follows the
attribution rules in `numerical-rigor.md` § Golden fixtures.

The fixture is what gives the doc its proof. Without it, the doc is a
description. With it, the doc is auditable.

### 8.4 Reviewer-feedback round

After Phase 5 of [§9](#9-phased-execution-plan), `options-research.md` goes
to an external LLM reviewer (in the role used for the IV docs — quant /
math reviewer, not code-style). §8 of the truth doc is populated from
that round's responses.

---

## 9. Phased execution plan

Each phase is gated. No phase advances until its exit criterion is met.

### Phase 0 — Alignment (no code) — DONE 2026-04-29

- Owner reviewed this document.
- D1–D10 in [§7](#7-decisions-log) ratified, including sub-decisions
  D9a (preserve full-Greek-per-row) and D10a (past-chain inspector
  mounts as a card on the options-companion config row). D2 confirmed
  as (b) full-backfill.

**Exit criterion (met):** all original questions answered in §7;
expanded scope (D9, D10) ratified; sub-decisions D9a, D10a locked.

### Phase 1 — Authority refresh and inventory ratification — DONE 2026-04-29

**Authority refresh — done.** [docs/architecture/options-math-authorities.md](options-math-authorities.md)
revised:

- Stamped "Last reviewed: 2026-04-29 (Phase 1 of options-routes cleanup)".
- Fixed factual error on line 29: the QuantLib IV function is
  `quantlib_pricer.implied_volatility` (line 314 of that file),
  **not** `solve_implied_volatility` as the doc previously claimed. A
  caveat was added clarifying that direct callers should use
  `volatility/solver.implied_volatility` — the QL path is the internal
  branch of the solver's fallback chain, not a public alternate.
- All other module/function names in the table (six modules, eleven
  functions) were verified against the code on 2026-04-29 and confirmed
  correct: `bs_european_price`, `bs_european_vega`, `black_scholes_greeks`,
  `implied_volatility`, `solve_iv_chain`, `compute_skew_metrics`,
  `compute_put_call_parity_forward`, `price_option`, `price_strategy`,
  `price_contract`, `price_contract_from_market`. The deletion of
  `app/research/options/bs_solver.py` (claimed in the doc's history
  section) is also confirmed — the file is gone.

**Inventory ratification — done, with one correction.** Phase 1
re-greps surfaced two findings:

1. **G5 / P1 are not orphan.** Original hypothesis was wrong; the C#
   Backend at [Backend/Services/Implementation/PolygonService.cs:545-547](../../Backend/Services/Implementation/PolygonService.cs#L545-L547)
   POSTs `/api/options/contracts` for the `/stock-analysis` and
   `/stock-analysis/day/...` 0DTE-listing pages. Full audit in
   [§4.2.1](#421-phase-1-correction--p1-and-g5-are-not-orphan).
   R3 revoked in [§5.1](#51-routes--keep-merge-delete) and Phase 4
   dropped. Verification protocol [§8.1](#81-pre-deletion-verification-protocol)
   hardened to require three-layer greps (frontend + C# + Python) plus
   a reverse callee-name grep.

2. **The other §4 inventory entries hold.** Re-greps confirm:
   - `/options/contracts` URL path: zero matches in `Frontend/src` (the
     direct-call check still returns zero — but as Phase 1 surfaced,
     that's not the right check for orphan status).
   - `marketDataService.getOptionsContracts` (G5 caller): two matches
     in `stock-analysis` — drove the correction above.
   - `analyzeOptionsStrategy`: confirmed three frontend callers
     (`options-strategy-lab`, `strategy-builder`, plus the same name
     in `utils/black-scholes.ts` as a comment reference).
   - `PayoffChartComponent`: imported by `options-strategy-lab` (its
     home) and `strategy-builder` (the migration target — confirms R0a
     dependency).

**Exit criterion (met):** authority doc revised + correct; inventory
locked; one significant correction (R3 revocation) made and audit-
trailed.

### Phase 2 — Test gap closure for the surviving pages — DONE 2026-04-29

**Outcome:** all tests green; no regressions in either suite.

- ✅ Backend GraphQL resolver tests for G1, G3, G4 added at
  [`Backend.Tests/Unit/GraphQL/QueryTests.cs`](../../Backend.Tests/Unit/GraphQL/QueryTests.cs)
  in three new regions:
  - `#region GetOptionsExpirations` (3 tests: success, filter pass-through, service-throws).
  - `#region AnalyzeOptionsStrategy` (3 tests: base shape, Phase-1.1 flag propagation, service-throws).
  - `#region PricingModelComparison` (3 tests: full model-curve mapping, numPoints + spotMin/spotMax pass-through, service-throws).
  - All 9 pass; full `QueryTests` suite is 23/23 green.
- ✅ Frontend spec for F2 added at
  [`Frontend/src/app/components/strategy-builder/strategy-builder.component.spec.ts`](../../Frontend/src/app/components/strategy-builder/strategy-builder.component.spec.ts).
  Targets the SB-A (data-fetch prelude), SB-C (analyze workflow),
  and SB-G (edge cases) buckets per the agreed minimum-viable set.
  15 tests / 15 pass.
- ✅ Frontend spec for F4 added at
  [`Frontend/src/app/components/pricing-lab/pricing-lab.component.spec.ts`](../../Frontend/src/app/components/pricing-lab/pricing-lab.component.spec.ts).
  Targets the PL-A (data-fetch prelude), PL-B (compare workflow),
  and PL-E (edge cases) buckets. 12 tests / 12 pass.
- ✅ Full frontend test suite: **47 test files / 511 tests passing**
  after adding the two new specs.
- ✅ (already existed) Python `strategy_engine` coverage — 818 lines
  of tests across 2 files; confirmed sufficient.
- ⏸ Phase-3-deliverable specs (`past-chain.service.ts`,
  `past-chain-inspector.component.spec.ts`) — not in Phase 2.

**UX-design questions raised during Phase 2 (per [§7 D11](#7-decisions-log)):**
none. Phase 2 was test-authoring; no UI choices needed defaults.

**Truth-doc citations enabled by Phase 2.** The §6 (Tolerances and
validation) section of `options-research.md` now has concrete cites:
- `analyzeOptionsStrategy` shape contract → `QueryTests.cs:AnalyzeOptionsStrategy_*`
- `pricingModelComparison` shape contract → `QueryTests.cs:PricingModelComparison_*`
- `getOptionsExpirations` shape contract → `QueryTests.cs:GetOptionsExpirations_*`
- `/strategy-builder` analyze workflow + edge cases → `strategy-builder.component.spec.ts:SB-C/SB-G`
- `/pricing-lab` compare workflow + edge cases → `pricing-lab.component.spec.ts:PL-B/PL-E`

Per [§7 D2](#7-decisions-log): full backfill.

**Phase-2 entry inventory correction (2026-04-29):** the Phase-0 surface
audit overstated the gap. Existing coverage on entry:

- `app/services/strategy_engine.py` — **already heavily tested** by
  [`tests/test_strategy_engine.py`](../../PythonDataService/tests/test_strategy_engine.py)
  (612 lines: TestPayoffAtExpiry, TestStrategyCost, TestBreakevens,
  TestMaxProfitLoss, TestWeightedIV, TestD2, TestPOP,
  TestExpectedValue, TestPayoffCurve, TestAnalyzeStrategy,
  TestInterpolateIV, plus an iron-condor four-leg case) and
  [`tests/test_strategy_engine_phase1_1.py`](../../PythonDataService/tests/test_strategy_engine_phase1_1.py)
  (206 lines: TestPayloadShapeStableByDefault, TestCurrentCurve,
  TestGreekCurves, TestLegDiagnostics, TestZeroDTEHandling). No
  Python-side work in Phase 2.
- Backend GraphQL resolver tests at
  [`Backend.Tests/Unit/GraphQL/QueryTests.cs`](../../Backend.Tests/Unit/GraphQL/QueryTests.cs)
  cover **G2 `getOptionsChainSnapshot`** (3 cases: success, null
  underlying, service throws) and **G5 `getOptionsContracts`** (2
  cases: success, service throws). G1, G3, G4 are still gaps.

**Actual Phase-2 work:**

- **Backend GraphQL resolver tests** for G1 (`getOptionsExpirations`),
  G3 (`analyzeOptionsStrategy`), G4 (`comparePricingModels`). Three
  new test regions in `QueryTests.cs` following the existing
  `MethodName_Scenario_ExpectedResult` convention and the
  success/error paths the existing G2/G5 tests use as a template.
- **Frontend specs** for F2 (`/strategy-builder`) and F4
  (`/pricing-lab`). Angular Testing Library + Vitest, mocked
  `MarketDataService` at the DI level.
- No spec is written for F1 (`/options-chain`) or F3
  (`/options-history`) because both routes are deleted in Phase 3 —
  the F3 spec migrates to `past-chain-inspector.component.spec.ts` in
  Phase 3.
- Spec for the new `past-chain-inspector` sub-component (Phase 3
  delivers this; the spec lives next to it).
- Spec for `past-chain.service.ts` (Phase 3 delivers this; service
  test asserts OCC-ticker construction, batching, and result shape).

**Revised effort estimate:** ~2–3 days (down from the original
1–2 weeks estimate, on the basis of the Phase-2-entry inventory
correction). The original estimate assumed strategy_engine and Backend
resolvers were untested; both were partially tested.

**Exit criterion:** every assertion the truth doc will make has a passing
test that can be cited by file:line.

### Phase 3 — Migrations + shared-logic extractions — PARTIAL (2026-04-29)

What shipped vs deferred is recorded item-by-item below; the full
per-item audit is in
[`docs/architecture/options-cleanup-2026-04-29.md`](options-cleanup-2026-04-29.md).

| ID | Status | Notes |
|---|---|---|
| **R5 `OccTickerFormat`** | ✅ DONE | `Frontend/src/app/utils/occ-ticker.ts` + 18-test parity spec including round-trip on 7 representative tuples. |
| **R0a delete `/options-strategy-lab`** | ✅ DONE | `payoff-chart/` relocated to `Frontend/src/app/shared/`. Component dir deleted. Route replaced with 7-day-watch redirect to `/strategy-builder`. Spec still 15/15 green. |
| **R0b delete `/options-chain`** | ⏸ DEFERRED — UX-design-gated | UX-Q1, UX-Q2, UX-Q4 in [`options-ux-design-prompt.md`](options-ux-design-prompt.md) need a Claude Design pass. Mechanical migration is doable but the UX cost of guessing is too high to ship blind to a working production page. |
| **R1 port `/options-history` → `/data-lab`** | ⏸ DEFERRED — UX-design-gated | UX-Q3 (past-chain inspector card visual) in the design prompt. Plus the port itself is non-trivial: service extraction + new sub-component. |
| **R8 `bs_greeks` authority migration** | ⏸ DEFERRED — focused session | Sovereignty math; needs the 1000-point parity test plus a latency benchmark before swapping the live-preview Greeks path. Phase 1.2 of `options-vol-platform-tdd.md`. |
| **R6 `GreekFormat`** | ⏸ DEFERRED — post-consolidation | Reclassified: not a migration enabler since strategy-builder already has its own local versions. Smaller blast radius after R0b/R1 land. |
| **R7 `ContractPricePicker`** | ⏸ DEFERRED — post-consolidation | Same reclassification as R6. |
| **R4 `OptionsChainStateService`** | ⏸ DEFERRED — post-consolidation | After R0b/R1 land, only 2 live-chain consumers remain (`/strategy-builder`, `/pricing-lab`); abstraction has lower leverage. |

PR order when work resumes: **R0b** (largest blast-radius deferred
item, currently gating §5.1 of the truth doc), then **R1**, then
**R8**, then **R6/R7/R4** as cleanup.

Each PR contains its parity test from [§8.2](#82-parity-tests-for-every-extraction).
Legacy code removed in the same PR per [§7 D7](#7-decisions-log),
unless the verification protocol [§8.1](#81-pre-deletion-verification-protocol)
applies (R0a's redirect carries a 7-day watch period; R0b, R1 will too).

**Exit criterion (target):** R0b, R1, R8 land. **(Today: R5 + R0a only.)**

### Phase 4 — Orphan removal — DROPPED

R3 (orphan removal of P1 and G5) was revoked in Phase 1 once the full
call chain `frontend → G5 → C# → P1` was discovered. There are no
orphan routes to remove. See [§4.2.1](#421-phase-1-correction--p1-and-g5-are-not-orphan)
and [§5.1 R3](#51-routes--keep-merge-delete) for the audit trail.

The phase-numbering is preserved (Phase 4 still exists as a numbered
slot) so prior references stay valid; future phases keep their
numbering. **Exit criterion: trivially met (nothing to do).**

### Phase 4.5 — Redirect cleanup

After ≥ 7 days from each Phase-3 redirect commit, remove the redirects
introduced by R0a, R0b, R1 (`/options-strategy-lab`, `/options-chain`,
`/options-history` → survivor pages). One commit per redirect.

**Exit criterion:** `app.routes.ts` no longer contains the dead-route
redirects; no inbound links broke during the watch period.

### Phase 5 — Single truth doc authoring (`options-research.md`) — MVP SCAFFOLD SHIPPED (2026-04-29)

[`docs/architecture/options-research.md`](options-research.md) is
created with §1–§4 fully populated, §6/§7/§9–§13 populated, and §5.x
production-pipeline sections as stubs awaiting R0b/R1 to land. Per-page
fleshing-out happens after the corresponding migration ships.

Original phasing description retained below for reference.



Per [§7 D3](#7-decisions-log): one file, sections per surviving surface.
Authoring order:

1. §1–§4 (framing, overview, constraints, math) — first PR. Absorbs
   `options-math-authorities.md`, `options-cross-section-overview.md`,
   `options-bs-greeks-2026-04-24.md` content into §4 with the textbook-
   anchored worked examples per [§7 D6](#7-decisions-log).
2. §5.1 (`/strategy-builder` — chain view + payoff builder + drill-down),
   §5.2 (`/pricing-lab`), §5.3 (`/data-lab` options sub-feature),
   §5.4 (companion data formats — absorbs `options-companion-format.md`)
   — one PR per sub-section, smallest first: §5.4, §5.2, §5.3, §5.1.
3. §6 tolerances, §7 decisions log, §8 reviewer feedback log (empty
   stub), §9 future plan, §10 out of scope, §11 references — last PR.
4. After all sections merge: redirect-stub-then-delete protocol on the
   six absorbed source files (see §6.1).

Each §5.x sub-section follows [§14 Appendix B](#14-appendix-b--documentation-skeleton-for-each-surviving-page).
Each lands with its end-to-end fixture under
`PythonDataService/tests/fixtures/golden/<page-name>/`, anchored on a
real market date per [§7 D6](#7-decisions-log).

**Exit criterion:** `docs/architecture/options-research.md` is merged
end-to-end; every cited number has a passing test; the six absorbed
source files are deleted.

### Phase 6 — F5 fate executed — DONE (2026-04-29)

`Frontend/src/app/components/research-lab/options-math-docs/`
deleted. The `/research-lab` "Options Math" sub-section now renders
an inline link-out panel referencing `options-math-authorities.md`,
`options-bs-greeks-2026-04-24.md`, and `options-cross-section-overview.md`.
`OptionsMathDocsComponent` removed from `research-lab.component.ts`
imports. Build clean.



- Per [§7 D1](#7-decisions-log): delete `options-math-docs.component.ts`
  (and its sibling `.html`/`.scss`) outright. Replace the research-lab
  sub-section that hosted it with a link-out to the relevant `#anchor`
  in `options-research.md`.

**Exit criterion:** D1 implemented; component directory removed; no
broken in-app routes.

### Phase 7 — External review and feedback log

- Send `options-research.md` to an external quant LLM reviewer.
- Populate §8 (reviewer feedback log) in the truth doc with the response
  (accepted / deferred / declined per the iv-ownership convention).

**Exit criterion:** §8 of `options-research.md` has at least one
reviewer-feedback log entry.

### Phase 8 — Cleanup audit doc + UX design prompt delivery — DONE (2026-04-29)

[`docs/architecture/options-cleanup-2026-04-29.md`](options-cleanup-2026-04-29.md)
authored with the full per-phase ledger, deferred-work rationale,
duplication delta (baseline vs measured-after), and file-change
inventory. The UX design prompt
([`docs/architecture/options-ux-design-prompt.md`](options-ux-design-prompt.md))
is seeded with UX-Q1 through UX-Q4 and ready to paste into Claude
Design when R0b / R1 are unblocked.



- Write `docs/architecture/options-cleanup-2026-XX-XX.md` summarising
  Phases 0–7. Include the duplication delta from [§2.3](#23-headline-anchor--the-duplication-baseline)
  with measured-after numbers, and the full PR audit trail.
- Lock the `docs/architecture/options-ux-design-prompt.md` accumulator
  file: every Phase-3 migration PR is closed, every UX question shipped
  with a working default has its entry, and the file is ready to paste
  into Claude Design as a standalone prompt.
- Owner takes the prompt to Claude Design separately; that work is
  out of scope for this cleanup.

**Exit criterion:** the cleanup audit doc is the single artifact a future
reader needs to understand what changed and why; the UX design prompt is
self-contained and deliverable.

---

## 10. Out of scope

- The TradingView-style chain UI itself. `options-chain-research-plan.md`
  at the repo root is the (implemented) plan for that work; this plan does
  not propose changes to its visible behaviour.
- The IV pipeline. `iv-ownership-research.md` is its truth doc; this plan
  does not propose changes to it, except cross-linking from
  `options-research.md` where appropriate.
- The backtesting engine's internal options surface
  (`PythonDataService/app/engine/options/`). It is not reachable from any
  of the routes in [§4.1](#41-frontend-routes-that-touch-options); it is
  used only by the backtest engine. Any cleanup there is a separate
  effort.
- New features — adding pages, adding endpoints, adding chart types.
- Vendoring or upgrading reference repositories. The QuantLib version pin
  and the Polygon SDK version pin are out of scope.

---

## 11. Risks, anti-patterns, and rollback

### 11.1 Risks

- **The "unused" claims are wrong.** P1, G5, and the legacy
  `Frontend/src/app/utils/black-scholes.ts` may have callers I missed in
  greps (e.g., dynamic strings, generated code, scheduled jobs in C#).
  The verification protocol in [§8.1](#81-pre-deletion-verification-protocol)
  is the mitigation; the watch period is the second mitigation.
- **Refactor introduces silent regressions.** Pulling a state machine out of
  a component can change re-render timing in subtle ways
  (signal-effect ordering, RxJS subscription lifecycle). Mitigated by the
  parity tests in [§8.2](#82-parity-tests-for-every-extraction) plus a
  manual smoke pass on each surviving page after each extraction.
- **Documentation rigor exceeds available evidence.** If we don't have a
  fixture that proves a numerical claim, the truth doc cannot cite it.
  [§7 D2](#7-decisions-log) is the relief valve — a minimum-viable test
  backfill — but it caps the number of citations the doc can make.
- **Doc rot.** Six months from now `options-research.md` may drift from the code.
  Mitigated by the IV-ownership convention of "re-revise this single doc
  on subsequent reviews; do not spawn sibling docs" — same rule applies
  here.

### 11.2 Anti-patterns to reject (specific to this work)

- "Let's also redesign the chain page while we're refactoring." No.
  Behaviour-locked.
- "Let's reformat the existing markdown docs while we're at it." No.
  Reformatting unrelated files violates CLAUDE.md hard rules.
- "The Greek formatter is so simple, just inline it." Inlining is what got
  us into a 3-way duplication.
- "We don't need a parity test for `OccTickerFormat`, the parser is
  obvious." Every extraction lands with a parity test. No exceptions.
- Loosening the strict-float tolerance to make a parity test pass. See
  `numerical-rigor.md` § Tolerance rules.

### 11.3 Rollback

Each phase produces revertable commits. Phase 4 (orphan removal) and Phase
3 R8 (sovereignty migration) carry the most blast radius and ship as
single-purpose PRs so revert is a `git revert <sha>`.

---

## 12. References

- `docs/architecture/iv-ownership-research.md` — the rigor template this
  document inherits from.
- `docs/architecture/options-math-authorities.md` — the canonical authority
  index; this plan promotes it.
- `docs/architecture/options-vol-platform-tdd.md` — the 8-phase migration
  plan that R8 (sovereignty) executes Phase 1.2 of.
- `docs/options-cross-section-overview.md` — the math overview this plan
  proposes folding into the authorities doc.
- `docs/options-companion-format.md` — kept as a wire-format spec.
- `docs/references/options-bs-greeks-2026-04-24.md` — the BS formula
  reference card kept under `references/`.
- `docs/phase-1-2-deep-dive.md` — Phase 1.2 design; folds in once stable.
- `options-chain-research-plan.md` (repo root) — the *implemented* TradingView-
  style chain UI build plan. Distinct from this cleanup plan.
- `.claude/rules/numerical-rigor.md` — tolerance and golden-fixture rules
  this plan inherits.
- `CLAUDE.md` — the repo guiding philosophy this plan inherits.

---

## 13. Appendix A — File map of every options touchpoint

This appendix is the *authoritative* list of files this plan considers in
scope. Any file not on this list is out of scope. It exists so a reviewer
can verify nothing was forgotten.

### 13.1 Frontend

```
Frontend/src/app/components/
  options-chain-v2/                                                # MOSTLY DELETE per D9 (R0b)
    options-chain.component.ts                                     # delete
    options-chain.component.html                                   # delete
    options-chain.component.scss                                   # delete
    expiration-ribbon/expiration-ribbon.component.ts               # KEEP — already imported by /strategy-builder
  options-strategy-lab/                                            # DELETE per D8 (R0a)
    options-strategy-lab.component.ts                              # delete
    options-strategy-lab.component.html                            # delete
    options-strategy-lab.component.scss                            # delete
    payoff-chart/payoff-chart.component.ts                         # RELOCATE to shared/ before parent delete
  strategy-builder/                                                # KEEP (F2 — survivor per D8; absorbs D9 drill-down)
    strategy-builder.component.ts                                  # extend per D9
    strategy-builder.component.html                                # add drawer markup
    strategy-builder.component.scss
  options-history/                                                 # DELETE per D10 (R1)
    options-history.component.ts                                   # logic ports to past-chain.service.ts
    options-history.component.html                                 # rendering ports to past-chain-inspector/
    options-history.component.scss                                 # delete
    options-history.component.spec.ts                              # MIGRATES to past-chain-inspector.component.spec.ts
  pricing-lab/                                                     # KEEP (F4)
    pricing-lab.component.ts
    pricing-lab.component.html
    pricing-lab.component.scss
  research-lab/options-math-docs/                                  # DELETE per D1 — markdown only
    options-math-docs.component.ts                                 # delete
    options-math-docs.component.html                               # delete
    options-math-docs.component.scss                               # delete
  data-lab/                                                        # KEEP + extend (F6 — gains past-chain inspector per D10)
    data-lab.component.ts                                          # mount the sub-component per D10a outcome
    past-chain-inspector/                                          # NEW — created by D10 (R1)
      past-chain-inspector.component.ts                            # new — hosts the rendering ported from F3
      past-chain-inspector.component.html
      past-chain-inspector.component.scss
      past-chain-inspector.component.spec.ts                       # migrated from options-history/

Frontend/src/app/shared/
  payoff-chart/                                                    # NEW — relocated from options-strategy-lab/ (R0a)

Frontend/src/app/services/
  market-data.service.ts          # GraphQL query/mutation methods
  quantlib.service.ts             # QuantLib pricing wrapper
  past-chain.service.ts           # NEW per D10 (R1) — fetchPastChain(ticker, date, n, atmMethod)

Frontend/src/app/utils/
  black-scholes.ts                # legacy client-side BS — to delete in R8

Frontend/src/app/graphql/
  queries.ts                      # options-related GraphQL operations
  types.ts                        # generated/manual types
```

### 13.2 Python (FastAPI)

```
PythonDataService/app/routers/
  options.py                      # P1, P2 (REST: /options/contracts, /options/expirations)
  quantlib_options.py             # /quantlib/* (multi-engine pricing)

PythonDataService/app/services/
  strategy_engine.py              # analyze_strategy() — backs G3
  options_companion_service.py    # 30-day IV companion (research, not page math)
  bs_greeks.py                    # canonical BS / Greeks authority (R8 target)
  quantlib_pricer.py              # QuantLib adapter

PythonDataService/app/engine/options/
  chain_resolver.py               # backtest only (out of scope per §10)
  pricer.py                       # backtest only (out of scope per §10)

PythonDataService/app/research/options/
  contract_finder.py
  diagnostics.py
  iv_builder.py

PythonDataService/tests/
  research/options/test_contract_finder.py
  research/options/test_iv_builder.py
  research/options/test_diagnostics.py
  research/options/test_options_features.py
  services/test_options_companion_service.py
  research/test_options_runner.py
```

### 13.3 Backend (C# / Hot Chocolate)

```
Backend/GraphQL/
  Query.cs                        # G1, G2, G3, G4, G5 resolvers

Backend/Services/Interfaces/
  IPolygonService.cs

Backend/Services/Implementation/
  PolygonService.cs               # FetchOptionsExpirationsAsync, FetchOptionsChainSnapshotAsync, FetchOptionsContractsAsync

Backend.Tests/Unit/
  GraphQL/QueryTests.cs           # inspect for G5 usage per §8.1 protocol (D4 cleared)
  Services/PolygonServiceTests.cs
```

### 13.4 Docs (current)

```
docs/architecture/options-math-authorities.md
docs/architecture/options-vol-platform-tdd.md
docs/options-cross-section-overview.md
docs/options-companion-format.md
docs/references/options-bs-greeks-2026-04-24.md
docs/phase-1-2-deep-dive.md
options-chain-research-plan.md          # at repo root; implemented UI build plan
```

### 13.5 Docs (to be created — Phase 5 + Phase 8)

```
docs/architecture/options-research.md            # the single truth doc (Phase 5)
docs/architecture/options-cleanup-2026-XX-XX.md  # Phase 8 audit ledger
```

The truth doc is one file, not per-page. Per [§7 D3](#7-decisions-log).
The cleanup ledger is separate because it is a one-shot record of moves,
not an evergreen authority.

---

## 14. Appendix B — Documentation skeleton

Two skeletons. **B.1** is the top-level shape of `options-research.md`
(the whole single doc). **B.2** is the per-page sub-section that lives
inside §5.x of that doc.

### B.1 Top-level skeleton — `options-research.md`

```markdown
# Options — Implementation Truth Document

> Single source of truth for the options feature in learn-ai —
> the surviving options surfaces (`/strategy-builder`, `/pricing-lab`,
> the options sub-feature of `/data-lab`), the math
> they invoke, the data flow, and how we know it's correct.
> Modelled on `iv-ownership-research.md`. Re-revise this doc on
> subsequent reviews; do not spawn sibling docs.
>
> Last revised: YYYY-MM-DD.

## Table of contents
[generated]

## 1. Reviewer framing
[copy template from iv-ownership §1, adjust scope to options feature]

## 2. Executive overview
  ### 2.1 What the options feature does (overview of all surviving surfaces — strategy-builder, pricing-lab, data-lab options sub-feature)
  ### 2.2 What is owned vs delegated
  ### 2.3 Headline empirical anchor (one cross-page anchor, e.g. a worked
        end-to-end pricing on SPY 2024-12-20 plus a textbook BS check)

## 3. Hard constraints
  Platform constraints (Polygon Starter, int64 ms UTC, sovereignty)
  plus options-specific (live-only snapshots, FRED rate fetch, etc.)

## 4. Mathematical foundations
  ### 4.1 Black-Scholes price
    - Equation (LaTeX)
    - Canonical source (Hull §15.8, p. 332 9th ed.)
    - Implementation file:line (`PythonDataService/app/services/bs_greeks.py:NN`)
    - Tolerance level (strict float, atol=1e-9, rtol=0)
    - Worked numerical example (TEXTBOOK anchor per §7 D6)
  ### 4.2 Greeks (delta, gamma, theta, vega, rho)
  ### 4.3 IV solver (Newton → QuantLib → Brent fallback)
  ### 4.4 Probability of profit (POP) / numerical integration
  ### 4.5 Multi-leg payoff (intrinsic + BS)
  ### 4.6 Multi-engine pricing (binomial, finite-diff, Monte Carlo)

## 5. Production pipelines
  ### 5.1 /strategy-builder (chain view + payoff builder + drill-down) — see B.2 skeleton
  ### 5.2 /pricing-lab — see B.2 skeleton
  ### 5.3 /data-lab options sub-feature (companion config + past-chain inspector; live-vs-historical constraints box lives here per D10)
  ### 5.4 Companion data formats (absorbs options-companion-format.md)

## 6. Tolerances and validation
  | Assertion | Tolerance | Test file:line |
  |---|---|---|
  | (one row per cited number across all §4 and §5) | | |

## 7. Decisions log
  - YYYY-MM-DD — <decision> — <rationale> — <PR>
  (Absorbs decisions from this plan + phase decisions from `options-vol-platform-tdd.md`)

## 8. Reviewer feedback log
  | Date | Reviewer | Feedback | Status | Resolution |

## 9. Future plan / deferred items
  (Absorbs the unfinished phases of `options-vol-platform-tdd.md`)

## 10. Out of scope

## 11. References + PR audit trail

## 12. Appendix A — worked numerical examples
  (Anchored per §7 D6 — textbook for math, market date for E2E)

## 13. Appendix B — file map
  (Absorbs §13 of `options-routes-research.md`)
```

### B.2 Per-page sub-section skeleton (lives inside §5.x of B.1)

```markdown
### 5.X /<route-name>

**What the page does** (1 paragraph).

**Data flow** (numbered steps; every wire timestamp annotated `int64 ms UTC`):
  1. User selects ticker → `OptionsChainStateService` (R4) emits
  2. Service calls G1 `getOptionsExpirations(ticker)`
  3. → Backend C# resolver `Backend/GraphQL/Query.cs:NN`
  4. → Python `POST /options/expirations`
  5. → Polygon API
  6. Response sanitised, cast to `int64 ms UTC` at the ingestion boundary
  7. (page-specific compute step, e.g. for /pricing-lab: call G4
     `comparePricingModels`)
  8. UI renders the result; timestamps converted to ET only at the
     render layer, never persisted.

**Compute path** (which §4 formulas this page invokes):
  - e.g. /pricing-lab invokes §4.1, §4.2, §4.6
  - e.g. /strategy-builder invokes §4.1, §4.2, §4.4, §4.5

**End-to-end fixture** (per §7 D6 — market-date anchor):
  - Path: `PythonDataService/tests/fixtures/golden/<page-name>/`
  - Anchor date: SPY 2024-12-20 (default)
  - What's recorded: input snapshot, expected output, attribution
  - Test that loads it: `tests/.../test_<page>.py:NN`

**Cited tolerances** (one row per cited number; aggregate to top-level §6):
  | Assertion | Tolerance | Test file:line |
  |---|---|---|
  | <e.g., POP(SPY 591/595 BCS, 28DTE) within…> | atol=1e-6 | tests/services/test_strategy_engine.py:NN |

**Page-specific constraints** (only if applicable):
  - e.g. /options-history: snapshot endpoint is live-only on Polygon
    Starter, so this page reconstructs chains via per-contract aggregate
    scans. This is the explicit reason it cannot share the prelude with
    /options-chain, /strategy-builder, /pricing-lab.

**Decisions specific to this page** (cross-link to top-level §7):
  - PR-NN: <decision> (e.g. R4 extraction landed; this page now reads
    from `OptionsChainStateService`)
```

The skeletons are enforced by review, not by template tooling. The truth
doc is not merged if §4 (math), §6 (tolerances and validation), or any
§5.x's "End-to-end fixture" sub-section is missing.

---

*End of document.*
