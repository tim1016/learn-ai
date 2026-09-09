# ADR 0059 — Real-money Alpaca Live is reachable only through a shadow gate, a per-instance arming ceremony, and a cash-bound risk envelope

**Status:** Accepted 2026-09-07
**Provenance:** Proposed, read and amended the same day; the owner's answers are recorded under "Resolved before Accepted", which under [ADR 0039](0039-adr-status-is-decision-standing.md) is the read-through that promotes; code lands from this point in the slice order under Consequences. Scope memo [`live-account-attachment-scope-2026-09-07.md`](../live-account-attachment-scope-2026-09-07.md) (which this ADR resolves), and the 2026-09-07 grilling session in which the repo owner chose real-money submission over read-only observation, a mandatory shadow gate, a cash-only envelope, no-new-entries-on-loss, and then — on reading the draft — extended hours and every symbol in scope, no per-order cap, and every envelope value sourced from the environment file. External facts verified on 2026-09-07 against Alpaca's credential-management, Intraday Margin Rule, regulatory-fee, order and extended-hours documentation and the SEC's FY2026 §31 rate advisory.
**Decision drivers:** The owner wants sealed programs to trade a real-money Alpaca account. The repo forbids this at thirteen deliberate sites (census in the memo §2) and in doctrine — ADR 0042 ("Future real-money Live remains unreachable"), PRD #1723 FR-035, ADR 0021 §6 guardrail 2. None of that is drift; it is the correct posture for a research tool with no live risk envelope. Reaching live therefore needs a decision that says *what replaces the refusal*, not a toggle that removes it. Five facts learned during the session shaped the shape: FINRA retired the Pattern Day Trader rule on 2026-06-04 in favour of an Intraday Margin Rule that measures continuous exposure rather than counting trades; Alpaca API keys carry per-scope access controls and MFA gates only dashboard sign-in; every sealed program in the registry is long-only; every program order is a market DAY order, and Alpaca rejects a market order outside regular hours; and the decision clock is regular-hours-only today (`feed_continuity_policy` refuses `use_rth=False` as `all_session_not_supported`).
**Related:** ADR 0002 (shadow-mode no-submit adapter — the pattern the shadow gate reuses), ADR 0011 (broker safety verdict — its principles govern the new live verdict; its IBKR derivation is untouched), ADR 0021 (guardrail envelope — amended), ADR 0022 (`int64 ms UTC`; the calendar is the sole scheduled-session authority), ADR 0029 (session authority and `BarSessionPhase`), ADR 0033 (custody clocks; custody is not an economic claim), ADR 0035 / 0037 (SQLite Clerk is the sole custody authority), ADR 0042 (one semantic seam; exact-identity authority selection; Paper carryover globally disabled — superseded in one sentence, load-bearing in every other), ADR 0046 (breaker semantics), ADR 0047 (no surfaced action may be structurally unarmable), ADR 0051 (cohort flatten is N attributed legs), ADR 0054 (corpus coverage is a stamp on paper — already blocks UNCOVERED off-paper).
**Vocabulary:** `CONTEXT.md` § "Live account, shadow, and risk envelope (sharpened 2026-09-09)".
**Amended 2026-09-09 (owner decision, slice 7):** Decisions 2, 3 and 8 are amended in place — D2 and D3's shadow-receipt sentences now say the receipt is recorded on the arming record when the instance holds one, never required to arm or to graduate; D8's mid-session halt refuses new entries under `LIVE_VERDICT_TRANSITION_HALT` without writing `desired_state = PAUSED`. Reasons: the slice-7 design's R3 (graduation needs no shadow receipt), R7 (arming needs no shadow receipt), R10 (`PAUSED` is observe-only for EXIT too and would strand a real position) and R12 (D2 names `sealed_account_id`, not `submit_mode`). Authority: the owner's two 2026-09-09 grill-me decisions — no `desired_state = PAUSED`, only the ENTER refusal; and "shadow should be just a mode, not a requirement to run a bot."
**Supersedes:** ADR 0042's Consequences statement that real-money Live remains unreachable (that sentence only). Amends ADR 0021 §6 guardrail 2 and its non-consequence "Does not touch real-money/live trading" for the Alpaca V2 path. Supersedes PRD #1723 FR-035. Extends ADR 0011.

## Context

The scope memo established what this ADR takes as given:

