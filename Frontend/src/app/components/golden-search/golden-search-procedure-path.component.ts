import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';

import { ReceiptLabelPipe } from '../../shared/pipes/receipt-label.pipe';
import { knobsByName } from './golden-search-display';
import type { StrategyCapability, ZoomRound } from './golden-search.types';

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
 * A Zoom procedure's round-by-round path (#2696): for every round, the range
 * searched, where the knob started, each value tried with its eligibility,
 * the values a constraint ruled out (never evaluated), the value kept, and
 * whether the knob moved. Rows are the server's rounds, in order.
 */
@Component({
  selector: 'app-golden-search-procedure-path',
  imports: [ReceiptLabelPipe],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './golden-search-procedure-path.component.html',
  styleUrl: './golden-search-procedure.component.scss',
})
export class GoldenSearchProcedurePathComponent {
  readonly rounds = input.required<readonly ZoomRound[]>();
  readonly capability = input<StrategyCapability | null>(null);
  /** Names the scrolling region, e.g. "All-period search — search path". */
  readonly label = input.required<string>();

  protected readonly rows = computed<PathRow[]>(() => {
    const knobs = knobsByName(this.capability());
    return this.rounds().map((round) => ({
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
