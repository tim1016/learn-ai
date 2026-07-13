import { describe, expect, it } from 'vitest';

import { applyJobEvent, streamTimestamp, type JobState } from './jobs.service';

function queuedJob(): JobState {
  return {
    id: 'job-1',
    type: 'engine_backtest',
    status: 'queued',
    recentLogs: [],
    logSeq: 0,
  };
}

describe('JobsService SSE reducer', () => {
  it('uses the Redis stream id as the authoritative server event timestamp', () => {
    const started = applyJobEvent(
      queuedJob(),
      { type: 'job.started' },
      '1783896000123-0',
    );

    expect(started.startedAt).toBe(1_783_896_000_123);
    expect(started.recentEvents?.[0]).toMatchObject({
      id: '1783896000123-0',
      timestamp: 1_783_896_000_123,
      type: 'job.started',
      summary: 'Run started',
    });
  });

  it('records phase and progress events in the structured timeline', () => {
    const phased = applyJobEvent(
      queuedJob(),
      { type: 'job.phase', phase: 'running_indicators', friendly: 'Running indicators' },
      '1783896001000-0',
    );
    const progressed = applyJobEvent(
      phased,
      { type: 'job.progress', current: 250, total: 1000, unit: 'bars', message: 'Evaluated' },
      '1783896002000-0',
    );

    expect(progressed.recentEvents?.map((event) => event.summary)).toEqual([
      'Running indicators',
      'Evaluated · 250 / 1,000 bars',
    ]);
  });

  it('deduplicates a replayed SSE event by stream id', () => {
    const event = { type: 'job.phase' as const, phase: 'persisting' };
    const once = applyJobEvent(queuedJob(), event, '1783896003000-0');
    const replayed = applyJobEvent(once, event, '1783896003000-0');

    expect(replayed.recentEvents).toHaveLength(1);
  });

  it('rejects malformed stream ids as timestamps', () => {
    expect(streamTimestamp('not-a-stream-id')).toBeNull();
    expect(streamTimestamp('0-0')).toBeNull();
  });
});