- The blocker is not MFA. Alpaca requires MFA before any account may use the Trading API; it gates dashboard sign-in and does not challenge, throttle or log an API order. A leaked live secret is fully exploitable with MFA on. The Alpaca-side control that matches the intent is per-key **Access Controls** (Read only / Full access / Custom per scope).
- The contract already admits live: `BrokerAccountSnapshot.account_mode` is `Literal["paper", "live"]`. The lock is policy — the adapter hardcodes `"paper"` and thirteen sites refuse `!= "paper"`.
- `AccountAuthorityKind` is a closed two-value type (`real_paper` | `synthetic`). `require_real_paper_account_id` only rejects the `sim:` namespace; it has never checked paper by shape, so a live account id passes it today and the mode check lives elsewhere.
- The Dry Run world (`sim:`) is synthetic on both ports. ADR 0042 forbids a real port from binding to it and forbids synthetic facts from entering a real account's custody journal.
- The per-order admission seam is `enter.py::accept_enter`, which calls `require_admission` before the `ENTER_ACCEPTED` fold; R1 forbids broker contact before acceptance.
- A day-P&L fact can be composed without new math: `realized_pnl_today` (canonical Clerk FIFO) plus Σ broker-observed unrealized, with `marks_complete == False` meaning *unknown*.
- Both real leg constructions (`runtime.py` ENTER, `exit_resolution.py` EXIT) set only `symbol`, `side`, `quantity`; the contract defaults them to `MARKET` / `DAY`. `to_alpaca_order_request` forwards `type`, `time_in_force`, `limit_price` and nothing about sessions.
- Alpaca's rule for orders outside regular hours: **`LIMIT` only, `time_in_force` `DAY` or `GTC`, `extended_hours=True`; a market order is a 422.** The extended window is 04:00–20:00 ET; the overnight session runs 24/5; a `DAY` order placed overnight stays working through the next day's regular and after-hours sessions and is cancelled at 20:00 ET; `GTC` expires after 90 days.
- Missing outright: any margin-field ingestion, any Alpaca fee model (the only commission model is IBKR's), any loss limit, any live verdict on the Alpaca path (the global banner is IBKR market-data status), an `extended_hours` field on the leg, and a decision clock that fires outside regular hours.

## Decision

### 1. Four account worlds, closed and named; a live authority exists only on three-way mode agreement

`AccountAuthorityKind` becomes `real_paper | real_live | shadow | synthetic`. `shadow:<live_account_id>` is a new reserved namespace beside `sim:`. `require_real_paper_account_id` is renamed `require_real_account_id` — it rejects `sim:` and `shadow:`, and never checked paper.

A `real_live` authority may be constructed only when three independent facts agree on the same account id: `ALPACA_MODE=live` in settings; a **live activation record** for that exact account id (Decision 4); and the broker-observed account resolving under the live endpoint. Any disagreement is `LIVE_MODE_DISAGREEMENT` and refuses construction — the Alpaca counterpart of `IbkrSettings._enforce_port_mode_consistency`. `AlpacaSettings._enforce_paper_only` becomes `_enforce_mode_agreement`: `paper` needs nothing; `live` needs every `ALPACA_LIVE_*` setting (Decisions 3, 4, 5) present and within its domain (Decision 4) before the client is built. The other two legs — the live activation record (the account cutover's `ActivationRecord`, which Decision 11 lets exist for a live account) and the broker-observed mode — are checked where an authority is constructed (`select_active_clerk_runtime`), the only place custody can begin. A live account is therefore readable as soon as settings agree, and can hold custody only when all three legs do.

The adapter stops hardcoding `account_mode`. It derives it from the settings mode that selected the endpoint. The account-id shape (Alpaca paper ids begin `PA`) is a **refusal input only**: a `PA…` id under `live`, or a non-`PA` id under `paper`, is `LIVE_MODE_DISAGREEMENT`. Shape never grants a mode (ADR 0054's "never a guess from the account-id shape" holds).

### 2. The shadow gate: every sealed instance shadows the live account before it may arm

Before any sealed instance may submit a real-money order, it completes `ALPACA_LIVE_SHADOW_SESSIONS` calendar trading sessions on the live account under a **Shadow Account Authority**: the real live read port (account, positions, clock, activities) bound to a **no-submit trade port**, writing custody to an isolated database under `accounts/shadow/<live_account_id>/` with its own append-only activation fence modelled on `SyntheticActivationStore`.

`NoSubmitAlpacaTradePort` implements `BrokerTradePort` and the synthetic-only `bind_evaluated_bar` capability: `submit` synthesizes a `BrokerOrder` filled at the bound decision bar under an explicit `fill_model`, typed `execution_source="shadow_sim"`; `cancel` is a no-op; `get_order_by_client_order_id` returns the synthesized order. It never constructs a `TradingClient` write call. ADR 0002's invariants transfer unchanged: cold-start requires the broker to report **zero orders, ever**, in the shadow instance's `client_order_id` namespace (nonzero is poisoned state and refuses); every execution row is typed; the shadow authority can poison only its own directory; and graduation mints a **new run ledger**, because `sealed_account_id` is inside the sealed program's hash and therefore inside run identity (amended 2026-09-09).

A shadow session is a calendar trading day and covers every session phase the instance's binding decides in (Decision 5). It counts only when the sweep reconciled cleanly for the whole day and the instance's synthesized trades reconcile against its paper twin (same seal, same days) within the fixture tolerance. Completion writes a **shadow receipt** — per instance, sha256-sealed, naming the days and the reconciliation report. A current receipt is recorded on the arming record when the instance holds one; arming does not require it (amended 2026-09-09 — shadow is a mode, not a requirement to run a bot).

Shadow proves the live plumbing end to end — auth, live reads, mode agreement, the decision → Clerk → (no-)submit path on a live-mode authority. It does not prove execution quality; synthesized fills are optimistic by construction. That is why it gates arming and does not replace the envelope.

### 3. Arming is a supervised, per-instance ceremony that lapses on an operator-set count

Real-money submission is permitted per **sealed instance**, never per account and never by configuration. The ceremony is modelled on `cutover.py`: `plan` is read-only and re-observes every input; `apply` re-observes again, accepts no force mode, and honours the same confirmation TTL (120 s default, 300 s maximum). It appends a `LiveArmingRecord` to an account-rooted, append-only ledger, sha256-sealed over: `account_id`, `strategy_instance_id`, the instance's seal hash, the shadow receipt sha (null when the instance holds no receipt, amended 2026-09-09), the **envelope sha** (Decision 5), `armed_at_ms`, and `max_sessions`.

`max_sessions` is `ALPACA_LIVE_ARMING_MAX_SESSIONS` from the environment file — required when `ALPACA_MODE=live`, with **no default in code**: a missing value refuses to arm rather than picking a number nobody chose. Arming is bound to what it named: a change to the seal, any envelope value, or the account disarms the instance; re-arming is the same ceremony. Arming **lapses** after `max_sessions` calendar trading sessions and is `LIVE_ARMING_LAPSED` until renewed — a deliberate "come back and look" fence, because an armed bot nobody has looked at in a month is the config accident D7 was written to prevent. Operator intent (PAUSE / STOP) is unchanged and orthogonal: a paused armed bot is still armed; a stopped one is not disarmed by stopping.

### 4. The risk envelope: cash-bound, and an account-wide loss hold — nothing else

The envelope is account-scoped, evaluated in `accept_enter` as a sibling of `require_admission` — inside the custody fence, before `ENTER_ACCEPTED`, so no broker contact precedes it (R1). It is never evaluated in a Signal Program (broker-neutral, ADR 0042) and never composed by the Frontend (ADR 0011 §7). EXIT is never subject to it.

Two rules, in this order:

1. **Cash bound.** An ENTER is admitted only if gross long exposure after the fill would not exceed broker-observed `cash`, where gross long exposure counts the cash already reserved by every working (nonterminal) ENTER across all instances, not only settled positions — a resting extended-hours limit changes neither cash nor positions until it fills, and a second instance must not spend the same cash twice. The bound is `cash`, not `buying_power`, so it is the same on a cash account and a Reg T margin account, and an Intraday Margin Deficit is impossible by construction — the Intraday Margin Rule needs no tracking. Refusal: `LIVE_ENVELOPE_CASH_EXCEEDED`. A refusal changes no other state.
2. **Daily loss hold.** `day_pnl` is **account-wide**: the Clerk's realized session P&L summed over every custody subject — each instance, manual and external orders alike, never one instance's projection — plus Σ broker-observed unrealized P&L over every open position, net of every `FEE` activity the Clerk journals (Decision 6). When `day_pnl ≤ −(ALPACA_LIVE_LOSS_FRACTION × start_of_session_equity)` or `≤ −ALPACA_LIVE_LOSS_USD`, the account enters **loss hold**: every ENTER on the account is refused with `LIVE_ENVELOPE_LOSS_HOLD`; every EXIT still runs, so each program keeps managing its own open position and nothing is stranded or flattened by the limit itself. Loss hold clears only by a guarded operator action of the ADR 0011 §6 shape — the action re-reads the fact and refuses if the breach still stands. It does not clear at session rollover.

Any input the Clerk cannot prove — `marks_complete == False`, a stale account snapshot, an unknown position — is `LIVE_ENVELOPE_UNKNOWN` and refuses ENTER. Unknown is never zero (NFR-002).

**The envelope restricts nothing else.** By the owner's decision there is no per-order notional cap, no symbol allowlist, no session restriction, and no fleet-wide reduce-only on a cash-bound refusal. Any symbol a sealed program is bound to, in any session phase the binding decides in, at any size the cash bound admits.

**Every envelope value comes from the environment file** — `ALPACA_LIVE_LOSS_FRACTION`, `ALPACA_LIVE_LOSS_USD`, `ALPACA_LIVE_SHADOW_SESSIONS`, `ALPACA_LIVE_ARMING_MAX_SESSIONS`, and the two extended-hours anchors in Decision 5 — required when `ALPACA_MODE=live` **and validated within its domain** — fraction in (0, 1), USD > 0, session counts ≥ 1, basis points ≥ 0, every value finite — with no defaults in code. Presence alone is not safety: `ALPACA_LIVE_SHADOW_SESSIONS=0` or a non-finite loss bound refuses to start. At arming they are read and sealed into the envelope sha. At runtime the envelope uses the sealed values; a difference between the environment and the sealed record is `LIVE_ENVELOPE_DISAGREEMENT`, refuses ENTER, and surfaces on the live verdict — the mode-agreement rule applied to the numbers. Changing a value is therefore a re-arm, never a silent drift.

### 5. Extended hours are in scope: a session-wide decision clock and a marketable-limit anchor

Alpaca will not accept the order the programs make today outside regular hours, and the decision clock never asks them to. Both change.

1. **The leg carries the session.** `BrokerOrderLeg.extended_hours: bool = False`; `to_alpaca_order_request` forwards it. The contract validator enforces the vendor rule at the boundary: `extended_hours=True` requires `order_type=LIMIT` and `time_in_force ∈ {DAY, GTC}`, so a 422 for this cause is impossible by construction.
2. **The decision clock fires in every phase the binding names.** `decision_session` widens from `rth` to `rth | extended`; `use_rth=False` bindings are offered instead of refused. The regular session's boundaries come from the canonical calendar module and nowhere else (ADR 0022). The 04:00–20:00 ET window and the overnight session are **broker capability facts** carried on `BrokerCapabilities` and clock evidence — never calendar authority and never a literal in session logic. The feed's `BarSessionPhase` (`PRE` / `RTH` / `POST` / `OVERNIGHT`) labels each bar; the clock triggers on the timeframe within the phases the binding includes.
3. **Outside the regular session, every program leg is a marketable limit anchored to the decision bar.** Buy at `close × (1 + ALPACA_LIVE_XH_ENTRY_BPS / 10⁴)`, sell at `close × (1 − ALPACA_LIVE_XH_EXIT_BPS / 10⁴)`, `time_in_force=DAY`, `extended_hours=True`. Inside the regular session, market `DAY` is unchanged. The two anchors are environment settings under Decision 4's rule. `GTC` is not used for program orders: a ninety-day resting order outlives the decision context of the seal that made it.
4. **Unfilled is handled by machinery that exists.** An extended-hours ENTER unfilled at 20:00 ET is cancelled by the vendor; the Clerk folds the cancel through its existing terminal handling and no exposure results. An extended-hours EXIT unfilled at the same instant is an **uncertainty** under the existing uncertainty policies — surfaced, never silently re-priced; the program's next decision re-issues the EXIT at the new bar's anchor.
5. **Shadow synthesizes extended-hours fills honestly.** Outside the regular session the shadow port's `fill_model` is `limit_touch`: fill eligibility starts with the first bar **after** the decision bar (the decision bar's own range predates the order and can never fill it) and every subsequent bar is evaluated until the limit is reached or the vendor would have cancelled; a bar that reaches the limit fills at the limit. Explicit, per ADR 0002's third invariant.

This is the largest single build after shadow, and it does not depend on live existing: it lands and is exercised on the paper account first, which is why it precedes the shadow slice.

### 6. Fees: observed is truth, the model is a dated prediction

Port an `AlpacaEquityRegulatoryFeeModel` as canonical math under the Math Provenance Contract, with a golden fixture and a `docs/references/` note: SEC §31 at **$20.60 per $1 000 000** of sell notional (effective 2026-04-04); FINRA TAF at **$0.000195 per share, capped at $9.79 per trade**, sells only; FINRA CAT at **$0.000003 per share**, buys and sells; commission $0; charged end-of-day and rounded **up** to the cent. The rate table carries effective dates, because §31 changes on the SEC's fiscal calendar and a stale rate is a wrong backtest, not a rounding error.

Fees the broker actually charged arrive as `FEE` activities and are the truth the Clerk journals. The model is used for two things only: backtest cost parity on the vendor being traded, and envelope headroom. Predicted-vs-observed is reconciled per session; drift beyond the fixture tolerance is a reconciliation finding, never silently absorbed. The IBKR model stays for QC parity and is never applied to an Alpaca fill.

### 7. Margin fields are ingested to prove the bound, not to use it

The adapter ingests `multiplier`, `regt_buying_power`, `daytrading_buying_power`, `maintenance_margin`, `initial_margin`, `sma` and `last_equity` onto `BrokerAccountSnapshot` (nullable; absence is unknown). The account card renders them so an operator can see that exposure sits under cash. The envelope reads none of them.

### 8. The live verdict is server-derived, reactive and loud — ADR 0011 extended to Alpaca

The Alpaca path gains an `AlpacaLiveVerdict { account_mode, mode_agreement, envelope_agreement, armed_instances, envelope_state, shadow_state, final_verdict }` computed in the data plane and carried on the existing panel payload. `final_verdict` is `paper` | `live-armed` | `live-unarmed` | `unknown`; the Frontend renders it and never composes it. A **global Alpaca account-mode banner** replaces the silence the memo found (the present banner is IBKR market-data status and would show nothing for a live Alpaca account). On a live account the treatment is the ADR 0021 §2 kind of loud: the account id, the mode, the armed instance count, the envelope state and the session phase are always on screen. A transition out of `live-armed` observed mid-session halts new submission: new entries are refused with a `LIVE_VERDICT_TRANSITION_HALT` receipt and the verdict says why; exits keep running; re-arming restores submission (amended 2026-09-09 — a paused instance would strand its position, because PAUSED is observe-only for EXIT too).

### 9. Capabilities select by mode

`ALPACA_LIVE_CAPABILITIES` (`paper_only=False`, otherwise identical) joins `ALPACA_PAPER_CAPABILITIES`; `AlpacaBroker.capabilities()` returns by settings mode instead of unconditionally. The extended and overnight windows are added to both as capability facts (Decision 5.2).

### 10. Fault injection and developer reset refuse live

`ALPACA_FAULT_INJECTION_ENABLED` stays paper-gated, as its config comment already demands. `dev_reset` against a `real_live` or `shadow` authority is a hard refusal, not a warning — it moves authority aside rather than deleting it, and on a live account that is an incident.

### 11. The thirteen gates change meaning, not count

Each `account_mode != "paper"` refusal becomes an explicit admitted-set per context. Custody and order paths (`active_authority`, `runtime`, `account_operator_posture`, `enter`) admit `real_live` **only through an armed instance**. `manual_order_runtime` stays paper-only — manual live orders are out of scope. `historical_execution_recovery` admits live: recovery matters more on real money, not less. `cutover.BrokerCutoverEvidence.account_mode` widens to `paper | live`; the panel `broker_bots` schema widens and the OpenAPI contract is regenerated in the same change. `panel_deploy` admits live only for an armed instance. `run_admission`'s ADR 0054 corpus gate is already right: UNCOVERED parameter points stay blocked off-paper, so a live instance must be corpus-covered.

## Considered and rejected

- **Shadow under `sim:` with the real read port bound in.** Violates ADR 0042's stated isolation ("a real Alpaca port cannot be bound to it"), even though the binding code would not stop it.
- **Shadow fills written into the live account's custody journal, typed `shadow_sim`.** The reconciliation sweep would immediately and correctly flag broker-flat versus journal-long; the Clerk is the sole custody authority and cannot assert positions the broker does not hold.
- **Shadow = existing Dry Run plus read-only live observation.** Cheaper, but proves none of the live plumbing this ADR exists to prove; the first exercise of the live decision path would be with real orders.
- **`ALPACA_MODE=live` alone as arming.** The exact "config accident" D7 rejected.
- **Account-level arming.** Would let an instance that never shadowed trade on the strength of a sibling's receipt.
- **A per-order notional cap.** Owner rejected on 2026-09-07. The cash bound already bounds every order by settled cash.
- **A symbol allowlist, or one symbol at a time.** Owner rejected: every symbol a sealed program is bound to is in scope.
- **Regular-hours-only on live.** Owner rejected; Decision 5 makes extended hours real rather than merely permitted.
- **Envelope defaults in code.** Owner rejected: every value comes from the environment file, is required when live, and is sealed at arming.
- **Reduce-only fleet freeze on any envelope breach.** Owner rejected; a cash-bound refusal is one refused order and needs no account-wide state.
- **Flatten everything on loss breach.** Owner rejected; realizes the loss at the worst possible timing and takes custody decisions away from the programs that hold the positions.
- **Pause only the bot that tripped the loss limit.** An account-scoped fact driving per-bot behaviour — the defect family #1773 exists to kill.
- **`buying_power` as the cash bound.** Margin-dependent; makes an Intraday Margin Deficit possible and drags the Intraday Margin Rule into scope.
- **Extended-hours limits priced from the quote.** The programs consume bars, not quotes; a quote feed is new machinery and a second time source for the same decision.
- **Refusing extended hours for market-order programs.** Every current program is a market-order program; this would make the owner's choice a dead letter.
- **`GTC` for extended-hours program orders.** A ninety-day resting order outlives the seal's decision context.
- **Reusing the IBKR commission model for Alpaca.** Wrong vendor; wrong sign (Alpaca is commission-free with pass-through fees on sells).
- **A Frontend-composed live verdict.** ADR 0011 §7.
- **Arming that never lapses.** Correct for identity, wrong for attention.

## Consequences

**Delivery is eight slices, in this order, each its own PR with its own thermo review and targeted tests; no slice ships out of order because each is a precondition of the next, and none before slice 7 can place an order:**

1. Worlds and identity — `AccountAuthorityKind`, `shadow:`, the rename, mode agreement in settings and adapter, the required `ALPACA_LIVE_*` settings, capability selection, margin-field ingestion, the live verdict and banner. After this slice a live account is *visible* and every order path still refuses.
2. The fee model, fixture and reference note; predicted-vs-observed reconciliation.
3. Extended hours — the leg flag and validator, the adapter field, the session-wide decision clock, the marketable-limit anchor, the unfilled handling, and the `limit_touch` shadow fill model. Landed and exercised on the paper account.
4. The Shadow Account Authority, `NoSubmitAlpacaTradePort`, the shadow activation fence, the shadow receipt.
5. The envelope in `accept_enter`; the day-P&L fact; loss hold and its guarded clear; envelope agreement; the reason codes.
6. The arming ceremony and ledger; lapse.
7. Gate re-meaning, schema widening, contract regeneration. This is the slice after which a real order is possible.
8. A live qualification campaign on a small funded account, recorded like the paper campaigns.

- The five existing tests that pin `LIVE_ACCOUNT_REFUSED` keep passing wherever refusal remains (manual orders, unarmed instances, dev reset) and gain armed-path counterparts.
- `.env.example` gains every `ALPACA_LIVE_*` name with a placeholder and no value.
- `CONTEXT.md` gains the terms in the Vocabulary line; `Authority kind` is amended in place.
- ADR 0042 and ADR 0021 carry supersession notes; ADR 0011 carries an extension note.
- **Not done by this ADR:** manual live orders; short selling (every registry program is long-only, recorded as a fact); Paper or Live exposure carryover (ADR 0042's global disable stands); more than one live account per installation; a live risk envelope for anything but US equities.

## Resolved before Accepted (2026-09-07)

The draft closed with three open items. The owner answered all three, and one answer was widened by consistency:

1. *Extended hours and multiple symbols — choices or oversights?* **Choices, and stronger:** extended hours is in scope to build, not merely to permit (Decision 5); every symbol is in scope, with no allowlist.
2. *Envelope defaults — accept or set?* **No per-order cap at all.** The envelope is the cash bound and the loss hold, nothing else.
3. *Arming lapse — twenty sessions?* **A fixed count determined from the environment file.** Applied by consistency to every envelope value: all of them come from `.env`, are required when live, and are sealed at arming, so no dollar or session number lives in code. The owner named only the lapse; the extension was stated to them in the same exchange and stands unless reversed.
