import type { LiveInstanceStatus } from '../../../../api/live-instances.types';

export type FleetState = 'STEADY' | 'CONFIGURE' | 'BLOCKED';

/**
 * Derives the cockpit banner's fleet state from server-authored signals.
 *
 * Per the page-wide collapse rule (CONTEXT.md), every visible cockpit
 * state must be bound to a server-authored verdict — never a frontend
 * heuristic. Today's mapping uses readiness.verdict directly. TRIAGE is
 * NOT returned here; it is operator-driven via the Detective tab strip.
 *
 * TODO(#584): once the engine emits ReadinessGate.shape, sharpen CONFIGURE
 * to `readiness.gates.some(g => g.shape === 'config' && g.status === 'fail')`
 * per the design doc. Until then, DEGRADED is the closest proxy: an
 * attention-needed verdict that is not a hard block. The mapping is
 * lossy but safe (DEGRADED already surfaces as a non-steady state).
 */
export function deriveFleetState(status: LiveInstanceStatus): FleetState {
  const verdict = status.readiness?.verdict;

  if (verdict === 'READY') return 'STEADY';
  if (verdict === 'DEGRADED') return 'CONFIGURE';
  return 'BLOCKED';
}
