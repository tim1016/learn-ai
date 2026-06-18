# Broker Instances Cockpit — Visual Design

Date: 2026-06-17
Author: Output of the `frontend-design` skill, run on the IA locked in
PR #580 (`docs(broker-instances): operator-surface IA revision`).
Mockup: [`broker-instances-cockpit-2026-06-17.html`](./broker-instances-cockpit-2026-06-17.html) — open in a browser; the top-right
`DEMO STATE` toggle cycles through STEADY / CONFIGURE / BLOCKED /
TRIAGE.

## Direction

**Terminal Cockpit.** The existing token surface in
`Frontend/src/app/styles/_tokens.scss` (TradingView dark) is already a
financial-terminal language: deep slate canvas, TV blue accent, bull /
bear / warn semantics, JetBrains Mono available, tight density. The
right move on top of that is to *commit* to the terminal aesthetic for
the cockpit-layer affordances — not invent a competing language.

Three intentional moves make the page distinctive without drifting into
kitsch:

1. **LED-segment status pills.** JetBrains Mono, uppercase, sharp 2-3px
   corners. No rounded SaaS-pills. Tabular-mono digits. Pills read like
   exchange ticker tape — a vocabulary that fits operator muscle memory
   from Bloomberg / TWS / TradingView, not from Material Design.
2. **Verdict border as the page-level peripheral-vision signal.** The
   Can-It-Trade card's border thickens from 1px → 2px on attention
   states *and* gains a low-opacity outer glow
   (`box-shadow: 0 0 32px -6px var(--verdict-soft)`). An operator
   walking back to their desk sees a red glow in their peripheral vision
   and knows the bot needs attention before they've read a word.
3. **Keycap action buttons** in the banner toolbar. The Resume / Pause /
   Flatten-and-pause / kebab buttons render as 32×34px keycaps with an
   inset hairline on top, an inset shadow at the bottom, and a 1px outer
   bottom shadow. The result reads as physical depth (a hardware
   console), not glass (a CRM). Pressing translates the cap down 1px.

What we are **avoiding**: rounded-everything, Inter / Roboto, glass
panels, soft gradients on light backgrounds, fade-in skeleton loaders,
pulsing dots, scanline kitsch, purple-on-anything. What we are keeping
**calm** is almost everything else — the cockpit is still in steady
state. Motion is reserved for verdict-border transitions (180ms) and
card expand/collapse (200ms). No looping animation anywhere.

## Layout sketches (4 dynamic states)

State is driven by `body[data-state]` in the mockup; in Angular it is
driven by the server-authored verdicts per the page-wide collapse rule
(CONTEXT.md, "Page-wide collapse rule").

### STEADY — bot is running, can-trade verdict READY, no attention

```
┌─────────────────────────────────────────────────────────────────────────┐
│ BROKER · INSTANCES / ORB-15-PAPER  ● IBKR · CONNECTED  PAPER · DU1234   │
├─────────────────────────────────────────────────────────────────────────┤
│ ORB-15-paper                                              ╔══╗ ╔══════╗ │
│ 3f9c2e8b1d                                                ║▰▰║ ║ ⋯    ║ │
│                ● RUNNING  INTENT · RUNNING                ╚══╝ ╚══════╝ │
│                | SAFETY · PAPER-ONLY  ✓ LAST RUN CLEAN  Flatten & pause │
│ ──────────────────────────────────────────────────────────────────────  │
│ CONFIGURATION  SPY · ORB-15 · MARKET · 5/day · 1.0%       REDEPLOY  ▾  │
├─────────────────────────────────────────────────────────────────────────┤
│ CURRENT RISK   FLAT · 0 positions · 0 pending · cap 0/5 · $0 @ risk  ▾ │
├─────────────────────────────────────────────────────────────────────────┤
│ CAN IT TRADE   ● READY · 7 of 7 checks pass                          ▾ │  ← green border
├─────────────────────────────────────────────────────────────────────────┤
│ ACTIVITY  DIAGNOSTICS                                                   │
│ ┌─────────── candle chart with entry/exit markers ─────────────┐        │
│ │                                                              │        │
│ └──────────────────────────────────────────────────────────────┘        │
│ LONG · sig_a3f9_0732 · 601.34 · stop 599.10 · target 605.80            │
│ Trades table (mono, hairline borders)                                   │
└─────────────────────────────────────────────────────────────────────────┘
                                            ┌──────────────────┐
                                            │ ✓ CHECKLIST      │   ← phosphor top border
                                            └──────────────────┘
```

