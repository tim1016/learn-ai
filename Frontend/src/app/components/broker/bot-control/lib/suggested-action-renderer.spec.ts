import { describe, expect, it } from 'vitest';

import {
  presentSuggestedAction,
  presentTraderRemediation,
} from './suggested-action-renderer';

describe('presentSuggestedAction', () => {
  it('presents every closed remediation shape without carrying dispatch', () => {
    expect(presentSuggestedAction({ kind: 'invoke_capability', capability: 'resume' }))
      .toEqual({ label: 'Resume', variant: 'primary' });
    expect(presentSuggestedAction({
      kind: 'focus_action',
      tab: 'audit',
      action: 'mark_poisoned',
    })).toEqual({ label: 'Mark poisoned →', variant: 'link' });
    expect(presentSuggestedAction({ kind: 'redeploy' })).toEqual({
      label: 'Redeploy →',
      variant: 'link',
    });
    expect(presentSuggestedAction({ kind: 'open_runbook', slug: 'broker-reconnect' }))
      .toEqual({ label: 'Open runbook →', variant: 'link' });
  });

  it('is the shared presenter used for trader endpoint remediation', () => {
    expect(presentTraderRemediation({
      kind: 'invoke_endpoint',
      endpoint: 'reconcile_instance',
      method: 'POST',
      path_template: '/api/live-instances/{strategy_instance_id}/reconcile',
    })).toEqual({ label: 'Reconcile now', variant: 'primary' });
  });

  it('fails closed for absent, none, and unknown actions', () => {
    expect(presentSuggestedAction(null)).toBeNull();
    expect(presentTraderRemediation({ kind: 'none', reason: 'READY' })).toBeNull();
    expect(presentSuggestedAction(
      { kind: 'future_action' } as never,
    )).toBeNull();
  });
});
