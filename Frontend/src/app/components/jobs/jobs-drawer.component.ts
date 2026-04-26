import {
  ChangeDetectionStrategy,
  Component,
  computed,
  inject,
  signal,
} from '@angular/core';
import { Drawer } from 'primeng/drawer';
import { ButtonModule } from 'primeng/button';
import { JobProgressComponent } from './job-progress.component';
import { JobsService } from '../../services/jobs.service';

/**
 * Persistent global drawer surfacing every in-flight and recently-finished
 * job. Mounted once in `app.component.ts`. A floating launcher button
 * sits in the bottom-right; the drawer slides in from the right.
 */
@Component({
  selector: 'app-jobs-drawer',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [Drawer, ButtonModule, JobProgressComponent],
  styles: [`
    .launcher {
      position: fixed;
      right: 1.5rem;
      bottom: 1.5rem;
      z-index: 900;
      display: inline-flex;
      align-items: center;
      gap: 0.5rem;
      padding: 0.6rem 1rem;
      border-radius: 999px;
      border: 1px solid var(--surface-border, #e2e8f0);
      background: var(--surface-card, #fff);
      color: var(--text-color, #0f172a);
      font-size: 0.8rem;
      font-weight: 600;
      box-shadow: 0 6px 16px rgba(15, 23, 42, 0.12);
      cursor: pointer;
    }
    .launcher:hover {
      background: var(--surface-hover, #f1f5f9);
    }
    .launcher .badge {
      background: var(--primary-color, #2563eb);
      color: white;
      border-radius: 999px;
      padding: 0.05rem 0.5rem;
      font-size: 0.7rem;
    }
    .launcher .badge.idle {
      background: var(--surface-300, #cbd5e1);
      color: var(--text-color, #0f172a);
    }
    .panel {
      display: flex;
      flex-direction: column;
      gap: 0.75rem;
    }
    .empty {
      color: var(--text-color-secondary, #64748b);
      font-size: 0.85rem;
      padding: 1rem 0;
    }
    .row {
      display: flex;
      align-items: flex-start;
      gap: 0.5rem;
    }
    .row app-job-progress {
      flex: 1;
    }
    .dismiss {
      align-self: stretch;
    }
    h4 {
      margin: 0 0 0.25rem;
      font-size: 0.95rem;
    }
    p.subtitle {
      margin: 0 0 0.75rem;
      color: var(--text-color-secondary, #64748b);
      font-size: 0.8rem;
    }
  `],
  template: `
    <button
      type="button"
      class="launcher"
      (click)="open.set(true)"
      [attr.aria-label]="'Jobs drawer, ' + activeCount() + ' active'"
      [attr.aria-expanded]="open()"
    >
      <i class="pi pi-server" aria-hidden="true"></i>
      <span>Jobs</span>
      <span class="badge" [class.idle]="activeCount() === 0">{{ activeCount() }}</span>
    </button>

    <p-drawer
      [visible]="open()"
      (visibleChange)="onVisibleChange($event)"
      position="right"
      [modal]="true"
      [dismissible]="true"
      [style]="{ width: 'min(480px, 92vw)' }"
    >
      <ng-template pTemplate="header">
        <h4>Background jobs</h4>
      </ng-template>

      <p class="subtitle">
        Long-running work shows up here so you can keep using the app while it runs.
      </p>

      @if (jobs().length === 0) {
        <div class="empty">No jobs yet.</div>
      } @else {
        <div class="panel">
          @for (job of jobs(); track job.id) {
            <div class="row">
              <app-job-progress [jobId]="job.id" />
              @if (job.status === 'completed' || job.status === 'failed' || job.status === 'cancelled') {
                <button
                  type="button"
                  class="dismiss"
                  aria-label="Dismiss job"
                  (click)="dismiss(job.id)"
                  style="background:none;border:none;color:var(--text-color-secondary,#64748b);cursor:pointer;padding:0 0.25rem;"
                >
                  <i class="pi pi-times" aria-hidden="true"></i>
                </button>
              }
            </div>
          }
        </div>
      }
    </p-drawer>
  `,
})
export class JobsDrawerComponent {
  private jobsService = inject(JobsService);

  readonly open = signal(false);

  // Most-recent first.
  readonly jobs = computed(() =>
    [...this.jobsService.jobs()].sort((a, b) =>
      (b.startedAt ?? 0) - (a.startedAt ?? 0),
    ),
  );

  readonly activeCount = computed(() => this.jobsService.activeJobs().length);

  onVisibleChange(visible: boolean): void {
    this.open.set(visible);
  }

  dismiss(id: string): void {
    this.jobsService.dismiss(id);
  }
}
