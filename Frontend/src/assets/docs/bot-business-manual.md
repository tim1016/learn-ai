<p class="manual-reading-note">Start here: a bot follows a recipe. It does not understand the news, know the future, or guarantee a profit. This handbook explains the recipe, the checks around it, and the evidence you can read afterward.</p>

## 1. The big picture {#big-picture}

Think of a bot as a small team with clearly separated jobs.

| Team member | Its job | Everyday comparison |
|---|---|---|
| Market-data feed | Supplies prices, completed bars, option quotes and trading-status evidence. Live bots use Interactive Brokers (IBKR). | The team's eyes and ears. |
| Strategy | Reads price history and its indicators. Produces an ENTER, EXIT or no-action decision. | A recipe that says when to act. |
| Action Plan | Says what an ENTER opens and what an EXIT closes. | The shopping list. |
| Account Clerk | Checks permission, money, orders and holdings; keeps the durable records. | A cashier who also keeps the books. |
| Alpaca | Holds the broker account and handles submitted orders and execution reports. | The outside broker. |

**The key distinction is “listens to” versus “trades.”** A bot can listen to one stock or ETF and trade that same asset, another stock or ETF, or options. The signal still comes from the asset it listens to.

For example, a bot might listen to the price of SPY, an ETF, and buy a call option on SPY when its strategy says ENTER. The strategy decided *when*. The Action Plan decided *what*. The Clerk must still decide whether the proposed order can go ahead.

### A signal is only the beginning

The sequence is: **market observation → strategy decision → proposed trade → admission checks → order → confirmed fills → holdings**.

An order asks the broker to trade. A **fill** is evidence that some or all of the trade actually happened. A chart marker, a successful button click or an accepted order does not prove a full fill.

> **Remember:** “The bot wanted to buy,” “the broker accepted the order,” and “the account owns it” are three different facts.

### Why this separation matters

A good-looking signal can still be blocked by old data, insufficient money, an account restriction or an unresolved earlier order. That is expected behavior: the strategy is allowed to suggest an action without being allowed to spend money.

Each bot has its own identity and attributed holdings. Two bots can trade the same symbol while retaining separate records. Closing one bot should reduce the holdings attributed to that bot, rather than blindly selling the account's entire position.

This manual describes the implementation reviewed on **October 8, 2026**, at source revision **2a04922f**. It is a guide to the rules, not a live account-status report. Availability and permission still come from the current Deploy review and account notices.

## 2. Four different trading worlds {#trading-worlds}

“Using live prices” does not necessarily mean “placing real-money orders.”

| World | What happens to an order? | What the result tells you |
|---|---|---|
| **Dry Run** | The app simulates trades in a separate account for that bot. No Alpaca order is submitted. | How the bot behaves under the app's simulation rules. |
| **Paper** | Orders go to Alpaca's Paper environment. The money and fills are simulated by the broker. | How the broker integration behaves in Paper. |
| **Live Shadow** | An isolated Shadow account uses shared simulated cash and its own holdings. It sends no real Alpaca orders. | How bots work together in the account's rehearsal environment. |
| **Live** | Authorized orders go to the live broker account. Real money and positions are involved. | Actual execution, with actual trading consequences. |

IBKR remains the live market-data provider for these bot paths. Alpaca supplies accounts, orders and execution reports. Historical research can use stored data from other sources; that is a different job from the live bot feed.

### What options support currently means

| Option plan | Dry Run / Live Shadow | Paper | Live |
|---|---|---|---|
| One bought call or put | Implemented, subject to checks. | Implemented, subject to broker and account checks. | Opening is disabled by default; requires the Live options rollout setting and all other checks. |
| Supported structure with two to four legs | Implemented, subject to structure and quote checks. | Requires the multi-leg rollout setting, which defaults off. | Requires both the multi-leg and Live options rollout settings, plus all other checks. |
| A lone sold option, or a plan mixing stock and options | Refused by the bot's plan rules. | Refused. | Refused. |

The checked-in defaults are **not evidence that a particular running account has those settings**. Read the actual review. Broker-facing Deploy also checks option permission: the current rule requires level 2 for a bought call or put and level 3 for a multi-leg structure. Missing permission is not approval.

Automated tests cover the options paths, including simulated and stand-in broker responses. The implementation's rollout notes still call for checking option orders against Alpaca. A feature being implemented is different from it being verified with a real broker account.

### What a rehearsal cannot prove

Simulations cannot promise a real fill price, the speed of a fill or how a broker will handle an assignment. Options simulations fill supported structures together or not at all. A real multi-leg order can leave uneven fills across its legs.

**A profitable stock backtest also does not prove that replacing its shares with options will be profitable.** Options add time decay, changing volatility, bid-ask costs and contract selection. The current Deploy warning explicitly says these option trades have no options backtest here.

