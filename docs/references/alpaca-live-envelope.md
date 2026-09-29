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
   `min(loss_fraction × last_equity, loss_usd)` from the sealed envelope, the
   account enters loss hold — every ENTER refused `LIVE_ENVELOPE_LOSS_HOLD`,
   every EXIT still running so each program keeps managing its own open
   position. Only a guarded operator action releases it; it does not clear at
   session rollover.

By the owner's decision the envelope restricts nothing else: no per-order
notional cap, no symbol allowlist, no session restriction.

## Where it runs

- `PythonDataService/app/broker/alpaca/clerk/sqlite/envelope_admission.py::require_envelope_admission`
  is called from `PythonDataService/app/broker/alpaca/clerk/sqlite/enter.py::accept_enter`,
  immediately after `require_admission` and before the transition is built —
  its sibling, not a second gate somewhere else.
- `PythonDataService/app/broker/alpaca/clerk/sqlite/live_envelope_sync.py::LiveEnvelopeSync`
  is an independent fixed-cadence background tap (`ENVELOPE_SYNC_INTERVAL_S =
  15.0`), modelled on `StreamHealthHoldSync`: it alone produces the account
  observation and the loss hold, decoupled from the reconciliation pass whose
  backoff reaches 300 s on failure. `accept_enter` never contacts the broker.
- Composition: `PythonDataService/app/broker/alpaca/clerk/active_runtime.py::compose_repository_runtime`
  takes `live_envelope: LiveEnvelopeGate | None` and
  `envelope_read: BrokerReadPort | None`, and starts/stops the sync with the
  runtime. `PythonDataService/app/main.py` takes `LiveEnvelopeValues` from the
  binding the worker resolved (`alpaca_binding.context.live_envelope`, ADR
  0060) once at startup, only when the effective revision's endpoint mode is
  not paper, and passes them down through `select_active_clerk_runtime`.
- Shadow rehearsal: `PythonDataService/app/broker/alpaca/clerk/shadow_authority.py::select_shadow_clerk_runtime`
  is the only consumer of a non-`None` `live_envelope_values` today — an
  activated live account composes the envelope on its live authority with
  `custody_is_simulated=False` (slice 7); an unactivated one rehearses it
  under the Shadow Account Authority. It
  composes `LiveEnvelopeGate(values=live_envelope_values,
  custody_is_simulated=True)` and passes the *live* account's own read port
  as `envelope_read=read` — not the shadow composite — so the sync observes
  the live account directly. Settings validation
  (`resolve_runtime_context`, and `AlpacaSettings._enforce_mode_agreement` on
  the pre-cutover environment bootstrap) already refuses a live revision
  without every envelope value, before the Clerk is composed, so that boot
  never reaches the shadow authority. `LIVE_ENVELOPE_MISSING` remains
  the selector's refusal for a caller that composes the shadow authority
  without an envelope.
- Sealing: `PythonDataService/app/broker/alpaca/clerk/sqlite/live_envelope_sync.py::LiveEnvelopeSync._refresh_arming`
  re-reads the account's arming ledger on every observation (through
  `ArmingRefresh` in `sqlite/arming_refresh.py`) and assigns
  `LiveEnvelopeGate.sealed` from the newest arming record's own
  `envelope_values` (ADR 0059 slice 6). Until then `sealed` was permanently
  `None`, so `envelope_agreement` could only answer `unsealed` and
  `LIVE_ENVELOPE_DISAGREEMENT` was unreachable. It is now the live rule: an
  envelope edit that becomes effective after an arming refuses every ENTER
  until a re-arm. See
  [alpaca-live-arming](alpaca-live-arming.md).
