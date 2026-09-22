/**
 * Client-authored fallback copy for the fleet control plane's closed
 * refusal vocabulary (#2067).
 *
 * AUTHORITY: `FleetControlError.detail()` always ships an operator-facing
 * `message`, and usually a `next_step`, authored per raise site
 * (`PythonDataService/app/broker/fleet/errors.py`) — that backend prose is
 * the sole semantic-copy authority and always wins over this map. This
 * fallback exists for the one family the backend ships with no prose at all
 * (`qualification_market_status_unavailable`, a typed Pydantic literal with
 * no `message` field) and for any raise site that omits `next_step`; see
 * `panel-action-outcome.ts`'s `deriveActionRejection`, which consults this
 * map only when the refusal body itself carries none.
 *
 * `outcome` is hand-set from each family's pinned wire status, per the retry
 * semantics `refusal_vocabulary.py`'s module docstring pins: a `409` is a
 * state conflict the caller must re-read and re-prepare before retrying —
 * `'conflict'`. Every other status (terminal `404`s, retry-safe `503`s, the
 * `4xx` auth/validation refusals) is a `'failure'` the panel cannot resolve
 * by re-preparing the identical command — `ActionRejection.outcome` has no
 * finer-grained bucket than `conflict` / `failure` / `unknown`.
 *
 * Locked against `fleet-refusal-vocabulary.snapshot.json` by
 * `fleet-refusal-copy.spec.ts`: a reason code the backend mints without a
 * matching entry here fails that test instead of silently rendering blank.
 */

export type FleetRefusalOutcome = 'conflict' | 'failure';

export interface FleetRefusalCopy {
  readonly outcome: FleetRefusalOutcome;
  readonly message: string;
  readonly nextStep: string;
}