All cards collapsed to one-line summaries. Verdict-bordered Can-It-Trade
is green-quiet. Pre-Trade Checklist FAB is minimized, no badge.

### CONFIGURE — `start_defaults` exists but readiness has a config-shaped failing gate (definition iii)

```
┌─────────────────────────────────────────────────────────────────────────┐
│ ORB-15-paper                                              ╔══╗ ╔══════╗ │
│ 3f9c2e8b1d  ● RUNNING  INTENT · RUNNING  PAPER-ONLY  ✓ CLEAN           │
│ ─────────────────────────────────────────────────────────────────────── │
│ CONFIGURATION   SPY · ORB-15 · MARKET · 5/day · 1.0%                    │
│   [@ RISK · FLAT · 0 pending · $0]   ← pinned risk-chip when expanded   │
│                                                            REDEPLOY  ▴  │
│ ┌───────────────────────────────────────────────────────────────────┐   │
│ │ STRATEGY KEY  spy_opening_range_breakout_15m  [v3 spec]           │   │
│ │ ORDER MODE    MARKET on signal · entry on next bar open           │   │
│ │ DAILY CAP     5 orders/day  [used 0 today]                        │   │
│ │ SIZING SUMMARY  SetHoldings(1.0%) · audit-copy v3  [SHA verified] │   │
│ │ ▸ ADVANCED                                                        │   │
│ │ ▸ SIZING DETAIL                                                   │   │
│ │ PER-TRADE SIZING AUDIT  [12 rows · all proven]                    │   │
│ │   ╭────────────────────────────────────────────╮                  │   │
│ │   │ TS · INTENT · SYM · SIDE · QTY · RULE · VERDICT │             │   │
│ │   ╰────────────────────────────────────────────╯                  │   │
│ └───────────────────────────────────────────────────────────────────┘   │
│ CURRENT RISK  FLAT · 0 positions · …                                  ▾ │
│ CAN IT TRADE  ● READY · 7 of 7 checks pass                            ▾ │
│ ...                                                                     │
└─────────────────────────────────────────────────────────────────────────┘
                                                ┌──────────────────┐
                                                │ ✓ CHECKLIST [1]  │  amber badge
                                                └──────────────────┘
```

Configuration card expanded. Risk-chip pins in its header (the operator
configuring should never be blind to held risk). FAB shows an amber
soft-fail badge if any soft gate is failing — operator-triggered only,
no auto-pop.

### BLOCKED — `can_trade` verdict is BLOCKED (hard gate failing)

