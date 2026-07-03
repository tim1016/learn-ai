# PRD — Broker Session Mirror (IBKR client observatory + robust recovery)

**Status:** Ready for agent. Authored 2026-07-03 from a `grill-with-docs` session.
**Authority:** ADR 0018 (`docs/architecture/adrs/0018-broker-session-mirror-client-observatory.md`) and `CONTEXT.md` §§ "Broker session mirror — client-connection observability" and "Recovery / reconnect & resume semantics". Where this PRD and the ADR/CONTEXT disagree, the ADR/CONTEXT win and this PRD must be corrected.
**Implementer:** Codex (AFK agent). Slices are self-contained with file anchors and acceptance criteria.

---

## Problem Statement

I run multiple IBKR paper bots at once. Right now I cannot tell, at a glance or even after debugging, **which of my bots are actually connected to Interactive Brokers and which are not.** The broker's connection status shows up in ~9 different places in the app and they disagree — the sidebar says "connected" while the Bot Cockpit's attention section says "not connected." Interactive Brokers Gateway shows a channel full of clients writing logs that refresh constantly, but I have no way to see those logs in my app.

Concretely, on 2026-07-03 I started three bots, could not confirm whether they were running, tried to debug it, and **could not find out** — because the tool I was debugging with (the process registry / Bot Cockpit) reported them `offline` even though they were alive and holding live sockets to the Gateway. I need the uncertainty gone and the reality visualized.

## Solution

A single **read-only** page — the **broker session mirror** — that faithfully mirrors, 1:1, every IBKR API client socket the way IB Gateway actually sees it, and streams the Gateway's event activity in a categorized, legible table instead of a raw text firehose.

Its headline job is to answer, unambiguously and within seconds: **"Did my bot actually start and connect — yes or no?"** It does this by reconciling three altitudes that silently drift apart today:

- **Intent** — what the operator started.
- **Registry claim** — what the control plane says (`live`/`offline`).
- **OS/Gateway reality** — the actual established sockets, read from the OS network layer via `lsof`.

When the three agree, the bot is running and connected. When they disagree, *that disagreement is the alert*, and the page names which altitude is lying. The mirror gates nothing (its only mutation is purging diagnostic logs); all trading control stays at its existing canonical sites. Folded into the same effort is a rebuilt, IBKR-aware **recovery/reconnect** state machine, because the current one is basic and its behavior is invisible — the mirror finally makes recovery observable.

## User Stories

