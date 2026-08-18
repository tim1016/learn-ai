# Live Alpaca operator surface inventory

**Question:** What do the live Alpaca operator surfaces actually present — every route, panel, control, and action, as rendered?

**Method:** Containers up (`podman compose up -d`, all five healthy), surfaces walked in a real browser at 1512×950. Page text and accessibility trees captured from the rendered DOM, not from Angular templates.

**Date:** 2026-08-18. **Code read at:** `9d6fe9c65`.

**Account observed:** `PA3KWXU1C4C3` (paper, Active) — the ADR-0035 cutover account. Equity $99,990.70, 10 configured bots, 58 recorded transactions.

**Filename note:** the ticket specified `…-2026-08-17.md`; this file is dated 2026-08-18 because that is when the surfaces were observed. An audit's date is its verification date.

This is point-in-time supporting evidence, not implementation authority. It enumerates; it does not adjudicate.

> **Corrected 2026-08-18 after review.** The first revision compared the rendered
> manual against `docs/broker-v2-operator-manual.md`. **The app does not load that
> file.** `broker-v2-manual-page.component.ts:69` loads
> `/assets/docs/broker-v2-operator-manual.md`, a **separate committed copy** that
> documents **11** actions where the repo-root copy documents 9. The documented
> counts, the headline ratio, and two rows of the action table are corrected
> below. Five further claims were corrected against code — flagged inline with
> **[corrected]**. The divergence between the two manual copies is itself a
> finding and is recorded under Incidental observations.

---

## Headline

**The undocumented actions are not undiscoverable.** Of the **nine** enum actions missing from the rendered manual, **five** render on the per-bot panel — four in the Operator lens under a panel headed **"Active command gates · 2 ready · 5 blocked"**, plus `resume` as the Trader lens primary button. The *blocked* ones render too, each with a backend-authored sentence explaining why it is blocked.

*(The first revision said "seven of eleven", measured against the stale repo-root manual. Against the manual operators actually read, the ratio is five of nine — a weaker number for the same conclusion.)*

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
| `/brokers/alpaca/deploy` | **[corrected]** A registered **redirect-only** route (`app.routes.ts:265`) to `brokers/alpaca?deploy`, which opens the deploy **drawer**. The first revision called it "not a route"; it is a valid bookmarkable entry point with no component of its own. |

**[corrected]** One route-shape note against the ticket's assumptions, not two. The gallery a bot links to is **account-scoped** (`/brokers/:broker/accounts/:accountId/gallery`) — but `brokers/:broker/gallery` is *also* registered (`app.routes.ts:392`), resolving the account through `brokerGalleryRedirectGuard`. Both are real; the first revision reported the unscoped form as nonexistent.

The **"Deploy strategy"** button appears on the Alpaca desk (`alpaca-desk.component.html:12`) and the bots-list page (`bots-list-page.component.html:34`). **[corrected]** It is **not** on the gallery — the first revision listed it there.

---

## Actions: enum vs manual vs rendered

The closed enum is `ActionId` in `PythonDataService/app/broker/v2panel/vocabulary.py` — **19** ids.

**[corrected] There are two committed operator manuals, and they disagree.** The
Button Reference in `Frontend/src/assets/docs/broker-v2-operator-manual.md` — the
file the app loads — documents **11** actions. `docs/broker-v2-operator-manual.md`
documents **9**, missing `recover_exact_execution_evidence` and
`resolve_execution_coverage`. The "In manual" column below reads the **rendered**
copy, since that is what an operator sees.