```
┌─────────────────────────────────────────────────────────────────────────┐
│ ORB-15-paper                                              ╔══╗ ╔══════╗ │
│ 3f9c2e8b1d  ● PAUSED  INTENT · PAUSED  ⚠ UNSAFE  ✕ LAST RUN HALTED      │
│ ▰▰▰▰▰▰▰▰▰▰▰  ← red banner-attention strip (4px gradient)               │
│ ─────────────────────────────────────────────────────────────────────── │
│ CONFIGURATION  …                                                     ▾  │
│ CURRENT RISK   ● LONG +40 SPY · 1 position · …                        ▴ │  (positions held → expanded)
│   SYM · POSITION · QTY · AVG PX · UNREALIZED PNL                        │
│   SPY · opened 10:32 ET · +40 · 601.34 · +$148.20                       │
├═════════════════════════════════════════════════════════════════════════┤  ← 2px red border + outer glow
│ CAN IT TRADE   ● BLOCKED · 2 fail · 5 pass                            ▴ │
│ ┌───────────────────────────────────────────────────────────────────┐   │
│ │ ✕ FAIL · HARD  Broker safety verdict                       [FIX]  │   │
│ │   Verdict is unsafe. Failing gate: connected_account_prefix —     │   │
│ │   connected account is non-DU (live identity); paper-only         │   │
│ │   requires DU prefix.   broker_safety_verdict                     │   │
│ │ ✕ FAIL · HARD  Submit-intent ledger reconciliation         [FIX]  │   │
│ │ ✓ PASS · Strategy spec hash matches binding                       │   │
│ │ ✓ PASS · Daily order cap available                                │   │
│ └───────────────────────────────────────────────────────────────────┘   │
│ ...                                                                     │
└─────────────────────────────────────────────────────────────────────────┘
                                                ┌──────────────────┐
                                                │ ✓ CHECKLIST [3]  │  ← red badge
                                                └──────────────────┘
```

The page's loudest moment. The Can-It-Trade card border is 2px red with
a 32px outer glow (low-opacity red bleed). The banner gets a red 4px
attention strip below it. Resume keycap is disabled with the
`DISABLED_REASON_COPY` tooltip (hover shows operator-language reason).
Pause keycap is *also* disabled here (already paused). Flatten-and-pause
remains enabled (operator can still flatten the held position). FAB
badge is red.

### TRIAGE — operator switched to Diagnostics tab to investigate

```
┌─────────────────────────────────────────────────────────────────────────┐
│ ORB-15-paper                                              ╔══╗ ╔══════╗ │
│ 3f9c2e8b1d  ● PAUSED  INTENT · PAUSED  ⚠ DEGRADED  ✓ LAST RUN CLEAN     │
│ ▰▰▰▰▰▰────  ← amber banner-attention strip                              │
│ ─────────────────────────────────────────────────────────────────────── │
│ CONFIGURATION  ...                                                   ▾  │
│ CURRENT RISK   ● LONG +40 SPY · 1 position · ...                      ▴ │
│ CAN IT TRADE   ● DEGRADED · 1 soft check fails                       ▾  │  ← amber border
├─────────────────────────────────────────────────────────────────────────┤
│ ACTIVITY · DIAGNOSTICS                            [POISON RUN]          │
│   ┌─────────────────────────────────────────────────────────────┐       │
│   │ ▌BROKER · 1100   Connection to IBKR Gateway lost            │       │
│   │    Auto-recovery succeeded after 8s. No order activity      │       │
│   │    during the gap.    fix: monitor logs the next 30 min;    │       │
│   │    escalate if recurrence.                  [RAW LOG]       │       │
│   │    2026-06-17 13:48:22 ET · run_id 3f9c2e8b1d               │       │
│   ├─────────────────────────────────────────────────────────────┤       │
│   │ ▌CAP · WARN  Daily order cap 80% consumed                   │       │
│   ├─────────────────────────────────────────────────────────────┤       │
│   │ ▌DEPLOY  Run started · binding sealed against contract v3   │       │
│   └─────────────────────────────────────────────────────────────┘       │
│   PRIOR SESSION  [CLEAN EXIT]                                           │
│     Run 3f9c2e8b1c exited cleanly at 16:00:08 ET on 2026-06-16          │
│     with desired_state=STOPPED at session-end policy.                   │
│   AUDIT TRAIL · PROVENANCE        [📄]  ▸                               │
└─────────────────────────────────────────────────────────────────────────┘
```

Detective tab strip switches to Diagnostics. The **Poison run** action
appears in the tab bar header (right-side; red-outlined keycap-style) —
forces the operator to read the evidence tab before quarantining. View
Run Log is the compact 📄 icon-button in the Audit accordion header, no
`run_id` displayed on its face (it shows inside the modal).

## Visual language

### Color tokens

All inherited from `_tokens.scss` — no new tokens for the canvas,
surfaces, text, or semantic bull / bear / warn / info families. The
mockup adds **one new layer** (verdict tokens) that the cockpit needs,
intended for upstream addition to `_tokens.scss`:

