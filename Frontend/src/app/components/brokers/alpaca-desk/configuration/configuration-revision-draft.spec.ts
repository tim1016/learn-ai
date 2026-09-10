import { describe, expect, it } from 'vitest';

import type { BrokerProfileRevision } from '../../../../api/alpaca.types';
import {
  ENVELOPE_KEYS,
  draftFromRevision,
  draftProblems,
  emptyDraft,
  preferredSlot,
  toRevisionContent,
} from './configuration-revision-draft';

function liveDraft() {
  return {
    ...emptyDraft('live'),
    endpoint_mode: 'live' as const,
    loss_fraction: 0.05,
    loss_usd: 5000,
    shadow_sessions: 3,
    arming_max_sessions: 20,
    xh_entry_bps: 11,
    xh_exit_bps: 17.5,
  };
}

describe('configuration revision draft', () => {
  it('starts every live envelope value empty rather than defaulted', () => {
    const draft = emptyDraft('default');

    for (const key of ENVELOPE_KEYS) {
      expect(draft[key]).toBeNull();
    }
  });

  it('lets a paper draft save with no envelope at all', () => {
    expect(draftProblems(emptyDraft('default'))).toEqual([]);
    expect(toRevisionContent(emptyDraft('default')).live_envelope).toBeNull();
  });

  it('names every live value the operator has not supplied', () => {
    const problems = draftProblems({ ...emptyDraft('live'), endpoint_mode: 'live' });

    expect(problems).toHaveLength(ENVELOPE_KEYS.length);
    expect(problems.every((problem) => problem.includes('needs a number'))).toBe(true);
  });

  it('refuses a fractional session count, which the service would only reject as a 422', () => {
    const problems = draftProblems({ ...liveDraft(), shadow_sessions: 3.5 });

    expect(problems).toEqual(['Shadow sessions required must be a whole number.']);
  });

  it('refuses a loss fraction outside the open unit interval', () => {
    expect(draftProblems({ ...liveDraft(), loss_fraction: 0 })).toHaveLength(1);
    expect(draftProblems({ ...liveDraft(), loss_fraction: 1 })).toHaveLength(1);
    expect(draftProblems({ ...liveDraft(), loss_fraction: 0.5 })).toEqual([]);
  });

  it('admits a zero extended-hours offset and refuses the ceiling', () => {
    expect(draftProblems({ ...liveDraft(), xh_entry_bps: 0 })).toEqual([]);
    expect(draftProblems({ ...liveDraft(), xh_exit_bps: 10_000 })).toHaveLength(1);
  });

  it('refuses to build a live body from a draft with a missing value', () => {
    expect(() => toRevisionContent({ ...liveDraft(), loss_usd: null })).toThrowError(/loss_usd/);
  });

  it('carries a stored revision forward without changing a value', () => {
    const stored: BrokerProfileRevision = {
      profile_id: 'p1',
      revision: 4,
      schema_version: 1,
      credential_slot: 'live',
      endpoint_mode: 'live',
      account_pin: '9LIVE0001',
      account_pinned_at_ms: 1_757_000_000_000,
      live_envelope: {
        loss_fraction: 0.05,
        loss_usd: 5000,
        shadow_sessions: 3,
        arming_max_sessions: 20,
        xh_entry_bps: 11,
        xh_exit_bps: 17.5,
      },
      content_sha256: 'a'.repeat(64),
      complete: true,
      author_owner_id: 'owner-1',
      created_at_ms: 1_757_000_000_000,
    };

    expect(toRevisionContent(draftFromRevision(stored)).live_envelope).toEqual(
      stored.live_envelope,
    );
  });

  it('prefers a slot with credentials injected, and still offers one when none has any', () => {
    expect(
      preferredSlot([
        { slot: 'default', label: 'Default', available: false },
        { slot: 'live', label: 'Live', available: true },
      ]),
    ).toBe('live');
    expect(
      preferredSlot([{ slot: 'default', label: 'Default', available: false }]),
    ).toBe('default');
    expect(preferredSlot([])).toBe('');
  });
});