## 3. How a bot makes a decision {#decision-clock}

### It reads completed bars

A **bar** summarizes a time interval: opening price, highest price, lowest price, closing price and volume. Strategies generally build their decision bars from minute observations.

A 15-minute bar labeled 10:00 describes the interval from 9:45 up to 10:00. The strategy evaluates it once that interval has closed. It does not treat an unfinished candle as a final result.

The exchange calendar supplies the trading sessions, holidays and early closes. The live feed supplies separate evidence about whether trading is currently live, including halt-related status. A calendar saying “open” is not enough by itself.

### Warmup is the bot doing its homework

An indicator such as a moving average needs earlier prices before its value is useful. At startup, the bot rebuilds its indicator and decision state from the required history.

Warmup is not permission to place orders for old signals. The handoff into the live stream must preserve the required observations without silently filling gaps with invented prices.

### Every decision must be timely

The normal decision-delivery allowance is **20 seconds after the decision bar closes**. Option selection and the remaining order work must also fit within their deadlines. A decision that arrives too late is recorded as late; a skipped option entry is not stored as a shopping order for a later bar.

After a reconnect, old option quotes cannot be reused as though they came from the new connection. Missing observations, a stalled feed or uncertain trading status can block entry or end the run.

### The strategy gets an outcome before moving on

The runner stages the strategy's decision, asks the execution side what happened, and settles that decision before evaluating the next one. For options, a proven unfilled or rejected entry leaves no newly held position. Partial or uncertain outcomes must preserve the possibility of holdings so that later exits can address them.

> **Example:** A fresh crossover says ENTER, but the option's quote is too old. The entry is skipped. If that strategy needs a *fresh* crossover, the next bar does not automatically get a second chance just because the quote has improved.

### Three meanings of time

- **Decision time:** when a completed bar is evaluated.
- **Order time:** when an order is sent, rests, fills or is cancelled.
- **Holding time:** the strategy's rule for how long to stay in the trade.

These clocks can differ. A five-bar holding rule counts five eligible decision bars. Overnight hours do not add bars. An owner-set end or option close deadline can close the position earlier.

## 4. The strategy rulebook {#strategy-rules}

These are implemented signal programs. Their appearance in a Deploy list still depends on current validation, a supported runtime, the selected parameters and account-specific admission. Default settings below explain the starting recipe; a bot's sealed settings say what *that bot* uses.

### Read the indicators as simple questions

**SMA** is an ordinary moving average: “What was the average price over the last few bars?” **EMA** is a moving average that gives more weight to recent prices. A fast average responds sooner than a slow one.

**RSI** is a 0-to-100 score comparing recent gains and losses. It is not a percentage chance of profit. **MACD** compares moving averages to describe momentum. **ADX** measures trend strength, not whether the trend points up. **Supertrend** supplies a direction filter based on price and volatility.

### EMA Crossover Signal

On 15-minute bars, enter only when all of these are true:

1. The fast EMA crosses from at-or-below the slow EMA to above it on this bar.
2. The gap passes both configured minimums: the absolute-price gap and the gap in basis points.
3. RSI is within the configured band, including its endpoints.
4. The indicators are ready and the strategy has no open position lifecycle.

Starting values are **EMA 5 and 10, RSI 14, RSI band 50-70, absolute gap 0.20 and basis-point gap 0**. Both gap tests apply; a zero floor removes that particular minimum.

The strategy exits after **five further 15-minute decision bars** by default. It does not have a strategy-level stop-loss, profit target or scale-in. Five bars can span two sessions.

### EMA Previous Crossover

This variation allows a recent crossover to qualify later. The fast EMA must have crossed above the slow EMA on this bar or within the allowed age, and stayed above it. The gap and RSI must pass **on the current entry bar**.

The default age limit is **one 15-minute interval of clock time**. A crossover from an earlier session never qualifies. The default EMA lengths, RSI band, gap and five-bar exit are the same as above.

**Example:** At the crossover, the gap is 0.10 and too small. One bar later it is 0.25, RSI is 60, and the fast line stayed above. The previous-crossover strategy can qualify; the fresh-crossover strategy cannot enter on that old cross.

### SMA Crossover

Enter when the short SMA crosses above the long SMA. While holding, exit whenever the short SMA is **at or below** the long SMA; equality counts, and the exit does not require a new cross. The starting windows are **10 and 30 bars**, with **15-minute bars**. Window sizes and resolution are configurable. A fast line simply being above the slow line is not a new entry crossover.

### RSI Mean Reversion

Buy when RSI is **strictly below 30**, and exit when it is **strictly above 70**, using the starting settings. RSI exactly 30 or exactly 70 does not pass those respective rules.

