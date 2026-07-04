# Broker session mirror slice 1 — divergences and critical follow-ups

Date: 2026-07-03

This note records where slice 1 intentionally diverges from the full PRD/ADR
scope. The branch implements the first reviewable vertical cut: host-daemon
socket observation, a data-plane broker-session mirror API/SSE stream, the pure
three-altitude reconciler, and the Angular roster table.

## Critical divergences

1. **Recovery state machine deferred.**
   ADR 0018 says the robust recovery state machine is folded into phase 1, but
   that is a live-trading-path rewrite. I did not mix it into the read-only
   mirror PR because it would create a giant review and would need its own
   transition-function tests, ResumeGuard receipt wiring, and submit-uncertain
   gate validation.

2. **Data-plane socket attribution is not yet true 1:1.**
   `/api/broker/health` publishes `client_id` and account, but not the local
   socket source port or PID. Slice 1 therefore renders a system/data-plane row
   from health when connected, while child bot rows are attributed from
   `lsof`/`--run-dir`. Exact de-duplication between the data-plane health row
   and an unattributed OS socket row needs a later contract that publishes the
   data-plane source socket identity.

3. **True orphaned-socket proof needs session history.**
   The reconciler can classify a known bot socket with no live PID as
   `orphaned_bot_socket`, but production-quality orphan attribution needs the
   durable session-level history from later slices: last-known PID/clientId,
   close events, and observer-loss demotion. Slice 1 does not raise ADR 0015
   operator notices.

4. **Categorized event content is not in this PR.**
   The roster surface is live over SSE, but API-event categorization, per-client
   drill-down, unclassified event surfacing, raw technical detail for IBKR codes,
   durable history, and diagnostic purge are still separate slices.

5. **ADR 0011 documentation conflict remains unresolved.**
   The PRD calls out the conflict between ADR 0011's older "one shared IBKR
   connection" language and the observed per-child socket model. This branch
   implements the mirror as the referee but does not rewrite ADR 0011.

## Review boundary

The intended review boundary for this PR is slice 1 only:

- Does the host daemon expose a bounded, authenticated socket probe?
- Does the pure reconciler correctly surface stale registry vs live socket,
  started-without-socket, ghost, orphan-shaped, and system rows?
- Does the public data-plane endpoint degrade honestly when the host-daemon
  probe is unavailable?
- Does Angular render CURRENT, PAST, and UNKNOWN without deriving broker meaning
  client-side?

## Slice 2 addendum — categorized event content

Date: 2026-07-03

The stacked slice-2 branch adds the shared IBKR event-code vocabulary,
classified diagnostic events, an `/events` REST endpoint, an `/events/stream`
SSE endpoint, per-row category counts when `client_id` is known, and Angular
row drill-down for categorized event history.

Additional divergences / limits:

1. **Child bot event drill-down remains blocked by missing child `client_id`.**
   The event stream can be filtered precisely when a roster row has
   `client_id`, but current child runtime artifacts still publish
   `client_id: null`. Rows without a client id therefore render an honest-empty
   event panel rather than guessing by PID/run-dir. PRD slice 4 must publish
   actual connected child client ids before child drill-down can be complete.

2. **Event stream is diagnostic JSONL polling, not a durable session store.**
   Slice 2 reads the existing `_broker/connection_events.jsonl` and streams new
   line-indexed classified rows. It does not implement the bounded
   session-level store, retention policy, or purge endpoint from PRD slice 6.

3. **Classifier covers the existing client callback vocabulary only.**
   Codes already understood by the IBKR client share a single table with the
   mirror. Unknown `IBKR_CODE` values are visibly classified as `unclassified`.
   Order/execution, pacing, and auth/session categories exist in the wire
   vocabulary but need additional capture sites before they can be populated.

## Slice 3 addendum — child client id publishing

Date: 2026-07-03

The stacked slice-3 branch intentionally pulls the PRD's slice 4 forward:
child runtime snapshots now publish `broker.client_id`, and the mirror runtime
index uses it to attach per-child event counts and drill-down rows to bot
sockets.

Critical change from the PRD order:

1. **Child `client_id` publishing precedes orphan-notice work.**
   The PRD lists orphaned-socket notices before child `client_id` publication,
   but precise orphan attribution and per-client event history both need the
   child client id. Implementing the schema/wiring first keeps the next
   orphan-notice slice from relying on PID/run-dir guesses alone.

Remaining limits:

1. **Existing historical `engine_runtime.json` files remain client-id unknown.**
   The schema is backward compatible, so older artifacts parse with
   `broker.client_id = null`. They cannot retroactively populate event
   drill-down unless a later bounded history/index maps the old PID or run dir
   to the client id.

