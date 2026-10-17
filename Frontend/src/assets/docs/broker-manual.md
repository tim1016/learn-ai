## 1. Your first account visit {#first-visit}

**Your goal:** identify the account, find its activity, and understand where to look before placing any order.

Start with a **Paper** account. Paper trading uses simulated money. A **Live** account can create real positions and charges. Simply reading an account page does not place a trade.

1. Open **Alpaca → Accounts**, or use **Open broker accounts** above.
2. Select the intended account. Read its identity and Paper or Live environment before opening deployment or order controls. Do not identify an account only by its balance.
3. Open **Home**. Find the account's cash, holdings and readiness information. Connection health alone does not prove that a price is fresh enough to trade.
4. Open **Activity**. Select Today, 30D or 60D. Expand **Order records and recovery**, then use **Transaction history → View details** on an existing row to follow its status, fills and charges. An empty account may have no activity yet; that is not an error.
5. Open **Settings** to read the account's configured limits and operating choices. Leave them unchanged during this orientation.
6. Return to Activity and answer: what was requested, what actually filled, what is still held, and which fees are confirmed?

> **You have finished the tour when:** you can name the account and environment, locate its holdings and activity, and distinguish an order request from a confirmed fill. You do not need to submit a practice order to complete this lesson.

For deployment steps, continue with the [bot operating routine](/docs/bot-business-manual#operating-routine). For a historical exercise first, use the [Options Backtest walkthrough](/docs/options-backtest-manual#first-run).

## 2. Who does what {#providers-and-modes}

**Your goal:** know which service supplies each piece of evidence.

| Part of the system | Its job |
|---|---|
| IBKR live data | Supplies live market prices and trading-status evidence through the read-only connection. |
| Alpaca | Holds the trading account and handles submitted orders, executions and account activities. |
| Historical data lake | Supplies the stored historical data used by Options Backtest. Historical-data access is separate from live IBKR subscriptions. |
| Our app's Clerk | Maintains the account's trading record, checks requests, and follows order and position evidence. |

Our live setup retains **IBKR market data with Alpaca execution**. Direct Alpaca market-data subscriptions are not part of this configuration. A data-feed problem needs a connection or entitlement repair, not a switch to another provider.

### Choose the right trading world

| Mode | What happens |
|---|---|
| Historical backtest | Replays a setup against past data. No broker order is submitted. |
| Dry Run | Runs a private simulated trading world. Its funds and results are not the broker's actual holdings. |
| Paper | Submits to Alpaca Paper. Orders and activity come from that simulated brokerage environment. |
| Live Shadow | Simulates execution while observing the live environment. Its simulated fills are not real broker fills. |
| Live | Can submit real orders using the account's permissions and applicable checks. |

**Check before moving on:** account environment and bot mode are separate facts. A successful backtest, Dry Run or Paper trade does not itself authorize or prove a Live deployment.

## 3. Understand your money {#money}

**Your goal:** separate a trading expense from money temporarily committed to a position.

| Amount | How to read it |
|---|---|
| Cash | Money recorded in the account. It is not automatically the amount available to withdraw or spend on a new option. |
| Premium | The option price paid or received. Read the contract quantity and multiplier when interpreting the amount. |
| Buying power | The broker's allowance for additional exposure under the account's rules. |
| Collateral or requirement | An amount pledged against obligations. It is not a commission. |
| Fee provision | An estimate set aside for charges. A later posted amount may differ. |
| Commitment | What the proposed entry sets aside, including its premium, requirement and fee provision. |
| Realized profit | The recorded outcome of completed transactions, subject to the fee evidence available. |
| Unrealized profit | A valuation of a position still held; it can change before a closing fill. |

### Follow the direction of the money

When buying an option, premium leaves cash. When opening a sold option, premium arrives, but the obligation may require collateral. That incoming premium is not necessarily free to spend.

Closing a position can release collateral, but the broker may still require money available **before** a particular buy-back. A close that looks affordable after releasing collateral can be refused before it executes.

**Practice:** inspect an existing option position or preview. Identify premium, requirement and estimated fees separately. If a figure is missing or stale, treat it as unknown rather than zero. Read the [option prices and order lesson](/docs/bot-business-manual#option-orders) for the app's preview and order terms.

## 4. What gets charged {#fees}

**Your goal:** identify the charge, its trigger, and whether the amount is estimated or posted.

The following is a **published-rate snapshot checked October 16, 2026**, using Alpaca's October 12 schedule. It describes ordinary retail stock/ETF options. Professional flow, index options, partner arrangements and special routing can have other commissions. This is not a personalized account invoice.

| Charge | Trigger | Published rate |
|---|---|---|
| Commission | Eligible retail stock/ETF-option trade | \$0, subject to exceptions |
| SEC | Sale | Sale proceeds × 0.0000206 |
| FINRA TAF | Sale | Currently \$0 per contract |
| CAT | Buy and sale | \$0.000003 per equivalent share; use the contract multiplier |
| ORF | Buy and sale | \$0.015 per contract |
| OCC clearing | Buy and sale | \$0.025 per contract |

Each fee type aggregates per account/day, then rounds upward to cents. Accrual affects balances intraday; posting occurs at day-end. Index options and professional pricing require their own applicable schedule. [Alpaca fee schedule](https://files.alpaca.markets/disclosures/library/BrokFeeSched.pdf).

The TAF pause runs through December 2026. Its current zero is not permanent. [FINRA filing](https://public-inspection.federalregister.gov/2026-19392.pdf).

### Read a round trip correctly

An opening buy followed by a closing sale has two executions. Charges applying to both sides can apply at each one; sale-only charges apply to the sale. A sold option opens with a sale and closes with a buy, so sale-only does not mean closing-only. A multi-leg position has executions in each leg.

Premium, collateral, spread and slippage are different from the fee rows above. A commission-free trade can still have costs. No separate exercise or assignment service charge has been verified for this guide; do not infer that it is zero from its absence here.

### Choose a backtest fee policy deliberately

**Estimated** uses the app's dated option-fee model. **Per contract** uses your chosen charge on each fill. **Not charged** deliberately omits fees. Those choices do not change the broker's invoice. The backtest also rounds estimates per fill, which can differ from daily aggregation. Continue with [matching fees to the account](/docs/broker-manual#reconcile).

## 5. Data and account costs {#data-and-account-costs}

**Your goal:** identify costs outside an individual option fill.

### IBKR market-data subscriptions

Data subscriptions belong to IBKR usernames. Multiple worker client IDs are not themselves separate subscription usernames. Partial-month use generally incurs a full month's subscription; recurring charges are billed within the first three business days of the following month. Packages, professional status, taxes, snapshots and waiver conditions affect the bill. [IBKR market-data pricing](https://www.interactivebrokers.com/en/pricing/market-data-pricing.php?menu=B).

Read the actual subscription page for the account. Alpaca execution does not establish that an IBKR trading-commission waiver has been earned. Keep historical data-plan costs separate as well; they are not simulated per-trade charges.

### Account services

| Service | Published snapshot |
|---|---|
| Outbound wire | Domestic \$15; international \$35 |
| Returned ACH / outbound ACATS | \$25 each |
| Currency conversion | 1.5%, capped at \$40 per transfer |
| Margin borrowing | Default 6.50% annualized; qualifying Elite 100k 5.00%, applied to daily balances |

Borrowing stock, special transfers and corporate actions have additional terms. Read the applicable entry before using the service. [Alpaca fee schedule](https://files.alpaca.markets/disclosures/library/BrokFeeSched.pdf).

**Keep this account worksheet:** account environment; retail or professional classification; applicable partner/routing plan; IBKR username and subscribed packages; actual monthly data total; margin/borrow terms if used; transfer method; and the latest statement date. Public rates cannot fill in these account-specific facts.

## 6. Follow an order {#orders}

**Your goal:** know what has happened, what is merely requested, and what remains to do.

1. **Before submission:** read the account, instrument, side, quantity, price terms, commitment, fee provision and any readiness problem.
2. **After submission:** find the order in Activity. A sent request or accepted order is not proof that a position exists.
3. **While working:** watch for fills. A partial fill establishes some exposure even if the remainder is later cancelled.
4. **After a cancel or replace request:** inspect the confirmed outcome. A fill can arrive while cancellation is in progress. Follow the replacement chain instead of counting every order version as a new position.
5. **At the end:** compare filled quantity with holdings and cash activity. Read pending work and unresolved evidence before declaring the trade finished.

| State | What it means for you |
|---|---|
| Accepted / Working | The order exists and can still fill. |
| Partially filled | Some quantity traded. Confirm what remains working and what is held. |
| Filled | The recorded executed quantity has traded. Fees and later lifecycle events may still arrive. |
| Cancel requested | The cancellation outcome is not yet established. |
| Cancelled | The unfilled remainder ended; earlier fills still count. |
| Rejected | Read the broker's reason and check for any separately confirmed fills or holdings. |

**Practice:** take one existing Paper order and follow its request, fills and final status. If the account has no order history yet, complete the bot manual's [operating routine](/docs/bot-business-manual#operating-routine) before attempting a deployment.

## 7. Know the deadlines {#timing}

**Your goal:** distinguish exchange hours, our app's scheduled actions and the broker's restrictions.

All clock times in this section are **New York time (ET)**. Session times come from the trading calendar; holidays and early closes change the actual deadlines. An expiry date by itself is not a closing-order deadline.

| Timing rule | What to expect |
|---|---|
| Our expiry-day close | Default: 45 minutes before the calendar session close. |
| Our daily close for a sold-leg structure | Default: 10 minutes before session close. If expiry-day close also applies, the earlier deadline wins. |
| Our entry cutoff | Earlier than the due close: it allows for entry waiting, cancellation confirmation and a margin. Read the deployed terms and displayed deadline. |
| Broker's published 0DTE cutoff | Orders must be submitted before 3:15 p.m. ET generally; before 3:30 p.m. ET for broad ETFs such as SPY and QQQ. |
| Product trading end | Usually 4:00 p.m. ET; some eligible products trade to 4:15 p.m. ET. A product's later hours do not extend our app's calendar-based close rule. |

The broker rows are published guidance, not an account-specific confirmation: [Alpaca 0DTE cutoffs](https://alpaca.markets/support/what-are-the-cutoff-times-for-trading-0dte-options-on-alpaca) and [option expiration](https://alpaca.markets/support/what-is-option-expiration). Our app currently identifies Alpaca's own expiry-day cutoff as unverified for the account. Use the displayed restrictions and confirmed account procedure; do not assume the public cutoff is implemented as the app's deadline.

> **A scheduled close is an action the system tries to complete.** It is not a guaranteed fill time. A rejected order, missing price, halt or unresolved holding can prevent a clean close.

Before a session ends, check the position's due close, working entries, remaining holdings and any failed close. Waiting until an exchange's final trading minute can leave no time to resolve a problem.

## 8. Close, stop or reach expiry {#closing}

**Your goal:** finish with confirmed holdings and orders, rather than relying on a button label.

| Action | What it does |
|---|---|
| Strategy exit | The trading rule requests an exit from the bot's recorded holdings. |
| Stop bot decisions | Ends decision-making and initiates cleanup of working entries. It does not itself sell real-account holdings. |
| Flatten | Prepares and submits a reduction of confirmed attributed exposure. Fills must confirm completion. |
| Owner-set end | Applies the deployment's supported Sell or Keep choice at its chosen end. |
| Option scheduled close | Works the option close obligation independently of the strategy's holding rule. |
| Clear | Archives an eligible finished registration. It is not a liquidation action. |

An operator Stop is terminal for that run. Use **Deploy again** for a new deployment. Stop cancels the owner-set end, so do not expect that cancelled end to sell later. Held-option scheduled-close obligations are separate; read their actual status and any action left to the owner.

### Exercise and assignment can create shares

Exercise uses a bought option's right. Assignment applies the resulting obligation to a seller. A real account can receive stock positions and cash movements. A disappearing option symbol alone is not proof that the account is flat.

Alpaca describes automatic exercise for options in the money by at least one cent and possible pre-expiry liquidation when buying power is insufficient. Follow the position's actual broker activities. [Alpaca options overview](https://docs.alpaca.markets/us/docs/options-trading).

### Do Not Exercise needs a confirmed procedure

Do Not Exercise (DNE) is an instruction about exercise, not an ordinary closing trade. Public Alpaca instructions differ by channel and publication date: the older email guidance names an earlier cutoff, while newer expiration guidance discusses later best-effort handling and an API endpoint exists. Confirm the supported method and exact account deadline before relying on DNE. [DNE support](https://alpaca.markets/support/what-is-a-do-not-exercise-dne-and-how-can-i-place-a-dne-on-my-options-position), [expiration guidance](https://alpaca.markets/support/what-is-option-expiration), [DNE API](https://docs.alpaca.markets/us/reference/optiondonotexercise).

Simulated expiry in our live simulation modes is cash-only and does not deliver shares. Historical Options Backtest has its own closing and marking rules. Neither should be mistaken for a complete rehearsal of real assignment.

**After any close:** inspect holdings, remaining orders, exercise/assignment activity and fee evidence. “Stopped,” “cancelled” and “flat” are different facts.

## 9. When money becomes available {#settlement}

**Your goal:** keep execution, fee posting, settlement and withdrawal availability separate.

1. **Execution:** a fill establishes the trade at a price and quantity.
2. **Fee evidence:** the displayed cost may begin as an estimate and later gain broker-posted evidence.
3. **Settlement:** option premium and equity-option exercise/assignment generally settle on **T+1**, the next business day. It is not a promise of funds arriving exactly 24 hours later. [OCC/OIC settlement explanation](https://prd-web.optionseducation.org/news/understanding-t-1-conversion).
4. **Withdrawal availability:** check the account's eligible amount and funding holds. A settled trade and a deposit's withdrawal hold are different things. Processing depends on the transfer method and account conditions. [Alpaca deposits and withdrawals](https://alpaca.markets/support/deposits-and-withdraws-faqs).

Paper lifecycle activities may arrive the next day after positions or balances have already changed. Do not manufacture an exercise or assignment record from the balance change alone. [Alpaca options overview](https://docs.alpaca.markets/us/docs/options-trading).

**Check before withdrawing:** the eligible withdrawal balance, outstanding obligations, any recent deposit hold, the selected transfer method and its fee. Use the broker's current account information rather than the backtest's Ending cash.

## 10. Match fees to the account {#reconcile}

**Your goal:** explain a difference without forcing an estimate to match the broker.

| Evidence state | Meaning |
|---|---|
| Estimated | A provision or forecast under the fee model. |
| Modeled settled | A daily model calculation; it is still not a posted broker charge. |
| Observed | Supported by broker activity. Allocation among bots or orders may still be inferred if the broker posts a combined charge. |

### Follow this reconciliation routine

1. Choose the account and trading day. Confirm whether its activity is Paper or Live.
2. Gather all fills in the relevant population, including orders outside a particular bot. Account-day charges can include more than the bot you are viewing.
3. Compare quantities, sides and instruments. A sold opening option is still a sale for sale-side fees.
4. Compare the model by fee type with posted fee activities, allowing for the model's rounding basis and the broker's reporting delay.
5. Separate a confirmed account total from an inferred allocation to an individual bot or order.
6. Keep an unexplained difference visible. Record the day, fee type, related orders and broker activity identifiers for investigation.

The project has recorded a Paper example where modeled daily fees and posted charges differed. Do not change a rate merely to make one example agree. Backtest per-fill rounding is one known reason for differences, but it is not an explanation for every discrepancy.

**Check your understanding:** you should be able to say whether a displayed fee is estimated or observed, what population it covers, and whether its allocation is exact or inferred.

## 11. When something looks wrong {#troubleshooting}

**Your goal:** preserve the known facts and find the missing evidence.

| Symptom | Next action |
|---|---|
| Connected, but entry is held | Read the readiness reason. Check data freshness, trading status, account evidence and applicable limits separately. |
| IBKR prices are missing | Check the read-only Gateway connection, account entitlement and feed state. Retain the IBKR provider configuration. |
| Fees are unknown | Inspect missing or late activity. Do not replace unknown with zero to admit a trade. |
| A buy-back is rejected | Read the broker reason and available buying power. Collateral released afterward may not fund that order beforehand. |
| Stop completed but holdings remain | Inspect positions and working orders. Stop is not proof of a sale; follow the supported closing action and scheduled-close status. |
| An expired option disappeared | Look for exercise, assignment, resulting shares and cash activity. |
| Paper activity is late | Recheck the documented lifecycle reporting timing. Keep unconfirmed events unresolved. |
| Withdrawal amount is smaller than cash | Inspect unsettled activity, collateral, deposit holds and the broker's eligible withdrawal amount. |
| Posted fees differ from the estimate | Use the reconciliation steps above and retain the difference for investigation. |

For a support request, retain account environment, order/activity identifiers, affected instrument, displayed times and timezone, exact error and the last confirmed holdings. Never attach API keys or connection secrets.

## 12. Words and official sources {#glossary}

| Term | Plain meaning |
|---|---|
| 0DTE | An option on its expiration day. |
| Commission | A broker's charge for executing a trade, distinct from pass-through fees. |
| Spread | The gap between the quoted buy and sell prices. |
| Slippage | The difference between an execution assumption or reference and the eventual fill; the backtest applies its chosen model. |
| Settlement | Completion of the trade's cash and security obligations on the settlement schedule. |
| Exercise / Assignment | Using an option's right / receiving the resulting obligation as its seller. |
| DNE | Do Not Exercise instruction. |
| Reconciliation | Matching recorded trades, money and broker evidence, with unexplained differences retained. |

Use the source links beside each rate or policy. Rates and operating procedures change. Before acting on an account-specific deadline or charge, verify its applicable account arrangement. The October 16, 2026 review date identifies this guide's public-source snapshot, not a guarantee that a rate remains current.

Continue with [the bot business manual](/docs/bot-business-manual) for deployment and operating controls, or [Options Backtest](/docs/options-backtest-manual) to practice interpreting a historical result.