The default RSI length is **14**, on **15-minute bars**. There is no fixed holding-time exit in this strategy. It can remain invested while waiting for the exit threshold.

### Strategies A, B and C

These strategies share a default RSI entry band of **38-70, inclusive**, and default to 15-minute decision bars. They enter while flat; they do not keep adding entries while already holding.

| Strategy | All extra entry checks | Exit check |
|---|---|---|
| **A: EMA-gap + MACD + RSI-range** | Fast EMA minus slow EMA is **greater than 0.50** by default; MACD line is above zero. Default EMA lengths: 20 and 50. | ADX is **below 15** by default. |
| **B: Supertrend + ADX + MACD + RSI-range** | Supertrend is long; ADX is **above 20**; MACD line is above zero. | ADX is **below 20** by default. |
| **C: ADX-rising + RSI-range** | ADX is **above 20** and above its previous bar's value. | ADX is **below 15** by default. |

The configured thresholds can differ. In Strategy C, rising ADX means a strengthening trend; it is not itself proof that price is rising.

### A testing strategy has a different purpose

**Deployment Validation** is a lifecycle and timing test program. It helps exercise the trading machinery. Its presence does not make it a researched profit strategy.

> **Do not assume every bot has a stop-loss.** A strategy exit, an account risk restriction, an owner-set end and Flatten solve different problems. Read the particular exit rule before deploying.

## 5. Stocks, sizing and budgets {#stocks-and-budgets}

### The signal asset and traded asset can differ

Choosing “another stock or ETF” changes what the bot trades; it does not replace the signal stream. The runner uses the traded asset's own pricing observations rather than pretending it costs the same as the signal asset.

**Example:** A strategy listens to a \$500 ETF and trades a \$40 stock. Five shares of the stock cost about \$200 before costs, not \$2,500. The ETF's price generated the signal; the stock's price determines the purchase.

A strategy tested on one asset does not carry proof of returns on another. Deploy warns about this substitution. Paper, Shadow and Live require the relevant written acceptance; Dry Run warns without that acceptance requirement.

### Size and budget answer different questions

**Size** says how many units an entry asks for. The current Deploy sizing choices use one unit for the small canary preset, or a custom whole quantity from 1 to 100. Units are shares for stocks and **whole structures** for options.

**Budget** says how much money is allocated to the bot. Increasing a budget does not secretly increase the configured order quantity.

The money review must be able to fund the proposed entry and required provisions. Entry admission also depends on the account's current cash, reservations, exposure and risk controls. A bot's apparently free budget does not override an account-wide block.

### The daily loss limit is an account rule

Paper, Shadow and Live admission require an applicable daily loss limit and current evidence to judge it. Missing or stale cash, equity, fees or execution evidence can make new entries wait. A breached loss limit holds new exposure across the account; it is not a promise that every position is already closed.

Dry Run is exempt from the daily-loss-policy requirement, but still has money, execution-evidence and ordinary custody checks. A per-bot budget and an account loss limit are not interchangeable.

### Read the money as separate pockets

| Pocket | What it means |
|---|---|
| Held | Money committed to existing holdings, including option collateral where required. |
| Pending | Money reserved for orders whose outcome is not finished. |
| Free within the bot | Its remaining allocation, subject to fresh admission checks. |
| Released | Allocation returned when the bot stops, to the extent it is no longer needed. |
| Settling | Sale proceeds not yet treated as freely deployable cash. |

The same dollars cannot be freely spent by two bots at once. Stopping a bot does not release money still needed for its positions or unresolved orders.

### Costs and profit are evidence too

A stock bought at \$40 and sold at \$42 earns \$2 per share **before costs**. Five shares would have \$10 gross profit. Fees reduce that result; real fills may also differ from the price seen when the decision was made.

An open position's displayed profit is a mark using available prices. It is not a completed sale. Realized profit comes from booked transactions, and fee evidence may arrive later.

Backtests have their own quantity, cost and fill assumptions. They can use a different sizing policy from a deployed bot. Always compare those assumptions before comparing dollar results.

## 6. Options and structures {#option-structures}

An **option** is a contract tied to an underlying asset. A bought **call** gives a right to buy at its strike price; a bought **put** gives a right to sell. The **expiry** tells you when the contract ends. A seller takes on the corresponding obligation.

The price paid or received for an option is its **premium**. This bot entry path admits standard contracts with a **100-share multiplier**. A premium quoted at \$2.00 therefore represents \$200 for one contract, before fees. Adjusted or nonstandard contracts are not silently treated as standard ones.

A **leg** is one part of a trade. A **structure** is the full combination of legs. A ratio tells how many contracts of a leg belong in one structure.

### What the builder offers

