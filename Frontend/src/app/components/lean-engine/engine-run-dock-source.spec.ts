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
): JobState {
  return {
    id,
    type,
    status: 'running',
    startedAt: timestamp,
    recentLogs: [],
    logSeq: 0,
    recentEvents: [{
      id: `${timestamp}-0`,
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
});
