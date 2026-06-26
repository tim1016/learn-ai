// PRD #617 — account-row attention projection.  Pure function over
// `FleetAccountSummary` returning `{ isAttention, isCollapsible }`.

import type { FleetAccountSummary } from '../../../../api/live-instances.types';

export interface AccountAttentionState {
  isAttention: boolean;
  /** When attention is active, the row is expanded AND non-collapsible
   *  so the operator cannot dismiss the warning (PRD #617 §"User
   *  Stories" 17). */
  isCollapsible: boolean;
}

/**
 * Stable attention formula (PRD #616 §"User Stories" 14):
 *
 *   account_identity !== 'CONSISTENT' || contamination.verdict !== 'clean' ||
 *   contamination.policy_blocks_starts || notice != null
 *
 * `policy_blocks_starts` stays in the formula even when currently
 * impossible-with-clean so future policy semantics do not require an
 * Angular change.
 */
export function projectAccountAttention(
  summary: FleetAccountSummary,
): AccountAttentionState {
  const isAttention =
    summary.account_identity !== 'CONSISTENT' ||
    summary.contamination.verdict !== 'clean' ||
    summary.contamination.policy_blocks_starts ||
    summary.notice != null;
  return {
    isAttention,
    isCollapsible: !isAttention,
  };
}
