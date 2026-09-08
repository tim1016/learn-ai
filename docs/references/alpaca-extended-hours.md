# Alpaca extended hours — the window, the anchor, the clock, and unfilled orders

**Status:** canonical for ADR 0059 slice 3 (2026-09-08). Lineage: live.

## What was built

- `BrokerOrderLeg.extended_hours` (contract validator: LIMIT + DAY|GTC) and `BrokerOrder.extended_hours`; `to_alpaca_order_request` forwards the flag on every order body, `from_alpaca_order` ingests it.
- `BrokerCapabilities.extended_hours_window` — the declared session as ET minutes past midnight; `ALPACA_EXTENDED_HOURS_WINDOW` = 04:00–20:00 ET.
- `app/services/session_authority.py` resolves PRE / RTH / POST from the calendar's regular session and the declared window (source `broker_declared_window`, `extended_phase_proven=True`).
- `app/services/decision_clock.py::extended_trigger_instants` and `app/services/decision_session.py::RunDecisionSession` — one run-scoped value object carrying the run's session kind and declared window. It is resolved once at each run boundary (`run_trade_bot`, `run_dry_run_bot`, `RunReplayProofService._compute`, `shape_program_leg`), and `resolve()` returning `None` is the single place "extended run, no declared window" is judged; every boundary refuses loudly on it. It owns the bar filter, the force-flush instant and the clock binding, and it is what `ContinuityPolicy` carries — so the feed's continuity floor refuses an unprovable minute for exactly the minutes the run decides on.
- `app/broker/alpaca/marketable_limit.py::marketable_limit_price` and `app/broker/alpaca/clerk/program_leg.py::shape_program_leg`; the reducing order's shape is durable in `EXIT_REDUCING_ORDER_CREATED` facts.
- `app/broker/alpaca/clerk/fill_models.py` — the two synthetic fill models: `immediate_fill_price` (what the `sim:` world can do — fill at the decision bar's close or cancel on the spot) and `limit_touch_fill` (the resting model for the slice-4 shadow port). The `sim:` world reports limit legs honestly and calls the first of those rather than deciding marketability itself.
- `app/schemas/run_admission.py::ExtendedHoursAdmissionFact` on the Start/Resume admission surface, and the `RunReplayUnavailableError` refusal that keeps an extended run's replay proof honest when no window is declared.

## Vendor facts (pinned 2026-09-08)

| Fact | Value | Source |
|---|---|---|
| Pre-market / after-hours | 04:00–09:30 ET / 16:00–20:00 ET, Mon–Fri | Alpaca "Orders at Alpaca" § Extended Hours Trading, `https://docs.alpaca.markets/docs/orders-at-alpaca` |
| Overnight | 20:00–04:00 ET, Sun–Fri (Blue Ocean ATS) — **not a decision phase in slice 3** | same |
| Order shape | `type=limit`, `time_in_force ∈ {day, gtc}`, `extended_hours=true`; anything else is rejected | same |
| Day order lifetime | eligible only on its day; unfilled after the close is cancelled; extended eligibility lets it execute in supported extended hours | same, Time in Force |
| Market order after 16:00 | queued for release the next trading day | same |
| Early-close days | not documented by Alpaca — the declared window applies unchanged; a venue cancel folds through D5.4 | plan ruling R2 |
| `/v2/calendar` `session_open` / `session_close` | legacy `0700` / `1900`, never explained (forum thread 2400, 2020–2023) — rejected as a source | `https://forum.alpaca.markets/t/calendar-what-are-session-open-and-session-close/2400` |

## The anchor

`buy: ceil_tick(close × (1 + entry_bps/10⁴))`, `sell: floor_tick(close × (1 − exit_bps/10⁴))`; tick 0.01 at or above $1, 0.0001 below, chosen by the pre-quantisation value so a sub-dollar close crossing $1 rounds to $1.01 rather than to a sub-penny tick the vendor would reject. Rounding is in the marketable direction. The allowances are `ALPACA_LIVE_XH_ENTRY_BPS` / `ALPACA_LIVE_XH_EXIT_BPS`, required for any `use_rth=False` run; unset → Start and Resume refuse `EXTENDED_HOURS_ALLOWANCE_UNSET`. Each is bounded `0 <= bps < 10_000` — a 100 % allowance is not a price — and an anchor that still quantises to zero or below refuses the leg (`EXTENDED_ANCHOR_UNPRICEABLE`) rather than escaping as a contract-validation error.

## The clock

`extended` buckets run from `floor_et(04:00)` to 20:00 ET; the regular close is an ordinary bucket boundary; the day's last bucket is force-flushed at 20:00 (the runner's flush instant is `RunDecisionSession.close_ms`). A decision at exactly 16:00 is POST (sessions are half-open); a decision at 20:00 is CLOSED and refused (`SESSION_CLOSED_AT_DECISION`).

