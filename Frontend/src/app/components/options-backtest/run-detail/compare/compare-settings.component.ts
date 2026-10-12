import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';

import { TimestampDisplayComponent } from '../../../../shared/timestamp';
import type { RunComparison } from '../run-detail.types';

/**
 * What differs between two runs, before any figure: each setting that differs as the server worded it (the setting in
 * words, and the value in run A and in run B), then the dates when the two ranges differ. When nothing differs it says so.
 * Fees are a setting like the others, so a difference in them is a row here, in the server's sentences.
 */
@Component({
  selector: 'app-options-backtest-compare-settings',
  imports: [TimestampDisplayComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './compare-settings.component.html',
  styleUrl: './compare-tables.scss',
})
export class CompareSettingsComponent {
  readonly comparison = input.required<RunComparison>();

  protected readonly settings = computed(() => this.comparison().setting_differences);
  protected readonly range = computed(() => this.comparison().range_difference ?? null);
  protected readonly nothing = computed(() => this.settings().length === 0 && this.range() === null);
}
