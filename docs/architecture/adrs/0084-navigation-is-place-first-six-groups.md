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