| Family | What one structure contains | What to understand |
|---|---|---|
| Long call / long put | One bought call or put. | A directional position whose value also changes with time and volatility. |
| Bull call / bear put spread | A bought option and a sold option of the same type at different strikes. | The sold leg helps fund the purchase and limits the payoff. |
| Bull put / bear call spread | A sold option paired with a protective bought option. | A credit received at entry comes with a collateral requirement. |
| Long straddle | Bought call and put at the same strike. | Pays for movement; enough movement must overcome the premium. |
| Long strangle | Bought put at a lower strike and call at a higher strike. | Also pays for movement, with different strike locations. |
| Call / put butterfly | Buy a lower strike, sell two at the middle, buy an upper strike. | Three legs with a 1:2:1 ratio; four contracts per structure. |
| Iron condor / iron butterfly | Bought and sold puts plus bought and sold calls. | Four-leg shapes with protective wings and a bounded payoff when properly formed. |

These are descriptions of the starting shapes, not recommendations. The builder also supports Custom plans that pass the same structural rules.

### The plan must make sense

An option plan has one to four legs, all on one underlying. Each entry leg must have exactly one matching exit instruction. Ratios must be in their lowest terms.

A lone sold call or put is refused. Sold contracts need sufficient bought contracts of the same type; the resolved strikes and expiries must also provide the required protection. A named structure is checked against its actual shape. Naming something “bull call spread” does not make arbitrary legs into one.

Mixed expiries with a sold leg are refused. All-bought Custom plans can use different expiries, but a single combined at-expiry result is then unavailable. Named structures must also satisfy their common-expiry shape rules.

### Direction is chosen by the Action Plan

An ENTER signal means “open my configured plan.” It does not mean “always buy shares” or “always buy a call.” If you attach a long put plan to a signal designed around upward stock momentum, the bot follows that mapping. The system does not automatically reverse the signal to make the economics fit.

> **A structure is a package, not a share.** Two butterfly structures at 1:2:1 request two lower contracts, four middle contracts and two upper contracts.

## 7. How contracts are picked {#picking-contracts}

The plan usually stores **selection rules**, not a permanently chosen option symbol. At each new entry, the resolver tries to turn those rules into actual listed contracts.

### Choosing a strike

| Rule | Plain-language meaning |
|---|---|
| At the money | Look near the underlying's current price. |
| Offset from at the money | Move through the listed strikes by the chosen offset. |
| Exact strike | Request the specified strike. |
| Delta target | Look for a contract near the chosen model sensitivity, within its allowed tolerance. |
| Distance from another leg | Place this strike a specified dollar distance from that leg's strike. |
| Percent of the underlying | Target a strike relative to the underlying's current price. |

**Delta** estimates how much an option's model price responds to a small move in the underlying. A target of 0.50 is a sensitivity target, not a promise of a 50% chance of winning. The picker uses the rule's tolerance; it can refuse when no eligible candidate fits.

The options model needs appropriate rates and dividend information. Missing inputs are not quietly assumed to be zero. Selection uses the contracts and quotes actually available in the watched search window.

### Choosing an expiry

- **Minimum days to expiry:** choose the nearest listed expiry at least that many calendar days away.
- **Nearest weekly:** choose the nearest future listed expiry on the last trading session of its week. It does not choose today's expiry.
- **Same day:** request the decision date's expiry, subject to listing and entry cutoffs.
- **Fixed date:** request that particular expiry date.

Weekends and holidays affect the calendar. The app's session calendar determines the relevant closes, including early closes.

### A usable quote must pass several checks

Both bid and ask must have arrived for the current connection. Data must be live, prices must be valid, and the bid cannot exceed the ask. A sale needs a positive bid. The quote sides must be fresh enough; current entry admission uses a five-second quote-age limit.

IBKR allows a limited number of market-data subscriptions. The bot estimates the option quote lines its search window needs. If the Clerk cannot provide the required lines, or has no option quote source of its own, launch can be refused.

### A preview is a photograph, not a reservation

Preview shows the contracts and prices selected from the quotes held at that moment. It does not reserve money or send an order.

At entry, the bot picks and checks again. Price, available contracts or eligibility may have changed. A spread or cost warning in a Deploy preview can mean “an entry would be skipped now”; Deploy distinguishes those warnings from blockers such as no usable price or an unsupported plan.

Once an entry has filled, EXIT uses **those recorded contracts**. It does not rerun the entry rules and accidentally close a newer expiry or a different strike.

### Reading the model estimates

The preview's directional sensitivity describes how the whole position leans when the underlying moves. Its daily time decay estimates the change over **24 hours of clock time**, with price, volatility and rate held as the model specifies and dividends accounted for. It is not a guaranteed daily profit or loss, and it is not per trading session.

