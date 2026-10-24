import { HttpErrorResponse, HttpResponse, provideHttpClient, withInterceptors, type HttpInterceptorFn } from '@angular/common/http';
import { signal, type EnvironmentProviders, type Provider, type WritableSignal } from '@angular/core';
import { of, throwError } from 'rxjs';

import { GridSearchService } from '../../../components/grid-search/grid-search.service';
import type { GridSearchDetail, GridSearchSpecRequest } from '../../../components/grid-search/grid-search.types';
import type { StrategyInfo } from '../../../components/strategy-lab/strategy-lab.models';
import { WalkForwardStudyService } from '../../../components/walk-forward-study/walk-forward-study.service';
import { BacktestRunsService } from '../../../services/backtest-runs.service';
import type { BacktestRunDetail } from '../../../services/backtest-runs.types';
import { JobsService, type JobState } from '../../../services/jobs.service';
import { LeanSidecarService } from '../../../services/lean-sidecar.service';
import { fakePickerWorld } from '../../../shared/symbol-picker/testing/fake-picker-world';
import { provideSignalStrategyWorld, type SignalStrategyReads, type SignalStrategyWorld } from '../../testing/signal-strategy-world';

/**
 * The reads and writes Explore's three tools make, as a spec sees them. Every
 * call that would start work is recorded, so a spec can say that a handoff
 * started nothing; the reads answer from the world.
 */
export interface ExploreWorld {
  /** Saved backtest runs by id; an id not listed is a run the server does not have. */
  readonly runs: Readonly<Record<number, BacktestRunDetail>>;
  /** Saved Grid Searches by id. */
  readonly searches: Readonly<Record<string, GridSearchDetail>>;
}

export interface FakeJobs {
  /** Jobs the registry reports active, as `JobsService.resumeActive()` would find them. */
  readonly active: WritableSignal<JobState[]>;
  /** Every job a page asked to start: its type and its parameters. */
  readonly started: { readonly type: string; readonly parameters: unknown }[];
  /** How many times a page asked the registry again. One call per runner built. */
  refreshCalls: number;
  /** What a finished job's stored result reads as. */
  result: unknown;
}

export interface ExploreCalls {
  readonly jobs: FakeJobs;
  /** The specs the Grid Search preflight was asked about. */
  readonly sweepPreflights: GridSearchSpecRequest[];
  /** The specs the Walk-forward preflight was asked about. */
  readonly walkForwardPreflights: unknown[];
  /** Runs read through `BacktestRunsService.get`. */
  readonly runReads: number[];
}

export function noExploreEvidence(): ExploreWorld {
  return { runs: {}, searches: {} };
}

/**
 * Chart and catalog reads a loaded run pulls in that no Explore spec asserts on.
 * Each is answered rather than left open, and the two that carry real payloads
 * fail the way an outage does, so the page's own failure path handles them.
 */
function auxiliaryReads(schemas: readonly StrategyInfo[]): HttpInterceptorFn {
  return (request, next) => {
    const answer = (body: unknown) => of(new HttpResponse({ status: 200, body }));
    if (request.url.endsWith('/api/engine/strategies')) return answer(schemas);
    if (request.url.endsWith('/api/dataset/available')) return answer(null);
    if (request.url.endsWith('/api/snapshot/ticker')) return answer({ success: false, snapshot: null, error: null });
    if (request.url.endsWith('/api/strategy-gates/catalogue')) return answer({ indicators: [] });
    if (request.url.endsWith('/api/chart/indicators/supported')) return answer({ names: [] });
    if (request.url.endsWith('/api/engine/chart') || request.url.endsWith('/strategy-view')) {
      return throwError(() => new HttpErrorResponse({ status: 503, url: request.url }));
    }
    return next(request);
  };
}

/** A saved-run read: the run, or `null` when the server does not have it. */
function runRead(world: ExploreWorld, calls: ExploreCalls) {
  return (id: number) => {
    calls.runReads.push(id);
    return of(world.runs[id] ?? null);
  };
}

/**
 * Providers that put Explore in front of a world, behind the real routes: the
 * Signal strategies world, the services Strategy Lab, Grid Search and Walk-forward
 * read through, and the pickers' catalogs and calendar. Nothing here answers a
 * start: a job is recorded, and the registry's active list is the spec's to set.
 */
export function provideExploreWorld(
  strategyWorld: SignalStrategyWorld,
  evidence: ExploreWorld = noExploreEvidence(),
  reads: Partial<SignalStrategyReads> = {},
): { readonly providers: (Provider | EnvironmentProviders)[]; readonly calls: ExploreCalls } {
  const jobs: FakeJobs = { active: signal<JobState[]>([]), started: [], refreshCalls: 0, result: { success: false } };
  const calls: ExploreCalls = { jobs, sweepPreflights: [], walkForwardPreflights: [], runReads: [] };
  const schemas = strategyWorld.schemas instanceof Error ? [] : strategyWorld.schemas;
  let nextJob = 1;
  const picker = fakePickerWorld();

  return {
    calls,
    providers: [
      ...provideSignalStrategyWorld(strategyWorld, reads),
      provideHttpClient(withInterceptors([auxiliaryReads(schemas)])),
      ...picker.providers,
      {
        provide: JobsService,
        useValue: {
          jobs: jobs.active,
          activeJobs: jobs.active,
          job: (id: string) => jobs.active().find((job) => job.id === id) ?? null,
          resumed: signal(true),
          startJob: async (type: string, parameters: unknown) => {
            jobs.started.push({ type, parameters });
            return `job-${nextJob++}`;
          },
          fetchResult: async () => jobs.result,
          refreshActive: async () => {
            jobs.refreshCalls += 1;
          },
          registryError: signal<string | null>(null),
          cancelJob: async () => undefined,
        },
      },
      {
        provide: LeanSidecarService,
        useValue: {
          diagnose: async () => ({}),
          nextTradingDayOpen: async () => ({ session_open_ms_utc: 1_700_000_000_000 }),
        },
      },
      {
        provide: BacktestRunsService,
        useValue: { get: runRead(evidence, calls), list: () => of(Object.values(evidence.runs)), updateNotes: () => of({ id: 0, notes: null }) },
      },
      {
        provide: GridSearchService,
        useValue: {
          get: async (id: string) => {
            const search = evidence.searches[id];
            if (search === undefined) throw new HttpErrorResponse({ status: 404, url: `/api/research/grid-search/${id}` });
            return search;
          },
          list: async () => [],
          preflight: async (spec: GridSearchSpecRequest) => {
            calls.sweepPreflights.push(spec);
            throw new Error('preflight not answered in this world');
          },
          launch: async () => 'job-launch',
          cells: async () => ({ total: 0, page: 1, page_size: 25, sort_by: 'sharpe_ratio', direction: 'desc', without_curve: 0, cells: [] }),
          cellCurves: async () => ({ sessions_ms: [], curves: [], not_recorded: [] }),
        },
      },
      {
        provide: WalkForwardStudyService,
        useValue: {
          list: async () => [],
          get: async () => {
            throw new Error('no study in this world');
          },
          preflight: async (spec: unknown) => {
            calls.walkForwardPreflights.push(spec);
            throw new Error('preflight not answered in this world');
          },
          launch: async () => 'job-launch',
        },
      },
    ],
  };
}