| Action id | In enum | In manual | Observed rendered | Where / gate copy |
|---|---|---|---|---|
| `deploy` | ✔ | ✔ | ✔ | "Deploy strategy" button on desk and roster (**[corrected]** — not the gallery) |
| `resume` | ✔ | ✖ | ✔ | Per-bot Trader lens, primary button — **blocked**: *"Market Data Unavailable. Resume is blocked. The required market-data feed is not proven ready for this run."* |
| `pause` | ✔ | ✖ | ✖ | Requires an on-duty bot; fleet was entirely off duty |
| `continue` | ✔ | ✖ | ✖ | Same blocker as `pause` |
| `stop` | ✔ | ✔ | ✖ | Requires an on-duty bot |
| `flatten_stop` | ✔ | ✔ | ✖ | **[corrected]** Does *not* require on-duty. `_guard_flatten_stop` (`action_policy.py:237`) blocks only when `not running` **and** `not has_exposure`, so a stopped-but-exposed bot can flatten. Every bot here was flat, which is why it did not render. |
| `retire` | ✔ | ✔ | ✖ | **[corrected]** Cannot render for Alpaca: `supported_brokers=frozenset()` (`action_policy.py:395-397`), and `build_actions_from_registry` drops unsupported actions. Not "unreached" — **unsupported**. |
| `cancel_order` | ✔ | ✔ | ✖ | **[corrected]** Same as `retire` — `supported_brokers=frozenset()` (`action_policy.py:402-404`). Unsupported for Alpaca regardless of working orders. |
| `clear_hold` | ✔ | ✔ | ✖ | No hold active. Manual scopes it *"Legacy/unactivated JSONL accounts only"* |
| `record_inventory_baseline` | ✔ | ✔ | ✖ | Not observed on an activated SQLite account |
| `reconcile_now` | ✔ | ✔ | ✔ | Operator lens, **READY**: *"Compare durable Clerk custody with a fresh Alpaca account observation."* |
| `recover_exact_execution_evidence` | ✔ | **✔** | ✔ | Operator lens, **BLOCKED**: *"No active execution-coverage conflict requires historical evidence recovery."* |
| `resolve_execution_coverage` | ✔ | **✔** | ✔ | Operator lens, **BLOCKED**: *"No active execution-coverage conflict has a Clerk-owned resolution path."* |
| `cancel_verified_working_orders` | ✔ | ✖ | ✔ | Operator lens, **BLOCKED**: *"No working order has both a durable Clerk reference and broker identity."* |
| `prepare_safe_flatten` | ✔ | ✖ | ✔ | Operator lens, **BLOCKED**: *"No attributed exposure requires a flatten plan."* |
| `stop_bot_decisions` | ✔ | ✖ | ✔ | Operator lens, **BLOCKED**: *"Select a bot with an active run; no decision process is currently stoppable."* |
| `open_custody_timeline` | ✔ | ✖ | ✔ | Operator lens, **READY**: *"Inspect the immutable operation-first evidence timeline."* |
| `rebuild_from_mirror` | ✔ | ✖ | ✖ | **[corrected]** Rendering site is known: `OVERFLOW_ACTION_IDS` in `operator-bot-banner.component.ts:26-30`. Not reached on a healthy account. |
| `reset_authority` | ✔ | ✖ | ✖ | **[corrected]** Same overflow. Not reached on a healthy account. |
| `start` | **✖** | ✔ | ✖ | **Phantom.** Documented with a full "When available / What it does" entry; absent from the enum. |

**Counts:** 19 in the enum · **11 documented in the rendered manual** (9 in the stale repo-root copy) · 8 observed rendered · 1 documented-but-nonexistent. Of the 11 documented, 10 are in the enum and one is the phantom, so **9 enum actions are undocumented**. Two of those 11 — `retire` and `cancel_order` — are documented but **cannot render for Alpaca at all**.

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

- **Trader lens** — header (strategy, name, phase, revision, "Revision 13514 stopped"), primary action **Resume** with its blocked reason, overflow `⋯` ("More trader actions") — **[corrected]** not an unknown container: `trader-bot-banner.component.html:32` always places a **Manual order** link inside it, navigating to the Alpaca account desk with a symbol-prefilled ticket. The automation failed to open the menu; the contents were readable from the template all along. Panels: *Bot market tape* (Live / 15m Delayed tabs, Local/ET toggle, 5s/1m interval, IBKR source, "No candles in this window / The live tape will draw when IBKR publishes the next bar", fill-marker legend), *Trading summary* (fills today, realized, open, exposure, orders working/unresolved, last decision, last bar), *Execution / Fills today* ("No fills today.", "Fees not reported").
- **Operator lens** — everything above plus: **Current readiness → "Active command gates · 2 ready · 5 blocked"** (the seven-row table quoted earlier), *Account custody* (account, outstanding intents, hold, reconciliation, account freeze, channels with hover copy — *"The channel is down or lagging. Trading is gated until it recovers."* / *"The channel is connected and current."*), *Transaction Gate history* (six stations SIGNAL → INTENT → SUBMIT GATE → BROKER ACK → FILL → RECONCILED, all "Not applicable", with *"No active transaction. Resume the bot to see pipeline activity."*), *Working orders* (0, "No Clerk-attributed working orders."), *Bot health* (phase, desired state, process, last bar, resume custody, last decision), and collapsed *Audit trail* and *Run evidence*.

### Gallery

