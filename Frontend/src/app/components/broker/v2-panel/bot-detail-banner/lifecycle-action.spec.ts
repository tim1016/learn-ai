import { describe, expect, it } from 'vitest';

import {
  BOT_COCKPIT_RECONCILE_ANCHOR,
  BOT_COCKPIT_SAFE_FLATTEN_PREPARE_ANCHOR,
  type OperatorMove,
} from '../../../../api/operator-blocker.types';
import type { PanelAction } from '../lib/broker-v2-panel.types';
import { panelActionForMove } from './lifecycle-action';

function action(actionId: PanelAction['action_id'], enabled = true): PanelAction {
  return {
    action_id: actionId,
    revision: 1,
    concurrency_token: `${actionId}-token`,
    enabled,
    label: actionId,
    explanation: '',
    blockers: [],
    confirmation: null,
  };
}

function confirmInForm(anchor: string): OperatorMove {
  return { label: 'Move', action: { kind: 'confirm_in_form', anchor } };
}

describe('panelActionForMove', () => {
  it('runs Prepare safe flatten for the extended-hours flatten move (#2007)', () => {
    const prepare = action('prepare_safe_flatten');

    expect(
      panelActionForMove(
        { actions: [action('execute_safe_flatten', false), prepare] },
        confirmInForm(BOT_COCKPIT_SAFE_FLATTEN_PREPARE_ANCHOR),
      ),
    ).toBe(prepare);
  });

  it('offers no move when the backend presented Prepare disabled', () => {
    expect(
      panelActionForMove(
        { actions: [action('prepare_safe_flatten', false)] },
        confirmInForm(BOT_COCKPIT_SAFE_FLATTEN_PREPARE_ANCHOR),
      ),
    ).toBeNull();
  });

  it('keeps the reconcile anchor and refuses an unknown one', () => {
    const reconcile = action('reconcile_now');
    const panel = { actions: [reconcile] };

    expect(panelActionForMove(panel, confirmInForm(BOT_COCKPIT_RECONCILE_ANCHOR))).toBe(reconcile);
    expect(panelActionForMove(panel, confirmInForm('not-an-anchor'))).toBeNull();
  });
});