**The day's last bucket is therefore always refused.** The force-flush fires *at* the declared close, and the decision instant is then CLOSED under the half-open rule, so the 19:45–20:00 bucket produces a `SESSION_CLOSED_AT_DECISION` receipt every day rather than an order. For a bucket that decided ENTER this is harmless — no exposure is created. For one that decided EXIT it is not: the position carries overnight with a rejected receipt explaining why, and the program's next chance to reduce is the following session's first extended bucket at 04:16. Sizing the decision timeframe so the day's last bucket is not one a program is likely to exit on is the operator's lever until a later slice gives the final bucket a decision instant inside the session.

## Admission

Start and Resume each carry an `ExtendedHoursAdmissionFact` (`app/schemas/run_admission.py`) whose state is one of `NOT_REQUESTED | READY | UNSUPPORTED | ALLOWANCE_UNSET`. Both failing states refuse in **every** mode — trade, dry-run and log-only alike — and there is no mode carve-out:

- `UNSUPPORTED` — the active broker authority declares no extended window — refuses with `EXTENDED_HOURS_UNSUPPORTED`.
- `ALLOWANCE_UNSET` — `ALPACA_LIVE_XH_ENTRY_BPS` / `ALPACA_LIVE_XH_EXIT_BPS` are not both set — refuses with `EXTENDED_HOURS_ALLOWANCE_UNSET`. A Dry Run runs on the synthetic authority built with the same `ProgramLegPolicy` and routes every intent through `shape_program_leg`, which refuses the same condition per decision; admitting such a run would start a bot that then rejects every extended decision it makes.

`app/services/run_admission.py` maps `ExtendedHoursAdmissionFact.state` to the named `LegRefusal` values exported by `app/broker/alpaca/clerk/program_leg.py`, so the wording an operator reads at the gate is the wording on the receipt.

## Leg refusals

`app/broker/alpaca/clerk/program_leg.py::shape_program_leg` never guesses a leg shape; outside the regular session it raises one of five typed refusals, each a rejected receipt with no broker contact:

- `EXTENDED_HOURS_UNSUPPORTED` — no declared window (`policy.window is None`).
- `EXTENDED_ANCHOR_UNAVAILABLE` — no exact retained decision bar to anchor the leg.
- `SESSION_CLOSED_AT_DECISION` — the decision instant is neither RTH nor an extended phase.
- `EXTENDED_HOURS_ALLOWANCE_UNSET` — the window and the bar are both present but no allowance is configured.
- `EXTENDED_ANCHOR_UNPRICEABLE` — the bar's close and the configured allowance quantise to a non-positive limit price.

Inside the regular session (the run's `RunDecisionSession` is `rth`, or the decision bar's own phase resolves to `RTH`) the leg is always `regular_session_shape(side)` — market, DAY, `extended_hours=False` — so an `rth` binding is never shaped by the bar at all. An EXIT accepted with **no deciding program** — safe-flatten, the stuck-EXIT watchdog, recovery, a manual ticket — records no shape, so `_create_reducing_order` resolves `shape=None` to the regular-session shape and stays market DAY; it is never re-anchored to an extended-session limit. A *decision-driven* EXIT is different: its shape is durable on `EXIT_ACCEPTED` (see "Unfilled orders" below), so whichever later pass creates the reduction rebuilds the deciding program's leg.

Every decision reaches `shape_program_leg` with its exact retained decision bar: both runners resolve it through the one shared `bot_trade_strategy._decision_bar_evidence`, which also names that bar in the decision receipt's `bar_ref`. A runner that resolved none would refuse every `use_rth=False` decision `EXTENDED_ANCHOR_UNAVAILABLE`, RTH-inside-extended decisions included.