1. As an operator, I want a single page that lists every IBKR client socket currently connected to the Gateway, so that I know exactly how many clients exist and stop guessing ("10? 4?").
2. As an operator, I want each socket row attributed to the specific bot that opened it, so that "client 42" reads as "bot PrajiTSLADemo" instead of an opaque number.
3. As an operator, I want to start a bot and see its socket appear on the mirror within seconds (attributed, marked live), so that I can confirm it actually connected.
4. As an operator, I want a bot that started but never opened a socket to be shown as "started, not connected," so that a silent connect-failure is impossible to miss.
5. As an operator, I want the page to reconcile what I intended, what the registry claims, and what the OS actually shows, so that a lying registry (e.g., after a daemon restart) cannot fool me.
6. As an operator, I want each client classified as bot / system / orphaned-bot-socket / ghost, so that infrastructure connections, my bots, dangerous leftovers, and foreign sessions are visually distinct.
7. As an operator, I want a crashed bot whose socket is still held at the Gateway flagged as an "orphaned bot socket" safety hazard (not buried as an anonymous ghost), so that I know a dead bot may still be holding orders/positions or blocking a clientId.
8. As an operator, I want a notification when an orphaned bot socket is detected, telling me it needs attention, so that I can act rather than discover it later.
9. As an operator, I want the remediation guidance for an orphaned socket to be honest (detect → alert → guided steps), so that I am not offered a "close socket" button that the broker API cannot actually deliver.
10. As an operator, I want a socket at the Gateway that no bot I opened can be attributed to (a manual TWS login, an external session) shown as a "ghost," so that foreign access to my account is visible.
11. As an operator, I want each client row to show whether it is CURRENT (confirmed live now) or PAST (closed, or last-known because I lost the observer), so that stale history is never mistaken for live truth.
12. As an operator, I want PAST rows clearly demarcated (muted, "as of T", historical badge) and never rendered as CURRENT, so that a dead run's frozen "connected" snapshot cannot lie to me.
13. As an operator, I want the Gateway's event activity categorized into a small set of meaningful buckets (client lifecycle, link connectivity, recovery/reconnect, data farm, auth/session, order & execution, pacing, fault, unclassified), so that I can understand it instead of reading a scrolling wall of text.
14. As an operator, I want to drill from a client row into that client's own categorized event history, so that I can investigate one bot's broker behavior without noise from the others.
15. As an operator, I want raw IBKR codes and messages available but tucked inside expandable technical detail, so that the primary view stays legible but forensic evidence is one click away.
16. As an operator, I want an event whose code the backend does not recognize to appear in a visible "unclassified" bucket, so that unknown events fail loudly instead of being silently guessed or dropped.
17. As an operator, I want the page fed by a live push stream (SSE), so that it updates as sockets connect/drop without me refreshing.
18. As an operator, I want recent broker-session history retained durably (bounded window), so that I can investigate a reconnect storm that happened overnight while I wasn't watching.
19. As an operator, I want to purge historical diagnostic logs (by time range and/or per client), so that I can clear noise.
20. As an operator, I want purge to be impossible to point at the trading audit trail (WAL, ledger, receipts, fills), so that I can never accidentally destroy ownership/attribution evidence.
21. As an operator, I want purging a client's history to leave its live socket and roster row untouched, so that clearing logs never affects the actual connection.
22. As an operator, when the observer itself is down (data plane / SSE), I want the page to show last-known state marked as PAST rather than blanking or claiming everything is healthy, so that a dead observer never looks like a healthy fleet.
23. As an operator, when ghost/orphan detection is unavailable (host-daemon probe unreachable), I want ghost status shown as UNKNOWN rather than silently "clean," so that "clean" is never a lie.
24. As an operator, I want a client whose last signal is older than a freshness threshold shown as STALE/UNKNOWN, so that staleness is honest.
25. As an operator, I want the recovery/reconnect behavior of each client visible as its own event category, so that I can see when a bot lost its link and whether it recovered.
26. As an operator, I want a link blip (IBKR 1100) handled differently from a dead socket (the connection waits for IBKR's own 1101/1102 rather than churning), so that reconnect stops leaking clientIds on every flap.
27. As an operator, I want IBKR's nightly server-reset window treated as expected (info severity, no alarm/churn), so that a scheduled event doesn't look like a fault.
28. As an operator, I want a recovery that exhausts its attempts to reach a terminal, loudly-surfaced HARD_DOWN state, so that a bot never retries forever in silence.
29. As an operator, I want a bot that drops mid-session to NOT auto-resume trading, so that it never trades through ambiguity.
30. As an operator, I want a reconnected bot to reconcile its orders/positions and, only if provably clean, become resumable, so that recovery re-establishes full broker truth before I resume.
31. As an operator, I want to resume a recovered bot with one click from the Bot Cockpit (not from the mirror), so that the human stays in control of resuming live trading.
32. As an operator, I want the resume gate state (blocked / clearable / running) reflected in the UI, so that I can see when a recovered bot is ready to resume.
33. As an operator, I want a bot that dropped mid-submit (uncertain ack) to stay blocked even after a clean reconnect, so that Schrödinger's order is never resumed into.
34. As an operator, I want the mirror to show times in ET on the primary surface with canonical UTC available in technical detail, so that the display matches the market clock while forensic evidence stays exact.
35. As a developer, I want the socket roster derived from OS truth independent of the in-memory process registry, so that a daemon restart cannot make the mirror lie the way the registry does.
36. As a developer, I want the event classifier to share the single code→meaning table already in the IBKR client, so that the mirror and the safety verdict can never drift on what a code like 1101 means.
37. As a developer, I want the mirror to read live event content from API callbacks (not the encrypted Gateway log), so that no log decryption is ever required.
38. As an operator, I want to see when two bots double-started on the same intent (e.g., two processes 21s apart on the same strategy), so that accidental duplicate launches are visible.

## Implementation Decisions

### Altitude and posture

- The mirror is a **read-only, session-level (fleet-altitude) surface**, distinct from the per-`strategy_instance` Bot Cockpit. It gates nothing. Its only mutating capability is diagnostic purge (Slice 6).
- All trading control (resume/stop/flatten) stays at existing canonical render sites. The mirror may deep-link to the Bot Cockpit but hosts no such control.

### Two spines

- **Socket-enumeration spine (the referee):** the host daemon (`PythonDataService/app/engine/live/host_daemon.py`) enumerates every ESTABLISHED TCP connection to the Gateway port (`IBKR_PORT`, e.g. 4002; see `PythonDataService/app/broker/ibkr/config.py`) via `lsof` (or `/proc/net` equivalent), returning structured rows `{pid, command, local_port, remote_port}`. Attribution: `PID → process args → --run-dir → strategy_instance_id` (parse the `app.engine.live.run start --run-dir …` argv; map run-dir → instance via the run ledger / registry index). The data-plane socket is attributed via `GET /api/broker/health` (`client_id`, `account_id`). This is the authoritative roster, liveness, and attribution.
- **API-event spine (the content):** extend the existing per-client `errorEvent`/connect-disconnect capture (`PythonDataService/app/broker/ibkr/client.py::_record_broker_event`, today writing `connection_events.jsonl`) into a categorized event stream, pushed to the Frontend over the existing SSE transport (prior art: `PythonDataService/app/routers/broker_activity.py`, `live_instances.py`).
- **Rejected:** tailing/parsing the IB Gateway log — encrypted at rest (TWS 977+), GUI-only decrypt. The live broadcasts reach every connected API client in plaintext anyway.
- **Accepted 1:1 fidelity ceiling:** full event detail for our clients; identity-only (existence + PID, no private event content) for sockets we did not open.

### The reconciler (highest-value seam)

- A **pure function**: `(lsof_rows, registry_snapshot, run_dir_index, data_plane_health, event_history) → roster[]`, where each roster row carries: `identity_type ∈ {bot, system, orphaned_bot_socket, ghost}`, `recency ∈ {current, past_closed, past_last_known, unknown}`, `strategy_instance_id?`, `account_id?`, `posture?`, `client_id?`, `pid?`, `last_event_ms` (int64 ms UTC), `recovery_state?`, `connection_epoch?`, and per-category event counts.
- **Three-altitude reconciliation** is computed here: compare intent vs registry-claim vs OS-actual; disagreements become surfaced attention on the row (e.g., `REGISTRY_SAYS_OFFLINE_BUT_SOCKET_LIVE`, `STARTED_BUT_NO_SOCKET`, `SOCKET_WITHOUT_LIVE_PID`).

### Client identity (4-way) and recency (orthogonal)

- **Identity:** `bot` (attributed to a live child) · `system` (data-plane/infrastructure) · `orphaned_bot_socket` (attributable to a known bot whose process died while its socket lingers — safety hazard) · `ghost` (neither live nor attributable).
- **Recency:** `current` · `past_closed` (disconnect recorded) · `past_last_known` (observer lost; last seen at T) · `unknown`. **Invariant: PAST never renders as CURRENT.**

### Event categorization (closed vocabulary)

- Nine categories: **Client lifecycle · Link connectivity · Recovery/reconnect · Data farm · Auth/session · Order & execution · Pacing/throttling · Fault/client-error · Unclassified.** Unmapped codes → Unclassified (fail-visible; never guessed).
- The classifier **shares** the code→meaning table already in `client.py` (`_CONNECTIVITY_LOST_CODES = {1100,1300,2110,504}`, `_CONNECTIVITY_RESTORED_CODES = {1101,1102}`, `_SUBSCRIPTIONS_STALE_CODES`, `_DATA_FARM_DEGRADED_CODES`, `_DATA_FARM_OK_CODES`). Lift it to a shared module if needed; do not fork a second table.
- **Link connectivity** (raw signal) and **Recovery/reconnect** (our response) are separate categories.

### Orphaned-socket remediation (detect → alert → guide)

- Detection: a Gateway-side socket with **no matching live client PID**; attributed via last-known `client_id`/PID history to a known bot.
- Alert: an **operator notice** (ADR 0015 machinery; prior art `docs/architecture/operator-notice-prd.md`) deep-linked to the owning bot's cockpit.
- Guidance ladder: clientId-reclaim probe (confirm IBKR still holds it) → heavier operator remediation (Gateway reset/restart via host daemon/IBC — all-clients — or wait out IBKR timeout). **No surgical "close socket" button** — IBKR exposes no kick-client API.

### Robust recovery state machine (subsumes ADR-0011 halt path)

- Rebuild `PythonDataService/app/broker/ibkr/auto_reconnect_monitor.py` as an explicit state machine and make it the **single** authority for connectivity-driven halt/resume — superseding ADR-0011's `live_engine.py` connectivity-count halt and the 2026-06-22 reconnect-recovery amendment mechanism (cross-reference, don't run both).
- States and transitions (from a prototype of the transition function; trim to decision shape):
  - `HEALTHY --1100/1300/2110--> LINK_INTERRUPTED` (do NOT tear down socket; bounded wait for 1101/1102; nightly-reset window → info severity)
  - `LINK_INTERRUPTED --1102--> HEALTHY` (no resub; epoch unchanged)
  - `LINK_INTERRUPTED --1101--> RESTORING` (re-request market data + open orders + executions + positions; bump `connection_epoch`; run owned-orphan/outside-mutation ladder)
  - `RESTORING --clean reconcile--> HEALTHY`
  - `* --disconnectedEvent/504/507/probe-timeout/wait-expired--> SOCKET_DOWN`
  - `SOCKET_DOWN --> RECONNECTING` (backoff + jitter + max cap; reuse clientId)
  - `RECONNECTING --connect ok--> RESTORING`
  - `RECONNECTING --attempts exhausted--> HARD_DOWN` (terminal, surfaced loudly)
