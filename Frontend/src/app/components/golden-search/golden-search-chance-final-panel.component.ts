import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';

import { GoldenSearchChartComponent } from './charts/golden-search-chart.component';
import { GoldenSearchPanelComponent } from './charts/golden-search-panel.component';
import { sameChart } from './charts/golden-search-chart-spec';
import { carryOverSpec, finalIntervalsSpec, fitToFitText } from './golden-search-chance-final-charts';
import type { CandidateRef, ExamChance, FinalChance, MeasuredChanceCarryOver, MeasuredChanceComparison } from './golden-search.types';

/**
 * The decision step's final-uncertainty panel (#3014): the selected fit's
 * resampling support beside its frozen outcome — the middle-90% difference
 * band and the fit's own middle-90% result band against break-even, the
 * reproduced 1× rehearsal's percentile curve with the recorded final result
 * marked at its actual value, and the optional fit-to-fit comparison when
 * two fits ran. Every count, share, range, rank and sentence is the
 * server's; the browser formats and draws. The frozen "meets the rules"
 * verdict stays visually separate, the rank never truncates to the plotted
 * range, and the chosen-after-the-test caveat stays visible.
 */
@Component({
  selector: 'app-golden-search-chance-final-panel',
  imports: [GoldenSearchChartComponent, GoldenSearchPanelComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './golden-search-chance-final-panel.component.html',
  styleUrl: './golden-search-chance-final-panel.component.scss',
})
export class GoldenSearchChanceFinalPanelComponent {
  /** The fit the page is about. */
  readonly candidate = input.required<CandidateRef>();
  /** The fit's stored final chance components. */
  readonly chance = input.required<FinalChance>();
  /** The exam-level chance record: the optional fit-to-fit comparison. */
  readonly examChance = input.required<ExamChance>();

  protected readonly comparison = computed<MeasuredChanceComparison | null>(() => {
    const comparison = this.chance().comparison;
    return comparison.status === 'measured' ? comparison : null;
  });

  protected readonly carry = computed<MeasuredChanceCarryOver | null>(() => {
    const carry = this.chance().carry_over;
    return carry.status === 'measured' ? carry : null;
  });

  protected readonly intervalsSpec = computed(
    () => {
      const comparison = this.comparison();
      return comparison === null ? null : finalIntervalsSpec(this.candidate(), comparison, this.candidate().key);
    },
    // Equal while it draws the same stored reading, so a study poll (new objects, same evidence) does not redraw it.
    { equal: sameChart },
  );

  protected readonly carrySpec = computed(
    () => {
      const carry = this.carry();
      return carry === null ? null : carryOverSpec(this.candidate(), carry, this.candidate().key);
    },
    { equal: sameChart },
  );

  /** The profit line's words when profitability was measured, as Python recorded them. */
  protected readonly moneyText = computed(() => {
    const profitability = this.chance().profitability;
    return profitability.status === 'measured' ? profitability.sentence : null;
  });

  /** The fit-to-fit sentence, in Python's words, when two distinct fits ran. */
  protected readonly fitToFitSentence = computed(() => {
    const fit_to_fit = this.examChance().fit_to_fit;
    return fit_to_fit.status === 'measured' ? fitToFitText(fit_to_fit) : null;
  });
}
