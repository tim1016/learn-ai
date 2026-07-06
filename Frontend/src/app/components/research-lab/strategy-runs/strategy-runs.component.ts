import { CommonModule, DecimalPipe } from '@angular/common';
import {
  ChangeDetectionStrategy,
  Component,
  computed,
  inject,
  signal,
} from '@angular/core';
import { Router } from '@angular/router';
import { ButtonModule } from 'primeng/button';
import { MessageModule } from 'primeng/message';
import { TableModule } from 'primeng/table';
import { TagModule } from 'primeng/tag';

import { StrategyRunsService } from '../../../services/strategy-runs.service';
import type { RunLedger } from '../../../services/strategy-runs.types';
import { TimestampDisplayPipe } from '../../../shared/timestamp';

/**
 * Run-ledger list view (Phase B of the build-alpha-style research
 * pipeline). Talks to ``GET /api/research/strategy-runs`` directly via
 * ``StrategyRunsService``; no GraphQL passthrough yet (see
 * ``docs/references/run-ledger.md`` for the deferral rationale).
 *
 * Columns are intentionally limited to ``RunLedger`` fields — the
 * listing endpoint returns ledgers, not full results, so metric
 * columns (return %, drawdown, win rate) live on the detail page only.
 * Clicking a row navigates to ``/research-lab/strategy-runs/<run_id>``,
 * which fetches the full ``BacktestRunResult``.
 *
 * The ``Run SPY EMA fixture`` button POSTs the canonical fixture spec
 * verbatim — exercises the full reproducibility contract end-to-end
 * without requiring a curl command, and lets the workbench be useful
 * before a spec-editor UI lands.
 */
@Component({
  selector: 'app-strategy-runs',
  imports: [
    CommonModule,
    ButtonModule,
    MessageModule,
    TableModule,
    TagModule,
    DecimalPipe,
    TimestampDisplayPipe,
  ],
  templateUrl: './strategy-runs.component.html',
  styleUrls: ['./strategy-runs.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class StrategyRunsComponent {
  private readonly service = inject(StrategyRunsService);
  private readonly router = inject(Router);

  readonly runs = signal<RunLedger[]>([]);
  readonly loading = signal(false);
  readonly running = signal(false);
  readonly error = signal<string | null>(null);
  readonly lastFixtureRunId = signal<string | null>(null);

  readonly hasRuns = computed(() => this.runs().length > 0);

  constructor() {
    void this.refresh();
  }

  async refresh(): Promise<void> {
    this.loading.set(true);
    this.error.set(null);
    try {
      const response = await this.service.listRuns({ limit: 100 });
      this.runs.set(response.runs);
    } catch (err) {
      this.error.set(this.formatError(err));
    } finally {
      this.loading.set(false);
    }
  }

  async runFixture(): Promise<void> {
    this.running.set(true);
    this.error.set(null);
    try {
      const { ledger } = await this.service.runSpyEmaFixture();
      this.lastFixtureRunId.set(ledger.run_id);
      await this.refresh();
    } catch (err) {
      this.error.set(this.formatError(err));
    } finally {
      this.running.set(false);
    }
  }

  open(run: RunLedger): void {
    this.openById(run.run_id);
  }

  /** Navigate by raw run_id — used by the "fixture run completed" flash link. */
  openById(runId: string): void {
    void this.router.navigate(['/research-lab/strategy-runs', runId]);
  }

  /**
   * PrimeNG ``p-tag`` severity for a run status. Maps status →
   * one of the design-system severity tokens.
   */
  statusSeverity(status: RunLedger['status']): 'success' | 'warn' | 'danger' | 'info' {
    switch (status) {
      case 'completed':
        return 'success';
      case 'running':
        return 'info';
      case 'failed':
        return 'danger';
      default:
        return 'warn';
    }
  }

  shortHash(value: string | null | undefined, len = 8): string {
    if (!value) return '—';
    return value.slice(0, len);
  }

  trackByRunId(_index: number, run: RunLedger): string {
    return run.run_id;
  }

  private formatError(err: unknown): string {
    if (err instanceof Error) return err.message;
    if (typeof err === 'object' && err !== null && 'message' in err) {
      return String((err as { message: unknown }).message);
    }
    return 'Unknown error';
  }
}
