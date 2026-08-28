# CONTEXT — Live trading and operator glossary

Canonical language for the **live trading and operator domain**: bots, brokers,
custody, execution, and the surfaces an operator acts through. This file is a
**glossary only** — no implementation detail, no spec, no decisions. Decisions
live in `docs/architecture/adrs/`.

Repo-process vocabulary — ADR status values, lint rules, CI gates, branch and
review conventions — is **out of scope** and does not belong here.

**Lineage labels.** The ADR 0038 control-plane and ADR 0037 custody retirements
are complete. Every section declares which system its terms describe (decision
record: ADR 0040).

- **live** — the current Alpaca Broker V2 ecosystem.
- **historical (ADR 0038)** — removed IBKR bot-control machinery.
- **compatibility evidence (ADR 0038)** — read-only IBKR evidence and durable
  historical schemas preserved after #1583; never broker actuation or bot control.
- **historical (ADR 0037)** — removed Alpaca legacy-JSONL custody machinery.
- **neutral** — operator/trading vocabulary that survives a broker change.

The two retirements are **independent** and either may land first, so they never
share a label; a section is archived only when *its own* trigger fires. Nothing
is archived while the code it names still runs.

## Identity ladder

**Lineage: live.**

- **strategy_key** — algorithm family (e.g. `ema_crossover_signal`; `spy_ema_crossover` remains a legacy compatibility key).
- **strategy_instance_id** — one *configured* instance of a strategy_key. The
  unit the operator actually governs. Owns the `bot_order_namespace` and the
  durable control-intent sidecar. One strategy_key → many instances; one
  instance → many runs over time. Its configuration is immutable: changing
  strategy semantics creates a new strategy-instance identity.
- **Bot name / strategy instance ID** — one canonical identity for a deployed
  bot. The deploy flow may prefill a random, trader-editable name, but the final
  value is lifetime-unique, system-safe, and is the durable
  `strategy_instance_id` used for paths, ownership, broker attribution, and
  operator-surface identity. There is no separate display-only bot-name
  variable.
- **run_id** — a single execution (one process lifetime) of an instance. An
  artifact-storage key, **not** the operator's handle.
- **Current run** — the newest run currently bound to a strategy instance. It is
  shown first on the bot's operator surface but does not replace the instance as
  the operator's durable identity. Advancing this binding never rewrites an older
  run.
- **Current run binding** — the mapping `strategy_instance_id → currently bound
  run_id`. It is a *replaceable* pointer, never liveness proof and never terminal
  proof. A **stale run selection must never be the operator's primary control
  surface**.
- **Run history** — the append-only sequence of current and previous runs for
  one strategy instance. A historical run remains inspectable but cannot become
  a command target merely because an operator selects it for viewing.
- **Run terminal receipt** — create-once evidence from the owning backend that
  a specific run stopped, crashed, or exited without verification. Repeating
  the same write preserves the original receipt; conflicting terminal evidence
  is rejected. The UI may not invent terminal wording when this evidence is
  absent.
- **Continue** — allow an existing paused, still-live run to proceed. It keeps
  the same `run_id`.
- **Resume** — create and bind a new run of the same immutable strategy
  instance after its prior run stopped. Resume never edits the instance
  configuration. Any legacy use of “Resume” to mean continuing a paused live
  process should be renamed to **Continue**.
- **Dry run** — an explicitly non-submitting strategy run that consumes real
  market data and produces simulated decisions/fills without sending broker
  orders. Submission mode is part of immutable instance configuration, so
  changing an existing live-paper instance into a dry-run instance creates a
  new `strategy_instance_id`.
- **Sealed custody account** — the exact Clerk account captured in the final
  Start custody snapshot and made immutable with the strategy instance. Every
  later Resume and Clerk registration must match it; a legacy instance without
  a seal is readable evidence but cannot be resumed into newly selected custody.
- **Validation admission fact** — the Start/Resume-time receipt that re-reads
  the active human validation event and re-hashes each referenced validator,
  settings, and audit artifact. A rendered deploy page is not this fact; stale,
  missing, or unreadable proof refuses a new run.
- **Command intent identity** — the durable identifier for one operator command
  intent across transport retries. Reusing it with the same action and payload
  asks for the original outcome; reusing it with a different action or payload
  is a conflict.
- **Configuration hash** — the content hash of a strategy instance's immutable
  configuration. Two records agreeing on it describe the same configuration; a
  differing hash under the same `strategy_instance_id` is a conflict, never an
  update. It is what makes an instance's immutability checkable rather than
  merely asserted.
  _Avoid_: config version, spec version (neither is a content hash).
- **Launch reason** — why a run was created: a fresh deploy, a Resume of a
  stopped instance, or a legacy record lifted from an older artifact. Recorded
  on the run, never on the instance.

## Broker-facing identity (sharpened 2026-06-04)

**Lineage: live.**

How a fill is attributed to a strategy. The durable chain, distinct from the
ephemeral session id:

- **intent_id** — engine-generated, one per trading intent, created *before*
  the order is placed. The write-ahead idempotency key, and the primary key of
  the intent record the custody authority holds.
- **bot_order_namespace** — `learn-ai/{strategy_instance_id}/v1`. The
  per-instance ownership scope (unchanged; predates this work). The **`/v1` is
  the `order_ref` *wire-format* version — not a strategy, config, spec, or model
  version.** It versions only how `namespace:intent_id` is encoded into the
  broker's attribution field (delimiter/escaping, intent-id encoding, added
  segments, parse shape). It does **not** bump for parameter changes, code
  changes, spec-hash changes, retunes, or new run_ids — those live in the run's
  own configuration record and `strategy_instance_id`. A bump to `/v2` requires
  an ADR/migration note **and
  dual-read ownership** (recognize both `/v1` and `/v2` as owned until every
  prior-version broker order is closed/reconciled) — otherwise the bot
  classifies its own open orders as foreign and self-poisons.
- **order_ref** — `{bot_order_namespace}:{intent_id}`. The broker-facing
  attribution string the Clerk mints onto the broker's own attribution field
  (IBKR `orderRef`, Alpaca `client_order_id`), echoed back on
  open-order/execution callbacks. **The single ownership-proof identity.**
  _Avoid_: `client_order_id` (retired internally — the name encoded the wrong
  model and trained the `live-{order_id}` mistake; kept only as a transitional
  alias at external compatibility edges, if any).
- **Manual operator order** — an account-scoped paper order initiated through
  the operator ticket, not a deployed strategy. Its identity uses the broker's
  `manual/{operator}/v1:{intent_id}` namespace and it is accepted only through
  the Account Clerk's durable intent/acknowledgement lane. It has no fabricated
  bot binding; the Clerk journal is its canonical submit receipt and its broker
  callbacks retain the same `order_ref`.
- **Custody subject** — an immutable Account Clerk identity for the economic
  actor whose commands, effects, positions, holds, and uncertainty are being
  projected. A `BOT` subject is bound one-to-one to a strategy instance; a
  `MANUAL_OPERATOR` subject is bound one-to-one to an approved operator. A
  manual subject never creates a pseudo-bot, run, or strategy binding.
- **Order history** — a read-side transaction-history projection over canonical
  Clerk receipts and broker callbacks. It is not a fresh broker sweep and does
  not become an authority for order state. The operator view may show its full
  receipt/evidence details; the trader view receives only the backend-authored
  outcome suitable for action.
## IBKR order-attribution ladder (sharpened 2026-06-04)

**Lineage: historical (ADR 0038; actuation retired by #1583 on 2026-08-19).**

The former IBKR-side half of broker-facing identity: the broker's own order
handles, the run-scoped write-ahead log that backed them, and the reconciler
rules built on top. The executable submit/cancel runtime is absent; these terms
remain only for interpreting durable historical evidence. The live Alpaca half
is **Broker-facing identity** above.

- **intent ledger** — a *reconstructed logical view*, **not a stored artifact**.
  Its system of record is the run-scoped WAL (`intent_events.jsonl`) folded over
  the instance-scoped projection (`live_state.json`'s `submitted_orders`, keyed
  by `intent_id`); the fold replays WAL events after the projection's
  `last_intent_wal_seq` cursor (a per-run monotonic sequence number, never a
  wall-clock timestamp). There is no third store: ADR-0001's substrate is
  unchanged.
  An `intent_ledger.py` module may hold the *pure fold helpers* (append/read WAL
  events, fold over the `LiveStateEnvelope`, build the in-memory view the
  reconciler and halt logic read) but persists nothing of its own.
- **ib_client_id** — the `clientId` one bot uses on the IBKR Gateway connection,
  pinned per `strategy_instance_id` so executing and shadow processes never
  collide. One Gateway, many clientIds.
- **perm_id** — IBKR's stable per-TWS-order handle, captured post-submit.
- **exec_id** — per-partial-fill id; dedupes fills.
- **order_id** — IBKR's ephemeral, session-scoped order id. **Convenience for
  same-session API calls only; never an attribution key.** Deriving ownership
  from `live-{order_id}` is the bug class this ladder retires.
- **submit_mode** — the broker-adapter-level switch on a live run: `live_paper`
  (route through the IBKR adapter, a real order is placed) or `shadow` (route
  through the no-submit adapter, no broker order exists). Part of the hashed
  `live_config`, so changing it mints a new run identity.
- **execution_source** — which world produced an execution row: `broker_fill`
  (came from IBKR) or `shadow_sim` (synthesized by the no-submit adapter).
- **Layer A divergence** — did broker execution diverge from what this live run
  intended, on the same data? Slippage, latency, missed/extra/partial/rejected
  fills, commission drift. Meaningful only for a submitting run.
- **Layer B divergence** — did the live run's observed world diverge from the
  canonical research world when the same session is replayed against archived
  bars? Data drift, indicator-state drift, decision drift, coverage gaps.
  Meaningful for submitting and shadow runs alike.

### Owned orphan vs outside mutation (sharpened 2026-06-04)

The reconciler's two failure attributions, kept strictly distinct because they
route to opposite actions:

- **Owned orphan** — "I lost my receipt, but the broker `orderRef` proves this
  is mine." A broker order/fill whose parsed `order_ref` namespace exactly equals
  *this instance's* `bot_order_namespace` but whose `intent_id` is absent from the
  projection
  (a crashed-submit before flush). The namespace match is **stronger evidence
  than the stale projection** — the projection is *allowed* to lag; that lag is
  why the WAL exists. Verdict: **adopt, do not poison.** Bounded adoption:
  parse + verify `intent_id`/namespace, capture broker fields (`order_id`,
  `perm_id`, status, qty, filled, avg fill), append an `ADOPTED_BROKER_ORDER`
  event to the *new* run's WAL, fold into the projection keyed by `intent_id`,
  and **persist `live_state.json` before allowing any new submission.**
- **Outside mutation** — "Broker state cannot be attributed to this bot
  instance." An order/fill with an *unknown* namespace, no `order_ref`, or a
  foreign `perm_id`. Verdict: **poison/refuse.**

Adoption is not unconditional resume: an adopted order that is still
active/partially filled and creates **ambiguous exposure** vs expected strategy
state → **pause / refuse new orders pending operator reconciliation** (still
classified owned-orphan, never outside-mutation).

### Submit-uncertain halt (sharpened 2026-06-04)

`ACK_FAILED` is not "the order failed" — it is **"the broker side effect is
unknown"** (Schrödinger's order: `placeOrder` may have reached IBKR before the
ack/echo was lost; IBKR does **not** dedupe by `orderRef`). So the durable WAL
is a **submit-lifecycle state machine**, not three flat events:

- `PENDING_INTENT` → `SUBMITTED` (clean ack) **or** `ACK_FAILED_UNCERTAIN`.
- From uncertain, an **in-session resolution** (stop all new submissions; after a
  bounded settle, probe the broker by `order_ref` via the namespace-scoped calls)
  yields one of three, on a `PRESENT`/`PROVABLY_ABSENT`/`NOT_PROVABLE`
  discriminator: `SUBMITTED_RECOVERED` (any open/completed order or execution
  carries our `order_ref` → adopt, continue only if exposure reconciles),
  `INTENT_NOT_ACCEPTED` (**provably absent** = both probe calls returned and
  neither carries our `order_ref` → retry **at most once** reusing the same
  `intent_id`/`order_ref`, `RETRY_CAP = 1`; a second uncertain → halt), or
  `SUBMIT_UNCERTAIN_HALTED` (unreachable / probe error / ambiguous → halt, defer
  to cold-start). Halt is the default under any uncertainty.
- Cold-start treats an unresolved `ACK_FAILED_UNCERTAIN` / unacked
  `PENDING_INTENT` the same way: resolve by `order_ref`, then
  adopt / discard / poison.

**WAL read contract:** only a single *trailing* unterminated line is tolerated on
read (fsync-before-`placeOrder` proves no side effect for it); any other
malformation **poisons**, and a complete un-acked `PENDING_INTENT` is resolved,
never dropped.

**Banned:** blind re-submit. Retrying with a *new* `intent_id` double-submits if
the order had landed; retrying with the *same* `order_ref` is safe **only** once
the order is proven absent. The 1:1 `intent_id ↔ order_ref ↔ broker order`
invariant is never weakened to paper over an uncertain ack.

**Invariant:** when both components are present,
`order_ref == f"{bot_order_namespace}:{intent_id}"`. For an order **we placed**,
reconciliation stores these as separate fields and *validates* the equality — no
parse. For a **broker-sourced** `order_ref` (orphan / outside-mutation
classification) only the echoed string exists, so it is parsed on the **final**
`:` and the namespace compared by **exact equality** against the allowed set
(never `startswith` — `…/v10` must not match `…/v1`).