- **Resume is operator-only via `ResumeGuardState`** (ADR 0010 §A3). Recovery reconciles; on a **provably-clean** reconcile (broker exposure == `expected_position_by_symbol`, no owned-orphan ambiguity, no outside-mutation, no in-flight `ACK_FAILED_UNCERTAIN` at the drop) it writes a passing reconciliation receipt that clears the connectivity/reconciliation gate. The operator's Bot Cockpit click sets `desired_state = RUNNING`. Safety-verdict and uncertain-intent-WAL guards stay independent. Gate state `BLOCKED → CLEARABLE → RUNNING` is server-authored; Frontend never re-derives.

### Persistence and purge

- Backfill from durable `connection_events.jsonl` + a **session-level store** for data-plane and ghost/orphan sockets; tail live SSE; bounded rolling retention window.
- Purge endpoint: by time range and/or per client, with confirm. **Scoped to diagnostic broker-session logs only** — never `intent_events.jsonl` WAL, intent ledger, reconciliation receipts, or fill/execution records. Purge never disconnects a client, never removes a live roster row, and can never alter a verdict.

### Wire and display

- All timestamps on the wire/storage are **int64 ms UTC** (roster `last_event_ms`, event timestamps, `as_of_ms`). Primary UI renders **ET**; canonical UTC lives in expandable technical detail (per CONTEXT.md "Exchange-time display").
- No new authority in `.NET` — the mirror is data-plane → Angular over SSE, matching today's broker-health path (no GraphQL involvement).

