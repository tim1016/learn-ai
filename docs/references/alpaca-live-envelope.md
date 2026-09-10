# Alpaca live envelope — cash bound, day P&L, and the loss hold

**Status:** canonical for ADR 0059 slice 5 (2026-09-08). Lineage: live.

## What it is

Two rules, evaluated in `accept_enter` as a sibling of `require_admission` —
inside the custody fence, before `ENTER_ACCEPTED`, so no broker contact
precedes either one. Neither rule ever runs inside a Signal Program
(broker-neutral, ADR 0042) and neither is composed by the Frontend (ADR 0011
§7). EXIT is never subject to either rule.

1. **Cash bound.** An ENTER is admitted only if its own notional plus every
   working ENTER's unfilled notional does not exceed broker-observed cash.
   The bound is `cash`, not `buying_power`, so a cash account and a Reg T
   margin account are admitted identically and an Intraday Margin Deficit is
   impossible by construction. A refusal (`LIVE_ENVELOPE_CASH_EXCEEDED`)
   changes no other state.
2. **Daily loss hold.** `day_pnl` is account-wide: every custody subject's
   realized session P&L, plus broker-observed unrealized P&L over every open
   position, net of every journaled fee. When it breaches
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
- Sealing: `PythonDataService/app/broker/alpaca/clerk/sqlite/live_envelope_sync.py::LiveEnvelopeSync._refresh_sealed_envelope`
  re-reads the account's arming ledger on every tick and assigns
  `LiveEnvelopeGate.sealed` from the newest arming record's own
  `envelope_values` (ADR 0059 slice 6). Until then `sealed` was permanently
  `None`, so `envelope_agreement` could only answer `unsealed` and
  `LIVE_ENVELOPE_DISAGREEMENT` was unreachable. It is now the live rule: an
  envelope edit that becomes effective after an arming refuses every ENTER
  until a re-arm. See
  [alpaca-live-arming](alpaca-live-arming.md).
  Read the sealed record precisely: the runtime computes the loss limit from
  the *configured* values (`self.envelope.values`) and uses the sealed record
  as an equality gate, not as the source of the numbers. The two are equivalent
  while a disagreement refuses every ENTER — which it does — but the sealed
  numbers do not themselves bound the money, and a future change that let a
  disagreement admit anything would have to move the bound as well.

## The facts