2. **This does not yet create ADR 0015 notices.**
   The branch only publishes the child id and wires it into mirror attribution.
   Orphaned-socket notice creation and guided remediation remain the next
   review slice.

## Slice 4 addendum — orphaned-socket notice and guidance

Date: 2026-07-03

The stacked slice-4 branch adds a typed ADR 0015 `OperatorNotice` for
`orphaned_bot_socket` rows, renders the backend-authored notice in the broker
session mirror, and ships a runbook for the remediation ladder.

Critical limits:

1. **The notice is projected with the mirror row, not persisted as an incident.**
   ADR 0015 supports both ephemeral projection notices and persisted incidents.
   This slice uses the projection form because the mirror is already a
   read-only observability surface and durable incident lifecycle semantics
   require a session-level store from the history/purge slice.

2. **Guidance remains detect → alert → guide.**
   The notice links the operator to the owning Bot Cockpit and the runbook
   explains IBKR/Gateway remediation. It still does not offer a surgical socket
   close, because the PRD/ADR explicitly reject promising an API action IBKR
   does not expose.

3. **True orphan proof still depends on history quality.**
   The current classifier raises the notice when the reconciler has a known
   runtime row and a socket row with no live PID. Stronger production proof
   should incorporate the bounded session-level history from the later durable
   history slice.

## Slice 5 addendum — diagnostic history purge

Date: 2026-07-03

The stacked slice-5 branch adds a diagnostic-only purge endpoint over
`_broker/connection_events.jsonl`, an exact confirmation token, client/time
filters, and a broker-session mirror control that clears the live SSE buffer
after a successful purge.

Critical limits:

1. **This is not the full session-level history store.**
   The purge operates on the existing durable API-callback diagnostic log. It
   does not yet persist socket roster snapshots for ghost/orphan rows that have
   no API-event content.

2. **The UI time filter is raw `int64 ms UTC`.**
   That keeps the boundary exact and compliant with timestamp policy, but it is
   a diagnostic control rather than a polished date/time picker. A later UX
   pass can add ET date controls while still submitting epoch milliseconds.

3. **Recovery remains deferred.**
   The robust reconnect state machine is still intentionally outside these
   read-mostly mirror slices because it rewrites live-trading behavior and
   needs its own transition-function and ResumeGuard PR stack.

## Slice 6 addendum — fail-visible degradation

Date: 2026-07-03

The stacked slice-6 branch adds explicit fail-visible degradation behavior:
when the socket referee is unavailable, runtime artifacts project bot rows as
`past_last_known` instead of the table blanking; stale per-client runtime
signals add a `CLIENT_SIGNAL_STALE` attention marker.

Critical limits:

1. **Last-known rows come from runtime artifacts, not a full roster store.**
   This satisfies the honest "PAST, not CURRENT" display for known bot
   runtimes, but it does not reconstruct historical ghost sockets or closed
   socket rows without runtime artifacts. That still belongs to the future
   session-level history store.

2. **Staleness uses the mirror's runtime observation age.**
   A live OS socket remains `current`, but a stale child runtime signal is
   surfaced as attention. This avoids claiming the socket is dead when the
   referee can still see it, while still making stale child evidence visible.

3. **Recovery remains the only major PRD area not implemented.**
   The mirror is now covering roster, events, child client IDs, orphan notice,
   diagnostic purge, and degradation behavior. The robust reconnect /
   ResumeGuard state machine still needs a separate live-trading-path stack.

## Slice 7 addendum — recovery core state machine

Date: 2026-07-04

The stacked slice-7 branch starts the live-trading-path recovery rebuild with a
pure IBKR recovery transition table and monitor integration. The monitor now
treats a soft IBKR link interruption (`connection_lost` while the API socket is
still open) differently from a hard socket close: it waits for IBKR's own
1101/1102 restore signal before tearing down the socket, and it can surface a
terminal `hard_down` state when reconnect attempts are exhausted.

Critical changes from the PRD:

1. **This is the recovery core, not the full ResumeGuard integration.**
   The branch implements the transition vocabulary, bounded 1100 wait, 1101
   subscription recovery path, max-attempt `hard_down`, broker-health overlay,
   and frontend rendering. It does not yet write reconciliation receipts or
   wire `ResumeGuardState` from `BLOCKED` to `CLEARABLE`.

2. **Reconnect backoff still has no jitter.**
   The existing monitor already had exponential backoff with a cap. Slice 7
   adds a max-attempt terminal state but does not add jitter because that
   changes retry timing across live child processes and deserves its own small
   review.

