# ADR 0076 — Option structures on the money path

**Status:** Accepted 2026-10-06. It gains a section as each part of #2913 it decides is built.
**Provenance:** Owner decisions in [#2913](https://github.com/tim1016/learn-ai/issues/2913), grilled on 2026-10-05 and revised on 2026-10-06 after a review against the code and Alpaca's documents.
**Related:** ADR 0012 (the Action Plan, and its 2026-10-06 amendment that defines an option structure), ADR 0022 (i) (the option expiry anchor), ADR 0059 (the live envelope and the budget, which cover US equities only until this ADR's later sections extend them), ADR 0068 (Python owns the math).

## Context

A bot may listen to one symbol's signal and trade option structures on any underlying. Nothing on the trading side could do that: an order was one stock leg, money was counted per share, and no rule said what an option position costs to hold or what the bot must have before it opens one.

Alpaca's order and account references (read 2026-10-06) describe no call that says, before an order, what that order will cost. So the bot works it out itself from Alpaca's published rule, and compares with what Alpaca took afterwards.

## Decision

### 1. A contract has one spelling

An option contract is its root symbol, its expiry, call or put, and its strike. The expiry is the date's 16:00 New York anchor. The strike is held in whole thousandths of a dollar, which is the unit the OCC symbol writes it in, so no float decides which contract an order names. The symbol Alpaca uses (`AAPL240119C00100000`) is built from those four and read back from them, and anything that is not such a symbol is refused.

A symbol cannot say that a contract delivers 100 shares or is American. Those are facts of the listing, to be checked where a contract is picked. The one thing the spelling shows is an adjusted contract, whose root carries a digit; it is refused.

### 2. Five money quantities, kept apart

All are whole cents. The fee provision is the one figure that rounds, up to the next cent, where it is worked out. Nothing else is rounded; an amount that does not come to whole cents is refused.

| Quantity | What it is | Sign |
|---|---|---|
| **Net entry premium** | What the entry pays or receives: bought legs' prices minus sold legs' prices, times ratio and multiplier | Positive is paid, negative is received. This is the sign Alpaca's multi-leg limit price carries |
| **Broker requirement** | Alpaca's maintenance requirement by its published rule. Premiums are left out | Zero or more |
| **Entry commitment** | What the bot sets aside when an entry is accepted: requirement plus premium plus the fee provision, for the structures requested | Above zero |
| **Loss at expiry, estimate** | The most the structure loses if every leg has the same expiry, is held to it, is never exercised or assigned early, and settles at its exercise value, before fees | Zero or more |
| **Free budget** | What the bot's budget has left for a new entry (ADR 0059) | Can go below zero |

- **The requirement and the commitment are different numbers by design.** A $5-wide call spread sold for $3.07 has a requirement of $500.00 and a commitment of $193.00 before fees. Comparing the two is never an error.
- **None of them is a cap.** The commitment decides whether an entry may start. A market exit, fees, an early assignment or a close that does not complete can each cost more than the estimate.

### 3. The requirement is the lowest point of the exercise payoff

The requirement sums every leg's exercise payoff as a function of the underlying's price and takes the lowest point. It is read at a price of zero, at every strike, and from the slope above the highest strike. The strikes alone are not enough: a put structure can be at its worst at zero, and a call structure can keep losing above its last strike.

- A loss that grows without limit above the highest strike is refused. That reason is given first, ahead of any about expiries.
- Alpaca's page says to find the price with the lowest payoff. Reading it at zero and from the slope above the top strike is this repo's way of finding it.
- A requirement plus premium of zero or less is refused. Such a result means the prices do not belong together.
- A structure priced under what it must be worth at expiry is admitted, with a loss estimate of zero. For one made of bought legs only, such as a 440 call and a 460 put bought for less than $20, those prices do not belong together either, and whether to refuse them is the owner's to say. It is not always a fault: a box of four legs is worth the same at every price and properly costs a little less, by the interest to expiry.
- A bounded payoff does not prove Alpaca will take the structure as one order. Cover is a separate check.

### 4. Mixed expiries

Alpaca's rule works a position with several expiries out expiry by expiry and takes the largest figure. Its documents do not say how that treats a sold leg covered by a bought leg that expires later.

- A structure with mixed expiries and a sold leg is refused: its commitment cannot be computed. This stands until the treatment is verified on the account.
- A structure with mixed expiries and bought legs only is admitted. Its requirement is zero under either reading.
- For any structure with mixed expiries the loss at expiry is not estimated, and is shown as "not estimated". A leg that outlives another is worth something no exercise payoff describes.

The owner chose on 2026-10-06 to keep mixed expiries on these terms. The recommendation had been one expiry per structure.

### 5. Round-trip cost

A per-leg limit on the bid-ask gap does not bound what a structure costs to trade. Two legs with mids of $5.00 and $4.90, each four cents wide, cost $0.14 to open against each other and return $0.06 closed at once.

- The **round-trip cost** is the sum over the legs of ask minus bid, times ratio and multiplier. It is always worked out.
- An entry is to be refused when it is more than the **structure cost limit**: a percent of the entry commitment, 10 by default, set at Deploy. The owner chose this on 2026-10-06. The comparison is built; the setting arrives with Deploy. The commitment is the measure because it is always above zero; a percent of the structure's mid value breaks down when that value is near zero.

### 6. The pricing model matches the contract

Stock and ETF options are American: the holder may exercise early. Everything this repo priced options with before is European Black-Scholes, which prices a different contract. Delta and implied volatility for a contract a bot may pick come from an American model.

- **The model is a Leisen-Reimer binomial tree,** with the exercise test at every step. It was chosen over a finite-difference solver because it can be proven: with no dividend it is the same construction as QuantLib 1.42.1's `BinomialLRVanillaEngine`, and the two agree to 1e-9 on 400 cases from thirty days to fifteen minutes to expiry (fixture `AM-001`). A finite-difference solver could only be shown to agree with another to within both grids.
- **Time to expiry is a fraction of a 365-day year, to the millisecond.** A date-based day count gives a contract expiring today no time at all.
- **Dividends are cash amounts on dates, given explicitly, and held in escrow:** the tree is built on the price less the present value of the dividends still to go ex, and a node is exercised at that plus the dividends not yet paid. There is no dividend yield, and no default of none: a caller pricing a stock that pays nothing says so.
- **With dividends the tree is proven only to its own discretization error. This is an accepted divergence from the strict tolerance.** No pinned engine builds this tree with cash dividends, so there is nothing to match to 1e-9. Against QuantLib's finite-difference engine under the same escrowed model, itself good to about 5e-5, the tree is within 5.9e-4 in price and 7.2e-5 in delta at 2,001 steps (fixture `AM-002`, held to 1e-3 and 2e-4). An implied volatility solved from that engine's prices is within 3.0e-4 of its volatility inside one standard deviation of the money.
- **The other cash-dividend model would move the answer more than the step count does.** QuantLib's "spot" model changes the same cases by up to 8e-2 in price and 3e-3 in delta. Escrowed is the choice; it is the one the tree can carry.
- **The default is 401 steps.** Against the same second engine that costs up to 3.2e-3 in price and 4.2e-4 in delta, with or without a dividend; the test holds it to half a cent and a thousandth of delta. A delta rule's own tolerance is 0.05.
- **A solve with no answer says why and carries no number.** The reasons are kept apart: under a minute to expiry; a price under what exercising now pays; a price no higher than the model's at the lowest volatility tried, which includes an option priced at exactly its exercise value, whichever way its cents round; a price above the model's at the highest; no convergence, which is also the answer when the tree resolves the strike at no volatility it tries (a strike far from the price within a couple of minutes of expiry); an input the model has no meaning for. A vendor's implied volatility or Greeks are never put in a failure's place (ADR 0071 Decision 1, which this extends from the Polygon recorder to any quote a bot prices).
- **A strike dozens of standard deviations from the price is not priced.** The tree's branch sizes are rounding error there. It answers nothing, never a guess.

### 7. A structure is checked after its contracts are picked

A plan's legs are rules, and independent rules can land on a shape nobody meant. A bounded loss does not prove the result is the structure the owner named. So the picked contracts are checked before an order is built from them, and wherever a plan is previewed.

- **Every structure.** One contract per leg of the plan, of the leg's kind, on the plan's underlying. No contract in two legs. Every sold leg has a bought leg of its kind, contract for contract, that does not expire before it. A resolved structure takes each leg's side and ratio from the plan, so it cannot differ from what was declared.
- **A named structure, also its own shape.** One expiry, and:

  | Structure | Must hold |
  |---|---|
  | Bull call spread | bought strike below sold strike; premium paid |
  | Bear put spread | bought strike above sold strike; premium paid |
  | Bull put spread | bought strike below sold strike; premium received, and less than the width |
  | Bear call spread | bought strike above sold strike; premium received, and less than the width |
  | Long straddle | one strike |
  | Long strangle | put strike below call strike |
  | Iron condor | bought put, sold put, sold call, bought call, strikes rising; premium received |
  | Iron butterfly | the sold put and sold call share a strike, with the bought put below it and the bought call above; premium received |
  | Call or put butterfly | lower wing, body, upper wing, strikes rising; wings the same width |

- **The name is part of the plan** (ADR 0012's amendment). A plan with no name is Custom and gets the first check only.
- **A failure is a typed refusal** that names the rule and the legs. At an entry it skips the entry; in a preview it is the verdict. Its reasons are spelled as the money's are (`OPTION_STRUCTURE_…`), so one screen and one stored entry read both.
- **A share-class ticker is not handled yet.** A contract is on the plan's underlying when its root is the plan's ticker letter for letter, and `BRK.B` is written `BRKB` in a contract symbol. Such a plan's picks are refused until a root is matched to its ticker. The broker's contract reference now carries both spellings (decision 11); nothing matches them yet.
- **Not asked here:** whether the loss is bounded, which the requirement answers (decision 3), and whether Alpaca lists each contract as standard and tradable, which waits for its contract reference.
- **Where a named structure starts** is a separate list: the legs and rules the builder fills in. Most hang on one leg picked by delta with the others a dollar distance from it; the strangle and the iron condor pick two legs by delta, a put and a call out of the money. Every value is a starting point the owner changes, and a deployed plan carries its own. **The values themselves are provisional.** Only the bull call spread's (delta 0.50, $5 above, the nearest weekly expiry) come from the builder's design (#2915); the 0.30 delta for a leg out of the money and the other eleven starts await the owner's word.

### 8. Rules become contracts from the quotes held, or not at all

A plan's legs are rules. At an entry one pure function turns them into one contract for every leg, or one typed refusal; nothing is resolved in part. It reads no feed and no clock: the chain's definition, the quotes held, the underlying's price and the times are handed in.

- **There is no snapshot of a chain.** There is the newest bid and the newest ask received for each contract, each with the time its callback delivered it. Four moments are kept apart: the decision bar's close, the selection time, each quote side's receipt time, and the recheck just before an order is sent.
- **A quote is usable or fails as one thing.** The tests are asked in one order and the first to fail is the fault: an older subscription generation; one side missing; not live data; a side that is no price; an ask of zero; a bid of zero where something is to be sold; bid above ask; a side older than the decision-data budget (ADR 0067), or received after the moment it is used at. The first two say the quote has not arrived whole since the last reconnect.
- **The observations must belong together.** Every picked leg's quote and the underlying's must have been received within a coherence window of each other, 2,000 ms unless set otherwise. Each being fresh is not enough.
- **The money is the decision bar's close; the model's price is the underlying's live mid.** Strike rules that count from the money use the bar the decision was made on. Delta is solved from each candidate's mid against the underlying's mid, which arrived with the option quotes.
- **A chain may name its expiries before the contracts of each are known.** An expiry rule chooses among every date named. A leg that comes to an expiry whose contracts are not known yet is a search not yet complete.
- **A rule picks the nearest contract with a usable quote.** Each contract stands one of three ways for a leg:
  - **Its quote has not arrived:** none is held, or one side only, or only one from before the last reconnect. Nothing is known of it and it could have been the pick, so it stops the search.
  - **Its quote has arrived and fails a test,** or the rule picks by delta and the model cannot turn the quote into one. It is passed over for the next nearest.
  - **It is usable.**

  | Refusal | When |
  |---|---|
  | Search incomplete | A contract that could be the pick has a quote that has not arrived. The contract is named, and its fault when a quote is held |
  | No usable quote | Every contract the rule may pick was passed over. The first one and its fault are named |
  | Nothing close enough | The chain lists nothing within the tolerance |

- **A rule by delta walks toward its target.** It cannot know the delta of a contract with no quote. From the usable contract nearest the target it goes strike by strike toward the target, until it meets a usable contract beyond the target, or the last listed strike. It walks past the contracts passed over, and past a usable one still short of the target: far from the money a cent of quote can put two deltas out of order. A contract on the way whose quote has not arrived makes the search incomplete, even with a pick inside the tolerance. The way to walk takes delta to fall as a call's strike rises and to grow as a put's does; where the walk ends does not.
- **With no usable contract a rule by delta has nothing to walk from,** and the watched window is all it can judge by. While any watched contract's quote has not arrived the search is incomplete, whatever has arrived faulty, and the awaited contract nearest the money is named. Once every watched one has arrived and been passed over it is "no usable quote", naming the one nearest the money, though strikes nobody watches are listed beyond: what is wrong then is the quotes held, not the window.

- **A rule by strike uses what the chain's definition already says.** It knows every listed strike, so a target just outside the watched window is not "search incomplete" when the nearest listed strike is watched, and a target nothing is listed near is "nothing close enough" without any quote. This is stricter in meaning than "outside the window is always incomplete", and refuses less.
- **Ties** go as the plan's rules say: the money between two strikes is the lower; a delta or a percent as near two strikes takes the one nearer the money, then the lower; a distance from another leg takes the narrower spread.
- **Expiry rules count on the New York calendar from the decision's date.** The nearest weekly is the nearest listed expiry after that date on the last session of its week, so a week cut short by a holiday has its weekly on the Thursday. An expiry the chain does not list is refused as that, and so is one it lists on a day the calendar has no session for.
- **Nothing is opened too close to its last trade.** A contract is not offered once the selection is within the entry cutoff's lead of the session close on its expiry date, half days included. Time to expiry runs to that same close. A product that trades past the session close on its last day is counted as stopping with the session until each contract's own hours have a source.
- **Only standard contracts are on the ladder:** 100 shares, a multiplier of 100, American exercise. A rule counts strikes along the standard ones and never picks another.
- **The evidence is kept with the pick,** enough to reproduce it. For each leg: the expiries its expiry rule chose from and the strikes of its expiry, as the chain lists them, because a count along the ladder is made on those; and every watched contract of its right and expiry with its quote, receipt times, data type, generation, the provider's own delta, and why it was passed over; on a refused leg a usable contract says the leg was refused, not that another was nearer. The pick, and every contract a rule by delta asked the model about, also carries the model's inputs and its implied volatility and delta. Beside the legs: the watched window, the moments, and the model's rate and dividends with their sources.
- **The recheck picks nothing.** Just before an order is sent the picked legs' quotes are tested again at that moment. A fault, a missing quote, an unhealthy feed or observations now too far apart refuses the entry, and nothing is sent. So does a picked contract that has come inside its entry cutoff since the selection: the order is sent at the recheck's moment, not the selection's. So does a reconnect since the selection, even when every quote has arrived again in the new generation (ADR 0067); the feed is asked before any quote, so a reconnect is never reported as one of the quotes it left behind. The recheck is held to the limits the selection was made under and keeps the generation it saw. What the structure now costs and how wide its legs' markets are is not the recheck's question; those limits are asked where the entry's price is.
- **A refusal is a fact about the market.** An input that could not describe one, such as a bar close that is no price above zero or a selection before its bar's close, is the caller's mistake and raises an error of its own.
- **Not yet proven on a real chain.** The tests build their chain from the pricing model's own prices. A chain frozen from the provider during a session is still to be added as a golden fixture; taking one draws on the account's shared quote lines, so it waits for the owner's word.

### 9. Option quotes arrive through the Clerk's own supervisor, on a budget of lines

The Clerk's IBKR supervisor (ADR 0067) owns option quote lines as it owns the underlying's. It owns the connection, sees every reconnect, and drives the option side once a second. No route opens a line of its own. A fault in any step of the option side is logged and never stops the pass that keeps the stock quotes; when what is wanted cannot be read, what was wanted a moment ago stands. After such a fault the option feed reads unhealthy until a whole pass has gone through, so no contract is picked from lines left part way between what was wanted and what is.

- **A quote is what its callbacks delivered.** For each contract with a line: the newest bid and the newest ask, each with the time its own callback delivered it, and the kind of data IBKR says it is. Reading never renews a receipt. A side is first held when a tick of its price arrives on this line, and its price is that tick's own. The client library keeps one cache of prices for a contract across every request made for it, so a price found there may be from before a data loss or from a line given up long ago; nothing is read from it. Once a side is held, a tick of its size is a receipt for it, because the price stands, and a size of nothing says the side has emptied. A callback that carries neither side, such as IBKR's own model figures, renews neither.
- **A side IBKR delivers as -1 has arrived and has nothing there.** It is kept as zero: nothing bid, or nothing offered. A contract with no bid can still be bought at its ask and cannot be sold; one with no ask is passed over. The cost limit on a structure (decision 5) is what stops a contract whose bid went missing for a moment.
- **A switch between live, delayed and frozen data leaves nothing received.** A quote is of one kind. Delayed data is kept as delayed, so a rule refuses it as not live and not as a quote still to arrive.
- **The feed's generation is one number for the connection's epoch.** It moves on every reconnect and on every recovery from a data loss, IBKR's "data maintained" recovery included, and nothing delivered before it is kept: neither a receipt time, nor a price, nor IBKR's model delta. After it each side waits for a price tick of its own. The underlying's quote is handed over in the same generation. One contract's line being replaced empties that contract's quote and does not move the generation: otherwise one broken line would empty every quote.
- **The chain's definition is read from IBKR once a New York day.** Its expiries come in one answer and its contracts one expiry at a time, so only the expiries a plan's rules come to, and the expiries of contracts held, have their contracts read. A day's read replaces the last only when it lands, so yesterday's listing and the lines opened from it stay in service through the read. IBKR answers a listing request it cannot serve with nothing, not with an error; nothing is a failed read, tried again with a growing wait, and never a chain with no contracts. A contract is the one its own OCC symbol names, as IBKR gives it; one whose fields disagree with its symbol is left out. The multiplier is IBKR's. What a listing cannot say is taken as standard: American exercise, and one share per unit of the multiplier for a contract in the underlying's own trading class. Both are for the broker's contract reference to prove for each picked contract before any order; nothing asks it yet.
- **The watched window.** For each leg: the expiry its rule comes to today, and the listed strikes within a set number of the strike its rule aims at. A rule by delta aims at the money. The window is centred on the newest pricing bar's close, the price the resolver calls the money, so it moves at most once a minute; with no bar yet, on the underlying's live mid, taken at most once every fifteen seconds so that a mid hovering between two strikes does not open and give up the window's edges on every pass. When there is no price to centre it on, or only a quote no decision could use, it stays where it was: it is not given up and not moved. A window too small for a rule makes its search incomplete and never changes its pick.
- **A Clerk keeps to a line budget of its own.** IBKR grants the username a number of concurrent lines, shared by every Clerk and any open TWS, and no session can see the whole. The budget is the most option contracts the Clerk asks for at once, the ones its bots hold included. It is configuration, and it is zero until the owner sets it, because every option line comes out of the allowance the stock quotes of every bot draw on. A count of the Clerk's own lines never claims that IBKR has capacity.
- **One contract is one line, and lines go by priority.** Contracts a bot holds or has a working order in come first, are never given up for a lower class, and are asked for even past the budget, so that a refusal is IBKR's and is seen. Then the windows of running bots, nearest their centre first. Then previews and pages, which get only what is spare. A window that moves keeps the strikes it still wants; the lines it no longer wants are given up first, and the new ones asked for after, at most twenty in a pass. A wanted contract with no line is always reported with the reason, a held one included.
- **When IBKR refuses a line for capacity,** the contract has no quote for want of capacity, which is a different thing from the Clerk's own budget. The number of lines open at that moment becomes the ceiling until the next reconnect: the lowest classes give way, and a held contract is asked for again. Once a minute, while something waits, the ceiling is tried one line higher. A line refused for another reason, such as a missing subscription, stays refused until nothing wants it or the connection changes.
- **A quiet line is not a broken one.** A line that has gone the subscription timeout with no whole quote, both sides, is asked for again, which makes IBKR send its current prices. The wait counts from when the line was opened, or from when it last lost a side it held (a switch of the kind of data, a size reported on a side with no price); a line kept through a recovery counts as opened at the recovery. One that holds a whole quote and then had nothing new to say is left alone: replacing it would turn a stale quote, which a search walks past, into one not yet arrived, which stops it.
- **Not yet measured on a live connection.** How often a contract's quote is older than the decision-data budget in a calm market; what IBKR sends for a contract with no bid; whether a quiet contract's line still delivers model figures. These decide how often entries are refused, and the answers come from a session with the Gateway: `scripts/capture_option_chain.py` freezes a chain as the supervisor holds it and counts, second by second, how each watched quote stood.

### 10. One order, signed as Alpaca signs it

A structure goes to the broker as one order. The broker order contract gains the types that say so, beside the stock leg and not inside it: a stock leg, and every hash taken over one, is what it was.

- **Two shapes.** A structure of one leg is a plain option order on that contract. A structure of two to four legs is a multi-leg order: each leg's contract, side, ratio and intent, a number of whole structures, and a market order or one net limit price. A one-leg structure is never sent as a multi-leg order of one.
- **The net price keeps one sign from the quotes to the wire.** Positive is paid, negative is received. That is the sign `natural_net_price` returns and the sign Alpaca's order reference gives a multi-leg `limit_price`, so nothing between them flips it. A one-leg order's price is above zero and its side says which way the money goes.
- **A quantity is a whole number and a price is an exact decimal.** The stock contract holds both as floats because it began as a display surface. An option order's price is compared with what was set aside for it in exact cents, so no float sits between them.
- **Every leg says whether it opens or closes.** An exit's legs all close. The intent is there so a repeated exit cannot open the reverse position. That Alpaca refuses a closing intent beyond what is held is not in its documents and is to be shown on the account.
- **An option order is for the day and for regular hours.** A request has no field for a time in force or for extended hours, so neither can be asked for.
- **Ratios share no divisor above one, the legs are on one underlying, and no contract is in two legs.** The first is Alpaca's stated rule; an order that breaks it is refused before it is sent.
- **No price step is applied.** Alpaca's Trading API pages state none for an option or for a multi-leg net price. The contract carries the exact decimal it is given, and Alpaca's own refusal is the check until a recording settles it.
- **An accepted order is never reported as refused, and never as absent.** Once Alpaca has answered a submit with an order, an answer this repo cannot read is an unknown outcome, settled by an exact lookup. Nothing is asked of Alpaca a second time inside the submit, because that request's failure could be taken for the order's. The thirty seconds in which a not-found proves nothing start then too, exactly as they do after a submit that got no answer. They count again from any answer Alpaca gives for the order that is not taken as the order's, and end only when an answer is read whole and is about the order asked for. They cover the lookup by client order id and a read by the broker order id an unreadable answer named.
- **An unreadable answer still names the order to cancel, when it can.** The refusal carries the broker order id only when the answer's own ids are the ones the caller asked by. Another order's id is never handed on.
- **An order goes out under its caller's own client order id, and an answer must be about that order.** An order with no client order id is not sent: Alpaca would make one up, and no lookup could then find the order. Nor is one sent under an id Alpaca could not keep as given: longer than the 128 characters its reference allows, or padded with spaces. An answer that names another client order id, or another broker order id, is no answer; its fills are not counted and the outcome stays unknown. Broker order ids are compared as the ids they are, however they are spelled.
- **An answer is read whole or not at all.** The stock order reader reads a damaged row as far as it can, because one row must not refuse a whole list. The answer about one option order is different: a filled quantity that will not parse must not become zero contracts filled. An answer that says more filled than was ordered, names one contract in two legs, or states an intent on a multi-leg parent is refused the same way. A stock order found by an option lookup is refused, not read. A leg's quantity is not held to its ratio times the parent's: Alpaca's own example request and answer disagree on that, and a recording has to settle it.
- **Legs come nested under their parent.** Each leg is an order of its own, with its own id, filled quantity, average price and status. Alpaca documents the way to ask for nested legs on the read by broker order id only, so a multi-leg parent found by its client order id without its legs is read again by its own id. No undocumented parameter is sent.
- **A broker says whether it takes option orders.** Two capability flags, both no unless stated. Alpaca says yes to both. The simulated brokers say no and refuse an option order with a typed error that is never an unknown outcome. A shadow world serves its live broker's capabilities with both turned off, because its own order book takes no option order yet.

### 11. A contract is checked against the broker's own listing

A resolved contract is looked up in Alpaca's contract reference, in the Trading API. It is not a market-data read. One rule says whether a bot may trade it, and gives the first reason it may not, by the contract's symbol:

| Refusal | When |
|---|---|
| Not listed | Alpaca answers that it has no such contract. A contract IBKR lists and Alpaca does not ends here |
| Not the contract asked for | The answer's symbol, expiry, call or put, strike or root is not the contract's own |
| Not active, not tradable | By the listing's status and its tradable flag |
| Not American | By the listing's style |
| Not standard | Its multiplier or the shares it delivers is not 100; or, when the answer lists its deliverables, they are not exactly 100 shares of its underlying |

- An outage is not "not listed": only Alpaca's own not-found answer is.
- A listing that leaves out what the rule needs, such as its multiplier, cannot be read at all. It is never read as standard.
- The listing's words are taken exactly as Alpaca's reference writes them, in lower case (`active`, `american`, `equity`). A word it does not list is not taken for the one it resembles.
- **A share-class ticker is still not matched.** The listing's root symbol and underlying symbol are both carried as Alpaca writes them (`BRKB` and `BRK.B`). The rule holds the root to the contract symbol's own and matches nothing to a ticker. Decision 7's refusal of such a plan stands until that matching is built on these two fields.

### 12. The account's option fields are read beside the account, not into it

Alpaca's account answer carries an option buying power, the option level it approved, and the level in force. They are read into a model of their own, by a request of their own, from the same endpoint.

- The account snapshot a route serves is unchanged, so the served contract and the generated frontend types did not move.
- A field Alpaca leaves out stays unknown. It is never zero: a level of zero is Alpaca saying options are off. Alpaca's own example answer has no approved level.
- A level outside the 0 to 3 Alpaca lists is refused, not passed on as approval for everything.
- Nothing reads them yet. Deploy's refusal for a missing approval, and the comparison of buying power with what was set aside, come later.

## Where it lives

`PythonDataService/app/options/`: `contract.py` (decision 1), `structure.py` (a resolved structure: one underlying, each contract in one leg, as one order carries it), `requirement.py` (decisions 2 to 5), `exact.py` (the one exact decimal conversion they share), `american.py` (decision 6), `named_structures.py`, `structure_check.py` and `structure_catalogue.py` (decision 7), `quotes.py` and `resolver.py` (decision 8), `watch.py` (decision 9: the window, and who holds a line). The IBKR side of decision 9 is `PythonDataService/app/broker/ibkr/option_chain.py` (the listing) and `option_quotes.py` (the lines), driven by `market_liveness.py`. Golden fixture `OSM-001` pins the requirement, premium, commitment, loss estimate and round-trip cost against Alpaca's two documented examples and hand-derived cases, in exact cents. Fixtures `AM-001` to `AM-003` pin the American price, delta and implied volatility against QuantLib.

`PythonDataService/app/broker/contract/options.py`: the option order requests, the order as the broker reports it, the contract listing and the rule for trading it, and the account's option fields (decisions 10 to 12). `app/broker/contract/capabilities.py` holds the two flags and `ports.py` the five port methods. `app/broker/alpaca/adapter.py` writes the request body and reads Alpaca's answers; `client.py` and `broker.py` send them. The request and answer shapes are held to the examples Alpaca publishes, kept in `tests/fixtures/alpaca_documented/` and marked as documentation to be replaced by recordings.

Nothing on the money path calls these yet: no bot reaches any of it, and nothing in the Clerk calls the new port methods.

## Alternatives considered

- **Ask Alpaca what an order costs.** Its references describe no such call. The account does report an option buying power, which Deploy is to read as a second check, but that is an account-wide figure and not the cost of one order.
- **Use the loss at expiry as the amount to set aside.** Rejected: Alpaca holds the requirement, not the loss, and the loss is not estimated at all when expiries are mixed. Keeping both lets the bot compare its own figure with Alpaca's.
- **Keep Black-Scholes and call it close enough.** Rejected: it gives a deep put less than exercising it pays, and it cannot see why a call is exercised the day before a dividend. The PRD allows an approximation only with its measured cost; the tree is the model itself, and its measured cost is the step count's.
- **Take delta from IBKR.** Rejected by the owner's rule that a number picking a contract must be one this repo can check. IBKR's delta is to be recorded beside ours, and blocks nothing.
- **Check the payoff only at the strikes.** Rejected: it reports zero for one bought 450 put against two sold 440 puts, whose payoff is above zero at both strikes and falls to minus $430 a share at a price of zero.
- **Add an option field to the stock leg.** Rejected: the stock leg's dump feeds durable hashes, and a new field moves every one of them unless it is left out on both the writing and the checking side for ever. New types beside it cannot move a stock order's identity.
- **Put the three option fields on the account snapshot.** Rejected for now: a route serves that model, so it would change the served contract for fields nothing on a screen reads yet. The cost is a second account request when a caller wants both.
- **Ask the lookup by client order id for nested legs with an undocumented parameter.** Rejected: Alpaca documents the parameter on the read by broker order id. A second, documented read costs one request and guesses nothing.
- **Read an option order as leniently as a stock order.** Rejected: leniency there protects a list from one bad row. Here it would turn an unreadable fill into no fill.
- **Apply the stock sub-penny rule to an option's limit price.** Rejected: that rule is written for stock orders, and an option trades in steps the documents read do not state.
