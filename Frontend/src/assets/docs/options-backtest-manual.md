## 1. Run your first backtest {#first-run}

**Your goal:** finish a small simulation and explain one trade. You do not need to place an order or connect a trading account to learn this workflow. Historical data still needs to be available to the app.

A backtest asks, “What would these rules have done with past prices?” It does not reserve a contract or send an order to a broker. A profitable result is a reason to investigate, not a promise about the next trade.

### Follow this first exercise

1. Open **Strategy Tools → Options Backtest**. Stay on the **Backtest** tab.
2. In **Instrument**, choose an underlying, such as SPY. The underlying is the stock or fund the option refers to. In **Duration**, choose a short range of completed trading days. The available range depends on the data plan.
3. Keep the initial **Entry time** and **Exit time** while you learn. They begin five minutes after the open and fifteen minutes before the close. Leave **No re-entry after** off for this first exercise.
4. In **Leg builder**, choose **Buy → Call → 1 contract → Weekly → At the money**, then press **Add leg**. Leave Stop loss, Momentum and Re-entry off. This is a practice configuration, not a trading recommendation.
5. In **Costs**, review Cash and Slippage. Choose **Estimated** fees if the selected dates have published rates. The page starts with **Not charged**; that choice leaves fees out of the result. If estimated rates are unavailable, choose later dates or deliberately state another fee assumption.
6. Read the setup's sentences beside the form. Check the instrument, contract choice, times, quantity and costs. Give the setup a name such as **First practice** and save it.
7. Press **Check data**. Read the session list. **Ready** means the necessary data is stored; **Needs download** means it must be fetched; **Cannot be tested** needs a change or investigation. Nothing downloads during this check.
8. If every requested session can be tested, start the backtest. Watch downloading and replay progress. If the page offers **Run available dates**, read the replacement range before choosing it, then start the run for that range.
9. When the run is **Complete**, read **Total fees**, **Slippage paid**, and **Sessions** before judging Net profit. A run can finish successfully without making a trade.
10. Open a day in the daily table or trades. Find the selected contract, its entry, its closing fill or remaining mark, and the reason it ended. Explain those events in your own words.

> **You have finished the exercise when:** you can identify the setup used, the dates actually requested, the cost assumptions, and what happened to one leg. If no trade occurred, finding the recorded reason is a useful result too.

**Try next:** repeat with one stop rule. Keep everything else the same, then compare the two saved runs. Learn one change at a time.

## 2. Understand the basics {#basics}

**Your goal:** understand the words you will see before adding more legs.

| Word | Meaning here |
|---|---|
| Underlying | The stock or ETF an option refers to. |
| Call / Put | A call gives its buyer a right to buy the underlying; a put gives a right to sell it, under the contract's terms. |
| Strike | The contract's exercise price. It is different from the option's own trading price. |
| Premium | The option's quoted price. The contract multiplier turns that quote into a contract amount. |
| Expiry | The contract's expiration date. It is different from your setup's exit time. |
| Leg | One part of the setup: a buy or a sale, a contract-selection rule, a quantity and optional trading rules. |
| Buy / Sell | Buy opens a bought leg, later sold to close. Sell opens a sold leg, later bought back to close. |
| Setup / Run | A setup is reusable instructions. A run is those instructions replayed over particular dates. |

### Know what this replay observes

The backtest uses historical **minute trade bars**. A bar summarizes trades during a minute; it does not contain the bid and ask. Fills are simulated from those trade prices with the slippage and price rounding stated in the result.

Rules inspect completed minutes. An order decided at a minute's close fills from the **next minute's open**, when the contract has the required bar. A stop is not a promise to fill at its trigger price. A minute's high or low alone does not trigger these close-based rules.

Each day has its own trading window. The setup tries to close its holdings at the exit time. This is not a simulation of holding an option through real delivery of shares at exercise or assignment.

> **Read the fills banner on every result.** It tells you the fill assumptions, slippage, price step and fee policy. Keep those assumptions with any result you share.

## 3. Choose dates and times {#dates-and-times}

**Your goal:** say when the experiment runs and when its rules act within each session.

### Choose the historical range

Use **Duration** to select the requested dates. After **Check data**, the page shows the earliest option prices served by the current plan. A range can fail because it is too old, not final yet, missing required data or missing fee rates. Downloading cannot solve every one of those problems.

