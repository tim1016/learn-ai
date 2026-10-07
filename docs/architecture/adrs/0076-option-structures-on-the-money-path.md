# ADR 0076 — Option structures on the money path

**Status:** Accepted 2026-10-06. It gains a section as each part of #2913 it decides is built.
**Provenance:** Owner decisions in [#2913](https://github.com/tim1016/learn-ai/issues/2913), grilled on 2026-10-05 and revised on 2026-10-06 after a review against the code and Alpaca's documents.
**Related:** ADR 0012 (the Action Plan, and its 2026-10-06 amendment that defines an option structure), ADR 0022 (i) (the option expiry anchor), ADR 0035 (the Clerk's event-sourced store: schema v23 adds the two contract tables, both written only by folds of the custody log), ADR 0059 (the live envelope and the budget, which cover US equities only until this ADR's later sections extend them), ADR 0068 (Python owns the math).

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
- **An accepted order is never reported as refused, and never as absent.** Once Alpaca has answered a submit with an order, an answer this repo cannot read is an unknown outcome, settled by an exact lookup. Nothing is asked of Alpaca a second time inside the submit, because that request's failure could be taken for the order's. The thirty seconds in which a not-found proves nothing start then too, exactly as they do after a submit that got no answer. They count again from any answer Alpaca gives for the order that is not taken as the order's, and end only when an answer is read whole and is about the order asked for. They cover the lookup by client order id, a read by the broker order id an unreadable answer named, and a read by any broker order id after an answer to it that was not taken.
- **An unreadable answer still names the order to cancel, when it can.** The refusal carries the broker order id only when the answer's own ids are the ones the caller asked by. Another order's id is never handed on.
- **An order goes out under its caller's own client order id, and an answer must be about that order.** An order with no client order id is not sent: Alpaca would make one up, and no lookup could then find the order. Nor is one sent under an id Alpaca could not keep as given: longer than the 128 characters its reference allows, or padded with spaces. An answer that names another client order id, or another broker order id, is no answer; its fills are not counted and the outcome stays unknown. Broker order ids are compared as the ids they are, however they are spelled.
- **An answer is read whole or not at all.** The stock order reader reads a damaged row as far as it can, because one row must not refuse a whole list. The answer about one option order is different: a filled quantity that will not parse must not become zero contracts filled. An answer is refused the same way when it says more filled than was ordered, is for no contracts or no structures, names one contract in two legs or legs on two underlyings, has a multi-leg parent that states a contract, a side or an intent of its own, or prices one contract below zero (only a multi-leg net price is signed). From Alpaca, an order id or a leg's id that is not a UUID is refused too: the port could not read or cancel by it. A stock order found by an option lookup is refused, not read. A leg's quantity is not held to its ratio times the parent's: Alpaca's own example request and answer disagree on that, and a recording has to settle it.
- **Legs come nested under their parent.** Each leg is an order of its own, with its own id, filled quantity, average price and status. Alpaca documents the way to ask for nested legs on the read by broker order id only, so a multi-leg parent found by its client order id without its legs is read again by its own id. No undocumented parameter is sent.
- **A broker says whether it takes option orders.** Two capability flags, both no unless stated. Alpaca says yes to both. A Dry Run's simulation says yes to an order of one leg and no to several (decision 16). What a simulated broker does not take it refuses with a typed error that is never an unknown outcome. A shadow world serves its live broker's capabilities with both turned off, because its own order book takes no option order yet.

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
- A level outside the 0 to 3 Alpaca lists is refused, not passed on as approval for everything. So is a trading level above the approved level: Alpaca's reference defines the first as the lower of the second and the owner's own maximum.
- Nothing reads them yet. Deploy's refusal for a missing approval, and the comparison of buying power with what was set aside, come later.

### 13. Custody counts a contract by its own multiplier

The Clerk's books counted every quantity as shares: a fill of 2 at 1.50 moved $3.00. An option fill of 2 contracts at 1.50 moves $300.00.

- **The multiplier is a recorded fact of the contract.** It is written once, when the contract first enters the books, in `option_contracts` (symbol, multiplier), and never edited. It is not read off the symbol: the symbol says which contract, never what it multiplies by. One symbol has one multiplier for good; recording another for the same symbol is refused, because an adjusted contract trades under another symbol. FIFO refuses a fill that counts differently from its symbol's open lots.
- **An order trades one instrument.** `order_contracts` says which contract an order trades. The row is written by the same fold that creates the order, so that an option order never exists without its contract, and the contract's terms must already be recorded. Decision 15 names the two folds that write one. A fill names its order, so a fill's contract and multiplier are read through the order.
- **Absent, not defaulted.** A stock order has no row in either table. Its fills are shares and are counted by the same expressions as before. Nothing stored before this decision has a row. The readers that turn a fill into money or a position join the two tables: the three economic projections, the entry's cash claim and the execution history. Readers that only total an order's quantity do not, and need not. A store no Clerk has opened since is read on a private copy brought to the current schema, as any older store is (#2615), so no reader meets a file without them.
- **Positions are held per contract.** A position is keyed by the fill's own symbol, so two contracts on one underlying are two positions and a contract is never netted against the underlying's shares. The one write that moves a position refuses a fill whose symbol is not its order's contract, and refuses a fill spelled as an option contract on an order bound to none. Shape is used only to refuse, never to price.
- **Where the multiplier is applied.** FIFO realized and open P&L, a lot's cost basis, the budget's position cost, the proceeds of a sale cash has not seen, simulated cash, and what an entry order claims while cash has not caught up with it: each is contracts × premium per share × multiplier, in the exact decimal arithmetic those modules already use. The account's day P&L is the broker's equity less the prior close's and transfers, and multiplies nothing.
- **An entry order's claim is counted as its position will be.** A fill cash has not seen claims its actual cost, by the multiplier, so the cash free to deploy is the same before and after cash sees the fill. The unfilled part of an order bound to a contract claims contracts × the reservation's reference price × multiplier: the reference price is read as a premium per share, as a fill's price is. A Dry Run's entry in one bought option records one (decision 15), with its limit price. A sold leg's entry commitment (decision 2) also holds the broker's requirement, which that row has no place for: whatever first records one must change this reading.
- **Option fees are predicted from dated, published rates.** Alpaca charges no commission on stock and ETF options and passes five fees through: the SEC fee and FINRA's TAF on sales, and the ORF, the OCC clearing fee and the CAT fee on both sides. The SEC fee is on the premium sold (contracts × premium × multiplier); the CAT fee counts a contract as its multiplier in shares; the rest are per contract. Alpaca sums each fee type over an account's day and rounds each total up to the cent. An entry's fee provision is its own fills settled as a day of their own.
  - SEC and TAF are dated by the regulators. Alpaca dates its schedule by revision and states no effective dates, so its ORF, OCC and CAT figures are dated by the revision that first printed them. A rate with no date to stand on is unknown, never zero.
  - The option model uses the stock model's date lookup, cent rounding and day settlement, and its unpinned-rate error is a kind of the stock model's. Whatever takes an unpinned stock rate as an unknown takes an option's the same way.
  - Every figure is an estimate until Alpaca posts the day's fees. Observed fee activities stay the truth (ADR 0059 Decision 6).
- **The fee attribution does not price option fills yet.** It prices fills with the listed-stock model and settles them by day. A real account holding an option fill has its fees flagged unresolved, so that no money reader uses them, until option fees are attributed from Alpaca's own fee activities, which have not been seen. The rows under that flag are still worked out by the stock model; the slice that first puts an option fill on a real account's books must leave option fills out of them. A simulated account's option fills are charged by the option fee model (decision 16).
- **Not done by this decision:**
  - who writes the two tables (the option order path; decision 15, for a Dry Run's one bought option);
  - the cash an option entry sets aside in full (the entry commitment, decision 2);
  - what the trade stream does with a fill the position write refuses. Today the refusal reaches the stream, which reconnects and is handed the same fill again. This is to be settled where an order is first bound;
  - a fill in an adjusted contract's symbol on an order bound to none: decision 1 does not read it as a contract, so the shape test lets it through as shares;
  - a sold leg in the budget, which is long-only today and refuses a short lot;
  - marks for an open option position in a simulation (decision 16, for a bought one);
  - an option order placed outside the Clerk, whose multiplier nothing records.

### 15. A Dry Run's Clerk takes one bought option, and works its entry out from facts

**Who takes an option plan today.** Only a Dry Run's account, and only a plan of one bought call or put. A plan with a sold leg, or with more than one leg, is refused by name. A Paper, Live Shadow or Live account refuses to register an option bot and refuses an option effect, and writes nothing. Beneath both, the Clerk's own send takes an option order to a simulation only, and a Live Shadow's port refuses one by type. Nothing about option orders has been checked against Alpaca.

- A Dry Run option bot is registered under its underlying. Its budget is committed with no minimum position cost: what one option position costs is known only from its contract's quotes, at each entry.

**The caller hands over facts, never figures.** An option entry cannot be rebuilt from the Action Plan inside the Clerk: its contract was picked from quotes. So the caller hands over, for each leg, the contract picked, its listed multiplier and the quote read just before, with the generation those quotes were read in and the bot's two limits. It hands over no price, no commitment and no fee. The money a bot sets aside must be the Clerk's own arithmetic: a caller that could hand over a commitment could hand over a wrong one.

From those facts the Clerk asks, in order. The first "no" refuses the entry with its own reason code, and nothing is sent:

1. the plan is one it takes;
2. the facts cover exactly the plan's leg, and each is what it must be: two limits that are percents of zero or more, a generation and a read time that are whole numbers;
3. the contract is a standard one, with a multiplier of 100. The multiplier is the caller's word until the broker's listing is read (decision 11), so no other is taken on it;
4. the contract's last session has not ended, each quote is usable at the Clerk's own clock (decision 8's tests, with the 5,000 ms decision-data budget of ADR 0067), and each side is priced in whole cents, as an order's price must be;
5. the entry's fee provision, from the option fee model: its own fills settled as a day of their own;
6. the structure check, each leg's bid-ask gap against the owner's limit, the natural limit price, the money, and the round-trip cost against the owner's limit.

- **Only a fault in what was handed over is told as incomplete evidence.** Anything else that fails while the money is worked out is a fault of the code and is raised as one.
- **The leg gap limit.** An entry is refused when any leg's ask less its bid is more than the owner's percent of that leg's mid, compared exactly. It is asked after the structure check and before the money, so the first "no" is the leg's own market. The starting value is 8. A leg can pass it and the structure still cost most of itself to trade, which is why the structure cost limit (decision 5) is asked as well.
- **A contract has expired when its last session has ended.** That is the calendar's session close on its expiry date, a half day's included, and the close of the session before when the date has none. It is not the expiry anchor's 16:00, and not the end of the date. From then on nothing is bought in it and nothing is sold (decision 16).
- **Until the option quote lines are wired to the Clerk,** the Clerk tests the quotes it is handed; it does not read them again itself. The two limits come with each call until Deploy sets them.

**An option entry is the stock ENTER's own fact with one more key.** It is recorded under `ENTER_ACCEPTED` and its close under `EXIT_REDUCING_ORDER_CREATED`. No transition kind is added.

- The ENTER's identity hash is taken over `{account, bot, decision, "ENTER", option_order: <the order>}`. The key is `option_order`, never `leg`, so an entry in shares and one in a contract cannot hash alike. The idempotency key is the existing one: one entry per bot and decision.
- The facts gain `option`, left out when absent. It holds the order as asked, the contract each leg resolved to with its multiplier, the quote each leg was priced from with both receipt times, the units, the limit price, the money in whole cents with the fee provision and the round-trip cost, both limits, and the Clerk's own admission time.
- The facts' `leg` is then the order's line in the same keys a stock leg has: the contract's symbol, the side, contracts, and the limit a share. It is not a stock leg and cannot be made into one.
- The EXIT's acceptance gains `option_contract`, the OCC symbol it closes, and `option_exit`, the quotes its decision read. Both are left out when absent.
- A stock ENTER's hash and facts are pinned by a test to the values `origin/master` computed before any of this.

**A decision sent again** is answered from the entry already on the books, before anything is judged again: its quotes have aged since, and a refusal would deny a position the bot may hold. Its price is expected to differ. A decision sent again for another contract, or another number of structures, is a second request under the first one's name: the same durable conflict it is for an entry in shares.

**An option entry passes the fences an entry in shares passes, and they are one function.** The bot is registered, the run is the bot's active one, nothing holds new exposure for it, a budgeted account has a commitment for it, and a budgeted deployment has its envelope. Then the account is on budgets, its reading is current, and the budget rule is asked: one function, told only what the entry requires. For shares that is the notional plus the stock fee provision. For an option it is the entry commitment in exact cents.

**The contract tables are written where the order row is** (decision 13 left this to come). The fold that creates an option order's row records the contract's terms and binds the order to it: the ENTER's fold from its facts, and the reducing order's fold whenever the symbol it reduces is a contract the books have recorded. The second covers every maker of a reducing order (a deciding program's EXIT, an ended run's close, the flatten, the watchdog's re-drive) without any of them having to remember it. That fold reads only the symbol of its fact. An entry in a contract the books hold with another multiplier is refused with a receipt, and nothing is written.

**The reservation counts by the contract's multiplier.** The reservation row keeps contracts and the premium a share; its claim, and the cost of a fill the cash reading has not seen, are counted with the contract's multiplier read through the order (decision 13). For a bought option, whose requirement is zero, that claim plus the fee provision is the commitment. A sold leg's requirement has no place in the reservation yet.

### 16. How a Dry Run fills, marks and closes an option position

- **The simulation takes an option order of one leg** and says so in its capabilities. An order of several legs it refuses. A shadow world still takes none.
- **The Clerk binds a quote to the order's id before it sends.** An opening order is filled from the quote its limit was worked out from. With no quote bound it is refused definitively: **the simulation invents no fill**.
- **A fill is at once, in full, at the natural price.** The order's record keeps the order, the quote with both receipt times, the model and the quote's age.
- **A closing order never opens a position.** The simulation refuses a closing order for more than it holds, and one for a contract it does not hold.

**The quotes kept.** Every quote a Dry Run's Clerk is handed for a contract is kept beside its simulated orders: an entry's, an exit decision's, and the newest one the runner holds for a contract the bot holds. Two questions are asked of them.

- *Which is the newest?* By receipt time. A close with no usable quote of its own, and the close of an ended run, are priced from it and say so with its age. A quote stamped later than the Clerk's own clock prices nothing.
- *What was last stated to be the newest held?* By hand-over time. The simulated account marks an open contract at that quote's bid, while the hand-over is no older than a share bar may be. A quiet contract's bid can stand for minutes, so it is the hand-over that must be fresh.
- **What is kept is bounded.** A hand-over every few seconds would grow the file without end. It is cut back as it grows to what a later reader can still be answered from: for each contract the newest quote by receipt, the last one handed over, and the last one handed over before each session's close, which is the mark a contract held overnight starts the next day from. What the file holds is also held in memory and read again only when the file has changed, so a mark is never a scan of it. A fill keeps the quote it used in its own record; the kept row it cites may since have been cut.
- A file whose whole line is not a kept quote makes the quotes unavailable by name. Nothing is priced or marked from it.

**An option position is closed by its entry's own contract,** read from the entry's facts, in one market order with a closing intent, sized from the position confirmed when the order is made. The plan's rules are never asked again.

- One EXIT closes one contract. An EXIT of a bot that holds several is refused by name: closing one and answering "flat" would leave the others open.
- An exit's quotes are named by their contract's symbol, as the quotes kept are. A quote for another contract is refused.

**Who waits for the regular session, and who does not.**

- An order that closes an option position is sent only in the regular session. Every EXIT of an option position is recorded as regular-session-only, whoever made it, and takes no priced leg: a limit price for one is refused by name. Outside the session a deciding program's EXIT is held and sent at the next open, and the Clerk's own retry of a failed close waits for it. The hold has its own reason, `OPTION_CLOSE_WAITS_FOR_OPEN`, so that it is never told, or listed on the panel, as a bot that reached its scheduled end.
- **A Dry Run's close of what an ended run left sells at any hour,** as it does for shares (owner decision 2026-09-29): at the newest quote kept, saying that quote's age. It is the simulation tidying its own books, not an order a venue would take after hours. The hold's words say only what is true of the order they are about.

**The operator's Flatten of an option position** is priced as every other close of one is: from the newest quote kept for the contract. It sells exactly what is held, once.

- Whether a contract can be closed now is answered from its kept quotes: the newest one received by now has something on the side the close fills at, a bid above zero for a sale. The stock quote source is never asked about a contract, so a contract never joins the shares the IBKR supervisor watches.
- The Flatten is not offered while it cannot work: the contract has expired, the regular session is closed, or nothing kept prices it. Pressed anyway from an older plan, it is refused by name before any EXIT exists.
- A newer quote with a bid makes the next Flatten sell. Who hands a stopped bot's Clerk a newer quote is the runner's part: the Clerk is to read a held contract's quotes from its own IBKR supervisor (decision 9).

**What the books cannot hold is refused, not approximated.** A fill needs a price above zero. So a close with nothing kept to price it from is not sent, and a close of a contract nobody bids for is not recorded as a sale for nothing: each is refused with its reason and the position stays open. The Clerk retries three times in regular hours and then stops; the Flatten remains. The PRD's "unpriced, left out of P&L with a flag" is not built.

**A contract past its last session is neither bought nor sold.** What it is then worth is its exercise value, which no kept quote states. So nothing is priced from one.

- A close of such a contract is refused under `OPTION_CONTRACT_EXPIRED` before anything is priced: a deciding program's EXIT, an ended run's close, the Clerk's retry, the Flatten, and an EXIT held since before the expiry. It is judged where the order would arrive, as the session is, so a close too late to arrive is refused and never held for an open that will not come.
- Two fences. The Clerk's is asked first. The simulation's own refuses to bind a quote to such a close and refuses an order in such a contract.
- The position stays on the books. The notice names the contract, says it expired, and says a Dry Run does not yet settle an expired contract at its exercise value. The Clerk then holds: it makes no EXIT and spends no retry on a contract that can never trade again, and it never gives up on it either.
- **The owner's write-off does not clear it on a Dry Run.** The write-off clears only a position the broker does not hold, and the simulation is this account's broker and holds the contract too. Neither the notice nor the refused write-off sends the owner to a flatten. Settling an expired contract comes with the expiry and assignment work.

**The deadlines an option position is held to.** Each is counted back from a session close the calendar gives, so a half day's are a half day's.

- The **close deadline** of a contract is the session close on its expiry date less the expiry close lead, which starts at 45 minutes.
- The **entry cutoff** is the close deadline less the entry wait (how long a resting entry waits for its fill, starting at 20 seconds), the allowance for proving a cancel (30 seconds) and a margin (30 seconds). It is the lead decision 8's resolver is given.
- A structure with a sold leg owes a close every session it is held, the sold-leg close lead before that session's close; the lead starts at 10 minutes. When that close and the expiry-day one fall on the same day, the earlier binds.
- These are worked out and tested. Nothing schedules a close by them yet: that is the runner's part.

**A simulated account's option fees.** A simulated account observes no fee. Its option fills are charged by the option fee model: each of the five fee types summed over the day's option fills, rounded up to the cent, and shared among the bots by the one largest-remainder rule. A simulated day that also traded stock rounds the two populations apart, which is never less than rounding them together; how Alpaca rounds such a day has not been seen. A real account's option fills still leave its fees unresolved (decision 13).

## Where it lives

`PythonDataService/app/options/`: `contract.py` (decision 1), `structure.py` (a resolved structure: one underlying, each contract in one leg, as one order carries it), `requirement.py` (decisions 2 to 5), `exact.py` (the one exact decimal conversion they share), `american.py` (decision 6), `named_structures.py`, `structure_check.py` and `structure_catalogue.py` (decision 7), `quotes.py` and `resolver.py` (decision 8), `watch.py` (decision 9: the window, and who holds a line). The IBKR side of decision 9 is `PythonDataService/app/broker/ibkr/option_chain.py` (the listing) and `option_quotes.py` (the lines), driven by `market_liveness.py`. Golden fixture `OSM-001` pins the requirement, premium, commitment, loss estimate and round-trip cost against Alpaca's two documented examples and hand-derived cases, in exact cents. Fixtures `AM-001` to `AM-003` pin the American price, delta and implied volatility against QuantLib.

`PythonDataService/app/broker/contract/options.py`: the option order requests, the order as the broker reports it, the contract listing and the rule for trading it, and the account's option fields (decisions 10 to 12). `app/broker/contract/capabilities.py` holds the two flags and `ports.py` the five port methods. `app/broker/alpaca/adapter.py` writes the request body and reads Alpaca's answers; `client.py` and `broker.py` send them. The request and answer shapes are held to the examples Alpaca publishes, kept in `tests/fixtures/alpaca_documented/` and marked as documentation to be replaced by recordings.

`PythonDataService/app/broker/alpaca/clerk/sqlite/contract_terms.py` (decision 13: the two tables, their two fold helpers, the reads, and the refusal at the position write). The multiplier reaches `clerk/fifo_pnl.py`, `clerk/money.py::notional`, `sqlite/budget_projection.py`, `sqlite/envelope_reservations.py::entry_cash_claims` and `sqlite/simulated_account.py` on `FillRecord.multiplier`, read by `sqlite/economic_projection.py::effective_fill_rows`, or straight from the same join. `PythonDataService/app/broker/alpaca/option_fees.py` holds the option fee prediction and an entry's fee provision; golden fixture `FEE-002` pins it, exact, against Alpaca's published schedule and the SEC and FINRA filings, with a case that straddles FINRA's pause of the TAF on 2026-10-01.

`PythonDataService/app/broker/alpaca/clerk/` (decisions 15 and 16). `option_effects.py` holds what a caller hands over with an option decision and the refusal codes. `sqlite/option_admission.py` works an entry out from those facts; `sqlite/option_entry.py` is the one reader and writer of the option part of an ENTER's and an EXIT's facts. `sqlite/enter.py` and `sqlite/envelope_admission.py` hold the fences and the budget rule both kinds of entry share. `sqlite/exit_resolution.py` makes an option close, holds it outside the session, and refuses one of an expired contract; `sqlite/exit_watchdog.py` holds its retries; `sqlite/recovery_policy.py` says when a Flatten of one is offered. `synthetic_broker.py` fills one-leg option orders, and `kept_option_quotes.py` keeps the quotes they and the marks are priced from. `app/options/entry.py` prepares an entry (the leg gap limit is its own), `app/options/deadlines.py` holds the close deadline, the entry cutoff and a contract's last session, and `app/options/simulated_fill.py` prices a simulated fill. `app/services/alpaca_fee_attribution.py` charges a simulated account's option fees.

Custody reads the contract tables on every read of fills, and tells a contract's symbol by decision 1's spelling. One order path writes the tables: a Dry Run's entry in one bought option, and the orders that close it (decisions 15 and 16). No bot reaches it yet: the runner hands the Clerk no option entry, so the path is called by tests alone. Nothing sends an option order to Alpaca, nothing in the Clerk calls the contract reference or the account's option fields, and a real account's option fees are not attributed.

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
- **A new transition kind for an option entry.** Rejected: many readers key on `ENTER_ACCEPTED` and `EXIT_REDUCING_ORDER_CREATED` and on the order's symbol and quantity in them: the order projection, the exact-lookup worklists, the flat proof, the run-end close, the failed-entry fence. A new kind is silently skipped by every reader that is missed. The same kind with the option's own values is read correctly by all of them, and the places that would build a stock leg from it fail loudly.
- **Let the caller hand over the commitment, or the limit price.** Rejected: the Clerk would then set aside a figure it had not worked out.
- **Take any multiplier the caller names.** Rejected: a hundred contracts counted at a multiplier of one were admitted for one contract's premium. Only the standard 100 is taken until the broker's listing is read for the contract.
- **Keep every quote ever handed over.** Rejected: fifty contracts for one day is 390,000 rows and 80 MB, and every mark read parsed all of it. A fill keeps the quote it used in its own record, so nothing is lost by cutting the working file back.
- **Record a close with nothing bid as a sale for nothing, or as unpriced with a flag.** Not built: a fill row needs a price above zero, and FIFO, cash and fees all read it. Refusing the close leaves the books true and the position open.
- **Sell an expired contract at the last bid kept before it expired.** Rejected: that books a sale that could not have happened, at a price from another day. The true outcome is the exercise value or nothing, and that belongs to settling the expiry.
- **Hold a Dry Run's close of an ended run for the next open, as every other option close is held.** Not chosen: the owner's rule for a Dry Run is that its simulation closes what an ended run left on every kind of ending, overnight included, at the last price it knew. Holding it would leave a stopped Dry Run holding a contract nobody marks until the open. The owner may still prefer it; it is one rule in `exit_resolution._send_verdict`.
- **Ask the IBKR stock quote for a contract's symbol to price a Flatten.** Rejected: it never answers, and each ask put the contract on the watched list of shares for a minute.