Sold legs reverse the sign of their contracts' decay. If a required leg cannot be estimated, the whole structure's figure is unavailable; the app does not show a misleading partial total. Near a dividend date, read the rough-estimate and early-assignment notes.

## 8. Option prices, orders and fills {#option-orders}

### Five money figures that must not be confused

| Figure | Meaning in this app |
|---|---|
| Premium | Cash paid for the options, or a credit received. A credit has a negative sign. |
| Requirement | The collateral requirement calculated from the structure's exercise payoff, leaving premium out. |
| Fee provision | Estimated fees reserved for the entry. Posted fees may differ. |
| Commitment / set aside | Requirement **plus signed premium plus fee provision**. This is the money the entry claims. |
| Loss at expiry | A payoff-based estimate, shown separately. It does not include every execution, fee or assignment consequence. |

**Bought-call example:** One call at an ask of \$2.00 has a \$200 premium. With no additional payoff requirement, its commitment is \$200 plus the fee provision. If it expires worthless, the premium has been lost; fees are additional.

**Credit-spread example:** A \$5-wide spread sold for \$3.07 per share has a \$500 requirement and a \$307 credit. It commits \$193 before the fee provision. The incoming \$307 is not all free spending money: the position still needs collateral.

These are teaching examples with assumed prices, not quotes from an account.

### Entry starts at the natural price

The entry price uses the **ask for bought legs** and the **bid for sold legs**, multiplied by each leg's ratio and combined. A positive net price is a debit paid; a negative one is a credit received.

The order is a limit order, with the bot's selected wait for a fill. A favorable quote does not guarantee that the broker fills it. The Clerk rechecks prices and money before taking the entry.

### The four option order terms

These terms are chosen at Deploy and fixed for the bot's life.

| Term | Starting value | What it controls |
|---|---|---|
| Widest bid-ask gap per leg | **8% of that leg's midpoint** | Refuses entry when any leg is too wide. |
| Entry wait | **20 seconds**; the form allows 1-30 | How long a resting entry waits before cancellation of its unfilled remainder. |
| Whole-structure round-trip cost limit | **10% of commitment** | Limits the quoted spread cost of immediately opening and closing the structure. |
| Expiry-day close lead | **45 minutes before that session's close** | When the Clerk schedules the expiry-day reduction. |

Round-trip spread cost is the sum of each leg's bid-ask gap, adjusted for ratios, structures and multiplier. It is **not a forecast of future loss**, and it does not include fees in its numerator. Fees are reserved separately and are part of commitment.

For a bid of \$1.90 and ask of \$2.00, the midpoint is \$1.95. The gap is about 5.13% of the midpoint. One contract would lose \$10 crossing that spread immediately, before fees. Passing the per-leg test does not guarantee that the whole-structure cost test passes.

### What happens after the order is sent

The runner allows a bounded window, normally up to 50 seconds after the decision bar's close, to learn the entry outcome. That includes work beyond the owner's fill wait. Starting too late to finish the necessary checks and cancellation proof means the entry is refused.

| Observed outcome | What the bot must remember |
|---|---|
| Completely filled | All requested contracts are held. |
| Partly filled, remainder ended | The filled contracts remain held, even if they do not form a complete structure. |
| Cancelled unfilled / proven rejected | No position was opened by that entry. |
| Still resting / outcome unproven | The app must not assume nothing happened. It keeps the unresolved order and possible holdings in its records. |

A multi-leg order has **one parent order**, but fills belong to its individual legs. Cancelling the parent does not undo contracts already filled. Uneven terminal fills block that bot from taking new exposure while confirmed reductions remain possible.

Real-account option buying power is also compared before and after an entry when the readings are comparable. An unexplained drop larger than the amount reserved can hold the bot. A reading that cannot be compared is recorded as unknown, not as a clean pass.

## 9. Exits, Stop and Flatten {#exits-and-stopping}

There are several ways a position can end. Learn the differences before using the buttons.

| Action or event | What it means |
|---|---|
| **Strategy EXIT** | The strategy's own exit rule fired. The Clerk works to reduce the bot's recorded holdings. |
| **Stop bot decisions** | Ends decision-making and triggers cleanup of working entries. It does not itself sell real-account holdings. |
| **Flatten** | Prepares and then submits a reduction of the bot's confirmed, attributed exposure. Completion needs fill evidence. |
| **Owner-set end** | Stops the bot at the chosen time and applies the selected Sell or Keep instruction where supported. |
| **Option scheduled close** | The Clerk works the option deadline, independently of the strategy's holding rule. |
| **Clear** | Archives a finished registration after its stopped and flat state and lack of outstanding work are provable. It is not a liquidation action. |