Holidays are handled by the trading calendar. On a shortened session, the window must still leave room for entry and exit. A session whose window does not fit is skipped with a reason.

### Set the three times

| Control | What it changes |
|---|---|
| Entry time | When contracts are first selected and immediate entry or momentum waiting begins. |
| Exit time | When open legs are told to close and waiting entries are cancelled. Closing fills still need the following minute's data. |
| No re-entry after | A stop decided later than this time earns no re-entry. It does not block a first entry. |

Times on this form are **New York clock times**. The setup stores their distance from the open or close, shown in the explanatory sentence. A half day keeps that distance. Check the sentence after changing a time or loading a setup.

**Check before moving on:** the exit must be later than entry; entry needs a completed minute; exit must leave a minute to fill. Read any field problem instead of trying to bypass it.

## 4. Choose your option legs {#legs}

**Your goal:** describe which contracts the replay should pick, rather than choosing today's contract for every historical day.

1. Choose **Buy** or **Sell**, then **Call** or **Put**.
2. Enter the number of contracts.
3. Select an expiry rule and strike rule from the tables below.
4. Press **Add leg**. Review the leg's summary.
5. Adjust the contract directly on its card. **Flip to Buy / Sell** changes direction while keeping the leg's rules. Use the strike ladder, **− / +**, or **Exact offset** to move through listed strikes; **Reset to ATM** selects at the money. Call / Put, contracts and expiry stay editable. Use **Copy** to add a similar leg or **Delete** to remove one.

The current setup allows one to eight legs. The page reports limits and invalid inputs before a run is accepted.

| Expiry choice | Which expiry it selects |
|---|---|
| Same day | The expiry on that trading date. If none is listed, it cannot invent one. |
| Weekly | The nearest listed expiry, including today, on the last trading day of its week. It can be in a later week and can be a monthly expiry. |
| At least N days | The nearest listed expiry at least N calendar days after the trading date. |

| Strike choice | How to read it |
|---|---|
| At the money | The listed standard strike nearest the underlying's price; the lower strike wins a tie. |
| Steps from the money | Move through the listed strikes. Positive steps go higher; negative steps go lower. A step is not necessarily one dollar. |

### Add sold legs only after learning bought legs

A sold call requires a bought call to cover it; a sold put requires a bought put. There must be at least as many bought contracts of that kind as sold contracts. A setup with sold legs uses one expiry rule, and the replay checks the actual expiries and cover when entries fill.

A bought leg waiting for momentum is not already-held protection. If it fails to fill, a sold entry depending on it can be refused. An entry can also be refused for a conflicting position in the same contract, a second expiry, or insufficient cash.

**Practice:** build a two-leg setup with one bought call and one sold call at different strikes and the same expiry rule. Read the summary and cover requirements before running it. Do not interpret the presence of a protective leg as a guarantee that every entry or close will fill.

## 5. Add stops and entry rules {#rules}

**Your goal:** know what each rule watches and what happens after it acts.

### Momentum waits for the option's own price

Enable **Momentum** on a leg and select percent up, percent down, points up or points down. The starting premium comes from the contract's minute closing at the entry time. The leg waits until a later minute closes at or beyond its level.

Momentum here reads the option's premium. It is not a signal calculated from the underlying's chart. If the contract has no starting price, the leg cannot begin that wait. If the level is never reached, no entry occurs.

### Stop loss watches the filled leg

Enable **Stop loss**, choose Percent or Points, and enter the distance. A bought leg stops when its premium closes sufficiently below its fill price. A sold leg stops when its premium closes sufficiently above its sale price. The next fill can be worse than the stop level.

| Square-off setting | What a stop closes |
|---|---|
| Partial | The stopped leg. If that leg protects sold legs, those sold legs are also closed as required before protection can leave. |
| Complete | All open legs. Legs still waiting for momentum are cancelled for that day. |

A sold leg's own stop can buy it back while leaving the bought protection held. A protective bought leg may have to wait to close when a required buy-back has no bar. Inspect the recorded events to understand which legs actually left.

### Re-entry is another attempt

Enable **Re-entry on stop**, set its count, and choose:

- **At once:** pick a fresh contract under the same rules and attempt entry without a momentum wait.
- **On momentum:** pick a fresh contract, take a fresh starting premium, and wait for the leg's momentum rule again. Momentum must be enabled.

