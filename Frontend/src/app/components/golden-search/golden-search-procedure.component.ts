import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';

import { ReceiptLabelPipe } from '../../shared/pipes/receipt-label.pipe';
import { knobsByName, pointEntries } from './golden-search-display';
import { GoldenSearchMetricsComponent } from './golden-search-metrics.component';
import type { GoldenSearchMethod, ProcedureView, StrategyCapability } from './golden-search.types';

interface TriedValue {
  readonly value: number;
  /** Null when the value's evaluation was eligible; otherwise its ineligibility code. */
  readonly ineligibility: string | null;
  readonly chosen: boolean;
}

interface PathRow {
  readonly key: string;
  readonly pass: number;
  readonly knob: string;
  readonly round: number;
  readonly low: number;
  readonly high: number;
  readonly startedAt: number;
  readonly tried: readonly TriedValue[];
  readonly skipped: readonly { value: number; reason: string }[];
  readonly chosen: number;
  readonly moved: boolean;
}

/**
 * One search procedure's evidence (#2696): the retained settings and their
 * development metrics, why it stopped, knobs that ended at the edge of their
 * searched range, and — for Zoom — the round-by-round path: the values tried
 * with their eligibility, the constraint-skipped values, the value kept, and
 * whether the knob moved. Every number is the server's.
 */
@Component({
  selector: 'app-golden-search-procedure',
  imports: [GoldenSearchMetricsComponent, ReceiptLabelPipe],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './golden-search-procedure.component.html',
  styleUrl: './golden-search-procedure.component.scss',
})
export class GoldenSearchProcedureComponent {
  readonly procedure = input.required<ProcedureView>();
  readonly heading = input.required<string>();
  readonly description = input.required<string>();
  readonly method = input.required<GoldenSearchMethod>();
  readonly capability = input<StrategyCapability | null>(null);
  /** Unique per page; ties the section to its heading. */
  readonly headingId = input.required<string>();

  protected readonly winner = computed(() => pointEntries(this.procedure().winner, this.capability()));
  protected readonly edgeHits = computed(() => {
    const knobs = knobsByName(this.capability());
    return this.procedure().edge_hits.map((name) => knobs.get(name)?.label ?? name);
  });
  protected readonly path = computed<PathRow[]>(() => {
    const knobs = knobsByName(this.capability());
    return this.procedure().rounds.map((round) => ({
      key: `${round.pass_index}:${round.knob}:${round.round_index}`,
      pass: round.pass_index + 1,
      knob: knobs.get(round.knob)?.label ?? round.knob,
      round: round.round_index + 1,
      low: round.low,
      high: round.high,
      startedAt: round.current_before,
      tried: round.results.map(([value, ineligibility]) => ({ value, ineligibility, chosen: value === round.chosen })),
      skipped: round.invalid.map(([value, reason]) => ({ value, reason })),
      chosen: round.chosen,
      moved: round.moved,
    }));
  });
}