An operator Stop is terminal for that bot's run in the current product: use **Deploy again** to create a fresh deployment. Stop cancels the owner-set end. Do not expect that cancelled end to sell the holdings later.

Dry Run has its own ending rules for simulated positions. Stock Dry Runs sell at their last observed price when ending. Option simulation closes need appropriate retained quotes or expiry-settlement evidence; do not assume every failure can manufacture a flat result.

### The owner-set end

The default end is one minute before the current session's close, or the next session if that point has passed. You can choose an allowed future end or no end. The current Deploy flow uses regular hours. Early-close adjustments come from the calendar and are shown to the owner.

A Dry Run cannot choose Keep at its end. For other supported uses, Keep leaves holdings behind. Changing the end is possible only while the applicable edit rules permit it.

### Options have additional deadlines

A held contract gets an expiry-day close, normally **45 minutes before the exchange session ends**. A structure with a sold leg also owes a close **10 minutes before every session's end**. If both apply, the earlier deadline wins.

These are deadlines the system tries to carry out, not guaranteed fill times. The entry cutoff is earlier still, to leave room for the entry wait, cancellation proof and a margin. Holidays and half days change the actual clock times.

Stopping decisions does not erase the Clerk's option close obligations. Neither an earlier strategy exit nor a disabled Live-opening switch should prevent an otherwise permitted close of existing contracts.

### Close what was actually filled

Before an options exit, the Clerk cancels outstanding parent entry orders and determines what contracts are actually held. It closes those holdings, including residual legs from an uneven entry. Option closes use market orders during regular hours; their price is not guaranteed.

An expired contract cannot simply be sold with an ordinary order. It moves to the option-event handling path.

### Expiry, exercise and assignment

**Exercise** uses an option's right. **Assignment** tells an option seller that the obligation has been exercised. Those events can produce stock positions as well as cash movements.

On a real account, the Clerk reads broker activities and matches them to the recorded option lots. Ambiguous or incomplete events can cause holds and require review. The disappearance of an option symbol is not, by itself, proof of a clean end.

Simulated expiry is different: it uses **cash-only exercise value**, with bought contracts receiving value and sold contracts paying it. It delivers no shares and does not claim to reproduce real assignment. Missing required price evidence can leave a simulated contract unresolved.

> **After any exit, read the holdings and working orders.** “Requested,” “cancelled,” “stopped” and “flat” are different statements.

## 10. Your operating routine {#operating-routine}

### Before deploying

1. **Open the intended Alpaca account.** Read its actual mode badge. Paper, Shadow and Live have different consequences.
2. **Choose a strategy and inspect its evidence and account permission.** Validation applies to particular code and settings; it is not a promise of profit. “Allow on Paper” or “Allow on Shadow” grants the displayed permission; it does not launch a bot or authorize Live trading.
3. **Check “listens to” and “trades.”** Say the relationship aloud: “This bot watches ___ and trades ___.”
4. **Choose the quantity and budget separately.** For options, count structures and then understand the contracts each structure contains.
5. **Read the exit rule and end setting.** Know what happens if the position remains open late in the day.
6. **For options, inspect each leg, expiry and order term.** Read the picked contracts, money figures and any unavailable estimate.
7. **Read “Can't deploy yet” and the warnings.** Check the account's daily loss limit in Settings, resolve the named blocker and review any required acknowledgement.
8. **Confirm the current review.** Changes to settings or money can require a new review. An earlier review is not approval for a changed request. Live also requires the displayed typed account-and-dollar confirmation.

Deploy records the bot's plan and settings so the runner cannot quietly change its recipe halfway through the run. Current proof and account checks still apply at launch.

### While it runs

Read the bot in this order: **data health → latest decision → admission result → order → fills → holdings and money**.

If the bot is quiet, check whether it is warming up, waiting for a completed decision bar, already holding, or simply seeing no qualifying signal. Quiet is not automatically broken.

If it is blocked, read the stated cause. Restoring the data connection, supplying missing account evidence, or reconciling an uncertain order is different from changing strategy parameters.

### When you want to finish

Decide whether you want to stop decisions, close holdings, or both. Use the controls that express that intent. If the bot can still enter again, closing a position alone is not a substitute for ending its decision-making.

Afterward, verify that holdings and working orders match your intention. A stopped bot with exposure still needs attention and still claims money. Clear becomes available only when the required proof says it is finished.

### A complete option example

A bot listens to an ETF using EMA Crossover Signal. Its Action Plan buys one call structure. The latest complete bar has a new upward cross, a large enough gap and RSI 60.

The resolver chooses an eligible contract and rechecks its live bid and ask. Suppose they are \$1.90 and \$2.00. The previewed premium is \$200 for one standard contract, plus the fee provision. Both cost limits and the account's remaining checks must pass.