Re-entry needs a stop that can actually close that leg. Its count is per session. A stop after **No re-entry after** earns none. Every new attempt still needs data, money and any required protection.

**Practice:** add only a stop to your first setup. Run it, open a stopped day and locate the trigger and subsequent fill. Then add one re-entry and follow the second attempt separately.

## 6. Set cash, slippage and fees {#costs}

**Your goal:** describe what the result assumes about execution costs and available funds.

**Cash** is the starting money for this simulation. The run carries cash between its sessions. A larger amount may admit trades that a smaller amount refuses, so it can change the path rather than just scale the result.

**Slippage** moves every simulated fill against the trader. Buying becomes more expensive and selling returns less. The fill is then rounded against the trader to the setup's price step. The result banner states that step. Slippage stands in for execution friction that minute trade bars cannot directly observe; it is not an observed bid-ask spread.

| Fee setting | What the run does |
|---|---|
| Not charged | Deliberately leaves fees out. This is the initial choice, not evidence that the broker is free. |
| Per contract | Uses your dollar amount on each contract in every fill, buys and sales, including re-entries and closes. This replaces the estimated policy. |
| Estimated | Uses the canonical option fee model's dated rates. If a required historical rate is unavailable, the run refuses that range under this policy. |

The estimate rounds each fee type upward to cents on each fill. The broker's daily fee aggregation can produce a different amount. See [broker fees](/docs/broker-manual#fees) and [reconciliation](/docs/broker-manual#reconcile) before expecting an invoice match.

**Check before running:** name your fee assumption, confirm slippage is deliberate, and check that the cash amount is the one you intended. No fee estimate here includes your monthly data subscription or every possible account-level charge.

## 7. Review and save a setup {#save-setup}

**Your goal:** preserve the instructions you intended to test.

1. Read the plain-language summary next to the setup. Follow each leg's contract choice and rules.
2. Resolve any field problems. If the summary cannot load, use its retry action before relying on it.
3. Enter a recognizable setup name and save. Read the confirmation or problem message.
4. Use the saved-setups menu to reopen a setup. Confirm the read-back before running it over new dates.
5. Use **Export setup** to download the setup file. **Import setup** reads a compatible JSON setup and reports rejected or invalid content instead of quietly repairing it.

The setup contains trading instructions; the run chooses historical dates. Exporting a setup does not export price data, completed trades or a result. Use the trade export for the ledger of a completed run.

Deleting a saved setup removes that library item. It is separate from deleting a run in History. A completed run retains the setup it used; later setup edits do not rewrite that recorded result.

## 8. Check data and start a run {#data-and-run}

**Your goal:** know whether the requested experiment is possible before waiting for it.

| Check data result | Your next action |
|---|---|
| Ready | Review the session details. A session skipped by the setup may need no data and still be ready. |
| Needs download | Read what is missing. Starting an eligible run fetches required data before replay. |
| Cannot be tested | Read every reason. Change the dates or fee assumption when appropriate; investigate persistent data errors. |

**Check data** reads stored information without calling the vendor. Changing the setup or dates, downloading data, or starting a run makes that answer stale. Check again when prompted.

If only part of a range can be tested, **Run available dates** offers its longest continuous testable stretch. Choosing it changes the stated range. A run will not silently leave out impossible sessions from your original request.

### Follow the run's state

- **Running:** watch the current phase and progress. Downloading comes before replay.
- **Complete:** the whole result is saved and can be reopened in History.
- **Failed:** read the reason and affected sessions. A partial replay is not presented as a completed result.
- **Cancelled:** the request ended without a completed result. Captures already in progress may finish before cancellation completes.
- **Interrupted:** the running job is gone and there is no completed result to read.

Use the run's Cancel action if you no longer want it. If completion won the race with cancellation, the page tells you the result was already saved. If reading progress fails temporarily, inspect the message before starting a duplicate run.

## 9. Read your results {#results}

**Your goal:** understand what the figures count before deciding whether a setup deserves more study.

Read in this order: **fills banner → session counts → costs → profit and drawdown → individual trades → capital evidence**.

