# Adversarial review: extended-hours bot-control design — 2026-09-04

## Scope and source-access limitation

This review checks the design summary supplied by the reviewer against the
current repository and current official Alpaca and IBKR documentation. It does
not recommend restoring deprecated IBKR actuation: IBKR is read/evidence-only
and Alpaca Broker V2 is the sole order-control product
(`docs/ibkr-integration-authority.md:7-15`).

The supplied Claude artifact URL returned **Page not found** on 2026-09-04, so
the exact artifact prose and any mitigation omitted from the supplied summary
could not be inspected. Conclusions below distinguish facts proved from the
summary from claims that require the artifact itself.

## Six independent verification results

### 1. “There is no bid/ask surface in the production path” — Refuted as written

There is no bot-consumable **stock execution-quote contract**, but the absolute
claim that production has no bid/ask surface is false:

- the live IBKR option-chain path converts ticker `bid`, `ask`, `bidSize`, and
  `askSize` (`PythonDataService/app/broker/ibkr/market_data.py:105-127`) and is
  called by a production SSE route
  (`PythonDataService/app/routers/broker.py:362-427`); that router is registered
  by the service (`PythonDataService/app/main.py:620`);
- that same path already opens a streaming stock ticker for the underlying
  (`PythonDataService/app/broker/ibkr/market_data.py:216-254`), but reduces it to
  one `marketPrice` value rather than exposing stock bid/ask
  (`PythonDataService/app/broker/ibkr/market_data.py:289-324`);
- the option-surface path independently does the same
  (`PythonDataService/app/broker/ibkr/surface.py:151-172` and `:207-252`);
- the capability probe also opens a stock `reqMktData` subscription but retains
  only `marketDataType`, not quote fields
  (`PythonDataService/app/broker/ibkr/capability.py:221-257`);
- Polygon has a real historical NBBO method with bid/ask/size/exchange
  (`PythonDataService/app/services/polygon_client.py:410-449`), but its only
  production caller is the reference-dataset exporter, not bot execution
  (`PythonDataService/app/services/reference_companion_service.py:250-281`);
- the data lake's quote zip is synthetic zero-spread trade-bar data and says
  real Polygon quote ingestion is deferred
  (`PythonDataService/app/data_lake/derived_quote.py:1-12` and `:38-67`);
- the stock snapshot deliberately serializes bars/change but not a last quote
  (`PythonDataService/app/services/polygon_client.py:919-929`), while the
  options snapshot does carry option bid/ask
  (`PythonDataService/app/services/polygon_client.py:705-752`).

Therefore the design does **not** need to invent IBKR top-of-book access, but it
does need a new, broker-neutral stock quote/freshness/venue contract and bot
wiring. None of the existing option, historical, database-read, or synthetic
surfaces is safe to use unchanged for an Alpaca equity order.

### 2. `daily_session_schedule.py` has no production caller — Confirmed

An exhaustive tracked-file search for the module and its two public functions
found only the module itself and
`PythonDataService/tests/services/test_daily_session_schedule.py:5`. The
implementation is a standalone policy at
`PythonDataService/app/services/daily_session_schedule.py:34-116`; no
production import, registry key, config string, or reflective lookup names it.
The only generic production module walk imports strategy algorithm modules,
not services (`PythonDataService/app/lean_sidecar/cross_runner.py:138-163`).

### 3. The two session submit helpers have no production caller — Confirmed

`evaluate_session_submit` and
`order_mechanism_sessions_from_capability` are defined at
`PythonDataService/app/services/session_authority.py:288-341`. Exact-symbol and
string-key searches found calls only in
`PythonDataService/tests/services/test_session_authority.py:251-325`. Production
modules import other names from `session_authority` (notably
`session_state_at_ms`), but no registry, dynamic dispatcher, config key, or
reflection path reaches these two functions.

This matters to implementation scope: their branch tables are tested design
material, not an already-enforced submit gate.

### 4. `rollback_blocked_exit()` is invoked on the refusal path — Refuted

