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

## Amendment (#3512): a setup with no saved name

A draft that has no saved name (a new setup, an imported file, the setup of a past run) is addressed `/options-setups/_new/(overview|define)`. A saved name must start with a letter or a digit, so no name can be `_new`, and the draft needs no reserved name. Saving it under a name moves the address to `/options-setups/:name/define`. `/options-setups/:name` with no tab, and a tab no page serves yet, are lost addresses and fail in place.

## Amendment (#3513): Explore's views and an opened run

An options setup's Explore is one address, `/options-setups/:name/explore`, and says what it shows in its query: `?view=` is `backtest` (the default, and left out), `history`, `chain`, `performance`, `lineage` or `comparison`; `run=` names the run the view is about, and `comparator_run_id=` the second run of a comparison. History's open run (`?view=history&run=30`) and two runs side by side (`?view=history&run=30&comparator_run_id=29`) are addressable the same way, so an opened run has a URL. The views are not path segments, so the setup's tab stays the third segment and the window title keeps reading `Name · Explore · Options setups`.

A backtest runs the setup **as last saved** and records the saved name; a draft with no saved name has none to run. A run froze its own setup, so nothing edited in Define changes what it shows. Take to Golden is the link `/options-setups/:name/golden?run=<id>`: it carries the run's id and approves nothing, and is offered only for a run that froze this setup's saved content (read by exact content, never by name).

## Amendment (#3514): Golden, and where a golden search study lives

An options setup's Golden is one address, `/options-setups/:name/golden`, and says what it reviews and forks in its query: `?run=<id>` is the run under review (what **Take to Golden** carries) and `?parent_study_id=<id>` the study a new plan forks. It holds both ways to an approved version, and the Options Golden Search page is gone: a plan is made on the tab, from the setup as last saved, named by its content hash.

A golden search study is evidence, so it has no saved name in its address: `/options-setups/evidence/study/:studyId` is the study to work on (its research, the comparison and final test, and the approval), and `?view=lineage` its recorded lineage. It opens whether or not a saved setup still holds the content it started from, and says which do. `/options-golden-search/:studyId` ends there in one redirect.

`/options-golden-search` ends at `/options-setups/:name/golden` when it names a setup (`?setup_name=`), carrying the study it forks, and otherwise at `/options-setups?then=golden`, where every saved setup opens on its Golden tab. No setup is chosen for a link that names none.
