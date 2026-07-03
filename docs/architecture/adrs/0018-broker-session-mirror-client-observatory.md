# ADR 0018 — Broker session mirror: a read-only client observatory over an lsof + API-event spine, with recovery subsuming the ADR-0011 halt path

**Status:** Proposed 2026-07-03. Drafted during the 2026-07-03 `grill-with-docs` session ("single truth authority for broker"). Vocabulary recorded in `CONTEXT.md` § "Broker session mirror — client-connection observability" and § "Recovery / reconnect & resume semantics". **Load-bearing claims verified live before drafting:** `lsof -iTCP:4002 -sTCP:ESTABLISHED` showed **4 real sockets** (data-plane on `client_id=42` acct `DUM284968` + 3 host `cmd_start` children, PIDs 21760/22332/88897), **not** the operator-believed 10; the host process-registry reported those three children `offline`/`STOPPED` **while they held live IBKR sockets**; every `engine_runtime.json` carried `client_id: null` and a frozen `connection_state: "connected"` (some since Jun 22); IBKR log-at-rest encryption confirmed against `interactivebrokers.github.io/tws-api/support.html` (TWS Build 977+).

**Decision drivers:** Broker connection status is displayed in ~9 Frontend surfaces (sidebar banner, Bot Cockpit connection pill, connectivity strip, account monitor, deploy form, …) that disagree — the sidebar reads the data-plane singleton's health while the Bot Cockpit reads a per-child observation, and there is no surface that shows *what the Gateway actually sees*. The operator has no way to visualize the IB Gateway "channel" logs in-app, and no way to tell a healthy fleet from a graveyard of orphaned sockets. The live inspection above proved the control plane already reports state that contradicts the OS network layer. The operator asked for a faithful, 1:1 visualization of every client socket — explicitly a **better view, not a new authority**.

**Related:** ADR 0004 (instance-addressed control plane / multi-process registry — the source of PID↔instance attribution), ADR 0011 (broker safety verdict + fail-closed reactive halt-on-transition, and its 2026-06-22 reconnect-recovery amendment — **this ADR's Decision 5 supersedes that amendment's recovery mechanism**), ADR 0010 (operator-action contract / `ResumeGuardState` — the resume path this ADR feeds), ADR 0013 (operator-surface: judgment vs evidence), ADR 0014 (broker-authored operator view / narratives — the Event narrative registry pattern), ADR 0015 (operator notice contract — the orphaned-socket alert), `CONTEXT.md` §§ "Broker session mirror", "Recovery / reconnect & resume semantics", "Broker-observed state & position ownership" (two altitudes, two authors).

## Context

Today's state (verified against code and a live system at session time):

| Concern | Today |
|---|---|
| Number of broker connections | Not one. `lsof` on the Gateway port showed **4 independent sockets** (data-plane `client_id=42` + 3 `cmd_start` children). Each child opens its own `IbkrClient()` and `connect()`s (`run.py:884`, `:1814`); `config.py:107` documents a single-owner assumption ("host-venv `cmd_start` process owns *the* IBKR session (client_id=42)") that the running system contradicts. |
| Divergent truths | The sidebar reads `GET /api/broker/health` (data-plane singleton); the Bot Cockpit reads `operator_surface.broker.connection` (per-child `engine_runtime.json`). The process registry reported 3 children `offline` **while they held live sockets**. No surface reconciles these against what the Gateway sees. |
| Gateway logs in-app | None. The "channel" the operator watches is the IB Gateway system-log window; there is no in-app view. Logs are **encrypted at rest** (TWS 977+), decryptable only via the Gateway GUI — so a file tailer cannot read them. |
| Client → bot attribution | Absent. `engine_runtime.json` carries `client_id: null`; the process registry `ManagedProcess` has no `client_id`. Nothing maps a Gateway client to a bot durably. |
| Recovery / reconnect | `AutoReconnectMonitor` does app-level probes, throttled 1101 subscription recovery, and exponential-backoff reconnect — but conflates a 1100 link-blip with a dead socket (both hit `_attempt_reconnect_loop`), is not nightly-reset-aware, retries forever with no terminal state, and does not couple resubscription to order/exec/position re-request + ownership reconcile. ADR-0011's 2026-06-22 amendment layered a recovery sweep on top. |
| Staleness | `engine_runtime.json` snapshots freeze at "connected" and are never demoted; `runtime_freshness.py` demotes *posture* but nothing demotes a dead run's connection claim. |

## Decision

### 1. The mirror is a read-only, session-level observatory — it gates nothing

The broker session mirror is a new surface at the backend-authored **fleet/session altitude** (distinct from the per-`strategy_instance` Bot Cockpit). It *visualizes*; it is **not** an authority and gates no trading decision. Its **only** mutating capability is purging diagnostic history (Decision 6). All trading control (resume, stop, flatten) remains at its existing canonical render sites; the mirror may deep-link but hosts no such control.

