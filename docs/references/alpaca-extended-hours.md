# Alpaca extended hours — the window, the anchor, the clock, and unfilled orders

**Status:** canonical for ADR 0059 slice 3 (2026-09-08). Lineage: live.

## What was built

- `BrokerOrderLeg.extended_hours` (contract validator: LIMIT + DAY|GTC) and `BrokerOrder.extended_hours`; `to_alpaca_order_request` forwards the flag on every order body, `from_alpaca_order` ingests it.
- `BrokerCapabilities.extended_hours_window` — the declared session as ET minutes past midnight; `ALPACA_EXTENDED_HOURS_WINDOW` = 04:00–20:00 ET.
- `app/services/session_authority.py` resolves PRE / RTH / POST from the calendar's regular session and the declared window (source `broker_declared_window`, `extended_phase_proven=True`).
- `app/services/decision_clock.py::extended_trigger_instants` and `decision_session="extended"`; `continuity_policy_for` offers it to `use_rth=False` bindings.
- `app/broker/alpaca/marketable_limit.py::marketable_limit_price` and `app/broker/alpaca/clerk/program_leg.py::shape_program_leg`; the reducing order's shape is durable in `EXIT_REDUCING_ORDER_CREATED` facts.
- `app/broker/alpaca/clerk/fill_models.py::limit_touch_fill` (for the slice-4 shadow port); the `sim:` world reports limit legs honestly and cancels a non-marketable limit immediately.
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

`buy: ceil_tick(close × (1 + entry_bps/10⁴))`, `sell: floor_tick(close × (1 − exit_bps/10⁴))`; tick 0.01 at or above $1, 0.0001 below. Rounding is in the marketable direction. The allowances are `ALPACA_LIVE_XH_ENTRY_BPS` / `ALPACA_LIVE_XH_EXIT_BPS`, required for a `use_rth=False` trade-mode run; unset → Start refuses `EXTENDED_HOURS_ALLOWANCE_UNSET`.

## The clock

`extended` buckets run from `floor_et(04:00)` to 20:00 ET; the regular close is an ordinary bucket boundary; the day's last bucket is force-flushed at 20:00 (the runner's flush instant is `decision_session_close_ms`). A decision at exactly 16:00 is POST (sessions are half-open); a decision at 20:00 is CLOSED and refused (`SESSION_CLOSED_AT_DECISION`).

## Admission

Start and Resume each carry an `ExtendedHoursAdmissionFact` (`app/schemas/run_admission.py`) whose state is one of `NOT_REQUESTED | READY | UNSUPPORTED | ALLOWANCE_UNSET`:

Both failing states refuse in **every** mode — trade, dry-run and log-only alike — and there is no mode carve-out:

- `UNSUPPORTED` — the active broker authority declares no extended window — refuses with `EXTENDED_HOURS_UNSUPPORTED`.
- `ALLOWANCE_UNSET` — `ALPACA_LIVE_XH_ENTRY_BPS` / `ALPACA_LIVE_XH_EXIT_BPS` are not both set — refuses with `EXTENDED_HOURS_ALLOWANCE_UNSET`. A Dry Run runs on the synthetic authority built with the same `ProgramLegPolicy` and routes every intent through `shape_program_leg`, which refuses the same condition per decision; admitting such a run would start a bot that then rejects every extended decision it makes.

`app/services/run_admission.py` maps `ExtendedHoursAdmissionFact.state` to the named `LegRefusal` values exported by `app/broker/alpaca/clerk/program_leg.py`, so the wording an operator reads at the gate is the wording on the receipt.

## Leg refusals

`app/broker/alpaca/clerk/program_leg.py::shape_program_leg` never guesses a leg shape; outside the regular session it raises one of four typed refusals, each a rejected receipt with no broker contact:

- `EXTENDED_HOURS_UNSUPPORTED` — no declared window (`policy.window is None`).
- `EXTENDED_ANCHOR_UNAVAILABLE` — no exact retained decision bar to anchor the leg.
- `SESSION_CLOSED_AT_DECISION` — the decision instant is neither RTH nor an extended phase.
- `EXTENDED_HOURS_ALLOWANCE_UNSET` — the window and the bar are both present but no allowance is configured.

