# PRD: Broker Account Desk overhaul — Trader/Operator lenses, robust equity history, evidence modal, deploy drawer, gallery nav

**Status:** ready-for-agent · **Date:** 2026-08-12 · **Execution:** fully AFK, parallelizable slices (no manual review gate)

## Problem Statement

The live Alpaca broker account surface (`brokers/alpaca`, `AlpacaDeskComponent`) stacks
everything into one undifferentiated page — account card, hold banner, custody, positions,
transaction history, order entry — with no notion of *who is looking*. Two very different
people use it:

- A **trader** who wants to know *"how am I doing?"* — trades, P&L, how many trades went
  through, how the account is performing over time.
- An **operator** who wants to know *"why is the system working — or not — and how do I
  fix it?"* — mechanism, evidence, and repair.

Today they get the same flat page. Worse, a trader/operator "lens" concept **already exists**
but is **orphaned**: `AccountDeskPageComponent` (built for the Account Desk work, PRD #1086)
has the Trader/Operator toggle, but when the Alpaca v2 surface landed every route to it was
redirected to `brokers/alpaca`, stranding the lens machinery. So the good idea exists as dead
code while the live page has none of it.

Three secondary problems compound it:

1. **The evidence modal is thin.** The per-transaction Clerk receipt reader
   (`clerk-transaction-evidence-drawer`) is four flat stacked `<dl>` blocks — no visual
   lifecycle, no accordions, no tables. It reads like a debug dump, not evidence you would
   show someone.
2. **The instrument-identity component is barely used.** `app-asset-identity` (logo + symbol,
   the canonical way to render a tradeable instrument) appears in ~4 files, while ~40 places
   render a raw `{{ symbol }}` / `{{ ticker }}` string.
3. **Deploy is a whole page** you navigate away to, and the **Bot Gallery** — a live chart
   wall of every running bot — isn't in the nav at all; it's only reachable from a button
   buried inside the Bots list.

## Solution

Rebuild the broker account desk **on the live Alpaca surface** as a **single route with an
in-page Trader ⇄ Operator lens toggle**, delete the orphaned lens page entirely, and give each
persona a purpose-built view:

- **Trader lens (outcomes):** a glance-layer of hero metric tiles over a narrative
  *"Today at the desk"* timeline, with a **Today · 30D · 60D** scope control that swaps in a
  **robust, investor-grade equity history**.
- **Operator lens (mechanism + repair):** one **dominant posture headline with the fix
  attached**, over a **forensic evidence grid**, over collapsed deep-system panels.

The equity history is the crucial, done-very-well centerpiece: not "our number" or "the
broker's number" but **both, reconciled** — the broker's authoritative curve as the displayed
spine, our independent FIFO P&L as the attribution that *explains* it, and a reconciliation that
*proves* the two agree. That is what lets the account be defended to an investor.

Alongside the desk: redesign the shared evidence modal (lifecycle timeline as hero, accordions,
tables), adopt `app-asset-identity` as the canonical symbol renderer, turn the deploy page into
a slide-over drawer, and add the Bot Gallery to the nav.

## User Stories

### Desk shell & lens

1. As a user of the broker account desk, I want a single desk that I can view through a
   **Trader** or **Operator** lens, so that I see only what's relevant to how I'm using it.
2. As a first-time visitor, I want the desk to **default to the Trader lens**, so that I land
   on the friendly outcomes view rather than a forensic one.
3. As an operator who lives in the operator view, I want the desk to **remember my last lens**,
   so that I'm not re-toggling on every visit.
4. As an operator, I want a **deep link to the operator lens** (`?lens=operator`), so that I can
   bookmark or share the exact view.
5. As a user, I want switching lenses to be **instant** (no reload or account re-fetch), so that
   comparing the two altitudes is frictionless.
6. As a trader, I want the **heavy operator data to load only when I switch to Operator**, so
   that the trader front door stays fast.

### Trader lens — outcomes

7. As a trader, I want a **hero row of metric tiles** — today's P&L ($ and %), open positions
   (count + net exposure $), **fills today** ("how many trades went through"), and realized P&L
   today — so that I get my account's state at a glance.
8. As a trader, I want account **equity and cash** in the desk header, so that I always see the
   headline number.
9. As a trader, I want a **"Today at the desk" timeline** as the centerpiece — a chronological
   narrative feed of what happened (entered SPY → filled @ X → exited → +$Y) — so that I can
   read my day like a story, not a table.
10. As a trader, I want each timeline entry and position to render the instrument through the
    **`asset-identity` component** (logo + symbol), so that instruments are recognizable at a
    glance rather than raw text.
11. As a trader, I want a **positions table** below the timeline, so that I can drop to detail
    when I need it.

### Trader lens — robust equity history

12. As a trader, I want a **Today · 30D · 60D** scope control on my desk, so that I can widen
    from live activity to account history without leaving the page.
13. As a trader/investor, I want a **reliable equity curve over 30/60 days** that matches what
    my broker's own statement shows, so that I can trust and defend the number.
14. As an operator, I want the equity curve backed by the **broker's authoritative
    portfolio history** (custodian of record), so that the displayed value is the legally/
    financially true account value.
15. As an operator, I want an **independent, locally-computed P&L attribution** (FIFO over our
    own fill history) alongside the broker curve, so that I can *explain* which bot / trade /
    fee moved the line on any given day.
16. As an operator, I want a **reconciliation that proves the broker curve and our local P&L
    agree** within an explicit tolerance, and **flags divergence** when they don't, so that the
    account view is defensible rather than a black box.
17. As a trader, I want a **trade/fill history list** beneath the equity curve for the selected
    window, so that I can see the transactions behind the curve.
18. As an operator, I want the system to **record our own daily equity snapshot from day one**,
    so that over time a fully-sovereign cross-check curve accrues independent of the broker.

### Operator lens — mechanism & repair

19. As an operator, I want **one dominant posture headline** — "System healthy" or the single
    most important thing wrong — so that I'm not parsing a wall of statuses to learn the state.
20. As an operator, I want the **fix action attached to the headline**, driven by the
    OperatorBlocker disposition (`fix_here` → repair button here; `fix_elsewhere` → deep link;
    `wait` → countdown; `terminal` → escalation), so that diagnosis and repair are one motion.
21. As an operator, I want a **forensic transaction evidence grid** — Recorded · Origin & context
    · Instruction & execution · Lifecycle · Evidence — so that I can audit every Clerk receipt.
22. As an operator, I want **filters** on the grid (origin, lifecycle, bot, run), so that I can
    narrow to the transactions I care about.
23. As an operator, I want the forensic grid to be **operator-only** (the trader gets the
    narrative timeline instead), so that each persona sees the right altitude of the same
    underlying transactions.
24. As an operator, I want **deep system panels** (custody clocks, broker session/capability,
    fleet, truth spine) available but **collapsed by default**, so that the mechanism is one
    layer down, not in my face.

### Evidence modal

25. As an operator, I want the evidence reader to open with an **`asset-identity` header** (which
    trade is this) next to the lifecycle badge and order reference, so that I orient instantly.
26. As an operator, I want the **custody lifecycle (A0 accepted → A1 broker write → A2 ack → A3
    economic terminal) as a visual timeline hero**, so that I can see at a glance where a
    transaction completed or stalled.
27. As an operator, I want the verbose sections — **instruction-vs-execution table, event log,
    raw receipt dump** — in **collapsed accordions**, so that the modal isn't a wall of
    definition lists.
28. As an operator, I want instruction/execution rendered as a **table** with the instrument via
    `asset-identity`, so that instructed-vs-filled is easy to compare.
29. As a developer, I want the evidence reader to remain **one shared canonical component**
    reused everywhere evidence is shown, so that there is a single source of truth.
30. As an operator, I want every backend label to keep rendering through the **`receiptLabel`
    pipe** and opaque IDs preserved verbatim, so that audit tokens stay exact.

### Instrument identity (ticker) rollout

31. As a user, I want any tradeable instrument symbol to render through **`asset-identity`**
    (logo + symbol) rather than raw text, so that instruments look consistent app-wide.
32. As a user, I want the component used **even inside tables** (compact variant), so that dense
    rows still show a recognizable instrument.
33. As a maintainer, I want `asset-identity` established as **the canonical symbol renderer with
    a written rule** (like the `receiptLabel` rule), so that new code doesn't reintroduce raw
    strings.
34. As a user, I want the logo lookup to **resolve reliably** (not fall back to initials for
    common symbols), so that logos actually appear on the broad surfaces.

### Deploy drawer

35. As a trader, I want to **deploy a strategy from the account desk** via a "Deploy strategy"
    button, so that I stay in context instead of navigating to a separate page.
36. As a user, I want deploy to open as a **roomy right-side slide-over drawer** (multi-step
    workflow), so that the desk stays visible behind it.
37. As a user, I want to launch the deploy drawer **from the Bots list** too, so that it's
    reachable from both natural entry points.
38. As a user, I want the old deploy **deep link and nav entry preserved** (they open the drawer
    over the desk via `?deploy`), so that bookmarks and muscle memory still work.

### Bot Gallery nav

39. As a user, I want the **Bot Gallery in the sidebar nav** (Alpaca group, next to Bots), so
    that the live chart wall is a first-class destination.
40. As a user, I want the gallery reachable from an **unscoped route** that resolves my account
    automatically (like Bots does), so that the nav link works without me supplying an account.
41. As a user, I want the **Gallery nav item highlighted** when I'm on the gallery page, so that
    the active state is correct.

## Implementation Decisions

### Foundation

- **Build on the live surface.** Evolve `AlpacaDeskComponent` at `brokers/alpaca`. **Delete**
  the orphaned `AccountDeskPageComponent` and its stranded lens machinery (the lens select,
  trader/operator event components, operator workspace, and any store wiring reachable only
  from it). Harvest *concepts*, not the stale wiring.
- **Lens shell.** Single route, in-page toggle backed by a `lens` signal
  (`'trader' | 'operator'`), reflected in a `?lens=` query param, default `trader`, last choice
  persisted (localStorage). Operator-only data is lazy-loaded on first switch to operator.

### The robust equity-history hybrid (crucial, contract-first)

Three producers + one recorder, each an independent workstream, meeting the frontend at fixed
contracts. **Ownership: Python / SQLite** (consistent with "SQLite is the sole authority for
Alpaca execution / fills / P&L / history"). The existing .NET `SnapshotService` is *not* reused
— it serves the internal Guid portfolio, the wrong account domain.

- **Contract C1 — Broker portfolio history (authoritative curve).** New FastAPI endpoint that
  proxies Alpaca's `/v2/account/portfolio/history`. Response (snake_case, `int64 ms UTC`
  timestamps): `{ timestamps: number[], equity: number[], profit_loss: number[], base_value:
  number, timeframe: string }`. Range selector (`1D`/`30D`/`60D`) maps to Alpaca `period` +
  `timeframe`. No recomputation — the broker's own curve is the displayed spine.
- **Contract C2 — Account P&L attribution (independent books).** Generalize the existing
  per-bot fill window (`bot_fill_window`) to an **account-level `from`/`to` window** over the
  SQLite economic projection, returning per-trade FIFO attribution rows, window realized P&L,
  start/end open-lot valuations, window fees with fidelity, and explicit account-execution
  coverage. A pre-window open book without boundary marks is incomplete evidence, never an
  implicit zero baseline. This is our independent record.
- **Contract C3 — Reconciliation.** A **pure function** taking the C1 broker curve and the C2
  local P&L over the same window, returning `{ broker_delta, local_delta, residual,
  within_tolerance, atol, rtol, divergences[] }` with **explicit tolerances**. Classify any
  divergence in the spirit of the existing `DivergenceCategory` taxonomy
  (`app/research/parity/qc_reconciler.py`). Surfaced with C1 so the UI can render the
  "agrees to within $X" proof. The local delta is `realized + end_open - start_open - fees`.
  A green result additionally requires complete account execution coverage, reported fees,
  complete boundary marks, and marks inside the corresponding C1 broker-timeframe buckets;
  otherwise C3 returns `FIXTURE_INSUFFICIENT`.
- **Contract C4 — Daily sovereign snapshot.** A snapshot-writer that appends a daily account
  equity row to a new SQLite table (append-only, account-scoped), plus a scheduled job to invoke
  it once per session close (session boundary from the **canonical trading calendar**, not a
  hardcoded time). Belt-and-suspenders; the sovereign curve accrues going forward.
- The new endpoint(s) will trip the **OpenAPI contract CI gate** — regenerating the committed
  contract is part of the backend slices.

### Frontend components

- **Desk shell** owns the lens toggle scaffold and renders one of two lens child components by
  signal. Trader and Operator lenses are **separate components with disjoint files**, meeting
  the shell at their selectors (a contract), so they parallelize.
- **Trader lens:** hero tile row, `TradingChart`/equity-curve for the historical scope, the
  "Today at the desk" timeline (reuse the existing `p-timeline` treatment), positions table.
  Consumes C1/C2/C3 through a typed frontend API service; builds against **mocked** contracts.
- **Operator lens:** dominant posture headline consuming the existing dominant-condition +
  `OperatorBlocker` disposition atoms; the forensic transaction grid (the existing 5-column
  `account-desk-transaction-history` grid, moved under the operator lens); collapsed deep
  panels.
- **Evidence reader (Contract C-Evidence):** the shared `clerk-transaction-evidence-drawer`
  keeps its input surface stable (`accountId`, `transaction`, `openerElement`, `closed`) so the
  operator grid and the evidence redesign parallelize. Internals redesigned: `asset-identity`
  header, custody-lifecycle **visual timeline hero**, and instruction/execution + event log +
  raw receipt in collapsed accordions/tables. Labels stay on `receiptLabel`.
- **Deploy drawer:** **container change only** — lift the existing deploy workflow, untouched,
  into a right-side slide-over launched from the desk + Bots list; nav "Deploy" and the old
  route open the drawer over the desk via `?deploy`.
- **Gallery nav:** add an unscoped `/brokers/alpaca/gallery` routed through the existing
  `brokerBotsRedirectGuard` (same pattern as Bots), a **"Gallery"** item in the Alpaca
  `NavGroup`, and `gallery` added to the sidebar active-route regex.

### Instrument identity

- Adopt `app-asset-identity` as the canonical symbol renderer; **write the rule down** next to
  the `receiptLabel` rule. Interface unchanged (`symbol`, `name`, `exchange`, `logo`, `size`,
  `showTitle`) so every surface adopts it in parallel. **Logos on** (compact `size="sm"` in
  tables, full in heroes) — TradingView attribution is covered at `/legal/notices` (PR #1459).
  Make logo-slug resolution more complete/data-driven so common symbols stop falling back to
  initials.
- **Rollout is phased:** the redesigned surfaces (desk, evidence, positions/transaction tables,
  gallery tiles) adopt `asset-identity` **within their own slices**. The remaining ~35 raw-symbol
  sites (research/data/options/engine/portfolio) are a **separate, later sweep**, partitioned by
  feature area into independent slices, reviewed site-by-site to skip `<option>`/dropdown/picker
  spots where a chip is wrong.

### Parallelization & AFK execution strategy

The slices are designed to run as **parallel AFK agents with no human review gate**. Principles:

- **Contract-first coordination.** C1–C4 and the component interface contracts are defined
  *here*, so producers and consumers build **simultaneously** — the frontend codes against
  mocked contracts, the backend against fixtures. No slice waits on another slice's code.
- **Disjoint file ownership.** Each slice owns a distinct set of files. Known hotspots:
  `app.routes.ts` (shell, deploy param, gallery) and `app-sidebar.component.ts` (gallery only)
  — route edits are **append-only** and resolved at worktree merge.
- **Worktree isolation** for every agent, so parallel edits never clobber.
- **Self-verifying DoD** (no human gate): each slice ships its own seam-tests green, passes
  project-scope lint (`ruff` / `eslint --max-warnings 0` / `dotnet format`), and passes the
  thermo-nuclear code-quality review before its PR. A slice that can't self-verify is not done.
- **One convergence point only:** a thin **integration slice** swaps the trader lens's mocked
  C1/C2/C3 for the real endpoints once both sides have landed. Everything else is independent.

## Testing Decisions

Good tests here assert **observable behavior** — what the persona sees and does — not internal
signals or implementation details. Highest existing seam per layer; new seams only where none
exists, and at the highest point.

- **Frontend (all tracks) — Vitest + Angular Testing Library**, mock services at DI, assert
  rendered output. Prior art: `alpaca-desk.component.spec.ts`, `account-desk-*.component.spec.ts`.
  - Lens toggle renders trader vs operator content; default is trader; `?lens=` deep-links;
    operator data lazy-loads on switch.
  - Trader: hero tiles show the metrics; timeline renders trade entries; scope control switches
    Today/30D/60D and renders the equity curve + trade list; symbols render via `asset-identity`.
  - Operator: dominant headline renders with the correct fix affordance per blocker disposition;
    forensic grid renders and a row opens the evidence modal; deep panels start collapsed.
  - Evidence modal: lifecycle timeline stages render; accordions collapsed by default, expand on
    click; `asset-identity` in header/tables; `receiptLabel` applied.
  - Deploy drawer: opens from the desk button and from `?deploy`; closes back to the desk (the
    unchanged workflow internals are **not** re-tested).
  - Gallery nav: sidebar renders the "Gallery" item; redirect guard resolves unscoped → scoped;
    active-route highlights on the gallery URL.
- **Backend historical hybrid — Python / pytest:**
  - **C1 proxy** at the **FastAPI endpoint** seam via `httpx.AsyncClient` + `ASGITransport`,
    Alpaca HTTP mocked with `respx`. Assert the mapped response shape, `int64 ms UTC` timestamps,
    and range→period/timeframe mapping. Prior art: `test_indicators_endpoint.py`,
    `tests/routers/`.
  - **C2 read-model** against a **seeded temp SQLite** fixture: an account-level `from`/`to`
    window returns the expected FIFO attribution rows and window totals; assert on shapes,
    dtypes, ranges.
  - **C3 reconciliation** as a **pure-function** numerical test with **explicit `atol`/`rtol`**:
    agreeing series pass within tolerance; an injected divergence is flagged and classified.
    Prior art: `test_indicator_parity.py`, `qc_reconciler.py`.
  - **C4 snapshot writer** tested directly — appends the expected daily equity row for an Alpaca
    account to SQLite; the scheduler itself is not tested.
- **Ticker rollout:** a slug-map resolver unit test (symbol → slug, fallback to initials) plus a
  couple of representative converted-surface render tests. The ~35-site sweep is verified per its
  own slices, not exhaustively here.

## Out of Scope

- **Deploy workflow internals** — this pass only re-hosts the existing workflow in a drawer;
  the Strategy Lab deploy-content redesign (PRD #917 / ADRs 0020–0021) is separate.
- **The Bot Gallery becoming the single "bots hub"** with a wall/list toggle — explicitly
  chosen as *two peer nav items* for now; the hub reframe is deferred.
- **The full ~35-site `asset-identity` sweep** across research/data/options/engine/portfolio —
  its own later, partitioned effort; only the redesigned surfaces convert now.
- **A fully-sovereign locally-computed equity curve as the displayed spine** — we display the
  broker curve now and only *accrue* sovereign snapshots; promoting them to the displayed line
  (with our own drawdown/Sharpe as first-class metrics) is a future option.
- **Any new user-role/auth model** — the lens is a manual choice, not driven by an identity.

## Further Notes

- **Time is `int64 ms UTC`; the calendar is the source of truth.** All timestamps in C1–C4 and
  the UI are `int64 ms UTC`; the snapshot job's session boundary derives from the canonical
  trading-calendar module, never a hardcoded time. UI renders via the shared timestamp component
  (instants local, date-anchored `date-et`).
- **Numerical rigor.** The reconciliation is the trust mechanism: broker curve (custodian of
  record) vs our independent FIFO books, proven to agree within an explicit, documented
  tolerance — the whole point of "done very well" for an investor-grade account view.
- **`live_instances.py` freeze** and other router line-count freezes still apply — new behavior
  goes in services, routers stay transport-only.
- **Thermo gate:** every slice's first (PR-opening) push runs the thermo-nuclear review and
  addresses all major findings before push.
