import { describe, expect, it } from 'vitest';
import type { OperatorNotice } from '../../../../models/operator-notice';
import { presentOperatorNoticeAction } from './operator-notice-action-renderer';

function notice(overrides: Partial<OperatorNotice> = {}): OperatorNotice {
  return {
    code: 'runtime.market_data_stale',
    tier: 'warning',
    title: 'Market data is stale',
    message: 'No fresh bar has arrived.',
    source_codes: [],
    forensic_facts: {},
    actionability: 'routed',
    resolution: 'Clears when a fresh IBKR bar arrives.',
    remedy_status: null,
    action: { kind: 'external_manual_check', label: 'Check IBKR market data', target: 'ibkr_connection' },
    runbook_slug: null,
    occurred_at_ms: null,
    ...overrides,
  };
}

describe('presentOperatorNoticeAction', () => {
  it('preserves the typed backend action for container dispatch', () => {
    const presented = presentOperatorNoticeAction(notice({
      action: { kind: 'open_runbook', label: 'Open runbook', target: 'runtime-freshness' },
    }));

    expect(presented).toEqual({
      action: { kind: 'open_runbook', label: 'Open runbook', slug: 'runtime-freshness' },
      label: 'Open runbook',
      variant: 'link',
    });
  });

  it('fails closed when required target evidence is missing', () => {
    expect(presentOperatorNoticeAction(notice({
      action: { kind: 'focus_cockpit_action', label: 'Focus lifecycle action', target: null },
    }))).toBeNull();
  });

  it('marks lease renewal as the primary action', () => {
    expect(presentOperatorNoticeAction(notice({
      action: {
        kind: 'renew_control_plane_lease',
        label: 'Renew control-plane lease',
        target: 'daemon_lease',
      },
    }))?.variant).toBe('primary');
  });
});