The order is sent at the admitted limit. If it fills, the bot records the exact contract. After the configured five decision bars, the strategy requests EXIT, unless a nearer owner-set end or option deadline has already intervened.

If no contract fills and cancellation is proven, there is no holding to sell. If the response is lost, the Clerk looks up the existing order; it must not create a second purchase merely because the first answer is missing.

## 11. Evidence and troubleshooting {#reading-evidence}

### Read the story, not just the last status

**Decision evidence** explains what the strategy saw. **Order evidence** explains what was sent and acknowledged. **Execution evidence** explains what filled. **Custody evidence** explains what is still held and which bot owns it.

The records are retained across process restarts. Recovery uses recorded identities and broker lookups to resolve outstanding work. Restarting is not permission to forget an uncertain order or replay an old signal as a new trade.

| What you see | Likely meaning | What to check next |
|---|---|---|
| No entry despite an attractive chart | No fresh qualifying signal, indicators not ready, already holding, or a later check failed. | The exact decision bar and recorded checks. |
| Decision late | The decision missed its allowed time window. | Feed health, reconnect history and the recorded timing. |
| Option quotes missing, old or delayed | A complete live quote is unavailable for the current connection. | IBKR connectivity, subscription capacity and each leg's quote evidence. |
| Structure refused | The legs, protection, strikes or expiries do not satisfy the plan rules. | The specific leg and reason named by the preview. |
| Entry too expensive | A leg's spread or the whole package's cost exceeds a sealed limit. | Both cost tests, not just the premium. |
| Insufficient money | Bot allocation, account funds, collateral or pending orders leave too little room. | The money review and current reservations. |
| Some legs filled | The bot can hold incomplete exposure even without one complete structure. | Per-leg executions, cancellation proof and the reduction offered. |
| Stop completed, position remains | Decision-making ended; the holdings were not sold. | The bot's holdings, option deadlines and Flatten review. |
| Unknown order or mismatch | Evidence is incomplete or contradicts what the books expected. | Account notices, reconciliation and the custody timeline. |
| “Not estimated” | A required model input or result is unavailable. | Its stated reason. Do not read it as zero risk. |

### Why the system sometimes refuses to act

There is one authorized account writer. Money is reserved before it can be reused. Repeated commands and recovered responses are matched to durable identities. New exposure is restricted when evidence is uncertain.

The purpose is to avoid guessing about money. A hold may still allow a confirmed reduction, while another kind of uncertainty can prevent even a safe Flatten plan until the position is proven. Read the offered action for the specific notice.

### How to read research claims

A **golden fixture** stores known inputs and reference outputs. A parity test checks the implementation against that evidence within a stated tolerance.

That supports a particular numerical or behavioral claim. It does not automatically prove future profitability, another symbol's results, real fill quality or an options substitution.

Golden Validation and the strategy-validation records also bind to specific configurations. Editing code or changing a tested parameter set can invalidate the evidence a launch depended on. A human evidence-only override is recorded explicitly and cannot bypass unrelated account or execution checks.

## 12. Plain-language glossary {#glossary}

| Word | Meaning |
|---|---|
| Action Plan | The instructions connecting ENTER and EXIT to the instruments actually traded. |
| Admission | The decision about whether a proposed action may proceed now. |
| Bar | A summary of trading over a completed time interval. |
| Basis point | One hundredth of a percentage point. 100 basis points equals 1%. |
| Bid / ask | Bid is what buyers offer; ask is what sellers request. Their difference is the spread. |
| Budget | Money allocated to one bot, distinct from its order quantity. |
| Clerk | The account's authority for orders, money, custody and durable evidence. |
| Collateral / requirement | Money held to support obligations in a position. |
| Commitment | What an entry sets aside, including signed premium, requirement and fee provision. |
| Contract multiplier | How many shares the quoted option premium represents; this entry path requires 100. |
| Custody | The record of what is held, by whom, and which orders can change it. |
| Delta | A model estimate of option-price sensitivity to a small underlying-price move. |
| ENTER / EXIT | A strategy's request to open or close its configured position lifecycle. |
| Expiry | The date an option contract ends. Trading and close deadlines need separate checks. |
| Exposure | Holdings or pending trades that can change the account's financial result. |
| Fill / partial fill | All or part of an order actually executed. |
| Flat | No remaining position, as proven by the relevant custody records. |
| Leg / structure | One component / the full package of an options trade. |
| Limit / market order | A limit constrains price; a market order seeks execution at available prices. Neither guarantees the outcome you hoped for. |
| Long / short | Holding something bought / owing something sold to open. |
| Mark | A current valuation using available pricing evidence; not a completed sale. |
| Premium | The option price paid or received, before interpreting collateral and fees. |
| Reconciliation | Comparing the app's records with broker evidence and resolving differences. |
| Sealed configuration | The recorded recipe and terms that a deployed bot is bound to. |
| Slippage | The difference between an expected trading price and the actual fill price. |
| Strike | The price at which an option's underlying can be bought or sold through exercise. |
| Time decay | The modelled change in option value as time passes, other inputs held as specified. |
| Underlying | The stock or ETF an option contract refers to. |
| Warmup | Reading enough prior observations to prepare indicators and strategy state. |

