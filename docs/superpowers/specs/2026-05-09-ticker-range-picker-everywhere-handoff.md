# Adopt the Engine-Lab `ticker-range-picker` UX everywhere — session handoff

**Status:** ready for brainstorm — do NOT skip the brainstorming skill, this is a non-trivial design with real tensions
**Date:** 2026-05-09
**Author of handoff:** Claude (Opus 4.7), at Tim's request after PR #198 shipped
**Next session:** start by reading this doc end-to-end, then invoke `superpowers:brainstorming`

---

## What Tim asked for

> "Make the engine lab UI/UX which has the **Instrument**, **Time window**, and **Sampling** [sections] — this exact UI/UX gets repeated everywhere we are fetching this data. This UI/UX is much better. Plan that and give me a session handoff."

The "Instrument / Time window / Sampling" three-section component he's pointing at is the **already-existing** `<app-ticker-range-picker>` at `Frontend/src/app/shared/ticker-range-picker/`. Engine Lab (`lean-engine`) and Data Lab (`data-lab`) are the only two pages using it today. Tim wants every other page that ingests Polygon data to adopt the same component for consistency.

This is **NOT** "build a new component." The component exists, is polished, and has its own docstring claiming to be "a single source of truth for both Engine Lab and Data Lab." The work is **migration + scope decisions** for the consumers that haven't adopted it yet — and reconciling that decision with PR #198 (`PolygonDateRangeComponent`), which shipped a *different* shared component at a smaller scope two hours earlier.

---

## Critical context from PR #198

**Two hours before this handoff, this branch shipped:** `feat/polygon-date-range-shared-component` → PR https://github.com/tim1016/learn-ai/pull/198

That PR introduced `PolygonDateRangeComponent` at `Frontend/src/app/shared/polygon-date-range/` and migrated **six** research-lab forms to it: `feature-runner`, `signal-runner`, `batch-runner`, `indicator-reliability`, `spec-strategy-runner`, `strategy-preflight`. It also lifted `parseYmd`/`formatYmd` out of `data-lab.component.ts` into `utils/date-validation.ts`.

`PolygonDateRangeComponent` is **date-range-only** (two `p-datepicker`s + advisory). It deliberately does NOT include ticker selection or sampling/resolution. That decision was made because the brainstorm explicitly said "no presets, no cache strip, no ticker selection — just the two date inputs with Polygon-aware constraints." Tim approved option C in that brainstorm.

**This new effort partially supersedes PR #198.** The brainstorm here must explicitly decide what happens to the just-shipped component:
- **Option A — Keep both, complementary roles.** `ticker-range-picker` for forms where ticker + dates + sampling are the primary pickable triple. `PolygonDateRangeComponent` for forms where the ticker concept lives elsewhere (e.g. strategy spec) and the form just needs date constraints.
- **Option B — Deprecate `PolygonDateRangeComponent`.** Roll it out everywhere with `ticker-range-picker`. Possibly delete the date-range component or keep it only behind a `hideTicker`/`hideResolution` config. **Risk:** undoes PR #198 partially.
- **Option C — Two-tier: `ticker-range-picker` is the canonical, `PolygonDateRangeComponent` is the "narrow" sibling.** Document when to use each. Smallest churn but highest cognitive load.

This decision dominates the brainstorm. Don't let it ride.

---

## Bootstrap reading (do this first, in order)

1. **`Frontend/src/app/shared/ticker-range-picker/ticker-range-picker.component.html`** — the full visual treatment (Instrument / Time window / Sampling cards, presets, advisories, availability strip, smart legend). 356 lines.
2. **`Frontend/src/app/shared/ticker-range-picker/ticker-range-picker.types.ts`** — the `TickerRange` payload, `TickerOption`, `AvailabilityCell`, `Advisory`, and the `computeAdvisories` smart-suggestion engine. 254 lines. Read in full — the type system is the API contract.
3. **`Frontend/src/app/shared/ticker-range-picker/ticker-range-picker.component.ts`** — TS-side wiring. Skim for `@Input` / `@Output` / `model` shape.
4. **`Frontend/src/app/components/lean-engine/lean-engine.component.html`** lines 71–78 — reference integration:
   ```html
   <app-ticker-range-picker
     [(value)]="rangeState"
     [tickerPool]="tickerPool"
     [recent]="recentTickers"
     [availability]="pickerAvailability()"
     [availableResolutions]="['minute', 'daily']"
     (advisoryAction)="onPickerAdvisoryAction($event)"
   />
   ```
