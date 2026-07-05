# CONTEXT — Live operator console glossary

Canonical language for the deployed-strategy operator console (the "Paper Run"
page and its backend). This file is a **glossary only** — no implementation
detail, no spec. For the full identity/control-plane term list see
`docs/ibkr-paper-deployment-plan.md` §16.4; this file holds the operator-UI
vocabulary that grilling sharpened and cross-references that list.

## Identity ladder (established — see plan §16.4)

- **strategy_key** — algorithm family (e.g. `spy_ema_crossover`).
- **strategy_instance_id** — one *configured* instance of a strategy_key. The
  unit the operator actually governs. Owns the `ib_client_id`,
  `bot_order_namespace`, durable desired-state sidecar, and (after PR-A) the
  managed-process registry slot. One strategy_key → many instances; one
  instance → many runs over time.
- **run_id** — a single execution (one process lifetime) of an instance. An
  artifact-storage key, **not** the operator's handle.

## Broker-facing identity (sharpened 2026-06-04)

How a fill is attributed to a strategy. The durable chain, distinct from the
ephemeral session id:

- **intent_id** — engine-generated, one per trading intent, created *before*
  the order is placed. The write-ahead idempotency key and the intent ledger's
  primary key.
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
- **bot_order_namespace** — `learn-ai/{strategy_instance_id}/v1`. The
  per-instance ownership scope (unchanged; predates this work). The **`/v1` is
  the `order_ref` *wire-format* version — not a strategy, config, spec, or model
  version.** It versions only how `namespace:intent_id` is encoded into IBKR's
  `orderRef` (delimiter/escaping, intent-id encoding, added segments, parse
  shape). It does **not** bump for parameter changes, code changes, spec-hash
  changes, retunes, or new run_ids — those live in `run_ledger` /
  `strategy_instance_id`. A bump to `/v2` requires an ADR/migration note **and
  dual-read ownership** (recognize both `/v1` and `/v2` as owned until every
  prior-version broker order is closed/reconciled) — otherwise the bot
  classifies its own open orders as foreign and self-poisons.
- **order_ref** — `{bot_order_namespace}:{intent_id}`. The broker-facing
  attribution string set on IBKR's `orderRef` and echoed back on
  open-order/execution callbacks. **The single ownership-proof identity.**
  _Avoid_: `client_order_id` (retired internally — the name encoded the wrong
  model and trained the `live-{order_id}` mistake; kept only as a transitional
  alias at external compatibility edges, if any).
- **perm_id** — IBKR's stable per-TWS-order handle, captured post-submit.
- **exec_id** — per-partial-fill id; dedupes fills.
- **order_id** — IBKR's ephemeral, session-scoped order id. **Convenience for
  same-session API calls only; never an attribution key.** Deriving ownership
  from `live-{order_id}` is the bug class this ladder retires.

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

## Sharpened by grilling (2026-05-30)

- **Bot Cockpit** — the canonical trader-facing name for the deployed-strategy
  operator console. It is the surface where a trader monitors and controls a
  `strategy_instance_id`. Implementation docs may refer to `cockpit-v2`, but
  trader-facing copy should use Bot Cockpit language, not "terminal cockpit" or
  code-oriented names.
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
- **Bot name / strategy instance ID** — one canonical identity for a deployed
  bot. The deploy flow may prefill a random, trader-editable name, but the final
  value is lifetime-unique, system-safe, and is the durable
  `strategy_instance_id` used for paths, ownership, broker attribution, and Bot
  Cockpit identity. There is no separate display-only bot-name variable.
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
- **Instance control room** — the operator console's correct shape. Its subject
  is the **strategy_instance**; the **current run** and its artifacts are
  attached as *evidence*, not as the object being operated. Contrast with the
  current implementation, which behaves like a *run artifact viewer with
  controls attached* — the thing we are correcting.
- **Current run binding** — the mapping `strategy_instance_id → currently bound
  run_id` (and its process). The console operates the instance and routes
  commands to the bound run if one exists. A **stale run selection must never be
  the operator's primary control surface**.
- **Readiness gate** ("can this strategy act on the next bar?") — an
  **instance-scoped** composite verdict computed from: current run binding,
  desired state, process state, broker-observed state, safety flags, hydrate
  status, and artifact freshness. (Detailed inputs tracked in the design, not
  here.)
- **Operator top-strip ladder** — `INSTANCE / PROCESS / CURRENT RUN / DESIRED /
  BROKER`. Reads as an instance being operated, not a run being viewed.

