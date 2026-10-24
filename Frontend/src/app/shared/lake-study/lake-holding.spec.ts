import { describe, expect, it } from 'vitest';

import type { InstrumentDataRequest } from '../data-lake';
import { heldReport } from '../data-lake/testing/instrument-data-report';
import {
  CANCEL_CAPTURE_ACTION_ID,
  CAPTURE_ACTION_ID,
  DISMISS_CAPTURE_ACTION_ID,
  describeLakeHolding,
  toLakeHolding,
  type LakeCapture,
  type LakeHoldingReading,
} from './lake-holding';

const REQUEST: InstrumentDataRequest = { symbol: 'SPY', from: '2024-03-04', to: '2024-03-15', tree: 'polygon_split_adjusted' };
const IDLE: LakeCapture = { kind: 'idle' };

function read(held: Parameters<typeof heldReport>[0]): LakeHoldingReading {
  return { kind: 'read', request: REQUEST, holding: toLakeHolding(heldReport(held)) };
}

describe('toLakeHolding', () => {
  it('counts the finished sessions the lake lacks that a capture can fetch, apart from each kind it cannot', () => {
    const report = heldReport({ sessions: 60, missing: 12, beforeProviderHistory: 3, providerNoData: 1, building: 2 });

    const holding = toLakeHolding(report);

    expect.soft(holding.sessions).toBe(60);
    expect.soft(holding.toCapture).toBe(12);
    expect.soft(holding.beforeProviderHistory, 'older than the provider serves').toBe(3);
    expect.soft(holding.providerNoData, 'the provider answered with no bars').toBe(1);
    expect.soft(holding.building, 'another writer holds a claim').toBe(2);
    expect.soft(holding.captureSpan, 'the span is the server’s own, not one the browser composed').toBe(report.fill_window);
  });

  it('counts a session that is still forming as neither held nor lacking', () => {
    const holding = toLakeHolding(heldReport({ sessions: 5, forming: 1 }));

    expect(holding).toEqual({ sessions: 5, toCapture: 0, beforeProviderHistory: 0, providerNoData: 0, building: 0, captureSpan: null });
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

  it('adds the sessions a capture cannot fetch, and that a run refused for one older than the provider serves fetches nothing, so is not slow', () => {
    const state = describeLakeHolding(read({ sessions: 60, missing: 12, beforeProviderHistory: 3 }), IDLE, null);

    expect.soft(state?.why).toBe(
      'More cannot be captured now. 3 sessions are older than the provider serves. A run that needs them is refused, and fetches nothing.',
    );
    expect.soft(state?.why).not.toContain('slow');
    expect.soft(state?.actions).toHaveLength(1);
  });

  it('keeps the slow-first-run warning when what a capture cannot fetch is only waited for or asked again', () => {
    const state = describeLakeHolding(read({ sessions: 60, missing: 12, building: 2, providerNoData: 1 }), IDLE, null);

    expect.soft(state?.why).toBe(
      'The first run fetches them one session at a time, so it will be slow. More cannot be captured now. ' +
        '1 session drew no bars from the provider before. A run asks again, and is refused if it still gets none. ' +
        '2 sessions are being captured by another run. A run waits for that capture, and is refused if it does not finish in time.',
    );
  });

  it.each([
    [
      'older than the provider serves',
      { beforeProviderHistory: 3 },
      '3 of 60 sessions are not in the lake and cannot be captured now.',
      '3 sessions are older than the provider serves. A run that needs them is refused, and fetches nothing.',
    ],
    [
      'older than the provider serves, one',
      { beforeProviderHistory: 1 },
      '1 of 60 sessions is not in the lake and cannot be captured now.',
      '1 session is older than the provider serves. A run that needs it is refused, and fetches nothing.',
    ],
    [
      'drew no bars before',
      { providerNoData: 2 },
      '2 of 60 sessions are not in the lake and cannot be captured now.',
      '2 sessions drew no bars from the provider before. A run asks again, and is refused if it still gets none.',
    ],
    [
      'held by another capture',
      { building: 2 },
      '2 of 60 sessions are not in the lake and cannot be captured now.',
      '2 sessions are being captured by another run. A run waits for that capture, and is refused if it does not finish in time.',
    ],
  ])('words what the run does with sessions a capture cannot fetch when nothing else is lacking: %s', (_name, lacking, happened, why) => {
    const state = describeLakeHolding(read({ sessions: 60, ...lacking }), IDLE, null);

    expect.soft(state?.kind).toBe('partial');
    expect.soft(state?.happened).toBe(happened);
    expect.soft(state?.why).toBe(why);
    expect.soft(state?.actions, 'there is nothing a capture could fetch').toBeUndefined();
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
