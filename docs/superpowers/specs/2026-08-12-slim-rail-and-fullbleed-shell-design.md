# Slim icon rail + full-bleed page shell — design

**Date:** 2026-08-12
**Status:** Approved (brainstorm), pending implementation plan
**Surfaces:** `Frontend/src/app/shell/app-sidebar.component.*`, `Frontend/src/app/app.component.ts`, `Frontend/src/app/styles/_tokens.scss`, all routed page components, `Frontend/src/app/components/broker/v2-panel/panel-shell/bot-panel-shell.component.*`

## Problem

Two frame-level complaints about the app chrome:

1. **The sidebar is always fully expanded** (232px). It permanently consumes horizontal space even though most of the time the user just wants to glance at where they are and jump. A slim, always-present icon rail that reveals detail on demand reclaims that space. A `--sidebar-w-collapsed: 56px` token already exists in `_tokens.scss` but is unused — the slim rail was intended and never built.

2. **A uniform "padding shell" frames every page.** `app.component.ts` sets `.main-content { padding: var(--page-pad-y) var(--page-pad-x) }` (1.5rem all around), so every page looks boxed-in. It is worst on the **bot detail page**, which is *double*-framed: `bot-panel-shell` adds its own `:host { padding: var(--space-4) }` inside the already-padded main-content, then fights back out with `.lens-navigation { margin: calc(var(--space-4) * -1) … }` negative-margin bleed and a `height: calc(100dvh - (2 * var(--page-pad-y)))` correction. The page wants to be edge-to-edge and is currently plumbing around the frame to fake it.

## Decisions (locked in brainstorm)

- **Rail expansion model:** overlay/flyout, never push. The page content must never reflow (many surfaces carry live charts that are expensive to re-lay-out).
- **Flyout granularity:** only the **hovered item** flies out, never the whole menu.
- **Reveal depth:** hovering a group icon reveals its submenu items **immediately, in one hover** — no second click on the chevron.
- **Persistence:** a **pin toggle** locks the rail into the full 232px sidebar; preference persisted in `localStorage`. Unpinned is the slim default.
- **Padding removal:** **hard global removal now** — the shell owns no padding; every page owns its own inset. No opt-out flag, no shell-level shim.
- **Bot detail depth:** full-bleed **frame** redesign. The lens **tab panel becomes the outermost shell** of the page. Banner + panel content (freshly redesigned in recent commits) are preserved.

## Part A — Slim icon rail with per-item flyout

### Default (unpinned) — 56px icon rail

- One icon per nav group (the 8 existing groups keep their PrimeIcons `g.icon`). Brand collapses to the logo mark only (drop the `quant/lab` wordmark).
- The ⌘K search box collapses to a **search icon** at the top of the rail. Clicking/focusing it (or pressing ⌘K globally, unchanged) opens the search. Search UI itself is unchanged — flat-match mode still applies once a query exists.
- The broker status banner (`app-broker-banner`) stays pinned in the footer; in slim mode it renders in a compact/icon form (detail deferred to plan — it must not force the rail wider).
- **Active-route indication on the collapsed icon:** the group whose child is the active route shows an accent tick / highlighted icon, so the user sees where they are without hovering (reuse the existing `groupHasActive` / `activeRoute` logic).

### Hover → single-group flyout

