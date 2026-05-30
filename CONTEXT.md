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