```scss
// New — proposed addition to _tokens.scss "Semantic: Market" block
--verdict-ready:         var(--bull);      // #26a69a
--verdict-ready-soft:    var(--bull-soft);
--verdict-degraded:      var(--warn);      // #ff9800
--verdict-degraded-soft: var(--warn-soft);
--verdict-blocked:       var(--bear);      // #ef5350
--verdict-blocked-soft:  var(--bear-soft);
--verdict-unknown:       var(--text-muted);
--verdict-unknown-soft:  rgba(107,111,122,.15);
--verdict-paper:         var(--info);      // #29b6f6 — paper-only safety
--verdict-paper-soft:    var(--info-soft);
--verdict-unsafe:        var(--bear);      // matches blocked
```

**Why paper-only uses cyan, not green:** the safety verdict pill must
read as *distinct* from the readiness pill at a glance. Both green
would collapse two independent verdicts into one perceived signal. Cyan
for paper-only borrows the `--info` token already in the palette and
visually separates "we know this is paper" from "the bot can trade
right now."

### Pill anatomy (LED-segment)

```
┌──────────────────────────┐   font: JetBrains Mono 11px / 600
│ ● STATE  RUNNING         │   letter-spacing: 0.06em
└──────────────────────────┘   text-transform: uppercase
                               padding: 5px 9px
                               border: 1px solid var(--border-light)
                               border-radius: 2px        ← sharp, not rounded
                               background: var(--bg-sunken)
                               color: <verdict / state>
                               .dot: 6px round + box-shadow currentColor
```

Three pill kinds: **state** (process), **state** (intent), **verdict**
(safety / readiness), **chip** (prior-run). Visual distinction via the
`data-tone` / `data-verdict` / `data-priorrun` attributes; the CSS
selector is the contract.

### Keycap anatomy (action toolbar)

```
                          ╔═════════════╗
                          ║   PAUSE     ║   font: JetBrains Mono 11px / 700
                          ╚═════════════╝   letter-spacing: 0.07em
                          ▔▔▔▔▔▔▔▔▔▔▔▔▔   bottom outer shadow (1px depth rest)
```

- Default: `bg-elevated` → `#181c28` 180deg gradient
- Inset top: `rgba(255,255,255,.04)` hairline
- Inset bottom: `rgba(0,0,0,.5)` shadow
- Outer bottom: `0 1px 0 rgba(0,0,0,.6)` rest shadow
- Hover: gradient lifts; outer shadow stays
- Active: `translateY(1px)` + outer shadow swaps to inset (pressed-in)
- Disabled: opacity 0.4, no shadows, tooltip on hover

**`keycap--panic`** (Flatten-and-pause): `--warn` border + text, faint
`rgba(255,152,0,.08)` gradient background. **Not red-filled.** Red-fill
encourages hair-trigger clicks; outline + the panic-button placement
between Resume/Pause and the kebab divider is enough. Confirm modal
catches accidental clicks.

### Typography

| Surface | Family | Size | Weight | Letter-spacing |
|---|---|---|---|---|
| Bot name (banner) | system sans | 18px | 600 | -0.02em |
| Card label | JetBrains Mono | 10px | 700 | 0.10em uppercase |
| Card summary | system sans | 13px | 400 / 600 (.strong) | 0 |
| Status pill | JetBrains Mono | 11px | 600 | 0.06em uppercase |
| Keycap | JetBrains Mono | 11px | 700 | 0.07em uppercase |
| Tab strip | JetBrains Mono | 11px | 600 | 0.08em uppercase |
| Tables | JetBrains Mono | 12px | 400 + tabular-nums | 0 |
| Incident category | JetBrains Mono | 10px | 700 | 0.08em uppercase |
| Body copy | system sans | 13px | 400 | 0 |
| Metadata / id | JetBrains Mono | 10-11px | 400 | 0.02em |