10 tiles (symbol, strategy, sparkline placeholder, fills, P&L). Page controls: Reset layout, filter counts **All 10 / Running 0 / Needs attn 0 / Stopped 10**, "Today · 5s", pagination, and a live-status line reading **"Connecting…"** for the duration of the visit.

**[corrected] Every tile also renders a lifecycle control.** `bot-tile.component.html:6-30` renders a `primary_action` button per tile — a Stop glyph for a running bot, a play glyph otherwise — disabled from `bot().primary_action.enabled`, with the backend-derived reason carried in both `title` and `aria-label`. That is **ten** lifecycle controls the first revision omitted, on a page whose inventory claimed to enumerate every rendered control. No deploy trigger exists on this page.

### Rendered manual

15,537 characters at `/brokers/alpaca/manual`.

**[corrected]** The first revision stated its Button Reference "matches the repo markdown's nine entries". That was an assumption, not an observation — the entries were not counted in the DOM, and the comparison was against the wrong file. The page loads `/assets/docs/broker-v2-operator-manual.md`, whose Button Reference has **11** `### \`action\`` entries: the root copy's nine plus `recover_exact_execution_evidence` and `resolve_execution_coverage`.

---

## Unreached, and why

Per the ticket, an unreachable surface is a finding.

- **`pause`, `continue`, `stop`** — require an **on-duty** bot, and every one of the 10 bots was off duty and flat. Starting one was not attempted: it would submit paper orders, which is beyond an inventory's remit.
- **`flatten_stop`** — **[corrected]** not gated on on-duty. It was absent because every bot was **flat**; a stopped-but-exposed bot would have shown it.
- **`retire`, `cancel_order`** — **[corrected]** these are **unsupported for Alpaca**, not unreached: both carry `supported_brokers=frozenset()`. No account state renders them, so no follow-up walk will find them. That is an implementation-versus-manual gap, since the manual documents both.
- **`clear_hold`, `record_inventory_baseline`** — no hold was active and the account is activated SQLite; `clear_hold` is manual-scoped to legacy JSONL accounts.
- **`rebuild_from_mirror`, `reset_authority`** — **[corrected]** their rendering site is **not** unknown: `OVERFLOW_ACTION_IDS` in `operator-bot-banner.component.ts:26-30` filters both out of `panel.actions` into the Operator banner overflow, and the SQLite recovery policy emits them on typed authority failure. A healthy account cannot present them; locating the surface is *not* outstanding work, and the first revision's call to go looking should not be acted on.
- **Operator lens on the account desk vs the bot panel** — both exist and differ; the bot-level lens is where the command-gate table lives.

**Market data was unavailable throughout** (IBKR not connected — the rail offers "Connect IBKR market data"). That gated `resume` and left the tape empty, which is itself a faithful observation of what an operator sees without the bridge running.

---

## Incidental observations

Recorded because they were seen, not adjudicated here.

1. **The IBKR control is labelled by function, not by broker.** The global rail button reads **"Connect IBKR market data"** and the roster channel strip reads **"Market Data unhealthy"** — both correctly scope IBKR to the market-data bridge rather than presenting it as a trading connection. ADR 0037 consequence flagged a risk of the opposite; on these two surfaces the labelling is right. The account desk's collapsed *"Broker connection"* technical-feed panel is the one that reads ambiguously.
2. **A filled order displays as "In Progress".** Transaction history row `2026-08-17 12:02:06`, SPY, "Buy 1 · Market", "1 filled at $774.76", status **In Progress**, 5 events — while the paired sell at 12:05:20 shows "Succeeded" with 10 events. The same shape recurs on the NVDA pairs. Consistent with an ENTER whose effect never terminalised.
3. **`clear_hold` documents a path ADR 0037 retires.** The manual scopes it to *"Legacy/unactivated JSONL accounts only"* — the authority ADR 0037 removes. One of the nine documented actions therefore documents a branch scheduled for deletion.
4. **The gallery's live indicator read "Connecting…" throughout** and never resolved during the visit.
5. **[new] Two committed operator manuals have diverged.** `docs/broker-v2-operator-manual.md` (18,234 bytes) and `Frontend/src/assets/docs/broker-v2-operator-manual.md` (17,502 bytes) are both tracked, and only the second is served. The repo-root copy — the one a developer or an agent grepping `docs/` will read — is **missing two documented actions** the operator can see. Any generation or parity gate that targets only the root file would report clean while operators read a stale page. This bears directly on [ADR 0041](../architecture/adrs/0041-generated-operator-button-reference.md) and [#1626](https://github.com/tim1016/learn-ai/issues/1626).
