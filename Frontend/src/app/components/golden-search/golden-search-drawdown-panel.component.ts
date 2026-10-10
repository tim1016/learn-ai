import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';

import { GoldenSearchChartComponent } from './charts/golden-search-chart.component';
import { GoldenSearchPanelComponent } from './charts/golden-search-panel.component';
import { sameChart } from './charts/golden-search-chart-spec';
import { drawdownSpec } from './golden-search-drawdown-charts';
import { percentText } from './golden-search-display';
import { shareText } from './golden-search-chance-charts';
import type { CandidateRef, ChanceDrawdown, MeasuredChanceDrawdown } from './golden-search.types';

/**
 * Compare's drawdown panel (#3013): the selected run's resampled session-
 * close fall range beside its recorded falls and the frozen ceiling — the
 * incumbent included, since the reading needs only its own source. Every
 * count, percentile, marker value, label and sentence is the server's; the
 * browser formats and draws. The engine's per-bar figure missing reads
 * "not recorded", never zero, and the panel offers no capital or sizing
 * advice.
 */
@Component({
  selector: 'app-golden-search-drawdown-panel',
  imports: [GoldenSearchChartComponent, GoldenSearchPanelComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './golden-search-drawdown-panel.component.html',
  styleUrl: './golden-search-drawdown-panel.component.scss',
})
export class GoldenSearchDrawdownPanelComponent {
  /** The run the panel explains: its key for colour and its label for words. */
  readonly candidate = input.required<CandidateRef>();
  /** The run's stored session-close drawdown reading. */
  readonly drawdown = input.required<ChanceDrawdown>();

  protected readonly measured = computed<MeasuredChanceDrawdown | null>(() => {
    const drawdown = this.drawdown();
    return drawdown.status === 'measured' ? drawdown : null;
  });

  protected readonly spec = computed(
    () => {
      const measured = this.measured();
      return measured === null ? null : drawdownSpec(this.candidate(), measured, this.candidate().key);
    },
    // Equal while it draws the same stored reading, so a study poll (new objects, same evidence) does not redraw it.
    { equal: sameChart },
  );

  /** What the panel says when no drawdown was drawn, narrowed here so the template only words it. */
  protected readonly note = computed<{ readonly kind: 'missing' | 'failed'; readonly text: string } | null>(() => {
    const drawdown = this.drawdown();
    switch (drawdown.status) {
      case 'missing':
        return { kind: 'missing', text: drawdown.reason };
      case 'failed':
        return { kind: 'failed', text: drawdown.sentence };
      default:
        return null;
    }
  });

  protected readonly engineText = computed(() => {
    const measured = this.measured();
    return measured === null ? '' : measured.recorded_engine_per_bar === null ? 'not recorded' : percentText(measured.recorded_engine_per_bar);
  });

  protected readonly beyondText = computed(() => {
    const measured = this.measured();
    return measured === null ? '' : `${measured.counts['beyond_ceiling'].toLocaleString('en-US')} of 10,000 (${shareText(measured.shares['beyond_ceiling'])})`;
  });

  protected readonly undefinedText = computed(() => {
    const measured = this.measured();
    return measured === null ? '' : `${measured.counts['undefined'].toLocaleString('en-US')} (${shareText(measured.shares['undefined'])})`;
  });
}