Mono is *generously* used: every machine-readable identifier, every
status pill, every count, every table cell, every label. System sans is
reserved for human-readable copy (card headers, descriptions, tooltips,
incident messages).

### Motion

| Surface | Property | Duration | Easing |
|---|---|---|---|
| Verdict border (Can-It-Trade card) | `border-color`, `box-shadow` | 150ms | ease-out |
| Card expand/collapse | `height` (auto-ish) / display | 200ms | ease-out |
| Keycap press | `transform`, `box-shadow` | 100ms | linear |
| Tab switch | `border-bottom-color` | 150ms | ease-out |
| FAB hover | `background` | 100ms | linear |

That is the entire motion budget. No looping animation. No pulsing dot.
No skeleton fade. The page is intentionally still — when something
moves, the operator's eye lands on it.

### Atmospheric detail

Just two:

1. **Page-level screen grain.** `body` carries a 1px-2px repeating
   linear gradient at `rgba(255,255,255,.005)` opacity — invisible until
   you look for it; reads as terminal-screen texture, not visual flair.
2. **Banner accent radial.** A `radial-gradient(circle at 50% 0%,
   var(--accent-soft), transparent 60%)` at very low opacity on the
   `body` background. The banner sits inside this gentle blue wash
   without needing its own background color trick.

Plus one phosphor reference: the FAB and the checklist panel both carry
a 1-2px `var(--info)` border-top (cyan). It is the only place in the
cockpit that hints at "old terminal phosphor" — a single nod, not a
theme.

## Component breakdown → file structure

Maps the rendered layout onto the Angular component tree implied by the
runbook revision in PR #580.

