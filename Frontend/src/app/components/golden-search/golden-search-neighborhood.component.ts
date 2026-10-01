import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';

import { knobsByName, metricTexts } from './golden-search-display';
import type { CandidateNeighborhood, EvidenceCellStatus, StrategyCapability, StressResult } from './golden-search.types';

const STATUS_WORDS: Readonly<Record<EvidenceCellStatus, string>> = {
  center: 'This candidate',
  tested: 'Tested',
  failed: 'Run failed',
  invalid: 'Invalid',
  outside_domain: 'Outside the legal range',
  untested: 'Not tested',
};

/**
 * The candidate's nearby settings and stress runs (#2696): each searched knob
 * one step either side with the actual metrics (tested, invalid, failed and
 * untested kept apart), and the predeclared cost scenarios rerun in the
 * engine. A plateau here is a local observation, not a probability.
 */
@Component({
  selector: 'app-golden-search-neighborhood',
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './golden-search-neighborhood.component.html',
  styleUrl: './golden-search-evidence-table.scss',
})
export class GoldenSearchNeighborhoodComponent {
  readonly neighbors = input.required<readonly CandidateNeighborhood[]>();
  readonly stress = input.required<readonly StressResult[]>();
  readonly capability = input<StrategyCapability | null>(null);
  readonly candidateLabel = input.required<string>();
  /** Which half to show: the neighbor tables or the stress table. */
  readonly view = input.required<'neighbors' | 'stress'>();

  protected readonly knobTables = computed(() => {
    const knobs = knobsByName(this.capability());
    return this.neighbors().map((hood) => ({
      knob: hood.knob,
      label: knobs.get(hood.knob)?.label ?? hood.knob,
      rows: hood.rows.map((row) => {
        const figures = metricTexts(row.metrics);
        return { value: row.value, status: STATUS_WORDS[row.status], reason: row.reason ?? figures.failure, center: row.status === 'center', ...figures };
      }),
    }));
  });
  protected readonly stressRows = computed(() =>
    this.stress().map((result) => {
      const figures = metricTexts(result.metrics);
      return { key: result.scenario, label: result.label, failed: figures.failure, ...figures };
    }),
  );
}