**`intent_id` encoding & `order_ref` length:** a `uuid4` whose 16 bytes are
base64url-encoded without padding → a 22-char token (vs 36 for the hyphenated
form). base64url's alphabet (`A-Za-z0-9-_`) never collides with the `/` and `:`
delimiters, so a last-`:` split parses `order_ref` unambiguously. `order_ref`
length is **bounded, not assumed**: fixed overhead is 35 chars and
`strategy_instance_id` may be up to 128, so once the IBKR cap `C` is verified (on
one live paper order, before committing — truncation is silent), building over `C`
fails closed and a broker-owned instance must satisfy
`len(strategy_instance_id) ≤ C − 35`.

### Uniform ownership ladder (sharpened 2026-06-04)

**Every** broker order — strategy submit *and* every flatten/liquidation path
(recovery, shutdown, force-flat, emergency) — enters the *same* identity ladder:
mint `intent_id` and stamp `order_ref`. **In-process run-owned** paths also append
to the live WAL; the **out-of-process emergency-flatten** (engine dead, no safe
concurrent writer) instead writes a separate `emergency_flatten_audit.jsonl` — a
later cold-start adopts it by namespace. Ownership is decided **only** by, in
order:

1. `order_ref` namespace — parsed on the final `:`, compared by **exact equality**
   (never `startswith`; `…/v10` must not match `…/v1`) against this instance's
   allowed-namespace set (one element, or `/v1`+`/v2` during dual-read),
2. known `intent_id` (in projection / WAL),
3. known `perm_id`,
4. known `exec_id` (fill dedupe).

`order_id` alone **never** proves ownership. **Provenance is not identity:**
`intent_kind` (`STRATEGY` | `RECOVERY_FLATTEN` | `SHUTDOWN_FLATTEN` | `FORCE_FLAT`
| `EMERGENCY_FLATTEN`) + `reason` are recorded for humans, but ownership must
never branch on those strings. This retires `recovery-flatten-*`,
`emergency-flatten-*`, and `live-{order_id}` as identity mechanisms.

## Trader-facing console vocabulary (sharpened 2026-05-30)

**Lineage: neutral.**

How a console speaks to a trader. These rules bind whichever console is current,
so they survive a broker change.

- **Trader-facing event language** — Bot Cockpit rows, cards, panels, badges,
  and section summaries use human-readable labels and explanations. Raw
  event/type codes such as `endpoint_snapshot` or `account_positions` are
  diagnostic evidence only; they may appear in an expandable technical-details
  area, but never as the primary text a trader has to interpret.
- **Backend-authored trader narrative** — trader-facing explanations for broker
  activity, audit evidence, incidents, and reconciliation states are authored by
  the backend from structured facts. The frontend renders the authored language
  and may format layout, ET display time, numbers, badges, and expansion state,
  but it does not decide what a broker or audit event means.
- **Event narrative registry** — the closed backend vocabulary of trader-facing
  event meanings. Each supported event meaning has a human label, explanation,
  severity/attention level, and diagnostic facts that can be expanded for audit.
  Unsupported event meanings fail visibly as unmapped diagnostics instead of
  being guessed by the Bot Cockpit.
- **Exchange-time display** — primary Bot Cockpit tables, panels, and audit
  summaries display market/session times in `America/New_York` (ET), matching
  the U.S. market clock the bot trades. Canonical `int64 ms UTC` remains the
  storage and wire format, and may appear in expandable technical/audit details
  when exact forensic evidence is needed.
- **Market Pulse** — the persistent Bot Cockpit header summary of required
  market-data availability and freshness. It shows a backend-authored
  `LIVE`/`STALE`/`MISSING` state, the latest market-data time in ET, and its age;
  it never turns transport reachability alone into proof that usable data is
  current.
- **Backend-authored folding** — repeated Bot Cockpit rows or panels are folded
  only when the backend supplies a stable fold identity and count. The frontend
  must not infer sameness by comparing rendered text, raw JSON, timestamps, or
  partial event fields; it renders the authored fold key/count and preserves the
  individual evidence rows inside expansion.
- **Activity structural cluster** — the backend-authored identity that groups
  related Activity rows under one logical order or execution family, such as
  partial fills under the same broker order. This is distinct from duplicate
  noise folding; clustering explains structure, folding suppresses repetition.
- **Usable activity row** — a Bot Cockpit Activity update worth changing the
  visible table: a broker fill, order lifecycle event, trader-relevant broker
  evidence summary, or incident that changes what the trader can understand or
  act on. Low-level polling/file-refresh churn is diagnostic evidence, not a
  reason to redraw the primary table.
- **Stable activity stream** — the Bot Cockpit Activity table is updated by
  incrementally merging backend-authored rows or fold-count changes by stable
  visible-row identity. Parent panels stay mounted; row expansion state, scroll
  context, and table identity are preserved. Visible highlighting or motion
  occurs only when a usable row is added or a visible fold count changes.
- **Configuration vs audit boundary** — Configuration shows what the bot was
  intended and configured to run with. Audit shows evidence of what actually
  happened and whether that evidence supports the intended configuration. The
  same raw fact should not be duplicated as primary content in both places; if
  needed, one surface may link to or summarize the other as provenance.
- **Closed-trade summary** — a trader-readable round-trip summary derived from
  durable trade artifacts. It is not a broker execution row and must not be
  counted as another fill; it references the constituent fill evidence that
  produced the round trip.
- **Validated strategy package** — the deployable unit for live-paper bots. It
  immutably binds a strategy implementation/spec, approved settings, golden
  fixture/parity evidence, and required backtest/audit provenance by content
  hash. The Deploy a strategy page owns creating or selecting this package;
  Engine Lab is not the package-authoring surface for this workflow.
- **Strategy package settings** — package-specific tunable settings exposed as
  named, human-readable controls only when the selected validated strategy
  package requires them. Raw settings-file paths are technical provenance, not a
  normal trader input.
- **Connected broker account** — the broker account currently observed through
  the connected broker session. Deploy displays this account as read-only
  evidence and fails closed when the account is unavailable or ambiguous; traders
  do not type broker account identifiers into the deploy form.
- **Strategy Lab portfolio account** — a research/simulation account in the
  portfolio domain, distinct from a broker-reported trading account. It is not
  a Broker Account Authority and the two account domains are not unified.
- **Trader-readable instrument picker** — Deploy action plans use rich,
  trader-friendly stock and option selectors instead of raw symbol/contract
  entry rows. Stocks surface recognisable symbol/company/exchange context when
  available. Options surface underlying, expiry, strike, call/put, multiplier,
  and market quote context when available; raw contract identifiers remain
  technical details.
- **PrimeNG-first cockpit UI** — Bot Cockpit and Deploy UI should prefer PrimeNG
  components for tables, accordions, badges, panels, forms, dropdowns, pickers,
  and dialogs, with custom CSS limited to layout and theme glue. Apache ECharts
  remains appropriate for charting. Existing bespoke controls should be replaced
  with PrimeNG only inside the narrow surface owned by the current slice, when
  the replacement is straightforward and preserves behavior.
- **Theme-token evidence styling** — broker/audit evidence surfaces use the
  app's theme tokens for contrast, severity, spacing, and emphasis. One-off
  hard-coded colors are avoided; PrimeNG components should be styled through the
  app theme/token layer so evidence panels remain readable in the supported
  themes.

## Account authority and custody proofs (sharpened 2026-05-30)

**Lineage: live.**

- **Broker Account Authority** — the account-scoped safety and audit boundary
  for one broker-reported paper or live trading account. It governs every bot
  bound to that account; it is a domain seam, not another runtime service. Its
  Account service remains present while the approved broker account is
  connected, including when no bots are on duty.
- **Synthetic Account Authority** — the account-scoped safety and audit
  boundary for an explicitly activated `sim:` account. It may exercise Clerk
  custody semantics for a Dry Run, but it has no claim on a broker-reported
  account or its exposure.
- **Authority kind** — the closed account-world label `real_paper` or
  `synthetic` carried with an authority-scoped read. It prevents an operator
  view or aggregate from presenting simulated and broker-paper facts as one
  account truth.
- **Sealed account** — the exact account identity committed by a bot's
  immutable configuration. A run can register only with the same Account
  Authority; a mismatch is a refusal before any run or custody work exists.
- **Account service standby** — the healthy idle state of an attached Broker
  Account Authority with no bots on duty. Observation and reconciliation
  continue in the background, so standby is ready rather than fenced.
  _Avoid_: idle Clerk, no active bot, unattached account
- **Approved-account pin** — durable operator approval of the exact
  broker-reported account this installation may operate. Paper/live mode alone
  is not sufficient account identity.
- **Account observation proof** — a fresh, clean Account Truth assessment for
  one broker account. It proves that current account state is attributable and
  may include non-zero account exposure attributed to an active strategy
  instance; it is the right proof for ongoing trading permission, not a claim
  that the account is flat.
- **Account exposure** — the broker-observed net position of the connected
  trading account. A bot does not own a broker position.
- **Broker inventory baseline** — an operator-confirmed, fresh broker-position
  snapshot that begins a new account-exposure accounting cutover without
  deleting earlier Clerk history or assigning the inventory to any instance.
  Pre-cutover instance attribution is retired as current custody, while its
  fills remain visible as historical evidence.
  _Avoid_: trade deletion, synthetic fill, bot position.
- **Instance-attributed account exposure** — the Account Clerk's projection of
  the portion of account exposure supported by exact order and fill identity
  for one `strategy_instance_id`. _Avoid_: bot exposure, bot-owned position.
- **Bot process fact** — the bot runner registry's typed observation of whether
  one bound run has a currently owned process (`RUNNING`, `STOPPING`, `EXITED`,
  or `UNKNOWN`). It proves process presence only; it does
  not prove broker custody, order state, exposure, or permission to trade.
- **Clerk custody snapshot** — the Account Clerk's typed, fresh-or-explicitly-
  stale answer for one strategy instance about reconciled broker positions,
  working orders, pending orders, terminal orders, unresolved effects, holds,
  and attribution. Every count and exposure remains explicitly `unknown` when
  the Clerk cannot prove it; unknown is never presented as zero. Account facts
  may feed the Clerk internally, but callers do not combine a second independent
  account interpretation with this snapshot.
- **Start admission decision** — the backend-authored answer to whether a new
  run may start for one immutable strategy instance. It is a pure function of
  the bot process fact, validation admission fact, and Clerk custody snapshot;
  market-data readiness is carried inside the bot-side facts together with
  runner boot-recovery and restart-intensity evidence. Preview and execution
  call the same typed policy, and Angular renders its explanation without
  recreating safety logic.
- **Start custody fence** — the Clerk intake lock held across the final Start
  decision and run activation. The fence proves that no new Clerk effect can
  change the exact custody journal cut between admission and activation. If the
  cut cannot stabilize, Start is refused without writing a bot binding.
- **Clean strategy exit** — a terminal Clerk effect proving that working entry
  and exit orders are resolved and the instance-attributed account exposure is
  zero. An exit that deliberately leaves exposure open is a carryover stop, not
  a clean exit.
- **Carryover stop checkpoint** — the durable Clerk-backed account-exposure
  evidence that a future individually qualified program may use when an
  approved STOP leaves instance-attributed exposure in place. Alpaca Paper
  carryover is currently globally disabled: no current program or setting may
  create an approved checkpoint or resume exposure from one.
- **Resume custody proof** — a fresh proof that immutable strategy
  configuration, current Clerk attribution, and broker account truth exactly
  match the carryover stop checkpoint. A new run may attach to the stopped
  instance only when this proof passes.
- **Account recovery proof** — the stricter account reconciliation receipt used
  for recovery actions such as freeze clearing and ADR-required flat starts.
  It combines observation proof with accepted resolved exposure/flatness and
  must not be used to stop a healthy bot merely because account exposure is
  attributed to its strategy instance.
- **Recovery-required broker exposure** — a current broker position or working
  order attributable to a known retired bot but lacking an active manager. It
  is known rather than foreign, yet blocks ordinary account trading until
  revived, resolved, or explicitly overridden.

## Instance console mechanics (sharpened 2026-05-30)

**Lineage: historical (ADR 0038; retired 2026-08-18).**

The shape of the live-instances operator console. Its live successor surfaces are
the Broker Desk, the bot panel, and the Bot Gallery.

- **Bot Cockpit** — the trader-facing name for the live-instances
  deployed-strategy operator console (`cockpit-v2` in implementation docs). The
  surface where a trader monitored and controlled one `strategy_instance_id`
  before the Alpaca **bot panel** took that role.
- **Instance control room** — the operator console's correct shape. Its subject
  is the **strategy_instance**; the **current run** and its artifacts are
  attached as *evidence*, not as the object being operated. Contrast with the
  current implementation, which behaves like a *run artifact viewer with
  controls attached* — the thing we are correcting.
- **Readiness gate** ("can this strategy act on the next bar?") — an
  **instance-scoped** composite verdict computed from: current run binding,
  desired state, process state, broker-observed state, safety flags, hydrate
  status, and artifact freshness. (Detailed inputs tracked in the design, not
  here.)
- **Operator top-strip ladder** — `INSTANCE / PROCESS / CURRENT RUN / DESIRED /
  BROKER`. Reads as an instance being operated, not a run being viewed.

## Binding authority (resolved 2026-05-30)

**Lineage: historical (ADR 0038; retired 2026-08-18).**

Four distinct sources, never conflated:

- **Live binding** — `strategy_instance_id → live bound run_id | null`. Owned by
  the **process registry** (process truth: pid, state, start/exit). "Live" is a
  *process fact, not an artifact fact* — only the registry can prove a process
  is alive and currently writing a run. The registry carries
  `strategy_instance_id, run_id, run_dir, process state, pid, start time, exit
  state`.
