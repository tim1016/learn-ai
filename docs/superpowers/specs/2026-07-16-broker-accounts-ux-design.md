# Broker Accounts UI/UX — Component Upgrade Design

**Date:** 2026-07-16
**Scope:** `/broker/accounts` (roster) and `/broker/accounts/:id` (account desk)
**Approach:** Option A — Component Upgrade (Evolutionary). Same information architecture, same routing, same stores/services. Pure presentation layer replacement.

---

## Goals

1. Fix a critical contrast failure: the account-desk component re-declares light-mode color values (`#172033`, `#556070`, `#ffffff`) that fight the global dark-mode token system (`--bg-canvas: #0b0e14`).
2. Replace hand-rolled CSS with PrimeNG components and Tailwind utilities, consistent with the rest of the broker section (bots page, deploy form, session mirror).
3. Make the roster page usable as both a monitoring dashboard and a fast entry point.
4. Align both pages with the TradingView-dark design token system in `_tokens.scss`.

## Non-goals

- Routing changes
- Information architecture changes (same data, same order)
- Logic changes in stores, services, or GraphQL queries
- Adding new features or data
- Merging the two pages into a master-detail layout

---

## 1. Posture Severity Mapper

**File:** `Frontend/src/app/components/broker/lib/account-posture-tag-severity.ts` (new file)

A single pure function shared by both pages:

```ts
export function accountPostureTagSeverity(
  posture: string,
): 'success' | 'warn' | 'danger' | 'secondary' {
  if (/clean|ready|active/i.test(posture)) return 'success';
  if (/degraded|warning|stale/i.test(posture)) return 'warn';
  if (/blocked|error|failed|frozen/i.test(posture)) return 'danger';
  return 'secondary';
}
```

Both `account-roster-page` and `account-desk-page` import from this location. No duplication.

---

## 2. Roster Page (`/broker/accounts`)

**Component:** `account-roster-page.component`

### Template

Replace the `<ol>/<button>` list with a `p-table`:

- Columns: Account ID, Broker, Posture, Account Service, Verdict, Last Verified
- Posture and Verdict rendered as `p-tag` with severity from `accountPostureTagSeverity()`
- `[selectionMode]="'single'"` — clicking a row fires `openDesk(row)` (no change to handler)
- Loading state: `p-skeleton` placeholder rows (3 rows, shown while `rosterLoading() && !rosterHasLastGood()`)
- Error state: `p-message` with severity `error` + a `pButton severity="secondary"` Retry
- Empty state: `p-message` with severity `info`
- Stale banner: `p-message` with severity `warn` at bottom

Header retains the "Accounts" `<h1>` and subtitle `<p>`. A `pButton` icon-only Refresh button (`icon="pi pi-refresh"`) sits at the header right — calls `retry()`.

### SCSS

Replace the 4-line file with Tailwind on the host wrapper:
```scss
:host { display: block; max-width: 70rem; margin: 0 auto; padding: 1rem; }
```
No other custom SCSS needed — PrimeNG table handles its own layout.

### PrimeNG imports added

`TableModule`, `TagModule`, `ButtonModule`, `SkeletonModule`, `MessageModule`

---

## 3. Account Desk (`/broker/accounts/:id`)

**Component:** `account-desk-page.component`

### 3a. Contrast Fix (highest priority)

Remove all local CSS custom property re-declarations from `account-desk-page.component.scss`:

```scss
/* DELETE the entire block: */
--text-primary: #172033;
--text-secondary: #556070;
--text-color: #172033;
--text-color-secondary: #556070;
--text-muted: #556070;
--operator-blocker-border: #d6a447;
--operator-blocker-left-border: #a96400;
--operator-blocker-surface: #fff9ec;
--operator-blocker-terminal-border: #dfafb1;
--operator-blocker-terminal-left-border: #b24045;
--operator-blocker-terminal-surface: #fff5f5;
--operator-blocker-warning-border: #9bc4e4;
--operator-blocker-warning-left-border: #176b9b;
--operator-blocker-warning-surface: #f1f8fd;
```

Replace operator-blocker palette with dark-mode equivalents in the operator-blocker component's own SCSS (wherever it consumes these tokens):

| Old (light) | New (dark token) |
|---|---|
| `--operator-blocker-surface: #fff9ec` | `var(--warn-soft)` |
| `--operator-blocker-border: #d6a447` | `var(--warn)` |
| `--operator-blocker-terminal-surface: #fff5f5` | `var(--bear-soft)` |
| `--operator-blocker-terminal-border: #dfafb1` | `var(--bear)` |
| `--operator-blocker-warning-surface: #f1f8fd` | `var(--info-soft)` |
| `--operator-blocker-warning-border: #9bc4e4` | `var(--info)` |
| `--operator-blocker-headline: #172033` | `var(--text-primary)` |
| `--operator-blocker-detail: #37485a` | `var(--text-secondary)` |
| `--operator-blocker-meta: #4f6172` | `var(--text-subtle)` |
| `--operator-blocker-link-surface: #ffffff` | `var(--bg-elevated)` |
| `--operator-blocker-link-text: #0d5e91` | `var(--accent)` |