Inside the regular session (`decision_session == "rth"`, or the decision bar's own phase resolves to `RTH`) the leg is always `REGULAR_SESSION_SHAPE` — market, DAY, `extended_hours=False` — so an `rth` binding is never shaped by the bar at all. A recovery-created reducing order (sweep, watchdog, safe-flatten, or a manual ticket) carries no deciding-program leg shape, so `_create_reducing_order` resolves `shape=None` to `REGULAR_SESSION_SHAPE` and stays market DAY — it is never re-anchored to an extended-session limit.

## Unfilled orders (D5.4)

- **Unfilled ENTER.** A broker-acknowledged ENTER the vendor cancels, expires, or rejects with zero fills leaves position 0, raises no uncertainty, and `NEW_EXPOSURE` stays allowed for a fresh ENTER decision — that much matches the original plan. What the plan did not anticipate: the ENTER fold (`app/broker/alpaca/clerk/sqlite/enter.py`) never drives its effect operation to a terminal state by design (its own docstring: reaching `succeeded` needs to know an ENTER is "done", which is EXIT/reconciliation territory) — so the dead ENTER's effect operation is left reading `operation_state="in_progress"` forever, not `failed` or any other terminal value, even though the position and admission facts are already correct. **Follow-up:** mirror plan ruling R12 for ENTER — a terminal, proven-zero-fill ENTER should reach `failed`, the same way R12 makes an unfilled EXIT reducing order reach `failed` today. Pinned by `tests/broker/alpaca/clerk/sqlite/test_unfilled_extended_orders.py::test_enter_cancelled_by_the_vendor_unfilled_leaves_no_exposure`.
- **Unfilled EXIT.** A reducing order the vendor cancels, expires, or rejects with no recorded execution is now proven unfilled and raises `EXIT_NOT_FLAT` immediately, on the first pass that observes the terminal broker state (plan ruling R12). Before this task the same evidence left the effect operation parked `unknown` — "awaiting websocket or recovery evidence" that a dead order was never going to produce — which also blocked any second EXIT attempt on that entry, since the parked `unknown` effect stayed active. The next EXIT decision, once accepted, is admitted despite the standing `EXIT_NOT_FLAT` uncertainty (selling the remaining long still moves the position toward zero) and creates a fresh reducing order at the new decision's anchor; the dead order is never resubmitted. Pinned by `tests/broker/alpaca/clerk/sqlite/test_unfilled_extended_orders.py::test_exit_reducing_order_cancelled_unfilled_is_an_uncertainty_immediately` and `::test_next_exit_decision_reissues_at_the_new_anchor`.
- Recovery-created reducing orders carry no decision context and stay market DAY; outside regular hours the vendor queues them for the next open.

## Replay proof

`RunReplayProofService._compute` (`app/services/run_replay_proof.py`) resolves an extended binding's (`use_rth=False`) window through `active_program_leg_policy().window` before any read or compute work. When the active authority declares no window, replay is refused loudly — `RunReplayUnavailableError` with `http_status=503` — rather than silently replayed as an empty-but-"proven" receipt for bars `_includes_decision_bar` could never have matched.

## The `sim:` world (R9)

The synthetic broker (`app/broker/alpaca/clerk/synthetic_broker.py`) fills a marketable LIMIT leg — or a MARKET leg — at the decision bar's close, recording honest `order_type`, `limit_price`, `time_in_force`, and `extended_hours` on the synthesized order; a non-marketable LIMIT is cancelled on the spot with zero fills, since the sim world cannot rest an order (ruling R9). The qualification **drill** double (`app/services/alpaca_sqlite_synthetic_drill_support.py::DRILL_CAPABILITIES`) declares no extended session at all — it copies the paper capabilities and does not carry an extended window, so drill runs never exercise this path.

## Hash-chain compatibility

Every durable leg hash — the manual instruction hash, the manual command's `payload_hash`, and the bot-driven ENTER decision's `payload_hash` — routes through the one canonical payload rule, `app/broker/alpaca/clerk/sqlite/facts.py::leg_instruction_payload`, which omits `extended_hours` from the hashed payload when it is `False`, so a leg accepted before the field existed keeps validating against its stored hash. `EXIT_REDUCING_ORDER_CREATED` facts (`ExitReducingOrderCreatedFacts`) apply the same rule to their four shape fields (`order_type`, `time_in_force`, `limit_price`, `extended_hours`): each is omitted from the serialized fact when it equals its regular-session default.

## Validation

`tests/broker/alpaca/test_marketable_limit.py`, `tests/services/test_session_authority_declared_window.py`, `tests/services/test_decision_clock.py`, `tests/broker/alpaca/clerk/test_program_leg.py`, `tests/broker/alpaca/clerk/sqlite/test_exit_reducing_shape.py`, `tests/broker/alpaca/clerk/sqlite/test_unfilled_extended_orders.py`, `tests/broker/alpaca/clerk/test_fill_models.py`.

## Follow-ups

- **Mirror R12 for ENTER.** A terminal, proven-zero-fill ENTER should reach `failed` instead of leaving its effect operation reading `in_progress` forever — see "Unfilled orders" above.
- Paper exercise: `.env` currently sets neither `ALPACA_LIVE_XH_ENTRY_BPS` nor `ALPACA_LIVE_XH_EXIT_BPS` — no default exists by design, so a `use_rth=False` trade-mode paper run refuses `EXTENDED_HOURS_ALLOWANCE_UNSET` until both are set. Deploy one sealed instance with `use_rth=False` on the paper account with both allowances set; record the first extended-session submission, its Alpaca acknowledgement (`extended_hours: true`), and the 20:00 cancel of an unfilled order. Until then the vendor's early-close after-hours end is unverified.
- Overnight (20:00–04:00): a separate venue, separate data; needs an overnight bar source before it can be a decision phase.
- Bar labels: the IBKR data feed labels extended bars `CLOSED` unless a session capability probe is fresh; the decision session is computed from the instant, so labels are informational. Re-labelling from the declared window is a feed concern, tracked separately.
