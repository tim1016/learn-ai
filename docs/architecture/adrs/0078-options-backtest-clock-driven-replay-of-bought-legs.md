# ADR 0078 — Options Backtest: a clock-driven replay of bought option legs over stored trade bars

**Status:** Accepted 2026-10-12
**Provenance:** Owner decisions in [#3030](https://github.com/tim1016/learn-ai/issues/3030), grilled on 2026-10-11 and 2026-10-12, and the build plan kept on that issue. Each decision says whose it is. The owner's decisions stand. A decision *made for the owner* by the build's coordinator stands until the owner revises it. Facts about the vendor come from a read-only probe of the owner's Options Starter key on 2026-10-12.
**Related:** ADR 0022 (time; its amendment (i) is the option expiry anchor), ADR 0049 (the data lake is the market-data authority), ADR 0065 (a worker's lease is a job's liveness), ADR 0068 (Python owns the math; a duplicate inside Python needs a parity test), ADR 0069 (numerical rigor), ADR 0074 (Golden Search, which this does not change), ADR 0076 (option structures on the money path, and its Deploy warning that options have no backtest).
**Vocabulary:** `CONTEXT.md` § "Options Backtest".

## Context

The owner researches option strategies that trade by the clock. Enter at a set time, pick contracts by a rule, wait for a move, stop out, come back, and be flat by a set time. Until now that took a third-party site that covers only Indian indices (#3030).

Nothing here replayed option contracts minute by minute. Strategy Lab backtests a stock signal on shares. Deploy tells the owner that options have no backtest (ADR 0076). No page could show which contracts were listed on a past date, or what they traded at.

The owner's data plan serves option **trade** bars. It serves no bid or ask. It reaches back two years, and that window moves forward every day. Most of what follows comes from those limits.

## Decision

### 1. A page of its own that tests a setup, not a bot

*Whose:* the owner's. It is a separate page, it does what AlgoTest's backtest page does and looks like it, its underlying can be changed, and Golden Search and Deploy are untouched. *Made for the owner:* the name, the first release's control set, and buy legs only.

Options Backtest is a page of its own. Strategy Lab, Golden Search, Deploy and the Clerk are not changed by it.

It replays a **setup**: a trading window by the clock, and rules on each leg's premium (stop loss, momentum, re-entry). It does not replay what a bot trades. A bot's options follow a stock signal, and a setup has no signal. So ADR 0076's Deploy warning, "options have no backtest here", stays true. This page is not that backtest, and nothing links the two.

The first release buys only. A sold leg needs a margin rule for uncovered options, and this system has none.

A run has no broker client and no custody writer, so research cannot touch an account. The feature takes from the broker packages only the option fee model, its cent rounding and an order-side enum. No test enforces that.

*Rejected:* one SPY strategy inside Strategy Lab, which was the first draft of #3030.
*To change:* testing a bot's real plan is another feature. It needs the signal program, which this replay does not have.

### 2. The replay is a package of its own, outside the engine tree

*Whose:* made for the owner. The PRD said "inside the Engine Lab boundary"; this decision says how.

The replay, the run, the picker and the service live in `app/options_backtest/`. Nothing of it goes under `app/engine/`.

Why. Research studies freeze a digest of the engine tree and a short list of other paths. A file added there moves the code identity that Grid Search and Golden Search freeze, and Golden Search is not to change. The replay is also not engine work: it follows the clock and not a signal. It is not a registered strategy and has no Signal Program.

What it shares with the engine. The canonical calendar. The option fee model. The contract spelling and the expiry anchor (ADR 0076 decision 1, ADR 0022 (i)). The engine's one-backtest-at-a-time gate, which a run takes for its replay because #3030 names it.

*To change:* moving any of it under `app/engine/` moves Golden Search's identity.

### 3. The plan's data decides what a fill is

*Whose:* the owner's, for using the existing Options Starter plan. The rest follows from it and was made for the owner.

Trade bars only, no bid or ask, two years back. So:

- A fill is a trade that printed. An order fills at the open of the next minute's bar. A minute with no trade has no bar, so nothing fills in it. No price is interpolated, carried forward or taken from a neighbouring contract.
- The spread cannot be seen, so a setting stands in for it. Each fill is moved against the trader by a slippage percent (0.3 to start, the figure #3030 gave) and then rounded against the trader to a price step (one cent, a setting on the setup). Decision 9 says how much slippage a result survives.
- Every result says what it is. A banner states that fills are simulated from trade prices, that bid and ask are not available, and what slippage was assumed.
- The lake keeps what it fetches (decision 14).

*Rejected:* fills at the trigger level, which AlgoTest's own examples use. Any price that did not print.
*To change:* a plan with quotes would let a fill be priced against a real spread, and would make slippage a check and not a stand-in.

### 4. The timing rule of a replay

*Whose:* made for the owner. The PRD's sixteen behaviours are defaults the owner may strike. The fill rule is stricter than AlgoTest's own examples.

- A minute bar is named by its **close**. The bar closing at *t* holds the trades in [*t* − 60 s, *t*), and its open is the first of them.
- A rule is read on a bar's close only. A high or a low inside a minute never triggers anything.
- An order decided at close *t* fills at the open of the bar closing at *t* + 60 s. It never fills at the trigger level.
- Inside one minute the order is fixed: fills that are due, then the exit time or else the stops and their square-off, then entries. An exit or a stop decided in a minute means no entry is decided in it.
- A buy whose contract has no bar in its fill minute is dropped. A leg that was waiting on momentum goes back to waiting. A leg that was bought at once has finished that attempt. A sale is tried each minute until its contract trades.
- At the exit time whatever is open is sold, legs still waiting are cancelled, and nothing more is bought.
- A leg still held when the session's data ends is **marked, not sold**. It is valued at the close of the last bar its contract traded in. Its session is incomplete and stays in the totals.
- A setup's times are distances from the open or the close, so a half day keeps them. A session the times no longer fit is skipped, and says so.

Why. A rule decided on a close can use only what had happened by then, so the replay cannot look ahead. The whole rule is short enough for the page to say in a sentence.

*Rejected:* AlgoTest's fill at the trigger level (#3030 notes that the fill rule here is stricter than AlgoTest's own examples).
*To change:* each line is one of the PRD's defaults. Striking one changes the replay, its hand-worked tests and the sentences the page shows.

### 5. One place turns a stored start into a close

*Whose:* made for the owner.

The vendor stamps a bar with the start of its minute. The lake keeps that, as it does for stock bars. Everything that replays is handed bars named by their close. One reader, the lake reader a run uses, adds the minute, for the underlying and for every option. No other code in the feature does.

Why. Above that reader a bar's time means one thing. A conversion repeated in several places is a one-minute look-ahead waiting to happen.

Inside the stored file a bar's time is the lake's own format, milliseconds after New York midnight (ADR 0049 1a). The reader turns it into ms UTC at the read boundary.

### 6. Contract picking is the backtest's own, and the live resolver is not edited

*Whose:* made for the owner. One question is the owner's to answer (Open questions, 1).

The backtest picks a contract with its own pure code, from a stored past listing and the underlying's raw price. No quote is read.

Why. The live resolver picks from live two-sided quotes and refuses a contract without one, for every rule, "at the money" included. A past date has no quotes. Its listing says which contracts existed and nothing more. The PRD planned a seam inside `app/options/resolver.py`, between reading a rule and admitting a quote. Instead the picker stands beside the resolver, so no money-path code changes.

This is a second implementation of the expiry and strike rules. ADR 0068 decision 4 allows that with a reason and a parity test. The parity test runs the resolver and the picker on the same inputs for every rule both have. They differ in two places, on purpose:

- "This week" counts today. The resolver's nearest weekly is strictly after the decision's date.
- "At least N days" allows 0. The resolver's minimum needs 1.

Other rules of the picker:

- Only standard contracts are picked: American, 100 shares, no additional underlyings. That leaves index options out, and they are out of scope.
- A strike rule compares against the underlying's **raw** price, never a split-adjusted one, because a listing holds the strikes as they were listed on that date.
- A listing that may be missing contracts is refused before any rule is read. A pick from half a chain is a guess shown as a fact.
- A pick depends on the underlying's price and never on an option's price. So the contracts a session can need are known from the underlying and the listing alone, before one option bar is read. Check data and the download phase use that set.

*Rejected:* cutting a seam in the live resolver.
*To change:* sharing one implementation means editing the resolver, which is the money path.

### 7. An expiry's series is decided from the calendar

*Whose:* the owner's, for Daily, Weekly and Monthly tags in a date browser (#3030). The method, the added Quarterly, and the floor were made for the owner.

An expiry date carries one of five tags: Daily, Weekly, Monthly, Quarterly, Unknown. They are tested in this order: Unknown (the date is not a session, or the trading date is before a chosen first tagged date), then Monthly, Quarterly, Weekly, Daily. So a Monthly that is also a week's last session is Monthly. The glossary defines each.

"Expires today" is a separate fact beside the series. A Monthly on its last day is a Monthly that expires today, never a Daily.

Why. The vendor's listing carries no weekly or monthly label. A tag is a claim about a date and never about a contract, so it is the same whatever else is listed. The PRD asked for tags "from evidence". The build reads the calendar, because the listing has no label to read. The tag is versioned, so a saved page can say which rules made it.

The floor, 2024-10-01 (`FIRST_TAGGED_TRADING_DATE`), is a chosen cut-off and not an exchange's date. Mid-week expiries reached different underlyings at different times, and an earlier date would be mislabelled by the pattern. No exchange notice was consulted.

*To change:* moving the floor needs a source for the underlying it concerns, and a version bump.

### 8. Money is exact, and the figures are computed once, in Python

*Whose:* the owner's for the reporting definitions (PRD). The exact module was made for the owner.

- All money and prices are `Decimal`. On the wire they are strings of plain digits. In the database they are `NUMERIC`, and the JSON columns hold their text. No float is on the path.
- The statistics are computed once, in Python, in a module of their own. .NET transports and Angular renders (ADR 0068).
- Why a new module. The stock engine's statistics are float, percent-based and built for all-in stock trades. They have no daily win rate, no dollar profit factor and no costs. The one figure both have, the drawdown fraction, is held to the engine's by a parity test at `atol=1e-12`.
- Break-even trades, break-even sessions, sessions with no trade and incomplete sessions are separate counts, so nothing hides in a rate. A rate keeps its numerator and denominator, with one rounded figure named as rounded.
- Drawdown is measured at session ends, on equity that starts at the starting cash. It does not see a fall inside a day.
- An unknown is never a zero. A fee with no published rate raises. A missing day raises. A profit factor with no losing trade is none, and so is a rate over nothing.

*Rejected:* reusing the stock engine's statistics.

### 9. Slippage tolerance is replayed, not re-priced

*Whose:* made for the owner. The PRD asked for a re-pricing of one replay's fills, marked approximate.

A run is replayed once per value of a slippage ladder (0 to 5 percent by default, with the setup's own value among them), on the same loaded data. Each value carries its own cash. The tolerance is the highest value at which, and at every lower value, net profit is above zero. When every value profits it reads "survives the whole ladder", not "survives any slippage".

Why. A different slippage moves a stop level and the minute a re-entry starts, and it can turn a cash refusal. Re-pricing one replay's fills would be wrong exactly where it matters. The replay is exact, so nothing is marked approximate.

*Rejected:* re-pricing with an approximate flag.

### 10. Fees: three policies, and an unknown fee is never nothing

*Whose:* the owner's for a fee policy on the setup. The rate boundary and the rounding were made for the owner.

A setup charges no fee, a fee per contract, or an estimate. The result says which.

Estimated fees come from the canonical dated option fee model (ADR 0076 decisions 13 and 16). They exist only from the first date the model has a published rate for every fee type of an option fill. A session before it cannot be tested with estimated fees. Check data says so, and a run refuses before it reads anything. The owner can choose a fee per contract or none for those dates.

Each fee type of each fill is rounded up to a cent. The model settles a whole day and rounds once. So a day charged per fill is never less than the model's settlement, and at most n − 1 cents per fee type more over n fills. The estimate leans high by cents a day and never low. This is an accepted divergence.

*Rejected:* charging nothing where the model has no rate.

### 11. Contracts are fixed in number, and an unsold leg keeps its money in the totals

*Whose:* made for the owner. The PRD did not say what cash does after an incomplete day.

- Contracts per leg are fixed. An entry the cash cannot cover is refused and counted. When several entries fill in one minute and together cost more than the cash on hand, all of them are refused.
- A leg marked, not sold, has paid out and not been paid back. Its session stays in the totals at the mark, and the mark returns to cash at the start of the next session. Profit is the cash change plus the marked value.

*Rejected:* dropping an incomplete day from the totals, so that losing days can leave them.

### 12. What the numbers are proven against

*Whose:* the owner's. This is our own page on our own data and claims no numerical match with AlgoTest.

This is not a port, so ADR 0069's equivalence levels do not apply and there is no reference to match. The numbers are exact, and they are proven this way:

- Scenarios worked by hand from the rules, never from the code's output.
- One golden fixture of real vendor data, `options-backtest-spy-2025-06-10`, with its attribution file. It keeps the vendor's five answers for one SPY session byte for byte: the stock bars, one expiry's listing, an at-the-money call and put, and a far call that never traded. The inputs are observed from the vendor. The expected figures are worked by hand from them and kept in a hand-written answer key. A test serves the recorded answers to the lake's real fetchers (the stock pipeline, for the underlying), reads what the lake stored through the run's lake reader, replays six traded scenes (one with three contracts) and one dropped entry, and compares each with its hand-worked figures. It also checks that the option fetchers send the key in a header and never in a URL.
- New arithmetic (slippage rounding, the tolerance) carries a provenance block and exact-equality tests.

The fixture does not prove the paging of a whole listing: it holds one page, recorded with an expiry filter the lake does not send. It does not prove stops, momentum, re-entries, a leg marked at the close, a half day or fees on real data. It does not prove price-step rounding, or the lake against a real Postgres catalog. Its attribution file lists the rest.

### 13. Option data lives in the lake, under a market name of its own

*Whose:* the owner's direction (the lake gains the two kinds, with their own identities). How was made for the owner.

An option contract's minute bars for one session, and the contracts an underlying listed as of one date, are held in the data lake (ADR 0049). They call the stock path's claim, lease, fencing, publication and admission functions unchanged. There is no migration and no change to the catalog's schema.

Both use the market name `usa_options`. Stock rows are `usa`. A listing is a `metadata` row with the underlying as its symbol. Bars are a minute-trade row with the contract's OCC symbol.

Why a name of its own. Every reader that lists or counts artifacts filters on market, so option rows stay out of every stock answer.

### 14. A session is captured only after its New York date has passed

*Whose:* made for the owner.

A session's option data is fetched or admitted only from New York midnight after its date. An early close follows the same rule.

Why. The plan's data runs behind and its late prints are unmeasured. An empty answer stored too early is a permanent wrong "never traded".

The stock pipeline that fetches the underlying's bars has no such gate. So each caller asks the lake's rule itself: Check data for a run, and the run's download and the past chain's download before they fetch the underlying.

A contract's bars for a session older than the plan's window (two years, with two days of headroom) are refused before any claim. A day at the edge is left to the vendor, whose own 403 says whether the plan reaches it. That failure is `outside_data_plan`. It is asked again on every call and never latched into `fetch_timeout`, because the owner upgrading the plan clears it.

The window moves forward each day, and a session not fetched in time is lost to the plan. So the lake keeps everything it fetches.

*Rejected:* thirty minutes after the close, the first design.

### 15. A "never traded" day is stored only for a contract the stored listing holds

*Whose:* made for the owner.

The vendor answers the same empty body for four different facts: a listed contract that did not trade, a contract that never existed, a contract after it expired, and a day with no session. An empty answer proves nothing.

So a day with no bars is stored, as a complete day with no rows, only when both hold:

- the stored listing for that same date holds the contract, and
- the answer is strictly well-formed: a confirmed status, the ticker asked for, a result count that matches the results, and no next page.

Otherwise the day is refused before any claim, or fails. A wrong "never traded" would be stored for good, and a run would read a contract that never existed as one that did not trade.

The same care applies to listings. A listing is stored whole or not at all: every page is read, or the capture fails. An empty listing is refused, because it cannot be told from a vendor that answered nothing.

The vendor key rides in an `Authorization` header and in no URL, so nothing that logs or stores a URL can leak it. The older stock fetchers still put it in the URL. That is not decided here.

### 16. One limit on vendor requests for the whole process

*Whose:* made for the owner.

At most four option requests are in flight in the process, however many runs and downloads it serves. A run's download and the past chain's download hold one gate (`option_lake.vendor_request_gate`) around each capture of a listing or of a contract's day. The capture functions do not take it themselves, so a single ask is never delayed by other people's batches.

The gate is one semaphore per event loop. So both callers run their captures on the process's background loop. A caller that held it from another loop would count a second four.

A past-chain download that the caller cancels (a client that hangs up) does not stop a capture that has begun. A capture that has claimed its row must reach its publish or its failure, or the row stays `fetching` until its lease runs out. A task that awaits work on another loop and is cancelled cancels that work, so the cancellation is held at the caller. It keeps waiting, through every further cancellation, until each capture it began has ended, a capture waiting for a place at the gate included. It begins no more, and only then is it cancelled. An error the captures meet after the cancellation is logged and dropped.

A day another request is fetching right now is not a failure. A run's capture waits and asks again for up to the lake's lease and a minute, then fails with its own reason. While it waits nothing else moves, so the job's log says so once for each wait, and says when it carries on.

Why. Two runs at once must not double the rate the vendor sees, and nothing should be asked of the vendor twice.

### 17. A stored day that cannot be read, or holds a bar that cannot be placed, is refused whole

*Whose:* made for the owner. It applies the fail-fast rule of the temporal-rigor rules.

For a contract's day the lake reader tells three things apart: not captured, captured and empty, and a fault. The underlying's day is held, not held, or a fault. A damaged file, bytes the catalog does not admit and an unreachable catalog are faults. They are never read as "missing".

The underlying's day is also a fault when its file holds a row the stock reader would skip or read loosely (a row of another width, an empty line, a number written another way), or does not hold exactly the one member named for the day. The stock reader would hand over a good, shorter day. So the reader checks every row of the admitted bytes against the form the lake writes, and refuses the day instead.

A bar that cannot be placed makes the whole day a fault. That means a close that is not on a whole minute, or that falls on another New York date. Nothing is left out, filled in or carried forward. The bar left out could be the minute a stop was hit, and the result would not say so. The reader raises the kind's unreadable-file error, or a subclass of it, and hands nothing over.

At ingest the lake refuses an option bar that does not start on a whole minute, like any other corrupt vendor answer. The lake's own parser of a stored option day refuses one too, so a stored day holding one is unreadable, and a download fetches it again. A bar that starts in the last minute of a date closes on the next date: the lake's parser accepts it, and the read refuses the day.

Whether a download fetches an unreadable day again is the lake's to say, and Check data says no more than that. The lake fetches a contract's day again whenever its own parser refuses the file, because its reuse gate runs that parser, and it holds as good the one thing the reader still refuses after it, a close on another New York date. For the underlying the stock capture's own gate decides, and it is not the reader. It rebuilds a day that fails the publication contract (a minute twice, a price of zero), a row it cannot parse, and bytes the catalog does not admit. It keeps a bar off a whole minute, a row of another width and an empty line. Check data says "will be fetched again" only of a day the lake replaces. A session whose stored day the lake keeps cannot be tested. A run that meets such a day fails as unreadable and names the session, in words that tell bars that break the rules of a bar from a file that cannot be read. The table is in the module docstring of `data_check.py`, and a test puts each row through a real download.

*Rejected:* leaving such a bar out and counting it, the lake reader's first design.

### 18. Check data comes first, and a request is never shortened

*Whose:* the owner's. A request is never shortened silently, and running the longest testable stretch (#3030 calls it "Run available dates") is an explicit choice that becomes a new stated range.

Check data tells, for each session of a range and from what is already stored, with no vendor call, whether it is ready, needs a download, or cannot be tested, and why.

It restates no rule of the lake. Whether a session is too old, whether its data is final and whether the fee model prices a date are asked of the code that owns each. A failure the lake recorded makes a session untestable only when the lake itself will not ask again. The contracts a session needs are known only once its underlying day and listing are stored. Until then the answer says "not yet known" and gives no number.

A run whose range holds any session that cannot be tested fails at once, before anything is fetched, and names each session and reason. It never runs a shortened range. The longest unbroken stretch of testable sessions is offered with the half-open range that asks for exactly it.

Why. A run on a quietly shortened range answers a question the owner did not ask.

### 19. Saved runs have their own tables, and what they pin

*Whose:* made for the owner, against the PRD's plan to use the stock run store with a marker. Keeping every run in a history is the owner's.

Runs are kept in tables of their own, not the stock saved-run store.

Why. Golden Search counts rows of the stock store as outside research on a symbol's dates, and a final-test interval they overlap reads "history unknown". Its money is float. Its trade rows have no contract. Three readers would have to learn to filter.

A run is never a result in part. It is made `running` when its job is accepted and ends in exactly one way:

- complete: the figures, every session and every leg trade, written in one transaction;
- failed: a code, a plain reason and the list of what failed, with no figures;
- cancelled: nothing but the status.

The database refuses figures on a run that is not complete. A running run whose job is gone reads back as **interrupted**. A running run whose job may still be alive cannot be deleted.

A run **pins** what it read. For each session it stores the sha256 of the underlying's day, of the listing and of each contract's day. It stores the calendar's identity and package version. The pins let data that changed later be told from the data the run read. A saved run's trades are stored and are never recomputed from the lake, so changed data cannot rewrite one.

*Rejected:* the stock run store with a result-kind marker.

### 20. A session is its open in milliseconds, and no date is stored or sent

*Whose:* ADR 0022 applied. The detail was made for the owner.

Every time a run stores or sends is `int64 ms UTC`. A session is identified by its open, the 09:30 New York anchor of its trading date. No date is stored or sent. Code that groups by day derives the New York date from the open, on each call.

The one place a date can appear, in words, is an event's detail: prose the backend writes for the owner, such as a refused pick saying which expiry it came to. It is stored as written. A failure's reason and a job's log lines say what happened without a date.

Why. A second date type is what ADR 0022 refuses. A date stored beside its ms is two answers to one question.

*Rejected:* result records that carried the trading date beside the open. The first build had them, and they were removed.

### 21. A long job is made of phases that can all be stopped

*Whose:* made for the owner. It applies ADR 0065.

A run is a background job on the existing transport. The phases are checking data, downloading data, waiting for the engine's gate, replaying and saving. A stop is asked for at every boundary, after every session of the check, the download and the replay, at least every few seconds while the download waits on the vendor, and while the run waits for the gate. Each ask renews the job's lease, so no stretch of a healthy run reads as interrupted, and no heartbeat thread is needed.

Nothing renews the lease during a write that ends a run (the save, the failure, the cancel). Each waits no longer than the job's lease less a minute. Past the lease the run would read as interrupted, and an interrupted run can be deleted.

Two limits. A chunk of the underlying's bars already asked of the vendor cannot be cut short and finishes in the background. A stop that comes after the save has begun is too late: the run is complete.

### 22. An underlying is one to five letters

*Whose:* made for the owner.

US stock and ETF tickers are at most five letters. A six-letter root makes a contract symbol of 21 characters, one more than the lake's catalog stores, so every contract of such an underlying would be refused. The setup and the past-chain routes take the same one to five.

### 23. Saved setups, and which routes carry the control secret

*Whose:* the owner's for saving by name and for the #3030 strategy as an example. The rest was made for the owner.

A setup is kept by name in the research store, as the JSON it was sent. It is validated again on every read. A setup that an older rule accepted and the current rule refuses is listed as one that cannot be opened, with the reason, and is never dropped. Two examples ship and cannot be overwritten or deleted: the #3030 strategy without a re-entry rule and with one, so the owner's comparison is two runs.

The setup routes touch no account and need no data-plane control secret. The past chain's download spends vendor requests and writes the lake, as `/api/data-lake/ensure-data` does, so it carries the secret too. The two past-chain reads need none.

## What this does not decide

- Sell legs, target profit, trailing stops, range breakouts, overnight holds, a volatility filter.
- Index options.
- Historical bid and ask, and anything priced from quotes.
- Entries triggered by a stock signal, and any link to Deploy or Golden Search.
- A numerical match with AlgoTest.
- Moving the Data Lab past-chain inspector onto the new past-chain query. It is left as it is.
- Whether a run's job route, `POST /api/jobs-internal/options-backtest`, carries the control secret. Its download phase spends vendor requests and writes the lake, as the past chain's download does. Today it is not on the control manifest, and the manifest lists no `/api/jobs-internal` route but the pool drain.

## Consequences

- A result is simulated from trade prints and is not measured execution. It cannot show a spread, a bid, an ask or a quote-based Greek. The banner says so, and the slippage tolerance says how much of the guess a result survives.
- An illiquid contract shows only as a dropped entry or a retried sale, counted and never forced.
- A listing is asked for as of a date, never a minute. Nothing proves that a picked contract was listed at the entry time and not only later that day.
- Only a session whose New York date has passed can be fetched, and only two years back (decision 14).
- Under a partial square-off, a stop on one leg also holds back every other leg's entry check in that minute (decision 4).
- The expiry and strike rules exist twice. Changing a rule in one means changing the other. The parity test fails if they drift.
- Estimated fees lean high by cents a day, and are unavailable before the model's rate table starts.
- Option runs and the stock run history are separate lists.
- Research schema versions 19 and 20 add the tables for saved setups and runs.
- Two older lake fetchers still put the vendor key in the URL, and the lake work found that they can drop the paging cursor on a later page. Neither is changed here.

## Open questions for the owner

1. **One shared implementation of contract picking for bots and backtests.** It means editing the live resolver, which is the money path. Today there are two implementations and a parity test.
2. **The re-entry minute (PRD behaviour 8).** The PRD says a re-entry at once picks a fresh contract "at the minute after the stop's fill". The build picks in the minute the stop's sale fills: decided on that bar's close, bought one minute after. One minute later is a one-line change.
3. **Whether a dropped entry uses up a re-entry (PRD behaviour 14).** As built, a re-entry count goes down only on a fill, so a dropped entry never uses one up. A re-entry bought at once ends the leg for the day when dropped, so the rule changes nothing there. A momentum leg whose entry is dropped goes back to waiting with its re-entry intact.