5. **`Frontend/src/app/components/data-lab/data-lab.component.html`** lines 871-872 — second reference. Same shape.
6. **`docs/superpowers/specs/2026-05-09-polygon-date-range-design.md`** — the PR #198 spec, so you understand what just shipped and why.
7. **`docs/superpowers/plans/2026-05-09-polygon-date-range.md`** — the PR #198 plan, for execution-style reference.

After reading: you'll have the picker's full API surface in your head. The brainstorm questions below assume that.

---

## Consumer inventory (post-PR #198 state)

The "fit" column flags how naturally each consumer maps onto `ticker-range-picker`'s `(symbol, from, to, resolution, session?)` payload.

| # | Consumer | Today (after PR #198) | Symbol shape | Sampling shape | Fit |
|---|---|---|---|---|---|
| 1 | `data-lab` | `<app-ticker-range-picker>` ✓ already adopted | single symbol via picker | minute/hour/daily + multiplier (separate `timespan` signal) | ✅ adopted |
| 2 | `lean-engine` (Engine Lab) | `<app-ticker-range-picker>` ✓ already adopted, `availableResolutions=['minute','daily']` | single | minute/daily | ✅ adopted |
| 3 | `feature-runner` | `<app-polygon-date-range>` (new) + separate `pInputText` ticker + `p-select` timespan + `pInputText` multiplier | single, separate input | timespan + multiplier (multiplier not in picker's `Resolution` type) | ⚠️ multiplier mismatch |
| 4 | `signal-runner` | same as #3 | single | same multiplier mismatch | ⚠️ multiplier mismatch |
| 5 | `batch-runner` (cross-sectional) | `<app-polygon-date-range>` + `selectedTickers: signal<string[]>` (chip-array universe selector) | **multi**-symbol | timespan separate | ❌ fundamentally different |
| 6 | `indicator-reliability` | `<app-polygon-date-range>` + separate ticker + indicator selectors | single | no sampling — uses indicator timeframe | ✅ with `hideResolution` |
| 7 | `spec-strategy-runner` | `<app-polygon-date-range>` + ticker is **inside the strategy spec** (not a top-level field) + initial cash + fill mode | symbol owned by spec, not form | resolution.period_minutes is in spec | ⚠️ ticker-in-spec |
| 8 | `strategy-preflight` | `<app-polygon-date-range>` + symbol input + timeframe (`5m`/`15m`/`1h`) + indicators | single | timeframe (not the picker's `minute|hour|daily`) | ⚠️ timeframe mismatch |
| 9 | `ticker-explorer` | single `type="date"` + raw ticker input | single | n/a (single date, not a range) | ⚠️ single date |
| 10 | `indicator-report` | template-driven `[(ngModel)]="fromDate"` | n/a | n/a | ❌ template-driven; deferred from PR #198 |

**Key takeaway:** Only `indicator-reliability` (and arguably `strategy-preflight` if timeframe is reconciled) is a clean drop-in for the picker. Every other research-lab form has a real friction point — multiplier, multi-ticker, ticker-in-spec, single date, timeframe-not-resolution. The brainstorm must pick which frictions to absorb in the picker, which to leave outside, and which consumers to skip altogether.

---

## Design tensions to resolve (in order)

These are the questions to anchor the brainstorm. Don't skip any.

### 1. Relationship between `ticker-range-picker` and `PolygonDateRangeComponent`

Three options listed above (A keep both / B deprecate / C two-tier). My weakly-held lean is **A**: keep both, document them as complementary. Reasons: (a) `PolygonDateRangeComponent` is genuinely simpler for forms where ticker is owned elsewhere (`spec-strategy-runner`); (b) deprecating it after a 14-commit PR landed two hours ago is churn; (c) two-tier is clarifying when documented well.

But this is really for the user to decide. Surface it explicitly.

### 2. Multiplier handling

Several research-lab forms (`feature-runner`, `signal-runner`, `batch-runner`) send `{ timespan, multiplier }` to the Python service for non-1× bar resolutions (e.g. `5m`, `15m`). The picker's `Resolution = "minute" | "hour" | "daily"` doesn't have multiplier baked in. Options:

- **A** — Extend the picker's `Resolution` to include multiplier (breaking change to its existing consumers, including data-lab and lean-engine).
- **B** — Add an optional `multiplier` to `TickerRange` and a multiplier dropdown to the Sampling section (visible only when consumer opts in).
- **C** — Leave multiplier outside the picker; consumer renders a separate `<p-select>` adjacent. Status quo for some, breaks visual consistency for others.

Lean: **B** (additive). Picker stays backward-compatible; new consumers opt in.

### 3. Multi-ticker (batch-runner)

The picker is single-symbol. Batch-runner needs a universe (chip array). Options:

- **A** — Build a sibling `<app-multi-ticker-range-picker>` that mirrors the picker's three-section layout but with a multi-select Instrument section.
- **B** — Skip batch-runner for this initiative. Document that multi-ticker UX is a separate problem.
- **C** — Add a `multi: boolean` config to the picker. Conditional rendering of the Instrument section.

Lean: **A** (sibling). The Instrument section's UX for multi-select is genuinely different (chip array vs combobox); cramming both into one component muddies the API.

### 4. Single-date use cases (`ticker-explorer`)

The picker is range-only. Consumers that want a single date have two reasonable paths:

- **A** — Skip them. They're outside scope.
- **B** — Build a `<app-ticker-date-picker>` sibling that mirrors the visual but takes a single `Date`.

Lean: **A** for v1. Surface this explicitly so the user can override.

### 5. Symbol owned by parent (`spec-strategy-runner`)

This form has the ticker inside the strategy spec object (`spec.symbol` or similar), not as a sibling form input. Picker assumes ticker is a top-level form field. Options:

- **A** — Refactor spec-strategy-runner to lift symbol out of the spec (large change).
- **B** — Drive the picker's symbol from `computed(() => this.spec().symbol)` and on change, mutate the spec. Possible but awkward (picker doesn't know it's writing into a nested object).
- **C** — Don't migrate this form. Keep it on `PolygonDateRangeComponent`.

Lean: **C**. Reinforces option-A on tension #1.

### 6. Timeframe vs Resolution (`strategy-preflight`)

`strategy-preflight` has `timeframe: '5m' | '15m' | '1h'`. Picker has `resolution: minute | hour | daily`. Reconciliation needs the multiplier work from tension #2. If multiplier is added, `'5m'` becomes `{ resolution: 'minute', multiplier: 5 }` — clean.

### 7. Migration order

Likely order, low-risk first:
1. `indicator-reliability` (cleanest fit, no multiplier)
2. `strategy-preflight` (depends on #2 multiplier resolution)
3. `feature-runner`, `signal-runner` (depend on #2)
4. `batch-runner` (depends on #3 sibling component decision)
5. `spec-strategy-runner`, `ticker-explorer` — likely SKIP per leans on #4 and #5

The implementation plan should commit to an order and rationalize it.

---

## What NOT to do (lessons from PR #198)

- **Don't skip brainstorming.** PR #198 went through it and the result was a tighter spec. The user explicitly says "grill me with questions." Honor that.
- **Don't roll back PR #198 commits without an explicit user decision.** That work is shipped and helped close a real bug. If tension #1 lands on Option B (deprecate), it's a separate PR with its own review.
- **Don't migrate consumers in a single mega-PR.** PR #198 was 14 logical commits, each independently revertable. Match that cadence.
- **Don't add to picker without keeping data-lab + lean-engine working.** Both consumers exist and have real users. Backward-compat must be non-negotiable.
- **Don't forget the `indicator-report` is template-driven**. It needs a separate signal-refactor PR before it can adopt anything. Document; don't fold in.
- **Don't change the `TickerRange` payload shape without coordinating .NET.** The shape is sent to the Python service — verify each migration target's network-tier serializer still produces what the backend expects.

## What to do at session start

1. Read this doc.
2. Read the bootstrap files §"Bootstrap reading" in order.
3. Verify PR #198 is merged (run `gh pr view 198 --json state,mergedAt`). If not merged, the date-range component is not yet on master — adjust the relationship discussion accordingly. The user's autonomous-merge memory says PR-monitor handles merges, so it's likely merged by the time you read this.
4. Pull master to get the merged state: `git checkout master && git pull`.
5. Branch off: `git checkout -b feat/ticker-range-picker-everywhere` (or per the user's preferred name).
6. Invoke `superpowers:brainstorming`. Anchor on the seven design tensions in order.
7. Once brainstorm completes and the spec is written, invoke `superpowers:writing-plans`, then `superpowers:executing-plans` for inline build (or `subagent-driven-development` if subagents are available).

## Skills + workflow notes

- **`superpowers:brainstorming` is non-negotiable** for this. The seven design tensions are real; "just doing it" will produce a worse outcome than 5 minutes of grilling the user with the questions above.
- **The user works fast.** Single-letter answers ("A", "C") are normal. Don't wait for paragraphs.
- **The user has a memory entry**: never commit to master, always branch + push + PR. Follow.
- **Memory entry**: PR monitor merges autonomously; never ask the user to merge. After opening the PR, stop — don't poll.
- **Memory entry**: "consistency of the data ingestion from polygon and replicating the shape of ingested data into our UI in the beautiful way is what we want." That's the north star — quote it back when scope creep tempts you.

## Files / folders this effort will touch

**Likely modified:**
- `Frontend/src/app/shared/ticker-range-picker/*` — if tensions #2 (multiplier) or #3 (multi) land as additive changes
- `Frontend/src/app/shared/polygon-date-range/*` — if tension #1 lands on B/C (deprecate or two-tier)
- 4–6 of the consumers listed in the inventory
- The two existing consumers (`data-lab`, `lean-engine`) only if backward-compat needs explicit verification

**Likely created:**
- A sibling `multi-ticker-range-picker` if tension #3 lands on A
- Tests for any new picker config flags

**Likely untouched:**
- Backend resolvers / Python endpoints — payload shapes are unchanged unless the brainstorm explicitly decides otherwise
- `data-lab` (already adopted)
- `lean-engine` (already adopted)
- `indicator-report` (deferred — separate signal-refactor PR)

## Questions you'll likely want to ask the user first

Listed in the order the brainstorm should pose them. Don't dump all at once — one per turn.

1. **Tension #1 first.** "Are we keeping `PolygonDateRangeComponent`, deprecating it, or running them as a two-tier system?" Frame with the three options and your lean.
2. **Tension #3 next.** "How should batch-runner's ticker universe fit?" — the multi-ticker decision is structural and affects the migration list.
3. **Tension #2.** Multiplier — additive on the picker, separate, or breaking change to the existing payload?
4. **Tension #5.** What about `spec-strategy-runner` where ticker is inside the spec? Skip, or refactor?
5. **Tension #4.** What about single-date cases (`ticker-explorer`)? Skip or extend?
6. **Migration order** confirmation.
7. **Scope of the brainstorm output** — one big PR, or one PR per consumer (matching PR #198's pattern)?

Each answer narrows scope. The brainstorm should aim to fit one PR's worth of work — if the answers point to too much, decompose into sub-projects per `superpowers:brainstorming` guidance.

---

## TL;DR for the next session

> The component exists. Six pages need to adopt it. Five of the six have non-trivial frictions (multiplier, multi-ticker, ticker-in-spec, etc.). PR #198 just shipped a smaller alternative (`PolygonDateRangeComponent`) two hours ago — the relationship between the two needs an explicit decision before any code is written. Read the seven design tensions, brainstorm them with the user, write a spec + plan, then execute.

Don't try to skip ahead. The shape of the answer here is genuinely undecided.
