import type { CohortActionResult, CohortLegResult } from '../lib/broker-v2-panel.types';

/**
 * A leg refused because what the operator confirmed no longer matches.
 *
 * The backend raises `StaleRevisionError` / `ActionNotAvailableError` without
 * a `reason_code` (`action_execution_service.py`, `sqlite_panel_source.py`),
 * and `cohort_execution.py` maps both to `refused`. Re-sending the same wave
 * re-sends the same stale token, so a same-key retry can only be refused
 * again — the cure is a fresh read and a new wave.
 */
export function isStalePresentationRefusal(leg: CohortLegResult): boolean {
  return leg.outcome === 'refused' && (leg.error?.reason_code ?? null) === null;
}

/**
 * Whether re-sending the same wave under the same key could change anything:
 * an unanswered leg (early exit), or a refused/failed/unknown leg that is not
 * a stale-presentation refusal. Applied legs replay as no-ops either way.
 */
export function sameKeyRetryCanChange(result: CohortActionResult, sentLegCount: number): boolean {
  if (result.legs.length < sentLegCount) return true;
  return result.legs.some((leg) => leg.error !== null && !isStalePresentationRefusal(leg));
}
