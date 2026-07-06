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
import { TagModule } from 'primeng/tag';

import { BaselinesService } from '../../../../../services/baselines.service';
import type { BaselineConfig } from '../../../../../services/baselines.types';
import type { RunLedger } from '../../../../../services/strategy-runs.types';
import { TimestampDisplayPipe } from '../../../../../shared/timestamp';

/**
 * Embedded section on the run-detail page showing null-baseline
 * analyses derived from the parent run, plus two buttons:
 *
 *   * "Run buy-and-hold baseline" — single deterministic spec on
 *     this run's symbol/window. ``sample_count=1``.
 *   * "Run random EMA windows baseline (30 samples)" — 30 random
 *     ``(fast, slow)`` EMA pairs from the workbench-default family
 *     (``fast ∈ [3, 12]``, ``slow ∈ [10, 30]``).
 *
 * Custom parameters (sample count, seed, custom EMA ranges) are
 * deferred to a future spec-form component.
 */
@Component({
  selector: 'app-baselines-section',
  imports: [
    CommonModule,
    ButtonModule,
    MessageModule,
    TableModule,
    TagModule,
    TimestampDisplayPipe,
  ],
  templateUrl: './baselines-section.component.html',
  styleUrls: ['./baselines-section.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class BaselinesSectionComponent {
  private readonly service = inject(BaselinesService);
  private readonly router = inject(Router);

  readonly run = input.required<RunLedger>();

  readonly baselines = signal<BaselineConfig[]>([]);
  readonly loading = signal(false);
  readonly running = signal<'buy_and_hold' | 'random_ema_windows' | null>(null);
  readonly error = signal<string | null>(null);
  readonly lastBaselineId = signal<string | null>(null);

  readonly hasBaselines = computed(() => this.baselines().length > 0);
  readonly anyRunning = computed(() => this.running() !== null);

  constructor() {
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
      const response = await this.service.listBaselines({
        parent_run_id: this.run().run_id,
        limit: 50,
      });
      this.baselines.set(response.baselines);
    } catch (err) {
      this.error.set(this.formatError(err));
    } finally {
      this.loading.set(false);
    }
  }

  async runBuyAndHold(): Promise<void> {
    await this.runWithMethod('buy_and_hold');
  }

  async runRandomEmaWindows(): Promise<void> {
    await this.runWithMethod('random_ema_windows');
  }

  private async runWithMethod(
    method: 'buy_and_hold' | 'random_ema_windows',
  ): Promise<void> {
    this.running.set(method);
    this.error.set(null);
    try {
      const { config } = await this.service.runFromRun(this.run(), method);
      this.lastBaselineId.set(config.baseline_id);
      await this.refresh();
    } catch (err) {
      this.error.set(this.formatError(err));
    } finally {
      this.running.set(null);
    }
  }

  open(baseline: BaselineConfig): void {
    void this.router.navigate(['/research-lab/baselines', baseline.baseline_id]);
  }

  openById(baselineId: string): void {
    void this.router.navigate(['/research-lab/baselines', baselineId]);
  }

  shortHash(value: string | null | undefined, len = 12): string {
    if (!value) return '—';
    return value.slice(0, len);
  }

  trackByBaselineId(_i: number, b: BaselineConfig): string {
    return b.baseline_id;
  }

  private formatError(err: unknown): string {
    if (err instanceof Error) return err.message;
    if (typeof err === 'object' && err !== null && 'message' in err) {
      return String((err as { message: unknown }).message);
    }
    return 'Unknown error';
  }
}
