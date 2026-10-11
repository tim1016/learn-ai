# PRD: SPY intraday options backtesting in Strategy Lab

**Status:** Draft for owner review. Recommended defaults below are proposals, not accepted ADR changes.  
**Date:** October 10, 2026.  
**Scope:** Historical research and reproducible results.  
**Confirmed owner choices:** Existing Options Starter subscription; compare no later retry against one retry; profit lock off for both variants; same-day expiry allowed; selecting a historical date must show the available options and their daily, weekly or monthly expiry type.  
**Architecture baseline:** `aa06fae49af5eaf36cda69541458575253036564`; current implementation inspected alongside accepted decisions.  
**Document home:** The GitHub issue is the working PRD. This local copy is an uncommitted review artifact, not documentation to merge into master.

## 1. Outcome and recommendation

Add an **Intraday Options** mode to Strategy Lab. A researcher can choose a historical trading date, inspect the available contracts with expiry-type labels, configure two independently triggered SPY option legs, check historical data coverage, run a reproducible simulation, inspect every entry and exit, and compare profit, loss, drawdown and win rates across saved runs.

**Best option:** Build a focused options replay capability within the existing Python Engine Lab ownership boundary. Reuse the calendar, option identities and selection rules, historical data lake, job transport, canonical money/statistics functions, and Python-owned backtest history. Use the owner's existing **Options Starter** plan: observed minute trade-price bars over verified coverage, with no historical bid/ask dependency. Show the simulation's sampling and fill assumptions prominently. Quote replay remains a future extension, not a first-release requirement.

This is a SPY adaptation of the described strategy. It is not a claim of numerical equivalence to AlgoTest's proprietary simulator or to NIFTY performance.

| Rank | Approach | Benefit | Cost or limitation |
|---|---|---|---|
| 1 | Integrate Options Starter minute bars into Strategy Lab and the Python engine | Fits existing ownership; uses the current subscription; creates inspectable results quickly | Approximately two years of fetchable history under published terms; no measured bid/ask spread or tick order |
| 2 | Use the same architecture, but acquire historical NBBO data before shipping results | Better spread and fill evidence; purchased archives may reach 2017 | Additional data cost and ingestion work; minute quote snapshots still miss intraminute paths |
| 3 | Prototype with LEAN's options support as the main execution engine | Can help construct an independent reference experiment | Adds execution and data-format integration; still needs licensed historical options data; weaker fit with Python sovereignty |

Model-generated premiums from spot and IV are excluded from historical performance claims. They may become a separately labelled scenario product later.

## 2. What the existing system provides

These are verified integration points, not assumptions that the new strategy already runs.

| Concern | Existing authority or implementation | Required integration |
|---|---|---|
| Historical strategy execution | `PythonDataService/app/engine/engine.py`, `app/services/engine_backtest_service.py`; engine authority map | Add a typed options replay branch with its own position/fill adapter; preserve stock semantics |
| Long-running work | `Backend/Jobs/JobsApi.cs` forwards public jobs; `app/routers/jobs.py`, `app/jobs/runner.py`, Redis progress/cancellation | Reuse this transport and progress lifecycle; keep simulation and validation in Python |
| Memory admission | `app/engine/run_gate.py` | Use the existing process-wide execution gate; stream sessions instead of loading years of chains |
| Saved results | `app/research/backtest_runs/{records,repository,engine_payload}.py`, `app/research/persistence/schema.py`; ADR 0058 | Extend the existing run with typed options evidence and leg records; use Python research migrations |
| Calendar | `app/lean_sidecar/trading_calendar.py`; ADR 0022 | Resolve every session and deadline here, including DST and early closes |
| Contracts and selection | `app/options/{contract,expiry,resolver}.py`, `app/schemas/action_plan.py`; ADR 0076 | Reuse exact identities, ATM tie-breaking and expiry meaning; separate pure selection from live-only quote admission where necessary |
| Historical source access | `app/services/polygon_client.py` provides contract reference and aggregates | Extend bounded, complete point-in-time retrieval; its capped contract-list helper alone cannot prove a complete historical chain |
| Historical storage | `app/data_lake/`; ADR 0049 | Add explicit option artifact identity and admission; do not encode options as stock bars under `equity/` |
| Money and statistics | `app/broker/alpaca/clerk/fifo_pnl.py`, `app/options/exact.py`, `app/engine/results/statistics.py` | Reuse pure computations with option multipliers; never construct a Clerk or write custody data for research |
| Fee estimates | `app/broker/alpaca/option_fees.py` | Reuse date-qualified rates where covered; preserve unknown rates as unknown |
| UI and history | `Frontend/src/app/components/strategy-lab/`, `BacktestRunsService` | Add an options mode, report detail and comparison using the existing run identity |
| Historical chain inspection | `Frontend/src/app/components/data-lab/past-chain-inspector/`, `Frontend/src/app/services/past-chain.service.ts` | Reuse the chain presentation and detail drill-down; replace guessed same-day symbols with the shared Python historical contract query and keep trading date separate from expiry |

**A key domain distinction:** the glossary's **option structure** opens its legs in one order. Here the call and put may enter at different times, and sometimes only one enters. Represent this as an **intraday option strategy with independent legs and shared exit rules**, not as an existing atomic two-leg structure or a live multi-leg order. Reuse leg selectors without broadening `ActionPlan`'s existing execution promise.

