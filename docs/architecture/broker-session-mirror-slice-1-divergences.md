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
