import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';

import { knobsByName, percentText, ratioText, signedPercentText } from './golden-search-display';
import type { CandidateNeighborhood, EvidenceCellStatus, Metrics, StrategyCapability, StressResult } from './golden-search.types';

const STATUS_WORDS: Readonly<Record<EvidenceCellStatus, string>> = {
  center: 'This candidate',
  tested: 'Tested',
  failed: 'Run failed',
  invalid: 'Invalid',
  outside_domain: 'Outside the legal range',
  untested: 'Not tested',
};

interface MetricCells {
  readonly netReturn: string;
  readonly worstFall: string;
  readonly trades: string;
  readonly sharpe: string;
}

function cells(metrics: Metrics | null): MetricCells {
  if (metrics === null || metrics.status === 'failed') return { netReturn: '—', worstFall: '—', trades: '—', sharpe: '—' };
  return { netReturn: signedPercentText(metrics.total_return_pct), worstFall: percentText(metrics.max_drawdown_pct), trades: String(metrics.total_trades), sharpe: ratioText(metrics.sharpe_ratio) };
}

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
      rows: hood.rows.map((row) => ({ value: row.value, status: STATUS_WORDS[row.status], reason: row.reason ?? (row.metrics?.status === 'failed' ? row.metrics.error : null), center: row.status === 'center', ...cells(row.metrics) })),
    }));
  });
  protected readonly stressRows = computed(() => this.stress().map((result) => ({ key: result.scenario, label: result.label, failed: result.metrics?.status === 'failed' ? (result.metrics.error ?? 'The run failed.') : null, ...cells(result.metrics) })));
}
