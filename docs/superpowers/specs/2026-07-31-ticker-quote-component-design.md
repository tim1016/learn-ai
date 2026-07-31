# Design: `app-ticker-quote` — reusable inline/card ticker quote

- **Date:** 2026-07-31
- **Status:** Draft (awaiting spec review)
- **Layer:** `Frontend/` (Angular 22 SPA)
- **Kind:** Presentational, data-in display component. No math port, no data fetching.

## 1. Context & motivation

We want one reusable component that renders a symbol's identity + live quote, usable in
many places. It originates from a hand-written Tailwind + `lucide-angular` + `NgModule`
snippet (a hardcoded SPY card) that does **not** fit this repo's stack. Rather than port
that snippet, we rebuild it to repo conventions (signals, `input()`, `OnPush`, co-located
SCSS + design tokens, no `NgModule`, no `ngClass`) and — crucially — **reuse existing repo
machinery** instead of reinventing it.

### The "beautiful icon" = the standard TradingView approach (already in the repo)

The snippet drew a generic `lucide` "activity" glyph. The real TradingView convention is to
render each symbol's **actual brand logo** from TradingView's public logo CDN:

```
https://s3-symbol-logo.tradingview.com/{company-slug}.svg
    SPY  -> spdr-s-p-500-etf-tr.svg
    AAPL -> apple.svg
    NVDA -> nvidia.svg
```

This is **already implemented** in `Frontend/src/app/shared/asset-identity/`
(`app-asset-identity`): symbol -> slug mapping (known-slug table + normalizer), lazy-loaded
SVG, graceful monogram-circle fallback on unknown/404 (`onLogoError`), `sm|md|lg` sizes,
`default|inverse` tone. It is already consumed in 4+ places (verdict-card,
ibkr-api-evidence-panel, bot-trade-chart-card, working-pending-orders-section).

**Decision:** `app-ticker-quote` *composes* `app-asset-identity` for the logo + symbol
(+ name + exchange) block and adds only a price/trend row. No logo logic is duplicated
(satisfies the repo single-source-of-truth / no-duplication rule).

## 2. Goals / non-goals

**Goals**
- One component, two presentation modes: `inline` (minimalistic, default, table-friendly)
  and `card` (large block).
- Fully generalized: all ticker info passed in via a single typed input; droppable anywhere.
- Real TradingView symbol logos via the existing `asset-identity` component.
- Strict adherence to repo Angular rules and accessibility (AXE / WCAG AA).

**Non-goals (YAGNI)**
- No self-fetching / live subscription. Parent supplies data.
- No `NgModule`, no new dependencies (`primeicons@^7` and `asset-identity` already exist).
- No timestamp / "as of" rendering in v1 (the view-model carries no time). If added later it
  must be `int64 ms UTC` via the shared display path per `temporal-rigor.md`.
- No golden fixture — this is display formatting, not a numerical port.

## 3. Public API

Location (mirrors `asset-identity/`):

```
Frontend/src/app/shared/ticker-quote/
  ticker-quote.component.ts | .html | .scss | .spec.ts
  index.ts   # barrel: component + TickerQuoteView
```

Selector: `app-ticker-quote`.

```ts
export interface TickerQuoteView {
  symbol: string;
  name?: string | null;
  exchange?: string | null;
  price: number;
  change?: number | null;     // $ move — optional; v1 shows it in the host title only
  changePercent: number;      // % move — drives color/caret/sign
  logoSlug?: string | null;   // optional override, forwarded to asset-identity
  currencySymbol?: string;    // optional, defaults to '$'
}

// component inputs
quote = input.required<TickerQuoteView>();
mode  = input<'inline' | 'card'>('inline');       // default inline (table cells)
size  = input<'sm' | 'md' | 'lg'>('md');           // applies in card mode; inline forces 'sm'
tone  = input<'default' | 'inverse'>('default');   // forwarded to asset-identity + figures text
logo  = input<boolean>(true);                      // forwarded to asset-identity; off for dense tables
```

A parent forwards a snapshot in one binding. Live quotes elsewhere come from
`MarketDataService` snapshots (`SnapshotUnderlyingResult = { ticker, price, change,
changePercent }`), which map directly onto `TickerQuoteView`.

```html
<app-ticker-quote [quote]="spyQuote()" />                   <!-- inline, in a table cell -->
<app-ticker-quote [quote]="spyQuote()" mode="card" size="lg" />
```

## 4. Modes

Same view-model, two densities. `mode` is the primary switch; a host class
(`.ticker-quote--inline` / `.ticker-quote--card`) drives layout and `computed` flags toggle
elements.

