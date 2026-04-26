import {
  ChangeDetectionStrategy,
  Component,
  computed,
  inject,
  input,
} from '@angular/core';
import { ButtonModule } from 'primeng/button';
import { ProgressBarModule } from 'primeng/progressbar';
import { JobsService, JobState } from '../../services/jobs.service';

/**
 * Inline progress widget shown next to a feature that started a job.
 * Reads from the global `JobsService` registry so the same job can be
 * rendered in the inline spot AND in the persistent drawer simultaneously.
 */
@Component({
  selector: 'app-job-progress',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [ProgressBarModule, ButtonModule],
  styles: [`
    :host {
      display: block;
      padding: 0.75rem 1rem;
      border: 1px solid var(--surface-border, #e2e8f0);
      border-radius: 8px;
      background: var(--surface-card, #fff);
      font-size: 0.875rem;
    }
    .header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      margin-bottom: 0.5rem;
    }
    .label {
      font-weight: 600;
    }
    .phase {
      color: var(--text-color-secondary, #64748b);
      font-family: var(--font-mono, monospace);
      font-size: 0.75rem;
      text-transform: uppercase;
      letter-spacing: 0.04em;
    }
    .meta {
      display: flex;
      justify-content: space-between;
      color: var(--text-color-secondary, #64748b);
      font-size: 0.75rem;
      margin-top: 0.5rem;
    }
    .error {
      color: var(--red-600, #dc2626);
      margin-top: 0.5rem;
    }
    .actions {
      display: flex;
      gap: 0.5rem;
    }
  `],
  template: `
    @let job = jobOrNull();
    @if (job) {
      <div class="header">
        <span class="label">{{ statusLabel() }}</span>
        @if (job.phase) {
          <span class="phase" aria-label="Current phase">{{ job.phase }}</span>
        }
      </div>

      @if (showBar()) {
        <p-progressBar
          [value]="percent()"
          [showValue]="true"
          [mode]="indeterminate() ? 'indeterminate' : 'determinate'"
        />
      }

      <div class="meta">
        @if (job.message) {
          <span>{{ job.message }}</span>
        } @else if (job.current !== undefined && job.total !== undefined) {
          <span>{{ job.current }} / {{ job.total }} {{ job.unit ?? 'items' }}</span>
        } @else {
          <span></span>
        }
        <span>{{ elapsedLabel() }}</span>
      </div>

      @if (job.errorMessage) {
        <div class="error" role="alert">
          {{ job.errorCode }}: {{ job.errorMessage }}
        </div>
      }

      @if (canCancel()) {
        <div class="actions" style="margin-top: 0.5rem;">
          <p-button
            label="Cancel"
            severity="secondary"
            size="small"
            [outlined]="true"
            (onClick)="cancel()"
          />
        </div>
      }
    } @else {
      <span class="phase">No job</span>
    }
  `,
})
export class JobProgressComponent {
  private jobs = inject(JobsService);

  readonly jobId = input.required<string>();

  // Read from the registry signal so updates flow automatically.
  readonly jobOrNull = computed<JobState | undefined>(() => {
    const id = this.jobId();
    return this.jobs.jobs().find(j => j.id === id);
  });

  readonly percent = computed(() => {
    const j = this.jobOrNull();
    if (!j || !j.total || j.total <= 0 || j.current === undefined) return 0;
    return Math.min(100, Math.round((j.current / j.total) * 100));
  });

  readonly indeterminate = computed(() => {
    const j = this.jobOrNull();
    if (!j) return false;
    if (j.status === 'running' && (j.total === undefined || j.total <= 0)) {
      return true;
    }
    return j.status === 'queued';
  });

  readonly showBar = computed(() => {
    const j = this.jobOrNull();
    return j !== undefined && j.status !== 'failed' && j.status !== 'cancelled';
  });

  readonly canCancel = computed(() => {
    const s = this.jobOrNull()?.status;
    return s === 'queued' || s === 'running';
  });

  readonly statusLabel = computed(() => {
    const j = this.jobOrNull();
    if (!j) return '—';
    switch (j.status) {
      case 'queued': return 'Queued';
      case 'running': return 'Running';
      case 'completed': return 'Completed';
      case 'failed': return 'Failed';
      case 'cancelled': return 'Cancelled';
    }
  });

  readonly elapsedLabel = computed(() => {
    const j = this.jobOrNull();
    if (!j?.startedAt) return '';
    const end = j.finishedAt ?? Date.now();
    const sec = Math.max(0, Math.round((end - j.startedAt) / 1000));
    if (sec < 60) return `${sec}s`;
    const min = Math.floor(sec / 60);
    const rem = sec % 60;
    return `${min}m ${rem}s`;
  });

  cancel(): void {
    const id = this.jobId();
    void this.jobs.cancelJob(id);
  }
}