### Delivery slices (tracer-bullet vertical cuts)

- **Slice 1 — "Is my bot connected right now?" (the referee, end-to-end):** host-daemon `lsof` probe + roster endpoint (three-altitude reconciler, identity + recency) + SSE-fed Angular roster table. Ships the thinnest cut that would have ended the 2026-07-03 debugging session.
- **Slice 2 — Categorized event content:** API-event capture → 9-category classifier (shared `client.py` table) → SSE event stream → per-row drill-down, raw-in-detail, Unclassified fail-visible.
- **Slice 3 — Orphaned-socket detection + ADR-0015 notice + guided remediation.**
- **Slice 4 — Publish actual connected `client_id` from children** into the registry/`engine_runtime.json`; cross-reference lsof PID ↔ client_id ↔ API events.
- **Slice 5 — Robust recovery state machine** + ResumeGuardState wiring; subsume ADR-0011 halt path.
- **Slice 6 — Bounded durable history + operator purge** (diagnostic-only).
- **Slice 7 — Fail-visible degradation** (mirror offline / ghost-detection degraded / per-client staleness; PAST demarcation).

## Testing Decisions

Good tests here assert **external behavior at the highest seam**, never subprocess/`lsof` internals or private signals.

- **The reconciler is the primary seam.** A pure function `(lsof_rows, registry_snapshot, run_dir_index, data_plane_health, event_history) → roster[]`. Table-driven pytest: inject fake lsof rows + fake registry + fake run-dir index and assert the classified roster — including the exact 2026-07-03 scenario (registry says `offline`, lsof shows a live PID → row is `bot`/`current` with a `REGISTRY_SAYS_OFFLINE_BUT_SOCKET_LIVE` attention), the started-but-no-socket case, and the orphaned-socket case (Gateway-side socket, no live PID). No real `lsof`.
- **Socket enumerator seam:** define a `SocketEnumerator` protocol returning structured rows; production shells to lsof, tests inject a fake. Never mock `subprocess` in reconciler tests.
- **Category classifier:** pure `(code/event) → {category, severity, label}`, parametrized over the shared code table (assert 1100/1101/1102/2103/2104/504/etc. land in the right bucket, and an unknown code lands in Unclassified). Prior art: existing broker health/model tests.
- **Recovery state machine:** test the transition function in isolation (1100→LINK_INTERRUPTED without socket teardown; 1101→RESTORING; 1102→HEALTHY; socket-dead→RECONNECTING; exhausted→HARD_DOWN; nightly-window severity). Integration: clean reconcile → passing receipt → `ResumeGuardState` gate clears; mid-submit drop → gate stays blocked. Prior art: existing reconnect/monitor tests and ResumeGuard tests.
- **FastAPI endpoints:** `httpx.AsyncClient` + `ASGITransport` for the roster + purge + SSE endpoints. Prior art: `broker.py`, `broker_activity.py`, `live_instances.py` endpoint tests.
- **Purge boundary test (safety-critical):** assert the purge endpoint cannot touch `intent_events.jsonl`/ledger/receipts/fills, and that purging leaves a live roster row and its socket intact.
- **Frontend (Vitest + Testing Library):** render the roster table from a fake SSE/service; assert identity/recency/attribution render; assert **PAST is never rendered as CURRENT**; assert honest-empty states (observer offline, ghost UNKNOWN, staleness). Prior art: existing broker component specs. Must pass AXE.