### Check your understanding

**The bot is stopped. Must its positions be gone?** No. Read custody and the recorded ending.

**An order was accepted. Did every contract fill?** Not necessarily. Read execution evidence.

**The stock backtest was profitable. Does that validate buying calls instead?** No. That changes the traded product and its economics.

**A credit arrived. Is it all free cash?** No. Sold option holdings can require collateral.

**The preview found a contract. Is that contract reserved for the next signal?** No. Entries select and check again; exits use the contracts actually recorded.

## 13. Implementation references {#source-notes}

This handbook is a reader-facing guide to the checked-in implementation, not an additional source of trading rules. When investigating a particular run, its recorded settings, decisions, executions and current account evidence matter most.

The source links below point to the reviewed revision. They are useful to someone auditing the explanation; you do not need to read code to use the manual. GitHub access may be required.

- **Strategy rules and parameters:** [signal algorithms](https://github.com/tim1016/learn-ai/tree/2a04922f/PythonDataService/app/engine/strategy/algorithms) and [program parameters](https://github.com/tim1016/learn-ai/tree/2a04922f/PythonDataService/app/engine/strategy/programs).
- **Live decisions and supported runtimes:** [strategy runner](https://github.com/tim1016/learn-ai/blob/2a04922f/PythonDataService/app/services/bot_trade_strategy.py), [continuity policy](https://github.com/tim1016/learn-ai/blob/2a04922f/PythonDataService/app/services/feed_continuity_policy.py) and [validation catalog](https://github.com/tim1016/learn-ai/blob/2a04922f/PythonDataService/app/services/broker_v2_panel/strategy_catalog.py).
- **What a plan can contain:** [Action Plan rules](https://github.com/tim1016/learn-ai/blob/2a04922f/PythonDataService/app/schemas/action_plan.py), [structure starting points](https://github.com/tim1016/learn-ai/blob/2a04922f/PythonDataService/app/options/structure_catalogue.py) and [Clerk option-plan admission](https://github.com/tim1016/learn-ai/blob/2a04922f/PythonDataService/app/broker/alpaca/clerk/option_effects.py).
- **Option selection, prices and money:** [resolver](https://github.com/tim1016/learn-ai/blob/2a04922f/PythonDataService/app/options/resolver.py), [entry checks](https://github.com/tim1016/learn-ai/blob/2a04922f/PythonDataService/app/options/entry.py), [commitment and requirement](https://github.com/tim1016/learn-ai/blob/2a04922f/PythonDataService/app/options/requirement.py) and [order-term ranges](https://github.com/tim1016/learn-ai/blob/2a04922f/PythonDataService/app/options/order_terms.py).
- **Option fills and deadlines:** [option runner](https://github.com/tim1016/learn-ai/blob/2a04922f/PythonDataService/app/services/bot_option_run.py), [entry outcome meanings](https://github.com/tim1016/learn-ai/blob/2a04922f/PythonDataService/app/options/entry_came_to.py) and [calendar-derived deadlines](https://github.com/tim1016/learn-ai/blob/2a04922f/PythonDataService/app/options/deadlines.py).
- **Stop, end and Clear:** [Stop custody evidence](https://github.com/tim1016/learn-ai/blob/2a04922f/PythonDataService/app/services/bot_carryover.py), [owner-set end](https://github.com/tim1016/learn-ai/blob/2a04922f/PythonDataService/app/services/bot_end.py) and [archive eligibility](https://github.com/tim1016/learn-ai/blob/2a04922f/PythonDataService/app/broker/v2panel/action_policy.py).
- **Options rollout and later multi-leg decisions:** [ADR 0076, especially decisions 27 and 28](https://github.com/tim1016/learn-ai/blob/2a04922f/docs/architecture/adrs/0076-option-structures-on-the-money-path.md). These later decisions supersede the earlier single-leg-only implementation limits.
- **Provider boundary and account operation:** [account runbook](https://github.com/tim1016/learn-ai/blob/2a04922f/docs/runbooks/add-an-alpaca-account.md).

To save this edition, select **Print / Save PDF**, choose **Save as PDF**, and use portrait orientation on Letter or A4 paper. The print layout removes the app controls, keeps the contents and uses dark text on white paper. Browser-added headers and footers can be switched off in the print dialog.
