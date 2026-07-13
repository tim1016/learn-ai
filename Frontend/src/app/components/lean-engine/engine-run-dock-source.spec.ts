import { provideZonelessChangeDetection, signal } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { describe, expect, it } from 'vitest';

import { JobsService, type JobState } from '../../services/jobs.service';
import { EngineRunDockSource } from './engine-run-dock-source';

function job(
  id: string,
  type: string,
  timestamp: number,
  summary: string,
  eventId = `${timestamp}-0`,
): JobState {
  return {
    id,
    type,
    status: 'running',
    startedAt: timestamp,
    recentLogs: [],
    logSeq: 0,
    recentEvents: [{
      id: eventId,
      type: 'job.phase',
      timestamp,
      level: 'info',
      summary,
    }],
  };
}

describe('EngineRunDockSource', () => {
  it('keeps Python and LEAN events in one server-ordered validation timeline', () => {
    const jobs = signal<JobState[]>([]);
    TestBed.configureTestingModule({
      providers: [
        provideZonelessChangeDetection(),
        EngineRunDockSource,
        { provide: JobsService, useValue: { jobs } },
      ],
    });
    const source = TestBed.inject(EngineRunDockSource);

    jobs.set([
      job('py-1', 'engine_backtest', 2000, 'Running indicators'),
      job('lean-1', 'lean_engine_run', 1000, 'Staging LEAN data'),
    ]);
    TestBed.flushEffects();

    expect(source.log().map((event) => event.message)).toEqual([
      '[LEAN] Staging LEAN data',
      '[Python] Running indicators',
    ]);
    expect(source.runMeta()?.runId).toBe('py-1');
  });

  it('continues folding events after the service trims the rolling event window', () => {
    const jobs = signal<JobState[]>([]);
    TestBed.configureTestingModule({
      providers: [
        provideZonelessChangeDetection(),
        EngineRunDockSource,
        { provide: JobsService, useValue: { jobs } },
      ],
    });
    const source = TestBed.inject(EngineRunDockSource);

    jobs.set([job('py-1', 'engine_backtest', 1000, 'Event 499', '499-0')]);
    TestBed.flushEffects();
    jobs.set([job('py-1', 'engine_backtest', 1001, 'Event 500', '500-0')]);
    TestBed.flushEffects();

    expect(source.log().map((event) => event.message)).toEqual([
      '[Python] Event 499',
      '[Python] Event 500',
    ]);
  });
});