- **Evidence binding** — `strategy_instance_id → latest evidence run_id | null`.
  *Derived* from the run scan / ledger index. Used to render artifact panels
  when no process is live; always labeled as stale/completed evidence. **Never a
  command-routing authority.**
- **Durable operator intent** — the desired-state sidecar (see below).
- **Run artifacts** — evidence only.

Commands route **only** to a live binding. No live binding → command controls
disabled; evidence panels still render, labeled "latest completed/stale run."
Liveness is resolved **server-side** and returned with names that make misuse
hard (`live_binding` vs `evidence_binding`) — the client never scans runs to
infer liveness.

## Operator intent — single knob (resolved 2026-05-30)

**Lineage: live.**

**Durable desired-state is the single operator intent knob**, with one
liveness-independent semantic:

- **PAUSED** — strategy should not make new decisions/orders.
- **RUNNING** — strategy may act when readiness gates pass.
- **STOPPED** — strategy must not restart without explicit operator change.

**Invariant** — any live actuation of PAUSE/CONTINUE/STOP must leave the durable
intent at the same semantic state as the action it executed. This makes
"paused-but-still-trading" structurally hard: durable state changes first, live
actuation is queued, the UI shows pending/acked actuation against the same
intent.

The knob is the same fact **Control intent** names under "Bot control plane"
below, and it is deliberately not held by the custody authority: a stopped bot
must refuse to restart itself even when that authority is unreachable.

## Live-instances intent endpoint and command channel (resolved 2026-05-30)

