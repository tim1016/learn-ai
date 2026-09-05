import { ChangeDetectionStrategy, Component, computed, inject, input, output, signal } from '@angular/core';
import { ButtonModule } from 'primeng/button';
import { InputText } from 'primeng/inputtext';

import { ReceiptLabelPipe } from '../../shared/pipes/receipt-label.pipe';
import { TimestampDisplayComponent } from '../../shared/timestamp';
import type { StrategyInfo } from '../strategy-lab/strategy-lab.models';
import { WalkForwardStudyService } from './walk-forward-study.service';
import type { WalkForwardStudyHistoryFilters, WalkForwardStudyStatus, WalkForwardStudySummary } from './walk-forward-study.types';

const STATUSES: readonly WalkForwardStudyStatus[] = ['queued', 'running', 'completed', 'failed', 'cancelled', 'interrupted'];

/**
 * Walk-Forward Study history (PRD #1925): every study, newest first,
 * filterable by strategy, symbol and status, each row carrying its verdict
 * and fold coverage, with per-row delete behind a confirmation.
 */
@Component({
  selector: 'app-walk-forward-study-history',
  imports: [ButtonModule, InputText, ReceiptLabelPipe, TimestampDisplayComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './walk-forward-study-history.component.html',
  styleUrl: './walk-forward-study-history.component.scss',
})
export class WalkForwardStudyHistoryComponent {
  private readonly service = inject(WalkForwardStudyService);

  readonly strategies = input<readonly StrategyInfo[]>([]);
  readonly opened = output<string>();

  readonly rows = signal<WalkForwardStudySummary[]>([]);
  readonly loading = signal(false);
  readonly error = signal<string | null>(null);
  readonly filters = signal<WalkForwardStudyHistoryFilters>({});
  readonly confirming = signal<string | null>(null);

  protected readonly statuses = STATUSES;
  protected readonly displayNames = computed(() => new Map(this.strategies().map((s) => [s.name, s.display_name])));
  protected readonly filterableStrategies = computed(() => this.strategies().filter((s) => s.sweep_eligibility?.eligible === true));

  constructor() {
    void this.refresh();
  }

  async refresh(): Promise<void> {
    this.loading.set(true);
    try {
      this.rows.set(await this.service.list(this.filters()));
      this.error.set(null);
    } catch {
      this.error.set('History could not be loaded.');
    } finally {
      this.loading.set(false);
    }
  }

  onFilterEvent(key: keyof WalkForwardStudyHistoryFilters, event: Event): void {
    const target = event.target;
    if (target instanceof HTMLSelectElement || target instanceof HTMLInputElement) this.setFilter(key, target.value);
  }

  setFilter(key: keyof WalkForwardStudyHistoryFilters, raw: string): void {
    this.filters.update((current) => {
      const kept = Object.fromEntries(Object.entries(current).filter(([name]) => name !== key)) as WalkForwardStudyHistoryFilters;
      return raw ? { ...kept, [key]: raw } : kept;
    });
    void this.refresh();
  }

  displayName(strategyKey: string): string | null {
    return this.displayNames().get(strategyKey) ?? null;
  }

  requestDelete(id: string): void {
    this.confirming.set(id);
  }

  cancelDelete(): void {
    this.confirming.set(null);
  }

  async confirmDelete(id: string): Promise<void> {
    try {
      await this.service.delete(id);
      this.rows.update((rows) => rows.filter((row) => row.id !== id));
    } catch {
      this.error.set('The study could not be deleted. If it is running, try again once cancellation is acknowledged.');
    } finally {
      this.confirming.set(null);
    }
  }
}
