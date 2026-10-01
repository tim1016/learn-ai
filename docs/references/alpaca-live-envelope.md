# Alpaca live envelope — cash bound, day P&L, and the loss hold

**Status:** canonical for ADR 0059 slice 5 (2026-09-08), account-day-P&L
semantics amended 2026-09-24 by owner decision #2423. Lineage: live.

## What it is

Two rules, evaluated in `accept_enter` as a sibling of `require_admission` —
inside the custody fence, before `ENTER_ACCEPTED`, so no broker contact
precedes either one. Neither rule ever runs inside a Signal Program
(broker-neutral, ADR 0042) and neither is composed by the Frontend (ADR 0011
§7). EXIT is never subject to either rule.

1. **Cash bound.** An ENTER is admitted only if its own notional plus every
   working ENTER's unfilled notional does not exceed broker-observed cash.
   The bound is `cash`, not `buying_power`, so a cash account and a Reg T
   margin account are admitted identically. The check is an estimate at the
   decision price, not a guarantee (amended 2026-09-27, #2442; it formerly
   claimed an Intraday Margin Deficit was impossible by construction): a
   regular-session market ENTER is priced at its decision bar's close and can
   fill above it, while an extended-hours entry is a day limit genuinely
   bounded by its limit price, and a recorded fill reserves its actual cost
   until the next broker read supersedes it. A refusal
   (`LIVE_ENVELOPE_CASH_EXCEEDED`) changes no other state.
2. **Daily loss hold.** `day_pnl` is account-wide: current broker equity minus
   broker `last_equity` at the prior regular-session close, minus signed
   deposits and withdrawals after that same close. When it breaches
   `min(loss_fraction × last_equity, loss_usd)` from the applied limits, the
   account enters loss hold — every ENTER refused `LIVE_ENVELOPE_LOSS_HOLD`,
   every EXIT still running so each program keeps managing its own open
   position. Only a guarded operator action releases it; it does not clear at
   session rollover.

By the owner's decision the envelope restricts nothing else: no per-order
notional cap, no symbol allowlist, no session restriction.

## The facts

