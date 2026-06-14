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
