import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';

import { GoldenSearchChartComponent } from './charts/golden-search-chart.component';
import { GoldenSearchPanelComponent } from './charts/golden-search-panel.component';
import { sameChart } from './charts/golden-search-chart-spec';
import { chanceComparisonSpec, countText, shareStripText, shareSegments, shareText } from './golden-search-chance-charts';
import type { CandidateRef, ChanceReadings, MeasuredChanceComparison, MeasuredChanceProfitability } from './golden-search.types';

/**
 * Compare's development-uncertainty panel (#3011): the selected fit's paired
 * resampling support against the current settings — the middle-90% difference
 * band with its median and zero reference, the better/tied/worse/undefined
 * share strip, the stored sentence and the selection-bias caveat, plus the
 * fit's own resampled profitability line. Every number, label and sentence is
 * the server's stored reading; a component that is missing, not measured,
 * unsupported or failed says so in the recorded words, never zero.
 */
@Component({
  selector: 'app-golden-search-chance-panel',
  imports: [GoldenSearchChartComponent, GoldenSearchPanelComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './golden-search-chance-panel.component.html',
  styleUrl: './golden-search-chance-panel.component.scss',
})
export class GoldenSearchChancePanelComponent {
  /** The fit the panel explains: its key for colour and its label for words. */
  readonly candidate = input.required<CandidateRef>();
  /** The fit's stored development chance readings. */
  readonly chance = input.required<ChanceReadings>();
  /** Names simultaneous candidate panels, keeping their landmarks distinct. */
  readonly instance = input<string | null>(null);

  protected readonly measured = computed<MeasuredChanceComparison | null>(() => {
    const comparison = this.chance().comparison;
    return comparison.status === 'measured' ? comparison : null;
  });

  protected readonly profitability = computed<MeasuredChanceProfitability | null>(() => {
    const reading = this.chance().profitability;
    return reading.status === 'measured' ? reading : null;
  });

  protected readonly spec = computed(
    () => {
      const measured = this.measured();
      return measured === null ? null : chanceComparisonSpec(this.candidate(), measured, this.candidate().key);
    },
    // Equal while it draws the same stored reading, so a study poll (new objects, same evidence) does not redraw it.
    { equal: sameChart },
  );

  protected readonly segments = computed(() => {
    const measured = this.measured();
    return measured === null ? [] : shareSegments(measured);
  });

  protected readonly stripText = computed(() => {
    const measured = this.measured();
    return measured === null ? '' : shareStripText(measured);
  });

  /** The profit line's words when profitability was not measured, as Python recorded them. */
  protected readonly moneyMissing = computed(() => {
    const reading = this.chance().profitability;
    return reading.status === 'missing' ? reading.reason : null;
  });

  /** The profit line's diagnostic when the profitability reading itself failed, in Python's words. */
  protected readonly moneyFailed = computed(() => {
    const reading = this.chance().profitability;
    return reading.status === 'failed' ? reading.sentence : null;
  });

  /** What the panel says when no comparison was drawn, narrowed here so the template only words it. */
  protected readonly note = computed<{ readonly kind: 'sentence' | 'missing' | 'failed'; readonly text: string } | null>(() => {
    const comparison = this.chance().comparison;
    switch (comparison.status) {
      case 'same_settings':
      case 'baseline':
        return { kind: 'sentence', text: comparison.sentence };
      case 'missing':
        return { kind: 'missing', text: comparison.reason };
      case 'failed':
        return { kind: 'failed', text: comparison.sentence };
      default:
        return null;
    }
  });

  protected readonly count = (value: number): string => countText(value);
  protected readonly share = (value: number): string => shareText(value);
}
