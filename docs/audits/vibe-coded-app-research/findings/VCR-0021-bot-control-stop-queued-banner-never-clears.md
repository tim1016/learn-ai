---
id: VCR-0021
severity: P2
status: open
area: ui-runtime-claims
canonical_file: Frontend/src/app/components/broker/broker-instances/**
reference: PRD §12.10 + ADR 0007 (cockpit ↔ daemon command channel)
first_seen: 2026-06-16
last_seen: 2026-06-16
lens: ui-vs-runtime-claims
dedupe_with_F: VCR-0010 (paper-mode hero) + VCR-0018 (UI vs runtime claims rollup)
confidence: high
---

## What

The cockpit's "STOP queued on `<run_id>`; awaiting ack" banner never
clears, even after the engine cleanly consumed the STOP via the command
channel, wrote `desired_state.json::desired_state=STOPPED`, and exited
with code 0. The daemon's `GET /api/live-instances/<sid>/commands`
endpoint returns `{"entries": []}` (empty list — no queued commands),
but the cockpit keeps showing the stale "queued ... awaiting ack" detail
string from the original `setInstanceDesiredState` response indefinitely.

Observed live during the 2026-06-16 HITL VCR-0002 receipt run: after
operator clicked Stop, the engine ack'd via command channel, set
`desired_state=STOPPED` (`reason: command_channel:STOP`), and exited
cleanly. Hours later the cockpit still rendered the queued banner. Hard
page refresh cleared it.

## Where

- The optimistic banner is rendered against the `detail` string returned
  by `setInstanceDesiredState({state: 'STOPPED'})` — see the spec fixture
  at `Frontend/src/app/components/broker/broker-instances/broker-instances.component.spec.ts:95`
  for the shape (`'PAUSE queued on run-live; awaiting ack'`).
- The acknowledgement reconciliation should consult
  `getInstanceCommands` poll output — `Frontend/src/app/api/live-runs.types.ts:129`
  declares `CommandStatus = 'queued' | 'acknowledged' | 'failed'`.
- The gap: when the polled commands list goes EMPTY, the optimistic
  banner state has no signal to clear itself against. The
  command-acknowledged transition is only modelled when the SAME
  `command_seq` reappears with `status='acknowledged'`. A command that
  was consumed by an engine-shutting-down path never emits its ack
  entry, so the banner ages forever.

## Why this severity

P2. The runtime state is correct: `desired_state.json` says STOPPED,
the daemon process is exited, the engine flushed its WAL and shut down
cleanly. The bug is purely UI display drift — the operator sees a
banner that no longer reflects reality.

P2, not P3, because the symptom is **specifically a Stop banner that
won't clear**, which is exactly the moment an operator wants to trust
that the bot is stopped. False uncertainty during a stress moment
(Stop, then look at the cockpit, see "awaiting ack ... awaiting ack ..."
without resolution) erodes the operator's mental model and can lead to
double-stops, page reloads at the worst time, or unnecessary
investigation when nothing is wrong.

Not P1 because it doesn't enable order submission against operator
intent; the actual durable state is fine.

## Trading impact

None directly. The bot IS stopped. The banner just doesn't say so.
Indirect impact:

- **Mistrust at stress moments.** The Stop button is the operator's
  most-used safety control; if it visually appears to never confirm,
  operators may click it repeatedly or take other (potentially worse)
  actions to "make sure".
- **Masks future genuine "lost command" cases.** If the cockpit ever
  starts losing queued commands for real (network drop between
  cockpit and daemon, daemon crash mid-queue), the banner will look
  identical to today's false positive and the operator won't
  distinguish.

## Reproduction

```bash
# 1. Deploy + start any instance (e.g., dep_val_smoke_002).
# 2. After at least one full bar processes, click Stop in the cockpit.
# 3. Engine consumes STOP via command channel and exits cleanly.
# 4. Cockpit displays "STOP queued on <run_id>; awaiting ack".
# 5. Wait 30+ seconds (longer than the commands poll interval).
# 6. Observe:
curl -s 'http://localhost:8000/api/live-instances/<sid>/commands' | jq .
# returns {"entries": [], "poll_interval_ms": 1000}
# 7. The banner is still showing "queued ... awaiting ack" despite
#    the empty commands list.
# 8. Hard refresh the cockpit page (Cmd+R / Ctrl+R) — banner clears.
```

## Suggested resolution (NOT auto-applied)

1. **In the cockpit's command-acknowledgement reconciliation**, when the
   polled commands list is empty AND the optimistic local "queued"
   command's age exceeds some threshold (e.g., 30s), transition the
   local state to "lost" or "acknowledged-implicit". The threshold
   should be conservative (most ACKs land in 1-2 polls), but a missing
   ACK that lasts longer than that is either acknowledged-implicitly-on-
   shutdown OR genuinely lost — both demand the banner clear or
   escalate.
2. **Alternatively**, have the engine subprocess explicitly write the
   ACK back to the daemon's commands log BEFORE exiting in the shutdown
   path. This is the cleaner fix: instead of inferring the ACK from
   absence, the engine reliably emits it. The current "consume STOP via
   command channel → exit" path skips this final write.
3. **Test**: cockpit component spec that mocks an empty commands list
   while the local state has an aged "queued" entry — asserts the
   banner clears within the threshold window.

## Provenance of the finding

Live observation during the 2026-06-16 HITL VCR-0002 Acceptance Gate #2
receipt run wind-down phase. After the cockpit Stop succeeded
(`desired_state.json` updated, engine exited cleanly), the banner
persisted; backend `GET /commands` returned empty; only a page reload
cleared it.
