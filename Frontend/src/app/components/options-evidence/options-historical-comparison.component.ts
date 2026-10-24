import { ChangeDetectionStrategy, Component, computed, input, signal } from '@angular/core';
import { form, FormField } from '@angular/forms/signals';
import { RouterLink } from '@angular/router';

import { CompareViewComponent } from '../options-backtest/run-detail/compare/compare-view.component';

@Component({
  selector: 'app-options-historical-comparison',
  imports: [RouterLink, CompareViewComponent, FormField],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './options-historical-comparison.component.html',
  styleUrl: './options-evidence-view.component.scss',
})
export class OptionsHistoricalComparisonComponent {
  readonly sourceRunId = input.required<number>();
  readonly comparator = input<string>();
  readonly route = input.required<readonly string[]>();
  /** What the page's address says besides the view, kept on the link that records a comparison. */
  readonly context = input.required<Readonly<Record<string, string | null | undefined>>>();
  protected readonly selection = signal({ runId: '' });
  protected readonly fields = form(this.selection);
  protected readonly comparatorId = computed(() => this.parse(this.comparator()));
  protected readonly requestedId = computed(() => this.parse(this.selection().runId));
  protected readonly query = computed(() => ({ ...this.context(), comparator_run_id: this.requestedId(), view: 'comparison' }));
  private parse(value: string | undefined): number | null {
    if (value === undefined || !/^[1-9][0-9]*$/.test(value)) return null;
    const number = Number(value);
    return Number.isSafeInteger(number) && number !== this.sourceRunId() ? number : null;
  }
}