## Binding authority (resolved 2026-05-30)

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

**Durable desired-state is the single operator intent knob**, with one
liveness-independent semantic:

- **PAUSED** — strategy should not make new decisions/orders.
- **RUNNING** — strategy may act when readiness gates pass.
- **STOPPED** — strategy must not restart without explicit operator change.

The intent endpoint (`POST /api/live-instances/{id}/desired-state`): (1) writes
durable intent first; (2) if a live binding exists, enqueues the matching live
actuation command to that run; (3) returns both durable-write status and
live-actuation ack pointer; (4) with no live binding, returns "durable only;
will gate next start."

**Writer contract:**
- *Primary writer* — `/api/live-instances/{id}/desired-state`.
- *Reconciling writers* — the engine command dispatcher and CLI emergency
  controls. They persist intent as **reconciliation, not primary ownership**;
  same-value/idempotent writes are acceptable (version churn, not semantic
  drift).
- **Invariant** — any live actuation of PAUSE/RESUME/STOP must leave
  `desired_state.json` at the same semantic state as the action it executed.
  This makes "paused-but-still-trading" structurally hard: durable state changes
  first, live actuation is queued, the UI shows pending/acked actuation against
  the same intent.

**One-shot command channel** is reserved for true one-shot operations:
`FLATTEN_NOW`, `RECONCILE_NOW`, `MARK_POISONED` (and maybe `DUMP_STATUS` later).
`PAUSE`/`RESUME`/`STOP` are **removed as first-class UI controls** (kept as
backend-compatible verbs for CLI/panic/older run-addressed paths only).

**Command lifecycle** (operator vocabulary; one row per command, not
pending-files-plus-ack-files): `queued` (pending, no ack) → `acknowledged` (ack
with success outcome) | `failed` (ack with error outcome). Staleness is judged
against the **server-provided** poll interval (the dispatcher owns its poll
cadence), not a client-side constant.

## Readiness gate (resolved 2026-05-30)

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

The console renders **no hardcoded indicator names**. The strategy-state panel is
driven by **decision-column descriptors** (`name`, `label`, `type`, `format`)
whose source of truth is the strategy spec (`resolve_decision_columns(spec)`,
plan §16.4 Resolution 5: "spec declares types + nullability + semantics"). The
**delivery vehicle is the status payload** — `/api/live-instances/{id}/status`
ships the resolved descriptors alongside `latest_decision` values, so the UI is
one-fetch, never joins the spec client-side, and a missing descriptor is an
API/test failure rather than a UI interpretation problem. EMA, VWAP-reversion,
and future strategies render through the same path. Likewise `bar_source` rides
in `/status` from the latest decision row (engine-authored provenance, not a
backend recompute).

## Broker-observed state & position ownership (resolved 2026-05-30)

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
- **P0 bug:** `check_unexpected_position` (`pre_flight.py:210`) is account-net
  and **not namespace-aware** — sibling managed instances are flagged as false
  contamination, which can block valid starts or train operators to bypass a
  noisy gate. Making it namespace-aware is **in-scope P0** for this work.

## Control-surface scoping (established — see plan §16.4 Resolution 7)

- **Durable desired state** — instance-scoped, survives crash/reboot:
  `artifacts/live_state/<strategy_instance_id>/desired_state.json`.
- **Per-run command** — ephemeral, run-scoped:
  `artifacts/live_runs/<run_id>/commands/`. One-shot verbs only (post-redesign).
- **Safety flags** (`halt.flag`, `poisoned.flag`) — run-scoped artifacts,
  distinct from durable desired state.

## Sizing authority (resolved 2026-06-08)

Where a live bot's position-*size* decision lives and what it claims. Separates
*who decides quantity* from *who decides the signal*. Sizing the magnitude is a
distinct concern from the alpha/entry logic, and for a **live** bot the two have
different homes.

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

## Resume / Pause / Stop guards — shared resolver (resolved 2026-06-20)

ADR 0010 §A3 and PRD #616 — the three Resume guards (broker safety
verdict, reconciliation receipt, uncertain-intent WAL) are resolved
once-per-request by `ResumeGuardState` and shared across:

- the capability projection (`operator_surface.actions.resume / pause / stop`)
- the desired-state mutation endpoint (re-validates before the durable
  write)
