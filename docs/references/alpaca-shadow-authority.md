# Alpaca shadow authority — the custody world, the session journal, the twin comparison, and the receipt

**Status:** canonical for ADR 0059 slice 4 (2026-09-08). Lineage: live.

ADR 0059 D2 says a live Alpaca account never gets a mutating Clerk until a sealed
instance has proven itself. This note records what slice 4 built to make that
true, the decisions taken inside the plan's rulings, and what was deliberately
left for later.

Every temporal value named here is `int64 ms UTC`. A trading date is one
ET-anchored instant — the calendar's session open for that day — never a string
and never a date type; see `.claude/rules/temporal-rigor.md`.

## What was built

**1. The Shadow Account Authority.** `PythonDataService/app/broker/alpaca/clerk/shadow_broker.py`
holds the three objects the world is made of: `ShadowOrderBook` (synthesized
orders and their settlement), `NoSubmitAlpacaTradePort` (a `BrokerTradePort`
over the book that holds no Alpaca client, so nothing in the module can reach
the vendor's write API), and `ShadowAccountReadPort` (the composite read port).
`compose_shadow_ports` binds one live read port into that world for one live
account. `app/broker/alpaca/clerk/shadow_authority.py` is the boot story:
`select_shadow_clerk_runtime` is what `active_authority.select_active_clerk_runtime`
calls when the broker-observed account resolves `account_mode == "live"` and no
live activation record exists for it; with one, the live authority is composed
instead (graduation, [alpaca-live-authority](alpaca-live-authority.md)). It
returns an `ActiveClerkRuntime` with `authority_kind="shadow"` — or a typed
`unavailable` runtime, never an aborted data-plane startup. `real_live` custody
is the live authority's (slice 7).

**2. The activation fence.** `app/broker/alpaca/clerk/shadow_activation.py`
gives the `shadow:` world its own append-only, sha256-sealed activation ledger,
modelled on the synthetic fence. No startup path appends to it;
`shadow_authority.activate_shadow_clerk_authority` is the one writer, and the
operator is the one caller. Without a record, a live boot refuses
`SHADOW_ACTIVATION_REQUIRED`; with a record whose `authority_generation` or
`db_identity_token` does not match the repository it names, it refuses
`SHADOW_ACTIVATION_RECORD_INVALID`.

**3. Session accounting and the gate.** `app/broker/alpaca/clerk/shadow_sessions.py`
journals each ET trading day's sweep cleanliness;
`app/services/alpaca_shadow_reconciliation.py` pairs one day's synthesized fills
against the paper twin's under the repo's divergence taxonomy and judges every
trading day since the shadow instance first ran.

**4. The receipt and the operator surface.**
`app/broker/alpaca/clerk/shadow_receipt.py` seals the passed gate into an
append-only per-instance receipt, over the shared write discipline in
`app/broker/alpaca/clerk/sealed_ledger.py`.
`PythonDataService/scripts/manage_alpaca_shadow.py` is the operator CLI, and the
existing live verdict (`app/services/alpaca_live_verdict.py`) gained the durable
observation behind its slice-1 `shadow_state` field — `observe_shadow_state` —
and a `clerk_authority` widened to `shadow`. Ruling R13: the operator surface is a CLI, not an
endpoint — an endpoint would only wrap the same functions.

## Worlds and paths

Custody is `shadow:<live_account_id>`, a reserved namespace beside `sim:`
(`account_authority.py`). It is an ordinary `ClerkSqliteRepository` account, so
it inherits the established-accounts registry, the recovery lock and the reset
registries unchanged — ruling R2: a second repository root would have needed a
parallel copy of all three.

| What | Where |
|---|---|
| Custody database, synthesized-order WAL, session journal | `accounts/alpaca/shadow:<live_account_id>/` — `clerk.db`, `simulated_orders.jsonl`, `shadow_sessions.jsonl` |
| Activation fence and receipts | `accounts/shadow/` — `shadow_activation.jsonl`, `shadow_receipts.jsonl` |
| Per-instance retained source bars | `accounts/alpaca/shadow-evidence:<strategy_instance_id>/` |

The ADR's "under `accounts/shadow/<live_account_id>/`" is satisfied in spirit by
the `accounts/shadow/` fence and receipt ledgers, which scope by row rather than
by directory; the custody database is at `accounts/alpaca/shadow:<id>/`. `shadow-evidence:` is deliberately not `shadow:`: an
evidence namespace is never a custody identity, and one function —
`account_authority.evidence_account_id_for` — chooses it for both the binding
authority and the replay proof.

`ShadowAccountReadPort` is a **composite** (ruling R3). Account, clock,
activities, assets, portfolio history and capabilities come from the live read
port; positions and orders come from the synthesized book, because the Clerk's
reconciliation sweep must reconcile the world it custodies and real positions
cannot share one reconciliation with synthesized fills. The consequence is worth
stating plainly: **a real position on the live account is invisible to the
shadow sweep and visible on the account card, which reads the broker directly.**

Two smaller wiring facts follow from the same posture. The shadow runtime
installs a null trade-updates evidence sink (ruling R15) — the live execution
stream is not shadow custody evidence, though the consumer still runs so the
stream-health gate samples the live websocket. And the Clerk-boundary liveness
recheck at ENTER now runs on the shadow authority as well as the real one: a
refusal the real Clerk would make has to appear in the paper twin too, or the
two stop reconciling trade-for-trade.

## Sharing the market-status connection with the Paper twin

The rehearsal keeps IBKR price bars and separate Alpaca custody/execution in
each worker. Alpaca may reject the second stock-data status socket with error
406, including across an owner's Paper and Live credentials
([vendor streaming contract](https://docs.alpaca.markets/us/docs/streaming-market-data)).
No additional data subscription is required by this implementation: the Paper
worker can set `ALPACA_MARKET_STATUS_UPSTREAM_URL` to the Shadow service's
`/api/brokers/alpaca/market-status-snapshot` endpoint. Both workers must share
the existing protected control-channel secret; no trading credential is sent
to this endpoint. The option is refused in Live mode.

The Paper worker polls that source instead of opening another status socket.
It still reads its own broker clock and uses its own trade-update stream.
Snapshots preserve original vendor halt/resume timestamps and retain known
halts across upstream reconnects. Missing, rejected, future-dated or stale
source evidence blocks new exposure; cached connection proof expires after
five seconds even if polling stalls. Only a status subscription or valid
status event can establish the vendor connection: connection greetings,
authentication errors and error 406 cannot briefly admit a run. Regression
coverage is in `tests/broker/alpaca/test_market_liveness.py`.

Shadow lifecycle projection and boot recovery use the same closed set of
SQLite-backed primary authorities as the other operator surfaces. The public
account route maps to its Shadow custody namespace for authority facts;
foreign accounts remain refused. Composed-runtime tests in
`tests/broker/v2panel/test_shadow_operator_surfaces.py` cover launch, stop,
resume, and recovery of a failed activation without replacing its binding.

## Fill models

The fill model is chosen by leg shape (ruling R4), and both models live in the
one canonical file `app/broker/alpaca/clerk/fill_models.py`:

- **`decision_bar_close`** — a leg without `extended_hours` fills at its bound
  decision bar's close, through the same `immediate_fill_price` the `sim:` world
  uses. A non-marketable regular limit cancels on the spot.
- **`limit_touch`** — an extended-session leg rests, and settles under
  `limit_touch_fill` against the bars its own instance retained after the
  decision bar. It cancels at the declared window's close for the decision's
  trading day — the instant the vendor would have cancelled a DAY extended
  order. A decision bar that closes outside the declared window cannot rest an
  order at all: the record would be incoherent, an order submitted at or after
  the instant it is recorded as cancelled.

Settlement is driven by **reads**, not by a background task (ruling R5): the
sweep's periodic order and position reads are the clock, and
`ShadowOrderBook._settle_locked` is the consumer of `limit_touch_fill`. A read
takes the transaction and appends only while a resting order exists; a book of
terminal orders costs a plain WAL read. With no touch, an order cancels only
once `now_ms >= cancel_at_ms + (decision_bar.end_ms - decision_bar.start_ms)` —
one bucket of slack, so the closing bucket has been retained before the book
concludes the order never touched. Once that slack has elapsed the cancel is
**final**: a bar retained afterwards that would have touched the limit does not
revive the order. That determinism is the point of R5 and it is the residual to
watch, because it is the shape of the #1921 feed-stall family.

`cancel` (ruling R6) marks a resting synthesized order cancelled and returns; on
a terminal order it is a no-op. The ADR's "no-op" means *no vendor call* — the
EXIT machine's cancel-and-prove still needs a terminal answer, and it gets one.

Provenance is durable in `SynthesizedAnchor` (`synthesized_orders.py`):
`fill_model`, `evidence_account_id`, `provider`, `bar_identity`, `bar_ref`,
`decision_bar_start_ms`, `decision_bar_end_ms`, `cancel_at_ms` for a resting
model, and `fill_bar_ref` once a bar produced the fill. A shadow fill is priced
only from its own instance's evidence: `ShadowOrderBook._evidence_namespace_for`
derives the expected `shadow-evidence:<sid>` from the order's own namespace, and
a bar retained by any other instance is refused.

## Cold start

`verify_shadow_namespace_empty` transfers ADR 0002 invariant 1 to the live
account: it holds no Clerk-minted order, ever. The check is bounded by what the
read port can see (ruling R9) — the newest page of the whole history plus the
newest page of open orders (both `limit=MAX_OPEN_ORDER_SNAPSHOT`, 500; only the
history page being full yields `SHADOW_NAMESPACE_UNPROVEN`) — and it insists on
the **live** read port, because handed the shadow port every category would
answer from the synthesized book and the check would pass vacuously on a
poisoned account.

Two refusals, both of which leave the live boot with no authority installed
rather than a permissive one:

- `SHADOW_NAMESPACE_POISONED` — any order whose `client_order_id` parses as a
  Clerk order ref already exists on the account.
- `SHADOW_NAMESPACE_UNPROVEN` — the history read reached the 500-row page
  boundary, so emptiness cannot be proven from one page. This is the sweep's own
  posture at the same boundary, and it is why a paginated order-history walk is
  a recorded follow-up: an account past 500 historical orders cannot shadow
  until one exists.

## Session accounting

`ShadowSessionRecorder` is a sweep listener; `ShadowSessionLedger` is the
append-only journal beside the shadow custody database. Rather than journal
every pass, it keeps three row kinds per ET trading date, each keyed by that
date's calendar session open in `int64 ms UTC`:

- `day_opened` — the first pass observed on that date, with the instant it
  happened.
- `non_clean` — a pass whose sweep verdict was not `clean`, deduplicated while
  consecutive verdicts repeat.
- `session_closed_clean` — the first clean pass at or after the declared
  window's close (the calendar's close when no window is declared).

A day is **complete** iff it was opened, closed clean, and was never non-clean
(`ShadowDayState.complete`). The **"opened late" rule** is separate and is the
gate's, not the journal's: if the sweep's first pass came *after* the instance's
decision session opened, the day is `sweep_opened_late` and does not count — a
process that started at noon cannot vouch for the morning.

## Twin reconciliation

`reconcile_twin_day` orders each side's fills by `(filled_at_ms, order_ref)` and
pairs them **by index**. The first failing rule classifies a pair, and the order
of the rules is itself a decision: a fill with no partner, or partners on
different symbols, is `DECISION_MISMATCH`; then different sides is
`DIRECTION_MISMATCH`; then different quantities is `QUANTITY_MISMATCH`; only
then is `|shadow.price - twin.price| > atol` `FILL_PRICE_DRIFT`. Direction is
judged before quantity because a side flip is a different decision, and
reporting it as a quantity difference would route the operator to
position-sizing when the bug is in order construction.

**The gating set is narrowed locally** to
`{DECISION_MISMATCH, DIRECTION_MISMATCH, QUANTITY_MISMATCH}` — a strict subset
of the repo-wide gating set in `.claude/rules/numerical-rigor.md`. The reason is
in the ADR: shadow proves the live plumbing, not execution quality, and
*synthesized fills are optimistic by construction*, so a shadow-vs-paper twin
comparison that gated on price would be gating on the synthesis rather than on
the port. `FILL_PRICE_ATOL = $0.01` (the taxonomy's own default) is therefore
computed, classified and **reported, never gated**; the day's
`max_fill_price_drift` rides the report. The taxonomy enum itself is untouched:
`app/research/parity/qc_reconciler.py::DivergenceCategory` stays the one
vocabulary, kept in lockstep as `numerical-rigor.md` requires.

What the pairing does **not** prove is written into the module's own Math
Provenance Contract: pairing is by sequence and shape, **never by decision bar**.
Two fills agreeing on symbol, side and quantity pair index-for-index however far
apart in the session they sit, so a day can be counted on decisions that were
not made at the same time. `max_fill_time_drift_ms` measures exactly that
distance and reports it — a diagnostic, never a divergence category and never
gating. Joining on the decision bar is a follow-up below.

**Reading the two sides.** Fills are read **per bot** —
`SqliteEconomicProjectionReader.bot_fill_window` over the ET calendar day
`[et_midnight_ms(day), et_day_end_ms(day))` — behind a memoised
`bot_economic_snapshot(...).execution_coverage == "complete"` assertion. The
account-wide `account_fill_window` was the obvious read and is the wrong one: it
refuses the whole account, time-unbounded, whenever *any* filled external order
exists, so one external fill on the paper account would fence this gate forever.
An external order is by schema definition outside every registered bot namespace
and is never a decision of the sealed program under comparison; whether the
Clerk vouches for *this instance's* executions is the question that matters, and
the coverage assertion asks exactly that. A window an authority will not vouch
for makes the day `not_evaluable` — a typed verdict carrying the reader's own
sentence, which does not count and never reads as a pass, and which does not
destroy every other day's verdict by escaping as an exception.

**A Clerk-fenced twin day reads as `DECISION_MISMATCH`,** not as a data gap: if
the twin could not decide that day, the shadow side has fills the twin side does
not, the first unpaired fill classifies as a decision divergence, and the day is
`twin_diverged`. That is the honest reading — the two worlds disagreed about
whether to trade — and it keeps "the evidence is unreadable" (`not_evaluable`)
distinct from "the evidence says they disagreed".

**Twin identity** is `twins_agree`: the configured signal hash, the action plan,
the size and the carryover policy, plus a shadow side sealed to a `shadow:`
account and a twin side that is not. It is **never** the instance id, the
account, or the outer seal hash — both of those legitimately differ between the
two worlds. Session shape is checked separately at the gate
(`shadow_binding.use_rth != twin_binding.use_rth` is a `ShadowTwinMismatch`),
because the seal carries no session shape and twins deciding on different
minutes would otherwise fail closed as a misleading twin divergence.

**A session counts** (ruling R10) when the journal shows the day opened at or
before the instance's decision session opened, closed clean after it closed, with no
non-clean pass; one run **on each side** — the shadow instance and its paper
twin — spanned the whole decision session, because a day the twin was not
running yields no twin fills and would otherwise reconcile against a silent
shadow day and count vacuously; and
the twin reconciliation has no gating divergence. The other five outcomes —
`sweep_not_clean`, `sweep_opened_late`, `run_not_covering`, `twin_diverged`,
`not_evaluable` — are named states, not silence.

**The shadow authority also rehearses the live risk envelope (ADR 0059 D4,
slice 5)**, on the live account's real cash — net of what its own synthesized
fills would have spent, so the reserved amount never double-counts a fill the
paper twin also made. An envelope refusal on the shadow side is therefore read
the same way any other one-sided decision is: it is a twin decision mismatch,
so that day does not count. See
[alpaca-live-envelope](alpaca-live-envelope.md).

## Receipt

`ShadowReceipt` carries `schema_version`, `live_account_id`,
`strategy_instance_id`, `configured_signal_hash`, `twin_account_id`,
`twin_strategy_instance_id`, `required_sessions`, the `sessions` themselves
(each a `session_open_ms` in `int64 ms UTC`, the `shadow_run_id`, and the
`reconciliation_sha256` naming that day's comparison), `written_at_ms`, and
`receipt_sha256` sealed over the rest.

The receipt proves its own invariants rather than trusting its writer, because
slice 6 trusts the receipt and not the process that wrote it: it refuses a
repeated session, a list shorter than `required_sessions`, a `required_sessions`
below 1, a `written_at_ms` or `session_open_ms` outside `[0, MAX_TIMESTAMP_MS]`,
and a `live_account_id` or `twin_account_id` naming a reserved `shadow:` or
`sim:` identity. `current(strategy_instance_id, configured_signal_hash=…,
required_sessions=…)` is the read slice 6's arming ceremony will call, and it
re-checks the seal and the count on every read — so a re-appended older row can
only answer for a caller it still satisfies, and no monotonicity guard on file
order is needed.

The receipt ledger and the activation ledger are different records with
different error types but the same file on disk, so **one write discipline**
serves both, in `sealed_ledger.py`: canonical JSON (`sort_keys`, tight
separators, `ensure_ascii`), sha256 sealing over that canonical form (each store
verifies its own rows against it on read, in its `from_payload`, using the
shared `canonical_sha256`), refusal of a symlinked or otherwise non-regular ledger
file, and a durable append that fsyncs the handle and fsyncs the parent
directory on creation. A correction to any of those — adding `O_NOFOLLOW`,
changing when the parent is fsynced — lands once instead of protecting one
ledger.

## Operator recipe

Deploying onto a live account goes through the shadow authority and says so on
the wire: `execution_mode` gained an honest `shadow` value rather than reusing
`paper` (ruling R14 — a label that lies is the ADR 0011 failure mode), and panel
deploy refuses a non-paper account whose primary custody world is not `shadow`,
naming activation as the next action. The paper twin runs on the paper host,
over the same days, sharing the shadow instance's configured signal, action
plan, size and carryover policy.

The commands below run from `PythonDataService/`. Both custody databases are
read `mode=ro` and no lease is taken; `sessions` writes nothing at all.

```bash
# 1. Once per live account, before the first shadow boot.
python -m scripts.manage_alpaca_shadow --live-account-id <ACCOUNT> activate

# 2. Any time: judge every trading day since the shadow instance first ran.
python -m scripts.manage_alpaca_shadow --live-account-id <ACCOUNT> sessions \
    --strategy-instance-id <SHADOW_SID> \
    --twin-account-id <PAPER_ACCOUNT> \
    --twin-strategy-instance-id <TWIN_SID> \
    --twin-artifacts-root <PAPER_HOST_CLERK_DIR>

# 3. When the gate holds: seal the receipt.
python -m scripts.manage_alpaca_shadow --live-account-id <ACCOUNT> receipt \
    --strategy-instance-id <SHADOW_SID> \
    --twin-account-id <PAPER_ACCOUNT> \
    --twin-strategy-instance-id <TWIN_SID> \
    --twin-artifacts-root <PAPER_HOST_CLERK_DIR>
```

**The data plane must stay up, and sweeping cleanly, until the declared
window's close.** A day is journaled `session_closed_clean` only by a clean
sweep pass at or after that close — the broker's declared window, 20:00 ET,
which is not the RTH close and does not narrow for an RTH-only instance.
Shutting the plane down at 16:00 ET therefore counts no session at all, and
the day reports `sweep_not_clean` with "the sweep did not close the day clean".

`--required-sessions` falls back to `ALPACA_LIVE_SHADOW_SESSIONS`; the repo's
`.env` does not set it today, so either the flag or the setting must be supplied
or the command refuses. Both are bounded `>= 1` — a gate of zero sessions is
satisfied by no evidence, so it is not a gate.

`--now-ms` is **both** the judging clock and the receipt's `written_at_ms`. A
replayed old value therefore backdates a receipt, but it cannot strengthen one:
it bounds the judged day range downward, and the receipt validator independently
refuses repeated sessions, a session list shorter than the required count, and a
timestamp past `MAX_TIMESTAMP_MS`.

Exit codes: `0` when the command answered (including `sessions` on an
unsatisfied gate — reporting is its whole job); `1` when the command cannot be
run as asked: a reserved `shadow:`/`sim:` identity, a binding or custody
database that is not there, a required session count nobody stated, an economic
projection the authority will not vouch for (`EconomicProjectionUnavailable`), a
receipt the sealer will not accept, or a usage refusal — an absent flag, an
unknown subcommand, a flag outside its bound; `2` when `receipt` found the gate
unsatisfied, or when the named twin is not this instance's twin
(`SHADOW_TWIN_MISMATCH`). Every invocation writes exactly one JSON object to
stdout, and every temporal value in it is `int64 ms UTC`, so a script reads the
`error` key rather than inferring a cause from an exit code. (`--help` is
argparse's own usage text and exits `0`; it runs no command.)

**The verdict banner.** `GET /api/brokers/{broker}/live-verdict` reports
`clerk_authority="shadow"` and a `shadow_state` of `none` / `in_progress` /
`complete`, and its headline reads `LIVE account <id> — shadow authority active,
no instance armed`. The headline deliberately names the **live**
account: `shadow:` is the runtime's custody namespace for the same account, not
part of its number.

`shadow_state == "complete"` is **account-wide**: `observe_shadow_state` asks
`ShadowReceiptStore.any_for_account(live_account_id)`, while slice 6's arming
gate will ask the instance-scoped `ShadowReceiptStore.current(...)`. An operator
can therefore read "complete" for an account whose second instance is still
mid-shadow. Per-instance progress on the verdict is a follow-up (ruling R12).

The endpoint **propagates a corrupt receipt ledger or session journal as a 500,
by design**: `observe_shadow_state` raises rather than answering `"none"`,
because a corrupt proof is not the absence of one and must never render as "no
progress yet". Only invalid Alpaca settings are absorbed, into the
`"unconfigured"` verdict.

Graduation — the live cutover — is the step after the receipt; stop the shadow
instances first, because after it they are foreign to the live authority.

## Validation

`PythonDataService/tests/broker/alpaca/clerk/test_shadow_broker.py`,
`test_shadow_activation.py`, `test_shadow_sessions.py`, `test_shadow_receipt.py`,
`test_synthesized_orders.py`, `test_account_worlds.py`, `test_active_authority.py`;
`PythonDataService/tests/broker/alpaca/clerk/sqlite/test_runtime_shadow.py` and
`test_qualification_shadow_trace.py`;
`PythonDataService/tests/services/test_alpaca_shadow_reconciliation.py`,
`test_alpaca_live_verdict.py`;
`PythonDataService/tests/routers/test_alpaca_live_verdict_endpoint.py`;
`PythonDataService/tests/scripts/test_manage_alpaca_shadow.py`;
`PythonDataService/tests/broker/v2panel/test_panel_deploy_shadow.py`;
`PythonDataService/tests/contracts/test_alpaca_active_authority_wiring.py`;
`Frontend/src/app/components/broker/broker-deploy-page/alpaca-deploy-workflow.component.spec.ts`.

## Follow-ups (not this slice)

- **A paginated order-history walk.** Cold start proves an empty Clerk namespace
  from one 500-row page; an account past 500 historical orders refuses
  `SHADOW_NAMESPACE_UNPROVEN` and cannot shadow until the walk exists.
- **The decision-bar join for the twin comparison.** Pairing is by sequence and
  shape today; `max_fill_time_drift_ms` reports how far apart the paired fills
  sat but never gates. Joining on the decision bar itself would make "the same
  decision" provable rather than inferred.
- **Overnight (20:00–04:00 ET).** A separate venue and separate data; not a
  decision phase, so not a shadow phase.
- **Per-instance shadow progress on the live verdict.** `shadow_state` is
  account-wide today (see "Operator recipe").
- **Slice 6 arming consumes `ShadowReceiptStore.current`.** Since slice 7 the
  arming ceremony records a current receipt when the instance holds one and
  arms without one (shadow is a mode, not a requirement — owner decision
  2026-09-09); `LIVE_SHADOW_INCOMPLETE` stays defined in `live_arming.py` for
  the verdict's vocabulary. This slice produces the receipt
  that gate reads; the gate itself is
  [alpaca-live-arming](alpaca-live-arming.md).