- Hovering a group **icon** (or its rail row) opens a flyout **anchored to that icon only**. It shows the group title as a header and its `items[]` rendered as links — the current child-link markup, moved into the flyout.
- Mouse-leave (with a small close delay / hover bridge so the diagonal travel to the flyout doesn't dismiss it) closes the flyout.
- The chevron/carrot renders as a "has children" affordance and may rotate on open, but does **not** gate reveal — children are visible on hover.
- Clicking a child navigates and closes the flyout, exactly like today.

### Pinned — full 232px sidebar

- A **pin button** at the top of the rail toggles pinned. Pinned = the full sidebar exactly as it exists today (brand + wordmark, full search box, accordion group tree with per-group expand/collapse, auto-open-active-group, footer banner). Pinned **reserves** the 232px in layout; content sits beside it (no overlay).
- State persisted in `localStorage`; restored on load. Default = unpinned/slim.

### Preserved behavior (non-negotiable)

⌘K focus, flat search-match mode, `activeRoute` longest-match resolution, auto-open of the active group (in pinned mode), broker banner, all existing routes and `NavItem`/`NavGroup` data. The `NAV` information architecture is unchanged.

### Accessibility

- Rail icons are real buttons/links with accessible names (the group title as `aria-label` when the label is visually collapsed).
- Flyout is keyboard-reachable: focusing a group icon opens its flyout; `Esc` closes; arrow/tab moves through children. Hover is an enhancement, not the only path (WCAG — no hover-only interaction).
- Pin button has an accessible name and `aria-pressed`.
- Must pass AXE; WCAG AA contrast on the slim rail and flyout.

## Part B — Remove the shell frame; bot detail full-bleed

### Global

- Delete `padding` from `.main-content` in `app.component.ts`. `.main-content` becomes edge-to-edge and remains `flex: 1` full-height inside `.main`. The `container: ide / inline-size` context on `.main` is unaffected.
- Introduce **one** shared page-inset convention backed by the existing `--page-pad-x/-y` tokens — a mixin (e.g. `@include page-inset`) or a host utility class. It is the *page's* responsibility to apply it in its own `:host` scss. This is not a shim: the shell no longer frames anything; each page declares its own spacing.
- **Sweep the routed pages** (≈39 `loadComponent` entries; only ~21 component styles currently set any `:host` padding, so most rely on the shell today). Each page that should keep standard framing applies `page-inset` in its own `:host`. Pages intended to be full-bleed (bot detail, and any we deliberately de-frame) apply nothing. Verify no page renders flush-broken.

### Bot detail — tab panel as outermost shell

- Restructure `bot-panel-shell` so the **Trader | Operator lens tab bar is the top-level, flush, edge-to-edge frame** of the page (directly under the app chrome, beside the rail). All lens content nests inside it.
- Remove the compensating hacks that only exist to fight the old shell frame:
  - `:host { padding: var(--space-4) }` (the double-pad) — removed.
  - `.lens-navigation { margin: calc(var(--space-4) * -1) … }` negative-margin bleed — removed; the tab bar is genuinely at the edge now.
  - `height: calc(100dvh - (2 * var(--page-pad-y)))` — simplifies to filling the now-unpadded full-height main (`height: 100%` / `100dvh` as appropriate).
- The redesigned operator + trader banners and their panel content are **untouched**; only the outer framing changes. Each lens owns whatever internal padding its own content needs.
- Preserve the existing responsive breakpoints (`max-width: 800px`, `max-width: 1100px`) and the trader-lens flex-fill layout, re-expressed without the negative-margin dependency.

## Non-goals

- No change to the `NAV` information architecture, routes, or nav data model.
- No content/hierarchy redesign of the bot detail banners or lens panels (framing only).
- No change to search behavior, the broker health poll, or the markdown drawer / toast hosts.

## Blast radius & risk

- **Highest-risk item:** the global padding sweep touches many routed pages. Mitigation: the shared `page-inset` gives one canonical value (no per-page drift), and each page is visually verified. Pages already owning `:host` padding are left as-is unless they double up with `page-inset`.
- Flyout hover-intent (diagonal travel, close delay) is a known fiddly UX area — needs a hover bridge / small timeout, not an instant close.
- Pinned mode reintroduces layout width reservation; ensure the container-query context and any width-dependent pages behave at both 56px and 232px.

## Shipping

- Part A (rail) and Part B (padding + bot detail) are **independent** — two commits, optionally two PRs, on a **fresh branch off `master`** (current branch `codex/operator-bot-detail-banner` is scoped to the operator banner).
- Each PR runs the thermo-nuclear code-quality review before its first push and addresses every major finding (per CLAUDE.md).
- Project-scope lint (`npx eslint Frontend/src/ --max-warnings 0`) + frontend tests green before push.
- New/changed components ship with `*.component.spec.ts` asserting rendered behavior (slim vs pinned rendering, flyout reveal on hover/focus, active-route indication; bot detail full-bleed structure).
