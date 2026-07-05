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

import { MonteCarloService } from '../../../../../services/monte-carlo.service';
import type { MonteCarloConfig } from '../../../../../services/monte-carlo.types';
import type { RunLedger } from '../../../../../services/strategy-runs.types';
import { TimestampDisplayPipe } from '../../../../../shared/timestamp';

/**
 * Embedded section on the run-detail page showing Monte Carlo
 * analyses derived from the parent run, plus a "Run reshuffle Monte
 * Carlo (1000 sims)" button.
 *
 * Loads the listing scoped to ``parent_run_id = run.run_id`` so each
 * detail page shows only its own derived MCs. The button posts a
 * 1000-simulation reshuffle via ``MonteCarloService.runReshuffleFromRun``
 * — custom methods (``resample``) and parameters (sim count, breach
 * thresholds, projection horizon) are deferred to a future
 * spec-form component.
 */
@Component({
  selector: 'app-monte-carlo-section',
  imports: [
    CommonModule,
    ButtonModule,
    MessageModule,
    TableModule,
    TagModule,
    TimestampDisplayPipe,
  ],
  templateUrl: './monte-carlo-section.component.html',
  styleUrls: ['./monte-carlo-section.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class MonteCarloSectionComponent {
  private readonly service = inject(MonteCarloService);
  private readonly router = inject(Router);

  readonly run = input.required<RunLedger>();

  readonly monteCarlos = signal<MonteCarloConfig[]>([]);
  readonly loading = signal(false);
  readonly running = signal(false);
  readonly error = signal<string | null>(null);
  readonly lastMcId = signal<string | null>(null);

  readonly hasMonteCarlos = computed(() => this.monteCarlos().length > 0);

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
      const response = await this.service.listMonteCarlos({
        parent_run_id: this.run().run_id,
        limit: 50,
      });
      this.monteCarlos.set(response.monte_carlos);
    } catch (err) {
      this.error.set(this.formatError(err));
    } finally {
      this.loading.set(false);
    }
  }

  async runReshuffle(): Promise<void> {
    this.running.set(true);
    this.error.set(null);
    try {
      const { config } = await this.service.runReshuffleFromRun(this.run());
      this.lastMcId.set(config.monte_carlo_id);
      await this.refresh();
    } catch (err) {
      this.error.set(this.formatError(err));
    } finally {
      this.running.set(false);
    }
  }

  open(mc: MonteCarloConfig): void {
    void this.router.navigate(['/research-lab/monte-carlo', mc.monte_carlo_id]);
  }

  openById(mcId: string): void {
    void this.router.navigate(['/research-lab/monte-carlo', mcId]);
  }

  shortHash(value: string | null | undefined, len = 12): string {
    if (!value) return '—';
    return value.slice(0, len);
  }

  trackByMcId(_i: number, mc: MonteCarloConfig): string {
    return mc.monte_carlo_id;
  }

  private formatError(err: unknown): string {
    if (err instanceof Error) return err.message;
    if (typeof err === 'object' && err !== null && 'message' in err) {
      return String((err as { message: unknown }).message);
    }
    return 'Unknown error';
  }
}
