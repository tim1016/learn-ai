// PRD #607 / Slice 1 (#608) — reason-code -> operator-language lookup.
//
// The server authors action capabilities with a stable
// ALL_CAPS_SNAKE_CASE token in ``disabled_reason_code``; the cockpit
// uses this typed map as the SINGLE place that translates a code into
// operator-facing copy (banner-keycap tooltip, Diagnostics POISON RUN
// tooltip, etc.).
//
// Unknown codes (server emits a new token before the Frontend ships its
// mapping) fall back to rendering the raw code — that is deliberate:
// the gap is *visible* in the UI rather than silently disappearing.
//
// Authority layers:
//   1. Server   — owns the code vocabulary and emits the token.
//   2. Frontend — owns this map (and tests it for closure).
//   3. UI       — renders the looked-up copy verbatim.

export type ActionReasonCode =
  | 'NO_LIVE_BINDING'
  | 'SAFETY_BLOCK_HALT'
  | 'RECONCILE_NOT_WIRED'
  | 'NO_OWNED_POSITIONS'
  | 'ALREADY_POISONED';

export const ACTION_REASON_COPY: Record<ActionReasonCode, string> = {
  NO_LIVE_BINDING:
    'No live run is bound to this instance — start the host runner first.',
  SAFETY_BLOCK_HALT:
    'A safety block is active — clear the halt before acting.',
  RECONCILE_NOT_WIRED:
    'Reconciliation is not wired for this account — read the runbook before acting.',
  NO_OWNED_POSITIONS:
    'This instance owns no positions — flatten-and-pause would be a no-op.',
  ALREADY_POISONED:
    'This run is already marked poisoned — deploy a fresh run to recover.',
};

/**
 * Look up the operator-language copy for a reason code.  Unknown codes
 * (a new server token before the Frontend ships its mapping) fall back
 * to rendering the raw code so the gap is visible, not silenced.
 */
export function getActionReasonCopy(code: string | null | undefined): string {
  if (code == null || code === '') {
    return '';
  }
  return (
    (ACTION_REASON_COPY as Record<string, string>)[code] ?? code
  );
}