- **Cash observation.** `LiveEnvelopeSync.observe`
  (`PythonDataService/app/broker/alpaca/clerk/sqlite/live_envelope_sync.py`)
  reads `account.cash` every tick. Under simulated custody
  (`custody_is_simulated=True`, true under shadow; false on the live
  authority, whose broker cash already reflects the Clerk's own fills) it
  subtracts `account_net_cash_spent_usd()` — what the Clerk's own synthesized
  fills would have spent — to get `cash_available_usd`; under real custody the
  broker's own cash already reflects it.
- **The observation is dated when its reads are issued** (fixed 2026-09-24,
  #2441). `observed_at_ms` is stamped before the bracketed cash-transfer and
  account reads go out, never when they return: a broker answer is only
  known to be at least as recent as its request. Stamped on return, the
  observation released the reservation of a fill the Clerk recorded during
  the round trip — which the broker's answer could predate — and a second
  instance was admitted against cash the first had already spent. One slow
  account read was enough.
  Freshness is aged from the same instant (it errs old by the round trip).
- **Reservations, fills-aware.** `PythonDataService/app/broker/alpaca/clerk/sqlite/envelope_reservations.py`
  prices the part of an accepted ENTER the latest observation cannot see. A
  fill counts as seen only when the Clerk recorded it before the observation's
  `fills_seen_before_ms` (`observed_at_ms − FILL_VISIBILITY_GRACE_MS`, a
  property of `AccountObservation`). Each part prices at what it costs
  (fixed 2026-09-27, #2442): a recorded-but-unseen fill reserves its actual
  cost — fill price × quantity plus any reported fee — because that is the
  cash the broker already took at the fill's own price; only quantity no
  recorded fill names prices at the reservation's reference price (the
  decision price the ENTER was admitted against). A working *or filled*
  order reserves its unseen fills at cost plus its unrecorded remainder at
  the reference price; an ended order reserves only its unseen fills, because
  its unrecorded remainder is cancelled quantity, never cash. An order has
  ended when the broker ended it (canceled/expired/rejected/replaced), or when
  its ENTER effect is terminal while nothing says the broker ever knew it -- no
  broker order id and no fill: refused before contact, failed outright, or
  proven absent (#2553, the leak research #2469 found). An ENTER whose outcome
  is unknown keeps its whole claim until it is resolved. Corrections fold at their restated size:
  only the head of each correction chain counts (resolved through
  `economic_projection.py::EFFECTIVE_FILL_LINEAGE_CTE`), dated by the
  *root* execution's `recorded_at_ms`, because the broker's cash at that
  instant already reflected the true quantity however late the Clerk recorded
  the restatement. While any of the order is unfilled, its remainder also
  claims the whole fee provision the ENTER recorded at admission -- no share
  is computed, and it is never a re-quote from the fee model (#2553; owner
  decision 2026-09-29). `envelope_reservations.entry_cash_claims`, which the
  money read hands `observation.fills_seen_before_ms`, prices every accepted
  ENTER exactly, and `sqlite/envelope_admission.py` judges the ENTER with
  `budgets.budget_entry_decision`: its entry requirement
  (`budgets.entry_requirement`, the notional plus the canonical BUY fee
  provision) against the deployment's free budget and the account's cash after
  every other claim. An account not yet switched to budgets admits no ENTER
  (`BUDGETS_NOT_SWITCHED_ON`); its owner switches it in Settings. A
  reservation written before provisions were recorded refuses with
  `ENTRY_FEE_PROVISION_UNRECORDED` while it has an unfilled remainder.
- **The fill-visibility grace.** `FILL_VISIBILITY_GRACE_MS = 5_000`
  (`live_envelope.py`): a fill the Clerk recorded up to 5 s *before* the reads
  were issued stays reserved too, because nothing Alpaca publishes says its
  account cash reflects a fill by the time that fill's trade update reaches
  us. What the primary sources do and do not say (checked 2026-09-24):
  - The Broker API FAQ says account values without a `last_` prefix are
    "updated Real-Time post trade executions", and that `cash` moves once a
    SELL fills ([Broker API FAQs](https://docs.alpaca.markets/us/docs/broker-api-faq)).
    Real-time is a latency description, not an ordering promise against the
    stream.
  - The Trading API account endpoint describes `cash` only as the cash
    balance, with nothing on update timing or consistency with fills
    ([Get account](https://docs.alpaca.markets/reference/getaccount-1)); the
    positions endpoint likewise says only that market values update as prices
    do ([All open positions](https://docs.alpaca.markets/us/reference/getallopenpositions)).
  - The `trade_updates` stream defines `fill` and `partial_fill` (with the
    position size after the event) but states no delivery, ordering or
    consistency guarantee relative to the REST account view
    ([Websocket streaming](https://docs.alpaca.markets/us/docs/websocket-streaming)).

  Measured 2026-09-28 (#2487) with
  `scripts/measure_fill_to_cash_visibility.py`: for each fill event received
  on `trade_updates`, the first account read (dated when its request was
  issued, the `observed_at_ms` discipline) whose cash reflects the fill. On
  the paper account's BTC/USD fills — 45 events in the committed run plus
  three rehearsal runs, ~150 observations, one idle account — the cash never
  lagged a fill's event by more than a read's own round trip. Per fill, what
  the first reflecting read proves depends on where its *answer* landed
  relative to the receipt: 2 fills' cash was provably visible before the
  event (the read answered 24 ms ahead of it); for the other 42 the read was
  issued before the event and answered at most 329 ms after it, so
  visibility at the receipt instant is interval-censored — provably no worse
  than 329 ms after the event, possibly before. Every measured bound is an
  answer-time bound: Alpaca guarantees nothing about the snapshot's as-of
  instant, so a read proves only that the cash was visible when it answered,
  never that it was visible at the read's own issue time. The worst bound
  observed across every run was 401 ms, itself poll-cadence quantization
  (reads p95 438 ms apart), not propagation. The committed fixture
  (`tests/fixtures/alpaca/fill_visibility/paper-btcusd-2026-09-28.json`, with
  `attribution.md`) carries the full read series so every bound is
  recomputable.

  So the grace stays at 5 s. The measurement found no seconds-long lag in
  this sample — every fill's cash was provably visible within one read's
  own round trip of its event — but it establishes no issue-to-snapshot
  ordering guarantee and does not license shrinking: the sample is
  crypto-only, weekend, one paper account,
  while the envelope gates equity ENTERs under market-hours load Alpaca's
  docs still refuse to bound. Five seconds remains an order of magnitude
  over everything observed, under one 15 s sync interval, so a fill stays
  reserved at most one observation longer than it otherwise would
  (`tests/broker/alpaca/clerk/test_live_envelope.py` pins it below the
  interval and above the measured maximum times a documented cushion
  factor). Over-reserving refuses an ENTER that would have fit;
  under-reserving admits a second ENTER against cash already spent.
  Re-measure any time — including equity fills on a trading day — with the
  same script's `observe` mode, which is read-only, places nothing, and
  censors fills whose attribution windows overlap rather than emitting
  per-fill delays from cash another fill moved.
- **Under shadow a fill can count twice, never zero times.** Simulated custody
  subtracts `account_net_cash_spent_usd()`, read *after* the broker answered,
  so a fill recorded during the read (or inside the grace) is both subtracted
  from `cash_available_usd` and still reserved until the next observation
  issued past the grace. For up to one tick the rehearsal refuses an ENTER
  the true free cash would cover; it never admits one that cash cannot.
- **Account day P&L, the prior-close rule.**
  `PythonDataService/app/broker/alpaca/clerk/sqlite/day_pnl.py::day_pnl_at`
  computes `current equity − last_equity − net cash flows after the prior
  regular-session close`.
  Alpaca defines `last_equity` as the previous trading day's 16:00 ET equity
  ([Account Object](https://docs.alpaca.markets/us/v1.1/docs/account-plans))
  and recommends `equity - last_equity` for the account's day change
  ([Working with /account](https://docs.alpaca.markets/us/docs/working-with-account)).
  The start is the canonical close of the trading day before the current ET
  calendar day. It therefore does not advance after today's session closes,
  and weekends, holidays, and early closes cannot make the cash-flow horizon
  disagree with the `last_equity` baseline. The sync reads the complete
  `TRANS` window immediately before and
  after its account snapshot and accepts it only when both economic row sets
  match. It subtracts signed `CSD` deposits and `CSW` withdrawals, whose
  `net_amount` sign is part of Alpaca's activity contract: a deposit must be
  strictly positive and a withdrawal strictly negative; zero or a
  contradictory sign makes the result unknown
  ([Account Activities](https://docs.alpaca.markets/us/docs/account-activities)).
  The dedicated `TRANS` pagination validates newest-first continuity and reads
  one proof page beyond the first page that crosses the date boundary; a
  missing, repeated, cyclic, or out-of-order cursor raises instead of returning
  a partial set. Non-object rows, rows that cannot be mapped, rows lacking a
  nonblank broker id, rows not definitively `executed`, and rows whose raw
  `net_amount` is missing, non-numeric, non-finite, or a JSON boolean make the
  read unavailable. This rejected-evidence condition withdraws the prior
  envelope observation immediately; only a transient transport failure uses
  the normal freshness age-out. Duplicate activity ids carrying different
  economic evidence do the same; economically identical duplicates are
  collapsed. The canonical evaluator defensively treats contract activities
  with missing or non-finite amounts as unknown too. Missing timestamps and a
  date-only transfer on the boundary session make `DayPnl.known` false rather
  than turning an
  unclassified cash movement into profit or loss. A sync tick whose account
  snapshot lands in a different ET loss window than its transfer queries is
  also unknown and withdraws the prior observation.
  Broker equity already includes every carried
  position and manual/external trade, so no Clerk FIFO or lifetime-unrealized
  composition participates in this account fact.
- **A malformed account response is rejected evidence.** The adapter's
  `to_float`/`opt_float`, which parse every Alpaca money and quantity field,
  refuse a JSON boolean, because `float(True)` is `1.0`: a boolean
  `last_equity` would otherwise be a $1 prior close. An account response the
  adapter cannot map (a boolean, null, non-numeric or missing `cash` or
  `equity`, or a boolean or non-numeric `last_equity`) makes
  `AlpacaBroker.get_account` raise `BrokerEvidenceUnavailable`, with the
  underlying error chained as its cause. That withdraws the prior envelope
  observation immediately, like rejected transfer evidence above. The sync's
  warning line logs the chained cause; the exception's own message and
  `detail` stay plain owner copy.
- **Positions are not a loss input.** Current equity already includes every
  open position. The sync therefore does not call the positions endpoint for
  this verdict; `AccountObservation.position_count` remains `None` rather
  than letting a diagnostic read suppress a loss hold or guarded clear.
- **An account with no loss limit is unjudgeable.** With no applied policy
  and no configured envelope there is nothing to judge a loss against, so the
  observation is withdrawn and every ENTER refuses `LIVE_ENVELOPE_UNOBSERVED`,
  exactly as an unknown day P&L does; the guarded clear names the missing
  limit rather than the broker feed (`EnvelopeReading.limit_set`). Until
  #2629 an unreadable arming seal took this branch too; nothing is sealed
  from an arming any more.
- **`last_equity`.** This one broker field is both the prior-close baseline in
  account day P&L and the equity base in `min(loss_fraction × last_equity,
  loss_usd)`; the factors are the applied risk policy's, else
  `LiveEnvelopeGate.values`. A broker
  snapshot with no `last_equity` leaves neither fact computable and is
  unjudgeable.
- **Extended-hours allowances.** A program leg's entry allowance is the
  envelope's `xh_entry_bps`; every EXIT prices from its own bot's immutable
  exit terms (ADR 0045), so an **EXIT is never refused or delayed for want of
  an arming**.
- **A sealed record's floats carry the environment's domains.** `AlpacaSettings`
  refuses to boot on a `loss_fraction` outside (0, 1), a non-positive or
  non-finite `loss_usd`, or a `*_bps` outside [0, 10000). `live_envelope.py`'s
  `_ENVELOPE_DOMAINS` re-asserts each on the values a record *sealed*, through
  `envelope_domain_violation()` — which `live_arming.py`'s record validator
  asks rather than restating, so the domain lives beside the type both
  producers build. `LiveEnvelopeValues` is a plain dataclass with no field
  validation, and a row re-sealed over a tampered value verifies both digests —
  so without this a sealed `inf` loss limit (never breached) or a sealed 10000
  bps exit anchor (floors to zero, refuses the leg) would reach the money. The
  two copies of the bounds are pinned together by
  `tests/broker/alpaca/test_config.py::test_the_envelope_domains_agree_with_the_settings_that_declare_them`.

## Refusals

| Code | Fires when | Scope |
|---|---|---|
| `LIVE_ENVELOPE_UNOBSERVED` | No observation is fresh (older than `OBSERVATION_MAX_AGE_MS = 45_000` ms — three missed 15 s sync ticks), or a market ENTER has no decision-bar price, or the sync withdrew its last observation because the reading was unjudgeable (missing prior-close or complete transfer evidence, non-finite cash/equity, no loss limit set) **or breached** | ENTER refusal |
| `LIVE_ENVELOPE_CASH_EXCEEDED` | The ENTER's notional plus reserved notional would exceed cash available | ENTER refusal |
| `LIVE_ENVELOPE_DISAGREEMENT` | Retired (#2629): no envelope is sealed from an arming any more, so nothing raises it; receipts recorded under it stay readable ([alpaca-live-arming](alpaca-live-arming.md)) | historical ENTER refusal |
| `LIVE_ENVELOPE_LOSS_HOLD` | The account-wide loss hold stands — checked earlier, by `require_admission` | ENTER refusal |
| `LIVE_ENVELOPE_MISSING` | At least one envelope value is absent for a live-mode boot | startup refusal of the shadow and live authorities — never an ENTER refusal |

All four ENTER-time codes are transient at the runner: none disarms an
instance or stops a bot, and the same decision can be re-admitted once the
sync republishes a judgeable observation.

**An ENTER behind the account reading waits for a new one (#2623, owner
decision 2026-09-29).** One `LIVE_ENVELOPE_UNOBSERVED` refusal is not
dropped: executions were recorded after the last reading
(`sqlite/risk_admission.py` raises `AccountReadingBehindExecutions`, judged by
the one watermark rule `sqlite/day_pnl.py::reading_covers_executions`).

- **The reading behind executions stays published.** Neither this refusal nor
  a cadence reading an execution overtakes withdraws it (the tick answers
  `superseded`, logged at INFO). So every other ENTER that decides meanwhile —
  a third bot on the same bar, one deciding during a pause — meets the same
  refusal and waits too, instead of finding no reading and being dropped as
  unobserved. Leaving it cannot admit anything: every commitment, a budget
  deploy included, re-runs the watermark rule, and that reading fails it until
  a newer one replaces it.
- **One reading, shared, paced.** The facade
  (`sqlite/runtime.py::SqliteAlpacaClerkFacade._execute_effect`) leaves its
  intake fence and asks `LiveEnvelopeSync.read_for_entry` for a reading now.
  Every waiting ENTER shares the reading in flight; a new one starts no sooner
  than `entry_reading_interval_s` (1 s) after the last began, and not at all
  once the ENTER's run has stopped. The reading runs through the cadence's own
  judgement, so it is published and logged exactly as a tick is; a fault no
  verdict maps is logged (`live_envelope_entry_reading_failed`) and ends the
  wait as unread rather than crashing each waiting bot.
- **Then the whole decision is judged again**, so every other refusal —
  cash, the market-closed gate — applies afresh and drops the ENTER under its
  own code.
- **How the wait ends without the entry.** Each ending is one `blocked`
  receipt under `LIVE_ENVELOPE_UNOBSERVED` whose words name it: the bot was
  stopped (checked under the intake fence Stop commits under, and first); the
  account could not be read; no covering reading landed while the decision was
  on time — its bar's close plus the 20 s delivery allowance, carried as
  `EffectDecisionEvidence.decision_valid_until_ms`, which also covers a reading
  an execution overtook with no time left for another, and a broker still
  answering at the limit (the wait is cut off there, so a slow Alpaca cannot
  hold a bot past its next bar); or the Clerk was shutting down (its sync
  stopped, and cancelled the reading in flight). The time limit is judged in
  `read_for_entry` alone.
- **Idempotent.** Nothing is durable before acceptance, so the decision is
  accepted at most once however many attempts it takes.
- **Stop.** Stop closes the bot's decision gate, then commits through the
  Clerk's intake fence. A retry that takes the fence in between is still
  admitted — the same race any ENTER in flight at Stop has always had. What the
  wait changes is which ENTERs can be in that window (one waiting up to its
  time limit, not only one already queued), not the window itself.

The daily loss limit still works from Alpaca's readings alone; no Clerk-side
loss figure was added.

## Loss hold

`LiveEnvelopeSync.tick`
(`PythonDataService/app/broker/alpaca/clerk/sqlite/live_envelope_sync.py`) is
the only writer that raises the hold, via `raise_account_hold(...,
reason_code=LIVE_ENVELOPE_LOSS_HOLD_REASON_CODE)`; it never releases one it
raised — a standing hold is left alone (`_hold_stands`) so its cause keeps
naming the breach it was raised on, not a later tick's numbers. The sync
publishes an observation only when the reading can be judged AND is not
breached (ruling R-A′): a breached reading withdraws exactly like an
unjudgeable one, so a raise that failed cannot leave the gate admitting
ENTERs on the cash bound alone. The cost is that the first ENTER after a
clear waits for the next judgeable observation (≤ one 15 s tick, or the
clear's own re-observation).
`require_admission`
(`PythonDataService/app/broker/alpaca/clerk/sqlite/uncertainty.py`) blocks
`NEW_EXPOSURE` under this reason code exactly like any other account hold,
but explicitly authorizes every `REDUCE` under it without a per-symbol proof
— the hold is account-scoped and says nothing about any one position, so
each program keeps managing its own EXIT. The live verdict (below) projects
the hold as a verdict field, never as a third custody state.

## Clearing it

```
curl -X POST -H "X-Data-Plane-Control-Secret: $DATA_PLANE_CONTROL_SECRET" http://localhost:8000/api/brokers/alpaca/live-envelope/loss-hold/clear
```

`PythonDataService/app/services/alpaca_live_envelope.py::clear_loss_hold`,
routed at `PythonDataService/app/routers/brokers.py`
(`POST /{broker}/live-envelope/loss-hold/clear`), is the ADR 0011 §6 shape:
it re-observes the account and refuses while the breach still stands, rather
than trusting the caller.

The clear judges both the current limit and the one the hold was raised on
(#2543): raising the configured loss limit and restarting releases nothing
while the retained threshold is still breached, and the `detail` says which
limit decided (`effective account policy` / `configured in the environment`).
No arming seal takes part (#2629).

Three outcomes:

- `cleared` — the re-observed day P&L is above the loss limit; the hold is
  released and new entries are admitted again.
- `no_hold` — the account was not (or is no longer) in loss hold; nothing
  changes.
- `refused` — with `LIVE_ENVELOPE_LOSS_HOLD_STANDS` (day P&L is still at or
  below the limit) or `LIVE_ENVELOPE_UNOBSERVED` (the account could not be
  re-observed, or the fact is unjudgeable); the hold stands.

`broker != "alpaca"` is a 404. No live envelope installed on the active
authority (paper, synthetic, or no runtime at all) is a 503. The route
requires the `X-Data-Plane-Control-Secret` header, like every other
data-plane control mutation.

## In the live verdict

Slice 5 fills `loss_hold` on `AlpacaLiveVerdict`
(`PythonDataService/app/schemas/alpaca_live_verdict.py`), server-authored and
rendered verbatim by the banner. It is not a custody state: it describes what
the envelope *is*, not what custody says. `not_applicable` is the common case
— every paper, unconfigured, disagreeing and unobserved verdict carries it.
(The slice-5 `envelope_agreement` field was retired with the arming verdict,
#2547.)

| Field | Value | Means |
|---|---|---|
| `loss_hold` | `not_applicable` | The hold is not observed here: no runtime, or an authority with no envelope (paper, synthetic, unavailable). |
| | `clear` | The hold is observed and no `LIVE_ENVELOPE_LOSS_HOLD` episode is active. |
| | `held` | The account-wide loss hold stands: every ENTER is refused, every EXIT still runs, and only the guarded clear above releases it. |

Under the current ruling the hold is observed only for a `shadow` authority
(`alpaca_live_verdict.py::observe_loss_hold`, one predicate); slice 7 widened
it to every facade authority.

## Residuals

- **Market slippage above the decision close, decision-to-fill only.** A
  market ENTER is admitted against `reference_price` — the decision bar's
  close — not its eventual fill price, so a fill above that close spends more
  cash than the bound admitted for. The reservation itself no longer
  under-states the spend (fixed 2026-09-27, #2442): once a fill is recorded,
  it reserves at its actual cost until the next broker read supersedes it.
  What remains estimated is the decision-to-fill window and the
  not-yet-recorded remainder of a filled order, both priced at the reference.
- **Account day P&L under shadow is the live account's.** The sync's
  `envelope_read` is the live read port, not the shadow book, so `equity`,
  `last_equity`, transfer activities, and cash describe the live account.
  Simulated fills affect the shadow rehearsal's available-cash
  subtraction but do not invent broker equity.
- **A mirror rebuild loses only the oldest reservations.** Every reservation
  is now folded from `ENTER_ACCEPTED`'s facts -- its exact price and recorded
  fee provision (#2553) -- so a rebuild restores it. A reservation written
  before that, as product evidence *outside* the custody hash chain (plan R9),
  is not carried by the mirror and a rebuild ceremony does not restore it.
  After a rebuild, such an accepted-but-unfilled ENTER reserves nothing, so
  the pre-cutover cash bound briefly admits against cash it has already
  claimed. The window is bounded by one sync cadence plus the life of that
  working order: the next 15 s tick re-observes cash, and any fill recorded
  by then is already subtracted through `account_net_cash_spent_usd`. (The
  budget read refuses outright while an entry order has no reservation.)
- **No Frontend button yet.** `Frontend/src/app/shell/alpaca-live-banner.component.ts`
  (Task 10) renders a `· loss hold` chip on the banner when
  `loss_hold === 'held'`; it carries no clear action. Clearing the hold is
  the `curl` above, operator-run, until one is built.

## Decision record

[ADR 0059](../architecture/adrs/0059-real-money-live-behind-shadow-gate-arming-and-cash-bound-envelope.md)
Decision 4 (the risk envelope); owner rulings 2026-09-08 fix the reservation
shape (a working *or filled* order reserves; a dead order reserves only its
post-observation fills), the simulated-custody cash subtraction, the sync as
the loss hold's sole raiser that never releases it, and every ENTER-time
refusal staying transient at the runner. Owner decision #2423 on 2026-09-24
replaces the old FIFO-plus-lifetime-unrealized day-P&L composition with the
cash-flow-adjusted prior-close equity change and requires withdrawal on a
missing baseline or incomplete transfer evidence.

The independently hand-computed golden fixture `PNL-001` applies the cited
Alpaca field semantics to no-flow, deposit, withdrawal, and mixed-flow cases.
It pins the canonical result with `atol=1e-9, rtol=0`, so the accepted dollar
error stays far below one cent and never grows with account magnitude. The
owner's figure, `DayPnl.display_total_usd`, is pinned bit-exact against the
same Decimal outputs (#2586): the mixed-flow case is exactly -3904.33, where
the float formula gives -3904.3300000000017.
