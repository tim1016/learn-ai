// PRD #617 — account attention projection specs.

import { describe, expect, it } from 'vitest';

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
});