### 2. Two-spine observability — `lsof` for the roster, API callbacks for the content; never the Gateway log

Truth about the broker comes from two complementary spines, **neither of which decrypts a log file**:

- **Socket-enumeration spine (the referee).** The host daemon runs `lsof` (or the `/proc/net` equivalent) against the Gateway port and reports every ESTABLISHED TCP connection, PID-attributed. This is the authoritative **roster, liveness, and attribution**: `PID → process args → --run-dir → strategy_instance_id` is the enrichment join available today (working around the unpublished `client_id`). Ghost/orphan detection falls out for free — a Gateway-side socket with no matching live client PID is orphaned/half-open; a live client PID the registry calls `offline` is a stale control plane.
- **API-event spine (the content).** The categorized event stream is the `errorEvent`/connect/disconnect callbacks each of our own clients already receives from the Gateway (extending today's `connection_events.jsonl` capture), pushed to the Frontend over the existing SSE transport. This carries *what each client is saying* (the 9 categories, Decision 4).

**Rejected: tailing/parsing the IB Gateway log.** Logs are encrypted at rest (TWS 977+) and decryptable only through the Gateway GUI. The live-broadcast events reach every connected API client in plaintext anyway, so the vault is unnecessary.

**Accepted 1:1 fidelity ceiling.** Full event detail for **our** clients (data-plane + children). For a socket we did not open, the `lsof` spine still proves **existence and PID** (identity), but its private event *content* is neither broadcast to us nor decryptable — **identity-only, by documented limit**. Also publish each child's *actual connected* `client_id` upward (new plumbing) to cross-reference the two spines; PID attribution is the interim join until then.

### 3. Four-way client identity × an orthogonal recency axis; PAST never renders as CURRENT

Every observed socket carries two independent classifications:

- **Identity:** `bot client` (attributed to a live child) · `system client` (data-plane / infrastructure) · **`orphaned bot socket`** (attributable to a *known* bot whose process died while the socket lingers — a safety hazard, Decision 7) · `ghost client` (neither live nor attributable — a foreign/manual session).
- **Recency:** `CURRENT` (fresh signal) · `PAST` — `closed` (a disconnect was recorded) or `last-known` (the observer was lost; last seen at T) · `UNKNOWN`.

**Invariant: PAST is never rendered as CURRENT.** When the observer degrades, rows demote to PAST/last-known with recorded history browsable and demarcated (muted, "as of T", historical badge) — the honest-empty rule applied to *time*. This is the direct fix for the frozen-`"connected"` `engine_runtime.json` graveyard.

### 4. A closed 9-category event vocabulary that shares `client.py`'s code→meaning table

Raw log lines are never the primary surface. Each event is classified into a **closed backend vocabulary** (extending the Event narrative registry pattern, ADR 0014); raw fields live only in expandable technical detail. The categories: **Client lifecycle · Link connectivity · Recovery/reconnect · Data farm · Auth/session · Order & execution · Pacing/throttling · Fault/client-error · Unclassified** (fail-visible; unmapped codes are never guessed). The classifier **shares the single code→meaning table already in `client.py`** (`_CONNECTIVITY_LOST_CODES = {1100,1300,2110,504}`, `_CONNECTIVITY_RESTORED_CODES`, `_DATA_FARM_*`) — it does not fork a second broker-event vocabulary. **Link connectivity** (raw signal) and **Recovery/reconnect** (our response) are deliberately split, because today they are fused in `connection_lost` and that fusion is why recovery is unobservable.

### 5. A robust recovery state machine, folded into Phase 1, that subsumes the ADR-0011 halt path

The recovery mechanism is rebuilt as an IBKR-aware state machine and becomes the **single** authority for connectivity-driven halt/resume — it **supersedes** ADR-0011's reactive halt-on-transition mechanism (`live_engine.py` connectivity-count snapshot) and the 2026-06-22 reconnect-recovery amendment's mechanism. Two halt-on-transition paths for one connection lifecycle is the single-source-of-truth violation this effort exists to kill.

- **States:** `HEALTHY` → `LINK_INTERRUPTED` (1100/1300/2110: **do not tear down the socket**; bounded wait for IBKR's own 1101/1102; nightly-reset-window-aware → info severity) → `RESTORING` (1101: re-request market data **+ open orders + executions + positions**, bump `connection_epoch`, run the owned-orphan/outside-mutation ladder) or fast-path back on 1102 → `HEALTHY`; and `SOCKET_DOWN` (`disconnectedEvent`/504/507/probe-timeout/wait-expired) → `RECONNECTING` (backoff **+ jitter + max cap**, reuse clientId) → `HARD_DOWN` (attempts exhausted — **terminal, surfaced loudly**, not infinite silent retry).
- **Resume is operator-only, from the Bot Cockpit.** Recovery **reconciles, it does not resume.** A *provably-clean* reconcile (broker exposure == `expected_position_by_symbol`, no owned-orphan ambiguity, no outside-mutation, no in-flight `ACK_FAILED_UNCERTAIN` at the drop) writes a passing reconciliation receipt that clears the connectivity/reconciliation gate; the operator's click sets `desired_state = RUNNING`. This wires into the existing `ResumeGuardState` (ADR 0010 §A3): recovery feeds its reconciliation-receipt guard; the safety-verdict and uncertain-intent-WAL guards stay independent, so a mid-submit drop stays blocked even after a clean reconnect. Gate state is server-authored (`BLOCKED → CLEARABLE → RUNNING`); the Frontend never re-derives it.

Folding this into Phase 1 (rather than mirror-first) is an accepted cost: it makes Phase 1 a live-trading-path change under the full `.claude/rules/numerical-rigor.md` + submit-protocol rigor, with a regression test per transition.

### 6. Bounded durable history with operator purge — diagnostic logs only, never the audit trail

The mirror backfills from the durable per-client `connection_events.jsonl` (plus a session-level store for the data-plane and ghost/orphan sockets) and tails live SSE; retention is a bounded rolling window. The operator may **purge** historical entries (by time range and/or per client, with confirm). **Purge is scoped to diagnostic broker-session logs only — it never touches the trading audit trail** (`intent_events.jsonl` WAL, intent ledger, reconciliation receipts, fill/execution records), which stay immutable as ownership/attribution proof. Purge never disconnects a client or removes its live roster row; because diagnostic logs are never an input to a safety/ownership/resume decision, purging can never alter a verdict.

### 7. Orphaned-socket remediation is detect → alert → guide, never a one-click "close"

An orphaned bot socket (Decision 3) raises an **operator notice** (ADR 0015) deep-linked to the owning bot's cockpit. It is **not** a passive row: it can hold open orders/positions and will collide with the bot's clientId on restart. But **IBKR exposes no surgical "kick client N" API**, and a cleanly-exited process's socket is closed by the OS — so a *lingering* socket means half-open or a hung process. The remediation ladder is therefore: **detect** (`lsof` Gateway-side socket with no live client PID) → **alert** → **guide** (a clientId-reclaim probe to confirm IBKR still holds it, then the heavier operator remediations: Gateway reset/restart via the host daemon/IBC — all-clients — or waiting out IBKR's timeout). No button promises a close the broker API cannot deliver; the heavier Gateway-reset lives at a session/host admin site, not the mirror.

### 8. UI: roster-first table, drill-down to categorized events; fail-visible to PAST/UNKNOWN

The page **is** a table — one row per socket (identity type · enriched bot/account/posture · recovery-state · recency · last-event · epoch · per-category counts), drill-down to that socket's categorized event history. Failure degrades visibly: **mirror offline** ("observer offline — last known as of T"), **ghost detection degraded** (host-daemon `lsof` unreachable → ghost/orphan status UNKNOWN, never "clean"), **per-client staleness** (→ STALE/UNKNOWN). Nothing optimistic renders when the observer cannot see.

## Scope

This ADR governs the **broker session mirror** surface and the recovery-mechanism rebuild (Decision 5). It does **not** re-plumb the existing ~9 broker-status surfaces into one authority — they remain as-is; the mirror is additive (a faithful view, per operator intent). Consolidating those surfaces onto the mirror's spine is a possible later decision, explicitly out of scope. Live config mutation, a surgical single-socket close, and a master-client-id cross-client order view are out of scope.

## Consequences

**Positive:**
- One surface finally shows *what the Gateway actually sees*, PID-attributed and 1:1 for owned clients — the live inspection proved the current control plane contradicts the OS network layer, and this closes that gap.
- Orphaned/half-open sockets and stale-registry rows (both caught live during the session) become first-class, honest signals instead of silent hazards.
- Recovery becomes observable and correct: 1100≠socket-dead, nightly-reset-aware, a terminal `HARD_DOWN`, and ownership-reconciled resume through the existing `ResumeGuardState` — with a single halt authority, not two.
- No log decryption, no brittle vendor-text parsing; the `lsof` + API-callback spines are structured and robust.

**Negative:**
- Phase 1 now includes a live-trading-path recovery rebuild (operator's explicit choice) — higher test burden and risk than a read-only page alone.
- New plumbing: publish each child's actual `client_id`; a host-daemon `lsof` probe; a session-level diagnostic store for non-child sockets.
- The mirror gains one mutating capability (diagnostic purge), a deliberate exception to its read-only stance.

**Non-consequences:**
- The four-layer `place_paper_order` enforcement, the connection-time multi-account refusal, and ADR-0011's *identity* verdict + halt-on-*identity*-transition are unchanged; Decision 5 supersedes only the **connectivity** recovery/halt *mechanism*, not the identity contract.
- The trading audit trail (WAL, ledger, receipts, fills) is untouched and remains immutable (Decision 6).
- The existing ~9 broker-status surfaces are not modified by this ADR (Scope).
