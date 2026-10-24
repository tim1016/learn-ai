# ADR 0084: Navigation is place-first: six groups, and an instrument, a signal strategy and an options setup are each a place

Date: 2026-10-23

**Status:** Accepted 2026-10-23

**Vocabulary:** `CONTEXT.md` § "Places (resolved 2026-10-23)" — **instrument place**, **signal strategy**, **options setup**, **Operations**. "Account workspace" keeps the meaning ADR 0064 gave it.

Extends ADR 0064 (Alpaca navigation is account-first). Contexts: PRD #3490 (§2, R01, R31, R32, T01) and slice #3497.

## Decision

1. **Six groups, in this order:** Markets, Research, Signal strategies, Options setups, Accounts, Operations. A group is a trigger and never navigates; its first page is its landing. There is no Documentation group.
2. **A place is where the owner works, and it keeps its identity across the tabs inside it.** ADR 0064 made the account one. An **instrument** (one symbol), a **signal strategy** (one registry key) and an **options setup** (one saved name) are places too. Each is addressed by its URL alone, highlights its group on every tab and detail page, and fails in place, with a way back, when the thing named does not exist. Nothing opens a default instrument, strategy or setup instead.
3. **Options setups are separated from signal strategies by strategy model, not by traded asset.** An options setup is a saved, timed, rule-based setup replayed by Options Backtest and approved through Golden Options. A registered signal program that trades options stays a signal strategy, with its own proof, sealed settings and Golden Search.
4. **Help replaces the Documentation menu.** The top-bar "?" lists every served document (`HELP_DOCUMENTS`), Architecture Manual and Legal Notices included, and a page's "How this works" link opens the one that explains it. A document belongs to no group, so opening one lights none up, and its window title is its own name.
5. **Titles.** A list page's title is its menu label, and the window reads `Page · Group`. An entity page is titled with the entity, its tab and group in the breadcrumb and window (`SPY · Chart · Markets`). A page's breadcrumb leads back to its group and to the list it sits inside.
6. **A lost address fails in place.** An address no page answers opens a not-found page at that address, with a way back to Markets. It is never redirected onto a default page, which drops the query and fragment and hides that the link was wrong.
7. **A move is one redirect.** When a slice moves a page, the old address goes straight to the final one, keeping query and fragment. A redirect that depends on stored evidence (a run, study or version id) resolves its owner from that record, or fails in place keeping the native id.

## Canonical addresses

Later slices build these and redirect old addresses to them. Nothing below exists yet, and no page has moved. Dates and instants in a query are `int64 ms UTC`, except the date-picker form values ADR 0075 allows.

| Place | Address |
|---|---|
| Markets | `/markets` (held instruments and search; `?then=options` or another tab asks which instrument first) |
| Instrument | `/markets/:symbol/(chart\|returns\|options\|data)`; all headlines `/markets/:symbol/chart/news`; dataset builder `/markets/:symbol/data/build`; options views `/markets/:symbol/options/(chain\|surface\|pricing)` with `?source=(snapshot\|live\|archive)&expiry=&at=` |
| Signal strategies | `/signal-strategies` (list); comparison `/signal-strategies/recency`; one strategy `/signal-strategies/:key/(overview\|define\|explore\|golden\|proof)`; Explore `/explore/(backtest\|sweep\|walk-forward)` with the native `?run=` or `?search=`; Golden `/golden/(configuration\|studies\|versions)` and `/golden/studies/:studyId`; evidence whose owner must be resolved `/signal-strategies/evidence/(run\|search\|study)/:id` |
| Options setups | `/options-setups` (list); one setup `/options-setups/:name/(overview\|define\|explore\|golden)`; immutable evidence `/options-setups/evidence/(run\|study\|golden)/:id`; the analytical builder `/options-setups/analysis` |
| Research | `/research`; one study `/research/(feature\|cross-sectional\|signal\|indicator-reliability\|realized-vs-iv\|rule-sandbox)`; demonstrations `/research/demos/(cross-asset\|regimes\|edge-score\|trade-simulator)`; History `/research/history` (`?type=&symbol=&status=&page=`) and `/research/history/(feature-experiment\|signal-report\|spec-run\|walk-forward\|monte-carlo\|baseline)/:id` |
| Operations | `/operations/(lake\|option-archive\|backups\|math-checks\|data-checks)`; data checks `/operations/data-checks/(tradingview\|divergence\|pre-flight)` |
| Accounts, documents | Unchanged: `/brokers/alpaca/…`, `/docs/…`, `/legal/notices` and the other document addresses |

Two rules keep these unambiguous without reserving names. A setup or strategy address always carries its tab, so a bare second segment never names one, and `evidence`, `analysis` and `recency` are never taken for a name. Evidence addresses have three segments after the group, which no tab address has.

## Why

- The owner starts from a thing (a symbol, a strategy, a saved setup, a study), and the old menu started from a tool. Each tool asked which thing again, and one page carried two names (the Stocks menu entry over the Data Lab heading).
- An address that names its place is what lets Back, Forward and reload keep the context, and what lets a link to a run or a version reach the page that owns it.
- Splitting by strategy model keeps two histories, two proofs and two execution paths apart even where they trade the same instruments.
- The Documentation group put thirteen documents in the main menu and tied each to a menu rather than to the page it explains.

