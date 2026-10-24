import { describe, expect, it } from 'vitest';

import {
  CANCEL_CAPTURE_ACTION_ID,
  CAPTURE_ACTION_ID,
  DISMISS_CAPTURE_ACTION_ID,
  describeLakeHolding,
  sameHoldingRequest,
  toLakeHolding,
  type LakeCapture,
  type LakeHoldingReading,
  type LakeHoldingRequest,
} from './lake-holding';
import { FIRST_FETCHABLE_MS, LAST_FETCHABLE_MS, instrumentDataReport } from './testing/instrument-data-report';

const REQUEST: LakeHoldingRequest = { symbol: 'SPY', from: '2024-03-04', to: '2024-03-15', tree: 'polygon_split_adjusted' };
const IDLE: LakeCapture = { kind: 'idle' };

function read(held: Parameters<typeof instrumentDataReport>[0]): LakeHoldingReading {
  return { kind: 'read', request: REQUEST, holding: toLakeHolding(instrumentDataReport(held)) };
}

describe('toLakeHolding', () => {
  it('counts the finished sessions the lake lacks that a capture can fetch, apart from those it cannot', () => {
    const holding = toLakeHolding(instrumentDataReport({ sessions: 60, missing: 12, beforeProviderHistory: 3, building: 2 }));

    expect.soft(holding.sessions).toBe(60);
    expect.soft(holding.toCapture).toBe(12);
    expect.soft(holding.cannotCapture, 'a session another writer holds and one older than the provider serves').toBe(5);
    expect.soft(holding.captureSpan).toEqual({ startMs: FIRST_FETCHABLE_MS, endMs: LAST_FETCHABLE_MS });
  });

  it('counts a session that is still forming as neither held nor lacking', () => {
    const holding = toLakeHolding(instrumentDataReport({ sessions: 5, forming: 1 }));

    expect(holding).toEqual({ sessions: 5, toCapture: 0, cannotCapture: 0, captureSpan: null });
  });
});

describe('sameHoldingRequest', () => {
  it('is true for equal values however the object was built, and for two absent requests', () => {
    expect.soft(sameHoldingRequest({ ...REQUEST }, { ...REQUEST })).toBe(true);
    expect.soft(sameHoldingRequest(null, null)).toBe(true);
  });

  it.each([
    ['symbol', { symbol: 'QQQ' }],
    ['start', { from: '2024-03-05' }],
    ['end', { to: '2024-03-14' }],
    ['tree', { tree: 'raw' as const }],
  ])('is false when the %s differs', (_name, change) => {
    expect(sameHoldingRequest(REQUEST, { ...REQUEST, ...change })).toBe(false);
  });

  it('is false for a request against none', () => {
    expect(sameHoldingRequest(REQUEST, null)).toBe(false);
  });
});

describe('describeLakeHolding', () => {
  it('warns how many of the run’s sessions the lake lacks, why that is slow and to capture first, with the action', () => {
    const state = describeLakeHolding(read({ sessions: 60, missing: 12 }), IDLE, null);

    expect(state).toEqual({
      kind: 'partial',
      happened: '12 of 60 sessions aren’t in the lake yet.',
      why: 'The first run fetches them one session at a time, so it will be slow.',
      next: 'Capture them first.',
      actions: [{ id: CAPTURE_ACTION_ID, label: 'Capture 12 sessions' }],
    });
  });

  it('says it in the singular for one session', () => {
    const state = describeLakeHolding(read({ sessions: 60, missing: 1 }), IDLE, null);

    expect.soft(state?.happened).toBe('1 of 60 sessions isn’t in the lake yet.');
    expect.soft(state?.actions?.[0].label).toBe('Capture 1 session');
  });

  it('adds the sessions a capture cannot fetch, which a run that needs them is refused for', () => {
    const state = describeLakeHolding(read({ sessions: 60, missing: 12, beforeProviderHistory: 3 }), IDLE, null);

    expect.soft(state?.why).toContain('3 more cannot be captured now.');
    expect.soft(state?.why).toContain('a run that needs them is refused');
    expect.soft(state?.actions).toHaveLength(1);
  });

  it('offers no capture when every session the lake lacks is beyond one', () => {
    const state = describeLakeHolding(read({ sessions: 60, beforeProviderHistory: 3 }), IDLE, null);

    expect.soft(state?.happened).toBe('3 of 60 sessions are not in the lake and cannot be captured now.');
    expect.soft(state?.actions).toBeUndefined();
  });

  it('says nothing when the lake holds every session, while a count is read, and before there is one to read', () => {
    expect.soft(describeLakeHolding(read({ sessions: 60 }), IDLE, null)).toBeNull();
    expect.soft(describeLakeHolding({ kind: 'checking' }, IDLE, null)).toBeNull();
    expect.soft(describeLakeHolding({ kind: 'idle' }, IDLE, null)).toBeNull();
  });

  it('says a count that could not be read could not be, with the reason, and never that nothing is missing', () => {
    const state = describeLakeHolding({ kind: 'unreadable', message: 'Invalid trading date: pick both dates.' }, IDLE, null);

    expect.soft(state?.kind).toBe('unavailable');
    expect.soft(state?.happened).toContain('Could not check');
    expect.soft(state?.why).toBe('Invalid trading date: pick both dates.');
    expect.soft(state?.happened).not.toMatch(/\b0\b|none|every session/i);
  });

  it('says a running capture first, with the server’s progress and a way to cancel it', () => {
    const running: LakeCapture = { kind: 'running', symbol: 'SPY', sessions: 12 };

    const started = describeLakeHolding(read({ sessions: 60, missing: 12 }), running, null);
    const ticking = describeLakeHolding(read({ sessions: 60, missing: 12 }), running, { done: 5, total: 20 });

    expect.soft(started?.kind).toBe('filling');
    expect.soft(started?.happened).toBe('Capturing 12 sessions of SPY into the lake.');
    expect.soft(started?.progress).toEqual({ kind: 'phase', label: 'Starting the capture' });
    expect.soft(ticking?.progress).toEqual({ kind: 'counts', done: 5, total: 20, unit: 'days of the span' });
    expect.soft(ticking?.actions).toEqual([{ id: CANCEL_CAPTURE_ACTION_ID, label: 'Cancel the capture' }]);
  });

  it('says a capture that stopped, what it was and that Run study still fetches the rest', () => {
    const failed = describeLakeHolding(read({ sessions: 60, missing: 12 }), { kind: 'stopped', cancelled: false, message: 'Backfill Failed: provider refused' }, null);
    const cancelled = describeLakeHolding(read({ sessions: 60, missing: 12 }), { kind: 'stopped', cancelled: true, message: 'x' }, null);

    expect.soft(failed?.kind).toBe('failed');
    expect.soft(failed?.why).toBe('Backfill Failed: provider refused');
    expect.soft(failed?.next).toContain('Run study fetches whatever is still missing');
    expect.soft(failed?.actions).toEqual([{ id: DISMISS_CAPTURE_ACTION_ID, label: 'Dismiss' }]);
    expect.soft(cancelled?.kind).toBe('cancelled');
    expect.soft(cancelled?.why).toBeNull();
  });
});