| Element | `inline` (default, minimalistic) | `card` (large) |
|---|---|---|
| container | `inline-flex`, baseline/middle-aligned, **no border/bg/margin**, `white-space: nowrap` | block flex, bordered + padded + radius + shadow, two-column (identity left / figures right) |
| logo (asset-identity) | `sm` (or off via `logo=false`) | `md` / `lg` |
| symbol | yes | yes |
| name | no | yes |
| exchange chip | no | yes (when provided) |
| price | yes | yes |
| caret icon | no — sign carries direction | yes (`pi pi-caret-up/down/minus`) |
| signed colored % | yes | yes |

**Table use (the default case).** Inline is tuned to live in a `<td>`: single line, nowrap,
no margins (the cell owns spacing), minimal height (won't inflate row height), `sm` logo.
Numeric alignment (e.g. right-aligned %) is the table cell's job, not the component's.

**Inline preview** (flows in prose / a cell):
```
Watching (o)SPY $542.44 +0.77% into the close, while (o)QQQ $471.10 -0.42% lags.
                 |-green-|                              |--red--|
```

**Card preview:**
```
+------------------------------------------+
| (O) SPY  [ARCA]            $542.44        |
|      SPDR S&P 500 ETF      ^ +0.77%       |  green
+------------------------------------------+
```

## 5. Rendering details

### Trend (three states)

Small upgrade over the repo's binary `>=0 / <0` split, matching TradingView:

- `changePercent > 0` -> `.positive` (`$bull` green), caret `pi-caret-up`, `+` sign
- `changePercent < 0` -> `.negative` (`$bear` red), caret `pi-caret-down`, pipe's `-`
- `changePercent === 0` -> `.flat` (muted), caret `pi-minus`, no sign

Direction is conveyed by **caret shape + sign**, never color alone. In `inline` (no caret)
the `+`/`-` **sign** is the non-color cue, so WCAG 1.4.1 holds without the arrow.

### Formatting

- Percent: repo-canonical pattern — `number:'1.2-2'` pipe + a `computed` sign prefix
  (`sign = changePercent > 0 ? '+' : ''`). No `ngClass`, no `toFixed`.
- Price: `{{ currencySymbol }}{{ price | number:'1.2-2' }}`. Price is a **value, not a receipt
  token** -> it is NOT piped through `receiptLabel` (per CLAUDE.md — only opaque IDs are).

### Accessibility

- Caret `<i>` is `aria-hidden`. The change span carries an `aria-label` ("up 0.77 percent").
- Host `title` = `Name (SYMBOL) — $price, +0.77%` (uses `change` $ when present).
- Must pass AXE; `$bull`/`$bear` on the card/inline surface must meet AA contrast (verify).

## 6. Shared-component change: `app-asset-identity` gains optional `exchange`

To show the `[ARCA]` chip on the symbol line in `card` mode without a second component that
knows about identity layout, add an **optional** `exchange` input to `app-asset-identity`:

- `exchange = input<string | null>(null)`; renders a small muted chip after the symbol only
  when non-empty.
- Purely additive, null default -> the 4 existing consumers are unaffected.
- Regression test added asserting the chip is absent when `exchange` is not provided.

Reversible: if rejected at review, render the chip locally inside `ticker-quote` (card mode)
or drop `exchange` from the view-model.

## 7. Testing plan (Vitest + Angular Testing Library, behavioral)

`ticker-quote.component.spec.ts`:
- positive/negative/flat -> correct `.positive/.negative/.flat` class, caret glyph (card),
  and sign (`+` / `-` / none).
- price rendered with currency prefix + 2 decimals; custom `currencySymbol` respected.
- `inline` (default): name, exchange chip, and caret are NOT rendered; single-line.
- `card`: name, exchange chip (when provided), and caret ARE rendered.
- `logo=false` -> no logo element (forwarded to asset-identity).
- assert rendered output, not private signals.

`asset-identity.component.spec.ts` (regression):
- `exchange` provided -> chip rendered with the value.
- `exchange` absent -> no chip (existing consumers unchanged).

## 8. Compliance checklist (repo rules)

- [x] Standalone, `OnPush`, no `standalone: true` noise, no `NgModule`.
- [x] `input()`/`computed()` signals; `inject()` if any DI needed (none expected).
- [x] `@if` control flow; `[class.x]` bindings, no `ngClass`/`ngStyle`.
- [x] Co-located SCSS with `@use ... tokens` (`$bull`/`$bear`/surfaces).
- [x] No `any`; `TickerQuoteView` typed; strict template type-safety via getters/computed.
- [x] Price unpiped (value, not receipt ID); no backend prose re-derived on client.
- [x] AXE + WCAG AA; direction not color-only.
- [x] No new deps; reuses `asset-identity` + `primeicons`.