Neither rollout strategy's method is called in production. The live path:

1. stages a decision and sends it to the Clerk
   (`PythonDataService/app/services/bot_trade_strategy.py:717-830`);
2. on a transient Clerk refusal, calls `_dispose_transient_exit_refusal`, which
   performs `Settlement.DISCARD`, writes a protected receipt, and waits for the
   next decision clock (`PythonDataService/app/services/bot_trade_strategy.py:924-958`);
3. on a returned rejected receipt, also just discards the stage
   (`PythonDataService/app/services/bot_trade_strategy.py:836-847`).

`_discard_evaluation` explicitly says the live adapter no longer calls either
rollback method (`PythonDataService/app/services/bot_trade_strategy.py:572-585`).
`SignalSession` maps DISCARD to `discard_signal_decision`
(`PythonDataService/app/engine/strategy/signal_program.py:213-227`), whose only
production implementation is the base no-op
(`PythonDataService/app/engine/strategy/base.py:280-288`). Repo-wide references
to `rollback_blocked_exit()` are definitions and direct unit tests only.

The **behavioral** retry claim is narrower than the wiring claim: both strategies
stage position mutation only on COMMIT, so DISCARD preserves their prior
in-position state. `deployment_validation`'s barrier remains level-true
(`PythonDataService/app/engine/strategy/algorithms/deployment_validation.py:208-239`),
and EMA preserves the terminal countdown until a later decision bar
(`PythonDataService/app/engine/strategy/algorithms/ema_crossover_signal.py:349-362`).
They can re-emit on a later bar, but no rollback method makes that happen.

### 5. A defaulted seal field preserves existing seals — Refuted

Defaults are included by the exact serialization the hash validators use:

- every seal model forbids unknown fields; `SignalClockContract` is one example
  (`PythonDataService/app/schemas/signal_program_seal.py:244-263`);
- the inner hash is over `model_dump(mode="json")`
  (`PythonDataService/app/schemas/signal_program_seal.py:300-301`);
- validation recomputes both the nested and outer hashes from fully parsed
  models (`PythonDataService/app/schemas/signal_program_seal.py:304-330`);
- neither dump uses `exclude_unset` or an equivalent presence-preserving rule.

The precise outcomes are:

1. A defaulted field added to `SignalClockContract` changes every **new** inner
   and outer seal hash. That is correct and expected.
2. Loading an **old** seal fills the absent field with its default; the model
   dump now contains bytes the stored `configured_signal_hash` never hashed, so
   nested validation fails at lines 326-327. If the nested hash were migrated,
   the old outer hash would still fail at lines 328-330.
3. A defaulted field added directly to `SealedBotProgram` can break even a new
   call to `seal_bot_program`: the builder hashes the raw caller payload before
   Pydantic supplies the default (`:334-340`), while validation dumps the parsed
   model with the default (`:328-330`). It works only if every builder passes
   the field explicitly or hashing is changed.
4. Old software reading a new seal fails on the new field because
   `extra="forbid"`; a default only helps new software parse missing input, not
   old software parse future input.

The repository already demonstrates the needed compatibility idea elsewhere:
later optional carryover fields are dumped with `exclude_none=True` specifically
to avoid invalidating old hashes
(`PythonDataService/app/services/bot_carryover.py:87-96`). The seal family needs
an explicit schema-version migration/presence rule; a default is insufficient.

### 6. Alpaca accepts GTC with `extended_hours=true` — Confirmed, with scope

For a whole-share US-equity order on Alpaca's direct Trading API, the current
official contract accepts only LIMIT with an explicit price and TIF `day` or
`gtc` when `extended_hours=true`. The detailed lifecycle and qualifications are
below. This does not mean GTC is a safe default for autonomous strategy intent,
and Alpaca's current fractional-order pages are not internally consistent
enough to generalize the conclusion beyond this repository's integer bot
quantity without an account qualification test.

