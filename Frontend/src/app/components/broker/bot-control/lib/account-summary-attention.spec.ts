// PRD #617 — account attention projection specs.

import { describe, expect, it } from 'vitest';

import type { FleetAccountSummary } from '../../../../api/live-instances.types';
import {
  FLEET_ACCOUNT_CONFLICTING_CLEAN,
  FLEET_ACCOUNT_CONSISTENT_CLEAN,
  FLEET_ACCOUNT_CONSISTENT_DIRTY,
  FLEET_ACCOUNT_UNKNOWN,
} from '../../../../../testing/operator-surface-fixtures';

import { projectAccountAttention } from './account-summary-attention';

describe('projectAccountAttention', () => {
  it('clean + consistent → not attention, collapsible', () => {
    const r = projectAccountAttention(FLEET_ACCOUNT_CONSISTENT_CLEAN);
    expect(r.isAttention).toBe(false);
    expect(r.isCollapsible).toBe(true);
  });

  it('conflicting identity → attention, non-collapsible (even if positions are clean)', () => {
    const r = projectAccountAttention(FLEET_ACCOUNT_CONFLICTING_CLEAN);
    expect(r.isAttention).toBe(true);
    expect(r.isCollapsible).toBe(false);
  });

  it('contaminated → attention, non-collapsible (even with consistent identity)', () => {
    const r = projectAccountAttention(FLEET_ACCOUNT_CONSISTENT_DIRTY);
    expect(r.isAttention).toBe(true);
    expect(r.isCollapsible).toBe(false);
  });

  it('unknown identity → attention, non-collapsible', () => {
    const r = projectAccountAttention(FLEET_ACCOUNT_UNKNOWN);
    expect(r.isAttention).toBe(true);
    expect(r.isCollapsible).toBe(false);
  });

  it('policy_blocks_starts → attention even with clean positions', () => {
    const fixture = {
      ...FLEET_ACCOUNT_CONSISTENT_CLEAN,
      contamination: {
        ...FLEET_ACCOUNT_CONSISTENT_CLEAN.contamination,
        policy_blocks_starts: true,
      },
    };
    const r = projectAccountAttention(fixture);
    expect(r.isAttention).toBe(true);
    expect(r.isCollapsible).toBe(false);
  });

  it('backend-authored account notice → attention even with clean positions', () => {
    const fixture: FleetAccountSummary = {
      ...FLEET_ACCOUNT_CONSISTENT_CLEAN,
      notice: {
        code: 'activity.source_blind_to_bot_orders',
        tier: 'warning',
        title: 'Broker evidence is unavailable',
        message: 'The data plane could not fetch broker net positions.',
        source_codes: [],
        forensic_facts: {},
        action: { kind: 'external_manual_check', label: 'Check positions in IBKR', target: null },
        runbook_slug: 'broker-evidence-health',
        occurred_at_ms: null,
      },
    };
    const r = projectAccountAttention(fixture);
    expect(r.isAttention).toBe(true);
    expect(r.isCollapsible).toBe(false);
  });
});