- the CLI `cmd_resume` (no bypass — the `--force` flag was deleted in
  PRD #616)

The closed reason-code vocabulary, the priority order for the
single-line tooltip, and the structured `disabled_reasons` list are
the only set of disabled-reason codes the cockpit's typed lookup
covers.  Unknown codes fail closed.

## Broker session mirror — client-connection observability (resolved 2026-07-03)

A read-only, session-level visualization of every IBKR API client socket — a
faithful mirror of what IB Gateway itself sees. It is **not** an authority: it
gates nothing (contrast the per-instance readiness/safety verdicts); it is a
better *view*. It sits at the backend-authored **fleet/session altitude** (see
"Broker-observed state & position ownership → two altitudes, two authors"),
distinct from the per-`strategy_instance` Bot Cockpit.

- **Broker client** — one IBKR API socket to the Gateway, identified by its live
  `client_id`. There is **no single "broker connection"**: there are N clients
  (the FastAPI data-plane singleton + one per live bot child + any others), each
  with its own independent connection state. The Gateway logs each separately, so
  the sidebar and Bot Cockpit "disagreeing" is often two *different clients* each
  reporting correctly — not one truth shown inconsistently. **Verified empirically
  2026-07-03** via `lsof -iTCP:4002` against a live system: 4 real sockets (1
  data-plane on client_id 42 + 3 host `cmd_start` children), each a distinct TCP
  connection — the per-child model, **not** ADR-0011's "one shared connection
  serves every instance" (that line conflicts with observed reality and the ADR
  must record the gap). The same probe caught the control plane reporting all three
  live children as `offline`/`STOPPED` while they held live sockets — the divergence
  this mirror exists to expose.
- **Socket-enumeration spine (the referee).** The authoritative roster, liveness,
  and attribution come from **`lsof` on the Gateway port, run by the host daemon** —
  every real TCP connection, PID-attributed, needing **no log decryption and no
  child self-report**. `PID → process args → --run-dir → strategy_instance_id` is
  the enrichment join available **today** (every `engine_runtime.json` carries
  `client_id: null` — the client_id is never published). Ghost/orphan detection
  falls out for free: a Gateway-side socket with **no matching live client PID** is
  orphaned/half-open; a live client PID the **registry calls `offline`** is a stale
  control plane. This spine complements the live API-event spine below: `lsof` owns
  *who is connected*, the API callbacks own *what each is saying* (the 9 categories).
- **Primary job — answer "did my bot actually start and connect?"** The mirror's
  north star (and acceptance test) is reconciling **three altitudes** that drift
  apart silently today: operator *intent* (what was started), the process
  *registry's claim* (`live`/`offline`), and *OS/Gateway reality* (`lsof` sockets).
  The registry is **in-memory only** (`ProcessRegistry._managed = {}`, no
  rehydration), so a host-daemon restart makes it forget live children — they keep
  running and holding sockets but report `offline` (observed 2026-07-03: three live
  children the registry called `offline`; the operator started bots and could not
  confirm they were running). The `lsof` spine reads OS truth independent of that
  memory, so it cannot be fooled the same way; disagreement across the three
  altitudes is itself the surfaced alert, naming which altitude is lying. Fixing the
  registry's amnesia (a durable/rehydrating registry) is an **adjacent** concern the
  mirror *exposes* but does not own.
