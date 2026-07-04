# Broker session mirror - critical deferred items

Date: 2026-07-04

This is the end-of-stack note requested for the PRD introduced at
`c3a52791`. The implementation was split into stacked, review-sized PRs. The
main divergence log remains
`docs/architecture/broker-session-mirror-slice-1-divergences.md`; this file is
the shorter list of critical items I did not proceed with because the current
codebase does not provide enough authority, or because the change needs a
separate product/safety decision.

## Implemented by the stack

- Read-only broker session mirror with host-daemon socket facts, runtime facts,
  data-plane health, SSE roster updates, and Angular roster rendering.
- Categorized broker-session diagnostic event stream with per-row drill-down,
  raw technical details, unclassified fail-visible events, reset-window severity
  demotion, all-code capture, and a broader shared IBKR code table.
- Child `client_id` and recovery-state publication through runtime artifacts.
- Orphaned-socket projection notices and guided remediation runbook.
- Diagnostic-only purge for event logs and roster history, with typed confirm.
- Bounded roster snapshot history, `past_closed` replay, recent-history UI,
  roster-history purge UI, and per-snapshot expansion.
- Monitor-owned reconnect/recovery path with bounded link-interruption wait,
  jittered reconnect backoff, terminal `HARD_DOWN`, recovery diagnostic events,
  child monitor installation, broker recovery reconciliation, and legacy
  reconnect revalidation standing down when a monitor is wired.
- Bounded broker event diagnostic retention with stable `broker_session_seq`
  cursors.

## Critical deferred items

### 1. Exact 1:1 data-plane socket de-duplication

The mirror still cannot prove the FastAPI data-plane client's exact OS socket.
`/api/broker/health` publishes the data-plane `client_id`, account, host, port,
and recovery state, but it does not publish the local source port or host PID
that would let the reconciler join that health row to a specific `lsof` row.

Current behavior: the mirror adds a system row from broker health when the
data-plane client is connected. If the host socket probe also reports the same
connection as an unattributed socket, the reconciler cannot safely de-duplicate
it without guessing.

Required follow-up: publish a data-plane socket identity contract, ideally
`local_port` plus a host-observable PID/process identity, or extend the host
daemon with a trusted container/process mapping. I did not infer this from
command names or remote port because that would undermine the PRD's 1:1
fidelity requirement.

### 2. Durable orphaned-socket incident lifecycle

The stack projects ADR-0015-style orphaned-socket notices directly on mirror
rows, but it does not persist them as durable incidents with acknowledgement,
resolution, or repeat-suppression lifecycle.

Current behavior: an orphaned row gets a critical operator notice and guided
remediation action while the row is present. This is enough for the mirror
surface, but it is not a durable incident ledger.

Required follow-up: decide whether broker-session orphan notices should enter
the existing incident/notice store, what resolves them, and how to avoid
duplicate notices across retained history replay. I did not add persistence in
the mirror PRs because the PRD positions the mirror as read-only except
diagnostic purge, and incident lifecycle semantics are broader than this page.

### 3. Strong orphan attribution for sockets with no PID or run-dir evidence

The reconciler can classify a socket as `orphaned_bot_socket` when the socket
row carries enough historical/run-dir evidence to join it to a known bot. It
cannot prove ownership for a raw Gateway socket that has neither live PID nor
run-dir attribution.

Current behavior: unknown sockets remain `ghost` unless runtime/history facts
give the reconciler a durable bot identity. This is conservative and
fail-visible, but it may under-classify some real orphaned bot sockets.

Required follow-up: store and join a stronger session-level socket identity
history: client id, local source port, run id, strategy instance id, and close
events. I did not infer orphan ownership from stale PID/name guesses because
that could falsely label a foreign/manual session as one of our bots.

### 4. Automatic ResumeGuard or incident clearing after clean recovery

Clean monitor recovery now reconciles broker truth, writes the normal
reconciliation receipt, releases the submit barrier, bumps the connection
epoch, and keeps the engine `PAUSED` with
`broker_recovery:operator_resume_required`. The older bar-loop reconnect gate
stands down when a monitor is wired.

Current behavior: the operator still resumes through the existing guarded
resume path. Safety-verdict, submission-capability, reconciliation, and
uncertain-intent guards remain independent.

Required follow-up: decide exactly which incidents or guard states should be
auto-cleared by a clean broker-recovery receipt, and which must remain manually
acknowledged. I did not auto-clear incidents or bypass resume gates because the
PRD also requires operator-only resume after ambiguity, and the current guard
contract is intentionally fail-closed.

## Non-critical deferred UX

- The recent-history panel is not a full timeline workspace. It supports
  retained snapshots, purge, and per-snapshot expansion, but not side-by-side
  snapshot comparison, saved filters, or historical per-row event streams.
- The purge time fields intentionally accept raw `int64 ms UTC`. A polished ET
  date/time picker can be added later while preserving epoch-ms boundaries.
- The shared IBKR code table is broader but not exhaustive. Unknown official
  codes now fail visible as `unclassified` until each code receives a reviewed
  category, severity, and operator label.

