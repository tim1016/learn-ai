# Live Alpaca operator surface inventory

**Question:** What do the live Alpaca operator surfaces actually present — every route, panel, control, and action, as rendered?

**Method:** Containers up (`podman compose up -d`, all five healthy), surfaces walked in a real browser at 1512×950. Page text and accessibility trees captured from the rendered DOM, not from Angular templates.

**Date:** 2026-08-18. **Code read at:** `9d6fe9c65`.

**Account observed:** `PA3KWXU1C4C3` (paper, Active) — the ADR-0035 cutover account. Equity $99,990.70, 10 configured bots, 58 recorded transactions.

**Filename note:** the ticket specified `…-2026-08-17.md`; this file is dated 2026-08-18 because that is when the surfaces were observed. An audit's date is its verification date.

This is point-in-time supporting evidence, not implementation authority. It enumerates; it does not adjudicate.

---

## Headline

**The undocumented actions are not undiscoverable.** Seven of the eleven actions missing from the operator manual render on the per-bot Operator lens under a panel headed **"Active command gates · 2 ready · 5 blocked"** — and the *blocked* ones render too, each with a backend-authored sentence explaining why it is blocked.

This matters for [#1599](https://github.com/tim1016/learn-ai/issues/1599), which was framed on the premise that the undocumented actions are the ones "an operator reaches for when something has gone wrong and the manual is most load-bearing". Observed behaviour is friendlier than that: the actions are visible, named, and self-explaining on a *healthy, idle* bot. The divergence is a **documentation** gap, not a discoverability gap.

---

## Routes reached

| Route | Result |
|---|---|
| `/brokers/alpaca` | Broker Desk. Renders. Lens tabs **Trader** / **Operator**. |
| `/brokers/alpaca/bots` | Fleet roster. Renders. 10 bots. |
| `/brokers/alpaca/accounts/PA3KWXU1C4C3/bots/<sid>` | Per-bot panel. Renders. Lens tabs **Trader** / **Operator**. |
| `/brokers/alpaca/accounts/PA3KWXU1C4C3/gallery` | Bot Gallery. Renders. 10 tiles. |
| `/brokers/alpaca/manual` | Rendered manual. Renders, 15,537 characters. |
| `/brokers/alpaca/deploy` | **Not a route.** Redirects to `/brokers/alpaca`. Deploy is a **drawer** opened by the "Deploy strategy" button, present on the desk, roster, and gallery. |

Two route-shape corrections against the ticket's assumptions: the gallery is **account-scoped** (`/brokers/:broker/accounts/:accountId/gallery`), not `/brokers/:broker/gallery`; and there is no deploy route at all.

---

## Actions: enum vs manual vs rendered

The closed enum is `ActionId` in `PythonDataService/app/broker/v2panel/vocabulary.py` — **19** ids. The manual's Button Reference documents **9**.

| Action id | In enum | In manual | Observed rendered | Where / gate copy |
|---|---|---|---|---|
| `deploy` | ✔ | ✔ | ✔ | "Deploy strategy" button on desk, roster, gallery |
| `resume` | ✔ | ✖ | ✔ | Per-bot Trader lens, primary button — **blocked**: *"Market Data Unavailable. Resume is blocked. The required market-data feed is not proven ready for this run."* |
| `pause` | ✔ | ✖ | ✖ | Requires an on-duty bot; fleet was entirely off duty |
| `continue` | ✔ | ✖ | ✖ | Same blocker as `pause` |
| `stop` | ✔ | ✔ | ✖ | Requires an on-duty bot |
| `flatten_stop` | ✔ | ✔ | ✖ | Requires on-duty **and** open exposure; every bot flat |
| `retire` | ✔ | ✔ | ✖ | Not seen; likely behind the per-bot overflow (`⋯`, "More trader actions") |
| `cancel_order` | ✔ | ✔ | ✖ | Requires a working order; 0 working |
| `clear_hold` | ✔ | ✔ | ✖ | No hold active. Manual scopes it *"Legacy/unactivated JSONL accounts only"* |
| `record_inventory_baseline` | ✔ | ✔ | ✖ | Not observed on an activated SQLite account |
| `reconcile_now` | ✔ | ✔ | ✔ | Operator lens, **READY**: *"Compare durable Clerk custody with a fresh Alpaca account observation."* |
| `recover_exact_execution_evidence` | ✔ | ✖ | ✔ | Operator lens, **BLOCKED**: *"No active execution-coverage conflict requires historical evidence recovery."* |
| `resolve_execution_coverage` | ✔ | ✖ | ✔ | Operator lens, **BLOCKED**: *"No active execution-coverage conflict has a Clerk-owned resolution path."* |
| `cancel_verified_working_orders` | ✔ | ✖ | ✔ | Operator lens, **BLOCKED**: *"No working order has both a durable Clerk reference and broker identity."* |
| `prepare_safe_flatten` | ✔ | ✖ | ✔ | Operator lens, **BLOCKED**: *"No attributed exposure requires a flatten plan."* |
| `stop_bot_decisions` | ✔ | ✖ | ✔ | Operator lens, **BLOCKED**: *"Select a bot with an active run; no decision process is currently stoppable."* |
| `open_custody_timeline` | ✔ | ✖ | ✔ | Operator lens, **READY**: *"Inspect the immutable operation-first evidence timeline."* |
| `rebuild_from_mirror` | ✔ | ✖ | ✖ | Not reached — see Unreached below |
| `reset_authority` | ✔ | ✖ | ✖ | Not reached — see Unreached below |
| `start` | **✖** | ✔ | ✖ | **Phantom.** Documented with a full "When available / What it does" entry; absent from the enum. |

**Counts:** 19 in the enum, 9 documented, 8 observed rendered, 1 documented-but-nonexistent.

### The phantom is a live contradiction, not a stale name

The manual's `start` entry reads: *"When available: When the bot's desired state is STOPPED and it is OFF_DUTY."* The observed bot is exactly that state — desired state `Stopped`, phase `Off duty` — and the button rendered is **Resume**, not Start. So the manual describes the correct *condition* and the wrong *control*, on the very state an operator is most likely to be looking at.

This is the pair `CONTEXT.md` disambiguates (Continue = resume a paused live run; Resume = new run of the same instance) and the manual documents neither.

---

## Panels observed

### `/brokers/alpaca` — Broker Desk

Account header: account id, Paper, Active, EQUITY / CASH / BUYING POWER / UPDATED (LOCAL), Account details (portfolio value, long/short market value, "Trading enabled", "Account clear", "Pattern day trader: Unknown", account opened).

- **Trader lens** — "Trader desk / A live view of your Alpaca account, activity, and open exposure." Range chips Today / 30D / 60D. Panels: *Current positions* ("No open positions."), *Activity* ("No account activity has been recorded today.").
- **Operator lens** — "Operator desk / Review account health, investigate broker activity, and use only actions supported by current evidence." Panels: *Account status* ("Account Clerk custody is healthy" / "Durable Clerk state has no active hold or unresolved uncertainty in this scope." / "Recommended next step: No recovery action is required."), *Transaction history* (58 rows, paginated 1–5 of 58, searchable), and four collapsed *Technical feed details*: Order custody & recovery, Broker connection, Trading fleet, Account source of truth.

### `/brokers/alpaca/bots` — fleet roster

Header strip: ACCOUNT CUSTODY, BROKER (Active / Trading available), RECONCILIATION (Clean + timestamp), ACCOUNT SERVICE (Clear / No custody block), **CHANNELS (Market Data unhealthy · Execution healthy)**, EQUITY / CASH / BUYING POWER.

Roster: filter chips All / Working / Off duty / Needs attention / Retired, search box, "Attention-first ordering", footer *"Fees not reported — P&L excludes transaction costs."* Each row: symbol, name, strategy · mode, phase, one-line status ("Off duty and flat."), exposure, "Clerk attributed", realized/open P&L, fill count, timestamp, and a single **Review** link. No per-row action other than Review.

### Per-bot panel

- **Trader lens** — header (strategy, name, phase, revision, "Revision 13514 stopped"), primary action **Resume** with its blocked reason, overflow `⋯` ("More trader actions"). Panels: *Bot market tape* (Live / 15m Delayed tabs, Local/ET toggle, 5s/1m interval, IBKR source, "No candles in this window / The live tape will draw when IBKR publishes the next bar", fill-marker legend), *Trading summary* (fills today, realized, open, exposure, orders working/unresolved, last decision, last bar), *Execution / Fills today* ("No fills today.", "Fees not reported").
- **Operator lens** — everything above plus: **Current readiness → "Active command gates · 2 ready · 5 blocked"** (the seven-row table quoted earlier), *Account custody* (account, outstanding intents, hold, reconciliation, account freeze, channels with hover copy — *"The channel is down or lagging. Trading is gated until it recovers."* / *"The channel is connected and current."*), *Transaction Gate history* (six stations SIGNAL → INTENT → SUBMIT GATE → BROKER ACK → FILL → RECONCILED, all "Not applicable", with *"No active transaction. Resume the bot to see pipeline activity."*), *Working orders* (0, "No Clerk-attributed working orders."), *Bot health* (phase, desired state, process, last bar, resume custody, last decision), and collapsed *Audit trail* and *Run evidence*.

### Gallery

10 tiles (symbol, strategy, sparkline placeholder, fills, P&L). Controls: Reset layout, filter counts **All 10 / Running 0 / Needs attn 0 / Stopped 10**, "Today · 5s", pagination, and a live-status line reading **"Connecting…"** for the duration of the visit.

### Rendered manual

15,537 characters at `/brokers/alpaca/manual`; its Button Reference matches the repo markdown's nine entries.

---

## Unreached, and why

Per the ticket, an unreachable surface is a finding.

- **`pause`, `continue`, `stop`, `flatten_stop`, `cancel_order`** — all require an **on-duty** bot, and every one of the 10 bots was off duty and flat. Starting one was not attempted: it would submit paper orders, which is beyond an inventory's remit.
- **`retire`** — not surfaced in the main panel; the `⋯` overflow did not open under automation (click registered, no menu in the DOM). Likely there; unconfirmed.
- **`clear_hold`, `record_inventory_baseline`** — no hold was active and the account is activated SQLite; `clear_hold` is manual-scoped to legacy JSONL accounts.
- **`rebuild_from_mirror`, `reset_authority`** — not surfaced anywhere walked. These are the deepest recovery actions; they presumably require a corrupt or unavailable authority, which a healthy account cannot present. **These two are the only actions in the enum with no observed rendering site at all**, and locating them is unfinished work.
- **Operator lens on the account desk vs the bot panel** — both exist and differ; the bot-level lens is where the command-gate table lives.

**Market data was unavailable throughout** (IBKR not connected — the rail offers "Connect IBKR market data"). That gated `resume` and left the tape empty, which is itself a faithful observation of what an operator sees without the bridge running.

---

## Incidental observations

Recorded because they were seen, not adjudicated here.

1. **The IBKR control is labelled by function, not by broker.** The global rail button reads **"Connect IBKR market data"** and the roster channel strip reads **"Market Data unhealthy"** — both correctly scope IBKR to the market-data bridge rather than presenting it as a trading connection. ADR 0037 consequence flagged a risk of the opposite; on these two surfaces the labelling is right. The account desk's collapsed *"Broker connection"* technical-feed panel is the one that reads ambiguously.
2. **A filled order displays as "In Progress".** Transaction history row `2026-08-17 12:02:06`, SPY, "Buy 1 · Market", "1 filled at $774.76", status **In Progress**, 5 events — while the paired sell at 12:05:20 shows "Succeeded" with 10 events. The same shape recurs on the NVDA pairs. Consistent with an ENTER whose effect never terminalised.
3. **`clear_hold` documents a path ADR 0037 retires.** The manual scopes it to *"Legacy/unactivated JSONL accounts only"* — the authority ADR 0037 removes. One of the nine documented actions therefore documents a branch scheduled for deletion.
4. **The gallery's live indicator read "Connecting…" throughout** and never resolved during the visit.
