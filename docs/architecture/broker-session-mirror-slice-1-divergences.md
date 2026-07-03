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