All remaining hardcoded hex values in account-desk SCSS (`#172033`, `#556070`, `#d4d9e1`, `#dce4ec`, `#176b9b`, `#1a6495`, etc.) are replaced with the nearest global token.

### 3b. Verdict Card

Replace `.verdict-card.card` div with `p-card`:

- **Header slot:** Flex row containing posture `p-tag` (large, severity from mapper) + freshness `<dl>` (right-aligned). No change to freshness data — just token-corrected text colors.
- **Content slot:** `<h2>` headline + `<p>` detail text + `app-account-desk-guidance` + operator-attention summary + primary-move button.
- **Primary-move button:** `pButton` (no severity = primary/accent). Replaces `.primary-move` hand-rolled button.
- **Headline metrics `<dl>`:** Kept as flex row at the bottom of the card content. `Day P&L` value gets `.positive` / `.negative` global class based on sign. Token-corrected `<dt>` colors.
- **Stale/error states:** `p-message` components inside the card.

### 3c. Lens Toggle

Replace `.lens-toggle` nav with `p-selectbutton` (`SelectButtonModule`):

```html
<p-selectbutton
  [options]="lensOptions"
  [ngModel]="lens()"
  (ngModelChange)="lens.set($event)"
  optionLabel="label"
  optionValue="value"
  aria-label="Account desk lens"
/>
```

`lensOptions` is a readonly array on the component: `[{ label: 'Trader', value: 'trader' }, { label: 'Operator', value: 'operator' }]`. Add `FormsModule` to the component's `imports` array — needed for `ngModel` binding on PrimeNG SelectButton in a zoneless component. The `lens` signal binds via `[ngModel]="lens()"` (read) and `(ngModelChange)="lens.set($event)"` (write) — this is the correct pattern for signals with PrimeNG form controls.

The `small` subtitle ("Positions and activity", "Proof and account actions") is dropped — the label alone is sufficient and PrimeNG SelectButton doesn't support two-line option labels cleanly.

Remove ~20 lines of `.lens-toggle` SCSS.

### 3d. Trader Workspace

**Holdings section (`account-desk-trader-holdings`):**

- Replace `.holdings-table` with `p-table` (`TableModule`), `[stripedRows]="true"`
- Columns: Symbol, Type, Quantity, Value, Live P&L, Owner
- Numeric columns: `class="text-right font-mono"` (Tailwind)
- Row expansion: `p-table` row expansion for holdings with blockers — `app-operator-blocker-list` renders in the expanded row
- Balances `<dl>` header: token-corrected colors, layout unchanged
- Holdings section is NOT wrapped in a collapsible panel — it is primary trader content and must be visible on load

**Events + Guidance sections (`account-desk-trader-events`, `account-desk-guidance`):**

- Each wrapped in `p-panel` — collapsible, collapsed by default on load
- Panel header text: existing `<h2>` content
- Inner content unchanged — just token-corrected

**Outer desk-body card:** `.desk-body.card` becomes `p-card` with `styleClass="!p-0"` — panels control their own padding.

### 3e. Operator Workspace

**Proof + Recovery columns:**

- `.operator-workspace__proof` div → `p-card`
- `.operator-workspace__recovery` div → `p-card` (replaces `background: #fbfdff; border-color: #bdd8ea`)
- Two-column CSS grid layout preserved: `grid-template-columns: minmax(0, 1.2fr) minmax(19rem, .8fr)`

**Proof subsections (`account-desk-operator-proof`):**

- "Reconciliation action & evidence" → `p-panel` (header slot = `<h3>` text)
- "Observation lease evidence" → `p-panel` side-by-side in existing grid
- `<dl>` / `<ul>` / `<ol>` inside: token-corrected colors, structure unchanged

**Recovery controls (`account-desk-recovery-controls`):**

- Wrap in `p-card`
- Action buttons → `pButton` with severity: confirm = default, destructive = `severity="danger"`, secondary = `severity="secondary"`

**Fleet evidence (`account-desk-operator-fleet`):**

- Wrap in `p-panel` with header "Fleet contamination", `[collapsed]="true"` default
- Inner `<dl>` and `<ul>`: token-corrected

**Operator service (`account-desk-operator-service`) and Operator events (`account-desk-operator-events`):**

- Each wrapped in `p-panel`, `[collapsed]="true"` default (these are audit/history — available but not always needed)
- Inner content: token-corrected

**Workspace header:** `.operator-workspace__header` border-bottom color → `var(--border-light)`. Text colors → global tokens.