- **Client identity type** — every observed `client_id` is classified as exactly
  one of:
  - **bot client** — opened by a live child; enriched with its
    `strategy_instance_id`, account, and posture.
  - **system client** — infrastructure, not a strategy (the data-plane
    singleton, a host-runner-owned session). Labeled as infrastructure, never
    dressed up as a bot.
  - **orphaned bot socket** — a `client_id` **attributable** to a known bot (its
    last-published id) whose owning process has died while the socket lingers at
    the Gateway. **Not a ghost** — a named, crashed bot whose connection IBKR still
    holds. Treated as a **safety hazard** (can hold open orders/positions, collides
    with the bot's clientId on restart): raises an **operator notice** (ADR-0015)
    with remediation, never a passive row.
  - **ghost client** — a `client_id` at the Gateway that is **neither live nor
    attributable** to any bot we opened (a manual TWS login, an external/foreign
    session). Detected and surfaced honestly; distinct from an orphaned bot socket.
- **Connection recency axis (orthogonal to identity type).** Independent of *who*
  owns a socket (bot/system/ghost), every row carries *how sure we are it is live
  right now*:
  - **CURRENT** — confirmed connected now (a fresh event/probe within the
    freshness threshold). Rendered active.
  - **PAST** — recorded history exists but a current connection cannot be
    confirmed. Two honest flavors: **closed** (a disconnect was recorded —
    definitively gone) and **last-known** (the observer was lost — data-plane/SSE
    down or stale; last seen connected at T, current state indeterminate).
    Rendered clearly demarcated (muted, "as of T", historical badge).
  - **UNKNOWN** — no basis at all (no history; observer down from the start).
  **Invariant: PAST is never rendered as CURRENT.** When the observer goes down the
  page does not blank or collapse to a bare UNKNOWN — it demotes rows to
  PAST/last-known with recorded history fully browsable, demarcated so an operator
  can never mistake last-known history for live truth. The honest-empty rule
  applied to *time*, not just to verdicts.
- **Orphaned-socket remediation is detect-alert-guide, not one-click close.** IBKR
  exposes **no surgical "kick client N" API**, and a cleanly-exited process's socket
  is already closed by the OS — so a *lingering* socket means half-open or a hung
  process IBKR hasn't noticed. The mirror therefore **detects**, **alerts** (operator
  notice deep-linked to the owning bot's cockpit), and **guides**: a clientId-reclaim
  probe to confirm IBKR still holds it, then the heavier operator remediations
  (Gateway reset/restart via the host daemon/IBC — all-clients — or waiting out
  IBKR's timeout). No button promises a surgical close the broker API cannot deliver;
  the heavier Gateway-reset action lives at a session/host admin site, not the mirror.
- **Live API-event spine** — the event stream is the API callbacks each of our
  own clients already receives from the Gateway (`errorEvent`, connect/disconnect),
  **not** the Gateway log files. On-disk logs are encrypted at rest (TWS Build
  977+) and readable only through the Gateway GUI; the same events are broadcast
  live and in plaintext to every connected API client, so the mirror listens to
  the broadcast rather than decrypting the vault.
- **1:1 fidelity ceiling (accepted 2026-07-03)** — full event detail for **our**
  clients (data-plane + bot children), since we receive their broadcasts and
  own-lifecycle directly. **Identity-only** for ghost clients: existence is
  detectable (via `client_id`-in-use collisions and host-daemon filename metadata
  of the encrypted `api.<id>.<day>.log` set) but private event *content* is not —
  it is neither broadcast to us nor decryptable outside the Gateway. A documented,
  accepted limit, not a defect to engineer around.
- **Client enrichment join** — `actual connected client_id → strategy_instance_id`.
  "Actual" because a child may reconnect under a different id after an
  `IbkrClientIdInUseError` collision, and the Gateway's truth is the id it
  actually holds. This join key is **not recorded durably today** (neither the
  process registry nor `engine_runtime.json` carries `client_id`); publishing it
  is the enrichment's prerequisite.
- **Categorized broker events, not a text stream** — raw log lines are never the
  primary surface. Each event is classified into a closed backend vocabulary
  (extending the **Event narrative registry** pattern); raw fields live only in an
  expandable technical-details area. The classifier **shares** the single
  code→meaning table already in `client.py` (`_CONNECTIVITY_LOST_CODES`, etc.) — it
  does not fork a second broker-event vocabulary.
- **Bounded durable history with operator purge.** The mirror backfills from the
  durable per-client `connection_events.jsonl` (plus a session-level store for the
  data-plane and ghost clients) and tails live SSE; retention is a bounded rolling
  window. The operator may **manually purge** historical entries (by time range
  and/or per client). **Purge is scoped to diagnostic broker-session logs only —
  it never touches the trading audit trail** (`intent_events.jsonl` WAL, intent
  ledger, reconciliation receipts, fill/execution records), which stay immutable
  as ownership/attribution proof. Purging history never disconnects a client, never
  removes its live roster row, and — because diagnostic logs are **never** an input
  to a safety/ownership/resume decision — can never alter a verdict. Purge is the
  mirror's only mutating capability; it is otherwise read-only.

The robust recovery state machine (folded into Phase 1) is the **single**
authority for connectivity-driven halt/resume — it **subsumes** the ADR-0011
reactive-halt-on-transition path (`live_engine.py` connectivity-count snapshot);
there are never two halt-on-transition mechanisms.

- **Recovery reconciles, it does not resume.** On reconnect the machine
  distinguishes IBKR's real failure modes (1100/2110 link-interrupt → *wait* for
  1101/1102, not a socket teardown; 1101 → re-request market data **+ open orders
  + executions + positions** and bump `connection_epoch`; socket-dead → backoff
  reconnect; exhausted → terminal `HARD_DOWN`). It then runs the owned-orphan /
  outside-mutation ladder. A **provably-clean** reconcile (broker exposure ==
  `expected_position_by_symbol`, no owned-orphan ambiguity, no outside-mutation,
  no in-flight `ACK_FAILED_UNCERTAIN` at the drop) produces a passing
  reconciliation receipt; **any** ambiguity stays hard-blocked.
- **Resume is operator-only, from the Bot Cockpit.** A clean reconcile *clears
  the connectivity/reconciliation gate* but **never auto-resumes trading**. The
  bot resumes only when the operator clicks Resume on the bot control panel (sets
  `desired_state = RUNNING`). This wires into the existing **ResumeGuardState**
  (ADR-0010 §A3, PRD #616): recovery feeds its reconciliation-receipt guard; the
  safety-verdict and uncertain-intent-WAL guards stay independent, so a mid-submit
  drop stays blocked even after a clean reconnect.
- **Gate state is server-authored and reflected in the UI.** `BLOCKED →
  CLEARABLE (clean reconcile) → CLEARED/RUNNING (after the click)` via
  `operator_surface.actions.resume`. The Bot Cockpit renders the verdict; it never
  re-derives it.
- **The broker session mirror stays read-only.** It *visualizes* the reconnect
  and gate-cleared events (and may deep-link to the Bot Cockpit) but carries **no
  Resume control** — the resume action's render site remains the Bot Cockpit.

## Daemon diagnostics — control-plane health (resolved 2026-07-04)

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

Sharpens the **Validated strategy package** entry above for the Deploy-a-strategy
redesign. Draws the line between what the *validated strategy* carries and what
the *deployment* binds.

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
- **Authoring-boundary supersession** — this **supersedes** the earlier
  "The Deploy a strategy page owns creating or selecting this package" clause in
  **Validated strategy package** (above). Authoring now splits: the **Strategy
  Validation page** owns *making a strategy validated*; the **Deploy a strategy
  page** only *selects* an already-validated strategy. Engine Lab remains a
  non-authoring surface for this workflow.
- **Strategy catalog & validation state** — the Strategy Validation surface is a
  catalog of **all** strategies carrying a validation state, not a list of only
  the good ones. A strategy is **validated** (has a QC backtest ID + saved audit
  copy + passing port-vs-QC reconciliation → deployable) or **unvalidated** (shown
  as "needs validation" → not deployable). **Validation is the gate to
  deployability**: only validated strategies appear in the Deploy flow's strategy
  dropdown; adding validation to an unvalidated strategy flips it deployable. The
  validation binding is **backend-owned and stored** (already present today as the
  `qc_cloud_backtest_id` + `qc_audit_copy_path`/`sha` + `strategy_spec_path`/`sha`
  chain in each `run_ledger.json`, plus the qc-shadow attribution and the
  `docs/references/reconciliations/` reports) — the surface consolidates it, it is
  not re-typed.
- **Deployment binding surface** — the Deploy-a-strategy flow selects one validated
  strategy (auto-populating its validation evidence: settings file, QC backtest ID,
  audit copy, reconciliation verdict — none typed) and binds the independent,
  per-deployment inputs: signal stream, position sizing, action-plan legs, launch
  options, deployment name, and the read-only connected account.
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

### Revised 2026-07-05 — validation is a human flag; Deploy re-homes to Bots (see ADR 0023)

A `grill-me` session revised several points above. Where they conflict, **ADR 0023 wins**:

- **Validation is a human flag, not an automatic verdict.** The Validation page
  **runs both engines** — our Python engine and the LEAN engine (QuantConnect is the
  LEAN reference; the backtest ID only *pins* the reference run) — and displays how
  well their buy/sell entry signals and PnL match (`DivergenceCategory` + a headline
  %). A **person** sets the `validated` / `invalidated` flag; there is **no automatic
  threshold** (a ~95% match is human guidance, not a rule). The QC backtest ID is
  **provenance**, not the credential. This replaces "validated iff … a passing
  port-vs-QC reconciliation" above.
- **The flag is always saved with its evidence + a reason — accountability, not
  prevention.** The system never blocks the human: a strategy flagged `validated` at
  0% agreement is allowed and is stored with the full evidence snapshot (both engines'
  output, match %, `DivergenceCategory`, backtest ID, flagger, timestamp) and a
  required reason. That persisted reason is the "documented reason"
  `numerical-rigor.md` requires for accepting a *behavioral* equivalence.
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