- **The sealed envelope is the source of the numbers, not only an equality
  gate** (fixed 2026-09-10; owner decision of the same date). `LiveEnvelopeGate.in_force`
  (`PythonDataService/app/broker/alpaca/clerk/live_envelope.py`) answers
  `sealed if sealed is not None else values`. It is the one place that rule is
  written, and both *judgements* go through it:
  - the loss limit the sync raises the hold on, and the one the guarded clear
    refuses against — `LiveEnvelopeSync.observe`;
  - the extended-hours allowance a program leg is priced from, entry and exit
    alike — `sqlite/runtime.py::SqliteAlpacaClerkFacade.program_leg_policy`,
    the one accessor that resolves the authority's `ProgramLegPolicy` against
    the envelope. The policy itself stays a plain value and
    `shape_program_leg` a pure function of it; the authority, which holds the
    envelope, is what applies the rule. Resolved on the accessor rather than
    at the pricing call so Start/Resume admission (`extended_hours_admission_fact`)
    cannot answer off a different policy than the one that prices.

  The configured `values` stay the input to the two questions that are *about*
  the environment — `agreement`, and the arming snapshot's own disagreement
  check — and they are the fallback for an account no ceremony has ever armed,
  which has nothing sealed to prefer.

  This used to be the other way around: the loss limit was computed from the
  configured values and the seal was only an equality gate. That was argued to
  be equivalent because a disagreement refuses every ENTER, and it was not: an
  operator could raise `ALPACA_LIVE_LOSS_USD`, restart, and *clear a standing
  loss hold* against the looser limit without re-arming. ENTER stayed refused
  under `LIVE_ENVELOPE_DISAGREEMENT`, so the account was left holdless and
  unable to trade — and the re-arm that fixed the disagreement restored no
  hold, because nothing re-raises one. A change to a value is a re-arm; that
  now holds for what the value *does*, not merely for whether it matches.

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
- **An unreadable seal is unjudgeable, not a fallback.** `sealed` also returns
  to `None` when the arming inputs cannot be read (a corrupt ledger row, a
  binding store that will not open), and *there* the fallback would be a
  relaxation: loosen `ALPACA_LIVE_LOSS_USD`, corrupt the ledger, and the
  account would be judged by the looser number. So
  `LiveEnvelopeSync._seal_unreadable` makes such a tick unjudgeable — the
  observation is withdrawn and every ENTER refuses `LIVE_ENVELOPE_UNOBSERVED`,
  exactly as an unknown day P&L does. Extended-hours pricing takes the opposite
  branch deliberately (see the allowances bullet below): an exit leaves the
  account, so falling back can only help.
- **Both arming inputs are read under one fault.** `ArmingRefresh` reads the
  ledger *and* the runner's sealed bindings inside one `try`
  (`sqlite/arming_refresh.py::_read_seals`), because a refresh holding only
  half of them can describe neither. The bindings callable reaches disk, so an
  `OSError` there is this module's own `LiveArmingInvalid` rather than an
  escaping error — the guarded clear re-observes through this path, and an
  unhandled error would answer HTTP 500 where the contract is "refused, the
  hold stands". Note the cost: the clear endpoint now does the same
  synchronous ledger + binding-store read the 15 s tap does, on the request. It
  is a handful of small files today; if the fleet grows enough for that to
  matter, the read moves off the request path, not the freshness rule.

- **`last_equity`.** This one broker field is both the prior-close baseline in
  account day P&L and the equity base in `min(loss_fraction × last_equity,
  loss_usd)`; the configured factors are read off
  `LiveEnvelopeGate.in_force` — the sealed envelope where one exists. A broker
  snapshot with no `last_equity` leaves neither fact computable and is
  unjudgeable.
- **Extended-hours allowances.** `xh_entry_bps` / `xh_exit_bps` are sealed at
  arming with every other envelope value, so the marketable-limit anchor
  (`PythonDataService/app/broker/alpaca/marketable_limit.py`) widens from the
  pair `in_force`. An **EXIT is never refused or delayed for want of a seal**:
  an account with no arming record, or one whose ledger this observation could
  not read, prices from the configured allowances rather than stranding a
  position the operator is closing. (The unsealed transition is already logged
  once by the sync — `live_envelope_unsealed`, or `live_arming_ledger_invalid`
  at error level — so the leg path adds no second line.) Entries need no such
  escape hatch: a live ENTER is already refused `LIVE_ENVELOPE_DISAGREEMENT`
  whenever the environment and the seal differ, and
  `LIVE_ENVELOPE_UNOBSERVED` whenever the seal cannot be read.