## Considered options

- **Keep tool-first groups and rename them.** Rejected: it leaves the repeated "which symbol, which strategy" question.
- **Split signal strategies from options setups by traded asset.** Rejected: a signal program that trades options would move out of the registry that proves and seals it, and a timed options setup has no signal program to prove.
- **Keep Documentation as a seventh group.** Rejected: the help index already lists every document, and a seventh trigger costs menu width.

## Consequences

- Pages keep their present addresses in this slice. The menu file lists the pages that still sit in another place's shell (`pages` on a group), and later slices delete those entries as the pages move.
- The route-inventory spec (T01) holds one row per declared route and the group it must light up. A route added without a row fails it.
- Legal Notices has no footer link today; it stays reachable from the help index. Whether to add a footer is an owner decision.

## Amendment 2026-10-23: the instrument's context rides in its address (#3501)

The instrument place (`/markets/:symbol/chart`, and `/markets/:symbol/chart/news` for every headline) is built.

- **Context keys.** The window, bar timeframe and session are query keys, `?from=YYYY-MM-DD&to=YYYY-MM-DD&timeframe=(1m|5m|15m|30m|1h|4h|1d)&session=(regular|extended)`, and every tab carries them. The two dates are the date picker's form values (ADR 0075; ADR 0022 (f) names the deviation): a host builds `int64 ms UTC` from them at its own request (Data Lab's anchors, UTC midnight to the end of the end date), and nowhere else.
- **Defaults are shown.** A missing timeframe or session reads as `1d` and `regular`. An address with no window opens on the 1Y range Python resolves, shown in the picker and written to the address only when something is committed over it. A window that is half there, unreal or reversed is refused in place with its reason; nothing is substituted for it.
- **Commit rules.** A shared picker commits by moving the address; there is no Apply and no Refresh. A half-typed, refused or reversed entry commits nothing. A commit equal to what is committed does nothing. A distinct, complete context loads once, a request a newer one replaced is ended, and its answer, even one already delivered, cannot fill the newer context.
- **Opening an instrument.** A held instrument opens. A listed one the lake does not hold goes through the one gate of ADR 0066 and opens when the lake confirms the bars (landing on its address is the pick); leaving releases only this place's hold on the fill. An address that names no symbol the lake can hold fails in place, with the way back to Markets.
- **Old addresses.** `/data-lab` and `/data-lab/explore` lead to the instrument their `ticker` names, with its window and `trading-session` (now `session`), or to Markets when they name none. `/research-lab/market/news` leads to that instrument's headlines the same way. `/markets?then=<tab>` opens the tab it names for the instrument picked, where that tab is routed, and Chart where it is not.

## Amendment 2026-10-24: Build dataset and saved setups live under the instrument (#3504)

The Data Lab's dataset builder and its saved setups are the instrument place's. Nothing under `/data-lab` is a page of its own any more: every address there is a redirect.

- **Build dataset** is `/markets/:symbol/data/build`: a secondary action in the instrument's header on every tab, and a page under Data, which stays the open tab. It reads the workspace the place already holds: the instrument, window, bar timeframe and session the address commits, the forward-fill and split-adjustment policy, and the indicator recipe the Chart tab holds. Companion options, output formats, the plan's provenance, progress, cancel and download are the builder's as before. Opening the page asks Python for a plan and starts no job; only its Generate button does. The run dock sits in the place's shell, so a job started here stays in view on the other tabs, and its saved expanded/collapsed choice keeps its key, `run-dock-expanded:data-lab`.
- **Saved setups** are the records `/api/data-lab/sessions` holds, unchanged. A header action opens the drawer (list, save, update, rename, delete). Opening a setup reads that one record, gives the workspace its recipe, scope and saved chart through `DataLabWorkspaceStore.restore` (schema v2, the validator that names what it drops), and moves the address to the setup's instrument, window, timeframe and session, so the setup opens under the instrument it is for. Its saved chart becomes the answer the Chart tab already holds, so no chart request is made; a chart whose timeframe the place does not offer, or that lacks a part the chart draws from, is not drawn, and the page says so. Whatever the page could not take is named in a "Saved setup" notice and stays in the record: opening never writes. "Update setup" replaces only the setup of the instrument the place is on.
- **`?sessionId=<id>`** on an instrument address asks for the same opening. The id is spent on arrival: the address that results carries the setup's own window, so Back, Forward and a reload never open it twice. The record names the instrument, so an address that claims another is moved to the record's, never rendered under the wrong header.
- **Old addresses.** `/data-lab`, `/data-lab/explore`, `/data-lab/export` and `/data-lab/returns` lead to the instrument their `ticker` names (Chart, Chart, Build dataset, Returns), with its window and `trading-session` (now `session`) and every other key kept, or to Markets when they name none. `/data-lab/validate`, and a `mode` that names Validate, lead to Operations → Data checks → TradingView comparison with their scope; an archive-card anchor (`#option-archive-…`) leads to the Operations tab that holds that part of the card, query and fragment intact. A key the Chart, Returns and Data tabs do not read is named in the place's "Migrated link" notice. A `sessionId` with no `ticker` reaches Markets, which does not read it: a redirect cannot read the record that names the instrument.