**Lineage: historical (ADR 0038; retired 2026-08-18; routes and code deleted by
PR-B of #1813, 2026-08-27).**

The transport the live-instances plane put in front of durable operator intent.
The *concept* — operator intent as a single durable knob — is live; see
"Operator intent — single knob" above. The transport described here is not:
`routers/live_instances.py` and the command channel were deleted, and no
`/api/live-instances` route is registered any more.

The intent endpoint (`POST /api/live-instances/{id}/desired-state`) used to:
(1) write durable intent first; (2) if a live binding existed, enqueue the
matching live actuation command to that run; (3) return both durable-write
status and live-actuation ack pointer; (4) with no live binding, return
"durable only; will gate next start."

**Writer contract, as it stood:**
- *Primary writer* — `/api/live-instances/{id}/desired-state`. **Retired**; the
  Alpaca Broker V2 bot-action surface (`POST
  /api/brokers/{broker}/accounts/{account_id}/bots/{sid}/actions`) is the live
  successor and owns its own intent contract.
- *Reconciling writers* — the engine command dispatcher and CLI emergency
  controls. They persisted intent as **reconciliation, not primary ownership**;
  same-value/idempotent writes were acceptable (version churn, not semantic
  drift).

**One-shot command channel** is reserved for true one-shot operations:
`FLATTEN_NOW`, `RECONCILE_NOW`, `MARK_POISONED` (and maybe `DUMP_STATUS` later).
`PAUSE`/`CONTINUE`/`STOP` are removed from that one-shot channel. They are
first-class Bot Cockpit controls only when the backend's capability projection
renders them as available; the UI never invents availability. Legacy `resume`
remains a backend-compatible wire verb for the same-run Continue control only
until the vocabulary migration is complete.

**Command lifecycle** (operator vocabulary; one row per command, not
pending-files-plus-ack-files): `reserved` → `accepted` / `in_progress` →
`succeeded` | `failed` | `rejected`, with `unknown` retained when the effect
cannot yet be proved and may later reconcile to a terminal result. Staleness is
judged against backend-authored freshness evidence, not a client-side constant.

## Readiness gate (resolved 2026-05-30)

**Lineage: historical (ADR 0038; retired 2026-08-18).**

"Can this strategy act on the next bar?" is an **instance-scoped, structured
verdict** — never a boolean, never recomputed from artifacts by the UI.

- **Live-readiness is engine-authored.** *Engine owns it, backend transports it,
  UI renders it.* The verdict is emitted by the **same runtime path that
  enforces the gates** — otherwise the UI becomes a second control
  implementation and will eventually lie (the repo's single-source-of-truth
  principle, applied to operator state).
- **Start-readiness is backend-derived** for dead instances, computed from
  durable artifacts (`desired_state`, halt/poison sentinels, hydrate, latest
  reconcile receipt). **Must be labeled `start_readiness`, not
  `live_readiness`.**

**Shape:** `{ kind: "live_readiness" | "start_readiness", as_of_ms, source:
"engine" | "backend_derived", verdict, summary, gates: [{ name, status:
pass|fail|unknown, severity: hard|soft, detail }] }`. Start-readiness also
carries `live_readiness_available: false`.

**Verdict rules:**
- `READY` — all hard gates pass, no material soft warnings.
- `BLOCKED` — at least one hard gate fails.
- `DEGRADED` — hard gates pass, but soft gates warn/unknown.
- `UNKNOWN` — no authoritative readiness source.

Gate inputs (finding #7): `desired_state`, `broker_connection`,
unexpected-position (namespace-scoped self-consistency), submission mode
(readonly/shadow/live), `orders_cap` (used/cap), hydrate result, latest
reconcile pass/fail, prior-day halt/poison sentinel, session/force-flat window,
and **`data_provenance`** — a *soft* gate that warns (→ DEGRADED) when the
latest decision's `bar_source` differs from the spec's expected primary (e.g.
expected `ibkr_realtime`, latest used `polygon_backfill`); BLOCKED only if a
spec explicitly disallows fallback data.

## Strategy-agnostic console (resolved 2026-05-30)

**Lineage: historical (ADR 0038; retired 2026-08-18; the status route it names
was deleted by PR-B of #1813, 2026-08-27).**

The console renders **no hardcoded indicator names**. The strategy-state panel is
driven by **decision-column descriptors** (`name`, `label`, `type`, `format`)
whose source of truth is the strategy spec (`resolve_decision_columns(spec)` —
the spec declares types, nullability, and semantics). The
**delivery vehicle was the status payload** — `/api/live-instances/{id}/status`
(retired) shipped the resolved descriptors alongside `latest_decision` values,
so the UI was one-fetch, never joined the spec client-side, and a missing
descriptor was an API/test failure rather than a UI interpretation problem. EMA,
VWAP-reversion, and future strategies rendered through the same path. Likewise
`bar_source` rode in `/status` from the latest decision row (engine-authored
provenance, not a backend recompute). The one-fetch descriptor principle carries
forward to the Alpaca bot panel; the route does not.

## Broker-observed state & position ownership (resolved 2026-05-30)

**Lineage: historical (ADR 0038; executable runtime retired by #1583).**

The per-instance ownership and readiness design below describes the retired
IBKR `LiveEngine` path. Current IBKR surfaces expose broker/account evidence
only; none constructs an engine-owned expected position or gates an executable
IBKR order path. The account-level truth and contamination projectors survive
as read-only evidence.

- **Expected position comes from the instance's `expected_position_by_symbol`
  (engine-authored live-state sidecar), never inferred from the latest trade
  row alone.**
- **Ownership is keyed on `bot_order_namespace`.** Per-instance owned position
  is reconstructed from the **namespace-attributed order/execution trail**, not
  decomposed from the raw account-position snapshot. The account snapshot is net
  reality; it is **not an ownership ledger** — ownership comes from the namespace
  trail.
- **Two altitudes, two authors:**
  - *Instance console (engine-authored):* engine-authored live readiness +
    namespace-attributed broker slice (my namespace orders/fills, my
    `expected_position_by_symbol`, my pending orders, my order cap, my
    desired/pause state, my artifact-flush state, my **Layer-A execution
    divergence**). The instance broker gate is **self-consistency only**:
    *my* expected vs *my* attributed fills. It never reads the whole account.
  - *Fleet/account overview (backend-authored):* broker net position,
    explained-by-instance buckets, **residual/unattributed bucket**
    (`residual = broker_account_position − Σ instance_expected_positions`), and
    the **account-contamination verdict**. This is the *only* readiness signal
    legitimately authored by the backend — no single engine can see sibling
    namespaces.
- **Fleet contamination is shown on the instance page as an *inherited* banner,
  never folded into the engine's readiness vector.** Example: "Account residual
  detected: DEGRADED — SPY +37 shares unattributed outside managed namespaces.
  Instance readiness remains READY, but account is dirty." Fleet contamination
  does **not** silently block an executing strategy's own readiness unless an
  explicit **fleet policy gate** ("dirty account blocks all starts") says so —
  and that gate stays visibly separate from engine readiness.
- **Severity matrix for broker divergence:**
  - `live_paper` *self-consistency* divergence (my expected vs my attributed) →
    **BLOCKED** (the engine's model and account reality disagree).
  - `shadow` broker exposure *in its namespace* → **BLOCKED / poisoned**
    (violates the no-submit invariant).
  - `shadow` / sibling positions outside my namespace → a **fleet** concern,
    surfaced as inherited `DEGRADED` / `not_applicable`, never a per-instance
    self-consistency BLOCK.
  - dead-instance start-readiness with unknown broker state →
    **UNKNOWN/DEGRADED**, unless start would submit orders immediately.
## Control-surface scoping (established 2026-05-30)

**Lineage: historical (ADR 0038; retired 2026-08-18).**

- **Durable desired state** — instance-scoped, survives crash/reboot:
  `artifacts/live_state/<strategy_instance_id>/desired_state.json`.
- **Per-run command** — ephemeral, run-scoped:
  `artifacts/live_runs/<run_id>/commands/`. One-shot verbs only (post-redesign).
- **Safety flags** (`halt.flag`, `poisoned.flag`) — run-scoped artifacts,
  distinct from durable desired state.

## Historical IBKR sizing authority (retired 2026-08-19)

**Lineage: historical (ADR 0038; broker consumer retired by #1583).**

The bullets below record the former IBKR live-sizing design for provenance; they
are not current product authority. `LivePortfolio`, its pending-order boundary,
and every registered IBKR submit path are gone. The broker-neutral sizing math
that remains is available to research/backtest consumers only and cannot produce
an IBKR order.

- **live sizing policy** — the **canonical** sizing authority for a *live* bot:
  `run_ledger.live_config.sizing`. Because `live_config` is hashed into `run_id`,
  any sizing change mints a new audited deployment identity (no extra hashing
  work — the hasher is already nested-dict-stable). The **launch page is the
  operator boundary** where this account-risk decision is set; **Angular only
  *selects* the policy, Python *resolves* the quantity** — Python stays the math
  authority.
- **reference / spec sizing** — the sizing declared in the strategy *spec*
  (`spec.entry.size`, the existing `SetHoldings | FixedContracts` `SizeRule`) or
  baked into a hand-coded algorithm (`ctx.set_holdings(symbol, 1.0)`). This is
  **reference/default metadata, not the live authority.** The live runtime
  executes hand-coded algorithms and does **not** run the spec, so treating
  `spec.entry.size` as canonical-for-live would be a false source of truth
  ("architectural theater — hashed but not executed"). `spec.entry.size` becomes
  canonical *only* for a bot whose live runtime actually executes `SpecAlgorithm`
  (a future state).
- **sized-live derivative** — a live run whose **signal logic is QC-anchored** but
  whose **sizing was overridden** by `live_config` (its sizing differs from the
  bound QC audit algorithm's). It is **not** the exact QC execution anchor; the
  ledger / reconciliation report must say so explicitly — *signal logic anchored
  to QC, sizing overridden by live config.* Contrast a run whose live sizing
  matches the QC audit algorithm, which **may** claim the QC execution anchor.
- **`sizing_provenance`** — an **engine-derived** audit stamp on the ledger,
  **never operator-supplied.** Records what the resolved live sizing claims
  against the bound QC audit copy. The operator sends only
  `live_config.sizing.{kind, value}`; the Python deploy/start boundary derives and
  stamps `sizing_provenance`. Values:
  - `reference_native` — resolved live sizing is equivalent (same sizing *rule*,
    not a coincidental share count) to the bound QC audit copy's sizing.
  - `live_override` — resolved live sizing differs from the QC audit copy, **or**
    equivalence cannot be *proven* (**fail-closed default** — never over-claim
    `reference_native`).
  - `spec_default` — **reserved**: only when the live runtime executes
    `SpecAlgorithm` and uses `spec.entry.size` with no live override. Not emitted
    today.
  Provenance is verified, not asserted (same spirit as "Provenance is not
  identity" above): the operator never types it, so there is **no mismatch path**
  today. A *future* optional "expected provenance" guard must **block** the deploy
  on mismatch — never silently downgrade `reference_native` → `live_override`
  (silent downgrade is bad audit UX: the operator believed they shipped a
  reference-native run, the system quietly shipped a derivative).
- **Sizing interception contract** — the deploy-page `live sizing policy` governs
  **`set_holdings` only.** `set_holdings(symbol, fraction)` is a *target-position
  intent* (direction + go-to-target); the policy reinterprets the **magnitude**:
  `SetHoldings(f)` → fraction path; `FixedShares(n)` → target `n` shares
  (`fraction > 0` → `n`, `fraction == 0` → flat; **long-only in v1**, no accidental
  short); `FixedNotional(v)` → `floor(v / price)` shares. `market_order(symbol,
  qty)` is **explicit strategy sizing, never overridden** (TradingView doctrine:
  explicit qty wins); `liquidate(symbol)` is **always target-flat, never
  size-policy modified.** A blanket quantity cap is **not** position sizing — if
  ever needed it is a separately-named **risk overlay**, not this policy.
- **`governed_by`** — engine-derived ledger metadata (not operator input),
  *orthogonal* to `sizing_provenance`: `live_config` (quantity set by the
  deploy-page policy through `set_holdings`) vs `strategy_explicit` (quantity set
  by the strategy's own `market_order` / `contracts_per_trade` — e.g.
  `spy_vwap_reversion`, the options strategy). A `strategy_explicit` run can still
  be `reference_native` if its explicit quantity matches the bound QC audit copy.
  Self-sized strategy registrations **disable the launch sizing control** in the
  deploy form.
- **Honest `reference_native` requires LEAN sizing.** A live `SetHoldings(1.0)`
  claiming `reference_native` must resolve through `LeanSetHoldingsSizing`
  (buffered, fee-aware — what QC's `SetHoldings` actually does), **not** the
  current live default `SimpleFloorSizing`, or the quantity boundary is not
  honestly LEAN-native. (`SimpleFloorSizing` leaves the live path entirely and
  remains a research/backtest model only.)
- **sizing skip** — when a policy resolves to a **zero** share target while flat
  (e.g. `FixedNotional(v)` where `floor(v / price) == 0`, or a percent target too
  small to buy one share), the engine **does not submit a zero order**; it logs a
  *sizing skip* diagnostic so the operator can see why no entry fired.
  Fail-loud-but-don't-crash; applies to every `kind`, not just `FixedNotional`.
- **sizing deploy default** — every new live deploy **always writes an explicit**
  `live_config.sizing`; the canonical default is `FixedShares(1)` (the safe
  canary). **Absence** of `sizing` means **legacy/unknown** (pre-policy
  `SimpleFloorSizing` all-in), *never* `FixedShares(1)` — so old empty-`live_config`
  runs never hash-collide with the new safe default. All-in (`SetHoldings(1.0)`) is
  **explicit opt-in**, never the default.
- **sizing preset** — a named launch-page choice that fills `live_config.sizing`:
  *Safe canary* (`FixedShares(1)`, the default) or *Reference parity*
  (`SetHoldings(1.0)`). A preset may carry an **expected-provenance contract**:
  *Reference parity* asserts `reference_native`, so if Python cannot **prove** the
  resolved sizing matches the bound QC audit copy, the deploy is **blocked** —
  never silently stamped `live_override`. The preset name is a promise; breaking it
  silently is the bad audit UX the provenance design exists to prevent.
- **canary fix is config-only** — switching `deployment_validation` to 1 share is a
  pure `live_config.sizing = FixedShares(1)` deploy: **no strategy `.py` edit, no
  spec edit, no QC re-cut.** The QC anchor stays `SetHoldings(1.0)`; the run is
  stamped `governed_by = live_config`, `sizing_provenance = live_override`. (This
  retires the handoff doc's assumption that a sizing change needs a fresh QC
  parity anchor — that was an artifact of sizing being fused into the algorithm.)
- **audit-copy sizing allow-list** — the **receipt** that backs a `reference_native`
  claim: a single indexed JSON file
  (`docs/references/audit-copy-sizing-allow-list.json`) of
  `{audit_copy_sha256, audit_copy_path, rule, registered_at_ms, registered_by}`
  entries, **not** AST-parsing of arbitrary LEAN code. The entry's `sha256` is
  re-verified against the on-disk audit copy at load — a mismatch is *cannot prove*,
  not a silent override. The proof has three outcomes — *proven match* / *proven
  mismatch* / *cannot prove (sha absent or sha-mismatch)* — and the **Reference
  parity** preset proceeds **only on proven match**; both other outcomes block. An
  audit copy absent from the index makes Reference parity unavailable until its sha
  + rule are registered.
- **`sizing_surface`** — a declarative `StrategyRegistration` attribute
  (`"policy" | "explicit"`) naming *which boundary sizes the strategy* (named for
  the boundary, not a bare `self_sized` bool — leaves room for a future `mixed` /
  `portfolio_model`). `policy`: the strategy targets via `set_holdings`, so
  `live_config.sizing` (`FixedShares | FixedNotional | SetHoldings`) governs and
  the deploy form's sizing control is **enabled**. `explicit`: the strategy
  supplies its own quantity/contracts (`market_order` / internal accounting), so
  the required `live_config.sizing` is `StrategyExplicit` and the deploy form's
  sizing control is **disabled + labeled "self-sized"** (e.g.
  `spy_vwap_reversion`, `spy_ema_crossover_options`).
- **`StrategyExplicit`** — the `live_config.sizing.kind` meaning "the algorithm
  supplied explicit quantity/contract sizing; `live_config` imposed no policy."
  The **honest** sizing value for an `explicit`-surface registration — never a
  misleading `FixedShares(1)`. It governs **who sized** (→ `governed_by =
  strategy_explicit`), **not** whether it matches the QC anchor: `reference_native`
  still requires a proven audit-copy allow-list match.
- **order-surface mismatch** — the runtime records the actual order surface used
  (`set_holdings | market_order | liquidate | internal_strategy_accounting`) and
  compares it to the registration's `sizing_surface`. A mismatch on an **entry**
  order is a registration bug → **fail-fast on the first mismatched entry order**,
  never continue with a misleading ledger. `liquidate()` is a **flatten command,
  not a sizing surface** — never a violation in either mode.
- **Sizing card** — the dedicated instance-console card that displays the live
  bot's sizing decision and its consequences. Three sections: (1) **static facts**
  — the resolved `live_config.sizing.{kind, value}`, the preset that produced it
  (Safe canary / Reference parity / Custom), `governed_by`, `sizing_provenance`,
  and the audit-copy verdict (*proven match* / *proven mismatch* / *cannot prove*)
  with the diff spelled out; (2) **live derivation** — the share count this policy
  would resolve to at the latest price (for `SetHoldings` / `FixedNotional`),
  and the **sizing-skip** counter for the session; (3) **per-trade audit list**.
  The provenance card stays unchanged (run-identity fingerprints only); the Sizing
  card is the sizing-specific surface. For `legacy/pre-policy runs`, the card
  degrades to a "Pre-policy run" badge and hides the live and per-trade sections.
- **per-trade audit list** — the bottom section of the Sizing card: one row per
  broker fill in the current session, joining each fill to the policy that sized
  the order (`policy_kind` → `intended_qty` → `actual_filled` at fill price). Lets
  the operator sanity-check that the policy's outputs match the fills (partial-
  fill drift, broker-side qty caps, etc.). Drives one new engine artifact named in
  ADR 0009.
- **legacy/pre-policy run** — a live run created before `live_config.sizing`
  shipped (`live_config` lacks a `sizing` key). The provenance and Sizing cards
  render this as an **honest "pre-policy" badge**, never a synthetic kind: the
  ledger is **not backfilled** (that would mutate `run_id` hashes), `governed_by`
  / `sizing_provenance` / audit-copy verdict / per-trade audit are all suppressed.
  Re-deploying from a legacy run defaults the deploy form to **Safe canary**, not
  to "whatever the legacy run effectively did" — the safe default applies on the
  first sizing-aware deploy.
- **capital sleeve** *(future — not v1)* — a Python **live buying-power budget**
  that scopes the portfolio value a single strategy's percent sizing may target.
  It will sit at the **portfolio-value provider** feeding `order_sizer`'s
  `SetHoldings` path (whole account today → per-strategy sleeve later →
  `LeanSetHoldingsSizing`); `FixedShares` / `FixedNotional` never read it. **Do not
  conflate with `allocation`** — `allocation` (`.NET`/Postgres
  `StrategyAllocation.CapitalAllocated`) is an after-the-fact attribution /
  reporting record; `capital sleeve` is a live pre-trade sizing input. The two
  words must stay distinct across stacks.
- **all-in coexistence guard** — the interim v1 stand-in for the capital-sleeve
  layer: a start / pre-flight **refusal**, scoped to the **trade symbol** (not the
  whole account). If resolved sizing is `SetHoldings(1.0)` (Reference parity) **and**
  *either* (a) the bound trade symbol has non-zero exposure in the broker account,
  *or* (b) another managed live binding on this account holds `SetHoldings(1.0)` on
  the same symbol → **block start** ("all-in coexistence requires the capital-sleeve
  layer, not built yet"); the deploy page surfaces the same state best-effort.
  `FixedShares` / `FixedNotional` are **never** blocked — an oversized custom
  notional fails loudly through broker / reconciliation, never via silent
  budget-clamping.
  **Permitted-but-unsafe**: two all-in bots on *different* symbols (e.g. SPY all-in
  + AAPL all-in) deploy successfully on the same cash account and *will* fight for
  shared buying power. This is an accepted v1 trade-off, not an oversight; the
  capital-sleeve layer closes it.

## Page-wide collapse rule (resolved 2026-06-17)

**Lineage: historical (ADR 0038; retired 2026-08-18).**

A reactive layout principle for the operator console, generalized from the
broker-instances page IA revision (see `docs/runbooks/broker-instance-operator-surface.md`
§ "IA revision 2026-06-17"). It is *the same single-source-of-truth principle*
ADR 0011 applies to the broker safety verdict — extended from a single pill to
the whole page's expand/collapse behavior.

- **Rule.** Cards collapse to a one-line summary in *steady state* and
  auto-expand when the operator needs to act. The expand trigger is **always a
  server-authored verdict** — readiness verdict, posture computed from
  server-filtered positions, prior-run exit class, safety verdict. The frontend
  never re-derives the trigger from raw fields.
- **Why server-authored.** Two clients viewing the same status payload must
  resolve to the same expanded/collapsed configuration. A frontend-derived
  trigger (e.g., "expand if any gate label looks like sizing") would let two
  clients disagree on what the operator should be looking at — the same failure
  mode ADR 0011 § Decision 7 closes for the safety verdict.
- **Implications.**
  - A new card MUST identify its server-authored expand trigger before being
    added to the page. "Always visible" is allowed as an explicit choice; "feels
    off, let me expand it ambient-style" is not a valid trigger.
  - Steady-state copy is the one-line summary — never a placeholder ("…") or a
    spinner. If the verdict is `UNKNOWN`, the card auto-expands and the
    `UNKNOWN` border surfaces that ambiguity honestly, never silently.
  - Cards with no possible verdict (e.g., the fleet header, the sticky banner)
    are always-visible by *design choice*, not by default — their always-on
    status is documented in the runbook.
- **What this is not.** It is not a CSS convention; it is a contract about
  *which signal* an expand state is bound to. A card that uses `<details>` /
  `<summary>` but expands on `localStorage` flip or a `(click)` toggle alone
  does not satisfy the rule — the toggle is an operator override of the
  server-authored default, never a replacement for it.
- **Live anchors.** The current consumers of the rule are:
  - `<app-configuration-card>` — expands when
    `operator_surface.configuration.verdict !== 'READY'` (PRD #607 Slice 4)
  - `<app-current-risk-card>` — collapses on
    `operator_surface.current_risk.verdict === 'READY'`; expands on
    `ATTENTION` / `UNKNOWN` (PRD #607 Slice 5)
  - `<app-can-it-trade-card>` — collapses on `READY`; auto-expands on
    `DEGRADED` / `BLOCKED` / `UNKNOWN`
  - `<app-action-plan-card>` — expands when
    `operator_surface.action_plan.anomaly_verdict !== 'READY'`.  Today the
    server returns `READY` whenever a plan is present; PRD #593 Slice 4
    flips it without a Frontend change (PRD #607 Slice 5)
  - `<app-fleet-header>` (account/fleet disclosure) — collapsed by
    default when `FleetContamination.verdict === 'clean'`; expanded with
    NO toggle when `verdict === 'contaminated'` or `'unknown'`
    (PRD #607 cockpit revision 2026-06-21).  The collapse target hides
    the emergency-flatten controls behind a one-line summary; attention
    states cannot be manually collapsed.

## Operator-surface inclusion boundary (resolved 2026-06-20)

**Lineage: historical (ADR 0038; retired 2026-08-18).**

`operator_surface` contains **verdicts, semantic classifications,
capabilities, attention-routing inputs, notices, and remediation
descriptors**.  Decisions, trades, incidents, sizing audit rows,
provenance, charts, and logs remain **evidence** on their canonical
channels.  Angular may format evidence and map stable classifications
to display copy.  Angular MUST NOT derive verdicts, action eligibility,
or remediation behavior from evidence.

- **Authority document.** ADR 0013 — operator-surface judgment vs
  evidence (2026-06-20).  Inclusion test for new fields is in §5 of
  that ADR.
- **Structural enforcement.** Every Playwright scenario in the cockpit
  suite asserts independent PROCESS, INTENT, READINESS, BROKER, and
  SAFETY values — the meta-rule that catches synthetic-verdict
  regressions when prose drifts.
- **Inclusion examples.** `actions.resume.disabled_reasons` (operator
  decision), `readiness_gates[].suggested_action` (remediation),
  `broker.safety_verdict` (ADR-0011 final verdict), `fleet_account_summary.account_identity`
  (cross-instance classification) all belong on `operator_surface`.
  Raw decision rows, trade rows, incident rows belong on their
  evidence channels with classification fields (`incident_category`)
  separately surfaced.

## Destructive-action canonical render site (resolved 2026-06-20)

**Lineage: historical (ADR 0038; retired 2026-08-18).**

Each destructive action (Stop, Mark Poisoned, Flatten-and-pause) has
**exactly one** canonical render site in the cockpit (ADR 0010 §A2,
PRD #617):

- **Mark Poisoned** → Audit tab, typed-HALT confirmation.
- **Stop** → identity-strip overflow menu, retirement confirmation.
- **Flatten-and-pause** → identity-strip primary button.

`OperatorGate.suggested_action` (PRD #616) authors only non-destructive
actions inline (`invoke_capability`); destructive actions reach the
operator only via `focus_action`, a navigation hint to the canonical
render site, never an inline button.  A future cockpit change that
adds a second render site for any destructive action is rejected at
review.

## Account identity vs position contamination (resolved 2026-06-20)

**Lineage: historical/read-only evidence (ADR 0038; IBKR runtime retired by #1583).**

The former fleet composition below is retained as terminology for historical
evidence. It no longer feeds an IBKR start, resume, submit, or cancel decision;
ADR 0037 removed the separate Alpaca legacy-custody family and did not adopt
this IBKR evidence as an Alpaca fallback.

The fleet altitude ships `FleetAccountSummary` (server-authored):

- **Account identity** (`CONSISTENT` / `CONFLICTING` / `UNKNOWN` with
  closed reason codes `ACCOUNT_ID_MISSING`, `INSTANCE_ACCOUNT_MISMATCH`,
  `BROKER_ACCOUNT_UNAVAILABLE`, `BROKER_ACCOUNT_MISMATCH`).
- **Position contamination** (`clean` / `contaminated` / `unknown` —
  the existing `FleetContamination`).

The two are **separate altitudes**: identity disagreement never raises
the contamination verdict; position contamination never raises the
identity verdict.  Cockpit attention is computed Frontend-side from a
stable formula:
`account_identity !== 'CONSISTENT' || contamination.verdict !== 'clean' || contamination.policy_blocks_starts`.
`policy_blocks_starts` stays in the formula even when currently
impossible-with-clean so future policy semantics do not require an
Angular change.

## Continue vs Resume — the legacy `resume` naming (resolved 2026-06-20)

**Lineage: live.**

Two different operator acts that a legacy wire name conflates. The distinction is
current and load-bearing; only the resolver that carried the legacy name retires.

- **Continue** and **Resume** are defined in the Identity ladder above: Continue
  lets an existing paused, still-live run proceed under the same `run_id`;
  Resume creates and binds a **new** run of the same immutable strategy instance
  after its prior run stopped.
- **The legacy `resume` identifier means Continue.** `ResumeGuardState`,
  `operator_surface.actions.resume`, the wire verb `resume`, and the CLI
  `cmd_resume` are legacy code and wire names for *continuing an existing paused
  live run*. They never mean creating a new run, and they do not define the
  domain meaning of **Resume**.
- **Renaming is gated on new-run admission.** Retiring or renaming those
  identifiers requires separate new-run admission first — otherwise the rename
  silently widens a Continue control into a Resume control.
  _Avoid_: using "Resume" for the same-run Continue control in trader-facing
  copy, and reading `actions.resume` as a new-run capability.

## Continue / Pause / Stop guards — shared resolver (legacy Resume naming)

**Lineage: historical (ADR 0038; retired 2026-08-18).**

The guard resolver behind the live-instances capability projection. The
Continue-vs-Resume distinction it was named after is **live** — see above.

ADR 0010 §A3 and PRD #616 — the three Continue guards (broker safety
verdict, reconciliation receipt, uncertain-intent WAL) are resolved
once-per-request by `ResumeGuardState` and shared across:

- the capability projection (`operator_surface.actions.resume / pause / stop`)
- the desired-state mutation endpoint (re-validates before the durable
  write)
- the CLI `cmd_resume` (no bypass — the `--force` flag was deleted in
  PRD #616)

The legacy `operator_surface.actions.resume / pause / stop` fields are
renderable Bot Cockpit capability fields, not one-shot commands. A present and
allowed field renders the matching Continue, Pause, or Stop control; a blocked
field renders that control disabled with its backend-authored reason. The
`actions.resume` name means Continue during migration and never means creating
a new run.

The closed reason-code vocabulary, the priority order for the
single-line tooltip, and the structured `disabled_reasons` list are
the only set of disabled-reason codes the cockpit's typed lookup
covers.  Unknown codes fail closed.

## Broker session mirror — client-connection observability (resolved 2026-07-03)

**Lineage: compatibility evidence (ADR 0038).**

The surviving surface reports connection and recovery evidence from the single
read-only FastAPI IBKR data-plane client. Historical captures from retired bot
clients remain readable, but they are never promoted to current bot/process
state. Current, past, and unknown evidence remain distinct; stale history is
never rendered as a live connection.

This surface is diagnostic only. It cannot start, pause, resume, stop, flatten,
disconnect, or otherwise control a bot or broker session, and its evidence is
never an input to Alpaca custody or execution authority. The former per-bot
socket enumeration, process-registry joins, orphaned-bot remediation, Bot
Cockpit controls, ResumeGuard, and connectivity gate state machine retired with
the IBKR execution runtime in #1583. ADR 0038 preserves their historical design
record; they are not a model for current product work.

## Daemon diagnostics — historical evaluator plane (retired 2026-08-18)

**Retired vocabulary.** This section records the former IBKR evaluator/host-runner
diagnostics design for provenance only. Issue #1636 removed its diagnostic routes,
builders, fleet/process registry, connectivity monitor, lifecycle producers, and
browser projection. Current daemon authority is limited to authenticated health,
Gateway-socket evidence, and capability-lease renewal;
Alpaca Broker V2 owns bot lifecycle and operator control. Do not use the terms or
flows below to design current control-plane behavior.

**Lineage: historical (ADR 0038; retired 2026-08-18).**

A read-only, backend-authored self-test of the **host-daemon plumbing altitude**,
the peer of `/api/broker/diagnose` (which self-tests the data-plane's *own* IBKR
client). Its subject is the control plane, not the broker session.

- **Daemon diagnostics (control-plane health)** — the plumbing-altitude report:
  the daemon hop (reachable / auth / protocol-contract), daemon boot identity,
  code freshness (running SHA vs on-disk HEAD), control-plane lease freshness,
  process-registry integrity, and orphan-candidate presence. It is a **distinct
  altitude** from the **broker session mirror** (socket roster, client identity,
  recovery), which remains the single authority for session/socket facts. See
  "Broker session mirror — client-connection observability".
- **Composed authority** — one backend builder is the single brain. It *authors*
  the plumbing checks (facts only the data plane can see — reachability, auth,
  code/lease freshness, registry integrity, orphan presence) and *embeds by
  reference* the mirror's already-authored socket-reconciliation attention codes
  (`REGISTRY_SAYS_OFFLINE_BUT_SOCKET_LIVE`, `ORPHANED_BOT_SOCKET`, …). It **never
  re-runs lsof and never re-classifies a client** — single authority per fact is
  preserved even inside the superset.
- **Two presentation surfaces, one report** — the same authored artifact is read
  by its own snapshot endpoint (the full diagnostics panel) *and* embedded as a
  control-plane header inside the broker session mirror page. "Available from both
  places" is achieved by composition, never by mounting one handler at two routes
  or fusing the snapshot self-test into the mirror's streaming/paginated payload.
- **No bare "degraded."** The word "degraded" as a catch-all bucket does not
  exist in this surface. Every distinct cause — daemon-down, auth-rejected,
  stale-code, stale-lease, orphans-present, registry-amnesia, socket-probe-
  unavailable — is its **own named check** with its own status, trader title,
  cause, and remediation. The report never collapses distinct failures into one
  amber word.
- **Dominant condition** — the specific, closed-enum cause the report elevates to
  its headline (e.g. `STALE_CODE`, `LEASE_STALE`, `UNREACHABLE`, `AUTH_REJECTED`,
  `ORPHANS_PRESENT`, `REGISTRY_AMNESIA`, `SOCKET_PROBE_UNAVAILABLE`, `HEALTHY`).
  Paired with backend-authored trader **headline** copy (`title` / `summary` /
  `remediation`) — the frontend renders the copy and keys off the enum, exactly
  as it does for reason codes and the event-narrative registry. `pass|warn|fail`
  survives only as the severity colour, never rendered as a standalone word.
- **Always 200.** The diagnose endpoint returns HTTP 200 with a full report even
  when the daemon is down; the failure lives in the checks, never in the HTTP
  status (contrast `/daemon-health`, which maps failures to 502/503 and returns
  no body). A top-level `transport` field mirrors `DaemonResult.kind` so the
  banner binds directly without scanning the checks list.
- **Container-actuatable gate** — a diagnostic fix becomes an invocable *button*
  only if the data plane can actually cause it from inside the container (the
  daemon executes it in-process on an authenticated forward). v1's only such
  action is `renew_lease`. Host-level fixes (start / restart the daemon) require
  host process control the container does not have, so they are **structurally
  never buttons** — only honest guidance. A diagnostics surface must **never
  render a control it cannot actuate.** The action model forbids attaching a
  `RECOVERY_MUTATION` to a host-only fix; those carry authored guidance instead.
- **Platform-aware host guidance** — the daemon is a host process that ports
  across Windows / Mac / Linux, so host-level remediation is authored per the
  daemon's **reported OS/supervisor** (`systemctl restart …` on Linux,
  `launchctl kickstart …` on Mac, the NSSM restart on Windows) — never one
  generic "restart the daemon" string that is wrong on two of three platforms.
  The daemon reports its platform/supervisor as an additive health fact.
- **Backend-authored redaction** — the backend is the **sole** redaction
  authority; nothing unsafe ever reaches the browser and the frontend never
  decides what is safe. Host-absolute paths are reduced to repo-relative or
  basename with the **home/user prefix and hostname stripped**
  (`/Users/inkant/learn-ai/…/live_runs/<run_id>` → `artifacts/live_runs/<run_id>`);
  raw tokens, connection strings, and full `sys.executable` argv are never
  emitted. **Operator handles pass through** (`run_id`, `strategy_instance_id`,
  short `boot_id`, `commits_behind`). There is **no per-check frontend exposure
  gate** — a gate would make the frontend a redaction authority; instead reduced
  fields carry an informational `redacted` marker, and export is just a
  serialization of the already-redacted report. The pre-existing `HostRunnerHealth`
  path/argv leak (`repo_root`, `live_runs_root`, `process.log_path`,
  `process.command` shipped raw to the browser) is tightened in the same effort.
- **Primary job — pinpoint why a *specific bot* is failing in the live daemon.**
  The north star is not a flat global "is the daemon healthy" report; it is a
  per-`strategy_instance_id` **diagnostic ladder** that walks: daemon reachable →
  bot has a managed process → process alive vs exited-and-why → registry-consistent
  (not amnesia) → has an IBKR socket → socket attributable/healthy (not
  orphan/ghost/collision) → child runtime fresh → code/artifacts visible — and
  surfaces the **first failing rung** as that instance's `dominant_condition`. The
  broker session mirror owns the socket rungs (embedded by reference); daemon
  diagnostics owns the process / registry / code / lease / runtime rungs. A global
  report still exists for control-plane-wide faults that hit every bot at once
  (unreachable, auth, stale code, stale lease); the per-instance ladder is the
  primary operator-facing view.
- **Fact sources (second-opinion-hardened 2026-07-04)** — the builder reads three
  existing daemon-adjacent sources: `fetch_health` (code / lease / boot /
  orphan-count), **`fetch_instances`** (the process registry — *required*, because
  the mirror omits idle/exited bots with no socket row, so process rungs are blind
  without it), and the mirror snapshot (socket reconciliation, embedded by
  reference). Registry-read-unavailable is its **own** condition
  (`REGISTRY_SNAPSHOT_UNAVAILABLE`), distinct from socket-probe-unavailable.
  Additive `HostRunnerHealth` facts this requires: OS/supervisor (platform-aware
  guidance), `lease_threshold_ms` + `lease_write_error` (split stale-lease from
  unwritable `control_plane/`), per-orphan candidate **detail** (not just the
  count), and `exit_reason` on the process status (single mapping authority —
  `_exit_reason_from_code` on the daemon, not a data-plane re-implementation).
- **One report, per-instance subreports** — a single global report carries
  `per_instance` subreports from **one consistent snapshot** (one lsof pass, no
  N+1); an optional per-sid projection route calls the same builder and projects
  one instance. The mirror header embeds the global header from that same report.
- **Linked authorities, not owned** — diagnostics never re-authors readiness or
  runtime-freshness thresholds (it calls `evaluate_runtime_freshness` and treats
  cockpit readiness/action gates as *linked* authorities), never uses the
  data-plane `IbkrConnectionHealth` as **per-bot** truth (that is the
  singleton/system client; per-bot broker state comes from the mirror row + child
  runtime snapshot), and reuses `DaemonResult.kind` **verbatim** as the transport
  field (`AUTH_FAILED`, not a renamed enum). Malformed-body (`PROTOCOL_ERROR`) and
  schema-mismatch (`INCOMPATIBLE_CONTRACT`) stay distinct conditions — different
  remediation.
- **Reachability sourcing** — an explicit diagnose/refresh does a **fresh probe**
  (current facts); the connectivity monitor's **folded state** refines the
  reachability rung (`RETRYING` → warn "reconnecting" vs terminal `UNREACHABLE` →
  fail "down") and is the only source that can prove `BOOT_CHANGED`. The
  always-visible mirror header binds to the folded state alone, so passively
  rendering it never probes the daemon.

## Strategy validation & signal stream (sharpened 2026-07-05)

**Lineage: neutral.**

Sharpens the **Validated strategy package** entry above for the Deploy-a-strategy
redesign. Draws the line between what the *validated strategy* carries and what
the *deployment* binds. A strategy is validated against a reference engine, not
against a broker, so this vocabulary survives a broker change.

- **Validated strategy** — a binary, **strategy-level** property (not per-symbol):
  our LEAN-engine port is proven numerically equivalent to a QuantConnect backtest.
  Validation is a **one-step** act performed against a single **validation-case
  symbol** (e.g. SPY) that serves as the strategy's **golden fixture**. Once the
  strategy is validated, it is validated *as a whole* — we do **not** re-validate
  per symbol. The validation carries: the strategy's settings file, its QC backtest
  ID, its saved QC algorithm source (the exact QuantConnect-equivalent code, under
  `references/qc-shadow/`), and the port-vs-QC reconciliation verdict. Selecting a
  strategy **auto-populates** all of these — the trader never types a settings-file
  path or a backtest ID.
- **Validation-case symbol (golden fixture)** — the single symbol the strategy was
  validated against (SPY). It is **provenance only**: it does not default,
  constrain, or warn the deployed signal stream, and the UI may or may not surface
  it. Its job was to prove the port; that job is done once.
- **Signal stream** — the symbol a deployed bot reads to compute buy/sell signals,
  bound via `live_config.symbol`. It is **completely independent of the strategy
  and of the validation-case symbol** — a free deploy-time choice. Distinct again
  from the **traded instrument**, which the Action plan controls: signal stream is
  *what the strategy watches*, the action-plan legs are *what it trades*. All three
  (validation symbol, signal stream, traded legs) may differ.
- **Signal intent** — an instrument-free ENTER or EXIT decision emitted by a
  signal-only strategy at a decision-bar close. The Action Plan consumes that
  decision to choose the traded leg; the strategy never chooses the asset.
- **Signal Program** — the versioned, registry-selected definition of a
  broker-neutral strategy decision stream. It constructs the strategy's
  **Signal Session**; it never selects an account, custody authority, or broker.
- **Signal Session** — one running Signal Program's ordered decision-clock
  state. It advances only accepted closed timeframe buckets and requires each
  **Evaluation Stage** to settle before it accepts another decision clock.
- **Evaluation Stage** — the one pending semantic result of a Signal Session
  evaluation, including its trace and an optional ENTER/EXIT candidate. It is
  settled as **COMMIT** (allow its execution-boundary request) or **DISCARD**
  (restore retryable strategy state); it is never a broker-order receipt.
- **Evaluation Trace** — stable, broker-neutral evidence of one closed decision
  clock: bar qualification, readiness, relation/signal facts, candidate, reason
  evidence, and the semantic Action Plan request. It is not a custody journal
  and does not prove an order was submitted.
- **Strategy Validation page** — the standalone surface that owns a strategy
  *becoming* validated and that displays the equivalence evidence. It is a
  **master-detail list** (a row per validated strategy, click through to detail),
  a sibling in spirit to the Golden Fixtures surface. The detail shows the
  strategy's brief metadata, the **validation diagnostics** (QC backtest ID,
  validation-case symbol, trades-matched / trades-validated counts, P&L-matched
  magnitude, and the `DivergenceCategory` taxonomy from `qc_reconciler.py`), and
  the **QuantConnect reference code** rendered inline. It never renders our
  internal LEAN/engine port source — sovereignty means the reference is shown for
  audit, the port is not.
- **Production candidate** — a strategy intended to advance through the
  external-reference validation cycle toward Paper deployment and, under a
  separate future release decision, possible Live deployment. Its current
  accepted proof includes the exact reference algorithm, pinned reference run,
  behavioral reconciliation, and human review.
- **Operational validation harness** — a strategy-shaped program used to prove
  the deployment machinery itself through deterministic internal replay. It may
  run in Dry Run or Paper, never enters the external-reference promotion track,
  and is permanently ineligible for Live deployment.
- **Strategy validation proof** — the ordered, inspectable evidence dossier for
  one strategy: program contract, reference source, reference run when
  applicable, reconciliation or harness qualification, human review, and a
  freshness check against the current artifacts. A proof can be current,
  missing, stale, rejected, blocked, or unreadable.
- **Authoring-boundary supersession** — this **supersedes** the earlier
  "The Deploy a strategy page owns creating or selecting this package" clause in
  **Validated strategy package** (above). Authoring now splits: the **Strategy
  Validation page** owns *making a strategy validated*; the **Deploy a strategy
  page** only *selects* an already-validated strategy. Engine Lab remains a
  non-authoring surface for this workflow.
- **Strategy catalog & validation state** — the Strategy Validation surface is a
  catalog of **all** strategies carrying a validation state, not a list of only
  the good ones. Under ADR 0023, validation state is projected from append-only
  flag events. A human-recorded `validated` flag is auditable immediately, but it
  reaches the Deploy dropdown only when the latest non-superseded flag event also
  has `behavioral_equivalence.verdict == accepted_for_deploy`. The validation
  binding is **Python-owned and stored** (already present today as the
  `qc_cloud_backtest_id` + `qc_audit_copy_path`/`sha` + `strategy_spec_path`/`sha`
  chain in each `run_ledger.json` — a retiring artifact family; see "Deploy
  binding and launch posture" below — plus the qc-shadow attribution and the
  `docs/references/reconciliations/` reports) — the surface consolidates it, it is
  not re-typed.

### Revised 2026-07-05 — validation is a human flag; Deploy re-homes to Bots (see ADR 0023)

A `grill-me` session revised several points above. Where they conflict, **ADR 0023 wins**:

- **Validation is a human flag, not an automatic verdict.** The Validation page
  refreshes registered Python-vs-LEAN/QC evidence (QuantConnect is the LEAN
  reference; the backtest ID only *pins* the reference run) and displays how well
  their buy/sell entry signals and PnL match (`DivergenceCategory` + a headline %).
  A **person** sets the `validated` / `invalidated` flag; there is **no automatic
  threshold that writes the flag** (a ~95% match is human guidance, not a rule). The
  QC backtest ID is **provenance**, not the credential. This replaces "validated iff
  … a passing port-vs-QC reconciliation" above. A future dedicated engine-run
  trigger must not be confused with the shipped manifest evidence refresh.
- **The flag is always saved with its evidence + a reason — accountability, not
  prevention.** The system never blocks the human from recording a judgment: a
  strategy flagged `validated` at 0% agreement is allowed and is stored with the full
  evidence snapshot (registered diagnostics, `DivergenceCategory`, backtest ID,
  artifact refs/hashes, authenticated flagger, timestamp) and a required reason.
- **Deployability is stricter than merely recording a `validated` flag.** The Deploy
  dropdown includes only the latest non-superseded flag event where the flag is
  `validated` and `behavioral_equivalence.verdict == accepted_for_deploy`,
  preserving `numerical-rigor.md`'s behavioral-equivalence requirement (matching
  signals/PnL within a documented tolerance plus a reason). A 0%-agreement
  `validated` event is auditable but not deployable unless the numerical-rigor
  authority changes.
- **Validation never trades.** No read-only/paper/live orders, no broker, no readiness
  gates on the Validation page. Its asset is the **safe canary** (the signal entity
  itself) and its sizing is a **1-share informational** readout, not an input.
- **Execution mode (read-only / paper / live) is a Deploy concern**, not a validation
  level. All three modes are plumbed; **read-only + paper are built now, `live` is
  runtime-inactive** (hard-blocked under ADR 0011) until a **separate IBKR live
  account** and a live-trading safety project. One backtest ID validates a strategy
  for every deploy mode.
- **The Deploy page re-homes from `Strategy Lab` to the `Broker` group, next to
  `Bots` / Bot Control.** There is exactly one Deploy page (rebuilt + re-homed, never
  duplicated). Validation stays in `Strategy Lab`.
- **Deploy signal stream now defaults to the validated signal, overridable** to any
  symbol — relaxing the "does not default, constrain, or warn" rule above to
  "defaults, does not constrain." (Amends ADR 0020 §2.)

## Deploy binding and launch posture (sharpened 2026-07-05)

**Lineage: historical (ADR 0038; retired 2026-08-18).**

What the live-instances deploy flow bound at launch, and how its readiness strip
behaved. The strategy-validation half of the same 2026-07-05 sharpening is
**neutral** — see above. The live successor is the Alpaca deploy drawer.

- **Deployment binding surface** — the Deploy-a-strategy flow selects one validated,
  deployable strategy (auto-populating its validation evidence: settings file, QC
  backtest ID, audit copy, reconciliation verdict — none typed) and binds the
  independent, per-deployment inputs: signal stream, position sizing, action-plan
  legs, launch options, deployment name, and the read-only connected account.
- **Actionable readiness gate** — a deploy readiness fact (Engine / Broker /
  Account / Fleet) rendered at **trader altitude** (a backend-authored named
  condition via `receiptLabel`, drill-down to its full page; never raw socket rows
  inline). A blocking gate carries a **server-authored action envelope** (the same
  `kind: recovery_mutation | navigation` model as daemon diagnostics). The strip
  renders a **"clear this gate" button only when the backend attaches an actuatable
  `recovery_mutation`** — reusing the **canonical existing mutation** (Account
  `NOT_PROVEN` → `reconcileAccount` / `POST /api/accounts/{id}/reconciliation`;
  daemon lease-stale → `renew_lease`), never a forked one. Non-actuatable, host-
  level fixes (start the daemon, broker `HARD_DOWN`) stay **guidance / deep-link,
  never buttons**. On success the gate **re-evaluates server-side**; a cleared gate
  unblocks deploy/start. The strip surfaces only **pre-deploy gate-clearing**
  actions; **bot lifecycle actions (RESUME/FLATTEN/STOP/PAUSE) keep their canonical
  render site in the Bot Cockpit** and are not rendered here (see "Destructive-
  action canonical render site").
- **Launch-default posture (deploy)** — the deploy flow defaults to **paper orders
  enabled**, **start-immediately on (rendered *loud*)**, and a **daily order limit
  of 2000** (a practically-unthrottled ceiling). This inverts the earlier
  read-only-first default and is safe **only** while three guardrails stay hard:
  Safe-canary 1-share sizing remains the default, `UNSAFE`/live-identity is a hard
  block, and account readiness gates the *start*. The standalone paper-confirm
  modal is replaced by the loud start treatment; a hard confirm/block is reserved
  for elevated conditions (live identity, account `NOT_PROVEN`).

## Historical bot event stream — narrated gate pipeline (retired 2026-08-19)

**Lineage: historical (ADR 0038; producer and control surface removed by #1583).**

The deleted per-bot stream narrated a strategy instance's live pipeline — bar
evaluation → gates → order → broker outcome — so an operator can answer both
"why isn't my bot trading?" and "where exactly did that order die, and what was
the most granular error we had?" The detailed vocabulary below is retained for
historical receipts only. `bot_event_spine.py`, its engine producers, and its
operator control surface are gone; the surviving broker-activity stream is
read-only evidence and does not imply a running bot pipeline.

- **Bot event stream** — the canonical name for the per-bot narrated pipeline
  stream. _Avoid_: "event service", "activity feed" (the ADR-0014 broker-activity
  stream is now the *tail* of this, not a peer). Extends [[Activity structural
  cluster]], [[Usable activity row]], [[Stable activity stream]].
- **Evaluation** — one bar-evaluation a bot performs. The spine unit *before* an
  order exists, so a block that happens upstream of any order (stale data, session
  closed, no signal) still has a home. Most evaluations are quiet.
- **Terminal error** — the **most-granular error captured at the exact gate where
  an evaluation or order actually failed**, preferring the external system's native
  error (IBKR `errorCode`/`errorString`, subprocess exit + stderr, OS errno) over
  any generic wrapper the engine puts around it. The operator sees a backend-authored
  *useful derivation* of it (title/message) with the *exact* error kept as expandable
  forensic evidence. _Avoid_: "last threaded error" (the originating phrase; fuzzy —
  it does not mean "outermost" or "most recent", it means *most granular at the
  failing gate*).
- **Gate-walk** — the ordered sequence of gates one evaluation traverses (e.g.
  `sizing ✓ → broker-safety ✓ → daily-cap ✗`). Drill-in detail, never a spine row.
- **Gate-step** — a single gate traversal in a gate-walk, raw-captured **at
  enforcement time** with `evaluation_id`, `gate_id`, `gate_result`
  (`pass | skip | block`), `source_authority`, and structured facts. Never
  reconstructed after the fact from the readiness sidecar — that is a "can it act
  on the next bar?" now-vector, not a history log. The block-outcome gate-step is
  where a [[Terminal error]] attaches.
- **Spine event vs gate-step event** — the two altitudes of the stream. **Spine
  events** are the sparse, authored, visible rows (`evaluation_idle`, `signal_fired`,
  `order_submitted`, `order_filled`, `order_cancelled`, `order_rejected`, `blocked`,
  `halted`, `launch_failed`). `order_cancelled` preserves existing ADR-0014
  cancellation rows as non-escalating broker-tail outcomes. **Gate-step events** are
  the drill-in detail beneath a row. A
  quiet bar folds to one `evaluation_idle` heartbeat; it never scrolls.
- **Order-cluster promotion** — a spine row **starts** keyed to its [[Evaluation]]
  and is **promoted** to the order's `order_ref` identity the moment an intent is
  minted, so the operator follows one unbroken row from *bar evaluated → signal →
  gates → submitted → filled/rejected*. This is [[Activity structural cluster]]
  extended across the full pipeline.
- **BotEventRow / BotEventRaw** — the stream's versioned wire contracts:
  `BotEventRaw` (the enforcement-point-captured raw event in the run-scoped WAL)
  and `BotEventRow` (the authored projection row), with [[Gate-step]] and
  [[Terminal error]] as child shapes. A **new** contract — broker executions are
  terminal child event-types; `BrokerActivityRow` maps into the stream tail via an
  explicit replacement map. _Avoid_: informally extending `BrokerActivityRow`
  (its "one IBKR execution" identity is a load-bearing ADR-0014 contract).
- **Enforcement-point authored** — the runtime that enforces *or observes* a gate
  owns its raw capture: the engine loop for evaluation/submit gates, the
  daemon/launcher for spawn failures and subprocess stderr, the broker session
  layer for session collisions. The publisher authors the projection. _Avoid_:
  "engine authored" as the blanket invariant — it is only the common case.
- **Surface disposal (replace, don't add)** — exactly one current-verdict surface
  (`operator_surface`) and one historical stream (the Bot event stream); every
  other surface (Broker Activity table, working/pending orders, rejection rows,
  incident headline, gate checklists) is a projection over one of the two, or is
  deleted. Removal in service of truth and robustness is encouraged. The gates
  themselves are the safety model and untouchable; only their duplicate
  visualizations are disposed. _Avoid_: peer surfaces; "a sixth channel".
- **The rejection break** — a broker rejection is *expected as a broker-callback
  shape* but *terminal as an operator outcome*. The old `verdict=expected`
  rejection row is **replaced** by `order_rejected`, never kept alongside a notice.
- **Stream evidence vs cockpit verdict (single source, two projections)** — gate
  outcomes are authored **once** per evaluation at the enforcement point. The
  `operator_surface` readiness verdict renders the **current** "can it trade now"
  summary (contract unchanged); the Bot event stream renders the **historical
  walk** over time. Neither re-derives the other's verdict — the same facts, two
  views. Honors the engine-authored-readiness doctrine and CLAUDE.md
  single-source-of-truth #5.
- **Terminal-outcomes escalate** — most stream events wait to be found; the terminal
  outcomes (`halted`, `order_rejected`, `launch_failed`, submit-uncertain) also mint
  an [[operator notice]] / OperatorIncident so the cockpit's `incident_headline` +
  page-wide auto-expand surface them even when the operator is not watching the
  stream. Self-protective, expected blocks (market closed, no signal) stay in-stream
  at `info` — escalating those is how alarm fatigue is trained. Escalation is
  deduped by incident key (instance + `order_ref`/`evaluation_id` + terminal code):
  one failure, one visible terminal story.

## Operator notice actionability & resolution (resolved 2026-07-08)

**Lineage: neutral.**

Every operator notice (ADR-0015) declares two orthogonal truths — how
much to distrust the bot, and what (if anything) can be done. Neither
implies the other.

- **Tier** (`info` / `warning` / `critical`) is **trust-impact only**:
  "how much should the operator distrust the bot right now." It never
  encodes what the operator should do. `critical` with no remedy is a
  legal, first-class state.
- **Actionability** — required closed classification on every notice:
  - **`actuatable`** — the cockpit performs or directly navigates to
    the fix (renew lease, focus a cockpit action, redeploy).
  - **`routed`** — a fix exists but lives elsewhere; the notice must
    name the destination and what to look at there (runbook, IBKR
    screen, host shell).
  - **`self_resolving`** — no operator action needed; the notice must
    name its clearing condition. Retires `wait`.
  - **`no_remedy`** — no action exists anywhere; the notice must state
    what the operator must not trust meanwhile. Carries a required
    `remedy_status`: **`inherent`** (no remedy can exist — justified in
    the authoring table) or **`unbuilt`** (remedy conceivable but not
    built — must cross-reference `docs/known-gaps.md`, enforced by the
    exhaustiveness gate). `no_remedy` is honest copy, never a
    dumping ground: an unbuilt remedy stays visible as a feature gap.
- **Resolution statement** — required on every notice: the condition
  under which it clears and who observes it. "Resolution unknown —
  requires manual reconciliation" is a legal truthful value; omission
  is not. A notice whose author cannot state its resolution condition
  is not ready to ship.
- **The `none` conflation is dead.** "No action *needed*" and "no
  action *exists*" are different states (`self_resolving` vs
  `no_remedy`) and demand opposite operator responses; `action.kind =
  "none"` survives only as "no clickable affordance."
- **Silent states get reserved codes.** A state with trust impact that
  emits nothing is a contract failure, same as an untruthful message.
  Known silent-critical states are declared upfront as reserved notice
  codes with honest pre-classification (`fleet.sibling_liveness_unproven`,
  `reconciliation.divergence_while_submitting`), cross-referenced with
  `docs/known-gaps.md`. Reserve first, implement second.

Authority: ADR-0015 § Amendment 2026-07-08. Placement/prominence is
resolved by the single-dominant-headline rule below.

## Single dominant headline (resolved 2026-07-08)

**Lineage: neutral.**

Placement of every operator notice is a pure function of
**tier × actionability** (ADR-0025). No surface opts out; no notice
chooses its own placement.

- **At most one banner, ever.** One arbitrated winner across all banner
  sources (control-plane, broker-evidence, runtime-freshness, incident).
  Highest tier wins; ties broken by blockage-ladder rung order.
  Concurrent criticals fold behind a "+N more critical" affordance that
  opens the ladder — one click away, never stacked.
- **`critical` × anything** → the banner slot. `no_remedy` criticals
  lead with the trust impact and the resolution statement.
- **`warning` × `actuatable`/`routed`** → attention dropdown row with
  the affordance inline. Never a banner.
- **`warning` × `self_resolving`/`no_remedy`** → attention dropdown,
  quiet (no pulse), resolution statement visible.
- **`info` × anything** → the **quiet status region** (session card
  tier). Never a banner, never the attention dropdown, and **never**
  PRD #951's lower documentation section — that section is "no CTAs,
  no live claims", and a live notice is a live claim.
- **Arbitration is backend-authored** (ADR-0013 verbatim rule): the
  banner winner, the "+N more" count, and the folded list are one
  server-side projection; the frontend never re-derives dominance.

Authority: ADR-0025.

## Rung receipt (resolved 2026-07-08)

**Lineage: neutral.**

Every mutation response (Resume, Start, Reconcile, Flatten-and-pause,
crash-recovery override, Mark Poisoned) carries a backend-authored
**rung receipt**: a notice-shaped statement naming the **next blocking
rung** from the blockage ladder — or the scoped all-clear ("no enforced
gate blocks the next start"). It exists because a mutation can succeed
while the thing the operator actually wanted (a running bot) is still
blocked downstream; the receipt connects the click to the next blocker
at the moment of the click.

- Inherits the full notice contract: tier, [[actionability]], mandatory
  resolution statement, verbatim rendering.
- Claims only what the enforcement layer guarantees; observational
  verdicts that disagree ride along as `warning`, never silently.
- Authored from a fresh ladder evaluation inside the mutation request —
  never from the client's pre-click poll.
- Same resolver as the status projection's ladder (shared-resolver
  pattern, ADR-0013 §6).

Authority: ADR-0015 § Amendment 2026-07-08 (b).

## Account custody language (resolved 2026-07-27)

**Lineage: live.**

- **Originator** — the immutable strategy instance, run, and namespace that
  authored an intent. It remains provenance after its process dies.
- **Custodian** — the one durable account authority responsible for resolving
  an admitted intent lifecycle. In normal paper operation this is the
  accepting Account Clerk generation.
- **Manager** — the at-most-one fenced actor permitted to issue the next
  broker write for a custody lifecycle. _Avoid_: owner, submitter.
- **A0 custody receipt** — proof that the Clerk fsynced an intent and accepted
  responsibility to resolve it; it is not broker acknowledgement or exposure
  evidence. _Avoid_: order accepted, broker success.
- **Custody timeline** — the per-intent evidence that keeps broker/source time,
  local arrival time, and durable record time distinct. Journal sequence orders
  file writes only and is never a causal clock.
- **Account epoch** — the accepting Clerk generation's bounded period of valid
  broker proof. Facts from an invalidated epoch cannot authorize a new entry.

## Custody log and fold (resolved 2026-08-06)

**Lineage: live.**

Decision record: ADR 0035. How the Alpaca custody authority stores what it knows.

- **Custody transition** — one appended, hash-chained record of something that
  happened to an account's custody: a registration, a command, an order effect,
  an execution, an uncertainty. The append-only sequence of them is the sole
  canonical custody record.
  _Avoid_: event, journal entry, audit row (each names a medium, not the fact).
- **Transition kind** — the closed name that says which custody transition this
  is, and which fold applies to it. An unrecognised kind is refused, never
  skipped.
- **Fold** — the materialized current-state view built by applying transitions in
  order. It is **never authored directly**: every current-state row changes only
  as the consequence of an appended transition, committed together with it, so
  the view is always rebuildable from the log.
  _Avoid_: projection cache, derived table, snapshot.
- **Hash chain** — each transition carries the hash of its predecessor and of its
  own payload, so a missing or altered transition is detectable rather than
  merely improbable. The first link is a fixed genesis marker.
- **Content-addressed idempotency** — request identity derived from what the
  request *is* rather than from a caller-supplied nonce. The same natural key
  with the same payload is a transport retry and returns the original outcome;
  the same key with a different payload is a durable conflict, never an update.
  A nonce is used only where re-issuing genuinely means something new.
- **Payload hash** — the immutable-once-committed fingerprint of a request's
  content, and the thing a conflict is detected against.
- **Capture-before-contact** — the rule that the durable record of an intent is
  fsynced *before* any broker call is made, so a lost response can never leave a
  broker effect the authority has no record of. Write latency is measured, never
  traded away.
  _Avoid_: write-ahead (accurate but names the technique, not the guarantee).
- **Authority generation** — which incarnation of an account's custody authority
  a fact belongs to. It advances only on an explicit reset — flatten, obtain
  fresh broker proof, retire the old authority, initialize a clean one — so facts
  from one generation can never collide with, or be mistaken for, the next.
  Every idempotency key and every hash payload carries it.
  _Avoid_: epoch (reserved for the broker-proof window in "Account custody
  language"), version, migration number.
- **Control revision** — the account-wide monotonic token that advances on every
  fold, and the concurrency token every economic read is bound to. It replaces
  concurrency tokens computed by hashing derived state.
- **Execution lease** — the durable, TTL-bounded claim naming the one process
  currently permitted to make broker contact for an account. It is renewed while
  held and expires on its own; an expired holder loses write authority
  immediately and cannot silently reacquire it. The owner is a per-process
  token, never a PID, which the OS can recycle.
  _Avoid_: lock, mutex, connection ownership.
- **Operation claim** — the narrower, per-work-item fencing token that admits one
  actor to one pending broker operation. Distinct from the account-scoped
  execution lease: the lease says *which process*, the claim says *which piece of
  work*.
- **Append mirror** — the separate write-ahead trail that records an intent
  before the database commits it and marks it finalized after. Only a contiguous,
  hash-verified, finalized stretch of it may rebuild a corrupted authority, and a
  command is neither accepted nor broker-eligible until it is finalized.

## Execution ledger (resolved 2026-08-10)

**Lineage: live.**

What the custody authority records about executions, and the units its numbers
are counted in. The units differ deliberately and must not be summed together.

- **Execution slice** — one broker execution fact, with its own side, quantity,
  price, and broker-occurrence time, identified by the broker's own execution
  identity. It is **not** an order's cumulative filled quantity and not an order
  lifecycle update.
  _Avoid_: fill (ambiguous — see **Fill count**), partial, execution report.
- **Effective execution slice** — the currently applicable version of an
  execution fact, after any correction has replaced it.
- **Correction** — an append-only replacement for one prior execution slice. The
  superseded slice stays auditable; the difference in quantity, price, and fee is
  applied forward. A quantity that regresses with no matching superseded slice is
  an exposure-blocking uncertainty, never a silently accepted fill.
- **Execution-coverage quarantine** — the state of an exact execution that
  arrives after a cumulative recovery already accounted for the same order. It is
  recorded but deliberately kept out of exposure, position, and P&L until a
  closed proof decides which account of the order is the covering one.
- **Fill count** — the number of *effective execution slices*. Never the number
  of filled orders, lifecycle updates, or closed lots.
- **Closed lot** — one realized-P&L record: a FIFO lot closure with its entry and
  exit prices, quantities, times, and fee. It is a P&L record, and is a different
  question from **Lot exhaustion** under "Flatness boundary" below, which asks
  whether a lot is used up. The `_Avoid_` there bans the phrase for *that*
  question, not for this record.
- **Realized P&L for a session** — the sum over closed lots whose *closing* time
  falls inside the session window. A late correction never moves an
  already-closed lot into the session the correction arrived in.
- **Open P&L** — the mark-to-market value of the lots still open. It is
  **unknown**, not zero, until a mark exists for every open symbol.
- **Custody column** — a field of a custody transition that the hash chain
  covers. The set is fixed: adding one would invalidate every existing row.
- **Provenance column** — evidence about *where an execution fact came from* —
  the broker execution identity, which capture path observed it, which slice it
  supersedes, and whether a fee was reported at all. It lives on fold tables and
  inside the hashed facts payload, never as a new custody column. This is why
  execution evidence can grow without breaking verification of the existing log.
- **Fee fidelity** — whether a fee was reported by the broker or simply not
  reported. A fee that is unknown is rendered unknown; it is never rendered as
  zero.

## Broker Desk lenses (resolved 2026-08-12)

**Lineage: live.**

- **Lens** — a manual, per-surface view mode that decides which of two purpose-
  built views of the same account or bot is rendered. It is a presentation
  choice, never an identity, a role, or an authorization decision.
  _Avoid_: role, mode, persona, permission.
- **Trader lens** — the outcomes view: *how am I doing?* Verified account facts,
  activity, positions, and equity history.
- **Operator lens** — the mechanism-and-repair view: *why is the system working
  or not, and what fixes it?* The dominant posture headline with its fix
  attached, plus forensic evidence.
- **Audience** — the backend-authored field on an operator blocker that routes it
  to the trader lens, the operator lens, or both. `both` is reserved for guidance
  that is genuinely identical in each lens; differing guidance is two blockers
  sharing one condition identity. Presentational routing only — never an
  authorization decision.
- **Broker Desk** — the account-scoped surface for one broker account, carrying
  both lenses. Distinct from the **bot panel**, the instance-scoped surface for
  one bot, which carries its own lens pair.
  _Avoid_: account monitor, account page, Bot Cockpit (the retiring
  live-instances console).
- **Evidence drawer** — the shared, on-demand reader for one immutable projected
  Clerk receipt, led by that receipt's custody timeline. It reads a receipt; it
  never re-derives one.
  _Avoid_: evidence modal, receipt viewer.
- **Deploy drawer** — the slide-over that hosts the deploy workflow over the
  Broker Desk, so deploying is an action taken *at* an account rather than a
  separate destination.
- **Asset identity** — the canonical rendering of one tradeable instrument:
  its symbol with the recognisable mark that goes with it. One renderer owns
  symbol presentation; feature surfaces do not re-derive logos or fallbacks.

## Market Scope shell (resolved 2026-08-13)

**Lineage: neutral.**

The application chrome every route is rendered inside. Broker-independent except
for the two broker status zones it hosts.

- **Market Scope** — the product name for this platform, used in the wordmark,
  the window title, and any user-facing reference to the application itself.
  _Avoid_: quant lab, quant/lab.
- **App menu** — the single canonical statement of the application's information
  architecture: ordered groups, each with ordered items, the first of which is
  the group's default. Every navigation surface projects from it; there is no
  second navigation structure.
  _Avoid_: nav config, route list, sitemap.
- **Rail** — the full-height left navigation strip. Slim by default with one
  icon per group and a hover flyout; pinned, it expands and reserves layout
  width. Distinct from the **transaction rail**, the per-transaction station
  pipeline in the bot panel's operator lens — two different objects, one word.
- **Active menu node** — the single app-menu node one URL resolves to, by longest
  match. It is the sole resolver behind rail highlighting, page title, and
  breadcrumbs, which is why those three can never disagree.
- **Breadcrumb trail** — a pure projection of the active menu node. It is derived
  from a URL and the app menu alone, never registered per route, and it stops at
  the deepest menu node: entity identity belongs in the page header, never in a
  crumb.
- **Contextual account cluster** — the account-scoped status zone in the top bar:
  which broker, paper or live, and how that account is doing right now. Present
  only on account-scoped routes. The account number is never rendered.
- **Global connection zone** — the always-present status zone for the market-data
  connection, independent of which account is on screen. It reports feed health,
  which is why it belongs on every route (see **Market-data bridge**).
- **Full-bleed route** — a route that declares it owns its own edges, so the
  shell adds no inner page padding. Declared by the route, never guessed by the
  page.

## Bot Gallery (resolved 2026-08-14)

**Lineage: live.**

- **Bot Gallery** — the live chart wall for one account: one tile per
  non-retired bot, fed by a single aggregated stream. A stopped bot keeps its
  tile; a retired bot has none, because an action offered on a retired tile
  would be a lie.
  _Avoid_: bot list, dashboard, Bot Sprite Gallery (an unrelated illustration
  showcase).
- **Tile** — one bot's place on the wall, keyed by strategy instance. Its chart
  is per-symbol and shared, so many tiles watching one symbol cost one
  subscription, not many.
- **Stream epoch** — the identity of the current stream generation. A change
  means the client's accumulated state is no longer continuous with the server's
  and must be rebuilt from a fresh snapshot rather than patched.
- **Fill event key** — the stable identity of one fill on a chart. It is
  deliberately **not** `order_ref`: every partial fill of one order shares an
  `order_ref`, so merging on that would let a later partial silently replace an
  earlier one instead of the two coexisting.
- **Session change** — a symbol's return measured from the first to the last bar
  *of the current session*. It is computed once on the backend and never derived
  on the client from the first bar in a buffer, which can belong to a prior
  session.
  _Avoid_: day change, Δ% as a client computation.
- **Day P&L** — realized P&L for the session plus open P&L, computed once on the
  backend. Adding the two already-fetched numbers on the client would be a second
  P&L authority outside the one that owns it.

## Flatness boundary (resolved 2026-08-17)

**Lineage: neutral.**

Decision record: ADR 0036. Sharpened during a `grill-with-docs` session on
wayfinder ticket #1597, after the numeric authority census found the word doing
load-bearing work with no definition behind it.

- **Flat** — a quantity small enough that the system treats it as no position.
  One rule decides it everywhere: `abs(quantity) >= 1e-9` is exposure, anything
  smaller is flat. Exactly `1e-9` is exposure. There is no second threshold and
  no surface that decides this for itself.
  _Avoid_: zero position, empty, no exposure, "effectively flat".
- **Exposure** — a quantity the flatness rule classifies as a position. Used of
  a symbol, an instance, or an account; the rule is the same at every altitude.
  Distinct from **Account exposure** (the broker-observed net position) and
  **Instance-attributed account exposure** (the Clerk's identity-backed share of
  it), which name *whose* exposure it is, not *whether* there is any.
- **Lot exhaustion** — whether a FIFO lot has been fully consumed by offsetting
  fills. A question about a lot, not about a position, and deliberately **not**
  governed by the flatness rule. Naming the two alike is what previously let a
  P&L tolerance decide an exposure question.
  _Avoid_: flat lot, closed lot, zeroed lot.

## Custody authority (resolved 2026-08-17)

**Lineage: live.**

Decision record: ADR 0037.

- **Custody authority** — the single implementation that owns what an account
  holds and what it owes. An account has exactly one, or none; it is never
  reconciled between two. For Alpaca it is the activated SQLite Clerk.
  _Avoid_: the Clerk (ambiguous — names the component, not the authority),
  custody source, position authority.
- **Activation fence** — the durable, account- and generation-bound record that
  binds an account to its custody authority and its database identity. Absent,
  invalid, or reset, the account has no authority and cannot trade; none of
  those states falls back to another implementation.
  _Avoid_: cutover marker, migration flag, activation flag.
- **Market-data bridge** — the sanctioned use of one broker's feed to supply bars
  to another broker's bots. It carries bars only; order effects always flow
  through the bot's own broker Clerk. IBKR is the bridge for Alpaca bots, so IBKR
  connection health is an Alpaca operating concern without IBKR being an Alpaca
  custody authority.
  _Avoid_: broker connection (conflates the feed with the trading path), data
  broker, shared broker.

## Bot control plane (resolved 2026-08-17)

**Lineage: live.**

Decision record: ADR 0038.

- **Bot control plane** — the single command path that starts, stops, and retires
  a bot, and the writers it owns. A bot identity belongs to exactly one. Alpaca's
  is the in-process runner reached through `routers/broker_bots.py`; the former
  IBKR evaluator path retired on 2026-08-18.
  _Avoid_: lifecycle authority, the evaluator, bot manager.
- **Duty fact** — a fact about whether a bot is on duty, under which run, and
  whether it is retired. For an Alpaca bot these are held and fenced by the
  custody authority; any file carrying them is a projection of it.
  _Avoid_: lifecycle state, duty state, phase (each names a file or a field, not
  the fact).
- **Control intent** — durable operator intent that outlives a run, so a stopped
  bot refuses to restart itself. Deliberately **not** held by the custody
  authority: it must still answer when that authority is unreachable.
  _Avoid_: desired state (names the file), command, pause flag.
- **Commit point** — the single durable write in a multi-artifact sequence that
  decides the sequence happened. Everything written after it is reconstructible
  from it; a crash past it is a repair, never an ambiguity. A launch's commit
  point is the custody authority's run registration.
  _Avoid_: transaction, atomic launch (neither is what this is).
- **Run registration** — the custody authority's durable record that a strategy
  instance exists and that one specific run of it is order-capable. It is a
  launch's commit point: the launch happened if and only if this exists, and it
  is fenced so an instance can have at most one registered active run.
  _Avoid_: deploy state, deployment record, binding, run ledger.
- **Runner restoration record** — the runner's own on-disk record of which
  instance configuration it launched and which run is current, so a restarted
  host can restore its supervision without guessing. It is evidence, never
  authority: where it disagrees with the run registration, the registration
  wins and the record is repaired to match.
  _Avoid_: deploy state, binding, deployment JSON, run ledger.

**"Deploy state" is retired as a term.** It named four different artifact
families at once. The IBKR evaluator's mutable `run_ledger.json` family retired;
the temporary parser retired with ADR 0037. A strict historical-identity reader
remains only to deny collisions against IBKR evidence and cannot write a ledger.
The legacy IBKR account-binding `DEPLOYED`/`ACTIVE`/`RETIRED` executable writers
retired with #1583; durable rows remain readable as historical evidence.
The two live Alpaca families are **Run registration** and **Runner restoration
record** above, and they are never used interchangeably.

## Signal Program build proof and legacy seal migration (resolved 2026-08-21)

**Lineage: live.**

Decision record: ADR 0043. Sharpens **Signal Program** and **Sealed account**
above for the PRD Slice 2 build that actually seals a deployed program and
proves its running build at Start/Resume.

- **Configured-signal seal** — the inner, self-hashed identity of one deployed
  Signal Program: program key/version, golden trace root, every resolved
  parameter as an explicit value/unit/origin triple,
  `parameters_match_validated_settings`, and the closed data/clock contracts
  (provider, symbol, timeframes, calendar, RTH, warmup, pause/replay policy).
  It carries no account, mode, or execution plan.
  _Avoid_: signal hash, program hash (names a field, not the payload).
- **Bot-configuration seal** — the outer, self-hashed identity that wraps a
  configured-signal seal and adds account, mode, Action Plan, quantity,
  carryover policy, and the selected validation event/snapshot.
  `strategy_instance_id` binds exactly one. It is appended alongside — never in
  place of — the strategy instance's original v1 `configuration_hash`; the two
  live as separate create-once artifacts under the same instance directory.
  _Avoid_: v2 hash, seal hash, bot hash.
- **Program build proof** — the Start/Resume admission fact answering whether
  the currently loaded program bytes have a golden-qualification receipt for
  the sealed `(program_version, golden_trace_root)`. One of three closed
  states: `PROVEN`; `UNPROVEN`, which refuses the run as
  `PROGRAM_BUILD_UNPROVEN` before any run or effect; or `NOT_APPLICABLE` for a
  `strategy_key` with no registered Signal Program at all, which does not gate.
  A registration may never acquire this identity by inheriting another
  registration's contract object — each sealed program authors its own.
  _Avoid_: build hash, artifact proof.
- **Program build receipt** — the golden-qualification job's committed
  evidence binding one running-artifact digest to one `(program_version,
  golden_trace_root)`. Minted only after that program's own golden-trace suite
  passes as a fresh subprocess run; a hand-authored or stale receipt is not
  evidence, and a behavior change without a matching version/root bump leaves
  the prior, now digest-mismatched receipt unable to prove the new bytes.
- **Signal-decision digest closure** — the exact file set a program's build
  proof hashes: the transitive first-party import closure of its declared
  artifact roots, minus a documented, reasoned exclusion list of files proven
  unreachable from the decision math before the evaluation stage settles.
  Neither the roots alone nor the full unfiltered import graph is this
  closure; an undeclared drift in either direction fails a dedicated census
  test before it can land.
- **Legacy seal migration** — the append-or-clone rule for a strategy instance
  that predates the v2 seal. Append an exact seal under the same
  `strategy_instance_id` when every semantic field still reconstructs from
  persisted v1 data; otherwise clone a new, deterministically-derived instance
  id with durable, one-directional lineage evidence. Either path leaves the
  original v1 bytes untouched forever; a clone is inspectable under its own id
  but the original can never Resume again under its old one.
  _Avoid_: seal migration, resealing, v2 upgrade.

## Exposure lifecycle closure (resolved 2026-08-24)

**Lineage: live.**

- **Recovery EXIT** — a reduction-only EXIT captured without the active-run fence, anchored to the run recorded on the targeted entry's effect operation. Admitted only by the safe-flatten gates or the stuck-EXIT watchdog policy, and always subject to the REDUCE capability's movement-toward-zero check.
- **Safe flatten** — the two-step operator capability over a prepared `SafeFlattenPlan`: `prepare_safe_flatten` (view) builds the versioned exact-close plan; `execute_safe_flatten` (mutation) submits it as recovery EXITs, re-deriving quantities from durable attributed positions and re-asserting no-active-run inside the capture transaction. Execution is gated to a single strategy-owned leg; account-wide and manual custody stay prepare-only.
- **Redrive** — the watchdog's bounded automatic re-submission of a reduction for a stale `EXIT_NOT_FLAT` episode; identity `exit-redrive-<episode-hex12>-<attempt>`, at most 3 per episode, counted by the command namespace (not a mutable timestamp).
- **`EXIT_STUCK`** — the durable custody-subject escalation raised when redrives exhaust; blocks new exposure, allows reduction toward zero, and clears on the same attributed-flat proof that clears `EXIT_NOT_FLAT`.

## Data lake (resolved 2026-08-27)

**Lineage: live.**

- **Data lake** — the authority for historical bar data: immutable LEAN-format files hold the canonical bytes, with Postgres as a catalog and coordination plane over them. Replaces the policy-keyed cache, which had bytes but no catalog. Governs *historical* bars only; the live broker feed is a separate concern and is not a market-data source (ADR 0049).
- **Catalog** — the Postgres side of the lake: rows describing which artifacts exist, their hashes, provenance, and outstanding claims. It holds statements *about* market data, never market data. Losing it loses the index, not the bytes — it is rebuildable by walking the lake and re-hashing, which is what keeps the lake inside ADR 0001's files-canonical doctrine.
- **Artifact** — one catalogued unit of bar data (a symbol's trading day at a resolution and adjustment mode), identified with its adjustment mode in the identity so adjusted and raw coexist without collision, and carrying the hash of the bytes on disk.
- **Coverage** — what the catalog says is owned for a symbol over a span, measured against trading days from the canonical calendar module. Distinct from the retired cache's completeness test, which counted exchange holidays as days it should have and so could never report complete (issue #1830).
- **Claim / lease** — the coordination rows that make two concurrent ensures for one artifact produce one fetch. Replaces the cache's filesystem advisory lock; the mechanism ADR 0001 anticipated under its third projection-layer trigger.