| Section | Component | File path | New / Extended / Existing |
|---|---|---|---|
| Banner — pills + toolbar | `<app-sticky-control-bar>` | `Frontend/src/app/components/broker/broker-instances/sticky-control-bar/` | **Extended** (today: identity + readiness pill + jump-to-controls; new: 4 LED pills + `p-toolbar` keycap row + prior-run chip + `banner-attention` strip) |
| Banner attention strip | inline | same component | New 4px gradient div below the banner — driven by attention level |
| Configuration card | `<app-configuration-card>` | `Frontend/src/app/components/broker/broker-instances/configuration-card/` | **New** (merges today's `<app-strategy-rules-card>` + `<app-broker-sizing-card>`) |
| Pinned risk chip | inline | `configuration-card` template | New — visible only when `config.expanded` AND configure-state |
| Embedded sizing audit table | reuse | `configuration-card/audit-table.html` | New — embeds the per-trade audit table directly (today lives inside sizing card) |
| Current Risk card | `<app-current-risk-card>` | `current-risk-card/` | **Existing**, lightly extended — adds collapsed one-line posture summary |
| Can-It-Trade card | `<app-can-it-trade-card>` | `can-it-trade-card/` (renamed from `readiness-card/`) | **Existing → renamed**. Adds verdict-driven `data-verdict` attribute and the new `border + box-shadow` styles |
| Detective section + tabs | `<app-detective-section>` | `detective-section/` | **New** — owns the tab strip, the two tab panels, and the Poison action |
| Activity tab content | `<app-bot-trade-chart-card>`, `<app-latest-signal-strip>`, `<app-bot-trades-table>` | existing folders | **Existing**, composed by detective-section |
| Diagnostics tab content | `<app-incidents-panel>`, `<app-last-session-card>`, `<app-audit-trail-accordion>` | existing folders | **Existing**, composed by detective-section |
| Compact `View run log` icon-button | inline | `audit-trail-accordion/` header | **Extended** — today is a full-width panel-toolbar row above the dashboard grid; moves into the accordion header as a 28×28px icon-button |
| Poison action | inline | `detective-section/` tab-strip header | **Moved** — today lives in inline `Advanced Actions`; relocates to the Diagnostics tab header. Visible only when Diagnostics tab is active |
| Floating Pre-Trade Checklist | `<app-checklist-dialog>` (new) | `checklist-dialog/` | **New** — `p-dialog` with `[modal]="false"` `[position]="'bottomright'"`. FAB is a sibling component that toggles the dialog |
| Floating Checklist FAB | `<app-checklist-fab>` (new) | `checklist-fab/` | **New** — minimized chat-bubble at bottom-right with failing-count badge |

Components that are **deleted** in the IA revision:

| Today's component | Why it goes |
|---|---|
| System Health panel (inline) | Subsumed by fleet header + banner pills (no info loss) |
| `<app-broker-start-stop-card>` | Subsumed by the banner action toolbar |
| Inline `panel-toolbar` `View run log` row | Replaced by the icon-button in audit accordion header |
| `<app-strategy-rules-card>` | Merged into `<app-configuration-card>` |
| `<app-broker-sizing-card>` (broker-instances scope) | Merged into `<app-configuration-card>` |
| Inline `Managed Positions` card | Already flagged for deletion in the runbook's post-merge cleanup |
| Inline `Latest Strategy Signal` card | Redundant with `<app-latest-signal-strip>` per runbook |
| Inline `Bot Behavior` row (PAUSE / RESUME / STOP) | Subsumed by banner toolbar |
| Inline `Advanced Actions` (FLATTEN, MARK_POISONED, paper reset) | Distributed: Flatten → banner; Poison → Diagnostics tab; paper reset → fleet header (account-level) |

## State driven by server verdicts (page-wide collapse rule)

Per `CONTEXT.md` § "Page-wide collapse rule (resolved 2026-06-17)",
every dynamic state in the mockup is bound to a server-authored
verdict. The mockup's `body[data-state]` knob is a demo proxy for what
the live page derives from the status payload:

| Demo state | Live derivation |
|---|---|
| STEADY | `readiness.verdict === 'READY'` AND `risk.posture === 'FLAT'` AND `risk.pending_orders === 0` |
| CONFIGURE | `readiness.gates.some(g => g.shape === 'config' && g.status === 'fail')` |
| BLOCKED | `readiness.verdict === 'BLOCKED'` |
| TRIAGE | (operator-driven; no server signal — tab state is local) |

The rule's contract — *expand only on server-authored verdicts, never
frontend heuristics* — is preserved everywhere except the tab strip
(the Activity ↔ Diagnostics switch *is* an operator choice). The
`risk-pin` in the Configuration card header derives from
`risk.posture` + `risk.pending_orders` + `risk.unrealized_pnl` directly;
no client-side composition.

## What ships next (implementation order)

This document is design only — no Angular code yet. The order I would
ship in:

1. **Tokens.** Add the seven verdict tokens to `_tokens.scss`. One
   commit, no behavior change.
2. **Banner extension.** Extend `<app-sticky-control-bar>` to render the
   four pills, the keycap toolbar, the prior-run chip, and the
   attention strip. Keep the existing parent wiring intact behind a
   feature flag until 4 lands.
3. **Verdict-bordered card mixin.** Add a Sass mixin
   `card-verdict-border($verdict)` that applies the
   `border-color + box-shadow` styles. Used by Can-It-Trade first;
   reusable by any future verdict-bordered card.
4. **Can-It-Trade card.** Rename `readiness-card` → `can-it-trade-card`,
   apply the verdict mixin, add the collapsed one-line summary.
5. **Configuration card.** New `<app-configuration-card>` merging
   Strategy Rules + Sizing. Embeds the per-trade audit table. Implements
   the pinned risk-chip in its expanded header.
6. **Detective tabbed section.** New `<app-detective-section>` owning
   the tab strip and Poison action. Reuses existing children.
7. **Floating Checklist (FAB + dialog).** New components, p-dialog
   non-modal at bottom-right. Operator-triggered only.
8. **Delete the subsumed surfaces.** System Health, start-stop card,
   inline panel-toolbar `View run log` row, etc.

Each step is independently shippable behind a `broker-instances-v2`
feature flag the parent component reads; flip the flag on a single bot
first, then fleet-wide.
