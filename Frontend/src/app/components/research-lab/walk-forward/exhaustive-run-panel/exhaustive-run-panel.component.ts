import { ChangeDetectionStrategy, Component, computed, effect, inject, input, signal } from '@angular/core';
import { RouterLink } from '@angular/router';
import { ButtonModule } from 'primeng/button';
import { MessageModule } from 'primeng/message';
import { ProgressBarModule } from 'primeng/progressbar';

import { ExhaustiveRunService } from '../../../../services/exhaustive-run.service';
import type { ExhaustiveRunResponse } from '../../../../services/exhaustive-run.types';
import { JobsService } from '../../../../services/jobs.service';
import { ExhaustiveRunTableComponent } from '../exhaustive-run-table/exhaustive-run-table.component';

const JOB_TYPE = 'spy_ema_exhaustive';

/** Runs and presents the selection-biased and forward evidence tracks together. */
@Component({
  selector: 'app-exhaustive-run-panel',
  imports: [
    ButtonModule,
    ExhaustiveRunTableComponent,
    MessageModule,
    ProgressBarModule,
    RouterLink,
  ],
  templateUrl: './exhaustive-run-panel.component.html',
  styleUrl: './exhaustive-run-panel.component.scss',
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class ExhaustiveRunPanelComponent {
  private readonly exhaustiveRuns = inject(ExhaustiveRunService);
  private readonly jobs = inject(JobsService);

  readonly walkForwardId = input.required<string>();
  readonly response = signal<ExhaustiveRunResponse | null>(null);
  readonly loading = signal(false);
  readonly starting = signal(false);
  readonly error = signal<string | null>(null);
  readonly jobId = signal<string | null>(null);
  readonly job = computed(() => {
    const id = this.jobId();
    return id === null ? undefined : this.jobs.job(id);
  });
  readonly running = computed(() => {
    const status = this.job()?.status;
    return this.starting() || status === 'queued' || status === 'running';
  });
  readonly progressPercent = computed(() => {
    const current = this.job()?.current;
    const total = this.job()?.total;
    if (current === undefined || total === undefined || total <= 0) return 0;
    return Math.min(100, Math.round((current / total) * 100));
  });

  private loadedFor: string | null = null;
  private resultSettledFor: string | null = null;
  private loadGeneration = 0;

  constructor() {
    effect(() => {
      const walkForwardId = this.walkForwardId();
      if (walkForwardId === this.loadedFor) return;
      this.loadedFor = walkForwardId;
      this.jobId.set(null);
      this.resultSettledFor = null;
      this.response.set(null);
      void this.load(walkForwardId);
    });

    effect(() => {
      if (this.jobId() === null) {
        const resumable = this.jobs.jobs().find((candidate) =>
          candidate.type === JOB_TYPE
          && (candidate.status === 'queued' || candidate.status === 'running')
          && candidate.parameters?.['walk_forward_id'] === this.walkForwardId());
        if (resumable !== undefined) this.jobId.set(resumable.id);
        return;
      }
      const current = this.job();
      if (current === undefined) return;
      if (current.status === 'completed' && this.resultSettledFor !== current.id) {
        this.resultSettledFor = current.id;
        void this.loadJobResult(current.id);
      } else if (current.status === 'failed') {
        this.error.set(current.errorMessage ?? 'The Exhaustive Run failed.');
      } else if (current.status === 'cancelled') {
        this.error.set('The Exhaustive Run was cancelled before completion.');
      }
    });
  }

  async load(walkForwardId: string): Promise<void> {
    const generation = ++this.loadGeneration;
    this.loading.set(true);
    this.error.set(null);
    try {
      const response = await this.exhaustiveRuns.getLatestForWalkForward(walkForwardId);
      if (this.isCurrentLoad(walkForwardId, generation)) {
        this.response.set(response);
      }
    } catch (error) {
      if (this.isCurrentLoad(walkForwardId, generation)) {
        this.error.set(errorMessage(error));
      }
    } finally {
      if (this.isCurrentLoad(walkForwardId, generation)) {
        this.loading.set(false);
      }
    }
  }

  async runAnalysis(): Promise<void> {
    const walkForwardId = this.walkForwardId();
    this.starting.set(true);
    this.error.set(null);
    try {
      this.resultSettledFor = null;
      const jobId = await this.jobs.startJob(JOB_TYPE, {
        walk_forward_id: walkForwardId,
      });
      if (walkForwardId === this.walkForwardId()) {
        this.jobId.set(jobId);
      }
    } catch (error) {
      if (walkForwardId === this.walkForwardId()) {
        this.error.set(errorMessage(error));
      }
    } finally {
      this.starting.set(false);
    }
  }

  async cancelAnalysis(): Promise<void> {
    const id = this.jobId();
    if (id === null || !this.running()) return;
    try {
      await this.jobs.cancelJob(id);
    } catch (error) {
      this.error.set(errorMessage(error));
    }
  }

  private async loadJobResult(jobId: string): Promise<void> {
    const walkForwardId = this.walkForwardId();
    ++this.loadGeneration;
    this.loading.set(false);
    try {
      const response = await this.jobs.fetchResult<ExhaustiveRunResponse>(jobId);
      if (walkForwardId !== this.walkForwardId() || jobId !== this.jobId()) return;
      if (response.config.source_walk_forward_id !== walkForwardId) {
        throw new Error('The completed job belongs to a different walk-forward receipt.');
      }
      this.response.set(response);
    } catch (error) {
      if (walkForwardId === this.walkForwardId() && jobId === this.jobId()) {
        this.error.set(errorMessage(error));
      }
    }
  }

  private isCurrentLoad(walkForwardId: string, generation: number): boolean {
    return walkForwardId === this.walkForwardId() && generation === this.loadGeneration;
  }
}

function errorMessage(error: unknown): string {
  if (error instanceof Error) return error.message;
  if (typeof error === 'object' && error !== null && 'message' in error) {
    return String((error as { message: unknown }).message);
  }
  return 'The Exhaustive Run evidence could not be loaded.';
}
