# Two bots trading one symbol in one Alpaca account (#2469)

Research for [#2469](https://github.com/tim1016/learn-ai/issues/2469) (parent #2439), 2026-09-29.
Code examined at `79c79f22` (origin/master). Tests:
[`test_two_bots_one_symbol.py`](../../PythonDataService/tests/broker/alpaca/clerk/sqlite/test_two_bots_one_symbol.py).

Evidence labels: **[test]** a test in that file (command at the end), **[code]** a
file:line at `79c79f22` (paths under `PythonDataService/app/` unless stated),
**[doc]** a primary public source listed at the end, **[fixture]** data already
committed in the repo.

## Answer

**Allow the setup, with guards; don't refuse it.**

- **Attribution is exact per bot.** Each bot owns its own orders, fills, position,
  FIFO results and budget. Its EXIT sells only its own shares.
- **The broker sees one net position.** Reconciliation can check the total, but
  it cannot say which bot a missing share belonged to.
- **Alpaca refuses opposite-side orders.** While either order is open, Alpaca
  rejects a buy and a sell on the same symbol in one account as a potential wash
  trade (HTTP 403, paper included). Two long-only bots hit this when one enters
  while the other exits.
- **The Clerk handles that refusal badly today:**
  - it reads the refusal as bad credentials;
  - a refused ENTER keeps its cash claim forever;
  - a refused EXIT waits on the other bot's order, then escalates to `EXIT_STUCK`,
    and automatic retries stop.
- **Day trading and settlement no longer constrain this.** Alpaca retired the
  pattern-day-trader rule and removed its fields on 2026-07-06. All Alpaca
  accounts are margin accounts.
- **A2 (#2441) is fixed on master.** A second, stricter gate now also refuses
  every ENTER after any bot's fill until cash is re-read.

## 1. How shares and fills are attributed

| Fact | Evidence |
|---|---|
| Every order carries `learn-ai/{bot}/v1:{intent}` as Alpaca's `client_order_id`, minted at ENTER acceptance and on the EXIT's reducing order. | [code] `engine/live/order_identity.py:105,135`; `broker/alpaca/clerk/sqlite/enter.py:253-254`; `exit_resolution.py:1440` |
| A fill is credited to the custody subject of the order's effect; positions are keyed `(subject, symbol)`. | [code] `clerk/sqlite/folds.py:937-965`; `clerk/sqlite/schema.py:336` |
| Two bots on SPY hold 3 and 5 separately; the account total is 8. | [test] `test_same_symbol_positions_stay_per_bot_while_reconciliation_sees_one_netted_position` |
| Reconciliation compares the broker's **net** quantity per symbol with the **sum** over subjects. A mismatch is `position_drift` on the symbol and names no bot. | [code] `clerk/sqlite/reconcile.py:154-172,220-232,275-289`; [test] same test (broker 5 vs attributed 8) |
| A bot's EXIT quantity is its own attributed position, never the broker's. It may only target its own entry order. | [code] `exit_resolution.py:312`; `idempotency.py:104`; [test] `test_each_bots_exit_sells_only_its_own_attributed_shares` (sells 3 of the broker's 8) |
| An EXIT cancels that bot's own working entries first and re-reads the proven remainder, so partial fills are per bot. A bot cannot cancel another bot's order. | existing tests `test_exit.py::test_resolve_exit_cancels_the_working_entry_before_computing_quantity`, `::test_partial_fill_during_cancel_uses_only_the_clerk_proven_remaining_quantity`, `::test_accept_exit_rejects_an_entry_order_belonging_to_a_different_bot` |
| Budgets and bot results run FIFO over that bot's fills only. | [code] `clerk/sqlite/budget_projection.py:153,289,403`; existing test `test_budget_claims.py::test_interleaved_same_symbol_deployments_keep_own_fifo_after_correction_and_stop` |
| The account-wide P&L attribution runs **one** FIFO over every bot's fills. One bot's sale can close the other bot's older lot. | [code] `clerk/sqlite/economic_projection.py:555-620`, `clerk/fifo_pnl.py:238` |

**The two FIFO views disagree on realized P&L; the totals agree.**

- A buys 1 at 100, B buys 1 at 200, B sells 1 at 210.
- Per bot: B realizes +10, and A still holds its 100 lot.
- Across the account: B's sale closes A's lot for +110.
- Realized plus open P&L is 115 in both views (exact Decimal; no tolerance).
- [test] `test_account_fifo_lets_one_bots_sale_close_the_other_bots_lot`.

So the account's realized figure is not the sum of the bots' realized figures. It
should not be presented as if it were.

### What if one bot's EXIT would sell shares the broker holds for the other?

Alpaca has no notion of "the other bot's shares". It holds one net position with
a blended average price. Nothing in the bot views reads that price. [code] A grep
for `average_entry_price` / `unrealized_pl` finds no bot-level consumer.

In the clean case (broker = sum of bots, all long), one bot's EXIT can never
oversell: it sells at most its own share of the total.

Oversell becomes possible only when the broker holds less than the bots believe,
for example when shares were sold outside the Clerk.

- **The guard.** `POSITION_DRIFT` then allows a reduction only if it moves both
  the broker quantity and the attributed sum toward zero without crossing.
  [code] `clerk/sqlite/uncertainty.py:768-804`.
- **Whoever exits first gets out.** Broker 5, A 3, B 5: either bot may sell
  first. After A sells 3 the broker holds 2, and B's 5-share EXIT is refused
  (only 2 may go). The shortfall lands on whichever bot exits second, not on
  whoever's shares were actually sold.
  [test] `test_shares_sold_outside_the_clerk_strand_whichever_bot_exits_second`.
- **Why the guard matters.** The captured paper account has `shorting_enabled:
  true` and multiplier 4 [fixture] `tests/fixtures/alpaca/account/account.json`.
  An oversell there would open a short rather than be refused.

**Outside orders** are account-wide, not symbol-scoped.

- A foreign order raises `UNEXPLAINED_ORDER_HOLD`. Its policy blocks new exposure
  and reductions for every bot. [code] `clerk/sqlite/uncertainty_policies.py:369-372`
- Its fill also raises the drift above.
- Existing tests: `test_reconcile.py::test_reconcile_account_raises_an_account_clerk_hold_for_a_foreign_order`.

### Budgets and the account cash bound

- **Each bot's budget is its own:** free = committed + realized − fees − own
  position cost − own pending orders. [code] `clerk/budgets.py:64-78`
- **The account check spans every bot.** An ENTER must also fit account cash
  after every other bot's positive free amount, all order claims and fee claims.
  [code] `clerk/budgets.py:140-145`
- **Any bot's fill pauses every bot's entries.** A fill recorded after the last
  account reading refuses every bot's ENTER with `LIVE_ENVELOPE_UNOBSERVED`
  ("Executions changed after the last account reading") until the next reading.
  [code] `clerk/sqlite/risk_admission.py:92-94`, added by `b920c799` / `78ca2b40`
  (#2543, #2566).
- **The refused ENTER is dropped.** The runtime returns a rejected receipt and the
  bot discards that decision. [code] `clerk/sqlite/runtime.py:1164-1168`
- **Two bots deciding on the same bar:** once the first bot's fill is recorded,
  the second bot's entry is refused rather than delayed.
  [test] `test_one_bots_fill_holds_the_other_bots_entry_until_the_next_account_reading`

## 2. Wash trades at Alpaca

### Alpaca's rule

Alpaca checks every new order against the account's open orders on the same
symbol [doc: User Protection].

- **When it refuses.** If the two orders could interact with each other, Alpaca
  rejects the new one with HTTP 403.
- **Paper too.** The protection applies to paper accounts.
- **Bracket and OCO orders** are the documented way to hold a take-profit and a
  stop-loss together.

The rejection table on that page gives the conditions:

| Open order | New order | Rejected |
|---|---|---|
| market, stop | any opposite-side order | always |
| any | opposite-side market or stop | always |
| limit / stop-limit | opposite-side limit / stop-limit | when buy limit ≥ sell limit |

- **The vendor code is second-hand.** Community reports give the body as code
  `40310000`, message "potential wash trade detected. use complex orders". The
  official page states only the 403. The Clerk's handling depends only on the
  status, so the tests build that body.
- **The 403 is order-level.** Alpaca's order reference documents a 403 on
  `POST /v2/orders` as buying power or shares not sufficient [doc: Create an
  Order].
- **FINRA's view.** The FINRA rule Alpaca links to (Rule 5210, Supplementary
  Material .02) treats self-trades from separate, unrelated strategies at one
  firm as generally bona fide. So Alpaca's block is a broker protection, not a
  statement that two bots trading against each other would be unlawful [doc:
  FINRA 5210].

### When two bots meet it

Under bot budgets every bot is long-only: an open short lot refuses the account's
budgets [code] `budget_projection.py:405`. So a conflict is always one bot
entering (buy) while another exits (sell). Same-side orders never conflict.

- **Regular session.** Legs are market orders, open until they fill. The one
  recorded paper SPY market order took 0.772 s from submit to fill [fixture]
  `tests/fixtures/alpaca/orders/orders.json` (per `docs/references/alpaca-order-fill-latency.md`).
  Two bots deciding opposite sides on the same bar submit inside that window, so
  the second order is rejected.
- **Outside the regular session.**
  - Every program leg is a marketable limit on the decision bar's close: a buy
    rounds up, a sell rounds down [code] `broker/alpaca/marketable_limit.py:51-79`.
  - So two opposite legs from one bar always satisfy "buy limit ≥ sell limit".
    [test] `test_opposite_extended_hours_legs_on_one_decision_bar_are_always_a_wash_trade_pair`
    (3 anchors × 3 allowance pairs).
  - These legs are DAY limits that can rest until they fill or the session ends.
    For that whole time the other bot's opposite order is refused.
- **One bot never trades against itself this way.** Its EXIT cancels its own
  working entry first (section 1).

### What the Clerk sees and does today

**The error.** `map_api_error` turns every 401/403 into `BrokerAuthError`, worded
"Alpaca rejected our credentials: potential wash trade detected. use complex
orders" with detail "HTTP 403". Alpaca's code is dropped.

- [code] `broker/alpaca/errors.py:77-82`
- [test] `test_a_wash_trade_refusal_reads_today_as_a_credentials_failure`
- [xfail] `test_a_wash_trade_refusal_is_an_order_rejection_that_keeps_alpacas_code`

**A refused ENTER** folds `ORDER_SUBMIT_FAILED` (effect and command `failed`).
The facts keep Alpaca's message but not its code, and the bot may enter again.
The decision itself is discarded.

- [code] `clerk/sqlite/enter.py:411-424`
- [test] `test_an_enter_refused_as_a_wash_trade_fails_and_the_bot_may_enter_again`

**Its cash claim is never released. This is a defect, and not only for two
bots.**

- **Where it comes from.** The claim query prices a working order's unfilled
  remainder unless the broker state is canceled, expired, rejected or replaced.
  A refused submit leaves the order's broker state empty, and the query never
  looks at the effect's `failed` state [code] `clerk/sqlite/envelope_reservations.py:50,112-148`.
- **The size.** After a refused 2-share buy at $100 and a Stop, the bot still
  claims $200.01 (2 × $100 plus a $0.01 fee provision).
- **Account money.** The account's `available` is short by that amount.
- **Home.** The bot stays in `bots_holding_money`, so it is never Finished.
  [code] `clerk/sqlite/budget_projection.py:231-257`
- **The pre-broker refusal leaks too.** A post-acceptance refusal
  (`ENTER_SUBMISSION_REFUSED`, the market-liveness re-check) uses the same fold
  and leaks the same way.
- **Tests.**
  - [test] `test_a_refused_enter_keeps_its_cash_claim_today`
  - [xfail] `test_an_enter_that_never_reached_the_book_releases_its_cash_claim[broker_wash_trade|preflight]`

**A refused EXIT** folds `EXIT_NOT_FLAT` and the bot keeps its shares
[code] `exit_resolution.py:1629-1639,1680-1725`.

- **The watchdog waits on the other bot.** It will not re-drive while the Clerk
  has other work on the symbol or the broker shows a working order there. It
  records each wait as a **failure** [code] `clerk/sqlite/exit_watchdog.py:113-118,403-432,459-473`.
- **Then it escalates.** After 480 s of regular-session failure time (120 s × 4,
  `uncertainty_policies.py:280`), it raises `EXIT_STUCK`. The episode says
  automatic re-drives stopped and an operator reduction is needed.
- **In the test:** 41 passes of 15 s. No second order is ever sent, and the
  episode names "work in flight that could fill under it".
  [test] `test_an_exit_refused_as_a_wash_trade_while_the_other_bot_buys_escalates_without_a_retry`
- **Net effect.** A resting opposite limit from another bot turns a temporary
  conflict into a permanent operator task.

## 3. Pattern day trading, the intraday margin rule and settlement

### Pattern day trading no longer applies

- **Alpaca removed the fields.** The Trading API account object lost
  `pattern_day_trader`, `daytrade_count` and `daytrading_buying_power` on
  2026-07-06 [doc: PDT removal changelog].
- **Alpaca says the rule is gone.** The PDT designation, the 4-trade limit and
  the $25,000 minimum are gone, and clients no longer meet PDT rejections [doc:
  Intraday Margin Rule; Non-leverage accounts].
- **The real capture confirms it.** The paper-account capture of 2026-07-24 has
  none of the three keys [fixture] `tests/fixtures/alpaca/account/account.json`.
  The existing test `tests/broker/alpaca/test_adapter_account.py:40-46` already
  pins both mapped fields to `None`.
- **What the code does.** It still ingests the fields and renders
  "Pattern day trader: Unknown" and a "Day-trading BP" row, nothing else.
  [code] `broker/alpaca/adapter.py:215,220`; `broker/contract/models.py:160,166`;
  `Frontend/.../alpaca-account-card.component.html:78`;
  `.../alpaca-account-margin.component.ts:33`.
- **For two bots on one symbol:** there is no day-trade count to exhaust.
  Same-day round trips across bots draw no Alpaca rejection.

### The Intraday Margin Rule replaces it [doc: Intraday Margin Rule]

- **What it measures.** It tracks the peak margin deficiency after a trade during
  the day.
- **The call.** A deficit issues a margin call due in two business days.
- **The freeze.** If the call is unmet by the fifth business day, the account is
  frozen for 90 days from increasing shorts or debits.
- **The de-minimis exception.** Deficits under $1,000 or 5% of equity (whichever
  is lower) generally don't trigger a call.

The Clerk's cash bound keeps gross long exposure within `cash` [code]
`budgets.py:140-145`. Two ways past it remain:

- a regular-session market fill above the decision price (ADR 0059 D4.1,
  amended 2026-09-27);
- an oversell that opens a short, which the drift guard in section 1 prevents
  for Clerk orders.

Two bots on one symbol add no new path: the bound is account-wide.

### Settlement

- **Every Alpaca account is a margin account.** Alpaca opens all accounts as
  margin accounts; multiplier 1 is a limited margin account with 1× buying power
  [doc: Trading Account; Margin and Short Selling].
- **Cash-account rules don't arise.** Good-faith violations and free-riding
  belong to cash accounts.
- **T+1 doesn't hold back reuse.** Alpaca notes that margin-enabled accounts
  avoid T+1 settlement delays even without leverage [doc: Non-leverage accounts].
- **`cash` moves at the fill.** Account `cash` reflected fills 35–425 ms before
  their trade update [fixture] `tests/fixtures/alpaca/fill_visibility/`, #2487.
  So the bound counts a sale's proceeds immediately, not at T+1.
- **The ADR wording is wrong.** ADR 0059's "Considered and rejected" says the
  cash bound "bounds every order by settled cash"
  ([ADR 0059](../architecture/adrs/0059-real-money-live-behind-shadow-gate-arming-and-cash-bound-envelope.md),
  per-order notional cap bullet). The code reads Alpaca `cash`, which is not
  settled cash.
- **Two bots change nothing here.** Settlement is account-level.

## 4. A2 / #2441 (cash race) on master

**Fixed and still in place.**

- **The fix.** The observation is stamped before the broker reads
  [code] `clerk/sqlite/live_envelope_sync.py:328`. Fills recorded up to
  `FILL_VISIBILITY_GRACE_MS = 5_000` before that stay reserved
  [code] `clerk/live_envelope.py:90,241`. Commits `f656066f` (#2473) and
  `d0cf484c` (#2487, measured lag at most 401 ms).
- **Its tests pass.**
  - `test_live_envelope_sync.py -k "while_the_broker_is_read or just_before_the_read"`: 3 passed.
  - `test_live_envelope.py`, `test_envelope_admission.py`, `test_envelope_reservations.py`: 48 passed.
- **A second gate closes it again.** The observation also records the fill
  sequence before its reads (`live_envelope_sync.py:329`). Any fill recorded
  since then refuses all ENTERs (section 1). The double-spend A2 described now
  needs both gates to fail.
- **Nothing new from the same-symbol setup.** The sibling-fill refusal is a
  separate cost, drafted below.

## 5. Recommendation: allow, with guards

**Why not refuse:**

- **Attribution is sound per bot:** positions, FIFO results, budgets and EXIT
  sizing (section 1).
- **The design already expects many bots per account:**
  - ADR 0059 D4.1 reserves working ENTERs "across all instances".
  - ADR 0062 D2 puts every bot of an account behind one clerk.
  - The watchdog is written for netted subjects
    (`exit_watchdog.py:413-416`, "A +10, B −10, broker 0").
- **Refusing would bring back a retired guard.** The all-in coexistence guard of
  ADR 0009 §13 was deleted with `engine/live/pre_flight.py` in `366545ba` (#1678).
- **It sits next to an owner rejection.** A symbol-exclusive rule is close to the
  "one symbol at a time" restriction the owner rejected in ADR 0059, though not
  the same thing.

**Why guard:** Alpaca's wash-trade protection makes opposite-side orders from two
bots fail predictably. Today each failure costs money or needs an operator:

- a claim that never releases;
- a mislabelled error;
- an exit that becomes `EXIT_STUCK`.

The Clerk already knows every working order in the account, since it placed them.
It can apply Alpaca's documented table before sending, instead of learning it
from a 403.

**Owner decision needed:** when bot A must EXIT while bot B's opposite ENTER is
working on the same symbol:

1. A waits until B's order ends, with no escalation while it waits (recommended
   default).
2. A cancels B's working ENTER, because exits come first.
3. Refuse a second bot on a symbol already traded in the account (the retired
   guard, brought back).

## 6. Follow-ups (drafted; numbers filled in by the orchestrator)

- **#FOLLOWUP-A:** a refused or rejected ENTER keeps its cash claim forever (bug,
  all accounts).
- **#FOLLOWUP-B:** Alpaca's order-level 403 reads as a credentials failure, and
  its code is dropped.
- **#FOLLOWUP-C:** check another bot's opposite working order before sending
  (the owner decision above).
- **#FOLLOWUP-D:** one bot's fill refuses the other bots' entries until the next
  reading, and the decision is lost (owner decision).
- **#FOLLOWUP-E:** remove the pattern-day-trader and day-trading-buying-power
  fields Alpaca retired.
- **#FOLLOWUP-F:** retire the stale coexistence-guard and capital-sleeve glossary
  entries, and correct ADR 0059's "settled cash".

## 7. Dead machinery found

| Item | Evidence | Proposed removal |
|---|---|---|
| `pattern_day_trader`, `daytrading_buying_power`: ingested, modelled, rendered. Always absent from Alpaca since 2026-07-06. | [code] section 3; [fixture] 2026-07-24 capture | #FOLLOWUP-E: remove from adapter, `BrokerAccountSnapshot`, OpenAPI/TS types, the account card and margin rows, the synthetic brokers and ~15 test constructors, and `test_malformed_pattern_day_trader_is_rejected`. |
| The CONTEXT.md "all-in coexistence guard" and "capital sleeve (future — not v1)" entries describe a guard deleted in `366545ba` and a sleeve that bot budgets replaced. ADR 0009 §13 still reads as live. | [code] no `coexist` match under `app/`; CONTEXT.md:936-958 | #FOLLOWUP-F |
| `app/engine/live/account_registry.py:73` `bot_order_namespace_for_instance` duplicates the canonical `order_identity.build_bot_order_namespace` with a hard-coded format. It is used only by three test helpers and one re-export. | [code] grep | Trivial: point the three test helpers at `build_bot_order_namespace` and delete the duplicate and its re-export (`account_artifacts.py:1316`, `account_registry.py:474`). No issue needed. |

## Tests and commands

From `PythonDataService/`:

```text
DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest tests/broker/alpaca/clerk/sqlite/test_two_bots_one_symbol.py -q -rxX
18 passed, 3 xfailed
```

- **Reuse.** The file uses the existing Clerk fixtures and fake broker ports:
  `conftest._FakeTradePort` and `_make_held_position`; the budget harness from
  `test_budget_commands`; `_append_slice`; and the watchdog harness from
  `test_reconcile`.
- **The xfails.** Each is `strict=True` and names its follow-up. Run with
  `--runxfail`, they fail on the asserted defect: `BrokerAuthError` is not
  `BrokerOrderRejected`, and `order_claims` is `200.01`, not 0.

## Sources

- Alpaca, *User Protection*: wash-trade rule, rejection table, paper applicability, Equity/Order ratio check. https://docs.alpaca.markets/us/docs/user-protection (read 2026-09-29)
- Alpaca, *Create an Order* (`POST /v2/orders` response codes). https://docs.alpaca.markets/us/reference/postorder
- Alpaca, *Pattern day trading fields and configurations removed* (changelog 2026-07-06). https://docs.alpaca.markets/us/changelog/2026-07-06-pdt-db49dba
- Alpaca, *Pattern day trading and DTBP fields and endpoints are deprecated* (changelog 2026-06-03). https://docs.alpaca.markets/us/changelog/2026-06-03-pdt-651df23
- Alpaca, *The Intraday Margin Rule*. https://docs.alpaca.markets/us/docs/the-intraday-margin-rule
- Alpaca, *Intraday Margin Rule for Non-Leverage Margin Accounts*. https://docs.alpaca.markets/us/docs/intraday-margin-rule-for-non-leverage-margin-accounts
- Alpaca, *Trading Account* (account plans, multipliers). https://docs.alpaca.markets/us/docs/account-plans
- Alpaca, *Margin and Short Selling*. https://docs.alpaca.markets/us/docs/margin-and-short-selling
- FINRA Rule 5210 and Supplementary Material .02 (linked from Alpaca's wash-trade section). https://www.finra.org/rules-guidance/rulebooks/finra-rules/5210
- Wash-trade body code/message (secondary: Alpaca community forum). https://forum.alpaca.markets/t/apierror-potential-wash-trade-detected-use-complex-orders/13441
