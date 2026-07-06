import { CommonModule } from '@angular/common';
import {
  ChangeDetectionStrategy,
  Component,
  computed,
  effect,
  inject,
  input,
  signal,
} from '@angular/core';
import { Router } from '@angular/router';
import { ButtonModule } from 'primeng/button';
import { MessageModule } from 'primeng/message';
import { TableModule } from 'primeng/table';

import type { RunLedger } from '../../../../../services/strategy-runs.types';
import { WalkForwardService } from '../../../../../services/walk-forward.service';
import type { WalkForwardConfig } from '../../../../../services/walk-forward.types';
import { TimestampDisplayPipe } from '../../../../../shared/timestamp';

/**
 * Embedded section on the run-detail page showing walk-forward
 * analyses derived from the parent run, plus a "Run rolling
 * walk-forward" button.
 *
 * Loads the listing scoped to ``parent_run_id = run.run_id`` so a
 * detail page only sees its own derived WFs. The button posts a
 * rolling 60/30/30 split via ``WalkForwardService.runFromRun`` —
 * custom split policies are deferred to a spec-form component.
 */
@Component({
  selector: 'app-walk-forward-section',
  imports: [
    CommonModule,
    ButtonModule,
    MessageModule,
    TableModule,
    TimestampDisplayPipe,
  ],
  templateUrl: './walk-forward-section.component.html',
  styleUrls: ['./walk-forward-section.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class WalkForwardSectionComponent {
  private readonly service = inject(WalkForwardService);
  private readonly router = inject(Router);

  readonly run = input.required<RunLedger>();

  readonly walkForwards = signal<WalkForwardConfig[]>([]);
  readonly loading = signal(false);
  readonly running = signal(false);
  readonly error = signal<string | null>(null);
  readonly lastWfId = signal<string | null>(null);

  readonly hasWalkForwards = computed(() => this.walkForwards().length > 0);

  constructor() {
    // ``input.required`` doesn't fire ngOnInit reliably for downstream
    // logic; an effect that reads ``this.run()`` is the idiomatic way
    // to react to input arrival in zoneless components.
    effect(() => {
      const r = this.run();
      if (r) {
        void this.refresh();
      }
    });
  }

  async refresh(): Promise<void> {
    this.loading.set(true);
    this.error.set(null);
    try {
      const response = await this.service.listWalkForwards({
        parent_run_id: this.run().run_id,
        limit: 50,
      });
      this.walkForwards.set(response.walk_forwards);
    } catch (err) {
      this.error.set(this.formatError(err));
    } finally {
      this.loading.set(false);
    }
  }

  async runRollingWalkForward(): Promise<void> {
    this.running.set(true);
    this.error.set(null);
    try {
      const { config } = await this.service.runFromRun(this.run());
      this.lastWfId.set(config.walk_forward_id);
      await this.refresh();
    } catch (err) {
      this.error.set(this.formatError(err));
    } finally {
      this.running.set(false);
    }
  }

  open(wf: WalkForwardConfig): void {
    void this.router.navigate(['/research-lab/walk-forward', wf.walk_forward_id]);
  }

  openById(wfId: string): void {
    void this.router.navigate(['/research-lab/walk-forward', wfId]);
  }

  splitSummary(policy: WalkForwardConfig['split_policy']): string {
    switch (policy.kind) {
      case 'chronological':
        return `chronological · train ${formatPct(policy['train_pct'])}`;
      case 'rolling':
        return `rolling · ${policy['train_days']}d train / ${policy['test_days']}d test / ${policy['step_days']}d step`;
      case 'anchored':
        return `anchored · ${policy['initial_train_days']}d initial / ${policy['test_days']}d test / ${policy['step_days']}d step`;
      default:
        return policy.kind;
    }
  }

  shortHash(value: string | null | undefined, len = 12): string {
    if (!value) return '—';
    return value.slice(0, len);
  }

  trackByWfId(_i: number, wf: WalkForwardConfig): string {
    return wf.walk_forward_id;
  }

  private formatError(err: unknown): string {
    if (err instanceof Error) return err.message;
    if (typeof err === 'object' && err !== null && 'message' in err) {
      return String((err as { message: unknown }).message);
    }
    return 'Unknown error';
  }
}

function formatPct(value: unknown): string {
  if (typeof value !== 'number' || !Number.isFinite(value)) return '?';
  return `${(value * 100).toFixed(0)}%`;
}