IBKR has no Alpaca-style `extended_hours` field. Its order contract separately
defines `tif=GTC`, `outsideRth=true`, and `includeOvernight=true`; support still
depends on product and destination. The active system does not execute at IBKR,
so IBKR's combination is evidence about the quote/session source, not authority
for the Alpaca submission shape.

## Verification item 6: extended-hours GTC and lifecycle

### Verdict: Confirmed, with important scope and lifecycle qualifications

For Alpaca's direct Trading API (`POST /v2/orders`), an equity order with
`extended_hours=true` may use `gtc` when it is a **limit** order with an explicit
limit price. Alpaca's current create-order reference says exactly that and was
updated three months before this review. The order guide's extended-hours
matrix also marks only LIMIT+DAY and LIMIT+GTC as valid. [Alpaca Create an
Order](https://docs.alpaca.markets/us/reference/postorder), [Alpaca Orders at
Alpaca](https://docs.alpaca.markets/us/docs/orders-at-alpaca)

That confirmation is not permission to treat GTC like a longer DAY order:

- A `day` order submitted during the overnight session remains eligible through
  the next pre-market, regular, and after-hours sessions, then is canceled at
  20:00 ET if unfilled.
- A direct-Trading-API `gtc` order can persist for 90 days and is canceled for a
  corporate-action event. It can therefore become eligible again in later
  sessions and on later days.
- Alpaca defines `done_for_day` as nonterminal: the order is done executing for
  that day but may receive updates again on the next trading day.
- The direct create-order schema says fractional quantity is only supported for
  market/DAY combinations. The repository correctly refuses fractional GTC at
  `PythonDataService/app/broker/contract/models.py:108-109`; do not generalize
  the whole-share GTC conclusion to arbitrary fractional orders without a
  broker qualification test.

[Alpaca 24/5 Trading for Trading API](https://docs.alpaca.markets/us/docs/245-trading-for-trading-api),
[Alpaca order lifecycle and time in force](https://docs.alpaca.markets/us/docs/orders-at-alpaca)

The repository does use that direct Trading API: `AlpacaBroker.submit` maps a
leg and sends it through the client at
`PythonDataService/app/broker/alpaca/broker.py:163-175`, and the client documents
its write as `POST /v2/orders` at
`PythonDataService/app/broker/alpaca/client.py:403-418`.

### Current contract does not preserve extended-hours eligibility

The current outbound adapter emits symbol, quantity, side, type, TIF, client
order ID, and optional limit price, but no `extended_hours` field
(`PythonDataService/app/broker/alpaca/adapter.py:276-299`). The inbound
`BrokerOrder` also has no such field
(`PythonDataService/app/broker/contract/models.py:189-214`), and
`from_alpaca_order` drops the vendor response's `extended_hours` value
(`PythonDataService/app/broker/alpaca/adapter.py:302-334`).

That omission creates a concrete panel/gate disagreement if the design adds
only limit price and TIF:

1. The deployment panel says an intent is extended-hours eligible.
2. The adapter submits a LIMIT/GTC order without `extended_hours=true`.
3. Alpaca accepts it as an ordinary order but queues it for the next eligible
   regular session instead of executing now.
4. The returned `BrokerOrder` cannot prove whether the vendor accepted the
   extended-hours instruction because the field is discarded.

Alpaca explicitly says orders not eligible for extended hours that are
submitted after 16:00 ET are queued for release the next trading day. [Alpaca
Orders Submitted Outside Eligible Trading Hours](https://docs.alpaca.markets/us/docs/orders-at-alpaca)

### Current lifecycle classification is incomplete for GTC

The Alpaca adapter recognizes a `done_for_day` event
(`PythonDataService/app/broker/alpaca/adapter.py:337-350`), but the Account
Clerk's canonical working-state set omits it
(`PythonDataService/app/broker/alpaca/clerk/sqlite/reads.py:112-118`). The
terminal-state set also correctly omits it
(`PythonDataService/app/broker/alpaca/clerk/sqlite/order_projection.py:21-23`).
Therefore a dormant GTC order is neither terminal nor classified as a broker
order that can still act. This is a concrete gap in any design that adds GTC
without first extending recovery, cancellation, panel projection, and exposure
admission around `done_for_day`.

### Concrete GTC failure scenario — design defect if no intent expiry exists

**State and input:** Monday 18:00 ET, a strategy emits ENTER. The execution rule
places an unmarketable extended-hours LIMIT/GTC buy. The signal disappears on
Tuesday, but the strategy has no position and emits no EXIT. The order becomes
`done_for_day`, then is eligible again on a later day.

**Wrong outcome:** On Thursday the price falls through the old limit and the
order opens exposure long after the strategy's entry condition ceased to be
true. The order behaved exactly as the broker documents; the design lost the
semantic lifetime of the strategy intent.

**Required control:** Every autonomous entry must carry a durable intent
deadline or explicit cancel-on-superseding-decision/session rule. GTC should not
be offered for an autonomous entry merely because Alpaca accepts the TIF.

## IBKR extended and overnight quote/routing behavior

### Confirmed vendor facts

- `outsideRth=true` permits an eligible IBKR order to trigger or fill outside
  regular hours. It does not by itself include the separate overnight venue.
- IBKR's current overnight order API requires `includeOvernight=true`.
  Overnight-only routing uses `Contract.exchange="OVERNIGHT"`; Overnight+DAY
  uses `exchange="SMART"`.
- IBKR explicitly says overnight market-data requests must use the same routing
  as the intended overnight venue because OVERNIGHT does not coincide with the
  regular SMART-routed data and prices may differ.

[IBKR Order reference](https://www.interactivebrokers.com/docs/tws-api/ref/order),
[IBKR Trading the Overnight Session](https://www.interactivebrokers.com/docs/tws-api/doc/orders/place-order/trading-the-overnight-session),
[IBKR API Overnight Trading](https://ibkrcampus.com/campus/ibkr-quant-news/api-overnight-trading/)

The current repository does not have that venue alignment. The shared
underlying qualifier hard-codes `SMART` and USD
(`PythonDataService/app/broker/ibkr/contracts.py:66-83`). The Alpaca capability
describes its data feed as `iex`
(`PythonDataService/app/broker/alpaca/broker.py:38-49`), while Alpaca's current
overnight execution and market data are provided by the independent BOATS ATS
and the `boats`/`overnight` data feeds. [Alpaca 24/5 Trading for Trading
API](https://docs.alpaca.markets/us/docs/245-trading-for-trading-api)

The existing IBKR options surface proves that the repository can consume bid
and ask values, but it is not an equity execution-quote contract: it subscribes
the SMART-routed underlying and option contracts
(`PythonDataService/app/broker/ibkr/market_data.py:216-277`), then publishes
bid/ask fields on `IbkrOptionQuote`
(`PythonDataService/app/broker/ibkr/models.py:299-332`). It must not be silently
reused as evidence of the price Alpaca can execute.

There is also a freshness trap in that converter: when `Ticker.time` is absent
or unusable it stamps the snapshot with the current process wall clock
(`PythonDataService/app/broker/ibkr/market_data.py:152-167`). That makes
"observed now" indistinguishable from "the price changed now." A protected
limit gate must preserve the vendor tick timestamp and reject unknown-age bid or
ask values; assembling a snapshot now is not evidence that the quote is fresh.

## Ranked findings from this pass

### Critical — design wrong: `use_rth` is not merely an execution permission

The proposal says strategy logic alone decides when a bot acts, but its chosen
sealed switch already controls which observations the strategy is allowed to
see. `SignalClockContract.use_rth` is part of the strategy clock
(`PythonDataService/app/schemas/signal_program_seal.py:244-263`). The live
adapter drops every non-RTH bar when it is true and admits every session phase
when it is false (`PythonDataService/app/services/bot_trade_strategy.py:299-305`),
then sends every admitted bar into the strategy and its consolidators
(`PythonDataService/app/services/bot_trade_strategy.py:408-413`).

**Failure scenario:** a strategy owns an 08:00 ET entry rule, but the sealed bot
has `use_rth=true`. The 08:00 bar is discarded before the strategy can evaluate
it, so the execution layer has overridden the strategy's timing. Conversely,
turning `use_rth=false` changes EMA inputs and later RTH signals. A Boolean also
cannot distinguish pre/post participation from Alpaca's separate overnight
session. Reusing the field may be a migration convenience, but describing it as
an execution-only permission is false.

### Critical — design wrong: a refused EXIT is not a durable exit obligation

The refusal path discards the staged evaluation and explicitly waits for the
next decision clock
(`PythonDataService/app/services/bot_trade_strategy.py:924-958`). The existing
stuck-exit watchdog starts from an accepted `EXIT_NOT_FLAT` uncertainty episode,
not a pre-custody refused exit
(`PythonDataService/app/broker/alpaca/clerk/sqlite/exit_watchdog.py:47-68` and
`:142-159`).

**Failure scenario:** a long bot emits EXIT at 19:59; the quote freshness gate
refuses it; the bar stream then stalls or the session stops producing prints.
There is no next bar, so neither rollout strategy re-emits. The bot remains long
despite having decided to exit, and no Clerk-owned retry timer exists for this
case.

The stronger supplied claim that this is completely invisible could not be
proved. A protected decision receipt is written
(`PythonDataService/app/broker/alpaca/clerk/sqlite/runtime.py:1204-1234`), and
Market Pulse can show stale/missing data with attention
(`PythonDataService/app/services/broker_v2_panel/market_pulse.py:129-151`). But
the main transaction rail classifies every non-quarantine latest decision,
including `blocked`, as SIGNAL `satisfied`
(`PythonDataService/app/services/broker_v2_panel/station_derivation.py:174-179`),
and the roster's attention flag is based on execution coverage rather than a
blocked pre-custody decision
(`PythonDataService/app/services/broker_v2_panel/catalog_projection_service.py:62-76`).
The evidence exists, but the primary summary can understate the emergency.

### Critical — design wrong: an IBKR quote cannot be the sole price authority for an Alpaca fill

**Failure scenario:** At 21:30 ET after material news, the IBKR OVERNIGHT book
is 50.00/50.05 while Alpaca's BOATS book is 44.00/44.10. A bot holding 1,000
shares emits EXIT. Its "protected" sell rule subtracts 20 bps from the fresh,
tight IBKR bid and submits a 49.90 limit to Alpaca. Alpaca cannot fill it; the
position remains exposed while BOATS falls further.

Both quote freshness and the IBKR spread check pass. They cannot detect a
venue mismatch. IBKR's official risk disclosure warns that prices displayed by
concurrently operating extended-hours systems can differ and that liquidity is
lower and spreads wider. [IBKR Risks of After-Hours
Trading](https://ndcdyn.interactivebrokers.com/Universal/servlet/Registration_v2.formSampleView?formdb=3261)

The opposite mismatch can lose money immediately: if the IBKR offer is much
higher than the Alpaca book, an aggressive buy limit derived from IBKR can
authorize Alpaca to sweep a thin BOATS book through many price levels up to the
limit. A limit caps the worst price; it does not cap market impact or guarantee
that the reference venue and execution venue agree.

**Would the summarized mitigations catch it?** No. Quote age, positive spread,
maximum IBKR spread, and a fixed offset all inspect only the reference book.
The submit gate needs a fresh execution-side Alpaca `boats`/`overnight` quote,
an explicit cross-venue divergence limit, and a quantity/notional or visible-
depth cap. If execution-side evidence is unavailable, fail the individual
order closed and surface an actionable exit alert.

### High — design wrong unless corrected: GTC can execute a stale strategy intent days later

The concrete Monday-to-Thursday scenario above is broker-conformant and can
create unwanted exposure. A sealed price rule is not a lifetime rule. Add a
durable intent deadline and cancellation state machine before allowing GTC for
autonomous entries.

### High — implementation evidence gap: `done_for_day` is not classified as working

Alpaca's lifecycle says it can reactivate; the current canonical working-state
set says it is not working. This routes differently from the GTC design defect:
even if the intended GTC semantics are accepted, the repository evidence and
recovery projections must be corrected and regression-tested first.

### High — design claim refuted: regular-hours behavior cannot stay byte-identical for EMA when extended bars are consumed

For `use_rth=False`, the live adapter admits every session phase
(`PythonDataService/app/services/bot_trade_strategy.py:299-305`) and sends each
admitted minute bar into the strategy and its consolidators
(`PythonDataService/app/services/bot_trade_strategy.py:408-413`).
`ema_crossover_signal` registers a 15-minute consolidator
(`PythonDataService/app/engine/strategy/algorithms/ema_crossover_signal.py:219-224`)
and recursively updates EMA5, EMA10, and RSI14 on every consolidated bar
(`PythonDataService/app/engine/strategy/algorithms/ema_crossover_signal.py:292-307`).
Premarket inputs therefore alter the indicator state and crossover history seen
after 09:30. That is a strategy-semantic change, not just a different execution
mechanism, so RTH decision traces cannot be byte-identical.

Sparse extended-hours data makes this harder: the consolidator emits a bucket
after elapsed wall-clock period without requiring 15 constituent one-minute
bars (`PythonDataService/app/engine/consolidators/trade_bar_consolidator.py:84-130`).
A single print can therefore become the close of a 15-minute input. This must be
explicitly qualified rather than assumed equivalent to dense RTH bars.

### High — evidence wrong: validated parameters do not prove extended-session coverage

Corpus coverage is derived solely from the symbol and registered parameter
values (`PythonDataService/app/services/signal_program_admission.py:668-672`),
then stamped `COVERED` from that Boolean
(`PythonDataService/app/services/signal_program_admission.py:448-475`). It does
not compare `clock.use_rth` or the sessions present in the validation corpus.
The committed manifest itself describes `deployment_validation` as internal
replay/plumbing evidence only
(`PythonDataService/app/data/strategy_validation_manifest.json:5-25`), while
EMA's recorded coverage is its existing SPY/QQQ W3mo/W6mo cells
(`PythonDataService/app/engine/strategy/registry.py:445-477`).

**Failure scenario:** an operator deploys the same symbol and numerical
parameters as a qualified RTH run but flips `use_rth=false`. Admission still
reports `COVERED`, although premarket input changes the EMA state and no
extended-session trace has been qualified. The design can therefore present
RTH evidence as proof for an extended-hours strategy.

### High — data-loss and money-risk gap: extended bots have no continuity contract

The continuity-policy factory deliberately returns no policy for
`use_rth=false`, with reason `all_session_not_supported`
(`PythonDataService/app/services/feed_continuity_policy.py:114-140`). ADR 0053
records the consequence: the legacy reconnection path creates a fresh minute
assembler and can retain a damaged minute after a stall; `use_rth=false` runs
are explicitly among the excluded populations
(`docs/architecture/adrs/0053-feed-continuity-same-run-recovery.md:91` and
`:135`). Full service boot recovery also says nothing is automatically
restarted (`PythonDataService/app/services/bot_runner.py:1199-1205`) and projects
interrupted runs to desired `STOPPED`
(`PythonDataService/app/services/bot_boot_recovery.py:284-310`).

**Failure scenario:** an extended-hours EMA bot is long when IBKR disconnects
for 40 seconds around a minute boundary. It has neither a proven extended
decision clock nor same-run substitution/continuity evidence; after a process
restart it is stopped, not resumed. Missed or partial bars can change the
15-minute EMA decision stream, and the held exposure is unmanaged until an
operator intervenes.

I could not prove a duplicate order in the current Clerk. It records accepted
effects before broker contact and shields the task
(`PythonDataService/app/broker/alpaca/clerk/sqlite/runtime.py:543-586`), and an
active EXIT fences later EXIT evaluations
(`PythonDataService/app/broker/alpaca/clerk/sqlite/runtime.py:764-797`). Whether
the proposed price/quote fields participate in the same durable identity is
**Could not determine** without the artifact and implementation.

### Medium — current panel and run-admission gate disagree outside RTH

Market Pulse changes raw Alpaca `CLOSED` to `TRADABLE` for a `use_rth=false`
bot when the extended phase is proven
(`PythonDataService/app/services/broker_v2_panel/market_pulse.py:58-76`). Start
and Resume, however, pass raw market-liveness evidence
(`PythonDataService/app/services/bot_start_admission.py:487-510` and
`bot_resume_admission.py:307-322`) to a policy that blocks every non-dry run
unless the raw state is `TRADABLE`
(`PythonDataService/app/services/run_admission.py:325-339`).

**Failure scenario:** at 18:00 ET, fresh extended bars and a capability snapshot
make the panel say `TRADABLE` / “Market data live.” After a restart the operator
presses Resume; run admission answers `MARKET_LIVENESS_CLOSED`. The order-level
ENTER gate itself was not broken: it and the live strategy path use the same
`liveness_blocks_entry` predicate
(`PythonDataService/app/services/market_liveness.py:213-237` and
`PythonDataService/app/broker/alpaca/clerk/sqlite/runtime.py:720-749`). The
disagreement is specifically panel versus Start/Resume admission.

### Medium — rollout evidence wrong: `deployment_validation` cannot positively canary an extended-hours entry

`deployment_validation` anchors its decision window to NYSE open + 15 minutes
through close - 15 minutes
(`PythonDataService/app/engine/strategy/algorithms/deployment_validation.py:48-59`).
Outside that window it returns HOLD
(`PythonDataService/app/engine/strategy/algorithms/deployment_validation.py:241-254`).
It therefore cannot emit an extended-hours ENTER and proves nothing about
protected quote pricing, `extended_hours=true`, or off-hours fill lifecycle.
An abnormal position still open after the RTH flatten barrier could cause a
later EXIT, but a nominal successful run will not. Promoting it first can
therefore produce a green rollout with zero exercise of the new mechanism.

## Claims this pass could not determine

- Whether the inaccessible artifact already requires an Alpaca execution-side
  quote or explicitly limits IBKR to secondary evidence.
- Whether it defines a durable intent deadline, cancel/replace identity,
  `done_for_day` recovery, and terminal proof for multi-day GTC orders.
- Whether its panel projection is sourced from the exact same pre-submit fact
  object or merely recomputes equivalent-looking fields.

Those are **Could not determine**, not findings against the design. The failures
above apply when the corresponding control is absent, as it is from the supplied
summary and the current repository.

## Single highest-leverage change from this pass

Turn every strategy EXIT decision into a durable Account Clerk obligation
*before* quote/session checks, with an expiry, an independent wall-clock retry
and escalation schedule, restart recovery, and an idempotent cancel/replace
identity. The obligation must not disappear because no later strategy bar
arrives.

Make each retry use one broker-return-verifiable execution contract containing:

- Alpaca `extended_hours` eligibility and the exact TIF;
- an intent expiry/cancellation policy;
- a fresh execution-side Alpaca venue/feed quote;
- the optional IBKR quote as secondary cross-venue evidence;
- cross-venue divergence, spread, age, and size/depth limits; and
- all multi-day lifecycle states, including `done_for_day`.

Derive both the panel preview and the final submit decision from that same
contract. This closes the most dangerous path—an acknowledged exit that never
reaches custody—while also addressing cross-venue pricing and stale GTC intent
without moving order authority back to IBKR.

## Claim least safe to sign off

"A fresh IBKR quote is sufficient to construct a protected price for an Alpaca
extended-hours fill." Official vendor documentation directly warns that the
relevant overnight books are independent and their prices may differ. Freshness
proves only age; it does not prove venue alignment or executable liquidity.
