// PRD #617 — per-instance tab state reducer.
//
// Tracks the operator's manual inner-tab selection per
// `strategy_instance_id`, the previous readiness verdict (so the
// transition classifier has the polling-delta input it needs), and a
// one-shot "attention unseen" flag so a background instance that
// enters attention does not yank the operator off their deliberate
// investigation.

import {
  classifyReadinessTransition,
  type ReadinessTransition,
} from './classify-readiness-transition';
import type { ReadinessVerdictEnum } from '../../../../api/live-instances.types';

export type InnerTab = 'status' | 'activity' | 'audit' | 'configuration';

export interface InstanceTabState {
  selectedTab: InnerTab;
  previousVerdict: ReadinessVerdictEnum | null;
  attentionUnseen: boolean;
}

export const DEFAULT_INSTANCE_TAB_STATE: InstanceTabState = {
  selectedTab: 'status',
  previousVerdict: null,
  attentionUnseen: false,
};

/**
 * Apply a readiness-verdict observation to one instance's tab state.
 *
 * - `transition === 'entered-attention'` AND the instance IS the
 *   foreground tab → force `selectedTab='status'` (auto-route exactly
 *   once).
 * - `transition === 'entered-attention'` AND the instance is in the
 *   background → mark `attentionUnseen=true`; do NOT change
 *   `selectedTab`.
 * - any other transition leaves `selectedTab` alone (manual
 *   selection is sticky across polls).
 */
export function reduceOnVerdictObserved(
  state: InstanceTabState,
  currentVerdict: ReadinessVerdictEnum,
  isForeground: boolean,
): { state: InstanceTabState; transition: ReadinessTransition } {
  const transition = classifyReadinessTransition(state.previousVerdict, currentVerdict);
  let next: InstanceTabState = { ...state, previousVerdict: currentVerdict };

  if (transition === 'entered-attention') {
    if (isForeground) {
      next = { ...next, selectedTab: 'status', attentionUnseen: false };
    } else {
      next = { ...next, attentionUnseen: true };
    }
  }
  return { state: next, transition };
}

/**
 * Apply a manual tab selection.  Always sticky; clears attentionUnseen
 * because the operator has acknowledged the instance.
 */
export function reduceOnTabSelected(
  state: InstanceTabState,
  tab: InnerTab,
): InstanceTabState {
  return { ...state, selectedTab: tab, attentionUnseen: false };
}

/**
 * Apply the operator switching to this instance's outer tab.
 *
 * - If the instance has `attentionUnseen` AND is non-READY, force
 *   Status & Risk once and clear the unseen flag.
 * - Otherwise leave `selectedTab` alone (manual choice survives the
 *   round trip).
 */
export function reduceOnInstanceFocused(
  state: InstanceTabState,
  currentVerdict: ReadinessVerdictEnum,
): InstanceTabState {
  if (state.attentionUnseen && currentVerdict !== 'READY') {
    return { ...state, selectedTab: 'status', attentionUnseen: false };
  }
  return { ...state, attentionUnseen: false };
}
