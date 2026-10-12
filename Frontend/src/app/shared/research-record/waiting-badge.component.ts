import { ChangeDetectionStrategy, Component, computed, inject, input } from '@angular/core';

import { JobsService } from '../../services/jobs.service';

/**
 * The pooled research run's waiting state, as one concise line (#3052).
 *
 * Server-authored from accepted scheduling facts: "waiting for machine
 * capacity" (runnable work queued behind busy lanes) is a different state
 * from "waiting on dependent results" (the procedure itself is bound), and
 * both are different from evaluating. Rendering only — never a liveness
 * authority; Cancel and Finish stay driven by the durable record.
 */
@Component({
  selector: 'app-waiting-badge',
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    @if (label(); as text) {
      <span class="waiting-badge" role="status">{{ text }}</span>
    }
  `,
  styles: `
    .waiting-badge {
      display: inline-block;
      padding: 0.1rem 0.5rem;
      border-radius: 999px;
      background: var(--surface-3, #eee);
      color: var(--text-muted, #666);
      font-size: 0.75rem;
      white-space: nowrap;
    }
  `,
})
export class WaitingBadgeComponent {
  private readonly jobs = inject(JobsService);

  /** The research job whose stream carries the waiting transitions. */
  readonly jobId = input<string | null>(null);

  protected readonly label = computed(() => {
    const id = this.jobId();
    if (!id) return null;
    const state = this.jobs.job(id);
    if (!state || state.status !== 'running') return null;
    if (state.waiting === 'capacity') return 'Waiting for machine capacity';
    if (state.waiting === 'adaptive') return 'Waiting on dependent results';
    return null;
  });
}