### 3f. Account Switcher (`account-desk-account-switcher`)

No structural change. Audit for any hardcoded light-mode hex values and replace with global tokens.

### 3g. Retained SCSS

After the token cleanup and PrimeNG replacement, only structural rules remain in `account-desk-page.component.scss`:

```scss
.account-desk { display: grid; gap: 1rem; }
.operator-workspace { display: grid; gap: 1.25rem; }
.operator-workspace__actions {
  display: grid;
  grid-template-columns: minmax(0, 1.2fr) minmax(19rem, .8fr);
  gap: 1rem;
  align-items: start;
}
.operator-workspace__header { display: grid; gap: .25rem; padding-bottom: .25rem; border-bottom: 1px solid var(--border-light); }
.headline-metrics { display: flex; gap: 1rem; flex-wrap: wrap; margin: 0; }
.headline-metrics div { display: grid; gap: .2rem; }

@media (max-width: 48rem) {
  .operator-workspace__actions { grid-template-columns: 1fr; }
}
```

### PrimeNG imports added to desk page

`CardModule`, `PanelModule`, `SelectButtonModule`, `TableModule`, `TagModule`, `ButtonModule`, `MessageModule`

---

## 4. Operator-Blocker Component

**Component:** `broker/shared/operator-blocker-list/operator-blocker-list.component.scss`

**No changes needed.** The operator-blocker component already uses `color-mix()` fallbacks pointing to the correct dark-mode tokens (e.g. `color-mix(in srgb, var(--bg-surface, #131722) 88%, var(--warn, #ff9800))`). It only renders incorrectly because `account-desk-page.component.scss` overrides the `--operator-blocker-*` CSS variables with light-mode values, which shadow the component's own fallbacks.

Deleting those overrides from `account-desk-page.component.scss` (§3a) is sufficient — the component SCSS is already correct and dark-mode aware.

---

## 5. File Change Summary

| File | Change |
|---|---|
| `broker/lib/account-posture-tag-severity.ts` | **New** — pure severity mapper |
| `account-roster-page.component.html` | Replace `ol/button` with `p-table` |
| `account-roster-page.component.scss` | Replace 4 lines with `:host` rule |
| `account-roster-page.component.ts` | Add `TableModule`, `TagModule`, `ButtonModule`, `SkeletonModule`, `MessageModule`; import severity mapper |
| `account-desk-page.component.html` | Replace `.card` divs, lens toggle, and primary-move button |
| `account-desk-page.component.scss` | Remove light-mode overrides; retain structural grid rules only |
| `account-desk-page.component.ts` | Add `CardModule`, `PanelModule`, `SelectButtonModule`, `FormsModule`, `TagModule`, `ButtonModule`, `MessageModule`; add `lensOptions` array; import severity mapper |
| `account-desk-trader-holdings.component.html` | Replace table with `p-table` + row expansion |
| `account-desk-trader-holdings.component.scss` | Token-correct all hex values |
| `account-desk-trader-events.component.html` | Wrap in `p-panel` |
| `account-desk-trader-events.component.scss` | Token-correct |
| `account-desk-operator-proof.component.html` | Wrap subsections in `p-panel` |
| `account-desk-operator-proof.component.scss` | Token-correct |
| `account-desk-operator-fleet.component.html` | Wrap in `p-panel` |
| `account-desk-operator-fleet.component.scss` | Token-correct |
| `account-desk-operator-service.component.html` | Wrap in `p-panel` |
| `account-desk-operator-service.component.scss` | Token-correct |
| `account-desk-operator-events.component.html` | Wrap in `p-panel` |
| `account-desk-operator-events.component.scss` | Token-correct |
| `account-desk-recovery-controls.component.html` | Wrap in `p-card`; buttons → `pButton` |
| `account-desk-account-switcher.component.scss` | Token-correct any hardcoded hex |
| `operator-blocker-list.component.scss` | No changes — fallbacks already correct; fixing the parent (§3a) is sufficient |

---

## 6. What Does NOT Change

- All Angular store services (`AccountDeskSurfaceStore`, `AccountDeskHoldingsStore`, etc.)
- All GraphQL queries and types
- All routing configuration
- All signal architecture and computed values
- All `receiptLabel` pipe usage
- All `TimestampDisplayComponent` usage
- All ARIA labels, fragment navigation, focus management
- The `account-desk-guidance.component` internal structure
- The `account-desk-recovery-confirm-dialog` internal structure

---

## 7. Testing

- Run `podman exec my-frontend npx ng test --watch=false` after changes
- Visually verify both routes against the live app
- Confirm AXE passes on both pages (the SelectButton must have `aria-label`)
- Confirm table row click still navigates correctly on the roster page
- Confirm lens switch still works on the desk page
- Confirm operator-blocker colors are visible on dark canvas (no white-on-white)