3. **The 1101 recovery callback remains market-data focused.**
   ADR 0018 says 1101 should re-request market data plus open orders,
   executions, and positions, then run the owned-orphan/outside-mutation
   ladder. This branch keeps the existing recovery-callback hook and preserves
   the current market-data resubscribe behavior. The order/execution/position
   reconciliation ladder is still the next recovery slice.

4. **ADR-0011 connectivity halt retirement is not complete.**
   This branch makes the monitor a clearer connectivity state authority, but it
   does not yet remove or cross-wire the older `live_engine.py`
   connectivity-count halt path. Running both recovery/halt mechanisms remains
   the critical remaining single-authority problem from ADR 0018.

## Slice 8 addendum — mirror recovery-state visibility

Date: 2026-07-04

The stacked slice-8 branch populates the existing mirror roster
`recovery_state` field and renders it as a dedicated Recovery column. This makes
the recovery lifecycle visible in the session mirror without changing any
trading gate.

Critical limit:

1. **Recovery state is projected from connection state for child rows.**
   Data-plane and child runtime rows do not yet persist the monitor's exact
   ADR-0018 state-machine value. Slice 8 maps the existing connection states
   (`soft_lost`, `subscriptions_stale`, `reconnecting`, `hard_down`, and so on)
   into the ADR-0018 recovery vocabulary for display. A later recovery-authority
   slice should publish the monitor-owned state directly from each child.

## Slice 9 addendum — broker-health recovery-state contract

Date: 2026-07-04

The stacked slice-9 branch publishes ADR-0018 `recovery_state` on
`IbkrConnectionHealth`. When the FastAPI data plane has an
`AutoReconnectMonitor`, broker health now carries the monitor-owned recovery
state and the broker-session mirror uses that value for the data-plane system
row instead of recalculating it from `connection_state`.

Critical limits:

1. **Child rows still project recovery state.**
   Child runtime artifacts do not yet publish the monitor-owned ADR-0018 state,
   so bot rows continue to map their existing `connection_state` into the
   recovery vocabulary. Publishing exact child recovery state should be a later
   slice that updates the live child runtime payload without changing submit
   behavior in the same PR.

2. **Health remains a visibility contract, not ResumeGuard clearing.**
   This slice does not advance `ResumeGuardState` or add reconciliation
   receipts. It only makes the recovery authority visible through the existing
   health and mirror APIs.

## Slice 10 addendum — child runtime recovery-state field

Date: 2026-07-04

The stacked slice-10 branch adds `broker.recovery_state` to
`engine_runtime.json` and carries that field into the broker-session mirror's
runtime index. New child runtime snapshots therefore publish the ADR-0018
recovery vocabulary explicitly instead of requiring the mirror reconciler to
derive it from `connection_state`.

Critical limits:

1. **The child value is still client-health projected, not monitor-owned.**
   Child `cmd_start` processes still do not install an `AutoReconnectMonitor`.
   Their `IbkrClient.health()` snapshot projects the recovery vocabulary from
   the client-observed connection state. Installing monitor-owned recovery in
   children remains a separate live-trading-path slice.

2. **Older runtime snapshots stay compatible.**
   `engine_runtime.json` files written before this slice have no
   `broker.recovery_state`. The mirror keeps the projection fallback for those
   historical artifacts.

## Slice 11 addendum — reconnect backoff jitter

Date: 2026-07-04

The stacked slice-11 branch adds capped positive jitter to
`AutoReconnectMonitor`'s exponential reconnect backoff. This closes the PRD's
`backoff + jitter + max cap` requirement without touching reconnect
classification, reconciliation, or ResumeGuard behavior in the same review.

Critical limits:

1. **Nightly reset severity is still not window-aware.**
   The monitor now avoids synchronized retry storms, but the 1100/1101/1102
   event severity remains driven by the shared event-code table rather than an
   IBKR reset-window classifier.

2. **Jitter does not install child monitors.**
   The jittered retry loop applies wherever `AutoReconnectMonitor` is already
   installed. Child `cmd_start` processes still use their existing recovery
   wiring until the child-monitor slice.

## Slice 12 addendum — scheduled reset event severity

Date: 2026-07-04

The stacked slice-12 branch makes broker-session event classification aware of
IBKR's published North America reset windows: Sunday-Friday 00:15-01:45 ET and
Saturday 00:00-02:00 ET. Warning-class IBKR connectivity/data-farm reset codes
that land inside those windows render as `info` diagnostics in the mirror
instead of operator alarms.

Critical limits:

1. **This is observability-only.**
   The classifier still records and streams the events. It does not suppress
   rows, short-circuit recovery, or change `AutoReconnectMonitor` state
   transitions.

2. **Only the published North America window is encoded.**
   If this deployment uses another IBKR region/window, the helper must be
   extended before those reset events can be demoted.

## Slice 13 addendum — monitor-authored recovery events

