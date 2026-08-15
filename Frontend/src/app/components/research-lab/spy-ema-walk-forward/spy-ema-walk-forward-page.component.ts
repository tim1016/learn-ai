import {
  ChangeDetectionStrategy,
  Component,
  computed,
  effect,
  inject,
  signal,
} from '@angular/core';

import { JobsService } from '../../../services/jobs.service';
import { StrategyRunsService } from '../../../services/strategy-runs.service';
import type {
  RunLedger,
  StrategyRunResponse,
} from '../../../services/strategy-runs.types';
import { WalkForwardService } from '../../../services/walk-forward.service';
import type { WalkForwardResponse } from '../../../services/walk-forward.types';
import type { SpyEmaPipelineResponse } from '../../../services/walk-forward.types';
import { AssetIdentityComponent } from '../../../shared/asset-identity/asset-identity.component';
import { SpyEmaEvidenceComponent } from './spy-ema-evidence.component';
import { SpyEmaProtocolComponent } from './spy-ema-protocol.component';

const CANONICAL_SPEC_ID = 'spy_ema_crossover';
const PROTOCOL_ID = 'spy-ema-normalized-gap';
const PROTOCOL_VERSION = '1.0';
const JOB_TYPE = 'spy_ema_walk_forward';

/** Research cockpit for the SPY EMA normalized-gap walk-forward study. */
@Component({
  selector: 'app-spy-ema-walk-forward-page',
  imports: [
    AssetIdentityComponent,
    SpyEmaEvidenceComponent,
    SpyEmaProtocolComponent,
  ],
  templateUrl: './spy-ema-walk-forward-page.component.html',
  styleUrl: './spy-ema-walk-forward-page.component.scss',
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class SpyEmaWalkForwardPageComponent {
  private readonly strategyRuns = inject(StrategyRunsService);
  private readonly walkForwards = inject(WalkForwardService);
  private readonly jobs = inject(JobsService);

  readonly control = signal<StrategyRunResponse | null>(null);
  readonly walkForward = signal<WalkForwardResponse | null>(null);
  readonly loading = signal(false);
  private readonly starting = signal(false);
  readonly jobId = signal<string | null>(null);
  readonly job = computed(() => {
    const id = this.jobId();
    return id === null ? undefined : this.jobs.job(id);
  });
  readonly running = computed(() => {
    const status = this.job()?.status;
    return this.starting() || status === 'queued' || status === 'running';
  });
  readonly error = signal<string | null>(null);
  readonly walkForwardError = signal<string | null>(null);

  private resultSettledFor: string | null = null;

  constructor() {
    effect(() => {
      if (this.jobId() === null) {
        const resumable = this.jobs.jobs().find(candidate =>
          candidate.type === JOB_TYPE
          && (candidate.status === 'queued' || candidate.status === 'running'));
        if (resumable !== undefined) {
          this.jobId.set(resumable.id);
        }
        return;
      }
      const current = this.job();
      if (current === undefined) return;
      if (current.status === 'completed' && this.resultSettledFor !== current.id) {
        this.resultSettledFor = current.id;
        void this.loadJobResult(current.id);
      } else if (current.status === 'failed') {
        this.error.set(current.errorMessage ?? 'The research job failed.');
      } else if (current.status === 'cancelled') {
        this.error.set('The research job was cancelled before completion.');
      }
    });
    void this.refresh();
  }

  async refresh(): Promise<void> {
    this.loading.set(true);
    this.error.set(null);
    this.walkForwardError.set(null);
    this.control.set(null);
    this.walkForward.set(null);

    try {
      const listing = await this.walkForwards.listWalkForwards({
        protocol_id: PROTOCOL_ID,
        protocol_version: PROTOCOL_VERSION,
        limit: 1,
      });
      const latest = listing.walk_forwards.find(isCanonicalProtocol);

      if (latest === undefined) {
        return;
      }

      const parentRunId = latest.parent_run_id;
      if (parentRunId === null) {
        throw new Error('The protocol receipt has no canonical control run.');
      }
      const control = await this.strategyRuns.getRun(parentRunId);
      if (!isCanonicalSpyEmaRun(control.ledger)) {
        throw new Error('The protocol parent is not the canonical SPY EMA control.');
      }
      this.control.set(control);
      try {
        const walkForward = await this.walkForwards.getWalkForward(
          latest.walk_forward_id,
        );
        if (!isCanonicalProtocol(walkForward.config)) {
          throw new Error('The stored receipt is not SPY EMA protocol V1 evidence.');
        }
        this.walkForward.set(walkForward);
      } catch (error) {
        this.walkForwardError.set(errorMessage(error));
      }
    } catch (error) {
      this.error.set(errorMessage(error));
    } finally {
      this.loading.set(false);
    }
  }

  async runStudy(): Promise<void> {
    this.starting.set(true);
    this.error.set(null);
    this.walkForwardError.set(null);

    try {
      this.resultSettledFor = null;
      this.jobId.set(await this.jobs.startJob(JOB_TYPE, {}));
    } catch (error) {
      this.error.set(errorMessage(error));
    } finally {
      this.starting.set(false);
    }
  }

  async cancelStudy(): Promise<void> {
    const id = this.jobId();
    if (id === null || !this.running()) return;
    try {
      await this.jobs.cancelJob(id);
    } catch (error) {
      this.error.set(errorMessage(error));
    }
  }

  private async loadJobResult(jobId: string): Promise<void> {
    try {
      const pipeline = await this.jobs.fetchResult<SpyEmaPipelineResponse>(jobId);
      if (!isCanonicalProtocol(pipeline.walk_forward.config)) {
        throw new Error('The completed job did not return SPY EMA protocol V1 evidence.');
      }
      this.control.set(pipeline.control);
      this.walkForward.set(pipeline.walk_forward);
    } catch (error) {
      this.error.set(errorMessage(error));
    }
  }
}

function isCanonicalSpyEmaRun(run: RunLedger): boolean {
  return run.strategy_spec_id === CANONICAL_SPEC_ID
    && run.symbol === 'SPY'
    && run.status === 'completed';
}

function isCanonicalProtocol(config: WalkForwardResponse['config']): boolean {
  return config.protocol_id === PROTOCOL_ID
    && config.protocol_version === PROTOCOL_VERSION;
}

function errorMessage(error: unknown): string {
  if (error instanceof Error) return error.message;
  if (typeof error === 'object' && error !== null && 'message' in error) {
    return String((error as { message: unknown }).message);
  }
  return 'The research evidence could not be loaded.';
}
