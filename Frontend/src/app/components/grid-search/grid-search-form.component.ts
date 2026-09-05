import { ChangeDetectionStrategy, Component, computed, inject, input, output, signal } from '@angular/core';
import { DecimalPipe } from '@angular/common';
import { ButtonModule } from 'primeng/button';

import { ReceiptLabelPipe } from '../../shared/pipes/receipt-label.pipe';
import { TimestampDisplayComponent } from '../../shared/timestamp';
import type { StrategyInfo } from '../strategy-lab/strategy-lab.models';
import { GridSearchSpecEditorComponent, type GridSpecEdit } from './grid-search-spec-editor.component';
import { GridSearchRefusedError, GridSearchService } from './grid-search.service';
import type { GridSearchPreflight, GridSearchRefusal, GridSearchSpecRequest } from './grid-search.types';

export interface GridSearchLaunch {
  readonly jobId: string;
}

/**
 * Grid Search setup (PRD #1926 "Frontend"): the shared spec editor plus a
 * live server preflight — count, limit, labelled estimate, run-up plan —
 * before launch. A preflight answer for an edit that is no longer current
 * is ignored, so Launch always describes what the form shows.
 */
@Component({
  selector: 'app-grid-search-form',
  imports: [ButtonModule, DecimalPipe, GridSearchSpecEditorComponent, ReceiptLabelPipe, TimestampDisplayComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './grid-search-form.component.html',
  styleUrl: './grid-search-form.component.scss',
})
export class GridSearchFormComponent {
  private readonly service = inject(GridSearchService);

  readonly strategies = input.required<readonly StrategyInfo[]>();
  /** Debounce between an edit and the server preflight; tests set 0. */
  readonly preflightDebounceMs = input(300);
  readonly launched = output<GridSearchLaunch>();

  readonly spec = signal<GridSearchSpecRequest | null>(null);
  readonly preflight = signal<GridSearchPreflight | null>(null);
  readonly refusal = signal<GridSearchRefusal | null>(null);
  readonly preflightError = signal<string | null>(null);
  readonly checking = signal(false);
  readonly launching = signal(false);

  protected readonly canLaunch = computed(() => this.preflight() !== null && this.refusal() === null && !this.launching() && !this.checking());

  /** Generation of the latest edit; a preflight that returns for an older generation is ignored. */
  private generation = 0;

  /** An edit is in progress: whatever was preflighted no longer describes the form, so Launch waits. */
  onEditing(): void {
    this.generation += 1;
    this.preflight.set(null);
    this.checking.set(true);
  }

  onEdit(edit: GridSpecEdit): void {
    this.spec.set(edit.spec);
    this.generation += 1;
    this.preflight.set(null);
    this.preflightError.set(edit.problem);
    this.checking.set(false);
    if (edit.spec !== null) void this.refreshPreflight(edit.spec);
  }

  async refreshPreflight(spec: GridSearchSpecRequest): Promise<void> {
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

  protected runUpSentence(plan: GridSearchPreflight): string {
    const sessions = plan.run_up.run_up_sessions;
    const days = `${sessions} trading day${sessions === 1 ? '' : 's'}`;
    return plan.run_up.carved_from_range
      ? `Slowest setting needs ${plan.run_up.required_samples} bars → run-up uses the first ${days} of your range.`
      : `Slowest setting needs ${plan.run_up.required_samples} bars → primed from ${days} the lake already holds before your start.`;
  }
}