Date: 2026-07-04

The stacked slice-13 branch writes `AutoReconnectMonitor` milestones into the
existing broker-session diagnostic JSONL stream: reconnect attempt, failure,
success, hard-down, probe-forced reconnect, and link-wait expiry. The mirror
can now show recovery/reconnect activity sourced from the monitor itself rather
than only client callback and recovery-completion events.

Critical limits:

1. **Events are diagnostic, not recovery receipts.**
   These rows explain monitor behavior in the mirror. They do not clear
   `ResumeGuardState`, prove broker reconciliation, or authorize trading.

2. **Child coverage still depends on child monitor installation.**
   The event writer is available on `IbkrClient`, but only processes that
   install `AutoReconnectMonitor` emit these monitor-authored events.

## Slice 14 addendum — bounded roster snapshot history

Date: 2026-07-04

The stacked slice-14 branch adds the PRD's missing session-level history
substrate for roster facts. Each composed broker-session mirror snapshot is
written to `_broker/session_roster_history.jsonl` with bounded retention, and
`GET /api/broker/session-mirror/history` returns recent snapshots newest-first
for later timeline/drill-down work. This complements, rather than replaces,
the classified API callback stream in `_broker/connection_events.jsonl`.

Critical limits:

1. **History is stored and readable, but not yet rendered as a timeline.**
   Angular has the typed service method for the endpoint, but the mirror page
   still renders the live SSE roster and event drill-down only. A future UI
   slice should add a dedicated recent-history view without client-side broker
   inference.

2. **The new roster store is bounded, not an audit ledger.**
   It is diagnostic evidence for recent investigation. It does not alter
   `intent_events.jsonl`, reconciliation receipts, fills, executions, or any
   trading verdict.

3. **Per-client roster-history purge remains separate.**
   Existing purge still applies to classified event diagnostics. Purging rows
   inside multi-client roster snapshots needs exact row-level semantics, so it
   remains a follow-up rather than being hidden in the event purge endpoint.

4. **The live roster does not yet replay `past_closed` rows from history.**
   This slice makes the evidence durable and readable. Reconstructing closed
   sockets back into the current roster as historical rows should be a smaller
   follow-up once row identity and purge semantics are pinned.

## Slice 15 addendum — roster-history diagnostic purge

Date: 2026-07-04

The stacked slice-15 branch adds diagnostic purge semantics for the roster
snapshot history introduced in slice 14. `POST
/api/broker/session-mirror/history/purge` supports the same explicit
confirmation token and client/time filters as the existing event purge. A
time-only purge removes matching snapshots; a `client_id` purge removes only
that client's rows from matching snapshots so unrelated clients' history
survives.

Critical limits:

1. **There is still no Angular purge control for roster history.**
   The frontend service and types exist, but the mirror page still exposes the
   prior event-log purge control only. A UI control should be added with clear
   copy that roster-history purge never disconnects clients or alters live
   rows.

2. **Rows without a `client_id` cannot be removed by client filter.**
   Ghost rows, some system rows, and older child snapshots with unknown
   `client_id` remain purgeable by time range only. That is deliberate: the
   purge does not infer identity from PID/run-dir or stale names.

3. **Purge does not rebuild a compacted history index.**
   The JSONL is rewritten after purge, but no secondary index exists yet. That
   remains acceptable while retention is bounded and the endpoint is
   diagnostic-only.

## Slice 16 addendum — history-backed `past_closed` rows

Date: 2026-07-04

The stacked slice-16 branch uses the bounded roster history to replay recently
closed rows into the live mirror as `recency="past_closed"` and
`socket_present=false` when the socket observer is online. This closes the
largest remaining recency-display gap: a socket that was present in recent
history but is absent from the current host-daemon socket snapshot is now shown
as PAST rather than disappearing.

Critical limits:

1. **Replay is composed around the reconciler, not inside it.**
   The PRD describes a pure reconciler that receives event/history evidence.
   This slice keeps the existing pure socket/registry/runtime reconciler stable
   and adds bounded history replay in `BrokerSessionMirrorService`. Moving the
   history input into the pure reconciler can be done later if the current
   composition becomes hard to test.

2. **`past_closed` replay only runs when the host socket observer is online.**
   When the observer is degraded, the mirror still uses `past_last_known`
   runtime rows instead of claiming sockets closed. That preserves the invariant
   that observer loss must never look like a healthy clean shutdown.

3. **Closed-row identity is best-effort and conservative.**
   Known bot rows key by `strategy_instance_id` + `run_id`; known client rows
   key by identity type + `client_id`; rows with no durable identity fall back
   to their historical row id. The mirror does not infer identity from stale
   PID/run-dir names.