A bar is *filtered into* the run by its **open** (`start_ms`, `RunDecisionSession.includes`) and *shapes an order* by its **close** (`end_ms`, `shape_program_leg`). Both are correct and they are deliberately different instants: the session that produced the bar is the one it opened in, while the order it drives exists only once the bucket has closed.

## Unfilled orders (D5.4)

- **Unfilled ENTER.** A broker-acknowledged ENTER the vendor cancels, expires, or rejects with zero fills leaves position 0, raises no uncertainty, and `NEW_EXPOSURE` stays allowed for a fresh ENTER decision — that much matches the original plan. What the plan did not anticipate: the ENTER fold (`app/broker/alpaca/clerk/sqlite/enter.py`) never drives its effect operation to a terminal state by design (its own docstring: reaching `succeeded` needs to know an ENTER is "done", which is EXIT/reconciliation territory) — so the dead ENTER's effect operation is left reading `operation_state="in_progress"` forever, not `failed` or any other terminal value, even though the position and admission facts are already correct. **Follow-up:** mirror plan ruling R12 for ENTER — a terminal, proven-zero-fill ENTER should reach `failed`, the same way R12 makes an unfilled EXIT reducing order reach `failed` today. Pinned by `tests/broker/alpaca/clerk/sqlite/test_unfilled_extended_orders.py::test_enter_cancelled_by_the_vendor_unfilled_leaves_no_exposure`.
- **Unfilled EXIT.** A reducing order the vendor cancels, expires, or rejects with no recorded execution is now proven unfilled and raises `EXIT_NOT_FLAT` immediately, on the first pass that observes the terminal broker state (plan ruling R12). Before this task the same evidence left the effect operation parked `unknown` — "awaiting websocket or recovery evidence" that a dead order was never going to produce — which also blocked any second EXIT attempt on that entry, since the parked `unknown` effect stayed active. The next EXIT decision, once accepted, is admitted despite the standing `EXIT_NOT_FLAT` uncertainty (selling the remaining long still moves the position toward zero) and creates a fresh reducing order at the new decision's anchor; the dead order is never resubmitted. Pinned by `tests/broker/alpaca/clerk/sqlite/test_unfilled_extended_orders.py::test_exit_reducing_order_cancelled_unfilled_is_an_uncertainty_immediately` and `::test_next_exit_decision_reissues_at_the_new_anchor`.
- **A deferred EXIT keeps its shape.** The decision's leg shape is durable on the `EXIT_ACCEPTED` facts (`ExitAcceptedFacts.with_reducing_shape`), not only on the reducing order, because the two can be many passes apart: when the entry is still working, cancel-and-prove defers, and whichever later pass creates the reduction — the 15 s reconciliation sweep, the exit watchdog, restart recovery — knows nothing about the decision. `resolve_exit` therefore takes no shape argument at all and reads it from the EXIT's own acceptance, so the deciding runner's first pass and every re-drive of that EXIT build the same leg. Only a **non-regular** shape is recorded: the regular-session shape is exactly what a reduction with no recorded shape already builds, so writing it out would change the canonical JSON of every regular-hours EXIT acceptance — and every sealed receipt hashed over one — to say what the default already said. Pinned by `tests/broker/alpaca/clerk/sqlite/test_exit_reducing_shape.py::test_a_deferred_cancel_still_reduces_with_the_decisions_shape`.
- **A resumed reducing submission replays a stale anchor.** The reducing order's shape is durable in its `EXIT_REDUCING_ORDER_CREATED` facts, so a resubmission after a crash or a `BrokerUnavailable` rebuilds the identical limit from those facts — priced against the decision bar of the decision that created it, not against the market as it stands now. It is a DAY order, so it dies at the session end rather than resting into a market that has moved; a genuinely stale one is cancelled by the venue and folds through the unfilled-EXIT path above.
- **No emergency or operator reduce executes during extended hours.** An EXIT with no deciding program carries no shape and stays market DAY (ruling R5) — and Alpaca queues a market order submitted outside regular hours until the next 09:30. This is the whole class: the exit watchdog, safe-flatten, reconciliation's flattening, and the Broker Desk's own "flatten and stop" (which takes the `use_rth=True` default). After this slice a bot can be long from 05:00, so that is a real change in exposure: between 05:00 and the next regular open there is no path — automatic or operator — that actually reduces the position, and the panel's success copy for "flatten and stop" reads as though the order is going out now when the vendor is holding it. Shaping the operator flatten from the current instant is tracked as a follow-up; until it ships, an operator who must be flat outside regular hours has to place the reducing order themselves.