export const FLEET_REFUSAL_COPY: Readonly<Record<string, FleetRefusalCopy>> = {
  broker_and_clerk_required: {
    outcome: 'failure',
    message: 'This command needs both a broker and a clerk identity, and one is missing.',
    nextStep: 'Resend the command with both a broker and a clerk id in the path.',
  },
  broker_clerk_capability_unavailable: {
    outcome: 'conflict',
    message: "This lane's provider does not support the requested action.",
    nextStep: "Confirm the lane's capabilities before retrying; this action is not available here.",
  },
  broker_not_supported: {
    outcome: 'failure',
    message: 'No adapter is registered for this broker in this deployment.',
    nextStep: 'Use a broker this deployment supports.',
  },
  clerk_account_mismatch: {
    outcome: 'conflict',
    message: "The account named by this command is not this clerk's effective account.",
    nextStep: "Refresh the clerk's confirmed account, then retry with the current identity.",
  },
  clerk_assignment_conflict: {
    outcome: 'conflict',
    message: 'Another clerk already owns this broker-qualified account assignment.',
    nextStep: 'Refresh the fleet directory to see which clerk holds the assignment before retrying.',
  },
  clerk_binding_generation_conflict: {
    outcome: 'conflict',
    message: "This command's expected binding generation is not the clerk's current one.",
    nextStep: "Refresh the lane's current binding generation, then re-prepare the command.",
  },
  clerk_broker_mismatch: {
    outcome: 'conflict',
    message: "The broker in the request path differs from this clerk's immutable broker.",
    nextStep: "Route the command through this clerk's own broker path.",
  },
  clerk_command_quiet_required: {
    outcome: 'conflict',
    message: 'This lane still holds a dispatched command whose outcome the coordinator lost.',
    nextStep: 'Reconcile or settle each unsettled attempt before retrying the ceremony.',
  },
  clerk_drain_deadline_pending: {
    outcome: 'conflict',
    message: "This lane's drain deadline has not elapsed yet.",
    nextStep: 'Wait for the deadline recorded on the lane, then re-run the ceremony.',
  },
  clerk_drain_required: {
    outcome: 'conflict',
    message: 'This ceremony requires a lane whose door is closed.',
    nextStep: 'Run the drain ceremony first, then retry.',
  },
  clerk_endpoint_not_approved: {
    outcome: 'conflict',
    message: 'This registration cites an endpoint the deployment has not approved.',
    nextStep: "Use one of the deployment's approved endpoints.",
  },
  clerk_identity_mismatch: {
    outcome: 'conflict',
    message: "This session's identity facts contradict the clerk's registry record.",
    nextStep: "Refresh the clerk's registry identity before retrying.",
  },
  clerk_lane_draining: {
    outcome: 'conflict',
    message: 'This lane is draining; registration and confirmation refuse.',
    nextStep: 'Finish the drain ceremony; this lane marks its own evidence and stays down.',
  },
  clerk_lane_quiet_unproven: {
    outcome: 'conflict',
    message: "No lane-quiet confirmation answers this lane's retirement gate.",
    nextStep: 'Run force-retire, the named exit, or wait for the lane-quiet provider.',
  },
  clerk_not_found: {
    outcome: 'failure',
    message: 'No clerk carries this identity.',
    nextStep: 'Confirm the clerk id; retrying the same identity will not succeed.',
  },
  clerk_reassignment_blocked: {
    outcome: 'conflict',
    message: "Lane-to-lane reassignment is blocked until the drain ceremony proves the drained lane's quiet.",
    nextStep: 'Use whole-machine migration, which moves the volume with the lane.',
  },
  clerk_routing_attempt_conflict: {
    outcome: 'conflict',
    message: 'This routing attempt hit an illegal transition or a reused idempotency key.',
    nextStep: "Refresh the routing attempt's state, then re-prepare the command with a fresh key if needed.",
  },
  clerk_routing_outcome_unknown: {
    outcome: 'failure',
    message: "The last routing attempt's outcome could not be confirmed.",
    nextStep: 'Retry with the same broker, clerk, and idempotency identity.',
  },
  clerk_unreachable: {
    outcome: 'failure',
    message: "This clerk's agent could not be reached.",
    nextStep: "Retry once the clerk's agent reconnects.",
  },
  clerk_volume_already_registered: {
    outcome: 'conflict',
    message: 'A different active clerk already owns this volume identity.',
    nextStep: 'Confirm which clerk owns this volume before retrying.',
  },
  clerk_volume_clone_detected: {
    outcome: 'conflict',
    message: "This volume carries another clerk's identity marker, as if it were copied.",
    nextStep: "Investigate the volume's provenance before retrying; do not reuse a cloned volume.",
  },
  clerk_volume_identity_mismatch: {
    outcome: 'conflict',
    message: "This volume's identity marker contradicts the registry's expectation for this clerk.",
    nextStep: "Reconcile the volume's marker with the registry before retrying.",
  },
  clerk_volume_identity_missing: {
    outcome: 'conflict',
    message: 'This volume root carries no identity marker at all.',
    nextStep: 'Enrol the volume through the host ceremony before retrying.',
  },
  clerk_volume_mount_unproven: {
    outcome: 'conflict',
    message: 'This root is not proven to be the canonical mounted volume.',
    nextStep: 'Mount the canonical volume directly; a symlink or alternate path will not qualify.',
  },
  command_envelope_invalid: {
    outcome: 'failure',
    message: "This command's envelope failed validation before any routing decision was made.",
    nextStep: "Fix the command envelope's shape and resend it.",
  },
  compatibility_read_retired: {
    outcome: 'failure',
    message: 'This compatibility read has retired.',
    nextStep: "Use this lane's canonical broker-and-clerk route instead.",
  },
  compatibility_retirement_state_invalid: {
    outcome: 'failure',
    message: 'The compatibility retirement evidence on this process is unreadable.',
    nextStep: 'This needs host operator action; it will not clear by retrying alone.',
  },
  confirmation_evidence_invalid: {
    outcome: 'conflict',
    message: "This clerk's durable confirmation evidence is unreadable or contradicts its volume.",
    nextStep: "Investigate the clerk's confirmation evidence before retrying.",
  },
  data_plane_control_secret_refused: {
    outcome: 'failure',
    message: "The presented data-plane control secret does not match this deployment's configured one.",
    nextStep: "Confirm the deployment's control secret is configured correctly.",
  },
  fleet_agent_token_refused: {
    outcome: 'failure',
    message: 'The presented agent token does not match the token mapped to this clerk.',
    nextStep: "Confirm this clerk's mapped agent token before retrying.",
  },
  fleet_boot_refused: {
    outcome: 'conflict',
    message: "This lane may not open authority under the fleet's admission rules.",
    nextStep: "Refresh the fleet directory to see the lane's current admission state before retrying.",
  },
  fleet_control_error: {
    outcome: 'conflict',
    message: 'The fleet refused this command for an unclassified reason.',
    nextStep: 'Refresh the fleet directory and re-prepare the command before retrying.',
  },
  fleet_control_plane_not_installed: {
    outcome: 'failure',
    message: "A required fleet control-plane component is not configured on this process.",
    nextStep: 'This needs host operator action; it will not clear by retrying alone.',
  },
  fleet_lane_draining: {
    outcome: 'conflict',
    message: 'The coordinator refused this presence call because the lane itself is drained.',
    nextStep: 'Finish the drain ceremony; this lane marks its own evidence and stays down.',
  },
  fleet_lane_capacity_exhausted: {
    outcome: 'failure',
    message: "This lane's request or stream budget is exhausted.",
    nextStep: 'Retry once the lane has available capacity.',
  },
  fleet_presence_unavailable: {
    outcome: 'failure',
    message: "The coordinator could not be reached, or refused this agent's presence call.",
    nextStep: 'Retry once the coordinator is reachable again.',
  },
  fleet_protocol_incompatible: {
    outcome: 'conflict',
    message: 'This agent and the coordinator speak different fleet protocol versions.',
    nextStep: 'Match the agent and coordinator to compatible protocol versions before retrying.',
  },
  fleet_registry_recovery_pending: {
    outcome: 'conflict',
    message: 'A restored registry has not yet reconciled its original lanes.',
    nextStep: 'Wait for the host recovery ceremony to finish before retrying.',
  },
  fleet_registry_unavailable: {
    outcome: 'failure',
    message: 'The fleet registry cannot currently be opened or read.',
    nextStep: 'Retry once the registry is reachable again.',
  },
  qualification_market_status_unavailable: {
    outcome: 'failure',
    message: 'The market-liveness dependency this qualification probe needs is not available.',
    nextStep: 'Retry once the market-status dependency is reachable again.',
  },
};

/** The fallback copy for a reason code, or `null` when the code is not in
 * the closed fleet refusal vocabulary (or `reasonCode` is `null`). */
export function fleetRefusalCopyFor(reasonCode: string | null): FleetRefusalCopy | null {
  if (reasonCode === null) return null;
  return FLEET_REFUSAL_COPY[reasonCode] ?? null;
}