## Out of Scope

- Re-plumbing the existing ~9 broker-status surfaces (sidebar banner, connectivity strip, etc.) onto the mirror's spine. The mirror is additive; consolidation is a later decision.
- Fixing the in-memory process-registry amnesia (a durable/rehydrating registry). The mirror **exposes** this defect but does not own it — it is a separate work item that also affects the Bot Cockpit, live bindings, and command routing.
- A surgical single-socket "close" (no IBKR API for it).
- A master-client-id cross-client order view (advanced IBKR config).
- Live config mutation.
- Reading a ghost's private event content (blocked by encryption; identity-only by design).

## Further Notes

- **Load-bearing findings verified live 2026-07-03:** `lsof -iTCP:4002 -sTCP:ESTABLISHED` showed 4 real sockets (data-plane `client_id=42` acct `DUM284968` + 3 host `cmd_start` children, PIDs 21760/22332/88897 = the `deployment_validation`/`PrajiTSLADemo`/`DEPVALJUL1` runs), not the believed 10; the registry reported all three `offline` while they held live sockets; every `engine_runtime.json` had `client_id: null` and a frozen `connection_state: "connected"` (some since Jun 22). These are the concrete scenarios the reconciler tests must encode.
- **Doc-vs-reality conflict to record:** ADR-0011 line 197 ("one shared IBKR connection serves every instance") conflicts with the observed per-child model. The mirror is the referee; the ADR text should be reconciled in a follow-up.
- Follow `.claude/rules/numerical-rigor.md` (timestamp rigor — int64 ms UTC boundaries), `.claude/rules/python.md`, `.claude/rules/angular.md`, `.claude/rules/testing.md`. Slice 5 is a live-trading-path change and carries the full submit-protocol rigor. Run project-scope lint + the relevant test surface before each push; run the thermo-nuclear code-quality review before the first PR per slice.
