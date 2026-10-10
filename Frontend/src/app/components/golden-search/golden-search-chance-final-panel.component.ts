import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';

import { GoldenSearchChartComponent } from './charts/golden-search-chart.component';
import { GoldenSearchPanelComponent } from './charts/golden-search-panel.component';
import { sameChart } from './charts/golden-search-chart-spec';
import { carryOverSpec, finalIntervalsSpec, finalProfitSpec, fitToFitText, type ProfitabilityWithRange } from './golden-search-chance-final-charts';
import type { CandidateRef, ChanceComparison, ChanceProfitability, FinalChance, MeasuredChanceCarryOver, MeasuredChanceComparison } from './golden-search.types';

/** Any per-component stored reading: measured, named-missing, or failed with its own sentence. */
type ChanceReading = FinalChance['carry_over'];

/** How a reading the server could not measure is shown: its own words, never an invented sentence. */
interface UnmeasuredNote {
  kind: 'failed' | 'missing' | 'recorded';
  text: string;
}

/**
 * The decision step's final-uncertainty panel (#3014, #3026): the selected fit's
 * resampling support beside its frozen outcome — the middle-90% difference band
 * and the fit's own objective band in the objective's units, the independent
 * compounded-net-return band against actual break-even in its own chart and
 * units, the reproduced 1× rehearsal's percentile curve with the recorded final
 * result marked at its actual value, and the optional fit-to-fit comparison
 * when two fits ran. Every count, share, range, rank and sentence is the
 * server's; the browser formats and draws. The profit readings stand on their
 * own — a missing or failed incumbent comparison never hides measured profit,
 * and a missing or failed profit reading shows its own reason beside a valid
 * comparison — and the money-support row itself exists only when the frozen
 * plan requires a positive net. A component the server could not measure says
 * why in its own words, a failed one raises its sentence as an alert — neither
 * renders as absence. The frozen "meets the rules" verdict stays visually
 * separate, the rank never truncates to the plotted range, and the
 * chosen-after-the-test caveat stays visible.
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
  /** The fit's stored final chance components, its own fit-to-fit direction included (#3029). */
  readonly chance = input.required<FinalChance>();
  /** The frozen plan's positive-net rule, read from the study — never inferred from a metric (#3026). */
  readonly positiveNetRequired = input.required<boolean>();

  protected readonly comparisonMeasured = computed<MeasuredChanceComparison | null>(() => {
    const comparison = this.chance().comparison;
    return comparison.status === 'measured' ? comparison : null;
  });
  protected readonly comparisonNote = computed<UnmeasuredNote | null>(() => describeUnmeasured(this.chance().comparison));

  protected readonly profitabilityMeasured = computed(() => {
    const profitability = this.chance().profitability;
    return profitability.status === 'measured' ? profitability : null;
  });
  protected readonly profitabilityNote = computed<UnmeasuredNote | null>(() => describeUnmeasured(this.chance().profitability));
  /** The measured profitability, narrowed to the records that carry the compounded-net range (#3026). */
  protected readonly profitabilityWithRange = computed<ProfitabilityWithRange | null>(() => {
    const measured = this.profitabilityMeasured();
    return measured !== null && measured.net_return_range != null ? (measured as ProfitabilityWithRange) : null;
  });
  /** A measured reading from before #3026: counts readable, the range simply not recorded — never fabricated. */
  protected readonly rangeNotRecorded = computed(() => this.profitabilityMeasured() !== null && this.profitabilityWithRange() === null);

  protected readonly carryMeasured = computed<MeasuredChanceCarryOver | null>(() => {
    const carry = this.chance().carry_over;
    return carry.status === 'measured' ? carry : null;
  });
  protected readonly carryNote = computed<UnmeasuredNote | null>(() => describeUnmeasured(this.chance().carry_over));

  protected readonly intervalsSpec = computed(
    () => {
      const comparison = this.comparisonMeasured();
      return comparison === null ? null : finalIntervalsSpec(this.candidate(), comparison, this.candidate().key);
    },
    // Equal while it draws the same stored reading, so a study poll (new objects, same evidence) does not redraw it.
    { equal: sameChart },
  );

  protected readonly profitSpec = computed(
    () => {
      const profitability = this.profitabilityWithRange();
      return profitability === null ? null : finalProfitSpec(this.candidate(), profitability, this.candidate().key);
    },
    { equal: sameChart },
  );

  protected readonly carrySpec = computed(
    () => {
      const carry = this.carryMeasured();
      return carry === null ? null : carryOverSpec(this.candidate(), carry, this.candidate().key);
    },
    { equal: sameChart },
  );

  /** The fit-to-fit sentence, in Python's words, from this fit's own direction of the shared draw (#3029). */
  protected readonly fitToFitSentence = computed(() => {
    const fit_to_fit = this.chance().fit_to_fit;
    return fit_to_fit.status === 'measured' ? fitToFitText(fit_to_fit) : null;
  });

  /** Why this fit's fit-to-fit view could not be read, when it failed; a failure never renders as absence. */
  protected readonly fitToFitFailed = computed(() => {
    const fit_to_fit = this.chance().fit_to_fit;
    return fit_to_fit.status === 'failed' ? fit_to_fit.sentence : null;
  });

  /** A named-missing directional view — a legacy one-direction record, or a fit the pair never compared (#3029). */
  protected readonly fitToFitNote = computed(() => {
    const fit_to_fit = this.chance().fit_to_fit;
    return fit_to_fit.status === 'missing' ? fit_to_fit.reason : null;
  });

  /** True only when every component holds a named-missing record — genuinely never measured, with nothing failed. */
  protected readonly nothingMeasured = computed(
    () =>
      this.chance().comparison.status === 'missing' &&
      this.chance().profitability.status === 'missing' &&
      this.chance().carry_over.status === 'missing' &&
      this.chance().fit_to_fit.status === 'missing',
  );
}

/** A stored reading the panel cannot chart, in the server's words: failed raises an alert, the rest read as notes. */
function describeUnmeasured(reading: ChanceComparison | ChanceReading | ChanceProfitability): UnmeasuredNote | null {
  if (reading.status === 'measured') return null;
  if (reading.status === 'failed') return { kind: 'failed', text: reading.sentence };
  if (reading.status === 'missing') return { kind: 'missing', text: reading.reason };
  return { kind: 'recorded', text: reading.sentence };
}