- **Cash observation.** `LiveEnvelopeSync.observe`
  (`PythonDataService/app/broker/alpaca/clerk/sqlite/live_envelope_sync.py`)
  reads `account.cash` every tick. Under simulated custody
  (`custody_is_simulated=True`, true under shadow; false on the live
  authority, whose broker cash already reflects the Clerk's own fills) it
  subtracts `account_net_cash_spent_usd()` — what the Clerk's own synthesized
  fills would have spent — to get `cash_available_usd`; under real custody the
  broker's own cash already reflects it.
- **Reservations, fills-aware.** `PythonDataService/app/broker/alpaca/clerk/sqlite/envelope_reservations.py`
  prices the part of an accepted ENTER the latest observation cannot see. A
  working *or filled* order reserves its quantity minus whatever was
  recorded before the observation; a dead order (canceled/expired/rejected/
  replaced) reserves only its post-observation fills, because its unrecorded
  remainder is cancelled quantity, never cash. Corrections fold at their
  restated size: only the head of each correction chain counts (resolved
  through `economic_projection.py::EFFECTIVE_FILL_LINEAGE_CTE`), dated by the
  *root* execution's `recorded_at_ms`, because the broker's cash at that
  instant already reflected the true quantity however late the Clerk recorded
  the restatement. `reserved_cash_usd(observed_at_ms=...)`
  sums this across every accepted ENTER, and `cash_bound_admits`
  (`PythonDataService/app/broker/alpaca/clerk/live_envelope.py`) checks
  `notional + reserved <= cash_available`.
- **Day P&L, the unknown rule.** `PythonDataService/app/broker/alpaca/clerk/sqlite/day_pnl.py::day_pnl_at`
  composes realized FIFO P&L (via `SqliteEconomicProjectionReader.account_pnl_attribution`)
  less reported fees, plus the same tick's broker-observed `unrealized_pl_usd`.
  `DayPnl.known` is `False` — never zero — whenever an external order was
  observed on the account today, because its realized P&L was never
  journaled.
- **`last_equity`.** The loss limit is `min(loss_fraction × last_equity,
  loss_usd)`. A broker snapshot with no `last_equity` leaves nothing to
  compute the limit against, which is unjudgeable in the same way an unknown
  day P&L is.

## Refusals

| Code | Fires when | Scope |
|---|---|---|
| `LIVE_ENVELOPE_UNOBSERVED` | No observation is fresh (older than `OBSERVATION_MAX_AGE_MS = 45_000` ms — three missed 15 s sync ticks), or a market ENTER has no decision-bar price, or the sync withdrew its last observation because the reading was unjudgeable (day P&L / `last_equity`) **or breached** | ENTER refusal |
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
than trusting the caller. Three outcomes:

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

- **Market slippage above the decision close.** A market ENTER is bound
  against `reference_price` — the decision bar's close — not its eventual
  fill price. A fill above that close spends more cash than the bound
  admitted for.
- **Unrealized P&L under shadow is the live account's.** The sync's
  `envelope_read` is the live read port, not the shadow book, so
  `unrealized_pl_usd` (and the cash and position facts it derives from)
  describe the live account net of what the Clerk's own synthesized fills
  would have spent — never the synthesized positions' own marks.
- **A live external order is invisible to the shadow rehearsal.** Under shadow
  the sweep reconciles the synthesized book (`ShadowAccountReadPort.list_orders`),
  so an order a human works on the *live* account today is never recorded as an
  external order: R5 does not withdraw day P&L to unknown for it, and its
  realized P&L is absent from the rehearsal's day P&L. Under live composition
  the sweep reads the live account and R5 applies unchanged. Whether the shadow
  sync should consult the live port's orders instead — which would refuse every
  rehearsal ENTER for the whole day a human trades that account — is an owner
  decision deferred to the arming ceremony (slice 6).
- **External orders make the fact unknown.** Any order the Clerk did not
  accept but observes on the account today withdraws day P&L to unknown for
  the rest of that day; nothing after that is inferred back to zero.
- **A mirror rebuild loses the reservations of still-working ENTERs.** The
  `envelope_reservations` side table is product evidence *outside* the custody
  hash chain (plan R9), which is what makes it safe to write inside
  `ENTER_ACCEPTED`'s transaction — and also means the mirror does not carry it
  and a rebuild ceremony does not restore it. After a rebuild, `reserved` reads
  0 while accepted-but-unfilled ENTERs are still working, so the cash bound
  briefly admits against cash those ENTERs have already claimed. The window is
  bounded by one sync cadence plus the life of those working orders: the next
  15 s tick re-observes cash, and any fill recorded by then is already
  subtracted through `account_net_cash_spent_usd`. An operator running a
  rebuild while entries are working should expect it rather than discover it.
- **No Frontend button yet.** `Frontend/src/app/shell/alpaca-live-banner.component.ts`
  (Task 10) renders a `· loss hold` chip on the banner when
  `loss_hold === 'held'`; it carries no clear action. Clearing the hold is
  the `curl` above, operator-run, until one is built.

## Decision record

[ADR 0059](../architecture/adrs/0059-real-money-live-behind-shadow-gate-arming-and-cash-bound-envelope.md)
Decision 4 (the risk envelope); owner rulings 2026-09-08 fix the reservation
shape (a working *or filled* order reserves; a dead order reserves only its
post-observation fills), the simulated-custody cash subtraction, withdrawal on
a missing `last_equity`, an external order or a breach (R-A′), the sync as the
loss hold's sole raiser that never releases it, and every ENTER-time refusal
staying transient at the runner.
