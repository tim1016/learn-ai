import { ComponentFixture, TestBed } from '@angular/core/testing';
import { HttpClientTestingModule } from '@angular/common/http/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { signal, computed, type Signal } from '@angular/core';
import { vi } from 'vitest';

import { FeatureRunnerComponent } from './feature-runner.component';
import { JobsService, type JobState } from '../../../services/jobs.service';

/**
 * The feature runner moved from a synchronous `ResearchService` call to a
 * job-based SSE flow (`JobsService.startJob`). The component is now
 * mostly a thin orchestrator over `JobsService`, so the unit test focuses
 * on what the component actually owns: the `canRun` derived state and
 * the `runResearch()` → `JobsService.startJob` wiring. The full
 * SSE-driven completion flow is exercised end-to-end in the runner test
 * suite under `tests/jobs` and via manual smoke tests; recreating it
 * here would just be re-mocking `JobsService` internals.
 */

interface JobsServiceMock {
  jobs: Signal<JobState[]>;
  job: (id: string) => JobState | undefined;
  startJob: ReturnType<typeof vi.fn>;
  cancelJob: ReturnType<typeof vi.fn>;
  fetchResult: ReturnType<typeof vi.fn>;
  dismiss: ReturnType<typeof vi.fn>;
}

describe('FeatureRunnerComponent', () => {
  let component: FeatureRunnerComponent;
  let fixture: ComponentFixture<FeatureRunnerComponent>;
  let jobsServiceMock: JobsServiceMock;
  // Backing signal so tests can simulate "this id is in a running job".
  let jobsByIdSignal: ReturnType<typeof signal<Map<string, JobState>>>;

  beforeEach(async () => {
    jobsByIdSignal = signal<Map<string, JobState>>(new Map());
    const jobsList = computed(() => Array.from(jobsByIdSignal().values()));

    jobsServiceMock = {
      jobs: jobsList,
      job: (id: string) => jobsByIdSignal().get(id),
      startJob: vi.fn().mockResolvedValue('job-id-1'),
      cancelJob: vi.fn().mockResolvedValue(undefined),
      fetchResult: vi.fn().mockResolvedValue({}),
      dismiss: vi.fn(),
    };

    await TestBed.configureTestingModule({
      imports: [
        FeatureRunnerComponent,
        HttpClientTestingModule,
        NoopAnimationsModule,
      ],
      providers: [
        { provide: JobsService, useValue: jobsServiceMock },
      ],
    }).compileComponents();

    fixture = TestBed.createComponent(FeatureRunnerComponent);
    component = fixture.componentInstance;
    fixture.detectChanges();
  });

  it('should create', () => {
    expect(component).toBeTruthy();
  });

  it('canRun is true with the default form values and no in-flight job', () => {
    // The component seeds AAPL / momentum_5m / 2024-01-01 / 2024-03-31
    // in its signals; that's enough to satisfy `canRun`.
    expect(component.canRun()).toBe(true);
  });

  it('canRun is false when the ticker is empty', () => {
    component.ticker.set('');
    expect(component.canRun()).toBe(false);
  });

  it('canRun is false while a job is running', () => {
    component.jobId.set('job-id-1');
    jobsByIdSignal.set(
      new Map<string, JobState>([
        [
          'job-id-1',
          {
            id: 'job-id-1',
            type: 'feature_research',
            status: 'running',
            recentLogs: [],
            logSeq: 0,
          },
        ],
      ]),
    );

    expect(component.loading()).toBe(true);
    expect(component.canRun()).toBe(false);
  });

  it('runResearch dispatches the feature_research job via JobsService', async () => {
    component.ticker.set('aapl'); // verify the upper-casing path
    // The catalog drives feature selection — pick the indicator + params
    // that map to the legacy feature_name='momentum_5m' worker call.
    component.selectedIndicator.set('mom');
    component.selectedParams.set({ length: 5 });
    component.fromDate.set('2024-01-01');
    component.toDate.set('2024-03-31');

    await component.runResearch();

    expect(jobsServiceMock.startJob).toHaveBeenCalledOnce();
    const [type, payload] = jobsServiceMock.startJob.mock.calls[0];
    expect(type).toBe('feature_research');
    expect(payload).toMatchObject({
      ticker: 'AAPL',
      feature_name: 'momentum_5m',
      from_date: '2024-01-01',
      to_date: '2024-03-31',
    });
    expect(component.jobId()).toBe('job-id-1');
  });

  it('runResearch surfaces a startJob failure into the error signal', async () => {
    jobsServiceMock.startJob.mockRejectedValueOnce(new Error('Network error'));

    await component.runResearch();

    expect(component.error()).toBe('Network error');
    expect(component.jobId()).toBeNull();
  });
});