## Replay proof

`RunReplayProofService._compute` (`app/services/run_replay_proof.py`) resolves an extended binding's (`use_rth=False`) window through `active_program_leg_policy().window` before any read or compute work. When the active authority declares no window, replay is refused loudly — `RunReplayUnavailableError` with `http_status=503` — rather than silently replayed as an empty-but-"proven" receipt for bars the run's `RunDecisionSession` could never have matched.

## The `sim:` world (R9)

The synthetic broker (`app/broker/alpaca/clerk/synthetic_broker.py::_resolved_order`) asks `fill_models.immediate_fill_price` whether the leg transacts, and records honest `order_type`, `limit_price`, `time_in_force`, and `extended_hours` on the synthesized order either way: a marketable LIMIT leg — or a MARKET leg — fills at the decision bar's close, and a non-marketable LIMIT is cancelled on the spot with zero fills, since the sim world cannot rest an order (ruling R9). The qualification **drill** double (`app/services/alpaca_sqlite_synthetic_drill_support.py::DRILL_CAPABILITIES`) declares no extended session at all — it copies the paper capabilities and does not carry an extended window, so drill runs never exercise this path.

## Hash-chain compatibility

Every durable leg hash — the manual instruction hash, the manual command's `payload_hash`, and the bot-driven ENTER decision's `payload_hash` — routes through the one canonical payload rule, `app/broker/alpaca/clerk/sqlite/facts.py::leg_instruction_payload`, which omits `extended_hours` from the hashed payload when it is `False`, so a leg accepted before the field existed keeps validating against its stored hash. `EXIT_REDUCING_ORDER_CREATED` facts (`ExitReducingOrderCreatedFacts`) apply the same rule to their four shape fields (`order_type`, `time_in_force`, `limit_price`, `extended_hours`): each is omitted from the serialized fact when it equals its regular-session default. `EXIT_ACCEPTED` facts (`ExitAcceptedFacts`) carry the same four plus `reducing_side`, under one shared `_omit_defaults` rule — and record them only for a non-regular shape, so every acceptance written before the fields existed, and every regular-hours one written after, serializes byte-identically.

## Validation

`tests/broker/alpaca/test_marketable_limit.py`, `tests/services/test_session_authority_declared_window.py`, `tests/services/test_decision_session.py`, `tests/services/test_decision_clock.py`, `tests/services/test_bot_trade_strategy_extended_bars.py`, `tests/services/test_feed_continuity_policy.py`, `tests/marketdata/test_feed_continuity.py`, `tests/services/test_run_admission.py`, `tests/services/test_run_admission_extended_hours.py`, `tests/broker/alpaca/clerk/test_program_leg.py`, `tests/broker/alpaca/clerk/sqlite/test_exit_reducing_shape.py`, `tests/broker/alpaca/clerk/sqlite/test_unfilled_extended_orders.py`, `tests/broker/alpaca/clerk/test_fill_models.py`.

## Follow-ups

- **Mirror R12 for ENTER.** A terminal, proven-zero-fill ENTER should reach `failed` instead of leaving its effect operation reading `in_progress` forever — see "Unfilled orders" above.
- **Shape the operator flatten from the current instant.** Today every emergency and operator reduce is a market DAY order the vendor queues to the next regular open, so nothing reduces an extended-hours position while it is held — see "Unfilled orders" above.
- Paper exercise: `.env` currently sets neither `ALPACA_LIVE_XH_ENTRY_BPS` nor `ALPACA_LIVE_XH_EXIT_BPS` — no default exists by design, so a `use_rth=False` paper run refuses `EXTENDED_HOURS_ALLOWANCE_UNSET` in any mode until both are set. Deploy one sealed instance with `use_rth=False` on the paper account with both allowances set; record the first extended-session submission, its Alpaca acknowledgement (`extended_hours: true`), and the 20:00 cancel of an unfilled order. Until then the vendor's early-close after-hours end is unverified.
- Overnight (20:00–04:00): a separate venue, separate data; needs an overnight bar source before it can be a decision phase.
- Bar labels: the IBKR data feed labels extended bars `CLOSED` unless a session capability probe is fresh; the decision session is computed from the instant, so labels are informational. Re-labelling from the declared window is a feed concern, tracked separately.