- **The sealed floats carry the environment's domains.** `AlpacaSettings`
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
| `LIVE_ENVELOPE_UNOBSERVED` | No observation is fresh (older than `OBSERVATION_MAX_AGE_MS = 45_000` ms — three missed 15 s sync ticks), or a market ENTER has no decision-bar price, or the sync withdrew its last observation because the reading was unjudgeable (missing prior-close or complete transfer evidence, non-finite cash/equity, unreadable seal) **or breached** | ENTER refusal |
| `LIVE_ENVELOPE_CASH_EXCEEDED` | The ENTER's notional plus reserved notional would exceed cash available | ENTER refusal |
| `LIVE_ENVELOPE_DISAGREEMENT` | The envelope sealed by the account's newest arming record disagrees with the effective profile revision's envelope values ([alpaca-live-arming](alpaca-live-arming.md)) | ENTER refusal |
| `LIVE_ENVELOPE_LOSS_HOLD` | The account-wide loss hold stands — checked earlier, by `require_admission` | ENTER refusal |
| `LIVE_ENVELOPE_MISSING` | At least one envelope value is absent for a live-mode boot | startup refusal of the shadow and live authorities — never an ENTER refusal |

All four ENTER-time codes are transient at the runner: none disarms an
instance or stops a bot, and the same decision can be re-admitted once the
sync republishes a judgeable observation.

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

The limit it refuses against is the **sealed** one. `sync.observe()` re-reads
the arming ledger before it re-reads the account, so the clear judges the
envelope armed right now — not one an operator edited into the environment
since. Raising `ALPACA_LIVE_LOSS_USD` and restarting releases nothing; a
re-arm at the new values does, and the `detail` says which envelope decided
(`sealed at arming` / `configured in the environment`).

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

Slice 5 fills two fields on `AlpacaLiveVerdict`
(`PythonDataService/app/schemas/alpaca_live_verdict.py`), both server-authored
and rendered verbatim by the banner. Neither is a custody state: they describe
what the envelope *is*, not what custody says. `not_applicable` is the common
case for both — every paper, unconfigured, disagreeing and unobserved verdict
carries it.

| Field | Value | Means |
|---|---|---|
| `envelope_agreement` | `not_applicable` | No envelope object is installed on the active authority: a paper or synthetic account, or a live boot the composition refused `LIVE_ENVELOPE_MISSING`. |
| | `unsealed` | An envelope is installed and this account's arming ledger holds no arming record, so nothing has sealed its values yet. |
| | `agreed` | The sealed values and the effective profile revision have the same sha. |
| | `disagreed` | They differ; every ENTER is refused `LIVE_ENVELOPE_DISAGREEMENT` until the account is re-armed. |
| `loss_hold` | `not_applicable` | The hold is not observed here: no runtime, or an authority with no envelope (paper, synthetic, unavailable). |
| | `clear` | The hold is observed and no `LIVE_ENVELOPE_LOSS_HOLD` episode is active. |
| | `held` | The account-wide loss hold stands: every ENTER is refused, every EXIT still runs, and only the guarded clear above releases it. |

`not_applicable` rather than `unsealed` is the deliberate answer for a missing
envelope: `unsealed` reads as "configured, not yet sealed", which is a
different and much less alarming thing than "this live boot installed no
envelope at all".

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

## Immediate risk edits and retained loss holds (#2543, 2026-09-27)

The effective loss policy is the last explicit account risk Apply in Clerk
custody; original profile and arming payloads remain historical. Apply, entry
admission, and explicit hold clearance use the same current equity-based risk
fact and account writer fence. Saving a draft has no effect. A held account
retains its original limit, policy, baseline, and session; loosening current
limits never clears that cause. Clearance requires fresh evidence satisfying
both the retained hold and current policy. Later-session clearance additionally
proves prior obligations resolved through the existing account-quiet authority.
There is no automatic rollover clearance or second real-account P&L formula.

The accepted prior-close correction in #2443 governs the real account's loss
calculation. Observed fees already reduce broker equity and must not be deducted
again. Budget free cash and pending obligations independently consume the sole
fee attribution authority; missing cash/fee coverage cannot create available
budget. Simulated equity includes its own canonical fees and marked holdings.
Risk revision, apply/clear races, unknown evidence, rollover, restart and mirror
rebuild are covered by `tests/broker/alpaca/clerk/sqlite/test_account_risk_policy.py`.