| Figure or view | What it means |
|---|---|
| Net profit | The run's reported outcome under its fees, slippage and valuation rules. Incomplete sessions remain in the totals. |
| Total fees / Slippage paid | The modeled cost assumptions applied during this run. |
| Trade win rate | Winning leg round trips divided by leg round trips. A multi-leg strategy can produce several trades in one session. |
| Daily win rate | Profitable sessions divided by traded sessions. It answers a different question from trade win rate. |
| Profit factor | Gross profit divided by gross loss. “No losing trade” is not a finite profit-factor estimate. |
| Max drawdown | The largest fall from a running equity peak, measured at session ends. It does not measure the worst intraday fall. |
| Daily chart and table | Inspect the sequence of days, equity and falls; open a day for its recorded details. |
| Monthly table | Look for concentration in particular periods rather than relying only on the total. |

### Read the days that made no money

**No trade** is different from **Skipped** or **Incomplete**. A no-trade session may simply have never reached momentum. A skipped session could not use the setup's window or required underlying observation. An incomplete session ended with something still held and marked.

The **What did not trade** counts explain refused picks, absent starting prices, missing fill bars, cash and protection refusals, and re-entry cutoffs. Some counts overlap: re-entries lost are also represented by their underlying causes. Do not add every count into a new total.

> **A mark is a valuation, not a closing fill.** A bought option left open is marked at its last available price; a sold option left open is an amount owed. Those marks stay in totals. Read Capital evidence before treating Ending cash as continuously trade-funded money.

## 10. Follow one trade and day {#one-day}

**Your goal:** tell the story behind a result, using the saved record.

1. In **Trades**, choose a day or leg filter. Use the page controls when the ledger spans several pages.
2. Open a day from a trade or the daily table. Check its session status and cash before and after.
3. On the timeline, find the underlying and option price series, the starting premium, momentum level and stop level where present.
4. Compare **Price seen** with **Price filled**. The difference reflects the fill assumptions; the displayed fill minute is the minute ending at that timestamp.
5. Read the events in order. Follow each attempt separately: first entry, stop, re-entry, cancellation or refusal.
6. Inspect **Marks** if a leg did not close. A marker in that section is not a sale or buy-back.
7. Use the trade export to download every trade in the run. The export is not limited to the visible table page or current filters.

A saved run retains its events and fills. Its price chart also checks that the stored market-data files still match the files used by that run. If files changed, disappeared or cannot be confirmed, the chart explains why a series is absent. The recorded trade facts remain readable. Do not replace missing price evidence with a neighboring strike or today's data.

**Check your understanding:** point to the rule decision, the subsequent fill, its cost, and its exit reason. If there was no fill, identify the refusal or missing observation.

## 11. Test costs and funding {#capital}

**Your goal:** examine whether a result depends on generous execution assumptions or money the replay could not continuously support.

### Slippage tolerance

Read the outcomes across the slippage ladder. The tolerance is the highest tested level at which that level and every lower tested level remained profitable. “Survives the whole ladder” applies only to the values tested. It does not establish tolerance to any possible spread or slippage.

### Cash ladder

Compare the runs at the displayed starting-cash levels. Each level carries its own cash through the replay at the setup's slippage. Compare entries filled, entries refused, net profit, and the funded stretch. More cash can permit a different set of trades. No ladder level is labeled safe.

### Capital evidence

| Evidence | How to use it |
|---|---|
| Funded continuously through | How far the recorded cash supported the run before the first capital-invalid event. |
| Most collateral held | The largest amount pledged against held positions. Collateral is not a fee. |
| Cash this path needed | A description of this recorded path. It is not a minimum account size or safe starting amount. A cash refusal can make it incomplete. |
| Least free cash seen | Cash after allowing for collateral; negative values are retained. |
| Largest assignment obligation | The gross obligation associated with sold contracts. It is not a loss forecast. |
| Closes that relied on their own relief | Buy-backs that cost more than free cash before closing. Alpaca may refuse such an order even if closing would release collateral. |

An unfunded close or a session ending with a marked position ends continuous funding evidence. Later sessions can still appear in totals under the assumption of settlement at the mark. The manual's question is therefore not only “Did it profit?” but also “How far was that path funded?”

Older runs can say **Not recorded** for newer evidence. That is not zero. The replay's requirement-rule date describes the rule it used, not proof of what the broker required on every historical date.

## 12. Compare and reuse runs {#history}

**Your goal:** compare a deliberate change without confusing different samples.

