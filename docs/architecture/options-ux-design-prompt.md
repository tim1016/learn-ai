# Options Feature — UX Design Prompt (accumulator)

> **Purpose.** This file accumulates UX-design questions raised during the
> options-routes cleanup ([docs/architecture/options-routes-research.md](options-routes-research.md))
> per [§7 D11](options-routes-research.md#7-decisions-log).
>
> **How to use it.** When the cleanup is complete (Phase 8), paste the
> contents below into Claude Design — or any design-capable LLM — and ask
> for a UX-improvement plan covering each entry. The owner picks which
> recommendations to action in a follow-up PR. That follow-up PR is *out
> of scope* for this cleanup; this file is its input.
>
> **How to extend it.** Every implementing PR that hits a UX choice it
> cannot resolve cleanly from existing patterns must append a new entry
> here in the same PR. Each entry follows the template in
> [§ Entry template](#entry-template) below. Do not delete entries even
> after the design pass — they're the audit trail of "what was decided
> at implementation time" vs "what was upgraded in design pass".
>
> **Last revised:** 2026-04-29. **All four UX questions answered by the
> Claude Design pass on 2026-04-29** (bundle hash `Ld_D7E4LcbEWqq4z2WPl0g`,
> `quant-trading-lab-design-system/project/options_ux_design/`). Locked
> picks recorded inline in each entry below.

---

## Project context (paste this first when prompting Claude Design)

`learn-ai` is a personal research platform for porting and validating
trading logic. The options feature spans four routes today:
`/options-chain` (chain viewer), `/options-strategy-lab` and
`/strategy-builder` (multi-leg payoff builders), `/options-history`
(past-date chain reconstruction), `/pricing-lab` (multi-engine pricing
comparison).

A cleanup is in flight that collapses these to:

- **`/strategy-builder`** — absorbs the chain-viewer role from
  `/options-chain` (including the per-contract historical price
  drill-down drawer) and remains the multi-leg payoff builder. Imports
  `ExpirationRibbonComponent`, `PayoffChartComponent` (relocated to
  `shared/`), `Drawer`, `CandlestickChartComponent`, `VolumeChartComponent`.
  Hosts: a strike chain table with full Greeks per row, an expiration
  ribbon, a leg-builder panel, a payoff/Greek-curve chart, what-if
  scenario toggles, strategy templates, a per-contract historical
  drawer, an optional QuantLib pricing toggle.
- **`/pricing-lab`** — single contract, multi-engine pricing
  comparison (analytic BS, binomial CRR/JR/LR, finite-diff, Monte
  Carlo). Sweep across a spot grid, render price + Greek curves +
  diff vs reference engine.
- **`/data-lab`** — extends with a past-chain inspector ported from
  `/options-history`. The inspector lives as a card on the
  options-companion config row (next to `optionsStrikesEachSide`,
  `optionsIncludeCalls`, etc.).
- **`/research-lab`** — its options-math-docs sub-section becomes a
  link-out to the markdown docs.

Stack: Angular 21 standalone components, signals, zoneless,
`ChangeDetectionStrategy.OnPush`, Tailwind + PrimeNG, dark theme
(`document.documentElement.classList.add('app-dark')`).

The owner values: information density (this is a research tool, not a
consumer app), keyboard accessibility, dark theme, scannability, and
single-page workflows over multi-step wizards.

---

## Open UX questions (the prompt body)

Each entry is self-contained: read it standalone, ignore the others if
needed.

### UX-Q1 — Drill-down trigger ambiguity in `/strategy-builder` — **ANSWERED 2026-04-29**

**LOCKED PICK: icon button per side.** A small drill-down icon
(`📈` for calls, `📉` for puts) sits *outside* the chain table — far-left
column for calls, far-right column for puts. Clicking the icon opens
the historical drawer for that contract. The rest of the row stays
available for L/S leg-add buttons (no overload).

**Rejected alternatives:**

- *Row body click* — too hidden; needs hover hints; low discoverability.
- *Hover overlay* — hostile to keyboard / touch; obscures data.

**Reference:** `quant-trading-lab-design-system/project/options_ux_design/recommendations.jsx`
and `chain.jsx` (`drill-icon` and `drill-icon-cell` styles).

---


**Context.** Today on `/options-chain`, clicking a chain cell opens a
drawer with the contract's 2-year historical price chart. On
`/strategy-builder`, clicking a chain cell adds that contract as a leg
in the current strategy. After the cleanup (per §7 D9 of the cleanup
research doc), `/options-chain` is deleted and its drill-down drawer is
absorbed into `/strategy-builder`. **Now the same chain cell needs to
serve two click intents.**

**What was done as a working default (R0b PR).**
TBD at implementation time. Pending defaults considered: separate
chart-icon button per cell that triggers the drawer (click on cell-body
adds leg; click on icon opens drawer); right-click context menu; hover
affordance.

**Visual neighbours.**
- Chain table cells show: Strike, IV, Vega/Theta/Gamma/Delta, Price,
  Bid/Ask, OI, Volume — per call/put side. Density is already high.
- Leg-builder panel sits adjacent (same page). Strategy templates
  drop legs into the same panel.
- Drawer (drill-down) is a slide-in side panel with a candlestick chart
  + volume chart for the selected contract.

**Specific UX questions for the design pass:**
1. What's the cleanest way to disambiguate "click for leg" vs "click
   for history" without forcing the user to learn a modifier key?
2. Does the drill-down deserve cell-level action (per strike, per
   call/put) or row-level action (per strike — drill into either side
   from one entry point)?
3. Should the historical drawer feel like a "preview" (lightweight,
   dismissable, ephemeral) or a "research panel" (substantial, with
   stats summary, dwellable)?

---

### UX-Q2 — Chain-table density under D9a (preserve full Greeks per row) — **ANSWERED 2026-04-29**

**LOCKED PICK: "Quick" density default with "Full Greeks" toggle.**

- **Default** ("quick"): per side shows L · S · Δ · Price · Vol (5
  columns). Centre: Strike · IV%. Total ~12 columns — fits at 1280px.
- **Toggle "Full Greeks"** ("greeks"): per side shows L · S · V · Θ ·
  Γ · Δ · Price (7 columns) — preserves the full-Greek display per
  row promised by D9a, but only on demand.
- **Sticky per-user** via `localStorage` so power users land in
  Greeks every time without re-toggling.
- **L/S = Long / Short** — small inline buttons in the table for
  one-click leg-add (preserves the current strategy-builder
  click-to-add-leg pattern).

**Rejected alternative:** Symmetric (TradingView-style) — visually
elegant but ~16 columns is cramped below 1440px. Reserved as opt-in
for wide monitors only; not part of MVP.

**Reference:** `chain.jsx` `colsFor()` for the column sets; the "quick"
and "greeks" modes are exactly the two columns this implementation
needs.

---


**Context.** Per §7 D9a of the cleanup research doc, strategy-builder's
chain table extends to show Vega/Theta/Gamma/Delta on every row — not
just Delta. This matches the F1 (`/options-chain`) reader's output so
the cutover is feature-complete. The table widens significantly.
Strategy-builder co-exists on the same page with the leg builder, the
payoff/Greek chart, the what-if scenario toggles, and the QuantLib
pricing toggle.

**What was done as a working default (R0b PR).**
TBD. Pending defaults considered: tabular layout matching F1's
TradingView-style symmetric chain (calls left, puts right, strike +
IV centre); collapsed/expanded toggle to hide secondary Greeks; sticky
header with abbreviated column labels.

**Visual neighbours.**
- Today F1 (`/options-chain`) renders chain in TradingView-style
  symmetric layout: calls right-to-left, strike + IV centre, puts
  left-to-right. ~10 strikes visible at once around ATM.
- F2 (`/strategy-builder`) currently renders a chain in `BuilderChainRow`
  shape but with fewer Greeks visible. The page also has the leg
  builder panel (right-hand or below), the payoff chart (typically
  large), and the strategy template selector.

**Specific UX questions for the design pass:**
1. Where does the chain table live on the page now that strategy-builder
   has both chain + leg builder + payoff chart? (Top, side, in a
   collapsible drawer of its own?)
2. What's the visual hierarchy between chain (read), legs (write),
   chart (output)?
3. How wide should the chain be? At 4 Greeks × 2 sides + 4 supporting
   columns, can we keep ~10 strikes visible without horizontal scroll
   on a typical research-screen width?
4. Is there a "compact mode" for users who already know what strikes
   they want to add and just need leg entry?

---

### UX-Q3 — Past-chain inspector card visual on `/data-lab` — **ANSWERED 2026-04-29**

**LOCKED PICK: inline collapsed card → progress-bar loading →
expanded chain → modal drill-down.**

Three states:

1. **Collapsed.** A compact card on the options-companion config row
   showing a "Preview chain on this date" CTA, the current ticker +
   date, and an estimated-bar count. No data fetched yet.
2. **Loading.** Inline progress bar (e.g. "Scanning contracts…
   23 of 50 strikes") plus a skeleton chain hinting at the output
   shape. Surfaces the expensive op concretely.
3. **Expanded.** The interactive chain (calls/puts split, ATM
   marker, change-from-prior-close colouring) renders inline below
   the config card so the operator can keep configuring while
   inspecting.

**Per-contract drill-down** — opens in a **modal** (not a nested
drawer). Focused, dismissable, reusable from anywhere in the app.

**"Show scan details" link** — off by default. Reveals the audit
table of which strikes had no data, for debugging without cluttering
the default view.

**Rejected alternatives:**

- *Open in new page* — loses config context; hurts iterate-and-tune
  flow.
- *Always-expanded* — fetches eagerly; expensive when user is just
  configuring.

**Reference:** `quant-trading-lab-design-system/project/options_ux_design/artboards-q3.jsx`
for the collapsed/loading/expanded states + modal drill-down sequence.

---


**Context.** Per §7 D10 + D10a, the past-chain inspector ported from
`/options-history` lives as a card on the options-companion config row
in `/data-lab`. The default is "expandable inline" — clicking the card
expands an inline panel below it with the inspector contents (calls/puts
split, ATM marker, change-from-prior-close, scan-results audit table,
per-contract drill-down). The user can keep the config visible while
inspecting.

**Visual neighbours.**
- The options-companion config row currently holds:
  `optionsCompanionEnabled` toggle, `optionsStrikesEachSide` ±N control,
  `optionsIncludeCalls` and `optionsIncludePuts` checkboxes,
  `optionsDteDistance` numeric input, plus an estimated-contract-count
  readout with a severity colour.
- The wider `/data-lab` page has: ticker-range picker, bar-timeframe
  dropdown, indicator-config cards, the chart preview below.

**Specific UX questions for the design pass:**
1. What does the card look like in its **collapsed state** before the
   user has clicked "preview"? (Disabled-looking? Just a label and a
   button? An info card showing "what you'll see when expanded"?)
2. What does the **loading state** look like (the past-chain fetch is
   batched 30-at-a-time and can take 5–15 seconds for ~50 strikes)?
   Per-batch progress bar? Skeleton chain rows? Spinner only?
3. The legacy F3 component shows a **scan-results audit table** —
   "we tried these 50 strikes, 23 had data, here's the cut" — is that
   surfaced in the card, hidden behind a "Show scan details" link, or
   dropped entirely?
4. Where does the per-contract drill-down chart open from inside the
   inspector card — same expanded panel, separate drawer, modal?

---

### UX-Q4 — `/strategy-builder` page layout after absorbing two more roles — **ANSWERED 2026-04-29**

**LOCKED PICK: two-column 60/40 — chain (60%) on left, build + payoff
(40%) stacked on right; templates as horizontal pills above the
chain; scenario toggles inline beneath the chart.**

- **Left column (60%)** — chain table is the visual anchor;
  decisions originate there.
- **Right column (40%, stacked)** — top: legs panel (always
  visible — no drawer toggling, no scroll-to-find). Bottom:
  payoff/Greek-curve chart.
- **Strategy templates** as horizontal pills above the chain (Bull
  Call Spread, Iron Condor, Straddle, …). Persistent and
  discoverable; quick-load is one click.
- **Scenario toggles** (`+1σ`, `IV ±5`, `T −7`) sit inline beneath
  the payoff chart for fast iteration.

**Rejected alternatives:**

- *Drawer (today's pattern)* — hides build state; users forget what
  legs they've added when toggled off.
- *Three-zone stacked* — density-elegant but chain shrinks to ~40%
  vertical, costly for chain-led decisions.

**Reference:** `quant-trading-lab-design-system/project/options_ux_design/artboards-q4.jsx`
"Variant B — Two-column 60/40".

---


**Context.** Once D8, D9, D9a are executed, strategy-builder is the only
page that reads chains and the only page that builds strategies. It
hosts: ticker input + expiration ribbon at top, chain table (now wide,
per UX-Q2), leg-builder panel, payoff/Greek-curve chart, what-if
toggles, strategy templates, QuantLib engine toggle, per-contract
drill-down drawer. That's a lot.

**What was done as a working default (R0b PR).**
TBD. Pending defaults considered: vertical stack (chain → legs → chart)
in priority order; two-column layout (chain left, legs+chart right);
tabs (chain | legs | chart).

**Visual neighbours / inspiration.**
- TradingView's options pages have a chain + payoff chart split.
- The existing `/options-chain` page is single-column with a side drawer.
- The existing `/options-strategy-lab` (deleted) had a more complex
  multi-pane layout.

**Specific UX questions for the design pass:**
1. What's the canonical screen real-estate split for strategy-builder
   given a 1440-wide research workstation?
2. Where do the strategy templates live so they're discoverable but
   not in the way of an experienced user who knows what they want?
3. What's the keyboard-only flow from "type ticker" → "pick expiration"
   → "click two strikes" → "see payoff" → "tweak what-if"?

---

## Entry template

```markdown
### UX-Qn — <one-line title>

**Context.** What the user is trying to do; why this UX question exists;
which decision (D-id) or recommendation (R-id) led here.

**What was done as a working default (which PR).**
The exact behaviour shipped — so the design pass knows what's already
there and what to compare against.

**Visual neighbours.**
What sits next to this on the page. Other components, other patterns
already in the codebase.

**Specific UX questions for the design pass:**
1. …
2. …
```

---

## Delivery (Phase 8)

This file is delivered with the cleanup audit doc as the input to a
Claude Design (or equivalent) pass. The owner's ask to that design pass
is roughly:

> *Read the project context above and each open UX question below. For
> each question, propose a recommendation with rationale, a sketch of
> the resulting layout (text-mode is fine — `<box>` / `<row>` /
> `<panel>` notation), and the trade-off it accepts. Mark each
> recommendation as: must-have, nice-to-have, or take-it-or-leave-it.
> Don't propose redesigns of pages not listed here.*

The owner reviews, picks the must-haves, opens follow-up PRs.
