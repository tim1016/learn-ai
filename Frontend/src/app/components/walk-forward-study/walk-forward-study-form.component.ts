import { ChangeDetectionStrategy, Component, computed, DestroyRef, inject, input, output, signal } from '@angular/core';
import { DecimalPipe } from '@angular/common';
import { ButtonModule } from 'primeng/button';
import { InputText } from 'primeng/inputtext';

import { ReceiptLabelPipe } from '../../shared/pipes/receipt-label.pipe';
import { TimestampDisplayComponent } from '../../shared/timestamp';
import { GridSearchSpecEditorComponent, type GridSpecEdit } from '../grid-search/grid-search-spec-editor.component';
import { GridSearchRefusedError } from '../grid-search/grid-search.service';
import type { GridSearchRefusal, GridSearchSpecRequest } from '../grid-search/grid-search.types';
import type { StrategyInfo } from '../strategy-lab/strategy-lab.models';
import { WalkForwardStudyService } from './walk-forward-study.service';
import type { WalkForwardStudyPreflight, WalkForwardStudySpecRequest } from './walk-forward-study.types';

export interface WalkForwardStudyLaunch {
  readonly jobId: string;
}

/**
 * Walk-Forward Study setup (PRD #1925 "Frontend"): the shared grid-search
 * spec editor supplies the grid, window, costs and ranking; this form adds
 * the two window lengths in whole months and preflights the study — fold
 * count, the fold calendar, the workload against the shared limit — before
 * launch. A preflight answer for an edit that is no longer current is ignored.
 */
@Component({
  selector: 'app-walk-forward-study-form',
  imports: [ButtonModule, InputText, DecimalPipe, GridSearchSpecEditorComponent, ReceiptLabelPipe, TimestampDisplayComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './walk-forward-study-form.component.html',
  styleUrl: './walk-forward-study-form.component.scss',
})
export class WalkForwardStudyFormComponent {
  private readonly service = inject(WalkForwardStudyService);
  private readonly destroyRef = inject(DestroyRef);

  readonly strategies = input.required<readonly StrategyInfo[]>();
  /** A grid search's stored request to start from (the "test this grid out of sample" hand-off). */
  readonly prefill = input<GridSearchSpecRequest | null>(null);
  /** Debounce between an edit and the server preflight; tests set 0. */
  readonly preflightDebounceMs = input(300);
  readonly launched = output<WalkForwardStudyLaunch>();

  readonly trainingMonths = signal(12);
  readonly testMonths = signal(3);
  readonly gridSpec = signal<GridSearchSpecRequest | null>(null);
  readonly preflight = signal<WalkForwardStudyPreflight | null>(null);
  readonly refusal = signal<GridSearchRefusal | null>(null);
  readonly preflightError = signal<string | null>(null);
  readonly checking = signal(false);
  readonly launching = signal(false);

  protected readonly canLaunch = computed(() => this.preflight() !== null && this.refusal() === null && !this.launching() && !this.checking());

  private generation = 0;
  private monthsDebounce: ReturnType<typeof setTimeout> | null = null;

  constructor() {
    this.destroyRef.onDestroy(() => {
      if (this.monthsDebounce !== null) clearTimeout(this.monthsDebounce);
    });
  }

  /** The exact wire body the server preflights and launches. */
  spec(): WalkForwardStudySpecRequest | null {
    const grid = this.gridSpec();
    return grid === null ? null : { ...grid, training_months: this.trainingMonths(), test_months: this.testMonths() };
  }

  onGridEdit(edit: GridSpecEdit): void {
    this.gridSpec.set(edit.spec);
    this.invalidate(edit.problem);
    if (edit.spec !== null) void this.refreshPreflight();
  }

  setMonths(which: 'training' | 'test', raw: string): void {
    const value = Number(raw);
    if (!Number.isInteger(value) || value < 1) {
      // Nothing to preflight: the shown plan no longer describes the form, so Launch waits.
      this.invalidate('Training and test lengths are whole months of at least 1.');
      return;
    }
    (which === 'training' ? this.trainingMonths : this.testMonths).set(value);
    this.invalidate(null);
    if (this.monthsDebounce !== null) clearTimeout(this.monthsDebounce);
    this.monthsDebounce = setTimeout(() => void this.refreshPreflight(), this.preflightDebounceMs());
  }

  async refreshPreflight(): Promise<void> {
    const spec = this.spec();
    if (spec === null) return;
    const generation = this.generation;
    this.checking.set(true);
    try {
      const plan = await this.service.preflight(spec);
      if (generation !== this.generation) return;
      this.preflight.set(plan);
      this.refusal.set(null);
      this.preflightError.set(null);
    } catch (error) {
      if (generation !== this.generation) return;
      if (error instanceof GridSearchRefusedError) {
        this.refusal.set(error.refusal);
        this.preflightError.set(null);
      } else {
        this.refusal.set(null);
        this.preflightError.set('The preflight could not be completed. Check the service and try again.');
      }
    } finally {
      if (generation === this.generation) this.checking.set(false);
    }
  }

  async launch(): Promise<void> {
    const spec = this.spec();
    if (spec === null || !this.canLaunch()) return;
    this.launching.set(true);
    try {
      const jobId = await this.service.launch(spec);
      this.launched.emit({ jobId });
    } catch (error) {
      if (error instanceof GridSearchRefusedError) this.refusal.set(error.refusal);
      else this.preflightError.set('The launch was not accepted. Check the service and try again.');
    } finally {
      this.launching.set(false);
    }
  }

  protected workloadSentence(plan: WalkForwardStudyPreflight): string {
    return `${plan.combinations} combinations × ${plan.fold_count} folds × 2 windows = ${plan.total_backtests} backtests (limit ${plan.backtest_limit})`;
  }

  /** An edit invalidates whatever preflight is showing or in flight; Launch waits for the next answer. */
  private invalidate(problem: string | null): void {
    this.generation += 1;
    this.preflight.set(null);
    this.preflightError.set(problem);
  }
}