1. Open **History** and find the run by setup, date range, start time and status. Use **Show older runs** when offered.
2. Open a completed run to read its saved result and setup. **Use this setup** brings those instructions back to the form for a new experiment.
3. Select two eligible completed runs and press **Compare**. Read any eligibility message if comparison is unavailable.
4. Start with the settings that differ. Then read the results over **shared sessions**: only days present in both runs contribute to those comparison totals.
5. If you intended to test one change but several settings differ, make a better-controlled pair before drawing a conclusion.

Deleting a run removes that saved research result. Export anything you need before confirming deletion. Deleting it does not close positions in a broker account.

**Practice:** compare your baseline with the version containing one stop. Explain the change in trades, costs and drawdown; do not judge it only by which total is larger.

## 13. Explore the Past chain {#past-chain}

**Your goal:** see what was listed and what had traded at a historical instant.

1. Open **Past chain**. Choose the underlying, trading day and time.
2. Read the underlying price and its timestamp, then choose an expiry from those listed.
3. Adjust the number of strikes shown around the money.
4. Read calls and puts alongside each strike. Each price is the last stored trade-bar close at or before the chosen instant, with its own timestamp. It is not a bid or ask.
5. If a download is offered, read its scope and start it. Inspect its progress and resulting data states; downloading cannot create a trade that never happened.

| Price state | What it tells you |
|---|---|
| Traded | A stored bar supplies a price by this instant. Check its time; it may be older than the selected instant. |
| Did not trade | The contract day was captured but has no trade bar by this instant. |
| Not stored | The required contract day has not been captured. |
| Unreadable | Stored data could not be used; read the accompanying problem. |

Expiry tags describe calendar position: **Daily, Weekly, Monthly, Quarterly, Unknown**. **Expires today** is a separate fact. A monthly expiry on its last day remains Monthly. The leg builder's Weekly selection rule is different from this series label.

Calls and puts can have different at-the-money strikes if their listed strikes differ. No price is borrowed from another day or a nearby strike.

## 14. Find out why it did not trade {#troubleshooting}

**Your goal:** find the first missing requirement rather than changing settings at random.

| What you see | What to check next |
|---|---|
| Cannot be tested | Read the session reason: data-plan boundary, data not final, unavailable fee rates or unusable stored data. |
| A stale Check data result | Run Check data again for the current setup and range. |
| No starting price | Open the day and contract. The required minute did not have a usable trade bar. |
| Momentum never reached | Compare the starting premium, direction, threshold and later closes. This can be a valid no-trade day. |
| Entry dropped | Inspect the next-minute fill data. An observed trigger does not guarantee a fill bar. |
| Refused for cash | Review starting cash, entry cost and collateral; use the cash ladder to study the effect. |
| Refused for protection or expiry | Check that the bought cover actually filled and that the contracts share the required expiry. |
| Re-entry did not occur | Check the remaining count, stop eligibility, cutoff, contract pick, starting price, cash and protection. |
| Incomplete session | Inspect the unsold or unbought-back mark and Capital evidence. It is not a completed close. |
| Failed or interrupted run | Read the failure details. Fix the cause and start a fresh run; no partial result should be treated as complete. |
| Saved chart unavailable | Read the file-verification message. Use the stored events and fills without inventing a replacement series. |

When asking for help, retain the setup export, run identifier, affected day and leg, and exact problem message. Do not include broker credentials.

## 15. Words and further reading {#glossary}

| Term | Plain meaning |
|---|---|
| Session | One trading day, with its calendar-defined open and close. |
| Fill | The simulated execution of an order at a price and quantity. |
| Attempt | One try to enter a leg; it can end without a trade. |
| Square off | Which positions a stop tells the replay to close. |
| Collateral | Money pledged against held positions. |
| Free cash | Cash after allowing for collateral. |
| Mark | A valuation of a position still held. |
| Shared sessions | The trading days common to two runs being compared. |

Continue with [Broker costs and timing](/docs/broker-manual) for account charges, order evidence and settlement. Read [How our trading bots work](/docs/bot-business-manual) before moving from historical experiments to a running bot.

This guide describes the Options Backtest interface reviewed on October 16, 2026. The controls, validation messages and saved result state determine what is available in your running version. Paper and Live trading have their own execution and account requirements.