The existing `app/research/options_runner.py` researches daily option/IV features. `strategy_engine.py` analyzes hypothetical payoff and Greeks. Neither is an intraday contract-level historical simulator. The lake's `derived_quote.py` synthesizes zero-spread quote files from trades; those files are not observed bid/ask evidence.

The current past-chain service constructs candidate OCC symbols with expiry equal to the analysis date, filters strikes by daily-bar availability, and can substitute another day's bar when the requested day is absent. That path cannot serve as the backtest's historical contract authority. Adapt its reusable UI to the admitted Python contract universe; listing eligibility must not depend on whether a contract happened to trade later that day. The existing expirations route also lacks a historical as-of input, so its current response is not proof of historical availability.

Existing authority wins over stale skill prose: Python owns new math, provenance lives with its implementation, and this work creates no separate math registry. No accepted decision or live deployment contract is changed merely by publishing this draft.

Governing decisions: [engine authority map](https://github.com/tim1016/learn-ai/blob/aa06fae49af5eaf36cda69541458575253036564/docs/architecture/engine-authority-map.md), [Python-owned backtest runs](https://github.com/tim1016/learn-ai/blob/aa06fae49af5eaf36cda69541458575253036564/docs/architecture/adrs/0058-backtest-runs-owned-by-python.md), [data lake authority](https://github.com/tim1016/learn-ai/blob/aa06fae49af5eaf36cda69541458575253036564/docs/architecture/adrs/0049-data-lake-is-the-market-data-authority.md), and [option contracts and execution](https://github.com/tim1016/learn-ai/blob/aa06fae49af5eaf36cda69541458575253036564/docs/architecture/adrs/0076-option-structures-on-the-money-path.md).

## 3. The supplied strategy and the SPY adaptation

Keep the original input alongside the normalized configuration. Every changed value must be visible before Run and retained with the result.

| Setting | Supplied value | Recommended SPY behavior |
|---|---|---|
| Underlying | NIFTY cash index | Raw SPY share price for ATM selection; no futures substitution |
| Requested dates | February 11, 2017–October 10, 2026 | Retain the request; offer only calendar sessions with verified data. Both endpoints are Saturdays; report actual included sessions explicitly |
| Strategy type | Intraday | Begin flat each session and require a documented closing outcome; no overnight strategy exposure |
| Observation start | 09:35 | 09:35 New York on normal sessions: session open plus 5 minutes |
| Exit decision | 15:14 | 15:14 New York on normal sessions: session close minus 46 minutes |
| Monitoring end | 15:14 | Same deadline. Cancel pending entries and prioritize closing; closure evidence remains mandatory |
| No re-entry after | 09:35 | Preserve in the imported preset; warn that later stops cannot create a re-entry |
| Call | Buy 1 lot, ATM, weekly | Buy 1 standard SPY call contract, independently triggered |
| Put | Buy 1 lot, ATM, weekly | Buy 1 standard SPY put contract, independently triggered |
| Momentum | Premium rises 25% | Threshold equals 1.25 times the leg's fixed baseline at observation start |
| Stop | 25% | Threshold equals 0.75 times the leg's actual simulated purchase premium |
| Re-entry | Once per leg after stop, ASAP | Support one extra filled entry per leg per session, subject to cutoff and shared-close rules |
| Square-off | Complete | A leg stop closes all active legs; scheduled and overall exits do so too |
| Profit lock | Reach 0, lock 1 | Off for both comparison variants by owner decision; retain the original values as inactive input |
| Slippage | 0.3% per execution | Adverse 30 basis points on each entry, re-entry and exit premium |
| VIX range | 0–150 inclusive | Cboe VIX, with a declared point-in-time sampling policy; never India VIX or SPY IV |

**Required comparison, chosen by the owner on October 10, 2026:** keep the entry/exit times, one contract per leg, 25% momentum, 25% stop, Complete square-off and 0.3% slippage. Compare the original 09:35 cutoff (**no later retry**) with a **one-retry variation** whose cutoff is session close minus 60 minutes, normally 15:00. Each leg may have at most one re-entry in the second variation. Keep the original input intact and show both results side by side. The owner chose to leave the profit lock off in both runnable variants.

The initial cash proposal is **$10,000**, with fixed one-contract sizing and no scaling with profits. It is an editable simulation assumption, not a capital recommendation. Refuse unaffordable entries and record them. Capital-based percentages must name this denominator.

SPY options are American-style and physically settled; a standard contract can deliver 100 SPY shares on exercise. Replacing NIFTY therefore changes the contract economics, not just the ticker. This intraday feature requires closure evidence and makes no cash-settlement substitution. [Cboe option facts](https://optionsfacts.cboe.com/)

### Decisions with alternatives

| Decision | Best default | Second option | Third option |
|---|---|---|---|
| Data | Existing Options Starter minute bars over verified coverage | Later acquire older/quote history | Separate theoretical scenario mode |
| Times | Keep 09:35–15:14 in New York, encoded as calendar-relative offsets | Choose new explicit offsets suited to SPY | Preserve the source market's relative timing after confirming its session convention |
| Expiry browsing — owner requested | Select trading date; show all listed expiries with Daily / Weekly / Monthly labels and a separate expires-today badge | Filter the same list by expiry type | Open an exact contract directly, after validating it against that historical date |
| Date-range expiry rule — proposed default | Nearest listed week-ending expiry including today, visibly named before Run | Nearest expiry in a selected category: daily, weekly or monthly | Expiring today only; record sessions without an eligible listed expiry |
| Re-entry — owner selected | Compare original cutoff with one retry until 15:00 | One-retry variation alone | Original alone |
| Profit lock — owner selected | Off in both comparison variants | Later configure an explicit USD trigger and lower USD floor | Later configure percentages of a declared premium basis |
| Execution detail | Minute-close signals and next-minute-open model | Sampled NBBO with natural-side fills | Event-level trade/quote replay when licensed |

**Owner input, October 10, 2026:** the account has Options Starter, and the owner does not believe quote data is included. Vendor documentation confirms historical quotes are not included in Starter. The draft therefore targets the existing subscription and available observed history; no upgrade or data purchase is required for the first release. Actual account access and usable sessions still require the implementation's data probe. The owner selected the no-retry versus one-retry comparison, profit lock off for both, and inclusion of same-day expiry. The latest requirement is a date-driven options browser that shows each expiry's type; the product must support inspecting and choosing the available expiries instead of imposing one global daily-versus-weekly choice. Both comparison variants use the same saved expiry selection rule.

## 4. Historical data contract

### Coverage comes before performance

The owner confirmed **Options Starter**. Published terms provide minute aggregates, two years of historical data and a 15-minute delay for recent observations. The delay affects when data becomes available, not the minute interval it describes; it does not shift historical decision timestamps by 15 minutes. Historical bid/ask quotes are not included. Use completed historical sessions and trade-price bars. [Starter plan](https://massive.com/pricing?product=options), [historical quote access](https://www.massive.com/docs/rest/options/trades-quotes/quotes)

The five-year assumption in `docs/architecture/options-research.md` and `polygon_history_floor()` must not govern options access. Resolve an options-specific entitlement boundary, validate expired-contract samples and report the actual earliest usable session. As of this draft, the planning range is roughly October 2024 onward, not 2017 onward; existing admitted local artifacts may extend it. This is a planning estimate, not a measured account inventory. [Options aggregate history](https://www.massive.com/docs/rest/options/aggregates/custom-bars)

The first release must work without the historical quotes endpoint, a quote subscription or a higher plan. Snapshot/Greeks access does not establish a historical quote archive. SPY underlying bars and Cboe VIX require their own verified sources; an options subscription does not by itself prove access to either.

IBKR remains the required live market-data provider. Its documented historical limitations include expired options; it cannot be assumed to backfill the requested archive. Alpaca remains the account/order provider, and direct Alpaca market-data subscriptions remain prohibited. [IBKR historical limitations](https://interactivebrokers.github.io/tws-api/historical_limitations.html)

Cboe advertises historical option quote intervals from January 2012 and NBBO snapshots with trade summaries. This is a future alternative for older coverage, subject to access and sample validation; it is not part of the Starter release or proof of purchased coverage. [Cboe Option Quotes](https://datashop.cboe.com/option-quote-intervals)

Preflight must report requested dates, eligible sessions, verified sessions, available granularity, observed trade/quote sources, missing contracts, missing observations, entitlement failures and the latest complete published session. Never silently shorten a request. The UI may offer **Run available dates** as an explicit new normalized request.

### Required inputs

1. **Raw underlying bars:** SPY minute bars with the correct session and adjustment basis. Do not compare historical raw strikes against split-adjusted prices from the default stock research path.
2. **Historical contract universe:** contracts listed by each selection instant, with expiry, right, exact strike, multiplier, deliverable and style. Add the normalized expiry category, its evidence source and classification version; preserve the provider's original program label when supplied. A present-day chain is not a historical universe. Fully exhaust relevant pagination; a truncated search returns `CHAIN_INCOMPLETE`. The current contract helper supplies call/put and expiration but no daily/weekly/monthly category; do not assume a ready-made provider field exists.
3. **Observed option prices:** Starter minute trade bars for v1; sampled NBBO and event quotes, if added later, remain distinct data kinds. Keep contract identity, interval boundaries, observation time, source, resolution, quality flags and content hash. Convert vendor bar-start timestamps to the repo's bar-close convention once, at ingestion. Historical second-level bars are not assumed from a plan's streaming capabilities.
4. **VIX observations:** the proposed first version uses the previous completed session's published Cboe VIX close, known by the observation start. Preserve the observation and availability evidence. Never filter a morning trade on that same day's future close. An intraday VIX mode requires its own historical coverage.
5. **Reference facts:** standard-contract status, expiry session, and any relevant contract adjustment. Refuse adjusted/nonstandard contracts in v1; do not assume a multiplier from the symbol.

As-of-date contract reference is not necessarily intraday listing-time evidence. If a source cannot establish that a candidate existed by selection time, expose that limitation and withhold a strict point-in-time claim. Using a pre-selection observed trade can provide an existence witness, but it does not prove an otherwise truncated strike ladder complete.

### Storage and admission

Extend the lake's existing file publication, hashes, leases and committed-catalog admission. The identity includes asset class, underlying, OCC contract where applicable, session, raw/adjustment basis, data kind, provider, sampling policy and schema version. Chain-reference and VIX artifacts are distinct from option prices. Observed and synthesized data must never share an identity.

Current equity paths and catalog constraints require explicit evolution; this is not a pass-through ticker change. Keep catalog schema changes with its current migration owner and update `catalog_schema.py` drift expectations. Research result tables continue to use the Python research migration ledger. Do not start a second market-data database or a new provider framework.

Reuse committed local bytes before downloading. Do not reject an already admitted historical artifact solely because the provider's rolling fetch window has since moved. Pin exactly which bytes each run consumed and refuse a replay whose bytes are missing or changed. Do not introduce catalog scan-and-rebuild behavior contrary to ADR 0049.

A missing minute may mean no qualifying trade; it is not automatically a vendor outage. Distinguish a successful empty interval from entitlement, transport, truncation or corruption failures. Never interpolate premiums or forward-fill a signal/fill. [Vendor aggregate behavior](https://www.massive.com/docs/rest/options/aggregates/custom-bars)

If necessary baseline or active-position evidence is missing, retain an incomplete diagnostic result. Never drop a losing or unclosable day from headline performance. Narrowing dates must happen visibly, independently of subsequent profit or loss. A run with zero evaluable sessions is unavailable, not a zero-return strategy.

## 5. Deterministic strategy behavior

### Selection and first entries

- Resolve session offsets through the canonical calendar. Early-close sessions use the same offsets; for a 13:00 close the proposed exit decision is 12:14. Display effective times before running. Skip a session whose selected deadlines leave no usable interval, with a reason.
- At observation start, use the raw SPY bar that closes at that time. Select the nearest listed strike using the existing resolver's lower-strike tie break. Require both the call and put at the selected strike and expiry; do not silently substitute a different strike for one leg.
- Freeze each initial contract and its own premium baseline. Do not continuously roll ATM while waiting for momentum. Each leg can enter independently after its baseline observation and strictly before the exit deadline; zero, one or both legs may enter.
- The owner permits expiry on the decision date. For a one-session experiment, allow an exact listed expiry chosen from the historical browser. A date-range run must save a repeatable rule, not reuse that one contract forever: proposed default is the nearest listed week-ending expiry on or after each session; alternatives are nearest listed expiry within a selected category or the existing `same_day` rule. Category-based selection chooses the earliest eligible expiry on or after that session with the required category and a valid closing deadline. Preview the rule and selected expiry for each session before Run. No synthesized expiries, silent fallback to another category/date, or modern daily-expiry schedule projected backward.
- Preserve the existing `nearest_weekly` meaning (strictly after today) for older configurations. Introduce an explicitly named research selector for week-ending including today. A monthly or quarterly expiry can count as week-ending; the rule is a schedule, not a promise that the contract's category is Weekly. Keep the actual category visible. An exact-expiry choice does not silently become a rolling rule when the user expands the date range; require a visible rule choice first. Re-entry uses the saved rule, and both comparison variants share it.
- Same-day eligibility never extends the contract's closing boundary. Derive expiry deadlines through the existing canonical calendar/deadline functions and use the earlier of the strategy's scheduled close and any applicable contract close. Incomplete closure stays incomplete; no automatic exercise or synthetic cash settlement makes the run appear flat.
- Buy when the selected signal price is at least `baseline × 1.25`. Slippage changes the fill, not the frozen baseline. A gap above the threshold buys at the model's next executable observation, not at the crossed threshold.

AlgoTest describes premium momentum from the selected option's price at the entry setting, and ASAP re-entry selects a then-current strike. Those are reference behaviors, not evidence about its undisclosed event ordering. [Momentum](https://docs.algotest.in/Time-Based-Algo-Trading/How-to-Master-Backtesting-on-AlgoTest/features/leg/), [ASAP re-entry](https://docs.algotest.in/Time-Based-Algo-Trading/How-to-Master-Backtesting-on-AlgoTest/features/re-entry/)

### Stops, closing and re-entry

- A filled long leg stops when its signal price is at or below 75% of its actual simulated buy premium, including entry slippage and excluding fees. The stop requests a sale; it guarantees no sale price.
- **Complete** closes every active leg after any leg's stop. Cancel all pending first-entry orders and untriggered legs in that round. Record a common exit group and each leg's own exit price/time; a shared decision is not proof of simultaneous venue fills.
- If a Complete stop is eligible for re-entry, previously active legs closed by that stop group each may use their remaining one-entry allowance. Previously unfilled legs remain cancelled for the day. This is the proposed SPY rule, not an asserted AlgoTest parity result. Test it explicitly before release.
- Re-entry selects a new current ATM contract using the same expiry rule; it does not reuse the old contract by accident. ASAP means the first subsequent admissible observation after every active leg in the exit group has closed. It does not wait for a second 25% rise. Each new fill gets a fresh 25% stop.
- The cutoff tests the stop event that creates re-entry eligibility. A stop after the cutoff cannot retry; initial momentum entry is not blocked by this cutoff. A qualifying retry must still fill before the overall exit deadline. AlgoTest documents this distinction. [No Re-Entry After](https://docs.algotest.in/Time-Based-Algo-Trading/How-to-Master-Backtesting-on-AlgoTest/features/no-re-entry-after/)
- At most two filled entries per leg per session. Failed attempts do not consume another filled-entry allowance, but cannot loop indefinitely: one pending attempt expires after one minute in the initial profile. No entry is allowed while a shared close is unresolved.
- Scheduled exit, monitoring end, or overall profit-lock exit ends entry eligibility for the entire session. No profit-lock re-entry is implied by the per-leg stop-loss setting.

**Ordering:** at each decision boundary, resolve fills already due; process the scheduled exit fence; value positions and evaluate overall exits; evaluate leg stops and their shared close; then consider eligible entries. Exits suppress new entries at that boundary. Simultaneous leg stops produce one close group and cannot double-sell. Provider sequence breaks ties when available; otherwise use documented batch ordering and stable leg identifiers.

### Profit lock

**Owner decision, October 10, 2026: leave the profit lock off for the first comparison.** Persist the disabled state in both normalized configurations and display it on both reports. Disabled lock values never arm, change state, trigger a close or block an otherwise valid run.

The supplied `trigger=0, locked_profit=1` cannot represent a realizable locked-profit floor under the proposed rule. Preserve those values only as inactive original input. If the researcher later enables a lock, require valid values before running; a later enabled experiment has a different saved configuration from this comparison.

For the initial USD lock, require `0 ≤ floor < trigger`. Arm once overall session net liquidation P&L reaches the trigger. Thereafter request a complete exit when it falls to or below the floor. Include closed-leg realized P&L, current liquidation value, slippage and known fees. A market gap may realize less than the floor. No new lock is armed while all legs are flat. Locking is distinct from continuously trailing; the supplied request does not specify a trailing increment. [AlgoTest lock description](https://docs.algotest.in/Time-Based-Algo-Trading/How-to-Master-Backtesting-on-AlgoTest/features/overall-strategy-settings/)

An enabled lock requires a complete valuation and a defined fee policy at every evaluation. Unknown marks or unknown fees cannot be treated as zero to trigger or avoid an exit; they make the session incomplete. Under an explicitly selected fees-not-charged scenario, the lock is also before fees and the report says so.

### Two declared simulation profiles

**Observed minute trades — initial release.** Premium momentum and stops use completed minute closes. Market orders fill at the next minute's observed open with adverse slippage. Persist that this is an interval-based fill convention, not a measured tick timestamp. The scheduled exit uses the next interval beginning at the deadline. Fill evidence must arrive within that interval; otherwise the session is incomplete. High/low values may illustrate possible intraminute triggers, but never invent an order between entry and stop. A close that stays below a momentum threshold does not enter merely because that bar's high touched it.

**Historical NBBO — future extension, unavailable on Starter.** Use observed quote midpoints for signal thresholds, ask for buys and bid for sells, with adverse slippage added separately. Keep quote observation, signal and fill times separate. Snapshot replay is labelled with its sampling interval; event replay preserves source order. Archived quotes are historical evidence, not `QuoteDataType.LIVE` fabricated to pass a live admission check. Reuse pure selection and price calculations behind an explicit historical evidence adapter. Pin freshness, quote coherence, size and partial-fill assumptions in this profile before enabling it. This profile is not selectable in the Starter release and its absence never blocks minute-bar replay.

These profiles can make different decisions because their signal series differ. Compare them as declared sensitivity cases, never as identical data with a hidden fallback. Tick-accurate continuous monitoring is outside the minute profile's claim.

Every Starter report must state **Historical trade bars; fills simulated; bid/ask spread unavailable**. The requested 0.3% slippage is an assumption, not a measured spread or evidence of obtainable fills. Support comparison with a higher user-selected slippage assumption through the same saved-run workflow. Do not synthesize bid/ask observations from OHLC or infer option fillability from bar volume alone.

### Money and results basis

For premium `p`, buy fill is `p × 1.003`; sell fill is `p × 0.997`, before the declared tick/rounding policy. Round adversely to the contract's admitted price increment; retain the unrounded value. A standard contract uses a recorded multiplier of 100. Round-trip P&L is `(sell − buy) × contracts × multiplier − fees`, implemented through the canonical exact-money/FIFO boundary, not duplicated in Angular or .NET.

Support three explicit fee policies: date-qualified canonical Alpaca estimates; a researcher-specified per-contract fee scenario; and **fees not charged**. Missing historical fee rates are not zero. The baseline may use fees-not-charged to match the supplied inputs, but its headline must say **after slippage, before fees**. An after-fee claim requires complete fee evidence. Do not apply a 2026 rate schedule across 2017–2026 silently.

Cash cannot go below zero through an entry. Reserve for both pending legs when needed; when simultaneous triggers cannot both be afforded, reject that entry group instead of favoring arbitrary iteration order. Profits do not change fixed contract count. Every admitted closed position must reconcile with cash and the equity curve.

## 6. User experience and reporting

1. Open Strategy Lab and select **Intraday Options**. Options Lab may link here without creating a second builder or run store.
2. Load **SPY ATM premium momentum**. Read a plain-language strategy summary with all times marked New York and quantity shown as contracts.
3. Set a **Trading date**. Automatically load the historical expiry list and show **Daily**, **Weekly** or **Monthly** beside each expiry, plus days remaining and **Expires today (0DTE)** where applicable. Select an expiry to inspect its calls and puts, strikes and observed price coverage. Default to showing all expiry categories.
4. Edit the date range, timing, two legs, exits, retry, profit lock, VIX and execution assumptions. For a range, display the rolling expiry rule and a session-by-session selection preview. Python validates the runnable configuration; Angular provides immediate field feedback without calculating strategy math.
5. Select **Check data**. Review exact coverage and estimated retrieval work. Missing access, invalid lock settings and incomplete chains have actionable messages. No purchase or account change happens here.
6. Run with progress for data, simulation, analysis and saving; cancellation leaves a clearly cancelled diagnostic result. A lost connection does not justify an automatic duplicate submission.
7. Run the selected **No retry vs one retry** comparison, inspect both overviews, daily results, leg trades and a selected session timeline. Save notes and reopen either run or the pair.

### Date-driven options browser

**Recommended interaction:** trading date → available expiry dates with type labels → call/put chain → strategy selection. This satisfies the owner's request without asking them to know the contract symbol in advance. The preview uses the strategy's observation-start instant, normally 09:35 New York, and displays that time. A historical trading date and a contract's expiry date are separate controls; changing one must not silently overwrite the other.

| Display | Required behavior |
|---|---|
| Trading date and preview time | Use the historical as-of universe and only observations available by that instant. Weekends/holidays show no trading session and offer nearby sessions without changing the requested date automatically |
| Expiry list | Show all then-listed, eligible expiries in ascending date order, with bounded pagination and visible completeness. A limited strike preview must not masquerade as the full chain |
| Expiry type | Daily, Weekly or Monthly from evidenced classification; preserve Quarterly / End-of-month / Other where applicable, and show Unknown when evidence is insufficient |
| Time remaining | Show calendar days to expiry computed in New York by Python, plus an independent Expires today (0DTE) badge. A monthly contract can expire today without becoming a Daily contract |
| Contract rows | Calls and puts around the selected ATM, exact strike, contract identity, multiplier, selected expiry/type and eligibility. Provide pagination/drill-down for additional listed strikes; the strategy's ATM rule still controls selection |
| Price and coverage | Show an observed trade-bar price with its interval, observation time and age, or a clear unavailable state. Keep bid/ask unavailable on Starter. A stale preview price cannot become a baseline or fill; execution still requires the exact admitted observations in section 5 |
| Selection and refresh | Show the chosen expiry, category and rule before Run. Changing date/time/underlying invalidates previous results and selection until revalidated; late responses from an older request cannot overwrite the current chain |

The browser is listing-first: an otherwise eligible contract stays visible when its historical prices are missing or outside the account's entitlement. Show **listed; prices unavailable** rather than claiming the contract did not exist. Distinguish no listed contracts, non-trading date, incomplete reference history, no qualifying trade, and access failure. An incomplete universe is inspectable with a warning but cannot silently pass replay preflight.

**Classification contract:** keep `call` / `put` separate from expiry category. Prefer explicit historical exchange/provider series classification; otherwise use a versioned Python classifier backed by dated exchange rules, actual listed-contract evidence and the canonical calendar. The normalized UI terms must be documented: Daily for evidenced weekday short-term daily series, Weekly for evidenced weekly series, Monthly for standard monthly series. Official program labels can overlap with everyday terminology; preserve that original label in detail. Never classify solely from days remaining, the weekday, an OCC symbol, or today's listing schedule. Holiday shifts and standard-monthly/quarterly overlaps require explicit fixtures. Unknown classification cannot silently satisfy a category filter. Cboe distinguishes short-term daily and weekly series and excludes overlapping standard/monthly/quarterly expirations in its listing rules. [Cboe listing-rule evidence](https://cdn.cboe.com/resources/regulation/rule_filings/approved/2026/SR-CBOE-2026-007.pdf)

For example, **Monthly · Expires today (0DTE)** is a valid combined display: one label describes the expiry series and the other describes its relationship to the selected trading date. Archive the displayed category and its evidence with each selected contract so reopening a saved run does not reclassify history using current rules.

The comparison clones one normalized configuration and changes only the re-entry cutoff. Both variants use the same requested sessions, frozen source-data version, calendar, cash, fees and slippage; each retains the exact artifacts it consumed, which may differ because a retry selects a new contract. Execute through the existing run gate, retain both run IDs and compare matched evaluable sessions. If one variant lacks required evidence, show the failed/incomplete comparison rather than silently comparing different date sets. Report the P&L and drawdown difference, extra entries, additional costs and days whose outcome changed. Do not select a winner automatically from in-sample profit alone.

The report includes:

- Total P&L, costs, starting/final equity, drawdown in dollars and percent, trade count, trade win rate, daily win rate, profit factor, average gain/loss and consecutive losses.
- Independent leg round trips as the headline **trade** unit; profitable trading sessions divided by traded sessions as **daily win rate**. Flat/no-trade days, break-even trades and missing sessions are separate counts. Undefined ratios are null with a reason.
- Intraday mark-to-market and closed-trade realized equity, with their bases named. Minute results cannot claim to capture drawdowns between observations. Undefined marks create visible gaps and make affected aggregate claims unavailable.
- Monthly/yearly tables and per-leg attribution, plus counts of no momentum, VIX exclusion, unaffordable entry, blocked retry, missing data and incomplete closure.
- A session timeline containing underlying price, both selected premiums, baseline/thresholds, contract changes, fills, costs and shared exit reasons. Show data timestamps and evidence on drill-down.
- Full paginated trade/event inspection and CSV/JSON export; the existing 500-trade report truncation must not silently truncate exports or statistics.
- Comparison that names differences in data, dates, expiry rule, resolution, fees, fills, cash and parameters. Separate in-sample development from a frozen out-of-sample date range; no optimizer is required in v1.

The page must never label research performance as deployable validation merely because it has a saved run ID. Live or Paper deployment of this independently triggered strategy is a separate future feature.

## 7. Implementation boundaries and contracts

Proposed additions, with names subject to the repo's existing conventions:

- `app/schemas/intraday_option_backtest.py`: versioned typed configuration, timing offsets in integer milliseconds, exact-expiry or rolling selection rule, two leg rules, pricing profile, VIX policy and validation errors.
- Extend the shared historical options query with a numeric as-of instant, expiration identity, complete/paginated contract references, observed-price coverage and typed category evidence. Put expiry classification and days-to-expiry calculations in the existing pure Python options boundary. Reuse this query for Strategy Lab and the past-chain inspector; Angular displays its results and does not generate candidate contracts or duplicate classification math.
- `app/engine/options/`: small deterministic session/leg state machine plus historical fill/portfolio adapters. Consume admitted observations and emit decisions, fills and evidence. No network, wall-clock reads or broker clients in the decision core.
- `app/data_lake/` option materialization: point-in-time listing, option-price and VIX artifacts using existing publication/admission machinery. Reuse the Polygon transport rather than creating another client.
- A thin service composes preflight, materialization, the existing execution gate, simulation, statistics and persistence. Keep options branching out of unrelated stock algorithms and avoid a platform-wide engine rewrite.
- Add one routed job type through `JobsApi` and `/api/jobs-internal/` using existing progress/cancellation. Preflight and result-detail routes follow the current FastAPI/OpenAPI conventions. Browsers do not call internal job routes directly.
- Keep `source=engine` / Python ownership and add a versioned result-kind discriminator. Extend saved evidence with session/leg/entry-attempt/exit-group IDs, OCC contracts, expiry/category evidence and classification version, multiplier, baseline, signal/decision/fill times, source intervals, fill policy, reason codes and costs. Existing stock readers remain compatible.

Reuse `/api/research/backtest-runs` and its run identity. Add typed child records or a versioned options evidence envelope as appropriate for pagination; never flatten two different contracts into the current symbol-free `TradeRecord` and lose identity. Record the normalized configuration, original input, strategy implementation version, calendar version, data hashes, fee policy and metric basis. A saved report reads its stored evidence, not today's contract chain.

ADR 0058's best-effort saving remains: a finished calculation may have a null run ID after a save failure. The UI must say **result not saved**, offer the complete export, and avoid offering persistent replay/compare until a save succeeds. Do not silently change all backtest persistence semantics for this feature.

All temporal wire/storage fields are integer UTC milliseconds. Use existing ET-midnight date anchors for requested trading dates and `OptionExpiryMs` for contract expiry identity; neither anchor substitutes for the calendar's actual closing instant. Human time strings are display/input only. Preserve camelCase on the existing job/read contracts and generate types through the repo's contract workflow.

When implementation assigns the options replay job to Engine Lab, update `engine-authority-map.md` in that PR. Add resolved glossary terms only when accepted. An ADR is warranted only for a durable ownership/semantic decision, not to duplicate this PRD.

## 8. Acceptance criteria

| ID | Observable proof |
|---|---|
| A01 | Both comparison variants persist and display profit lock off; inactive original 0/1 values neither affect decisions nor block the run, while explicitly enabling that invalid pair is refused |
| A02 | At baseline $2.00, $2.49 does not trigger and $2.50 does; a jump fills from the declared later observation, never at an invented $2.50 |
| A03 | Each stop derives from its own actual entry fill; entry slippage, exit slippage and fees reconcile exactly with cash |
| A04 | Call-only, put-only, both-leg and no-entry sessions produce the right independent positions and counts |
| A05 | A stop in one leg closes every active leg under Complete, cancels pending entries and never closes a leg twice |
| A06 | A stop after 09:35 cannot retry in the faithful preset; first momentum entry after 09:35 remains allowed |
| A07 | The one-retry variation selects the then-current ATM, bypasses initial momentum once, respects both leg budgets and the exit deadline, and cannot enter while a shared close is unresolved |
| A08 | Both comparison variants use the owner-selected expiry rule and permit today when that rule selects it; the existing strictly-after-today weekly selector keeps its meaning. Missing requested expiries never silently fall back, and holiday weeks, DST, early closes and expiry-day deadlines derive from calendar/listing fixtures |
| A09 | A future-listed strike, current-chain substitution, truncated chain, adjusted SPY basis or nonstandard contract cannot silently enter an accepted replay |
| A10 | A minute whose high crosses entry and low crosses stop does not generate an invented intrabar trade; the declared minute-close model determines the result |
| A11 | VIX 0 and 150 are included, values outside excluded, and missing or future VIX evidence never becomes a passing observation |
| A12 | At exit/monitoring end, all pending entries cancel; an unpriced close produces incomplete evidence rather than a fabricated flat portfolio or a dropped losing session |
| A13 | Costs apply to every execution including retry; unknown historical fee rates never produce an after-fee result |
| A14 | Repeating identical admitted inputs, versions and configuration reproduces the same decisions, fills and metric payload, apart from operational IDs/durations |
| A15 | All public timestamps are numeric; expiry identity and effective trading deadline remain distinct; UI timezone cannot alter a run |
| A16 | Saved options runs reopen with contract detail; stock history still reads correctly; exports include all trades and cancelled/incomplete runs never masquerade as completed performance |
| A17 | Headline trade win rate and daily win rate use distinct documented denominators; equity, fees and leg P&L reconcile without double-counting re-entry |
| A18 | The entire historical path runs without an IBKR/Alpaca execution client or a custody writer, and historical quotes are never disguised as live quotes |
| A19 | Starter preflight and replay work with historical quote access unavailable; the two-year fetch boundary is options-specific, recent-data delay preserves original observation times, and reports identify unknown spreads and assumed slippage |
| A20 | The owner-selected no-retry/one-retry pair changes only the cutoff, saves both run IDs, compares matching sessions and identical cost assumptions, and exposes an incomplete variant rather than silently narrowing its sample |
| A21 | Choosing a historical trading date automatically shows its then-listed expiries and call/put contracts; choosing a later expiry leaves the trading date intact. Future-listed contracts never appear as eligible, and old asynchronous responses cannot replace a newer selection |
| A22 | Independently sourced fixtures establish Daily, Weekly and Monthly labels, including holiday shifts and overlapping programs; unknown/other series are not guessed. A monthly expiry on the trading date displays Monthly plus Expires today, never Daily solely because DTE is zero |
| A23 | Listed contracts remain visible without price data; missing entitlement, empty intervals, partial chains and non-trading dates have distinct states. No future daily close or substituted prior-day bar appears as the selected instant's price; no quote access is required |
| A24 | One-session exact expiry and range rolling rules are visibly distinct and saved. The preview and engine select identical contracts from identical admitted inputs, category filters never admit Unknown, the same rule is used for both retry variants, and saved reports preserve original category evidence |

Pure money computations require provenance blocks and tolerance-pinned fixtures. Use existing canonical receipts first. New rule semantics need independently derived scenario expectations, not golden files copied from the new implementation. A claim of AlgoTest equivalence additionally requires an authorized exported run/trade trace, matched data and precise settings; until then the label remains **SPY adaptation**. Existing vendored LEAN may supply appropriate independent mechanics fixtures; it is not automatic strategy-parity proof.

Use exact equality for contract identity, state transitions, integer timestamps and exact-money boundaries; numeric comparisons declare tolerances justified by the reused authority. Cover API validation, migration/read compatibility and one UI journey. Follow current local lint/tests and the existing quick/daily CI split. Do not add CI jobs. Math/money implementation receives the required independent thermo review before its first PR push; this draft itself changes no code.

## 9. Delivery sequence and completion gates

| Slice | User-visible result | Gate |
|---|---|---|
| 1. Historical contract browser and preflight | Select a trading date, see listed expiries with Daily / Weekly / Monthly labels, inspect the call/put chain, and see which dates/contracts can be tested on Starter | A21–A23; verify Starter aggregate access and earliest usable session, underlying/VIX sources, category evidence, historical chain completeness and representative ordinary/expiry/early-close sessions; freeze initial profile semantics |
| 2. One-session vertical slice | One fully inspectable SPY day with two independent legs, momentum, stop, Complete close and costs | Independent scenario fixtures, no look-ahead, exact accounting, genuine observed input |
| 3. Full rule set and date range | Re-entry, valid profit lock, VIX, multi-session replay, bounded memory and cancellation | A01–A15 including failure/closure behavior; requested-versus-effective coverage is explicit |
| 4. Saved research workflow | History, detailed report, exports, required no-retry/one-retry comparison and out-of-sample range | Persistence compatibility, complete exports, provenance and replay checks; A16–A20 and A24 |
| 5. Higher-fidelity data, if selected | Sampled/event quote profile and/or older archive | Actual access verified, sample quality and timestamp conventions validated, declared fill/size policies and cross-profile differences visible |

Slices 1–4 are the Starter first release. Slice 5 is deferred and requires a later scope decision; the current release does not wait for it. No completion date should be promised before the data probe; access and completeness determine feasible coverage.

Suggested operational targets for the first release: preflight serves cached coverage promptly; progress updates at least every five seconds while working; cancellation is observed within five seconds of a cooperative boundary; memory is bounded by a session plus selected contracts and fits the existing container budget. Benchmark cached one-year runs on the deployment host and record a performance budget before calling the date-range workflow complete.

## 10. Boundaries and unresolved choices

The first release does not include NIFTY data, unattended optimization, arbitrary short-option strategies, realistic order-book/queue simulation, automatic exercise into shares, or Paper/Live deployment. An incomplete intraday close is reported rather than resolved by borrowing the live Dry Run's synthetic expiry settlement. Existing broker stores and provider settings remain under their current authority.

The data input is established: **Options Starter; no included historical quotes**. The working recommendation is to use its observed minute bars and verified date range, without an upgrade. The owner selected **comparison of no later retry and one retry until 15:00 New York on normal sessions**, with **profit lock off in both variants**, **same-day expiry allowed**, and **date-driven contract inspection with daily/weekly/monthly labels**.

Expiry is now a visible product choice: show all historical categories, allow inspection of a specific expiry, and require a named rule for date-range replay. The recommended initial range rule remains week-ending including today; category-specific or same-day-only rules are alternatives, not inferred owner decisions. This replaces the pending global daily-versus-weekly interview question. Exact historical coverage and reliable category evidence remain implementation discovery gates; neither has been established by publishing this PRD.

All other choices have proposed defaults above. Open design intent stays on this issue; only accepted decisions, operator procedures and source facts earn durable repository documentation.
