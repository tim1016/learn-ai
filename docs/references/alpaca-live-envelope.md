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
   `min(ALPACA_LIVE_LOSS_FRACTION × last_equity, ALPACA_LIVE_LOSS_USD)`, the
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
  runtime. `PythonDataService/app/main.py` builds `LiveEnvelopeValues` from
  the `ALPACA_LIVE_*` environment once at startup, only when the configured
  mode is not paper, and passes them down through `select_active_clerk_runtime`.
- Shadow rehearsal: `PythonDataService/app/broker/alpaca/clerk/shadow_authority.py::select_shadow_clerk_runtime`
  is the only consumer of a non-`None` `live_envelope_values` today —
  `real_live` custody stays unconstructible until slice 7, so every live-mode
  boot rehearses the envelope under the Shadow Account Authority instead. It
  composes `LiveEnvelopeGate(values=live_envelope_values,
  custody_is_simulated=True)` and passes the *live* account's own read port
  as `envelope_read=read` — not the shadow composite — so the sync observes
  the live account directly. A live-mode boot with an incomplete envelope
  (`LiveEnvelopeIncomplete`) is not aborted: the shadow authority simply
  declines to install one and refuses `LIVE_ENVELOPE_MISSING` at startup, so
  every unrelated data-plane surface stays up.

## The facts

- **Cash observation.** `LiveEnvelopeSync.observe`
  (`PythonDataService/app/broker/alpaca/clerk/sqlite/live_envelope_sync.py`)
  reads `account.cash` every tick. Under simulated custody
  (`custody_is_simulated=True`, always true today) it subtracts
  `account_net_cash_spent_usd()` — what the Clerk's own synthesized fills
  would have spent — to get `cash_available_usd`; under real custody the
  broker's own cash already reflects it.
- **Reservations, fills-aware.** `PythonDataService/app/broker/alpaca/clerk/sqlite/envelope_reservations.py`
  prices the part of an accepted ENTER the latest observation cannot see. A
  working *or filled* order reserves its quantity minus whatever was
  recorded before the observation; a dead order (canceled/expired/rejected/
  replaced) reserves only its post-observation fills, because its unrecorded
  remainder is cancelled quantity, never cash. `reserved_cash_usd(observed_at_ms=...)`
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
| `LIVE_ENVELOPE_UNOBSERVED` | No observation is fresh (older than `OBSERVATION_MAX_AGE_MS = 45_000` ms — three missed 15 s sync ticks), or a market ENTER has no decision-bar price, or the day-P&L/`last_equity` fact was unjudgeable so the sync withdrew its last observation | ENTER refusal |
| `LIVE_ENVELOPE_CASH_EXCEEDED` | The ENTER's notional plus reserved notional would exceed cash available | ENTER refusal |
| `LIVE_ENVELOPE_DISAGREEMENT` | The sealed envelope (slice 6 arming) disagrees with the current `ALPACA_LIVE_*` environment values | ENTER refusal |
| `LIVE_ENVELOPE_LOSS_HOLD` | The account-wide loss hold stands — checked earlier, by `require_admission` | ENTER refusal |
| `LIVE_ENVELOPE_MISSING` | At least one `ALPACA_LIVE_*` value is absent for a live-mode boot | startup refusal of the Shadow Account Authority — never an ENTER refusal |

All four ENTER-time codes are transient at the runner: none disarms an
instance or stops a bot, and the same decision can be re-admitted once the
sync republishes a judgeable observation.

## Loss hold

`LiveEnvelopeSync.tick`
(`PythonDataService/app/broker/alpaca/clerk/sqlite/live_envelope_sync.py`) is
the only writer that raises the hold, via `raise_account_hold(...,
reason_code=LIVE_ENVELOPE_LOSS_HOLD_REASON_CODE)`; it never releases one it
raised — a standing hold is left alone (`_hold_stands`) so its cause keeps
naming the breach it was raised on, not a later tick's numbers.
`require_admission`
(`PythonDataService/app/broker/alpaca/clerk/sqlite/uncertainty.py`) blocks
`NEW_EXPOSURE` under this reason code exactly like any other account hold,
but explicitly authorizes every `REDUCE` under it without a per-symbol proof
— the hold is account-scoped and says nothing about any one position, so
each program keeps managing its own EXIT. The live verdict (below) projects
the hold as `loss_hold: "held" | "clear"`, never as a third custody state.

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
- **External orders make the fact unknown.** Any order the Clerk did not
  accept but observes on the account today withdraws day P&L to unknown for
  the rest of that day; nothing after that is inferred back to zero.
- **No Frontend button yet.** `Frontend/src/app/shell/alpaca-live-banner.component.ts`
  (Task 10) renders a `· loss hold` chip on the banner when
  `loss_hold === 'held'`; it carries no clear action. Clearing the hold is
  the `curl` above, operator-run, until one is built.

## Decision record

[ADR 0059](../architecture/adrs/0059-real-money-live-behind-shadow-gate-arming-and-cash-bound-envelope.md)
Decision 4 (the risk envelope); owner rulings 2026-09-08 fix the reservation
shape (a working *or filled* order reserves; a dead order reserves only its
post-observation fills), the simulated-custody cash subtraction, unjudgeable-tick
withdrawal on a missing `last_equity` or an external order, the sync as the
loss hold's sole raiser that never releases it, and every ENTER-time refusal
staying transient at the runner.