## Shared simulated cash and risk reference (#2546)

`sqlite/simulated_account.py` composes existing canonical effective fills,
`fifo_pnl.py`, `custody_fee_attribution` and the NYSE calendar. The independent
arithmetic oracle in `tests/broker/alpaca/clerk/sqlite/test_simulated_account.py`
uses a Shadow initial baseline of $1,000, BUY 2 at $100, SELL 1 at $120, and a
$130 mark. With current reference cash of $1,200, available cash is exactly
`1200 - 200 + 120 = 1120`; open P&L is $30. Current risk equity is
`1000 + 20 + 30 - .03 = 1049.97`, including accrued modelled fees before
settlement. Pending provisions remain separate cash claims; they are not
deducted twice from cash or risk equity. A later real deposit cannot change
the retained $1,000 risk baseline. Once the date settles the canonical modelled
fees ($0.01 SEC + $0.01 TAF + $0.01 CAT), a $1,500 reference gives exact cash
`1500 - 200 + 120 - .03 = 1419.97`. The next baseline is
`1000 + 20 + 30 - .03 = 1049.97`, using the exact previous scheduled close mark.
Both effective fills and fee inputs are cut off inclusively at that close;
after-hours fills belong to the next loss window. Regression cases cover an
after-close BUY with no prior-close position and an after-close SELL whose
realized gain must not leak into the previous close baseline.

Equity, the retained baseline and open P&L read canonical FIFO's exact
fields (`exact_realized_pnl`, `exact_open_pnl`) and the exact retained bar
close, never a float view normalized back into money (#2556). The
observation's `equity_usd` and `unrealized_pl_usd` carry the exact `Decimal`
(a real account's `equity_usd` stays the broker's float), and the money view
Deploy and Home draw renders both from it unchanged. A whole-cent boundary
pins it for both a Dry Run and Shadow: $1,000.01 of starting cash, BUY
0.048360857 at $100 marked at $100.3101682007 (open P&L exactly
$0.0149999999999999999) and the buy's $0.01 modelled fee give equity of
exactly $1,000.0149999999999999999, shown as $1,000.01 with open P&L $0.01,
where the float views showed $1,000.02 and $0.02.

The observation's `last_equity_usd` carries the exact retained baseline too
(#2586). Deploy's today P&L is `DayPnl.display_total_usd` for every account
(owner decision 2026-09-29, "exact for all accounts"): each recorded figure --
current equity, the prior-close baseline, each transfer amount, whether a
broker float or an exact `Decimal` -- is normalized on its own, the formula
runs in exact `Decimal`, and the result is rounded once. No type test chooses
the path, so the loss-hold clearance path's mix of exact equity and a sealed
float baseline reads the same figure. $2,000 of starting cash, the same
0.048360857-share BUY marked at $100 at the close (baseline exactly
$1,999.99) and at $100.3101682007 today shows $0.01 today -- exactly
$0.0149999999999999999 -- where the float difference of the two equities
(0.015000000000100044) showed $0.02. A real account moves only at a half-cent
tie, by at most one cent: equity 10004.015 over a 10000.70 close with
deposits of 1.10 and 2.20 is exactly $0.015, shown $0.02, where the float
formula (0.014999999998690061) showed $0.01. The loss rule is unchanged:
`total_usd`, `loss_limit_usd` and the sealed `LossHoldCause` take each figure
as its nearest float, exactly the floats the observation carried before, so
no loss decision and no sealed hold's bytes change. A regression pins the
Shadow hold's sealed facts on both the cadence and the admission path.

Cash assertions use exact Decimal equality. Existing canonical FIFO outputs
use absolute tolerance `1e-9`, relative tolerance zero; this is composition of
the repository's mathematical authorities, not a new external numerical port.
The tests also pin independent private starting cash, shared Shadow commitment
competition, rejection of real P&L contamination, missing/wrong-provider/stale
marks, session-boundary expiry, retained-baseline mirror rebuild, and restart
between private budget commitment and first projection. Real broker read grace
remains unchanged; simulated cash's coherent cutoff supersedes that grace only
for its own effective fills and modelled settlement.
